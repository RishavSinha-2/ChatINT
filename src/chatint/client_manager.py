from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set
import urllib.parse
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

from chatint.catalog import (
    is_gemini_3_or_newer,
    model_recency_key,
    sort_models_newer_first,
    strip_model_prefix,
)
from chatint.db import (
    get_api_key_from_env,
    get_providers,
    get_web_search_models,
    is_saved_secret_non_placeholder,
    open_db,
    sanitize_provider_name,
    save_web_search_models,
    update_provider_models,
)
from chatint.logging_setup import get_logger

_logger = get_logger("chatint.client_manager")

_DEFAULT_PROVIDER_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "ollama": "http://localhost:11434/v1",
}


def normalize_endpoint_origin(url: Optional[str], provider_id: str = "") -> tuple[str, str, int]:
    """Normalize an endpoint URL to its canonical (scheme, host, port) origin for host-bound key checks."""
    raw = (url or "").strip()
    if not raw:
        raw = _DEFAULT_PROVIDER_URLS.get((provider_id or "").strip().lower(), "https://api.openai.com/v1")
    if "://" not in raw:
        lowered = raw.lower()
        default_scheme = (
            "http"
            if lowered.startswith(("localhost", "127.0.0.1", "[::1]"))
            else "https"
        )
        raw = f"{default_scheme}://{raw}"
    parsed = urllib.parse.urlparse(raw)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if host in ("127.0.0.1", "::1"):
        norm_host = "localhost"
    else:
        norm_host = host
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        port = 443 if scheme == "https" else 80
    return (scheme, norm_host, port)


def is_loopback_endpoint(url: Optional[str]) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urllib.parse.urlparse(raw)
    return (parsed.hostname or "").lower() in ("localhost", "127.0.0.1", "::1")


def _is_network_available(base_urls: Optional[Iterable[str]] = None) -> bool:
    """Fast check whether local/proxy/internet connectivity is available without leaking sockets."""
    for raw_url in base_urls or ():
        url = (raw_url or "").strip()
        if not url:
            continue
        try:
            parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
            host = parsed.hostname
            if not host:
                continue
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except OSError:
            pass

    for proxy_var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        proxy_val = (os.environ.get(proxy_var) or "").strip()
        if not proxy_val:
            continue
        try:
            parsed = urllib.parse.urlparse(proxy_val if "://" in proxy_val else f"http://{proxy_val}")
            if parsed.hostname and parsed.port:
                with socket.create_connection((parsed.hostname, parsed.port), timeout=1.5):
                    return True
        except OSError:
            pass

    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2.0):
            return True
    except OSError:
        pass
    try:
        socket.gethostbyname("api.openai.com")
        return True
    except OSError:
        return False


def _is_transport_exception(exc: Exception) -> bool:
    """Determine if an exception represents a transport/network/timeout error from SDK classes or text."""
    for mod_name, cls_names in (
        ("httpx", ("TransportError", "ConnectError", "TimeoutException")),
        ("httpx2", ("TransportError", "ConnectError", "TimeoutException")),
        ("openai", ("APIConnectionError", "APITimeoutError")),
        ("anthropic", ("APIConnectionError", "APITimeoutError")),
    ):
        try:
            mod = __import__(mod_name)
            for cname in cls_names:
                cls_obj = getattr(mod, cname, None)
                if cls_obj and isinstance(exc, cls_obj):
                    return True
        except ImportError:
            pass

    err_str = str(exc).lower()
    err_type = type(exc).__name__.lower()
    return (
        "connection" in err_type
        or "timeout" in err_type
        or "transport" in err_type
        or any(
            k in err_str
            for k in (
                "connect",
                "dns",
                "no such host",
                "network",
                "timeout",
                "timed out",
                "unreachable",
                "refused",
                "reset",
            )
        )
    )


