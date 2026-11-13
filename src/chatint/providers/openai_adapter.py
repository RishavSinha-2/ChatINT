"""OpenAI & OpenAI-compatible ProviderAdapter implementation."""
from __future__ import annotations

import itertools
import json
import re
import threading
from typing import Any, Dict, List, Optional

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
    get_openai_image_tool_spec,
    get_openai_tool_spec,
)

_logger = get_logger("chatint.providers.openai")


def _is_openai_retryable_param_error(api_err: Any) -> bool:
    return getattr(api_err, "status_code", None) in (400, 422)


def _clamp_openai_effort(provider_id: str, model_id: str, effort: Optional[str]) -> Optional[str]:
    """
    Clamp unified thinking effort tiers to values supported by the target OpenAI / OpenAI-compatible model:
    - gpt-5* supports minimal, low, medium, high, xhigh (clamp 'max' -> 'xhigh').
    - o1 / o3 / o4 models support low, medium, high (clamp 'minimal' -> 'low', 'xhigh'/'max' -> 'high').
    - Non-'openai' OpenAI-compatible endpoints (DeepSeek, OpenRouter, Groq, Ollama, custom) use standard
      OpenAI reasoning_effort ('low' | 'medium' | 'high') unless targeting gpt-5*.
    """
    if effort is None:
        return None
    pid = (provider_id or "").strip().lower()
    mid = (model_id or "").strip().lower()

    if "gpt-5" in mid:
        return "xhigh" if effort == "max" else effort

    if re.search(r"(?:^|/|-)(?:o1|o3|o4)(?:$|[-_])", mid) or pid != "openai":
        if effort == "minimal":
            return "low"
        if effort in ("xhigh", "max"):
            return "high"

    return effort


def _uses_deepseek_reasoning_replay(provider_id: str, model_id: str, base_url: str = "") -> bool:
    pid = (provider_id or "").strip().lower()
    mid = (model_id or "").strip().lower()
    url = (base_url or "").strip().lower()
    return pid == "deepseek" or "deepseek" in mid or "deepseek" in url


