"""Stream lifecycle registry, background auto-naming, and completion coordinator."""
from __future__ import annotations

import inspect
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import uuid

from chatint.client_manager import ClientManager
from chatint.db import (
    add_message,
    get_conv_db_size_bytes,
    get_conversation,
    get_token_stats,
    is_default_conversation_title,
    record_token_usage,
    update_conversation,
)
from chatint.logging_setup import get_logger
from chatint.naming import generate_chat_title
from chatint.providers.base import _emit_error, _safe_dispatch

_logger = get_logger("chatint.services.stream_coordinator")


class StreamManager:
    _streams_by_tab: Dict[str, tuple[str, threading.Event, str]] = {}
    _streams_by_id: Dict[str, tuple[str, threading.Event, str]] = {}
    _pending_approvals: Dict[tuple[str, str], Dict[str, Any]] = {}
    _lock = threading.Lock()

    @classmethod
    def _wake_approvals_for_stream(cls, stream_id: str, decision: str = "deny") -> None:
        for (sid, aid), entry in list(cls._pending_approvals.items()):
            if sid == stream_id:
                if entry.get("decision") is None:
                    entry["decision"] = decision
                entry["event"].set()

    @classmethod
    def request_approval(
        cls,
        tab_id: str,
        stream_id: str,
        approval_id: str,
        abort_event: threading.Event,
        timeout: float = 300.0,
    ) -> str:
        """Block the stream thread waiting for user approval, abort, or timeout."""
        ev = threading.Event()
        key = (stream_id, approval_id)
        with cls._lock:
            cls._pending_approvals[key] = {"event": ev, "decision": None, "tab_id": tab_id}

        deadline = time.monotonic() + max(1.0, float(timeout))
        try:
            while not abort_event.is_set():
                if ev.wait(timeout=0.1):
                    with cls._lock:
                        entry = cls._pending_approvals.get(key)
                        decision = str((entry or {}).get("decision") or "deny")
                    return decision if decision in ("allow_once", "allow_all_tab", "deny") else "deny"
                if time.monotonic() >= deadline:
                    return "deny"
            return "deny"
        finally:
            with cls._lock:
                cls._pending_approvals.pop(key, None)

    @classmethod
    def resolve_approval(cls, stream_id: str, approval_id: str, decision: str) -> bool:
        """Resolve a pending tool approval request and wake the blocked stream thread."""
        clean_decision = decision if decision in ("allow_once", "allow_all_tab", "deny") else "deny"
        with cls._lock:
            entry = cls._pending_approvals.get((stream_id, approval_id))
            if not entry:
                return False
            entry["decision"] = clean_decision
            entry["event"].set()
            return True

    @classmethod
    def abort_stream(cls, tab_id: str, stream_id: Optional[str] = None) -> None:
        with cls._lock:
            if stream_id and stream_id in cls._streams_by_id:
                cls._streams_by_id[stream_id][1].set()
                cls._wake_approvals_for_stream(stream_id, "deny")
            entry = cls._streams_by_tab.get(tab_id)
            if entry:
                entry[1].set()
                cls._wake_approvals_for_stream(entry[0], "deny")

    @classmethod
    def abort_conversation(cls, conversation_id: str) -> None:
        if not conversation_id:
            return
        with cls._lock:
            for sid, (_tid, ev, cid) in list(cls._streams_by_id.items()):
                if cid == conversation_id:
                    ev.set()
                    cls._wake_approvals_for_stream(sid, "deny")
            for _tid, (sid, ev, cid) in list(cls._streams_by_tab.items()):
                if cid == conversation_id:
                    ev.set()
                    cls._wake_approvals_for_stream(sid, "deny")

    @classmethod
    def register_stream(
        cls,
        tab_id: str,
        stream_id: Optional[str] = None,
        conversation_id: str = "",
    ) -> threading.Event:
        sid = stream_id or str(uuid.uuid4())
        with cls._lock:
            if sid in cls._streams_by_id:
                existing_tab, existing_ev, existing_cid = cls._streams_by_id[sid]
                if conversation_id and not existing_cid:
                    cls._streams_by_id[sid] = (existing_tab, existing_ev, conversation_id)
                    if existing_tab in cls._streams_by_tab and cls._streams_by_tab[existing_tab][0] == sid:
                        cls._streams_by_tab[existing_tab] = (sid, existing_ev, conversation_id)
                return existing_ev

            prev = cls._streams_by_tab.get(tab_id)
            if prev and prev[0] != sid:
                prev[1].set()
                cls._wake_approvals_for_stream(prev[0], "deny")

            event = threading.Event()
            cls._streams_by_tab[tab_id] = (sid, event, conversation_id or "")
            cls._streams_by_id[sid] = (tab_id, event, conversation_id or "")
            return event

    @classmethod
    def unregister_stream(cls, tab_id: str, stream_id: Optional[str] = None) -> None:
        with cls._lock:
            if stream_id:
                cls._streams_by_id.pop(stream_id, None)
                cls._wake_approvals_for_stream(stream_id, "deny")
                cur = cls._streams_by_tab.get(tab_id)
                if cur and cur[0] == stream_id:
                    cls._streams_by_tab.pop(tab_id, None)
            else:
                cur = cls._streams_by_tab.pop(tab_id, None)
                if cur:
                    cls._streams_by_id.pop(cur[0], None)
                    cls._wake_approvals_for_stream(cur[0], "deny")


