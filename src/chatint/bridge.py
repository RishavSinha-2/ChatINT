import collections
import functools
import json
import re
import threading
import urllib.parse
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional
import webview
from chatint.client_manager import BackgroundModelSyncer, ClientManager
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
from chatint.logging_setup import get_logger
from chatint.streamer import StreamManager, stream_chat_completion

_logger = get_logger("chatint.bridge")
_VALID_THINKING_LEVELS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def _clamp_max_context(value: Any, default: int = 30) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(500, parsed))


def _normalize_thinking_level(level: Any) -> str:
    val = str(level or "none").strip().lower()
    return val if val in _VALID_THINKING_LEVELS else "none"


def _sanitize_msg_role(role: Any) -> str:
    val = str(role or "user").strip().lower()
    return val if val in ("user", "assistant", "system") else "user"


def _guarded_rpc(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        self._require_trusted_origin()
        return fn(self, *args, **kwargs)
    return wrapper


def parse_chat_file_content(content: str, filename: str = "") -> Dict[str, Any]:
    """
    Parses exported .json or .md chat content into a structured dictionary
    containing title, system_prompt, max_context, and messages.
    """
    content_str = (content or "").strip()
    if not content_str:
        return {"title": "Imported Chat", "system_prompt": "", "max_context": 30, "messages": []}

    # 1. Try parsing JSON first
    try:
        data = json.loads(content_str)
        if isinstance(data, dict):
            title = data.get("title") or (Path(filename).stem if filename else "") or "Imported Chat"
            system_prompt = data.get("system_prompt") or ""
            max_context = _clamp_max_context(data.get("max_context_messages"), default=30)
            messages = [
                {
                    "role": _sanitize_msg_role(m.get("role", "user")),
                    "content": str(m.get("content", "") or ""),
                    "thinking_content": str(m.get("thinking_content", "") or ""),
                    "provider_id": str(m.get("provider_id", "") or ""),
                    "model_id": str(m.get("model_id", "") or ""),
                    "thinking_level": _normalize_thinking_level(m.get("thinking_level", "none")),
                    **({"created_at": float(m["created_at"])} if isinstance(m.get("created_at"), (int, float)) else {}),
                }
                for m in (data.get("messages") or [])
                if isinstance(m, dict)
            ]
            return {
                "title": title,
                "system_prompt": system_prompt,
                "max_context": max_context,
                "messages": messages,
            }
        elif isinstance(data, list):
            messages = [
                {
                    "role": _sanitize_msg_role(m.get("role", "user")),
                    "content": str(m.get("content", "") or ""),
                    "thinking_content": str(m.get("thinking_content", "") or ""),
                    "provider_id": str(m.get("provider_id", "") or ""),
                    "model_id": str(m.get("model_id", "") or ""),
                    "thinking_level": _normalize_thinking_level(m.get("thinking_level", "none")),
                    **({"created_at": float(m["created_at"])} if isinstance(m.get("created_at"), (int, float)) else {}),
                }
                for m in data
                if isinstance(m, dict)
            ]
            return {
                "title": (Path(filename).stem if filename else "") or "Imported Chat",
                "system_prompt": "",
                "max_context": 30,
                "messages": messages,
            }
    except Exception:
        pass

    # 2. Parse Markdown format (fence-aware & multi-line system prompt support)
    title = ""
    system_lines: List[str] = []
    messages: List[Dict[str, Any]] = []

    raw_lines = content_str.splitlines()
    in_fence_char = ""
    in_fence_len = 0
    in_details = False
    collecting_sys_quote = False
    current_role: Optional[str] = None
    current_model_id = ""
    current_body_lines: List[str] = []

    def _flush_current_message():
        if current_role is None:
            return
        body = "\n".join(current_body_lines).strip()
        thinking_content = ""
        think_match = re.search(
            r"<details>\s*<summary>\s*Thought Process\s*</summary>\s*([\s\S]*?)\s*</details>",
            body,
            re.IGNORECASE,
        )
        if think_match:
            thinking_content = think_match.group(1).strip()
            body = (body[:think_match.start()] + "\n" + body[think_match.end():]).strip()
        messages.append({
            "role": current_role,
            "content": body,
            "thinking_content": thinking_content,
            "provider_id": "",
            "model_id": current_model_id,
            "thinking_level": "medium" if thinking_content else "none",
        })

    header_re = re.compile(r"^###\s+(User|Assistant(?:\s*\((.+)\))?)\s*$", re.IGNORECASE)
    sys_start_re = re.compile(r"^>\s*\*\*System Prompt:\*\*\s*(.*)$")

    for line in raw_lines:
        stripped = line.strip()

        # Track fenced code blocks so ### headings inside examples never split messages
        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence_match:
            f_str = fence_match.group(1)
            if not in_fence_char:
                in_fence_char = f_str[0]
                in_fence_len = len(f_str)
            elif f_str[0] == in_fence_char and len(f_str) >= in_fence_len and stripped == f_str:
                in_fence_char = ""
                in_fence_len = 0

        if not in_fence_char:
            if not in_details and re.search(r"<details>\s*<summary>\s*Thought Process\s*</summary>", line, re.IGNORECASE):
                in_details = True
            if in_details and "</details>" in line.lower():
                if current_role is not None:
                    current_body_lines.append(line)
                in_details = False
                continue

            if current_role is None:
                if not title:
                    t_match = re.match(r"^#\s+(.+)$", line)
                    if t_match:
                        title = t_match.group(1).strip()
                        continue
                s_match = sys_start_re.match(line)
                if s_match:
                    collecting_sys_quote = True
                    first_part = s_match.group(1).strip()
                    if first_part:
                        system_lines.append(first_part)
                    continue
                if collecting_sys_quote:
                    if line.startswith(">"):
                        cont = re.sub(r"^>\s?", "", line)
                        system_lines.append(cont)
                        continue
                    else:
                        collecting_sys_quote = False

            if not in_details:
                h_match = header_re.match(stripped)
                if h_match:
                    collecting_sys_quote = False
                    _flush_current_message()
                    if h_match.group(1).lower().startswith("user"):
                        current_role = "user"
                        current_model_id = ""
                    else:
                        current_role = "assistant"
                        current_model_id = (h_match.group(2) or "").strip()
                    current_body_lines = []
                    continue

        if current_role is not None:
            current_body_lines.append(line)

    _flush_current_message()

    if not title:
        title = (Path(filename).stem if filename else "") or "Imported Chat"
    system_prompt = "\n".join(system_lines).strip()

    if not messages:
        messages.append({
            "role": "user",
            "content": content_str,
            "thinking_content": "",
            "provider_id": "",
            "model_id": "",
            "thinking_level": "none",
        })

    return {
        "title": title,
        "system_prompt": system_prompt,
        "max_context": 30,
        "messages": messages,
    }


class DesktopBridge:
    def __init__(self):
        self._event_queue = collections.deque()
        self._queue_lock = threading.Lock()
        self._window: Optional[Any] = None
        self._index_url: str = ""
        self._allowed_origin: Optional[tuple[str, str]] = None
        self._origin_trusted: bool = True

    def attach_window(
        self,
        window: Any,
        index_url: str,
        allowed_origin: Optional[tuple[str, str]] = None,
    ):
        self._window = window
        self._index_url = index_url
        self._origin_trusted = True
        if allowed_origin is not None:
            self._allowed_origin = allowed_origin
        elif index_url:
            parsed = urllib.parse.urlparse(index_url)
            if parsed.scheme:
                self._allowed_origin = (parsed.scheme.lower(), parsed.netloc.lower())

    def set_origin_trusted(self, trusted: bool):
        self._origin_trusted = bool(trusted)

    def _is_url_trusted(self, current_url: str) -> bool:
        if not current_url:
            return False
        parsed = urllib.parse.urlparse(current_url)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        if self._allowed_origin is not None:
            if (scheme, netloc) == self._allowed_origin:
                if scheme == "file":
                    return urllib.parse.unquote(parsed.path).replace("\\", "/").lower().endswith("/ui/index.html")
                return True
            if self._allowed_origin[0] == "file" and parsed.hostname in ("127.0.0.1", "localhost"):
                return urllib.parse.unquote(parsed.path).lower().endswith(("/index.html", "/"))
            return False
        return current_url == self._index_url

    def _require_trusted_origin(self):
        if self._window is None:
            return
        if not self._origin_trusted:
            raise PermissionError("Blocked RPC call from untrusted window state.")
        try:
            current_url = str(self._window.get_current_url() or "").strip()
        except Exception:
            current_url = ""
        if current_url and not self._is_url_trusted(current_url):
            _logger.warning("Blocked RPC from untrusted URL: %s", current_url)
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
        """Validate and open an HTTP/HTTPS URL in the system's default browser."""
        target = (url or "").strip()
        if not target or any(ch.isspace() or ord(ch) < 32 for ch in target):
            return {"success": False, "error": "Invalid URL."}
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return {"success": False, "error": "Only HTTP/HTTPS URLs can be opened externally."}
        webbrowser.open(target)
        return {"success": True}

    def push_event(self, event_data: Dict[str, Any]):
        """
        Thread-safe memory event append. Takes 0.001ms, never blocks or deadlocks.
        """
        with self._queue_lock:
            self._event_queue.append(event_data)

    @_guarded_rpc
    def get_pending_events(self) -> List[Dict[str, Any]]:
        """
        Frontend poll endpoint for real-time events.
        Coalesces consecutive chunk events for the same tab and stream_id to minimize DOM re-renders.
        """
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
            content = raw_content if (raw_content or "").strip() else (
                "[Response stopped]" if role == "assistant" else ""
            )
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
        """
        Loads all essential bootstrap data for the app instantly without blocking UI.
        Launches background fetch with 60s TTL and auto-retry upon connectivity.
        """
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
        }

    @_guarded_rpc
    def add_provider(self, provider: Dict[str, Any]) -> Dict[str, Any]:
        """Adds a new custom provider."""
        providers = get_providers()
        providers.append(provider)
        save_providers(providers)
        BackgroundModelSyncer.start_background_sync(self)
        return {"success": True, "providers": get_providers()}

    @_guarded_rpc
    def delete_provider(self, provider_id: str) -> Dict[str, Any]:
        """Removes a provider."""
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
        """Persist only the active theme without touching providers or triggering model sync."""
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
        old_by_id = {p["id"]: p for p in get_providers()}

        seen_names = set()
        for p in providers:
            pname = (p.get("name") or "").strip()
            if not is_valid_provider_name(pname):
                return {
                    "success": False,
                    "error": f"Invalid provider name '{pname}'. Provider names may only contain English alphabet letters (A-Z, a-z) with no spaces, dashes, underscores, numbers, or special characters.",
                }
            upper_name = pname.upper()
            if upper_name in seen_names:
                return {
                    "success": False,
                    "error": f"Duplicate provider name '{pname}'. No two providers can have the same name (case-insensitive).",
                }
            seen_names.add(upper_name)

        changed_provider_ids: List[str] = []
        for p in providers:
            pid = p["id"]
            old_p = old_by_id.get(pid)
            new_enabled = bool(p.get("is_enabled", True))
            pname = (p.get("name") or "").strip()
            is_native = pid.lower() in ("anthropic", "gemini") or pname.lower() in ("anthropic", "gemini")
            new_url = "" if is_native else (p.get("base_url") or "")

            key_changed = bool((p.get("api_key") is not None and str(p.get("api_key")).strip() != "") or p.get("clear_api_key"))
            url_changed = bool(old_p and not is_native and new_url != (old_p.get("base_url") or ""))
            name_changed = bool(old_p and pname.upper() != (old_p.get("name") or "").upper())
            enabled_changed = bool(old_p and new_enabled != bool(old_p.get("is_enabled", True)))
            needs_initial_verify = bool(
                new_enabled
                and old_p
                and old_p.get("has_api_key")
                and not BackgroundModelSyncer.is_provider_verified(pid)
                and not p.get("clear_api_key")
            )

            if old_p is None or key_changed or url_changed or name_changed or enabled_changed or needs_initial_verify:
                changed_provider_ids.append(pid)

        save_providers(providers)
        save_app_settings(settings)
        saved_ws = save_web_search_models(web_search_models) if web_search_models is not None else get_web_search_models()
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
    ) -> Dict[str, Any]:
        models, err, is_net = ClientManager.fetch_remote_models(
            provider_id,
            timeout=60.0,
            api_key=api_key,
            base_url=base_url,
            is_enabled=is_enabled,
            clear_api_key=bool(clear_api_key),
            update_sync_state=False,
        )
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
        conv = create_conversation(
            title=title,
            system_prompt=system_prompt,
            max_context=_clamp_max_context(max_context, default=30),
        )
        return conv

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
                if not p.exists():
                    return {"error": "File does not exist."}
                content = p.read_text(encoding="utf-8")
                parsed = parse_chat_file_content(content, filename=p.name)
                new_conv = db_import_conversation_data(
                    title=parsed["title"],
                    system_prompt=parsed["system_prompt"],
                    max_context=parsed["max_context"],
                    messages=parsed["messages"],
                )
                return {"success": True, "conversation": new_conv}
            except Exception as e:
                return {"error": f"Failed to open file: {str(e)}"}
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
            )
            return {"success": True, "conversation": new_conv}
        except Exception as e:
            return {"error": f"Failed to import chat: {str(e)}"}

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
            # Markdown format
            lines = [f"# {conv['title']}\n"]
            if conv.get("system_prompt"):
                sys_lines = str(conv["system_prompt"]).splitlines() or [""]
                prefixed_sys = [f"> **System Prompt:** {sys_lines[0]}"]
                prefixed_sys.extend(f"> {line}" for line in sys_lines[1:])
                lines.append("\n".join(prefixed_sys) + "\n")

            for msg in conv.get("messages", []):
                role_title = "User" if msg["role"] == "user" else f"Assistant ({msg.get('model_id', 'Model')})"
                lines.append(f"### {role_title}\n")
                if msg.get("thinking_content"):
                    lines.append(f"<details><summary>Thought Process</summary>\n\n{msg['thinking_content']}\n\n</details>\n")
                lines.append(f"{msg['content']}\n")

            content = "\n".join(lines)
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
                return {"error": f"Failed to save file: {str(e)}"}

        return {"content": content, "format": format_type, "filename": default_filename}

    @_guarded_rpc
    def save_tabs_state(self, tabs_state: Dict[str, Any]) -> Dict[str, Any]:
        save_tabs_state(tabs_state)
        return {"success": True}

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
        # Pre-register stream synchronously before thread startup so Stop works immediately
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
        """Adds user message and starts assistant streaming via bridge."""
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
            if title and title.strip().lower() != "new chat" and conv.get("title") == "New Chat":
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
        """Deletes from message_id onwards and re-streams from previous user message."""
        clean_provider = (provider_id or "").strip()
        clean_model = (model_id or "").strip()
        if not clean_provider or not clean_model:
            return {"error": "Please select a provider and model before regenerating."}

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
        """Edits user message, deletes any subsequent messages, and triggers new completion."""
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
