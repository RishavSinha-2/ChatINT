from __future__ import annotations

import concurrent.futures
import json
import re
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Set
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

from chatint.db import (
    PRIMARY_WEB_SEARCH_MODEL,
    get_api_key_from_env,
    get_connection,
    get_providers,
    get_web_search_models,
    sanitize_provider_name,
    save_providers,
    save_web_search_models,
    strip_model_prefix,
)


def model_recency_key(m: Any) -> tuple:
    """
    Ranks model dictionaries or IDs by estimated recency (newer models first).
    Considers explicit creation timestamp, release date patterns (YYYY-MM-DD),
    version numbers (e.g. 4.5 > 3.8 > 2.5), and capability tiers.
    """
    if isinstance(m, dict):
        mid = (m.get("id") or "").lower()
        created = m.get("created", 0)
    else:
        mid = str(m or "").lower()
        created = 0

    if not isinstance(created, (int, float)):
        created = 0

    date_val = 0
    m_date = re.search(r"(202\d)[-_]?([01]\d)[-_]?([0-3]\d)", mid)
    if m_date:
        try:
            date_val = int(m_date.group(1) + m_date.group(2) + m_date.group(3))
        except Exception:
            pass

    ver_val = 0.0
    m_dec = re.search(r"[-_v]?(\d+\.\d+)", mid)
    if m_dec:
        try:
            ver_val = float(m_dec.group(1))
        except Exception:
            pass
    else:
        m_dash = re.search(r"[-_](\d+)[-_](\d+)(?:[-_]|$)", mid)
        if m_dash and int(m_dash.group(1)) <= 10:
            ver_val = float(f"{m_dash.group(1)}.{m_dash.group(2)}")
        else:
            m_single = re.search(r"(?:gpt|gemini|claude|llama|deepseek|qwen|v)[-_]?(\d+)", mid)
            if m_single:
                try:
                    ver_val = float(m_single.group(1))
                except Exception:
                    pass

    bonus = 0
    if any(k in mid for k in ["o3", "o1", "r1", "reasoner"]):
        bonus += 50
    if any(k in mid for k in ["4o", "4.5", "argon", "carbon"]):
        bonus += 40
    if any(k in mid for k in ["latest", "preview"]):
        bonus += 20
    if any(k in mid for k in ["deprecated", "legacy", "0301", "0314", "0613"]):
        bonus -= 100
    if any(k in mid for k in ["embedding", "moderation", "tts", "whisper", "audio", "realtime"]):
        bonus -= 500

    return (created, date_val, ver_val, bonus)