def run_auto_naming(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    user_question: str,
    provider_id: str,
    model_id: str,
    stream_id: Optional[str] = None,
) -> None:
    """Spawn a daemon thread to generate and persist a conversation title when still unnamed."""

    def _naming_worker() -> None:
        try:
            conv = get_conversation(conversation_id)
            if not conv:
                return
            current_title = (conv.get("title") or "").strip()
            assistant_count = sum(1 for m in conv.get("messages", []) if m.get("role") == "assistant")
            if assistant_count < 1:
                return
            needs_name = (not conv.get("is_auto_named")) and is_default_conversation_title(current_title)
            if not needs_name:
                return

            final_title, in_tok, out_tok, cache_tok = generate_chat_title(
                user_question,
                provider_id=provider_id,
                model_id=model_id,
            )
            if in_tok > 0 or out_tok > 0 or cache_tok > 0:
                record_token_usage(
                    conversation_id=conversation_id,
                    provider_id=provider_id or "",
                    model_id=model_id or "",
                    prompt_tokens=in_tok,
                    completion_tokens=out_tok,
                    cached_tokens=cache_tok,
                )

            resolved_title = update_conversation(conversation_id, only_if_default=True, title=final_title)
            latest_conv = get_conversation(conversation_id)
            if not latest_conv:
                # Conversation was deleted while naming ran; do not emit chat_named (fixes H5 resurrection)
                return

            effective_title = resolved_title or (latest_conv.get("title") or "").strip() or "New Chat"
            ev_payload: Dict[str, Any] = {
                "type": "chat_named",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "title": effective_title,
                "token_stats": get_token_stats(conversation_id),
            }
            if stream_id:
                ev_payload["stream_id"] = stream_id
            _safe_dispatch(bridge, ev_payload)
        except Exception as ex:
            _logger.warning("Auto-naming failed for %s: %s", conversation_id, ex)

    threading.Thread(target=_naming_worker, daemon=True, name=f"auto-name-{conversation_id[:8]}").start()


