"""Backwards-compatible streaming facade delegating to `chatint.providers` and `chatint.services`."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

from chatint.providers import (
    UNIFIED_THINKING_CHAINS,
    _append_search_system_hint,
    _build_anthropic_effort_candidates,
    _clamp_openai_effort,
    _emit_api_error,
    _emit_chunk,
    _emit_error,
    _emit_start,
    _ensure_post_tool_separator,
    _ensure_post_tool_thinking_separator,
    _flush_think_buffer,
    _is_anthropic_adaptive_model,
    _is_openai_retryable_param_error,
    _parse_tool_query,
    _prepare_system_and_turns,
    _process_text_with_think_tags,
    _resolve_search_enabled,
    _run_web_search_tool_call,
    _safe_dispatch,
    _stream_anthropic,
    _stream_gemini,
    _stream_openai,
    clear_resolved_thinking_cache,
    get_unified_thinking_chain,
    record_resolved_thinking_level,
)
from chatint.services.stream_coordinator import StreamManager, run_auto_naming, stream_chat_completion_impl

_SDK_STREAMERS = {
    "anthropic": _stream_anthropic,
    "gemini": _stream_gemini,
    "openai": _stream_openai,
}


def _trigger_auto_naming(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    user_question: str,
    provider_id: str,
    model_id: str,
    stream_id: Optional[str] = None,
) -> None:
    run_auto_naming(
        bridge=bridge,
        tab_id=tab_id,
        conversation_id=conversation_id,
        user_question=user_question,
        provider_id=provider_id,
        model_id=model_id,
        stream_id=stream_id,
    )


def stream_chat_completion(
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
) -> None:
    stream_chat_completion_impl(
        bridge=bridge,
        tab_id=tab_id,
        conversation_id=conversation_id,
        messages=messages,
        provider_id=provider_id,
        model_id=model_id,
        thinking_level=thinking_level,
        assistant_msg_id=assistant_msg_id,
        web_search_enabled=web_search_enabled,
        stream_id=stream_id,
        sdk_streamers=_SDK_STREAMERS,
        auto_naming_fn=_trigger_auto_naming,
    )


__all__ = [
    "StreamManager",
    "UNIFIED_THINKING_CHAINS",
    "_SDK_STREAMERS",
    "_append_search_system_hint",
    "_build_anthropic_effort_candidates",
    "_clamp_openai_effort",
    "_emit_api_error",
    "_emit_chunk",
    "_emit_error",
    "_emit_start",
    "_ensure_post_tool_separator",
    "_ensure_post_tool_thinking_separator",
    "_flush_think_buffer",
    "_is_anthropic_adaptive_model",
    "_is_openai_retryable_param_error",
    "_parse_tool_query",
    "_prepare_system_and_turns",
    "_process_text_with_think_tags",
    "_resolve_search_enabled",
    "_run_web_search_tool_call",
    "_safe_dispatch",
    "_stream_anthropic",
    "_stream_gemini",
    "_stream_openai",
    "_trigger_auto_naming",
    "clear_resolved_thinking_cache",
    "get_unified_thinking_chain",
    "record_resolved_thinking_level",
    "stream_chat_completion",
]