def sort_models_newer_first(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(models, key=model_recency_key, reverse=True)


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
    def _resolve_provider_auth(cls, provider_id: str) -> tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        provider = cls.get_provider(provider_id)
        if not provider:
            return None, None, f"Provider '{provider_id}' not found."
        if not provider.get("is_enabled", True):
            return None, None, f"Provider '{provider['name']}' is currently disabled in Settings."
        api_key = get_api_key_from_env(provider.get("name", ""))
        if not api_key:
            return None, None, f"API key for {provider['name']} is missing in .env. Please set it in Settings (Ctrl+,)."
        return provider, api_key, None

    @classmethod
    def get_anthropic_client(
        cls, provider_id: str, timeout: float = 60.0
    ) -> tuple[Optional[anthropic.Anthropic], Optional[str]]:
        import anthropic
        import httpx2

        _, api_key, err = cls._resolve_provider_auth(provider_id)
        if err:
            return None, err

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
        from google import genai
        from google.genai import types as genai_types

        _, api_key, err = cls._resolve_provider_auth(provider_id)
        if err:
            return None, err

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
        import httpx
        import openai

        provider, api_key, err = cls._resolve_provider_auth(provider_id)
        if err:
            return None, err

        base_url = (provider.get("base_url") or "").strip() or "https://api.openai.com/v1"
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
                    mid = strip_model_prefix(raw_name)
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
                items = []
                for m in model_list.data:
                    mid = (getattr(m, "id", "") or "").strip()
                    if mid:
                        created = getattr(m, "created", 0) or 0
                        items.append({"id": mid, "created": created})
                return items

            fetch_fn = _do_fetch_openai

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(fetch_fn)
        try:
            raw_items = future.result(timeout=timeout)
            executor.shutdown(wait=False)
            seen = set()
            models = []
            for item in raw_items:
                if isinstance(item, dict):
                    mid = (item.get("id") or "").strip()
                    created = item.get("created", 0)
                else:
                    mid = str(item).strip()
                    created = 0
                if mid and mid not in seen:
                    seen.add(mid)
                    is_thinking = 1 if any(t in mid.lower() for t in ["o1", "o3", "r1", "reasoning", "thinking"]) else 0
                    models.append({
                        "id": mid,
                        "name": mid,
                        "supports_thinking": is_thinking,
                        "input_price": 0.0,
                        "cached_input_price": 0.0,
                        "output_price": 0.0,
                        "created": created,
                    })
            models = sort_models_newer_first(models)
            if models:
                BackgroundModelSyncer.mark_provider_verified(provider_id, {m["id"] for m in models})
                BackgroundModelSyncer.set_sync_status(provider_id, "ready", "")
            else:
                BackgroundModelSyncer.clear_provider_verified(provider_id)
                BackgroundModelSyncer.set_sync_status(provider_id, "error", "NO MODELS")
            return models, None, False
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False, cancel_futures=True)
            BackgroundModelSyncer.clear_provider_verified(provider_id)
            BackgroundModelSyncer.set_sync_status(provider_id, "network_error", "NETWORK ERROR")
            return [], f"Request timed out after {int(timeout)}s", True
        except Exception as e:
            executor.shutdown(wait=False)
            BackgroundModelSyncer.clear_provider_verified(provider_id)
            err_str = str(e).lower()
            err_type = type(e).__name__.lower()
            is_net = (
                "connection" in err_type
                or "timeout" in err_type
                or any(k in err_str for k in ["connect", "dns", "no such host", "network", "timeout", "unreachable", "refused", "reset"])
            )
            is_timeout = "timeout" in err_type or "timed out" in err_str
            msg = f"Request timed out after {int(timeout)}s" if is_timeout else str(e)
            status, brief = BackgroundModelSyncer.classify_sync_error(msg, is_net)
            BackgroundModelSyncer.set_sync_status(provider_id, status, brief)
            return [], msg, is_net


