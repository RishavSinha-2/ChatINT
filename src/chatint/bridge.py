import collections
import json
import re
import threading
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
    get_api_key_from_env,
    get_app_settings,
    get_connection,
    get_conversation,
    get_conversations,
    get_google_search_request_totals,
    get_providers,
    get_tabs_state,
    get_token_stats,
    get_web_search_models,
    is_valid_provider_name,
    save_app_settings,
    save_providers,
    save_tabs_state,
    save_web_search_models,
    update_conversation,
    update_message,
)
from chatint.streamer import StreamManager, stream_chat_completion


class DesktopBridge:
    def __init__(self):
        self._event_queue = collections.deque()
        self._queue_lock = threading.Lock()
        self._window: Optional[Any] = None
        self._index_url: str = ""

    def attach_window(self, window: Any, index_url: str):
        self._window = window
        self._index_url = index_url

    def return_to_chat(self) -> Dict[str, Any]:
        """Navigate the main WebView window back to the local ChatInt UI."""
        if self._window and self._index_url:
            # Delay navigation slightly so pywebview resolves this RPC callback before unloading the external page
            threading.Timer(0.05, self._window.load_url, args=(self._index_url,)).start()
            return {"success": True}
        return {"success": False, "error": "Window not attached"}

    def open_external_url(self, url: str = "") -> Dict[str, Any]:
        """Open the given (or currently viewed) web URL in the system's default browser."""
        target = (url or "").strip()
        if not target and self._window:
            try:
                target = str(self._window.get_current_url() or "").strip()
            except Exception:
                target = ""
        if not target.lower().startswith(("http://", "https://")):
            return {"success": False, "error": "Only HTTP/HTTPS URLs can be opened externally."}
        webbrowser.open(target)
        return {"success": True}

    def push_event(self, event_data: Dict[str, Any]):
        """
        Thread-safe memory event append. Takes 0.001ms, never blocks or deadlocks.
        """
        with self._queue_lock:
            self._event_queue.append(event_data)

    def get_pending_events(self) -> List[Dict[str, Any]]:
        """
        Frontend poll endpoint for real-time events.
        Coalesces consecutive chunk events for the same tab to minimize DOM re-renders.
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

        max_context = conv.get("max_context_messages", 30)
        all_msgs = [
            m
            for m in conv.get("messages", [])
            if m.get("role") != "assistant" or (m.get("content") or "").strip()
        ]

        history = all_msgs[-max_context:] if max_context > 0 else all_msgs
        while len(history) > 1 and history[0].get("role") == "assistant":
            history = history[1:]

        context_msgs: List[Dict[str, str]] = []
        if system_prompt:
            context_msgs.append({"role": "system", "content": system_prompt})

        for m in history:
            role = m.get("role", "user")
            content = m.get("content", "")
            if context_msgs and context_msgs[-1]["role"] == role and role in ("user", "assistant"):
                if content != context_msgs[-1]["content"]:
                    context_msgs[-1]["content"] = context_msgs[-1]["content"] + "\n\n" + content
            else:
                context_msgs.append({"role": role, "content": content})

        return context_msgs

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

    def add_provider(self, provider: Dict[str, Any]) -> Dict[str, Any]:
        """Adds a new custom provider."""
        providers = get_providers()
        providers.append(provider)
        save_providers(providers)
        BackgroundModelSyncer.start_background_sync(self)
        return {"success": True, "providers": get_providers()}

    def delete_provider(self, provider_id: str) -> Dict[str, Any]:
        """Removes a provider."""
        db_delete_provider(provider_id)
        return {"success": True, "providers": get_providers()}

    def get_provider_api_key(self, provider_name: str) -> Dict[str, Any]:
        return {"api_key": get_api_key_from_env(provider_name)}

    def get_conversation_token_stats(self, conv_id: str) -> Dict[str, Any]:
        return get_token_stats(conv_id)

    def save_settings(
        self,
        providers: List[Dict[str, Any]],
        settings: Dict[str, str],
        web_search_models: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
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

        save_providers(providers)
        save_app_settings(settings)
        saved_ws = save_web_search_models(web_search_models) if web_search_models is not None else get_web_search_models()
        BackgroundModelSyncer.start_background_sync(self)
        return {"success": True, "providers": get_providers(), "web_search_models": saved_ws}

    def fetch_provider_models(self, provider_id: str) -> Dict[str, Any]:
        models, err, is_net = ClientManager.fetch_remote_models(provider_id, timeout=60.0)
        sync_status, sync_error = BackgroundModelSyncer.get_sync_status(provider_id)
        return {
            "models": models,
            "fetched_model_ids": [m["id"] for m in models],
            "error": err,
            "sync_status": sync_status,
            "sync_error": sync_error,
        }

    def create_conversation(
        self, title: str = "New Chat", system_prompt: str = "", max_context: int = 30
    ) -> Dict[str, Any]:
        conv = create_conversation(title=title, system_prompt=system_prompt, max_context=max_context)
        return conv

    def get_conversations(self) -> List[Dict[str, Any]]:
        return get_conversations()

    def get_conversation(self, conv_id: str) -> Optional[Dict[str, Any]]:
        conv = get_conversation(conv_id)
        if conv:
            conv["token_stats"] = get_token_stats(conv_id)
        return conv

    def rename_conversation(self, conv_id: str, title: str) -> Dict[str, Any]:
        update_conversation(conv_id, title=title)
        return {"success": True}

    def update_conversation_settings(
        self, conv_id: str, system_prompt: str, max_context_messages: int
    ) -> Dict[str, Any]:
        update_conversation(
            conv_id,
            system_prompt=system_prompt,
            max_context_messages=int(max_context_messages),
        )
        return {"success": True}

    def delete_conversation(self, conv_id: str) -> Dict[str, Any]:
        delete_conversation(conv_id)
        return {"success": True}

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
                lines.append(f"> **System Prompt:** {conv['system_prompt']}\n")

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
    ) -> Optional[Dict[str, Any]]:
        conv = get_conversation(conversation_id)
        if not conv:
            return None
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
                "thinking_level": thinking_level,
                "web_search_enabled": bool(web_search_enabled),
            },
            daemon=True,
        ).start()
        return conv

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
    ) -> Dict[str, Any]:
        """Adds user message and starts assistant streaming via bridge."""
        conv = get_conversation(conversation_id) if conversation_id else None
        if not conv:
            conv = create_conversation(
                title=title or "New Chat",
                system_prompt=system_prompt or "",
                max_context=int(max_context_messages or 30),
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
            provider_id=provider_id,
            model_id=model_id,
            thinking_level=thinking_level,
        )

        if not self._start_stream(tab_id, conversation_id, provider_id, model_id, thinking_level, web_search_enabled):
            return {"error": "Conversation not found"}

        return {"user_message": user_msg, "conversation_id": conversation_id}

    def regenerate_message(
        self,
        tab_id: str,
        conversation_id: str,
        message_id: str,
        provider_id: str,
        model_id: str,
        thinking_level: str = "none",
        web_search_enabled: bool = False,
    ) -> Dict[str, Any]:
        """Deletes from message_id onwards and re-streams from previous user message."""
        delete_messages_from(conversation_id, message_id, include_self=True)
        if not self._start_stream(tab_id, conversation_id, provider_id, model_id, thinking_level, web_search_enabled):
            return {"error": "Conversation not found"}
        return {"success": True, "token_stats": get_token_stats(conversation_id)}

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
    ) -> Dict[str, Any]:
        """Edits user message, deletes any subsequent messages, and triggers new completion."""
        if message_id.startswith("user_temp_") or not message_id:
            conn = get_connection()
            row = conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            conn.close()
            if row:
                message_id = row["id"]

        delete_messages_from(conversation_id, message_id, include_self=False)
        update_message(
            message_id,
            content=new_content,
            provider_id=provider_id,
            model_id=model_id,
            thinking_level=thinking_level,
        )

        if not self._start_stream(tab_id, conversation_id, provider_id, model_id, thinking_level, web_search_enabled):
            return {"error": "Conversation not found"}
        return {"success": True, "token_stats": get_token_stats(conversation_id)}

    def stop_streaming(self, tab_id: str) -> Dict[str, Any]:
        StreamManager.abort_stream(tab_id)
        return {"success": True}