def stream_chat_completion_impl(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    messages: List[Dict[str, str]],
    provider_id: str,
    model_id: str,
    thinking_level: str = "none",
    assistant_msg_id: Optional[str] = None,
    web_search_enabled: bool = False,
    stream_id: Optional[str] = None,
    *,
    sdk_streamers: Dict[str, Callable[..., Any]],
    auto_naming_fn: Callable[..., None],
    agent_enabled: bool = False,
    workspace_dir: str = "",
    agent_mode: str = "manual",
) -> None:
    """Coordinate provider streaming, partial/aborted token persistence, auto-naming, and final done event."""
    active_stream_id = stream_id or str(uuid.uuid4())
    abort_event = StreamManager.register_stream(tab_id, stream_id=active_stream_id, conversation_id=conversation_id)
    user_question = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    provider = ClientManager.get_provider(provider_id)
    provider_name = (provider.get("name") if provider else "") or ""
    sdk_type = ClientManager.get_sdk_type(provider, provider_id)
    stream_fn = sdk_streamers.get(sdk_type) or sdk_streamers["openai"]
    norm_agent_mode = "auto" if str(agent_mode or "manual").strip().lower() == "auto" else "manual"
    tool_trace_collector: List[Dict[str, Any]] = []
    timing_stats_collector: Dict[str, int] = {"model_ms": 0}

    stream_kwargs: Dict[str, Any] = {
        "bridge": bridge,
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "messages": messages,
        "provider_id": provider_id,
        "model_id": model_id,
        "thinking_level": thinking_level,
        "assistant_msg_id": assistant_msg_id,
        "abort_event": abort_event,
        "user_question": user_question,
        "web_search_enabled": bool(web_search_enabled),
        "stream_id": active_stream_id,
    }
    try:
        sig = inspect.signature(stream_fn)
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if has_varkw or "agent_enabled" in sig.parameters:
            stream_kwargs["agent_enabled"] = bool(agent_enabled)
            stream_kwargs["workspace_dir"] = str(workspace_dir or "").strip()
            stream_kwargs["agent_mode"] = norm_agent_mode
            stream_kwargs["tool_trace"] = tool_trace_collector
        if has_varkw or "timing_stats" in sig.parameters:
            stream_kwargs["timing_stats"] = timing_stats_collector
    except (TypeError, ValueError):
        pass

    t_start = time.perf_counter()
    try:
        try:
            (
                full_content,
                full_thinking,
                prompt_tokens,
                completion_tokens,
                cached_tokens,
                stream_had_error,
                actual_thinking_level,
            ) = stream_fn(**stream_kwargs)
        except Exception as e:
            _logger.exception("Unhandled streaming exception on tab=%s conv=%s", tab_id, conversation_id)
            _emit_error(
                bridge, tab_id, conversation_id, f"Unexpected streaming error: {e}", stream_id=active_stream_id
            )
            return
    finally:
        StreamManager.unregister_stream(tab_id, stream_id=active_stream_id)
    wall_ms = max(0, int(round((time.perf_counter() - t_start) * 1000)))
    model_ms = max(0, int(timing_stats_collector.get("model_ms") or 0))
    duration_ms = model_ms if model_ms > 0 else wall_ms
    worked_ms = wall_ms

    final_content_str = "".join(full_content).strip()
    final_thinking_str = "".join(full_thinking).strip()

    # Preserve any Round 0 tool-call tokens in token_ledger even when aborted/errored before visible text (NEW-M2)
    if not final_content_str and not final_thinking_str:
        if prompt_tokens > 0 or completion_tokens > 0 or cached_tokens > 0:
            record_token_usage(
                conversation_id=conversation_id,
                provider_id=provider_id,
                model_id=model_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
            )
            if stream_had_error:
                _safe_dispatch(
                    bridge,
                    {
                        "type": "token_stats_updated",
                        "tab_id": tab_id,
                        "conversation_id": conversation_id,
                        "stream_id": active_stream_id,
                        "token_stats": get_token_stats(conversation_id),
                    },
                )
        if stream_had_error:
            return
        # Cleanly notify UI when user aborted before any chunk arrived
        _safe_dispatch(
            bridge,
            {
                "type": "done",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "stream_id": active_stream_id,
                "message": None,
                "token_stats": get_token_stats(conversation_id),
                "was_aborted": abort_event.is_set(),
            },
        )
        return

    total_output_chars = len(final_content_str) + len(final_thinking_str)
    if completion_tokens == 0 and total_output_chars > 0:
        completion_tokens = max(1, total_output_chars // 4)
    if prompt_tokens == 0:
        total_prompt_chars = sum(len(m.get("content", "")) for m in messages)
        prompt_tokens = max(1, total_prompt_chars // 4)

    msg_record = add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=final_content_str,
        thinking_content=final_thinking_str,
        provider_id=provider_id,
        model_id=model_id,
        thinking_level=actual_thinking_level,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        msg_id=assistant_msg_id,
        duration_ms=duration_ms,
        worked_ms=worked_ms,
        provider_name=provider_name,
        tool_trace=tool_trace_collector,
    )
    if msg_record is None:
        return

    # Spawn auto-naming on any turn of a conversation that still uses its default title
    if not stream_had_error:
        conv_snapshot = get_conversation(conversation_id)
        if conv_snapshot and (not conv_snapshot.get("is_auto_named")) and is_default_conversation_title(
            conv_snapshot.get("title")
        ):
            auto_naming_fn(
                bridge,
                tab_id,
                conversation_id,
                user_question,
                provider_id,
                model_id,
                stream_id=active_stream_id,
            )

    token_stats = get_token_stats(conversation_id)
    conv_size_bytes = get_conv_db_size_bytes(conversation_id)

    if not stream_had_error:
        _safe_dispatch(
            bridge,
            {
                "type": "done",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "stream_id": active_stream_id,
                "message": msg_record,
                "token_stats": token_stats,
                "db_size_bytes": conv_size_bytes,
                "was_aborted": abort_event.is_set(),
            },
        )
    else:
        _safe_dispatch(
            bridge,
            {
                "type": "token_stats_updated",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "stream_id": active_stream_id,
                "token_stats": token_stats,
                "db_size_bytes": conv_size_bytes,
            },
        )
