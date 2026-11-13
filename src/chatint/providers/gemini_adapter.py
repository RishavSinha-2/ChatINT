"""Google Gemini native GenAI SDK ProviderAdapter implementation."""
from __future__ import annotations

import itertools
import threading
from typing import Any, Dict, List, Optional

from chatint.catalog import is_gemini_3_or_newer, strip_model_prefix
from chatint.client_manager import ClientManager
from chatint.logging_setup import get_logger
from chatint.providers.base import (
    ProviderAdapter,
    _append_search_system_hint,
    _emit_api_error,
    _emit_chunk,
    _emit_error,
    _emit_start,
    _ensure_post_tool_separator,
    _ensure_post_tool_thinking_separator,
    _flush_think_buffer,
    _prepare_system_and_turns,
    _process_text_with_think_tags,
    _resolve_search_enabled,
    _run_web_search_tool_call,
    clear_resolved_thinking_cache,
    get_unified_thinking_chain,
    record_resolved_thinking_level,
)
from chatint.web_search_tool import (
    MAX_TOOL_ROUNDS,
    get_gemini_image_tool_declaration,
    get_gemini_tool_declaration,
)

_logger = get_logger("chatint.providers.gemini")

_GEMINI_THINKING_BUDGETS = {
    "minimal": 512,
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 24576,
}


class GeminiAdapter(ProviderAdapter):
    sdk_type = "gemini"

    def list_raw_models(self, client: Any) -> List[Any]:
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

    def complete_title(
        self, client: Any, model_id: str, system_instruction: str, user_snippet: str
    ) -> Optional[tuple[str, int, int, int]]:
        from google.genai import types as genai_types

        level_cfg = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=60,
            thinking_config=genai_types.ThinkingConfig(thinking_level="MINIMAL"),
        )
        budget_cfg = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=40,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        )
        plain_cfg = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=120,
        )
        configs = (
            [level_cfg, budget_cfg, plain_cfg]
            if is_gemini_3_or_newer(model_id)
            else [budget_cfg, level_cfg, plain_cfg]
        )
        resp = None
        for cfg in configs:
            try:
                resp = client.models.generate_content(
                    model=model_id,
                    contents=user_snippet,
                    config=cfg,
                )
                if (getattr(resp, "text", "") or "").strip():
                    break
            except Exception:
                resp = None

        if resp:
            raw_text = (getattr(resp, "text", "") or "").strip()
            usage = getattr(resp, "usage_metadata", None)
            return (
                raw_text,
                getattr(usage, "prompt_token_count", 0) or 0,
                getattr(usage, "candidates_token_count", 0) or 0,
                getattr(usage, "cached_content_token_count", 0) or 0,
            )
        return None

    def stream_chat(
        self,
        bridge: Any,
        tab_id: str,
        conversation_id: str,
        messages: List[Dict[str, str]],
        provider_id: str,
        model_id: str,
        thinking_level: str,
        assistant_msg_id: Optional[str],
        abort_event: threading.Event,
        user_question: str,
        web_search_enabled: bool = False,
        stream_id: Optional[str] = None,
    ) -> tuple[List[str], List[str], int, int, int, bool, str]:
        return _stream_gemini(
            bridge=bridge,
            tab_id=tab_id,
            conversation_id=conversation_id,
            messages=messages,
            provider_id=provider_id,
            model_id=model_id,
            thinking_level=thinking_level,
            assistant_msg_id=assistant_msg_id,
            abort_event=abort_event,
            user_question=user_question,
            web_search_enabled=web_search_enabled,
            stream_id=stream_id,
        )


