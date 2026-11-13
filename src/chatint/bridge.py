from __future__ import annotations

import collections
import functools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional
import urllib.parse
import urllib.request
import uuid
import webbrowser

import webview

from chatint.client_manager import (
    BackgroundModelSyncer,
    ClientManager,
    is_loopback_endpoint,
    normalize_endpoint_origin,
)
from chatint.db import (
    add_message,
    create_conversation,
    delete_conversation,
    delete_messages_from,
    delete_provider as db_delete_provider,
    duplicate_conversation as db_duplicate_conversation,
    get_api_key_from_env,
    get_app_settings,
    get_conversation,
    get_conversations,
    get_google_search_request_totals,
    get_providers,
    get_tabs_state,
    get_token_stats,
    get_web_search_models,
    import_conversation_data as db_import_conversation_data,
    is_default_conversation_title,
    is_saved_secret_non_placeholder,
    is_valid_provider_name,
    mask_api_key,
    open_db,
    save_app_settings,
    save_providers,
    save_tabs_state,
    save_web_search_models,
    update_conversation,
    update_message,
)
from chatint.logging_setup import get_logger, get_logs_dir
from chatint.services.import_export import (
    format_conversation_markdown,
    parse_chat_file_content,
    read_chat_file_safely,
)
from chatint.streamer import StreamManager, stream_chat_completion

_logger = get_logger("chatint.bridge")
_VALID_THINKING_LEVELS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}

PUBLIC_RPC_METHODS: tuple[str, ...] = (
    "return_to_chat",
    "open_external_url",
    "open_logs_folder",
    "get_pending_events",
    "get_initial_data",
    "add_provider",
    "delete_provider",
    "get_provider_api_key",
    "get_conversation_token_stats",
    "set_theme",
    "save_settings",
    "fetch_provider_models",
    "create_conversation",
    "get_conversations",
    "get_conversation",
    "rename_conversation",
    "duplicate_conversation",
    "open_import_chat_dialog",
    "import_conversation_content",
    "update_conversation_settings",
    "delete_conversation",
    "export_conversation",
    "save_tabs_state",
    "send_message",
    "regenerate_message",
    "edit_message",
    "stop_streaming",
)


def _clamp_max_context(value: Any, default: int = 30) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(500, parsed))


def _normalize_thinking_level(level: Any) -> str:
    val = str(level or "none").strip().lower()
    return val if val in _VALID_THINKING_LEVELS else "none"


def _normalize_file_uri_key(url: str) -> str:
    parsed = urllib.parse.urlparse((url or "").strip())
    raw_path = urllib.parse.unquote(parsed.path or "")
    try:
        fs_path = Path(urllib.request.url2pathname(raw_path))
        if fs_path.exists():
            return str(fs_path.resolve()).replace("\\", "/").lower()
    except Exception:
        pass
    norm = re.sub(r"^/+([a-zA-Z]:/)", r"\1", raw_path.replace("\\", "/"))
    return norm.rstrip("/").lower()


def wrap_rpc_envelope(raw_result: Any) -> Dict[str, Any]:
    """Normalize any DesktopBridge return payload into a uniform `{ok, data, error}` DTO envelope."""
    if (
        isinstance(raw_result, dict)
        and "ok" in raw_result
        and "data" in raw_result
        and "error" in raw_result
        and isinstance(raw_result["ok"], bool)
    ):
        return raw_result

    if isinstance(raw_result, dict):
        # `fetch_provider_models` returns preview metadata including `models` + `sync_status` + optional `error`
        if "models" in raw_result and "sync_status" in raw_result:
            err_val = raw_result.get("error")
            return {
                "ok": not bool(err_val),
                "data": raw_result,
                "error": str(err_val) if err_val else None,
            }
        if raw_result.get("success") is False or (raw_result.get("error") and raw_result.get("success") is not True):
            err_msg = str(raw_result.get("error") or "Operation failed.")
            return {
                "ok": False,
                "data": raw_result,
                "error": err_msg,
            }

    return {
        "ok": True,
        "data": raw_result,
        "error": None,
    }


