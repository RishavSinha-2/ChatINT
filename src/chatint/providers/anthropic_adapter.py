"""Anthropic Claude native SDK ProviderAdapter implementation."""
from __future__ import annotations

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
    get_anthropic_image_tool_spec,
    get_anthropic_tool_spec,
)

_logger = get_logger("chatint.providers.anthropic")

_ANTHROPIC_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 32000,
}


def _is_anthropic_adaptive_model(model_id: str) -> bool:
    """Return True for Claude 4.6+, 5.x, and Mythos models that use adaptive thinking + effort."""
    mid = (model_id or "").lower()
    if "mythos" in mid:
        return True
    match = re.search(r"claude(?:-[a-z]+)*-(\d+)(?:[.-](\d{1,2})(?=[-._@]|$))?", mid)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) is not None else 0
    return major > 4 or (major == 4 and minor >= 6)


def _build_anthropic_effort_candidates(model_id: str, effort: Optional[str]) -> List[Dict[str, Any]]:
    """Build candidate parameter overrides for a specific thinking effort on Anthropic."""
    if effort is None:
        return [
            {"thinking": {"type": "disabled"}, "max_tokens": 8192},
            {"max_tokens": 8192},
            {"max_tokens": 4096},
        ]

    adaptive_candidate = {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": effort},
        "max_tokens": 16384,
    }
    budget = _ANTHROPIC_THINKING_BUDGETS.get(effort, 4096)
    budget_candidate = {
        "thinking": {"type": "enabled", "budget_tokens": budget},
        "max_tokens": max(8192, budget + 4096),
    }

    if _is_anthropic_adaptive_model(model_id):
        return [adaptive_candidate, budget_candidate]
    return [budget_candidate, adaptive_candidate]