def _stream_gemini(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    messages: List[Dict[str, str]],
    provider_id: str,
    model_id: str,
    thinking_level: str,
    assistant_msg_id: Optional[str],
    abort_event: threading.Event,
    user_question: str,
    web_search_enabled: bool = False,
    stream_id: Optional[str] = None,
) -> tuple[List[str], List[str], int, int, int, bool, str]:
    from google.genai import types as genai_types

    full_content: List[str] = []
    full_thinking: List[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    client, err = ClientManager.get_gemini_client(provider_id)
    if err or not client:
        _emit_error(
            bridge, tab_id, conversation_id, err or "Failed to initialize Gemini client.", stream_id=stream_id
        )
        return full_content, full_thinking, 0, 0, 0, True, "none"

    try:
        system_prompt, turns = _prepare_system_and_turns(messages)
        search_enabled = _resolve_search_enabled(web_search_enabled)
        system_prompt = _append_search_system_hint(system_prompt, search_enabled)

        contents: List[genai_types.Content] = []
        for t in turns:
            g_role = "model" if t["role"] == "assistant" else "user"
            contents.append(
                genai_types.Content(
                    role=g_role,
                    parts=[genai_types.Part.from_text(text=t["content"])],
                )
            )

        gemini_fn_tools = (
            [
                genai_types.Tool(
                    function_declarations=[
                        get_gemini_tool_declaration(),
                        get_gemini_image_tool_declaration(),
                    ]
                )
            ]
            if search_enabled
            else []
        )

        def _build_config(
            effort: Optional[str], mode: str = "level", with_tools: bool = True
        ) -> genai_types.GenerateContentConfig:
            cfg_kwargs: Dict[str, Any] = {}
            if system_prompt:
                cfg_kwargs["system_instruction"] = system_prompt
            if with_tools and gemini_fn_tools:
                cfg_kwargs["tools"] = gemini_fn_tools
            if effort is not None:
                if mode == "level":
                    cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                        include_thoughts=True,
                        thinking_level=effort,
                    )
                elif mode == "budget":
                    budget = _GEMINI_THINKING_BUDGETS.get(effort, 4096)
                    cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                        include_thoughts=True,
                        thinking_budget=budget,
                    )
            return genai_types.GenerateContentConfig(**cfg_kwargs)

        is_gen3_or_newer = is_gemini_3_or_newer(model_id)

        def _is_gemini_retryable_config_error(exc: Exception) -> bool:
            if isinstance(exc, (TypeError, ValueError, AttributeError)):
                return True
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if code in (400, 422):
                return True
            if code in (401, 403, 404, 429, 500, 502, 503, 504):
                return False
            low = str(exc).lower()
            if any(
                tok in low
                for tok in (
                    "401",
                    "403",
                    "404",
                    "429",
                    "unauthorized",
                    "permission denied",
                    "api_key_invalid",
                    "api key not valid",
                    "resource_exhausted",
                    "quota",
                    "timeout",
                    "timed out",
                    "connect",
                    "dns",
                    "network",
                )
            ):
                return False
            return any(
                tok in low
                for tok in (
                    "400",
                    "422",
                    "invalid_argument",
                    "thinking",
                    "budget",
                    "level",
                    "unsupported",
                    "not supported",
                    "tool",
                    "function calling",
                )
            )

        has_emitted_start = False
        stream_had_error = False
        actual_thinking_level = "none"

        for tool_round in range(MAX_TOOL_ROUNDS + 1):
            if abort_event.is_set():
                break
            if tool_round > 0:
                _ensure_post_tool_separator(full_content, bridge, tab_id, conversation_id, stream_id=stream_id)

            if tool_round == 0:
                effort_chain = get_unified_thinking_chain(thinking_level, provider_id, model_id)
            else:
                effort_chain = [None if actual_thinking_level == "none" else actual_thinking_level]

            use_tools_opts = [True, False] if (gemini_fn_tools and tool_round < MAX_TOOL_ROUNDS) else [False]
            active_stream = None
            last_err = None

            try:
                for effort in effort_chain:
                    modes = ["level", "budget"] if is_gen3_or_newer else ["budget", "level"]
                    if effort is None:
                        modes = ["plain"]

                    effort_connected = False
                    for mode in modes:
                        for with_tools in use_tools_opts:
                            try:
                                raw_stream = client.models.generate_content_stream(
                                    model=model_id,
                                    contents=contents,
                                    config=_build_config(effort, mode=mode, with_tools=with_tools),
                                )
                                stream_iter = iter(raw_stream)
                                try:
                                    first_chunk = next(stream_iter)
                                    active_stream = itertools.chain([first_chunk], stream_iter)
                                except StopIteration:
                                    active_stream = iter([])
                                actual_thinking_level = effort if effort is not None else "none"
                                record_resolved_thinking_level(
                                    provider_id, model_id, thinking_level, actual_thinking_level
                                )
                                last_err = None
                                effort_connected = True
                                break
                            except Exception as attempt_err:
                                if not _is_gemini_retryable_config_error(attempt_err):
                                    raise attempt_err
                                if not with_tools:
                                    clear_resolved_thinking_cache(provider_id, model_id, thinking_level)
                                last_err = attempt_err
                                continue
                        if effort_connected:
                            break
                    if effort_connected:
                        break

                if active_stream is None and last_err is not None:
                    raise last_err
            except Exception as e:
                stream_had_error = True
                _emit_api_error(
                    bridge,
                    tab_id,
                    conversation_id,
                    provider_id,
                    model_id,
                    e,
                    full_content,
                    full_thinking,
                    stream_id=stream_id,
                )
                return (
                    full_content,
                    full_thinking,
                    prompt_tokens,
                    completion_tokens,
                    cached_tokens,
                    True,
                    actual_thinking_level,
                )

            if not has_emitted_start:
                has_emitted_start = True
                _emit_start(
                    bridge,
                    tab_id,
                    conversation_id,
                    assistant_msg_id,
                    provider_id,
                    model_id,
                    actual_thinking_level,
                    stream_id=stream_id,
                )

            is_in_think_tag = False
            think_buffer = ""
            has_round_visible_text = [False]
            has_round_emitted_thinking = False
            round_in = 0
            round_out = 0
            round_cached = 0
            round_parts: List[Any] = []
            round_fn_calls: List[Any] = []

            def _consume_gemini_stream(stream_iter):
                nonlocal is_in_think_tag, think_buffer, round_in, round_out, round_cached, has_round_emitted_thinking
                for chunk in stream_iter:
                    if abort_event.is_set():
                        break

                    usage = getattr(chunk, "usage_metadata", None)
                    if usage:
                        p_tok = getattr(usage, "prompt_token_count", 0) or 0
                        c_tok = getattr(usage, "candidates_token_count", 0) or 0
                        t_tok = getattr(usage, "thoughts_token_count", 0) or 0
                        cache_tok = getattr(usage, "cached_content_token_count", 0) or 0
                        if p_tok:
                            round_in = p_tok
                        if c_tok or t_tok:
                            round_out = c_tok + t_tok
                        if cache_tok:
                            round_cached = cache_tok

                    candidates = getattr(chunk, "candidates", None)
                    if not candidates:
                        continue

                    for cand in candidates:
                        content_obj = getattr(cand, "content", None)
                        parts = getattr(content_obj, "parts", None) if content_obj else None
                        if not parts:
                            continue
                        for part in parts:
                            round_parts.append(part)
                            fc = getattr(part, "function_call", None)
                            if fc and getattr(fc, "name", None):
                                round_fn_calls.append(fc)
                            part_text = getattr(part, "text", None)
                            if not isinstance(part_text, str) or not part_text:
                                continue
                            if getattr(part, "thought", False):
                                if tool_round > 0 and not has_round_emitted_thinking:
                                    _ensure_post_tool_thinking_separator(
                                        full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id
                                    )
                                has_round_emitted_thinking = True
                                _emit_chunk(
                                    bridge,
                                    tab_id,
                                    conversation_id,
                                    "thinking_chunk",
                                    part_text,
                                    full_thinking,
                                    stream_id=stream_id,
                                )
                            else:
                                think_buffer, is_in_think_tag = _process_text_with_think_tags(
                                    part_text,
                                    think_buffer,
                                    is_in_think_tag,
                                    full_content,
                                    full_thinking,
                                    bridge,
                                    tab_id,
                                    conversation_id,
                                    stream_id=stream_id,
                                    has_round_visible_text=has_round_visible_text,
                                )

            try:
                _consume_gemini_stream(active_stream)
            except Exception as e:
                stream_had_error = True
                _logger.warning("Gemini streaming loop exception: %s", e)
                _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}", stream_id=stream_id)
                _flush_think_buffer(
                    think_buffer,
                    is_in_think_tag,
                    full_content,
                    full_thinking,
                    bridge,
                    tab_id,
                    conversation_id,
                    stream_id=stream_id,
                )
                prompt_tokens += round_in
                completion_tokens += round_out
                cached_tokens += round_cached
                return (
                    full_content,
                    full_thinking,
                    prompt_tokens,
                    completion_tokens,
                    cached_tokens,
                    stream_had_error,
                    actual_thinking_level,
                )

            _flush_think_buffer(
                think_buffer,
                is_in_think_tag,
                full_content,
                full_thinking,
                bridge,
                tab_id,
                conversation_id,
                stream_id=stream_id,
            )
            prompt_tokens += round_in
            completion_tokens += round_out
            cached_tokens += round_cached

            if not round_fn_calls or abort_event.is_set() or tool_round >= MAX_TOOL_ROUNDS:
                break

            valid_parts = [
                p
                for p in round_parts
                if getattr(p, "text", None)
                or getattr(p, "thought", False)
                or getattr(p, "function_call", None)
                or getattr(p, "thought_signature", None)
            ]
            contents.append(genai_types.Content(role="model", parts=valid_parts or round_parts))
            fn_response_parts = []
            for fc in round_fn_calls:
                if abort_event.is_set():
                    break
                fc_name = getattr(fc, "name", "") or ""
                fc_args = getattr(fc, "args", None) or {}
                if not isinstance(fc_args, dict):
                    try:
                        fc_args = dict(fc_args)
                    except Exception:
                        fc_args = {}
                res = _run_web_search_tool_call(
                    bridge, tab_id, conversation_id, fc_name, fc_args, abort_event, stream_id=stream_id
                )
                fn_response_parts.append(genai_types.Part.from_function_response(name=fc_name, response=res))

            if not fn_response_parts or abort_event.is_set():
                break
            contents.append(genai_types.Content(role="user", parts=fn_response_parts))

        return (
            full_content,
            full_thinking,
            prompt_tokens,
            completion_tokens,
            cached_tokens,
            stream_had_error,
            actual_thinking_level,
        )
    finally:
        ClientManager.close_client(client)
