"""Unified ProviderAdapter registry for OpenAI, Anthropic, and Gemini."""
from __future__ import annotations

from typing import Dict

from chatint.providers.anthropic_adapter import (
    AnthropicAdapter,
    _build_anthropic_effort_candidates,
    _is_anthropic_adaptive_model,
    _stream_anthropic,
)
from chatint.providers.base import (
    UNIFIED_THINKING_CHAINS,
    ProviderAdapter,
    _append_search_system_hint,
    _emit_api_error,
    _emit_chunk,
    _emit_error,
    _emit_start,
    _ensure_post_tool_separator,
    _ensure_post_tool_thinking_separator,
    _flush_think_buffer,
    _parse_tool_query,
    _prepare_system_and_turns,
    _process_text_with_think_tags,
    _resolve_search_enabled,
    _run_web_search_tool_call,
    _safe_dispatch,
    clear_resolved_thinking_cache,
    get_unified_thinking_chain,
    record_resolved_thinking_level,
)
from chatint.providers.gemini_adapter import GeminiAdapter, _stream_gemini
from chatint.providers.openai_adapter import (
    OpenAIAdapter,
    _clamp_openai_effort,
    _is_openai_retryable_param_error,
    _stream_openai,
)

ADAPTERS: Dict[str, ProviderAdapter] = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
    "gemini": GeminiAdapter(),
}


def get_provider_adapter(sdk_type: str) -> ProviderAdapter:
    return ADAPTERS.get((sdk_type or "openai").strip().lower(), ADAPTERS["openai"])


__all__ = [
    "ADAPTERS",
    "AnthropicAdapter",
    "GeminiAdapter",
    "OpenAIAdapter",
    "ProviderAdapter",
    "UNIFIED_THINKING_CHAINS",
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
    "clear_resolved_thinking_cache",
    "get_provider_adapter",
    "get_unified_thinking_chain",
    "record_resolved_thinking_level",
]