class BackgroundModelSyncer:
    _lock = threading.Lock()
    _bridge: Optional[Any] = None
    _pending_network_retries: Set[str] = set()
    _retry_backoff: Dict[str, tuple[int, float]] = {}
    _last_emitted_sig: Dict[str, tuple] = {}
    _in_flight: Set[str] = set()
    _verified_providers: Set[str] = set()
    _verified_model_ids: Dict[str, Set[str]] = {}
    _sync_status: Dict[str, tuple[str, str]] = {}
    _watcher_thread: Optional[threading.Thread] = None

    @staticmethod
    def classify_sync_error(err_msg: str, is_network_error: bool = False) -> tuple[str, str]:
        """Classify a provider sync error into a short UI status code and hover reason."""
        if is_network_error:
            return "network_error", "NETWORK ERROR"
        low = (err_msg or "").lower()
        if any(
            tok in low
            for tok in (
                "api_key_invalid",
                "api key not valid",
                "invalid api key",
                "incorrect api key",
                "unauthorized",
                "authentication",
                "permission denied",
                "401",
                "403",
            )
        ):
            return "invalid_key", "INVALID KEY"
        if "no api key" in low or "missing" in low:
            return "no_key", "NO API KEY"
        return "error", "FETCH ERROR"

    @classmethod
    def set_sync_status(cls, provider_id: str, status: str, reason: str = ""):
        with cls._lock:
            cls._sync_status[provider_id] = (status, reason)

    @classmethod
    def get_sync_status(
        cls, provider_id: str, has_api_key: Optional[bool] = None, is_enabled: Optional[bool] = None
    ) -> tuple[str, str]:
        if is_enabled is False:
            return "disabled", "GEMINI DISABLED" if provider_id == "gemini" else "DISABLED"
        if has_api_key is False:
            return "no_key", "NO API KEY"
        with cls._lock:
            if provider_id in cls._sync_status:
                return cls._sync_status[provider_id]
            if provider_id in cls._verified_providers:
                return "ready", ""
        return ("checking", "CHECKING...") if has_api_key else ("no_key", "NO API KEY")

    @classmethod
    def report_runtime_error(cls, provider_id: str, err_msg: str, is_network_error: bool = False):
        """Update sync status if a runtime tool call (e.g. Web Search) hits an auth or network failure."""
        status, brief = cls.classify_sync_error(err_msg, is_network_error)
        cls.set_sync_status(provider_id, status, brief)
        if status in ("invalid_key", "no_key", "network_error"):
            cls.clear_provider_verified(provider_id)
        from chatint.db import get_providers

        prov = next((p for p in get_providers() if p["id"] == provider_id), None)
        if prov:
            cls._emit_provider_update(prov)

    @classmethod
    def _emit_provider_update(cls, provider_dict: Dict[str, Any]):
        pid = provider_dict.get("id", "")
        sig = (
            bool(provider_dict.get("is_accessible")),
            bool(provider_dict.get("has_api_key")),
            bool(provider_dict.get("is_enabled")),
            provider_dict.get("sync_status", ""),
            provider_dict.get("sync_error", ""),
            tuple(
                (
                    m.get("id"),
                    m.get("name"),
                    m.get("input_price"),
                    m.get("output_price"),
                    m.get("cached_input_price"),
                    m.get("supports_thinking"),
                )
                for m in (provider_dict.get("models") or [])
            ),
            tuple(provider_dict.get("fetched_model_ids") or []),
            tuple(
                (m.get("id"), m.get("input_price"), m.get("cached_input_price"), m.get("output_price"))
                for m in (provider_dict.get("web_search_models") or [])
            ),
        )
        with cls._lock:
            if cls._last_emitted_sig.get(pid) == sig:
                return
            cls._last_emitted_sig[pid] = sig
        if cls._bridge:
            cls._bridge.push_event({
                "type": "provider_models_updated",
                "provider": provider_dict,
            })

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
            cls._last_emitted_sig.pop(provider_id, None)

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
            cls._retry_backoff.clear()
            if not cls._watcher_thread or not cls._watcher_thread.is_alive():
                cls._watcher_thread = threading.Thread(target=cls._network_watcher_loop, daemon=True)
                cls._watcher_thread.start()

        providers = get_providers()
        for p in providers:
            if p.get("is_enabled") and p.get("has_api_key") and p.get("sync_status") != "ready":
                cls.set_sync_status(p["id"], "checking", "CHECKING...")
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
                status, brief = cls.get_sync_status(provider_id, has_api_key=bool(api_key), is_enabled=is_enabled)
                cls.set_sync_status(provider_id, status, brief)
                with cls._lock:
                    cls._pending_network_retries.discard(provider_id)
                    cls._retry_backoff.pop(provider_id, None)
                provider["has_api_key"] = bool(api_key)
                provider["is_accessible"] = False
                provider["fetched_model_ids"] = []
                provider["sync_status"] = status
                provider["sync_error"] = brief
                cls._emit_provider_update(provider)
                return

            # Fetch with strict 60s TTL
            fetched, err, is_network_error = ClientManager.fetch_remote_models(provider_id, timeout=60.0)

            if err or not fetched:
                cls.clear_provider_verified(provider_id)
                status, brief = cls.get_sync_status(provider_id, has_api_key=True, is_enabled=True)
                latest_failed = ClientManager.get_provider(provider_id)
                if latest_failed:
                    latest_failed["is_accessible"] = False
                    latest_failed["fetched_model_ids"] = []
                    latest_failed["sync_status"] = status
                    latest_failed["sync_error"] = brief
                    cls._emit_provider_update(latest_failed)
                if is_network_error:
                    with cls._lock:
                        prev_count, _ = cls._retry_backoff.get(provider_id, (0, 0.0))
                        new_count = prev_count + 1
                        delay = min(300.0, 10.0 * (2 ** (new_count - 1)))
                        cls._retry_backoff[provider_id] = (new_count, time.monotonic() + delay)
                        cls._pending_network_retries.add(provider_id)
                return

            # Success: remove from pending retries and mark verified
            fetched_ids = {m["id"] for m in fetched}
            with cls._lock:
                cls._pending_network_retries.discard(provider_id)
                cls._retry_backoff.pop(provider_id, None)
                cls._verified_providers.add(provider_id)
                cls._verified_model_ids[provider_id] = set(fetched_ids)
                cls._sync_status[provider_id] = ("ready", "")

            # Re-read latest provider state from DB after network call to avoid overwriting concurrent user edits
            conn = get_connection()
            row = conn.execute("SELECT models_json FROM providers WHERE id = ?", (provider_id,)).fetchone()
            conn.close()
            stored_models = json.loads(row["models_json"]) if row and row["models_json"] else []

            latest_provider = ClientManager.get_provider(provider_id)
            if not latest_provider:
                return

            # Sync models: retain existing configured models present in fetch; if any slots are empty (< 5), auto-fill from unconfigured fetched, newer ones first
            clean_fetched_ids = {strip_model_prefix(mid) for mid in fetched_ids}
            retained = [
                m
                for m in stored_models
                if m.get("id") in fetched_ids or strip_model_prefix(m.get("id", "")) in clean_fetched_ids
            ]
            retained_ids = {m["id"] for m in retained}
            clean_retained_ids = {strip_model_prefix(mid) for mid in retained_ids}

            if len(retained) < 5:
                unconfigured = [
                    fm
                    for fm in fetched
                    if fm["id"] not in retained_ids and strip_model_prefix(fm["id"]) not in clean_retained_ids
                ]
                for fm in sort_models_newer_first(unconfigured):
                    if len(retained) >= 5:
                        break
                    retained.append(fm)
                    retained_ids.add(fm["id"])
                    clean_retained_ids.add(strip_model_prefix(fm["id"]))

            synced_models = retained[:5]
            latest_provider["models"] = synced_models
            latest_provider["fetched_model_ids"] = sorted(list(fetched_ids))
            latest_provider["is_accessible"] = len(synced_models) > 0
            latest_provider["sync_status"] = "ready"
            latest_provider["sync_error"] = ""

            # Save updated provider to database atomically
            save_providers([latest_provider])

            # Keep Web Search fallback models pruned against fetched Gemini models (no auto-add, fixed primary)
            if provider_id == "gemini":
                ws_models = get_web_search_models()
                kept_ws = [
                    m
                    for m in ws_models[1:]
                    if m.get("id") in fetched_ids or strip_model_prefix(m.get("id", "")) in clean_fetched_ids
                ]
                latest_provider["web_search_models"] = save_web_search_models([ws_models[0], *kept_ws])

            # Push event to bridge memory queue if provider state actually changed
            cls._emit_provider_update(latest_provider)
        finally:
            with cls._lock:
                cls._in_flight.discard(provider_id)

    @classmethod
    def _network_watcher_loop(cls):
        """
        Periodically checks connectivity and retries pending network failures with exponential backoff.
        """
        while True:
            try:
                time.sleep(5.0)
                now = time.monotonic()
                with cls._lock:
                    due = [
                        pid for pid in cls._pending_network_retries
                        if cls._retry_backoff.get(pid, (0, 0.0))[1] <= now
                    ]

                if not due:
                    continue

                if _is_network_available():
                    for pid in due:
                        threading.Thread(target=cls._sync_single_provider, args=(pid,), daemon=True).start()
            except Exception as e:
                print(f"[Watcher Exception] {e}")