class AnthropicAdapter(ProviderAdapter):
    sdk_type = "anthropic"

    def list_raw_models(self, client: Any) -> List[Any]:
        page = client.models.list(limit=100)
        items = getattr(page, "data", None) or list(page)
        result = []
        for m in items:
            mid = (getattr(m, "id", "") or "").strip()
            if mid:
                result.append(mid)
        return result

    def complete_title(
        self, client: Any, model_id: str, system_instruction: str, user_snippet: str
    ) -> Optional[tuple[str, int, int, int]]:
        resp = client.messages.create(
            model=model_id,
            system=system_instruction,
            messages=[{"role": "user", "content": user_snippet}],
            max_tokens=25,
        )
        if resp and getattr(resp, "content", None):
            text_blocks = [getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"]
            usage = getattr(resp, "usage", None)
            return (
                " ".join(text_blocks),
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
                getattr(usage, "cache_read_input_tokens", 0) or 0,
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
        return _stream_anthropic(
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


def _stream_anthropic(
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
    import anthropic

    full_content: List[str] = []
    full_thinking: List[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    client, err = ClientManager.get_anthropic_client(provider_id)
    if err or not client:
        _emit_error(
            bridge, tab_id, conversation_id, err or "Failed to initialize Anthropic client.", stream_id=stream_id
        )
        return full_content, full_thinking, 0, 0, 0, True, "none"

    try:
        system_prompt, raw_turns = _prepare_system_and_turns(messages)
        search_enabled = _resolve_search_enabled(web_search_enabled)
        system_prompt = _append_search_system_hint(system_prompt, search_enabled)
        turns: List[Dict[str, Any]] = list(raw_turns)
        anthropic_tools = [get_anthropic_tool_spec(), get_anthropic_image_tool_spec()] if search_enabled else []

        has_emitted_start = False
        stream_had_error = False
        actual_thinking_level = "none"

        for tool_round in range(MAX_TOOL_ROUNDS + 1):
            if abort_event.is_set():
                break
            if tool_round > 0:
                _ensure_post_tool_separator(full_content, bridge, tab_id, conversation_id, stream_id=stream_id)

            base_kwargs: Dict[str, Any] = {
                "model": model_id,
                "messages": turns,
                "stream": True,
            }
            if system_prompt:
                base_kwargs["system"] = system_prompt

            if tool_round == 0:
                effort_chain = get_unified_thinking_chain(thinking_level, provider_id, model_id)
            else:
                effort_chain = [None if actual_thinking_level == "none" else actual_thinking_level]

            response_stream = None
            last_api_err = None
            try:
                for effort in effort_chain:
                    effort_candidates = _build_anthropic_effort_candidates(model_id, effort)
                    attempt_list: List[Dict[str, Any]] = []
                    for cand in effort_candidates:
                        if anthropic_tools and tool_round < MAX_TOOL_ROUNDS:
                            attempt_list.append({**base_kwargs, **cand, "tools": anthropic_tools})
                        attempt_list.append({**base_kwargs, **cand})

                    effort_succeeded = False
                    for attempt_kwargs in attempt_list:
                        try:
                            response_stream = client.messages.create(**attempt_kwargs)
                            actual_thinking_level = effort if effort is not None else "none"
                            record_resolved_thinking_level(provider_id, model_id, thinking_level, actual_thinking_level)
                            last_api_err = None
                            effort_succeeded = True
                            break
                        except anthropic.APIStatusError as api_err:
                            if api_err.status_code in (400, 422):
                                if "tools" not in attempt_kwargs:
                                    clear_resolved_thinking_cache(provider_id, model_id, thinking_level)
                                last_api_err = api_err
                                continue
                            raise
                    if effort_succeeded:
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
            round_base_in = 0
            round_cache_read = 0
            round_cache_create = 0
            round_in = 0
            round_out = 0
            round_cached = 0
            blocks_by_idx: Dict[int, Dict[str, Any]] = {}

            try:
                for event in response_stream:
                    if abort_event.is_set():
                        break

                    ev_type = getattr(event, "type", "")
                    if ev_type == "message_start":
                        msg_obj = getattr(event, "message", None)
                        usage = getattr(msg_obj, "usage", None) if msg_obj else None
                        if usage:
                            round_base_in = getattr(usage, "input_tokens", 0) or 0
                            round_cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                            round_cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
                            out_tok = getattr(usage, "output_tokens", 0) or 0
                            round_cached = round_cache_read
                            round_in = round_base_in + round_cache_read + round_cache_create
                            if out_tok:
                                round_out = out_tok

                    elif ev_type == "message_delta":
                        usage = getattr(event, "usage", None)
                        if usage:
                            out_tok = getattr(usage, "output_tokens", 0) or 0
                            if out_tok:
                                round_out = out_tok
                            in_tok = getattr(usage, "input_tokens", 0) or 0
                            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
                            if in_tok:
                                round_base_in = in_tok
                            if cache_read:
                                round_cache_read = cache_read
                            if cache_create:
                                round_cache_create = cache_create
                            if in_tok or cache_read or cache_create:
                                round_cached = round_cache_read
                                round_in = round_base_in + round_cache_read + round_cache_create

                    elif ev_type == "content_block_start":
                        idx = getattr(event, "index", 0)
                        cb = getattr(event, "content_block", None)
                        cb_type = getattr(cb, "type", "") if cb else ""
                        if cb_type == "thinking":
                            init_think = getattr(cb, "thinking", "") or ""
                            blocks_by_idx[idx] = {
                                "type": "thinking",
                                "thinking": init_think,
                                "signature": getattr(cb, "signature", "") or "",
                            }
                            if init_think:
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
                                    init_think,
                                    full_thinking,
                                    stream_id=stream_id,
                                )
                        elif cb_type == "redacted_thinking":
                            blocks_by_idx[idx] = {
                                "type": "redacted_thinking",
                                "data": getattr(cb, "data", "") or "",
                            }
                        elif cb_type == "text":
                            initial_text = getattr(cb, "text", "") or ""
                            blocks_by_idx[idx] = {"type": "text", "text": initial_text}
                            if initial_text:
                                think_buffer, is_in_think_tag = _process_text_with_think_tags(
                                    initial_text,
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
                        elif cb_type == "tool_use":
                            init_inp = getattr(cb, "input", None)
                            blocks_by_idx[idx] = {
                                "type": "tool_use",
                                "id": getattr(cb, "id", "") or f"toolu_{tool_round}_{idx}",
                                "name": getattr(cb, "name", "") or "",
                                "initial_input": init_inp if isinstance(init_inp, dict) else {},
                                "input_parts": [],
                            }

                    elif ev_type == "content_block_delta":
                        idx = getattr(event, "index", 0)
                        delta = getattr(event, "delta", None)
                        delta_type = getattr(delta, "type", "") if delta else ""
                        block = blocks_by_idx.get(idx)
                        if delta_type == "thinking_delta":
                            chunk_t = getattr(delta, "thinking", "") or ""
                            if block is not None:
                                block["thinking"] = block.get("thinking", "") + chunk_t
                            if chunk_t:
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
                                    chunk_t,
                                    full_thinking,
                                    stream_id=stream_id,
                                )
                        elif delta_type == "signature_delta":
                            if block is not None:
                                block["signature"] = block.get("signature", "") + (getattr(delta, "signature", "") or "")
                        elif delta_type == "text_delta":
                            c_chunk = getattr(delta, "text", "") or ""
                            if block is not None:
                                block["text"] = block.get("text", "") + c_chunk
                            if c_chunk:
                                think_buffer, is_in_think_tag = _process_text_with_think_tags(
                                    c_chunk,
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
                        elif delta_type == "input_json_delta":
                            if block is not None:
                                block.setdefault("input_parts", []).append(getattr(delta, "partial_json", "") or "")

            except Exception as e:
                stream_had_error = True
                _logger.warning("Anthropic streaming loop exception: %s", e)
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

            ordered_blocks = [blocks_by_idx[i] for i in sorted(blocks_by_idx)]
            tool_calls = [b for b in ordered_blocks if b.get("type") == "tool_use"]
            if not tool_calls or abort_event.is_set() or tool_round >= MAX_TOOL_ROUNDS:
                break

            assistant_blocks: List[Dict[str, Any]] = []
            for b in ordered_blocks:
                btype = b.get("type")
                if btype == "thinking":
                    if b.get("thinking") or b.get("signature"):
                        assistant_blocks.append({
                            "type": "thinking",
                            "thinking": b.get("thinking", ""),
                            "signature": b.get("signature", ""),
                        })
                elif btype == "redacted_thinking":
                    assistant_blocks.append({"type": "redacted_thinking", "data": b.get("data", "")})
                elif btype == "text":
                    if b.get("text"):
                        assistant_blocks.append({"type": "text", "text": b["text"]})
                elif btype == "tool_use":
                    raw_json = "".join(b.pop("input_parts", [])).strip()
                    init_inp = b.pop("initial_input", {})
                    if raw_json:
                        try:
                            parsed_input = json.loads(raw_json)
                        except Exception:
                            parsed_input = {"query": raw_json}
                    else:
                        parsed_input = init_inp if isinstance(init_inp, dict) else {}
                    assistant_blocks.append({
                        "type": "tool_use",
                        "id": b["id"],
                        "name": b["name"],
                        "input": parsed_input,
                    })

            tool_results: List[Dict[str, Any]] = []
            for tb in assistant_blocks:
                if tb.get("type") != "tool_use":
                    continue
                if abort_event.is_set():
                    break
                res = _run_web_search_tool_call(
                    bridge,
                    tab_id,
                    conversation_id,
                    tb["name"],
                    tb.get("input") or {},
                    abort_event,
                    stream_id=stream_id,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tb["id"],
                    "content": json.dumps(res, ensure_ascii=False),
                })

            if not tool_results or abort_event.is_set():
                break

            turns.append({"role": "assistant", "content": assistant_blocks})
            turns.append({"role": "user", "content": tool_results})

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