class OpenAIAdapter(ProviderAdapter):
    sdk_type = "openai"

    def list_raw_models(self, client: Any) -> List[Any]:
        model_list = client.models.list()
        items = []
        for m in getattr(model_list, "data", None) or list(model_list):
            mid = (getattr(m, "id", "") or "").strip()
            if mid:
                created = getattr(m, "created", 0) or 0
                items.append({"id": mid, "created": created})
        return items

    def complete_title(
        self, client: Any, model_id: str, system_instruction: str, user_snippet: str
    ) -> Optional[tuple[str, int, int, int]]:
        base_msgs = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_snippet},
        ]
        resp = None
        for tok_kw in ({"max_completion_tokens": 60}, {"max_tokens": 40}):
            try:
                resp = client.chat.completions.create(
                    model=model_id,
                    messages=base_msgs,
                    stream=False,
                    **tok_kw,
                )
                break
            except Exception:
                resp = None

        if resp and getattr(resp, "choices", None) and resp.choices[0].message:
            raw_text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            details = getattr(usage, "prompt_tokens_details", None)
            return (
                raw_text,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                getattr(details, "cached_tokens", 0) if details else 0,
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
        return _stream_openai(
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


def _stream_openai(
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
    import openai

    full_content: List[str] = []
    full_thinking: List[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    client, err = ClientManager.get_client(provider_id)
    if err or not client:
        _emit_error(bridge, tab_id, conversation_id, err or "Failed to initialize API client.", stream_id=stream_id)
        return full_content, full_thinking, 0, 0, 0, True, "none"

    prov_info = ClientManager.get_provider(provider_id) or {}
    prov_base_url = str(prov_info.get("base_url") or "")
    replay_reasoning_on_tools = _uses_deepseek_reasoning_replay(provider_id, model_id, prov_base_url)

    try:
        system_prompt, turns = _prepare_system_and_turns(messages)
        search_enabled = _resolve_search_enabled(web_search_enabled)
        system_prompt = _append_search_system_hint(system_prompt, search_enabled)
        normalized_messages: List[Dict[str, Any]] = (
            [{"role": "system", "content": system_prompt}] if system_prompt else []
        ) + [dict(t) for t in turns]
        openai_tools = [get_openai_tool_spec(), get_openai_image_tool_spec()] if search_enabled else []

        has_emitted_start = False
        stream_had_error = False
        actual_thinking_level = "none"
        include_stream_options = True

        for tool_round in range(MAX_TOOL_ROUNDS + 1):
            if abort_event.is_set():
                break
            if tool_round > 0:
                _ensure_post_tool_separator(full_content, bridge, tab_id, conversation_id, stream_id=stream_id)

            round_tools = openai_tools if tool_round < MAX_TOOL_ROUNDS else []
            response_stream = None
            last_api_err = None

            if tool_round == 0:
                raw_chain = get_unified_thinking_chain(thinking_level, provider_id, model_id)
                effort_chain: List[Optional[str]] = []
                for raw_eff in raw_chain:
                    clamped = _clamp_openai_effort(provider_id, model_id, raw_eff)
                    if clamped not in effort_chain:
                        effort_chain.append(clamped)
            else:
                effort_chain = [None if actual_thinking_level == "none" else actual_thinking_level]

            def _peek_and_wrap(raw_stream: Any):
                stream_iter = iter(raw_stream)
                try:
                    first_chunk = next(stream_iter)
                    return itertools.chain([first_chunk], stream_iter)
                except StopIteration:
                    return iter([])

            def _try_openai_create(eff_val: Optional[str], use_tools: bool):
                nonlocal include_stream_options, actual_thinking_level, last_api_err
                kw: Dict[str, Any] = {
                    "model": model_id,
                    "messages": normalized_messages,
                    "stream": True,
                }
                if include_stream_options:
                    kw["stream_options"] = {"include_usage": True}
                if eff_val is not None:
                    kw["reasoning_effort"] = eff_val
                if use_tools and round_tools:
                    kw["tools"] = round_tools
                    kw["tool_choice"] = "auto"

                try:
                    raw_stream = client.chat.completions.create(**kw)
                    wrapped_stream = _peek_and_wrap(raw_stream)
                    actual_thinking_level = eff_val if eff_val is not None else "none"
                    record_resolved_thinking_level(provider_id, model_id, thinking_level, actual_thinking_level)
                    last_api_err = None
                    return wrapped_stream
                except openai.APIStatusError as api_err:
                    if not _is_openai_retryable_param_error(api_err):
                        raise
                    last_api_err = api_err
                    if include_stream_options and "stream_options" in str(api_err).lower():
                        include_stream_options = False
                        kw.pop("stream_options", None)
                        try:
                            raw_stream = client.chat.completions.create(**kw)
                            wrapped_stream = _peek_and_wrap(raw_stream)
                            actual_thinking_level = eff_val if eff_val is not None else "none"
                            record_resolved_thinking_level(provider_id, model_id, thinking_level, actual_thinking_level)
                            last_api_err = None
                            return wrapped_stream
                        except openai.APIStatusError as retry_err:
                            if not _is_openai_retryable_param_error(retry_err):
                                raise
                            last_api_err = retry_err
                    # Only clear cached thinking level if the attempt without tools also failed
                    if not (use_tools and round_tools):
                        clear_resolved_thinking_cache(provider_id, model_id, thinking_level)
                    return None

            try:
                for effort in effort_chain:
                    if round_tools:
                        response_stream = _try_openai_create(effort, True)
                        if response_stream is not None:
                            break
                        # Test same effort WITHOUT tools before downgrading effort so tool-unsupported 400s
                        # never poison resolved_thinking to 'none'!
                        response_stream = _try_openai_create(effort, False)
                        if response_stream is not None:
                            break
                    else:
                        response_stream = _try_openai_create(effort, False)
                        if response_stream is not None:
                            break

                if response_stream is None and last_api_err is not None:
                    raise last_api_err
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
            round_text_parts: List[str] = []
            round_reasoning_parts: List[str] = []
            tool_calls_acc: Dict[int, Dict[str, str]] = {}

            try:
                for chunk in response_stream:
                    if abort_event.is_set():
                        break

                    if hasattr(chunk, "usage") and chunk.usage:
                        u = chunk.usage
                        u_in = getattr(u, "prompt_tokens", 0) or 0
                        u_out = getattr(u, "completion_tokens", 0) or 0
                        if u_in and isinstance(u_in, (int, float)):
                            round_in = int(u_in)
                        if u_out and isinstance(u_out, (int, float)):
                            round_out = int(u_out)
                        pt_details = getattr(u, "prompt_tokens_details", None)
                        if pt_details:
                            c_tok = getattr(pt_details, "cached_tokens", 0) or 0
                            if c_tok and isinstance(c_tok, (int, float)):
                                round_cached = int(c_tok)
                        if not round_cached and hasattr(u, "prompt_cache_hit_tokens"):
                            hit_tok = getattr(u, "prompt_cache_hit_tokens", 0) or 0
                            if hit_tok and isinstance(hit_tok, (int, float)):
                                round_cached = int(hit_tok)

                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    reasoning_chunk = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                    if isinstance(reasoning_chunk, str) and reasoning_chunk:
                        if tool_round > 0 and not has_round_emitted_thinking:
                            _ensure_post_tool_thinking_separator(
                                full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id
                            )
                        has_round_emitted_thinking = True
                        round_reasoning_parts.append(reasoning_chunk)
                        _emit_chunk(
                            bridge,
                            tab_id,
                            conversation_id,
                            "thinking_chunk",
                            reasoning_chunk,
                            full_thinking,
                            stream_id=stream_id,
                        )

                    content_chunk = getattr(delta, "content", None)
                    if isinstance(content_chunk, str) and content_chunk:
                        round_text_parts.append(content_chunk)
                        think_buffer, is_in_think_tag = _process_text_with_think_tags(
                            content_chunk,
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

                    for tc in getattr(delta, "tool_calls", None) or []:
                        tc_idx = getattr(tc, "index", 0) or 0
                        entry = tool_calls_acc.setdefault(tc_idx, {"id": "", "name": "", "arguments": ""})
                        if getattr(tc, "id", None):
                            entry["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn:
                            if getattr(fn, "name", None):
                                entry["name"] += fn.name
                            if getattr(fn, "arguments", None):
                                entry["arguments"] += fn.arguments

            except Exception as e:
                stream_had_error = True
                _logger.warning("OpenAI streaming loop exception: %s", e)
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

            ordered_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc) if tool_calls_acc[i].get("name")]
            if not ordered_calls or abort_event.is_set() or tool_round >= MAX_TOOL_ROUNDS:
                break

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": "".join(round_text_parts) or None,
                "tool_calls": [
                    {
                        "id": tc["id"] or f"call_{tool_round}_{idx}",
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"},
                    }
                    for idx, tc in enumerate(ordered_calls)
                ],
            }
            reasoning_str = "".join(round_reasoning_parts)
            if replay_reasoning_on_tools and reasoning_str:
                assistant_msg["reasoning_content"] = reasoning_str
            normalized_messages.append(assistant_msg)

            for idx, tc in enumerate(ordered_calls):
                if abort_event.is_set():
                    break
                call_id = tc["id"] or f"call_{tool_round}_{idx}"
                res = _run_web_search_tool_call(
                    bridge,
                    tab_id,
                    conversation_id,
                    tc["name"],
                    tc.get("arguments") or "{}",
                    abort_event,
                    stream_id=stream_id,
                )
                normalized_messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(res, ensure_ascii=False),
                })

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