def _guarded_rpc(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        self._require_trusted_origin()
        return fn(self, *args, **kwargs)

    return wrapper


def make_public_bridge_api(bridge: DesktopBridge) -> Any:
    """
    Build a sealed JS-facing RPC object exposing ONLY whitelisted `PUBLIC_RPC_METHODS`.
    - Contains zero instance attributes (`__slots__ = ()`), closing over `bridge` in function cells.
    - Blocks attribute traversal (`__getattribute__` whitelist) so `pywebview`'s `get_nested_attribute`
      cannot reach `attach_window`, `set_origin_trusted`, `push_event`, `_window`, or dunder internals.
    - Wraps every RPC response in a uniform `{"ok": bool, "data": ..., "error": Optional[str]}` DTO.
    """
    allowed_set = frozenset(PUBLIC_RPC_METHODS)
    namespace: Dict[str, Any] = {"__slots__": ()}

    def _make_endpoint(method_name: str):
        target_unbound = getattr(DesktopBridge, method_name)

        @functools.wraps(target_unbound)
        def _rpc_endpoint(self, *args, **kwargs):
            try:
                raw = getattr(bridge, method_name)(*args, **kwargs)
                return wrap_rpc_envelope(raw)
            except PermissionError:
                raise
            except Exception as exc:
                _logger.warning("RPC error in %s: %s", method_name, exc)
                return {"ok": False, "data": None, "error": str(exc)}

        return _rpc_endpoint

    for m_name in PUBLIC_RPC_METHODS:
        namespace[m_name] = _make_endpoint(m_name)

    def _dir(self) -> List[str]:
        return list(PUBLIC_RPC_METHODS)

    def _getattribute(self, name: str) -> Any:
        if name in allowed_set or name == "__class__":
            return object.__getattribute__(self, name)
        raise AttributeError(f"RPC attribute '{name}' is not exposed.")

    namespace["__dir__"] = _dir
    namespace["__getattribute__"] = _getattribute

    SealedBridgeApi = type("PublicBridgeApi", (object,), namespace)
    return SealedBridgeApi()


def read_window_current_url(window: Any) -> str:
    """
    Read a pywebview Window's current URL without blocking on `window.events.loaded.wait(20)`.
    In pywebview, `Window.get_current_url()` is decorated with `@_loaded_call`, which deadlocks
    the WinForms STA UI thread if invoked during `before_load` or before `loaded` is set.
    """
    if window is None:
        return ""
    if isinstance(window, webview.Window):
        gui = getattr(window, "gui", None)
        uid = getattr(window, "uid", None)
        if gui is not None and isinstance(uid, str) and hasattr(gui, "get_current_url"):
            try:
                url = gui.get_current_url(uid)
                if isinstance(url, str) and url.strip():
                    return url.strip()
            except Exception:
                pass
        events = getattr(window, "events", None)
        loaded_ev = getattr(events, "loaded", None) if events is not None else None
        if loaded_ev is not None and hasattr(loaded_ev, "is_set") and not loaded_ev.is_set():
            real_url = getattr(window, "real_url", None)
            return real_url.strip() if isinstance(real_url, str) else ""
    try:
        raw = window.get_current_url()
        return str(raw).strip() if raw else ""
    except Exception:
        return ""


class DesktopBridge:
    def __init__(self, *, allow_headless_rpc: bool = True):
        self._event_queue = collections.deque()
        self._queue_lock = threading.Lock()
        self._window: Optional[Any] = None
        self._window_ever_attached: bool = False
        self._allow_headless_rpc: bool = bool(allow_headless_rpc)
        self._index_url: str = ""
        self._allowed_origin: Optional[tuple[str, str, Optional[int]]] = None
        self._origin_trusted: bool = True
        self._public_api: Optional[Any] = None
        self._tabs_lock = threading.Lock()
        self._latest_tabs_state: Optional[Dict[str, Any]] = None
        self._tabs_state_dirty: bool = False

    def as_public_api(self) -> Any:
        """Return the sealed `PublicBridgeApi` instance safe to pass to `webview.create_window(js_api=...)`."""
        if self._public_api is None:
            self._public_api = make_public_bridge_api(self)
        return self._public_api

    def _attach_window(
        self,
        window: Any,
        index_url: str,
        allowed_origin: Optional[tuple] = None,
    ) -> None:
        if window is not None:
            self._window_ever_attached = True
        self._window = window
        self._index_url = (index_url or "").strip()
        self._origin_trusted = True
        if allowed_origin is not None:
            if len(allowed_origin) == 2:
                scheme_s, netloc_s = str(allowed_origin[0]).lower(), str(allowed_origin[1]).lower()
                parsed_nl = urllib.parse.urlparse(f"{scheme_s}://{netloc_s}") if netloc_s else None
                host_s = (parsed_nl.hostname or "").lower() if parsed_nl else ""
                port_i = parsed_nl.port if parsed_nl else None
                self._allowed_origin = (scheme_s, host_s, port_i)
            else:
                self._allowed_origin = (
                    str(allowed_origin[0]).lower(),
                    str(allowed_origin[1] or "").lower(),
                    allowed_origin[2],
                )
        elif self._index_url:
            parsed = urllib.parse.urlparse(self._index_url)
            if parsed.scheme:
                self._allowed_origin = (
                    parsed.scheme.lower(),
                    (parsed.hostname or "").lower(),
                    parsed.port,
                )

    # Python-side backwards-compatible alias for unit tests (never exposed on PublicBridgeApi)
    attach_window = _attach_window

    def _set_origin_trusted(self, trusted: bool) -> None:
        self._origin_trusted = bool(trusted)

    set_origin_trusted = _set_origin_trusted

    def _is_url_trusted(self, current_url: str) -> bool:
        if not current_url:
            return False
        parsed = urllib.parse.urlparse(current_url.strip())
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError:
            return False

        if self._allowed_origin is None:
            return bool(self._index_url) and current_url.strip() == self._index_url

        allowed_scheme, allowed_host, allowed_port = self._allowed_origin

        if allowed_scheme == "file":
            if scheme != "file":
                return False
            if not self._index_url:
                return False
            return _normalize_file_uri_key(current_url) == _normalize_file_uri_key(self._index_url)

        if scheme != allowed_scheme or host != allowed_host or port != allowed_port:
            return False

        clean_path = urllib.parse.unquote(parsed.path or "").replace("\\", "/")
        return clean_path in ("", "/", "/index.html")

    def _require_trusted_origin(self) -> None:
        if self._window is None:
            if self._window_ever_attached or not self._allow_headless_rpc:
                raise PermissionError("Blocked RPC call: window not attached.")
            return
        if not self._origin_trusted:
            raise PermissionError("Blocked RPC call from untrusted window state.")
        current_url = read_window_current_url(self._window)
        # Fail CLOSED on empty/missing URL or origin mismatch (fixes C1 fail-open bug)
        if not current_url or not self._is_url_trusted(current_url):
            _logger.warning("Blocked RPC from untrusted or empty URL: %r", current_url)
            raise PermissionError("Blocked RPC call from untrusted origin.")

    @_guarded_rpc
    def return_to_chat(self) -> Dict[str, Any]:
        """Navigate the main WebView window back to the local ChatInt UI."""
        if self._window and self._index_url:
            threading.Timer(0.05, self._window.load_url, args=(self._index_url,)).start()
            return {"success": True}
        return {"success": False, "error": "Window not attached"}

    @_guarded_rpc
    def open_external_url(self, url: str = "") -> Dict[str, Any]:
        """Validate and open an HTTP/HTTPS/mailto URL in the system's default handler."""
        target = (url or "").strip()
        if not target or any(ch.isspace() or ord(ch) < 32 for ch in target):
            return {"success": False, "error": "Invalid URL."}
        parsed = urllib.parse.urlparse(target)
        scheme = parsed.scheme.lower()
        if scheme in ("http", "https"):
            if not parsed.netloc:
                return {"success": False, "error": "Invalid HTTP/HTTPS URL."}
        elif scheme == "mailto":
            if not parsed.path or "@" not in parsed.path:
                return {"success": False, "error": "Invalid mailto address."}
        else:
            return {"success": False, "error": "Only HTTP, HTTPS, or mailto links can be opened externally."}
        webbrowser.open(target)
        return {"success": True}

    @_guarded_rpc
    def open_logs_folder(self) -> Dict[str, Any]:
        """Open the application logs directory (`data/logs`) in the OS file manager."""
        logs_dir = get_logs_dir()
        try:
            if sys.platform == "win32":
                os.startfile(str(logs_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(logs_dir)])
            else:
                subprocess.Popen(["xdg-open", str(logs_dir)])
            return {"success": True, "path": str(logs_dir)}
        except Exception as exc:
            _logger.warning("Failed to open logs folder %s: %s", logs_dir, exc)
            return {"success": False, "error": f"Could not open logs folder: {exc}", "path": str(logs_dir)}

    def _push_event(self, event_data: Dict[str, Any]) -> None:
        with self._queue_lock:
            self._event_queue.append(event_data)

    push_event = _push_event

    @_guarded_rpc
    def get_pending_events(self) -> List[Dict[str, Any]]:
        """Drain queued events, coalescing consecutive chunks for the same tab, conversation, and stream_id."""
        with self._queue_lock:
            raw_events = list(self._event_queue)
            self._event_queue.clear()

        if not raw_events:
            return []

        coalesced: List[Dict[str, Any]] = []
        for ev in raw_events:
            ev_type = ev.get("type")
            if (
                coalesced
                and ev_type in ("content_chunk", "thinking_chunk")
                and coalesced[-1].get("type") == ev_type
                and coalesced[-1].get("tab_id") == ev.get("tab_id")
                and coalesced[-1].get("conversation_id") == ev.get("conversation_id")
                and coalesced[-1].get("stream_id") == ev.get("stream_id")
            ):
                coalesced[-1]["chunk"] = coalesced[-1].get("chunk", "") + ev.get("chunk", "")
            else:
                coalesced.append(ev)
        return coalesced

    @staticmethod
    def _build_context_messages(conv: Dict[str, Any]) -> List[Dict[str, str]]:
        system_prompt = (conv.get("system_prompt") or "").strip()
        if not system_prompt:
            settings = get_app_settings()
            system_prompt = (settings.get("global_system_prompt") or "").strip()

        max_context = _clamp_max_context(conv.get("max_context_messages", 30), default=30)
        all_msgs = [
            m
            for m in conv.get("messages", [])
            if m.get("role") != "assistant"
            or (m.get("content") or "").strip()
            or (m.get("thinking_content") or "").strip()
        ]

        history = all_msgs[-max_context:] if max_context > 0 else all_msgs
        while len(history) > 1 and history[0].get("role") == "assistant":
            history = history[1:]

        context_msgs: List[Dict[str, str]] = []
        if system_prompt:
            context_msgs.append({"role": "system", "content": system_prompt})

        for m in history:
            role = m.get("role", "user")
            raw_content = m.get("content", "")
            content = raw_content if (raw_content or "").strip() else ("[Response stopped]" if role == "assistant" else "")
            if not content:
                continue
            if context_msgs and context_msgs[-1]["role"] == role and role in ("user", "assistant"):
                if content != context_msgs[-1]["content"]:
                    context_msgs[-1]["content"] = context_msgs[-1]["content"] + "\n\n" + content
            else:
                context_msgs.append({"role": role, "content": content})

        return context_msgs

    @_guarded_rpc
    def get_initial_data(self) -> Dict[str, Any]:
        providers = get_providers()
        settings = get_app_settings()
        conversations = get_conversations()
        tabs_state = get_tabs_state()
        BackgroundModelSyncer.start_background_sync(self)
        return {
            "providers": providers,
            "settings": settings,
            "conversations": conversations,
            "tabs_state": tabs_state,
            "google_search_totals": get_google_search_request_totals(),
            "web_search_models": get_web_search_models(),
            "logs_dir": str(get_logs_dir()),
        }

    @_guarded_rpc
    def add_provider(self, provider: Dict[str, Any]) -> Dict[str, Any]:
        """Add a validated custom provider (fixes NEW-M6)."""
        if not isinstance(provider, dict):
            return {"success": False, "error": "Invalid provider payload."}
        pname = str(provider.get("name") or "").strip()
        if not is_valid_provider_name(pname):
            return {
                "success": False,
                "error": (
                    f"Invalid provider name '{pname}'. Provider names may only contain English alphabet letters "
                    "(A-Z, a-z) with no spaces, dashes, underscores, numbers, or special characters."
                ),
            }
        providers = get_providers()
        if any((p.get("name") or "").strip().upper() == pname.upper() for p in providers):
            return {
                "success": False,
                "error": f"Duplicate provider name '{pname}'. No two providers can share the same name.",
            }
        new_id = str(provider.get("id") or f"provider_{uuid.uuid4().hex[:10]}").strip()
        clean_entry = {
            "id": new_id,
            "name": pname,
            "base_url": str(provider.get("base_url") or "").strip(),
            "api_key": provider.get("api_key", ""),
            "is_enabled": bool(provider.get("is_enabled", True)),
            "models": provider.get("models") if isinstance(provider.get("models"), list) else [],
        }
        providers.append(clean_entry)
        save_providers(providers)
        BackgroundModelSyncer.start_background_sync(self, provider_ids=[new_id])
        return {"success": True, "providers": get_providers()}

    @_guarded_rpc
    def delete_provider(self, provider_id: str) -> Dict[str, Any]:
        db_delete_provider(provider_id)
        return {"success": True, "providers": get_providers()}

    @_guarded_rpc
    def get_provider_api_key(self, provider_name: str) -> Dict[str, Any]:
        """Return only a non-sensitive masked hint for a saved key (never the raw secret)."""
        raw = get_api_key_from_env(provider_name)
        return {"has_api_key": bool(raw), "masked_hint": mask_api_key(raw) if raw else ""}

    @_guarded_rpc
    def get_conversation_token_stats(self, conv_id: str) -> Dict[str, Any]:
        return get_token_stats(conv_id)

    @_guarded_rpc
    def set_theme(self, theme: str) -> Dict[str, Any]:
        clean_theme = "light" if str(theme or "").strip().lower() == "light" else "dark"
        save_app_settings({"theme": clean_theme})
        return {"success": True, "theme": clean_theme}

    @_guarded_rpc
    def save_settings(
        self,
        providers: List[Dict[str, Any]],
        settings: Dict[str, str],
        web_search_models: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(providers, list) or not isinstance(settings, dict):
            return {"success": False, "error": "Invalid settings payload."}

        old_providers = get_providers()
        old_by_id = {p["id"]: p for p in old_providers}

        seen_names = set()
        for p in providers:
            if not isinstance(p, dict) or not p.get("id"):
                return {"success": False, "error": "Invalid provider entry."}
            pname = (p.get("name") or "").strip()
            if not is_valid_provider_name(pname):
                return {
                    "success": False,
                    "error": (
                        f"Invalid provider name '{pname}'. Provider names may only contain English alphabet letters "
                        "(A-Z, a-z) with no spaces, dashes, underscores, numbers, or special characters."
                    ),
                }
            upper_name = pname.upper()
            if upper_name in seen_names:
                return {
                    "success": False,
                    "error": (
                        f"Duplicate provider name '{pname}'. No two providers can have the same name (case-insensitive)."
                    ),
                }
            seen_names.add(upper_name)

        changed_provider_ids: List[str] = []
        for p in providers:
            pid = p["id"]
            old_p = old_by_id.get(pid)
            new_enabled = bool(p.get("is_enabled", True))
            pname = (p.get("name") or "").strip()
            is_native = pid.lower() in ("anthropic", "gemini") or pname.lower() in ("anthropic", "gemini")
            new_url = "" if is_native else str(p.get("base_url") or "").strip()

            has_new_typed_key = p.get("api_key") is not None and str(p.get("api_key")).strip() != ""
            is_clearing_key = bool(p.get("clear_api_key")) and not has_new_typed_key
            key_changed = has_new_typed_key or is_clearing_key

            # Host-bound secret protection (C2): block changing base_url host while keeping an existing saved secret!
            if old_p is not None and not is_native:
                old_origin = normalize_endpoint_origin(old_p.get("base_url"), pid)
                new_origin = normalize_endpoint_origin(new_url, pid)
                if old_origin != new_origin and not has_new_typed_key and not is_clearing_key:
                    saved_secret = get_api_key_from_env(old_p.get("name", ""))
                    if is_saved_secret_non_placeholder(saved_secret) and not is_loopback_endpoint(new_url):
                        return {
                            "success": False,
                            "error": (
                                f"Cannot change Base URL host for '{pname}' while keeping its existing saved API key. "
                                "Please enter the API key for the new endpoint or click 'Clear Key' first."
                            ),
                        }

            url_changed = bool(old_p and not is_native and new_url != (old_p.get("base_url") or ""))
            name_changed = bool(old_p and pname.upper() != (old_p.get("name") or "").upper())
            enabled_changed = bool(old_p and new_enabled != bool(old_p.get("is_enabled", True)))
            needs_initial_verify = bool(
                new_enabled
                and old_p
                and old_p.get("has_api_key")
                and not BackgroundModelSyncer.is_provider_verified(pid)
                and not is_clearing_key
            )

            if old_p is None or key_changed or url_changed or name_changed or enabled_changed or needs_initial_verify:
                changed_provider_ids.append(pid)

        try:
            save_app_settings(settings)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        save_providers(providers)
        saved_ws = (
            save_web_search_models(web_search_models)
            if web_search_models is not None
            else get_web_search_models()
        )
        BackgroundModelSyncer.start_background_sync(self, provider_ids=changed_provider_ids)
        return {"success": True, "providers": get_providers(), "web_search_models": saved_ws}

    @_guarded_rpc
    def fetch_provider_models(
        self,
        provider_id: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        is_enabled: Optional[bool] = None,
        clear_api_key: bool = False,
        provider_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        kw: Dict[str, Any] = {
            "timeout": 60.0,
            "api_key": api_key,
            "base_url": base_url,
            "is_enabled": is_enabled,
            "clear_api_key": bool(clear_api_key),
            "update_sync_state": False,
        }
        if provider_name is not None:
            kw["provider_name"] = provider_name

        models, err, is_net = ClientManager.fetch_remote_models(provider_id, **kw)
        if err:
            sync_status, sync_error = BackgroundModelSyncer.classify_sync_error(err, is_net)
        elif not models:
            sync_status, sync_error = "error", "NO MODELS"
        else:
            sync_status, sync_error = "ready", ""
        return {
            "models": models,
            "fetched_model_ids": [m["id"] for m in models],
            "error": err,
            "sync_status": sync_status,
            "sync_error": sync_error,
        }

    @_guarded_rpc
    def create_conversation(
        self, title: str = "New Chat", system_prompt: str = "", max_context: int = 30
    ) -> Dict[str, Any]:
        return create_conversation(
            title=title,
            system_prompt=system_prompt,
            max_context=_clamp_max_context(max_context, default=30),
        )

    @_guarded_rpc
    def get_conversations(self) -> List[Dict[str, Any]]:
        return get_conversations()

    @_guarded_rpc
    def get_conversation(self, conv_id: str) -> Optional[Dict[str, Any]]:
        conv = get_conversation(conv_id)
        if conv:
            conv["token_stats"] = get_token_stats(conv_id)
        return conv

    @_guarded_rpc
    def rename_conversation(self, conv_id: str, title: str) -> Dict[str, Any]:
        resolved_title = update_conversation(conv_id, title=title)
        return {"success": True, "title": resolved_title or title}

    @_guarded_rpc
    def duplicate_conversation(self, conv_id: str) -> Dict[str, Any]:
        conv = db_duplicate_conversation(conv_id)
        if not conv:
            return {"error": "Conversation not found"}
        return {"success": True, "conversation": conv}

    @_guarded_rpc
    def open_import_chat_dialog(self) -> Dict[str, Any]:
        if webview.windows:
            try:
                win = webview.windows[0]
                dialog_type = getattr(webview, "FileDialog", None)
                open_mode = dialog_type.OPEN if dialog_type and hasattr(dialog_type, "OPEN") else webview.OPEN_DIALOG
                file_types = (
                    "Supported Files (*.json;*.md)",
                    "JSON Files (*.json)",
                    "Markdown Files (*.md)",
                    "All files (*.*)",
                )
                selected = win.create_file_dialog(open_mode, file_types=file_types)
                if not selected:
                    return {"cancelled": True}
                if isinstance(selected, (list, tuple)):
                    if len(selected) == 0:
                        return {"cancelled": True}
                    selected = selected[0]

                p = Path(str(selected))
                content = read_chat_file_safely(p)
                parsed = parse_chat_file_content(content, filename=p.name)
                new_conv = db_import_conversation_data(
                    title=parsed["title"],
                    system_prompt=parsed["system_prompt"],
                    max_context=parsed["max_context"],
                    messages=parsed["messages"],
                    created_at=parsed.get("created_at"),
                )
                return {"success": True, "conversation": new_conv}
            except Exception as e:
                return {"error": f"Failed to open file: {e}"}
        return {"fallback": True}

    @_guarded_rpc
    def import_conversation_content(self, content: str, filename: str = "") -> Dict[str, Any]:
        try:
            parsed = parse_chat_file_content(content, filename=filename)
            new_conv = db_import_conversation_data(
                title=parsed["title"],
                system_prompt=parsed["system_prompt"],
                max_context=parsed["max_context"],
                messages=parsed["messages"],
                created_at=parsed.get("created_at"),
            )
            return {"success": True, "conversation": new_conv}
        except Exception as e:
            return {"error": f"Failed to import chat: {e}"}

    @_guarded_rpc
    def update_conversation_settings(
        self, conv_id: str, system_prompt: str, max_context_messages: int
    ) -> Dict[str, Any]:
        update_conversation(
            conv_id,
            system_prompt=system_prompt,
            max_context_messages=_clamp_max_context(max_context_messages, default=30),
        )
        return {"success": True}

    @_guarded_rpc
    def delete_conversation(self, conv_id: str, only_if_empty: bool = False) -> Dict[str, Any]:
        if not only_if_empty:
            StreamManager.abort_conversation(conv_id)
        deleted = delete_conversation(conv_id, only_if_empty=bool(only_if_empty))
        return {"success": True, "deleted": deleted}

    @_guarded_rpc
    def export_conversation(self, conv_id: str, format_type: str = "markdown") -> Dict[str, Any]:
        conv = get_conversation(conv_id)
        if not conv:
            return {"error": "Conversation not found."}

        if format_type == "json":
            content = json.dumps(conv, indent=2)
            ext = "json"
            file_types = ("JSON Files (*.json)", "All files (*.*)")
        else:
            content = format_conversation_markdown(conv)
            ext = "md"
            file_types = ("Markdown Files (*.md)", "All files (*.*)")

        safe_title = re.sub(r'[\\/*?:"<>|]+', "", (conv.get("title") or "conversation")).strip()
        safe_title = re.sub(r"\s+", "_", safe_title)[:48] or f"conversation_{conv_id[:8]}"
        default_filename = f"{safe_title}.{ext}"

        if webview.windows:
            try:
                win = webview.windows[0]
                dialog_type = getattr(webview, "FileDialog", None)
                save_mode = dialog_type.SAVE if dialog_type and hasattr(dialog_type, "SAVE") else webview.SAVE_DIALOG
                save_path = win.create_file_dialog(
                    save_mode,
                    save_filename=default_filename,
                    file_types=file_types,
                )
                if not save_path:
                    return {"cancelled": True}
                if isinstance(save_path, (list, tuple)):
                    if len(save_path) == 0:
                        return {"cancelled": True}
                    save_path = save_path[0]

                target_path = Path(str(save_path))
                if not target_path.suffix:
                    target_path = target_path.with_suffix(f".{ext}")

                target_path.write_text(content, encoding="utf-8")
                return {
                    "saved": True,
                    "path": str(target_path),
                    "format": format_type,
                }
            except Exception as e:
                return {"error": f"Failed to save file: {e}"}

        return {"content": content, "format": format_type, "filename": default_filename}

    @_guarded_rpc
    def save_tabs_state(self, tabs_state: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(tabs_state, dict):
            with self._tabs_lock:
                self._latest_tabs_state = tabs_state
                self._tabs_state_dirty = True
            save_tabs_state(tabs_state)
            with self._tabs_lock:
                self._tabs_state_dirty = False
        return {"success": True}

    def flush_tabs_state(self) -> None:
        """Persist any cached dirty tab state synchronously during native window closing (fixes L3)."""
        with self._tabs_lock:
            pending = self._latest_tabs_state if self._tabs_state_dirty else None
            self._tabs_state_dirty = False
        if pending is not None:
            try:
                save_tabs_state(pending)
            except Exception as exc:
                _logger.warning("Failed flushing tab state on close: %s", exc)

    def _start_stream(
        self,
        tab_id: str,
        conversation_id: str,
        provider_id: str,
        model_id: str,
        thinking_level: str,
        web_search_enabled: bool = False,
        stream_id: Optional[str] = None,
    ) -> Optional[str]:
        conv = get_conversation(conversation_id)
        if not conv:
            return None
        sid = (str(stream_id or "").strip()) or str(uuid.uuid4())
        clean_thinking = _normalize_thinking_level(thinking_level)
        StreamManager.register_stream(tab_id, stream_id=sid, conversation_id=conversation_id)
        context_msgs = self._build_context_messages(conv)
        threading.Thread(
            target=stream_chat_completion,
            kwargs={
                "bridge": self,
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "messages": context_msgs,
                "provider_id": provider_id,
                "model_id": model_id,
                "thinking_level": clean_thinking,
                "web_search_enabled": bool(web_search_enabled),
                "stream_id": sid,
            },
            daemon=True,
        ).start()
        return sid

    @_guarded_rpc
    def send_message(
        self,
        tab_id: str,
        conversation_id: str,
        content: str,
        provider_id: str,
        model_id: str,
        thinking_level: str = "none",
        title: str = "New Chat",
        system_prompt: str = "",
        max_context_messages: int = 30,
        web_search_enabled: bool = False,
        stream_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_provider = (provider_id or "").strip()
        clean_model = (model_id or "").strip()
        if not clean_provider or not clean_model:
            return {"error": "Please select a provider and model before sending."}

        clean_thinking = _normalize_thinking_level(thinking_level)
        clamped_ctx = _clamp_max_context(max_context_messages, default=30)

        conv = get_conversation(conversation_id) if conversation_id else None
        if not conv:
            conv = create_conversation(
                title=title or "New Chat",
                system_prompt=system_prompt or "",
                max_context=clamped_ctx,
            )
            conversation_id = conv["id"]
        else:
            if title and not is_default_conversation_title(title) and is_default_conversation_title(conv.get("title")):
                update_conversation(conversation_id, title=title.strip())
            if system_prompt and not conv.get("system_prompt"):
                update_conversation(conversation_id, system_prompt=system_prompt)

        user_msg = add_message(
            conversation_id=conversation_id,
            role="user",
            content=content,
            provider_id=clean_provider,
            model_id=clean_model,
            thinking_level=clean_thinking,
        )
        if not user_msg:
            return {"error": "Conversation not found"}

        active_sid = self._start_stream(
            tab_id,
            conversation_id,
            clean_provider,
            clean_model,
            clean_thinking,
            web_search_enabled,
            stream_id=stream_id,
        )
        if not active_sid:
            return {"error": "Conversation not found"}

        return {"user_message": user_msg, "conversation_id": conversation_id, "stream_id": active_sid}

    @_guarded_rpc
    def regenerate_message(
        self,
        tab_id: str,
        conversation_id: str,
        message_id: str,
        provider_id: str,
        model_id: str,
        thinking_level: str = "none",
        web_search_enabled: bool = False,
        stream_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate provider & abort active streams BEFORE deleting downstream messages (fixes NEW-H3)."""
        clean_provider = (provider_id or "").strip()
        clean_model = (model_id or "").strip()
        if not clean_provider or not clean_model:
            return {"error": "Please select a provider and model before regenerating."}

        if not conversation_id or not get_conversation(conversation_id):
            return {"error": "Conversation not found"}

        prov_err = ClientManager.validate_provider_ready(clean_provider)
        if prov_err:
            return {"error": prov_err}

        StreamManager.abort_conversation(conversation_id)
        clean_thinking = _normalize_thinking_level(thinking_level)
        delete_messages_from(conversation_id, message_id, include_self=True)
        active_sid = self._start_stream(
            tab_id,
            conversation_id,
            clean_provider,
            clean_model,
            clean_thinking,
            web_search_enabled,
            stream_id=stream_id,
        )
        if not active_sid:
            return {"error": "Conversation not found"}
        return {"success": True, "stream_id": active_sid, "token_stats": get_token_stats(conversation_id)}

    @_guarded_rpc
    def edit_message(
        self,
        tab_id: str,
        conversation_id: str,
        message_id: str,
        new_content: str,
        provider_id: str,
        model_id: str,
        thinking_level: str = "none",
        web_search_enabled: bool = False,
        stream_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate message & provider readiness and abort active streams BEFORE mutating SQLite (fixes NEW-H3)."""
        clean_provider = (provider_id or "").strip()
        clean_model = (model_id or "").strip()
        if not clean_provider or not clean_model:
            return {"error": "Please select a provider and model before sending."}

        clean_thinking = _normalize_thinking_level(thinking_level)
        if message_id.startswith("user_temp_") or not message_id:
            with open_db() as conn:
                row = conn.execute(
                    "SELECT id FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY rowid DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
            message_id = row["id"] if row else ""

        if not message_id:
            return {"error": "Message not found"}

        with open_db() as conn:
            existing = conn.execute(
                "SELECT id FROM messages WHERE id = ? AND conversation_id = ?",
                (message_id, conversation_id),
            ).fetchone()
        if not existing:
            return {"error": "Message not found"}

        prov_err = ClientManager.validate_provider_ready(clean_provider)
        if prov_err:
            return {"error": prov_err}

        StreamManager.abort_conversation(conversation_id)
        delete_messages_from(conversation_id, message_id, include_self=False)
        update_message(
            message_id,
            content=new_content,
            provider_id=clean_provider,
            model_id=clean_model,
            thinking_level=clean_thinking,
        )

        active_sid = self._start_stream(
            tab_id,
            conversation_id,
            clean_provider,
            clean_model,
            clean_thinking,
            web_search_enabled,
            stream_id=stream_id,
        )
        if not active_sid:
            return {"error": "Conversation not found"}
        return {"success": True, "stream_id": active_sid, "token_stats": get_token_stats(conversation_id)}

    @_guarded_rpc
    def stop_streaming(self, tab_id: str, stream_id: Optional[str] = None) -> Dict[str, Any]:
        StreamManager.abort_stream(tab_id, stream_id=stream_id)
        return {"success": True}