class ClientManager:
    @staticmethod
    def close_client(client: Any) -> None:
        if client is None:
            return
        try:
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                close_fn()
        except Exception as exc:
            _logger.debug("Ignored error closing SDK client: %s", exc)

    @classmethod
    def get_provider(cls, provider_id: str) -> Optional[Dict[str, Any]]:
        providers = get_providers()
        for p in providers:
            if p["id"] == provider_id:
                return p
        return None

    @classmethod
    def get_sdk_type(cls, provider: Optional[Dict[str, Any]], provider_id: str = "") -> str:
        """Determine which SDK adapter to use: 'anthropic', 'gemini', or 'openai'."""
        pid = (provider.get("id") if provider else provider_id or "").strip().lower()
        pname = sanitize_provider_name(provider.get("name", "") if provider else "").lower()
        if pid == "anthropic" or pname == "anthropic":
            return "anthropic"
        if pid == "gemini" or pname == "gemini":
            return "gemini"
        return "openai"

    @classmethod
    def validate_provider_ready(cls, provider_id: str) -> Optional[str]:
        """Verify that a saved provider is enabled and has credentials configured before destructive edits."""
        _, _, err = cls._resolve_provider_auth(provider_id)
        return err

    @classmethod
    def _resolve_provider_auth(
        cls,
        provider_id: str,
        override_api_key: Optional[str] = None,
        override_base_url: Optional[str] = None,
        override_is_enabled: Optional[bool] = None,
        clear_api_key: bool = False,
        provider_name: Optional[str] = None,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        all_providers = get_providers()
        db_provider = next((p for p in all_providers if p["id"] == provider_id), None)

        if db_provider is not None:
            provider = dict(db_provider)
            if provider_name and sanitize_provider_name(provider_name):
                provider["name"] = sanitize_provider_name(provider_name)
            sdk_type = cls.get_sdk_type(provider, provider_id)
            saved_base_url = str(db_provider.get("base_url") or "").strip()
            if override_base_url is not None:
                effective_base_url = str(override_base_url).strip()
                provider["base_url"] = effective_base_url
                origin_changed = sdk_type == "openai" and (
                    normalize_endpoint_origin(saved_base_url, provider_id)
                    != normalize_endpoint_origin(effective_base_url, provider_id)
                )
            else:
                effective_base_url = saved_base_url
                origin_changed = False
        else:
            clean_name = sanitize_provider_name(provider_name or provider_id) or "Provider"
            effective_base_url = str(override_base_url or "").strip()
            provider = {
                "id": provider_id,
                "name": clean_name,
                "base_url": effective_base_url,
                "is_enabled": True,
            }
            sdk_type = cls.get_sdk_type(provider, provider_id)
            # Prevent unsaved custom provider rows from borrowing an existing provider's .env key on a new host
            matching_named = next(
                (p for p in all_providers if sanitize_provider_name(p.get("name", "")).upper() == clean_name.upper()),
                None,
            )
            if matching_named is not None and sdk_type == "openai":
                origin_changed = normalize_endpoint_origin(
                    matching_named.get("base_url"), matching_named["id"]
                ) != normalize_endpoint_origin(effective_base_url, provider_id)
            else:
                origin_changed = False

        is_enabled = provider.get("is_enabled", True) if override_is_enabled is None else bool(override_is_enabled)
        has_override_key = override_api_key is not None and str(override_api_key).strip() != ""
        if not is_enabled and not has_override_key:
            return None, None, f"Provider '{provider['name']}' is currently disabled in Settings."

        if clear_api_key and not has_override_key:
            if sdk_type == "openai" and is_loopback_endpoint(effective_base_url):
                return provider, "ollama", None
            return None, None, f"API key for {provider['name']} is cleared. Enter an API key before fetching."

        if has_override_key:
            return provider, str(override_api_key), None

        # Host-bound secret guard (C2): never transmit a saved .env key to a different base_url host!
        if origin_changed:
            if sdk_type == "openai" and is_loopback_endpoint(effective_base_url):
                return provider, "ollama", None
            saved_lookup_name = db_provider.get("name", "") if db_provider else provider.get("name", "")
            saved_secret = get_api_key_from_env(saved_lookup_name)
            if is_saved_secret_non_placeholder(saved_secret):
                return (
                    None,
                    None,
                    f"Base URL host changed for {provider['name']}. Please enter the API key for the new endpoint "
                    "(saved keys are never sent to a different host).",
                )
            return None, None, f"API key for {provider['name']} is required for this endpoint."

        lookup_name = db_provider.get("name", "") if db_provider else provider.get("name", "")
        api_key = get_api_key_from_env(lookup_name)
        if not api_key and sdk_type == "openai" and is_loopback_endpoint(effective_base_url):
            api_key = "ollama"
        if not api_key:
            return (
                None,
                None,
                f"API key for {provider['name']} is missing in .env. Please set it in Settings (Ctrl+,).",
            )
        return provider, api_key, None

    @classmethod
    def get_anthropic_client(
        cls,
        provider_id: str,
        timeout: float = 60.0,
        api_key: Optional[str] = None,
        is_enabled: Optional[bool] = None,
        clear_api_key: bool = False,
        provider_name: Optional[str] = None,
    ) -> tuple[Optional[Any], Optional[str]]:
        import anthropic
        import httpx2

        _, key, err = cls._resolve_provider_auth(
            provider_id,
            override_api_key=api_key,
            override_is_enabled=is_enabled,
            clear_api_key=clear_api_key,
            provider_name=provider_name,
        )
        if err:
            return None, err

        client = anthropic.Anthropic(
            api_key=key,
            timeout=httpx2.Timeout(timeout, connect=min(10.0, timeout)),
            max_retries=0,
        )
        return client, None

    @classmethod
    def get_gemini_client(
        cls,
        provider_id: str,
        timeout: float = 60.0,
        api_key: Optional[str] = None,
        is_enabled: Optional[bool] = None,
        clear_api_key: bool = False,
        provider_name: Optional[str] = None,
    ) -> tuple[Optional[Any], Optional[str]]:
        from google import genai
        from google.genai import types as genai_types

        _, key, err = cls._resolve_provider_auth(
            provider_id,
            override_api_key=api_key,
            override_is_enabled=is_enabled,
            clear_api_key=clear_api_key,
            provider_name=provider_name,
        )
        if err:
            return None, err

        timeout_ms = int(max(1.0, timeout) * 1000)
        client = genai.Client(
            api_key=key,
            http_options=genai_types.HttpOptions(timeout=timeout_ms),
        )
        return client, None

    @classmethod
    def get_client(
        cls,
        provider_id: str,
        timeout: float = 60.0,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        is_enabled: Optional[bool] = None,
        clear_api_key: bool = False,
        provider_name: Optional[str] = None,
    ) -> tuple[Optional[Any], Optional[str]]:
        """Return (openai_client, error_message)."""
        import httpx
        import openai

        provider, key, err = cls._resolve_provider_auth(
            provider_id,
            override_api_key=api_key,
            override_base_url=base_url,
            override_is_enabled=is_enabled,
            clear_api_key=clear_api_key,
            provider_name=provider_name,
        )
        if err or not provider:
            return None, err

        raw_url = provider.get("base_url") if base_url is None else base_url
        base_url_val = (
            str(raw_url or "").strip()
            or _DEFAULT_PROVIDER_URLS.get(provider_id.lower(), "https://api.openai.com/v1")
        )
        client = openai.OpenAI(
            base_url=base_url_val,
            api_key=key,
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            max_retries=0,
        )
        return client, None

    @classmethod
    def fetch_remote_models(
        cls,
        provider_id: str,
        timeout: float = 60.0,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        is_enabled: Optional[bool] = None,
        clear_api_key: bool = False,
        update_sync_state: bool = True,
        provider_name: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], Optional[str], bool]:
        """
        Fetch models with a strict wall-clock timeout using a daemon worker thread (fixes M6).
        The SDK client is closed inside the worker's own `finally:` block so a wall-clock timeout
        never closes `httpx` out from under an active read or blocks Python's `atexit` shutdown.
        """
        from chatint.providers import get_provider_adapter

        provider = cls.get_provider(provider_id)
        if not provider:
            provider = {
                "id": provider_id,
                "name": sanitize_provider_name(provider_name or provider_id) or "Provider",
                "is_enabled": True,
            }

        sdk_type = cls.get_sdk_type(provider, provider_id)
        adapter = get_provider_adapter(sdk_type)
        per_call_timeout = max(1.0, min(float(timeout), 15.0))

        if sdk_type == "anthropic":
            client, err = cls.get_anthropic_client(
                provider_id,
                timeout=per_call_timeout,
                api_key=api_key,
                is_enabled=is_enabled,
                clear_api_key=clear_api_key,
                provider_name=provider_name,
            )
        elif sdk_type == "gemini":
            client, err = cls.get_gemini_client(
                provider_id,
                timeout=per_call_timeout,
                api_key=api_key,
                is_enabled=is_enabled,
                clear_api_key=clear_api_key,
                provider_name=provider_name,
            )
        else:
            client, err = cls.get_client(
                provider_id,
                timeout=per_call_timeout,
                api_key=api_key,
                base_url=base_url,
                is_enabled=is_enabled,
                clear_api_key=clear_api_key,
                provider_name=provider_name,
            )

        if err or not client:
            return [], err, False

        result_box: Dict[str, Any] = {}

        def _worker() -> None:
            try:
                result_box["items"] = adapter.list_raw_models(client)
            except Exception as exc:
                result_box["error"] = exc
            finally:
                cls.close_client(client)

        worker = threading.Thread(
            target=_worker,
            daemon=True,
            name=f"model-fetch-{provider_id}",
        )
        worker.start()
        worker.join(timeout=max(0.1, float(timeout)))

        if worker.is_alive():
            if update_sync_state:
                BackgroundModelSyncer.clear_provider_verified(provider_id)
                BackgroundModelSyncer.set_sync_status(provider_id, "network_error", "NETWORK ERROR")
            return [], f"Request timed out after {int(timeout)}s", True

        if "error" in result_box:
            e = result_box["error"]
            err_str = str(e).lower()
            err_type = type(e).__name__.lower()
            is_net = _is_transport_exception(e)
            is_timeout = "timeout" in err_type or "timed out" in err_str
            msg = f"Request timed out after {int(timeout)}s" if is_timeout else str(e)
            if update_sync_state:
                BackgroundModelSyncer.clear_provider_verified(provider_id)
                status, brief = BackgroundModelSyncer.classify_sync_error(msg, is_net)
                BackgroundModelSyncer.set_sync_status(provider_id, status, brief)
            return [], msg, is_net

        raw_items = result_box.get("items") or []
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
                is_thinking = 1 if any(t in mid.lower() for t in ("o1", "o3", "r1", "reasoning", "thinking")) else 0
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
        if update_sync_state:
            if models:
                BackgroundModelSyncer.mark_provider_verified(provider_id, {m["id"] for m in models})
                BackgroundModelSyncer.set_sync_status(provider_id, "ready", "")
            else:
                BackgroundModelSyncer.clear_provider_verified(provider_id)
                BackgroundModelSyncer.set_sync_status(provider_id, "error", "NO MODELS")
        return models, None, False


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
        if "no api key" in low or "missing" in low or "cleared" in low or "required" in low:
            return "no_key", "NO API KEY"
        return "error", "FETCH ERROR"

    @classmethod
    def set_sync_status(cls, provider_id: str, status: str, reason: str = "") -> None:
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
    def report_runtime_error(cls, provider_id: str, err_msg: str, is_network_error: bool = False) -> None:
        """Update sync status if a runtime tool call hits an auth or network failure."""
        status, brief = cls.classify_sync_error(err_msg, is_network_error)
        cls.set_sync_status(provider_id, status, brief)
        if status in ("invalid_key", "no_key", "network_error"):
            cls.clear_provider_verified(provider_id)
        prov = next((p for p in get_providers() if p["id"] == provider_id), None)
        if prov:
            cls._emit_provider_update(prov)

    @classmethod
    def _emit_provider_update(cls, provider_dict: Dict[str, Any]) -> None:
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
            ev = {
                "type": "provider_models_updated",
                "provider": provider_dict,
            }
            if hasattr(cls._bridge, "_push_event"):
                cls._bridge._push_event(ev)
            elif hasattr(cls._bridge, "push_event"):
                cls._bridge.push_event(ev)

    @classmethod
    def is_provider_verified(cls, provider_id: str) -> bool:
        with cls._lock:
            return provider_id in cls._verified_providers

    @classmethod
    def get_verified_model_ids(cls, provider_id: str) -> Set[str]:
        with cls._lock:
            return set(cls._verified_model_ids.get(provider_id, set()))

    @classmethod
    def mark_provider_verified(cls, provider_id: str, model_ids: Optional[Set[str]] = None) -> None:
        with cls._lock:
            cls._verified_providers.add(provider_id)
            if model_ids is not None:
                cls._verified_model_ids[provider_id] = set(model_ids)

    @classmethod
    def clear_provider_verified(cls, provider_id: str) -> None:
        with cls._lock:
            cls._verified_providers.discard(provider_id)
            cls._verified_model_ids.pop(provider_id, None)
            cls._last_emitted_sig.pop(provider_id, None)

    @classmethod
    def set_bridge(cls, bridge: Any) -> None:
        cls._bridge = bridge

    @classmethod
    def start_background_sync(
        cls, bridge: Optional[Any] = None, provider_ids: Optional[Iterable[str]] = None
    ) -> None:
        if bridge:
            cls._bridge = bridge

        target_ids = set(provider_ids) if provider_ids is not None else None

        with cls._lock:
            if target_ids is None:
                cls._retry_backoff.clear()
            else:
                for pid in target_ids:
                    cls._retry_backoff.pop(pid, None)
            if not cls._watcher_thread or not cls._watcher_thread.is_alive():
                cls._watcher_thread = threading.Thread(target=cls._network_watcher_loop, daemon=True)
                cls._watcher_thread.start()

        if target_ids is not None and not target_ids:
            return

        providers = get_providers()
        for p in providers:
            if target_ids is not None and p["id"] not in target_ids:
                continue
            if p.get("is_enabled") and p.get("has_api_key") and p.get("sync_status") != "ready":
                cls.set_sync_status(p["id"], "checking", "CHECKING...")
            threading.Thread(target=cls._sync_single_provider, args=(p["id"],), daemon=True).start()

    @classmethod
    def _sync_single_provider(cls, provider_id: str) -> None:
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

            fetched_ids = {m["id"] for m in fetched}
            with cls._lock:
                cls._pending_network_retries.discard(provider_id)
                cls._retry_backoff.pop(provider_id, None)
                cls._verified_providers.add(provider_id)
                cls._verified_model_ids[provider_id] = set(fetched_ids)
                cls._sync_status[provider_id] = ("ready", "")

            with open_db() as conn:
                row = conn.execute("SELECT models_json FROM providers WHERE id = ?", (provider_id,)).fetchone()
            stored_models = json.loads(row["models_json"]) if row and row["models_json"] else []

            latest_provider = ClientManager.get_provider(provider_id)
            if not latest_provider:
                return

            clean_fetched_lower = {strip_model_prefix(mid).lower() for mid in fetched_ids}
            retained = [
                m
                for m in stored_models
                if m.get("id") in fetched_ids or strip_model_prefix(m.get("id", "")).lower() in clean_fetched_lower
            ]
            synced_models = retained[:5]
            latest_provider["models"] = synced_models
            latest_provider["fetched_model_ids"] = sorted(list(fetched_ids))
            latest_provider["is_accessible"] = len(synced_models) > 0
            latest_provider["sync_status"] = "ready"
            latest_provider["sync_error"] = ""

            update_provider_models(provider_id, synced_models)

            if provider_id == "gemini":
                ws_models = get_web_search_models()
                kept_ws = [
                    m
                    for m in ws_models[1:]
                    if m.get("id") in fetched_ids or strip_model_prefix(m.get("id", "")).lower() in clean_fetched_lower
                ]
                latest_provider["web_search_models"] = save_web_search_models([ws_models[0], *kept_ws])

            cls._emit_provider_update(latest_provider)
        finally:
            with cls._lock:
                cls._in_flight.discard(provider_id)

    @classmethod
    def _network_watcher_loop(cls) -> None:
        while True:
            try:
                time.sleep(5.0)
                now = time.monotonic()
                with cls._lock:
                    due = [
                        pid
                        for pid in cls._pending_network_retries
                        if cls._retry_backoff.get(pid, (0, 0.0))[1] <= now
                    ]

                if not due:
                    continue

                base_urls = []
                try:
                    due_set = set(due)
                    base_urls = [
                        p.get("base_url", "")
                        for p in get_providers()
                        if p.get("id") in due_set and p.get("base_url")
                    ]
                except Exception as exc:
                    _logger.debug("Could not read provider base_urls in network watcher: %s", exc)

                if _is_network_available(base_urls=base_urls):
                    for pid in due:
                        threading.Thread(target=cls._sync_single_provider, args=(pid,), daemon=True).start()
            except Exception as e:
                _logger.warning("Watcher loop exception: %s", e)
