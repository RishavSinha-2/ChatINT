import concurrent.futures
import json
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Set
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

import anthropic
from google import genai
from google.genai import types as genai_types
import httpx
import httpx2
import openai
from chatint.db import get_api_key_from_env, get_providers, sanitize_provider_name, save_providers


def _is_network_available() -> bool:
    """
    Fast check if internet connectivity is available.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("1.1.1.1", 53))
        s.close()
        return True
    except Exception:
        pass
    try:
        socket.gethostbyname("api.openai.com")
        return True
    except Exception:
        return False


class ClientManager:
    @classmethod
    def get_provider(cls, provider_id: str) -> Optional[Dict[str, Any]]:
        providers = get_providers()
        for p in providers:
            if p["id"] == provider_id:
                return p
        return None

    @classmethod
    def get_sdk_type(cls, provider: Optional[Dict[str, Any]], provider_id: str = "") -> str:
        """
        Determines which SDK to use: 'anthropic', 'gemini', or 'openai'.
        """
        pid = (provider.get("id") if provider else provider_id or "").strip().lower()
        pname = sanitize_provider_name(provider.get("name", "") if provider else "").lower()
        if pid == "anthropic" or pname == "anthropic":
            return "anthropic"
        if pid == "gemini" or pname == "gemini":
            return "gemini"
        return "openai"

    @classmethod
    def get_anthropic_client(
        cls, provider_id: str, timeout: float = 60.0
    ) -> tuple[Optional[anthropic.Anthropic], Optional[str]]:
        provider = cls.get_provider(provider_id)
        if not provider:
            return None, f"Provider '{provider_id}' not found."
        if not provider.get("is_enabled", True):
            return None, f"Provider '{provider['name']}' is currently disabled in Settings."
        api_key = get_api_key_from_env(provider.get("name", ""))
        if not api_key:
            return None, f"API key for {provider['name']} is missing in .env. Please set it in Settings (Ctrl+,)."

        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=httpx2.Timeout(timeout, connect=min(15.0, timeout)),
            max_retries=0,
        )
        return client, None

    @classmethod
    def get_gemini_client(
        cls, provider_id: str, timeout: float = 60.0
    ) -> tuple[Optional[genai.Client], Optional[str]]:
        provider = cls.get_provider(provider_id)
        if not provider:
            return None, f"Provider '{provider_id}' not found."
        if not provider.get("is_enabled", True):
            return None, f"Provider '{provider['name']}' is currently disabled in Settings."
        api_key = get_api_key_from_env(provider.get("name", ""))
        if not api_key:
            return None, f"API key for {provider['name']} is missing in .env. Please set it in Settings (Ctrl+,)."

        timeout_ms = int(max(1.0, timeout) * 1000)
        client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=timeout_ms),
        )
        return client, None

    @classmethod
    def get_client(cls, provider_id: str, timeout: float = 60.0) -> tuple[Optional[openai.OpenAI], Optional[str]]:
        """
        Returns (openai_client, error_message).
        """
        provider = cls.get_provider(provider_id)
        if not provider:
            return None, f"Provider '{provider_id}' not found."

        if not provider.get("is_enabled", True):
            return None, f"Provider '{provider['name']}' is currently disabled in Settings."

        base_url = (provider.get("base_url") or "").strip()
        api_key = get_api_key_from_env(provider.get("name", ""))

        if not api_key:
            return None, f"API key for {provider['name']} is missing in .env. Please set it in Settings (Ctrl+,)."

        if not base_url:
            base_url = "https://api.openai.com/v1"

        client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
            max_retries=0,
        )
        return client, None

    @classmethod
    def fetch_remote_models(
        cls, provider_id: str, timeout: float = 60.0
    ) -> tuple[List[Dict[str, Any]], Optional[str], bool]:
        """
        Fetches models with a strict wall-clock TTL (default 60s).
        Uses native SDK for Anthropic and Gemini, and OpenAI SDK for all other providers.
        Returns (models_list, error_message, is_network_error).
        """
        provider = cls.get_provider(provider_id)
        if not provider:
            return [], f"Provider '{provider_id}' not found.", False

        sdk_type = cls.get_sdk_type(provider, provider_id)

        if sdk_type == "anthropic":
            client, err = cls.get_anthropic_client(provider_id, timeout=timeout)
            if err or not client:
                return [], err, False

            def _do_fetch_anthropic():
                page = client.models.list(limit=100)
                items = getattr(page, "data", None) or list(page)
                result = []
                for m in items:
                    mid = (getattr(m, "id", "") or "").strip()
                    if mid:
                        result.append(mid)
                return result

            fetch_fn = _do_fetch_anthropic

        elif sdk_type == "gemini":
            client, err = cls.get_gemini_client(provider_id, timeout=timeout)
            if err or not client:
                return [], err, False

            def _do_fetch_gemini():
                result = []
                for m in client.models.list():
                    raw_name = (getattr(m, "name", "") or "").strip()
                    if not raw_name:
                        continue
                    mid = raw_name[len("models/"):] if raw_name.startswith("models/") else raw_name
                    actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None)
                    if actions and isinstance(actions, (list, tuple, set)):
                        if not any("generateContent" in str(a) or "generate" in str(a).lower() for a in actions):
                            continue
                    if any(skip in mid.lower() for skip in ("embedding", "aqa", "imagen", "veo")):
                        continue
                    result.append(mid)
                return result

            fetch_fn = _do_fetch_gemini

        else:
            client, err = cls.get_client(provider_id, timeout=timeout)
            if err or not client:
                return [], err, False

            def _do_fetch_openai():
                model_list = client.models.list()
                return [(getattr(m, "id", "") or "").strip() for m in model_list.data if getattr(m, "id", "")]

            fetch_fn = _do_fetch_openai

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(fetch_fn)
        try:
            model_ids = future.result(timeout=timeout)
            executor.shutdown(wait=False)
            seen = set()
            models = []
            for mid in model_ids:
                if mid and mid not in seen:
                    seen.add(mid)
                    models.append({
                        "id": mid,
                        "name": mid,
                        "supports_thinking": 0,
                        "input_price": 0.0,
                        "cached_input_price": 0.0,
                        "output_price": 0.0,
                    })
            models.sort(key=lambda x: x["name"])
            if models:
                BackgroundModelSyncer.mark_provider_verified(provider_id, {m["id"] for m in models})
            else:
                BackgroundModelSyncer.clear_provider_verified(provider_id)
            return models, None, False
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False, cancel_futures=True)
            BackgroundModelSyncer.clear_provider_verified(provider_id)
            return [], f"Request timed out after {int(timeout)}s", True
        except (openai.APIConnectionError, anthropic.APIConnectionError) as conn_err:
            executor.shutdown(wait=False)
            BackgroundModelSyncer.clear_provider_verified(provider_id)
            return [], str(conn_err), True
        except (openai.APITimeoutError, anthropic.APITimeoutError) as time_err:
            executor.shutdown(wait=False)
            BackgroundModelSyncer.clear_provider_verified(provider_id)
            return [], f"Request timed out after {int(timeout)}s", True
        except Exception as e:
            executor.shutdown(wait=False)
            BackgroundModelSyncer.clear_provider_verified(provider_id)
            err_str = str(e).lower()
            is_net = any(k in err_str for k in ["connect", "dns", "no such host", "network", "timeout", "unreachable", "refused", "reset"])
            return [], str(e), is_net


class BackgroundModelSyncer:
    _lock = threading.Lock()
    _bridge: Optional[Any] = None
    _pending_network_retries: Set[str] = set()
    _in_flight: Set[str] = set()
    _verified_providers: Set[str] = set()
    _verified_model_ids: Dict[str, Set[str]] = {}
    _watcher_thread: Optional[threading.Thread] = None

    @classmethod
    def is_provider_verified(cls, provider_id: str) -> bool:
        with cls._lock:
            return provider_id in cls._verified_providers

    @classmethod
    def get_verified_model_ids(cls, provider_id: str) -> Set[str]:
        with cls._lock:
            return set(cls._verified_model_ids.get(provider_id, set()))

    @classmethod
    def mark_provider_verified(cls, provider_id: str, model_ids: Optional[Set[str]] = None):
        with cls._lock:
            cls._verified_providers.add(provider_id)
            if model_ids is not None:
                cls._verified_model_ids[provider_id] = set(model_ids)

    @classmethod
    def clear_provider_verified(cls, provider_id: str):
        with cls._lock:
            cls._verified_providers.discard(provider_id)
            cls._verified_model_ids.pop(provider_id, None)

    @classmethod
    def set_bridge(cls, bridge: Any):
        cls._bridge = bridge

    @classmethod
    def start_background_sync(cls, bridge: Optional[Any] = None):
        """
        Launches background fetch without blocking UI startup.
        Each endpoint with an API key is fetched asynchronously with a 60s TTL.
        """
        if bridge:
            cls._bridge = bridge

        with cls._lock:
            if not cls._watcher_thread or not cls._watcher_thread.is_alive():
                cls._watcher_thread = threading.Thread(target=cls._network_watcher_loop, daemon=True)
                cls._watcher_thread.start()

        providers = get_providers()
        for p in providers:
            threading.Thread(target=cls._sync_single_provider, args=(p["id"],), daemon=True).start()

    @classmethod
    def _sync_single_provider(cls, provider_id: str):
        with cls._lock:
            if provider_id in cls._in_flight:
                return
            cls._in_flight.add(provider_id)

        try:
            provider = ClientManager.get_provider(provider_id)
            if not provider:
                return

            api_key = get_api_key_from_env(provider.get("name", ""))
            is_enabled = bool(provider.get("is_enabled", True))

            # Strictly only fetch from endpoints that are enabled and have an API key associated with them
            if not is_enabled or not api_key:
                cls.clear_provider_verified(provider_id)
                provider["models"] = []
                provider["fetched_model_ids"] = []
                provider["has_api_key"] = bool(api_key)
                provider["is_accessible"] = False
                if not api_key:
                    save_providers([provider])
                if cls._bridge:
                    cls._bridge.push_event({
                        "type": "provider_models_updated",
                        "provider": provider,
                    })
                return

            # Fetch with strict 60s TTL
            fetched, err, is_network_error = ClientManager.fetch_remote_models(provider_id, timeout=60.0)

            if err or not fetched:
                cls.clear_provider_verified(provider_id)
                latest_failed = ClientManager.get_provider(provider_id)
                if latest_failed:
                    latest_failed["models"] = []
                    latest_failed["fetched_model_ids"] = []
                    latest_failed["is_accessible"] = False
                    if cls._bridge:
                        cls._bridge.push_event({
                            "type": "provider_models_updated",
                            "provider": latest_failed,
                        })
                if is_network_error:
                    with cls._lock:
                        cls._pending_network_retries.add(provider_id)
                return

            # Success: remove from pending retries and mark verified
            fetched_ids = {m["id"] for m in fetched}
            with cls._lock:
                cls._pending_network_retries.discard(provider_id)
                cls._verified_providers.add(provider_id)
                cls._verified_model_ids[provider_id] = set(fetched_ids)

            # Re-read latest provider state from DB after network call to avoid overwriting concurrent user edits
            from chatint.db import get_connection
            conn = get_connection()
            row = conn.execute("SELECT models_json FROM providers WHERE id = ?", (provider_id,)).fetchone()
            conn.close()
            stored_models = json.loads(row["models_json"]) if row and row["models_json"] else []

            latest_provider = ClientManager.get_provider(provider_id)
            if not latest_provider:
                return

            # Sync models: retain existing configured models present in fetch; if none retained, auto-fill up to 5 from fetched
            retained = [m for m in stored_models if m.get("id") in fetched_ids]
            retained_ids = {m["id"] for m in retained}

            if len(retained) == 0:
                for fm in fetched:
                    if len(retained) >= 5:
                        break
                    if fm["id"] not in retained_ids:
                        retained.append(fm)
                        retained_ids.add(fm["id"])

            synced_models = retained[:5]
            latest_provider["models"] = synced_models
            latest_provider["fetched_model_ids"] = sorted(list(fetched_ids))
            latest_provider["is_accessible"] = len(synced_models) > 0

            # Save updated provider to database atomically
            save_providers([latest_provider])

            # Push event to bridge memory queue so dropdown updates in real-time
            if cls._bridge:
                cls._bridge.push_event({
                    "type": "provider_models_updated",
                    "provider": latest_provider,
                })
        finally:
            with cls._lock:
                cls._in_flight.discard(provider_id)

    @classmethod
    def _network_watcher_loop(cls):
        """
        Periodically checks connectivity and retries pending network failures with 60s TTL.
        """
        while True:
            try:
                time.sleep(5.0)
                with cls._lock:
                    pending = list(cls._pending_network_retries)

                if not pending:
                    continue

                if _is_network_available():
                    for pid in pending:
                        threading.Thread(target=cls._sync_single_provider, args=(pid,), daemon=True).start()
            except Exception as e:
                print(f"[Watcher Exception] {e}")
