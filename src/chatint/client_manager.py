import json
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Set
import openai
from chatint.db import get_providers, save_providers


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
        api_key = (provider.get("api_key") or "").strip()
        is_local = "localhost" in base_url or "127.0.0.1" in base_url

        if not api_key:
            if is_local:
                api_key = "local-no-key-required"
            else:
                return None, f"API key for {provider['name']} is missing. Please set it in Settings (Ctrl+,)."

        if not base_url:
            base_url = "https://api.openai.com/v1"

        # Local endpoints fail fast (3s), remote endpoints use specified timeout (60s)
        actual_timeout = 3.0 if is_local else timeout

        client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=actual_timeout,
            max_retries=0 if is_local else 1,
        )
        return client, None

    @classmethod
    def fetch_remote_models(
        cls, provider_id: str, timeout: float = 60.0
    ) -> tuple[List[Dict[str, Any]], Optional[str], bool]:
        """
        Fetches models.
        Returns (models_list, error_message, is_network_error).
        """
        client, err = cls.get_client(provider_id, timeout=timeout)
        if err or not client:
            return [], err, False

        try:
            model_list = client.models.list()
            models = []
            for m in model_list.data:
                mid = m.id
                m_lower = mid.lower()
                supports_thinking = (
                    1
                    if any(
                        k in m_lower
                        for k in ["o1", "o3", "o4", "r1", "qwq", "reasoner", "thinking"]
                    )
                    else 0
                )
                models.append({
                    "id": mid,
                    "name": mid,
                    "supports_thinking": supports_thinking,
                })
            models.sort(key=lambda x: x["name"])
            return models, None, False
        except openai.APIConnectionError as conn_err:
            return [], str(conn_err), True
        except openai.APITimeoutError as time_err:
            return [], "Request timed out after 60s", True
        except Exception as e:
            err_str = str(e).lower()
            is_net = any(k in err_str for k in ["connect", "dns", "no such host", "network", "timeout"])
            return [], str(e), is_net


class BackgroundModelSyncer:
    _lock = threading.Lock()
    _bridge: Optional[Any] = None
    _pending_network_retries: Set[str] = set()
    _watcher_thread: Optional[threading.Thread] = None

    @classmethod
    def set_bridge(cls, bridge: Any):
        cls._bridge = bridge

    @classmethod
    def start_background_sync(cls, bridge: Optional[Any] = None):
        """
        Launches background fetch without blocking UI startup.
        Each endpoint is fetched asynchronously with a 60s timeout.
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
        provider = ClientManager.get_provider(provider_id)
        if not provider:
            return

        base_url = (provider.get("base_url") or "").strip()
        api_key = (provider.get("api_key") or "").strip()
        is_enabled = bool(provider.get("is_enabled", True))
        is_local = "localhost" in base_url or "127.0.0.1" in base_url

        has_key = bool(api_key) or is_local
        if not is_enabled or not has_key:
            return

        # Fetch with 60s TTL (or 3s for local)
        fetched, err, is_network_error = ClientManager.fetch_remote_models(provider_id, timeout=60.0)

        if err or not fetched:
            if is_network_error and not is_local:
                with cls._lock:
                    cls._pending_network_retries.add(provider_id)
            return

        # Success: remove from pending retries
        with cls._lock:
            cls._pending_network_retries.discard(provider_id)

        # Re-read latest provider state from DB after network call to avoid overwriting concurrent user edits
        latest_provider = ClientManager.get_provider(provider_id)
        if not latest_provider:
            return

        # Sync models: retain existing models present in fetch; if none configured/retained yet, auto-fill up to 5
        fetched_ids = {m["id"] for m in fetched}
        current_models = latest_provider.get("models", [])

        retained = [m for m in current_models if m["id"] in fetched_ids]
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
        latest_provider["is_accessible"] = len(synced_models) > 0

        # Save updated provider to database atomically
        save_providers([latest_provider])

        # Push event to bridge memory queue
        if cls._bridge:
            cls._bridge.push_event({
                "type": "provider_models_updated",
                "provider": latest_provider,
            })

    @classmethod
    def _network_watcher_loop(cls):
        """
        Periodically checks connectivity and retries pending network failures.
        """
        while True:
            try:
                time.sleep(15.0)
                with cls._lock:
                    pending = list(cls._pending_network_retries)

                if not pending:
                    continue

                if _is_network_available():
                    for pid in pending:
                        threading.Thread(target=cls._sync_single_provider, args=(pid,), daemon=True).start()
            except Exception as e:
                print(f"[Watcher Exception] {e}")
