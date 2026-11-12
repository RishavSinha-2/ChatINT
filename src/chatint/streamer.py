from __future__ import annotations

import json
import re
import threading
from typing import Any, Dict, List, Optional
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

from chatint.client_manager import ClientManager
from chatint.db import add_message, get_conversation, get_token_stats, record_token_usage, update_conversation
from chatint.naming import generate_chat_title
from chatint.web_search_tool import (
    MAX_TOOL_ROUNDS,
    WEB_SEARCH_TOOL_NAME,
    execute_web_search,
    get_anthropic_tool_spec,
    get_gemini_tool_declaration,
    get_openai_tool_spec,
    get_web_search_system_hint,
    is_web_search_available,
)


class StreamManager:
    _abort_events: Dict[str, threading.Event] = {}
    _lock = threading.Lock()

    @classmethod
    def abort_stream(cls, tab_id: str):
        with cls._lock:
            event = cls._abort_events.get(tab_id)
            if event:
                event.set()

    @classmethod
    def register_stream(cls, tab_id: str) -> threading.Event:
        with cls._lock:
            event = threading.Event()
            cls._abort_events[tab_id] = event
            return event

    @classmethod
    def unregister_stream(cls, tab_id: str):
        with cls._lock:
            cls._abort_events.pop(tab_id, None)


def _safe_dispatch(bridge: Any, event_data: Dict[str, Any]):
    if not bridge:
        return
    try:
        if hasattr(bridge, "push_event"):
            bridge.push_event(event_data)
    except Exception as e:
        print(f"[Streamer Dispatch Error] {e}")


def _emit_error(bridge: Any, tab_id: str, conversation_id: str, message: str):
    _safe_dispatch(bridge, {"type": "error", "tab_id": tab_id, "conversation_id": conversation_id, "error": message})


def _emit_start(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    assistant_msg_id: Optional[str],
    provider_id: str,
    model_id: str,
    thinking_level: str,
):
    _safe_dispatch(
        bridge,
        {
            "type": "start",
            "tab_id": tab_id,
            "conversation_id": conversation_id,
            "assistant_msg_id": assistant_msg_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "thinking_level": thinking_level,
        },
    )


def _emit_chunk(bridge: Any, tab_id: str, conversation_id: str, chunk_type: str, text: str, collector: List[str]):
    if not text:
        return
    collector.append(text)
    _safe_dispatch(bridge, {"type": chunk_type, "tab_id": tab_id, "conversation_id": conversation_id, "chunk": text})


def _flush_think_buffer(think_buffer: str, is_in_think_tag: bool, full_content: List[str], full_thinking: List[str]):
    if think_buffer:
        (full_thinking if is_in_think_tag else full_content).append(think_buffer)


def _resolve_search_enabled(web_search_enabled: bool) -> bool:
    return bool(web_search_enabled) and is_web_search_available()[0]


def _append_search_system_hint(system_prompt: str, search_enabled: bool) -> str:
    if not search_enabled:
        return system_prompt
    hint = get_web_search_system_hint()
    return f"{system_prompt}\n\n{hint}".strip() if system_prompt else hint


def _parse_tool_query(raw_args: Any) -> str:
    if isinstance(raw_args, dict):
        return str(raw_args.get("query") or "").strip()
    if isinstance(raw_args, str):
        s = raw_args.strip()
        if not s:
            return ""
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return str(parsed.get("query") or "").strip()
        except Exception:
            return s
    return ""


def _run_web_search_tool_call(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    tool_name: str,
    raw_args: Any,
    abort_event: threading.Event,
) -> Dict[str, Any]:
    """Execute a web_search tool call from any provider, emitting live UI status events."""
    if tool_name != WEB_SEARCH_TOOL_NAME:
        return {"ok": False, "error": f"Unsupported tool '{tool_name}'."}

    query = _parse_tool_query(raw_args)
    _safe_dispatch(
        bridge,
        {
            "type": "tool_status",
            "tab_id": tab_id,
            "conversation_id": conversation_id,
            "status": "searching",
            "query": query,
        },
    )

    if abort_event.is_set():
        return {"ok": False, "error": "Aborted by user."}

    result = execute_web_search(query, conversation_id=conversation_id)
    _safe_dispatch(
        bridge,
        {
            "type": "tool_status",
            "tab_id": tab_id,
            "conversation_id": conversation_id,
            "status": "done",
            "query": query,
            "ok": bool(result.get("ok")),
            "search_requests_used": int(result.get("search_requests_used") or 0),
            "sources": result.get("sources") or [],
        },
    )
    return result


def _ensure_post_tool_separator(full_content: List[str], bridge: Any, tab_id: str, conversation_id: str):
    """Ensure a blank line separates pre-tool text from post-tool response text."""
    if full_content and "".join(full_content).strip() and not "".join(full_content).endswith(("\n", " ")):
        _emit_chunk(bridge, tab_id, conversation_id, "content_chunk", "\n\n", full_content)


_ANTHROPIC_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 32000,
}

# Anthropic output_config.effort accepts 5 tags: low, medium, high, xhigh, max.
_ANTHROPIC_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
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


def _build_anthropic_attempts(model_id: str, thinking_level: str) -> List[Dict[str, Any]]:
    """Build ordered Anthropic create() parameter overrides for 400/422 fallback."""
    plain_attempts: List[Dict[str, Any]] = [{"max_tokens": 8192}, {"max_tokens": 4096}]
    if not thinking_level or thinking_level == "none":
        return [{"thinking": {"type": "disabled"}, "max_tokens": 8192}, *plain_attempts]

    effort = _ANTHROPIC_EFFORT_MAP.get(thinking_level, "medium")
    effort_chain = [effort]
    if effort == "max":
        effort_chain.extend(["xhigh", "high"])
    elif effort == "xhigh":
        effort_chain.append("high")

    adaptive_attempts: List[Dict[str, Any]] = [
        {
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": level},
            "max_tokens": 16384,
        }
        for level in effort_chain
    ]

    budget = _ANTHROPIC_THINKING_BUDGETS.get(thinking_level, 4096)
    budget_attempts: List[Dict[str, Any]] = [
        {
            "thinking": {"type": "enabled", "budget_tokens": budget},
            "max_tokens": max(8192, budget + 4096),
        }
    ]
    if budget > 4096:
        budget_attempts.append(
            {
                "thinking": {"type": "enabled", "budget_tokens": 4096},
                "max_tokens": 8192,
            }
        )

    if _is_anthropic_adaptive_model(model_id):
        return adaptive_attempts + budget_attempts + plain_attempts
    return budget_attempts + adaptive_attempts + plain_attempts

_GEMINI_THINKING_BUDGETS = {
    "minimal": 512,
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 24576,
}


def _trigger_auto_naming(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    user_question: str,
    provider_id: str,
    model_id: str,
):
    def _run_naming():
        try:
            conv = get_conversation(conversation_id)
            current_title = (conv.get("title") or "").strip() if conv else ""
            assistant_count = (
                sum(1 for m in conv.get("messages", []) if m.get("role") == "assistant")
                if conv
                else 0
            )
            is_default = current_title.lower() in ["new chat", ""]

            if is_default and assistant_count <= 1:
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
                update_conversation(conversation_id, title=final_title)
            else:
                final_title = current_title or "New Chat"

            _safe_dispatch(
                bridge,
                {
                    "type": "chat_named",
                    "tab_id": tab_id,
                    "conversation_id": conversation_id,
                    "title": final_title,
                    "token_stats": get_token_stats(conversation_id),
                },
            )
        except Exception as ex:
            print(f"[Naming Error] {ex}")

    threading.Thread(target=_run_naming, daemon=True).start()


def _process_text_with_think_tags(
    content_chunk: str,
    think_buffer: str,
    is_in_think_tag: bool,
    full_content: List[str],
    full_thinking: List[str],
    bridge: Any,
    tab_id: str,
    conversation_id: str,
) -> tuple[str, bool]:
    text = think_buffer + content_chunk
    think_buffer = ""

    while text:
        tag = "</think>" if is_in_think_tag else "<think>"
        chunk_type = "thinking_chunk" if is_in_think_tag else "content_chunk"
        target_list = full_thinking if is_in_think_tag else full_content

        if tag in text:
            before, rest = text.split(tag, 1)
            if before and (is_in_think_tag or before.strip() or full_content):
                _emit_chunk(bridge, tab_id, conversation_id, chunk_type, before, target_list)
            is_in_think_tag = not is_in_think_tag
            text = rest
            continue

        # Check if trailing characters could be an incomplete <think> or </think> prefix
        partial_len = next((i for i in range(len(tag) - 1, 0, -1) if tag.startswith(text[-i:])), 0)
        if partial_len:
            think_buffer = text[-partial_len:]
            pre = text[:-partial_len]
            if pre:
                _emit_chunk(bridge, tab_id, conversation_id, chunk_type, pre, target_list)
            break

        _emit_chunk(bridge, tab_id, conversation_id, chunk_type, text, target_list)
        break

    return think_buffer, is_in_think_tag


def _prepare_system_and_turns(messages: List[Dict[str, str]]) -> tuple[str, List[Dict[str, str]]]:
    """
    Extracts system prompt and normalizes user/assistant conversation turns so that switching
    between OpenAI, Anthropic, and Gemini models mid-conversation works seamlessly.
    """
    system_parts: List[str] = []
    turns: List[Dict[str, str]] = []

    for m in messages:
        role = m.get("role", "user")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        norm_role = "assistant" if role == "assistant" else "user"
        if turns and turns[-1]["role"] == norm_role:
            turns[-1]["content"] = turns[-1]["content"] + "\n\n" + content
        else:
            turns.append({"role": norm_role, "content": content})

    # Anthropic and Gemini require the first turn to be 'user'
    while len(turns) > 1 and turns[0]["role"] == "assistant":
        turns = turns[1:]
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "Hello"})

    # Models require the last turn to be 'user' when generating assistant completions
    while len(turns) > 1 and turns[-1]["role"] == "assistant":
        turns.pop()

    return "\n\n".join(system_parts).strip(), turns


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
) -> tuple[List[str], List[str], int, int, int, bool]:
    import anthropic

    full_content: List[str] = []
    full_thinking: List[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    client, err = ClientManager.get_anthropic_client(provider_id)
    if err or not client:
        _emit_error(bridge, tab_id, conversation_id, err or "Failed to initialize Anthropic client.")
        return full_content, full_thinking, 0, 0, 0, True

    _emit_start(bridge, tab_id, conversation_id, assistant_msg_id, provider_id, model_id, thinking_level)

    system_prompt, raw_turns = _prepare_system_and_turns(messages)
    search_enabled = _resolve_search_enabled(web_search_enabled)
    system_prompt = _append_search_system_hint(system_prompt, search_enabled)
    turns: List[Dict[str, Any]] = list(raw_turns)
    anthropic_tools = [get_anthropic_tool_spec()] if search_enabled else []

    has_named_chat = False
    stream_had_error = False

    for tool_round in range(MAX_TOOL_ROUNDS + 1):
        if abort_event.is_set():
            break
        if tool_round > 0:
            _ensure_post_tool_separator(full_content, bridge, tab_id, conversation_id)

        base_kwargs: Dict[str, Any] = {
            "model": model_id,
            "messages": turns,
            "stream": True,
        }
        if system_prompt:
            base_kwargs["system"] = system_prompt

        attempts = _build_anthropic_attempts(model_id, thinking_level)
        if anthropic_tools and tool_round < MAX_TOOL_ROUNDS:
            candidates = [{**base_kwargs, **a, "tools": anthropic_tools} for a in attempts] + [
                {**base_kwargs, **a} for a in attempts
            ]
        else:
            candidates = [{**base_kwargs, **a} for a in attempts]

        response_stream = None
        last_api_err = None
        try:
            for attempt_kwargs in candidates:
                try:
                    response_stream = client.messages.create(**attempt_kwargs)
                    break
                except anthropic.APIStatusError as api_err:
                    if api_err.status_code in (400, 422):
                        last_api_err = api_err
                        continue
                    raise
            if response_stream is None and last_api_err is not None:
                raise last_api_err
        except Exception as e:
            if full_content or full_thinking:
                stream_had_error = True
                _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}")
            else:
                _emit_error(bridge, tab_id, conversation_id, f"API Error ({provider_id} / {model_id}): {e}")
                stream_had_error = True
            return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error

        is_in_think_tag = False
        think_buffer = ""
        round_in = 0
        round_out = 0
        round_cached = 0
        blocks_by_idx: Dict[int, Dict[str, Any]] = {}

        try:
            for event in response_stream:
                if abort_event.is_set():
                    break

                if not has_named_chat:
                    has_named_chat = True
                    _trigger_auto_naming(bridge, tab_id, conversation_id, user_question, provider_id, model_id)

                ev_type = getattr(event, "type", "")
                if ev_type == "message_start":
                    msg_obj = getattr(event, "message", None)
                    usage = getattr(msg_obj, "usage", None) if msg_obj else None
                    if usage:
                        in_tok = getattr(usage, "input_tokens", 0) or 0
                        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
                        out_tok = getattr(usage, "output_tokens", 0) or 0
                        round_cached = cache_read
                        round_in = in_tok + cache_read + cache_create
                        if out_tok:
                            round_out = out_tok

                elif ev_type == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage:
                        out_tok = getattr(usage, "output_tokens", 0) or 0
                        if out_tok:
                            round_out = out_tok
                        in_tok = getattr(usage, "input_tokens", None)
                        cache_read = getattr(usage, "cache_read_input_tokens", None)
                        cache_create = getattr(usage, "cache_creation_input_tokens", None)
                        if in_tok is not None or cache_read is not None:
                            round_cached = cache_read or round_cached
                            round_in = (in_tok or 0) + round_cached + (cache_create or 0)

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
                            _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", init_think, full_thinking)
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
                                initial_text, think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id
                            )
                    elif cb_type == "tool_use":
                        blocks_by_idx[idx] = {
                            "type": "tool_use",
                            "id": getattr(cb, "id", "") or f"toolu_{tool_round}_{idx}",
                            "name": getattr(cb, "name", "") or "",
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
                            _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", chunk_t, full_thinking)
                    elif delta_type == "signature_delta":
                        if block is not None:
                            block["signature"] = block.get("signature", "") + (getattr(delta, "signature", "") or "")
                    elif delta_type == "text_delta":
                        c_chunk = getattr(delta, "text", "") or ""
                        if block is not None:
                            block["text"] = block.get("text", "") + c_chunk
                        if c_chunk:
                            think_buffer, is_in_think_tag = _process_text_with_think_tags(
                                c_chunk, think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id
                            )
                    elif delta_type == "input_json_delta":
                        if block is not None:
                            block.setdefault("input_parts", []).append(getattr(delta, "partial_json", "") or "")

        except Exception as e:
            stream_had_error = True
            print(f"[Anthropic Streaming Loop Exception] {e}")
            _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}")
            _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking)
            return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error

        _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking)
        prompt_tokens += round_in
        completion_tokens += round_out
        cached_tokens += round_cached

        ordered_blocks = [blocks_by_idx[i] for i in sorted(blocks_by_idx)]
        tool_calls = [b for b in ordered_blocks if b.get("type") == "tool_use"]
        if not tool_calls or abort_event.is_set() or tool_round >= MAX_TOOL_ROUNDS:
            break

        # Reconstruct exact assistant blocks (including thinking + signature) for multi-turn tool replay
        assistant_blocks: List[Dict[str, Any]] = []
        for b in ordered_blocks:
            btype = b.get("type")
            if btype == "thinking":
                if b.get("thinking") or b.get("signature"):
                    assistant_blocks.append({"type": "thinking", "thinking": b.get("thinking", ""), "signature": b.get("signature", "")})
            elif btype == "redacted_thinking":
                assistant_blocks.append({"type": "redacted_thinking", "data": b.get("data", "")})
            elif btype == "text":
                if b.get("text"):
                    assistant_blocks.append({"type": "text", "text": b["text"]})
            elif btype == "tool_use":
                raw_json = "".join(b.pop("input_parts", [])).strip()
                try:
                    parsed_input = json.loads(raw_json) if raw_json else {}
                except Exception:
                    parsed_input = {"query": raw_json} if raw_json else {}
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
            res = _run_web_search_tool_call(bridge, tab_id, conversation_id, tb["name"], tb.get("input") or {}, abort_event)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tb["id"],
                "content": json.dumps(res, ensure_ascii=False),
            })

        turns.append({"role": "assistant", "content": assistant_blocks})
        turns.append({"role": "user", "content": tool_results})

    return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error


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
) -> tuple[List[str], List[str], int, int, int, bool]:
    from google.genai import types as genai_types

    full_content: List[str] = []
    full_thinking: List[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    client, err = ClientManager.get_gemini_client(provider_id)
    if err or not client:
        _emit_error(bridge, tab_id, conversation_id, err or "Failed to initialize Gemini client.")
        return full_content, full_thinking, 0, 0, 0, True

    _emit_start(bridge, tab_id, conversation_id, assistant_msg_id, provider_id, model_id, thinking_level)

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

    _GEMINI_LEVEL_ENUM_MAP = {
        "minimal": "MINIMAL",
        "low": "LOW",
        "medium": "MEDIUM",
        "high": "HIGH",
        "xhigh": "HIGH",
        "max": "HIGH",
    }

    gemini_fn_tools = (
        [genai_types.Tool(function_declarations=[get_gemini_tool_declaration()])] if search_enabled else []
    )

    def _build_config(mode: str, with_tools: bool = True) -> genai_types.GenerateContentConfig:
        cfg_kwargs: Dict[str, Any] = {}
        if system_prompt:
            cfg_kwargs["system_instruction"] = system_prompt
        if with_tools and gemini_fn_tools:
            # Always route through custom function declaration (never native GoogleSearch grounding on chat models)
            cfg_kwargs["tools"] = gemini_fn_tools
        if mode == "level" and thinking_level and thinking_level != "none":
            lvl_str = _GEMINI_LEVEL_ENUM_MAP.get(thinking_level, "MEDIUM")
            cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                include_thoughts=True,
                thinking_level=lvl_str,
            )
        elif mode == "budget" and thinking_level and thinking_level != "none":
            budget = _GEMINI_THINKING_BUDGETS.get(thinking_level, 4096)
            cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                include_thoughts=True,
                thinking_budget=budget,
            )
        return genai_types.GenerateContentConfig(**cfg_kwargs)

    # Determine if this model is Gemini 3.x or newer (>= 3, e.g. gemini-3, gemini-4-argon, gemini-4-carbon)
    m_ver = re.search(r"gemini[-_](\d+)", (model_id or "").lower())
    if m_ver:
        is_gemini_3_or_newer = int(m_ver.group(1)) >= 3
    else:
        is_gemini_3_or_newer = not any(legacy in (model_id or "").lower() for legacy in ("gemini-1", "gemini-2", "gemini/1", "gemini/2"))

    if thinking_level and thinking_level != "none":
        thinking_modes = ["level", "budget", "none"] if is_gemini_3_or_newer else ["budget", "level", "none"]
    else:
        thinking_modes = ["none"]

    config_attempts: List[tuple[str, bool]] = (
        [(m, True) for m in thinking_modes] + [(m, False) for m in thinking_modes]
        if search_enabled
        else [(m, False) for m in thinking_modes]
    )

    has_named_chat = False
    stream_had_error = False

    for tool_round in range(MAX_TOOL_ROUNDS + 1):
        if abort_event.is_set():
            break
        if tool_round > 0:
            _ensure_post_tool_separator(full_content, bridge, tab_id, conversation_id)

        round_config_attempts = (
            config_attempts if tool_round < MAX_TOOL_ROUNDS else [(m, False) for m in thinking_modes]
        )
        response_stream = None
        active_attempt_idx = 0
        try:
            last_err = None
            for idx, (mode, use_tools) in enumerate(round_config_attempts):
                try:
                    active_attempt_idx = idx
                    response_stream = client.models.generate_content_stream(
                        model=model_id,
                        contents=contents,
                        config=_build_config(mode, use_tools),
                    )
                    last_err = None
                    break
                except Exception as attempt_err:
                    last_err = attempt_err
            if last_err is not None and response_stream is None:
                raise last_err
        except Exception as e:
            stream_had_error = True
            if full_content or full_thinking:
                _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}")
            else:
                _emit_error(bridge, tab_id, conversation_id, f"API Error ({provider_id} / {model_id}): {e}")
            return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, True

        is_in_think_tag = False
        think_buffer = ""
        round_in = 0
        round_out = 0
        round_cached = 0
        round_parts: List[Any] = []
        round_fn_calls: List[Any] = []

        def _consume_gemini_stream(stream_iter):
            nonlocal has_named_chat, is_in_think_tag, think_buffer, round_in, round_out, round_cached
            for chunk in stream_iter:
                if abort_event.is_set():
                    break

                if not has_named_chat:
                    has_named_chat = True
                    _trigger_auto_naming(bridge, tab_id, conversation_id, user_question, provider_id, model_id)

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
                            _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", part_text, full_thinking)
                        else:
                            think_buffer, is_in_think_tag = _process_text_with_think_tags(
                                part_text, think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id
                            )

        try:
            try:
                _consume_gemini_stream(response_stream)
            except Exception as stream_err:
                if active_attempt_idx + 1 < len(round_config_attempts) and not full_content and not full_thinking and not round_fn_calls:
                    recovered = False
                    for next_mode, next_tools in round_config_attempts[active_attempt_idx + 1 :]:
                        round_parts.clear()
                        round_fn_calls.clear()
                        round_in = round_out = round_cached = 0
                        try:
                            fallback_stream = client.models.generate_content_stream(
                                model=model_id,
                                contents=contents,
                                config=_build_config(next_mode, next_tools),
                            )
                            _consume_gemini_stream(fallback_stream)
                            recovered = True
                            break
                        except Exception:
                            if full_content or full_thinking or round_fn_calls:
                                break
                    if not recovered:
                        raise stream_err
                else:
                    raise stream_err
        except Exception as e:
            stream_had_error = True
            print(f"[Gemini Streaming Loop Exception] {e}")
            _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}")
            _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking)
            return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error

        _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking)
        prompt_tokens += round_in
        completion_tokens += round_out
        cached_tokens += round_cached

        if not round_fn_calls or abort_event.is_set() or tool_round >= MAX_TOOL_ROUNDS:
            break

        # Preserve exact non-empty model Parts (including thought/thought_signature) in turn history
        valid_parts = [
            p for p in round_parts
            if getattr(p, "text", None) or getattr(p, "thought", False) or getattr(p, "function_call", None)
        ]
        contents.append(genai_types.Content(role="model", parts=valid_parts or round_parts))
        fn_response_parts = []
        for fc in round_fn_calls:
            fc_name = getattr(fc, "name", "") or ""
            fc_args = getattr(fc, "args", None) or {}
            if not isinstance(fc_args, dict):
                try:
                    fc_args = dict(fc_args)
                except Exception:
                    fc_args = {}
            res = _run_web_search_tool_call(bridge, tab_id, conversation_id, fc_name, fc_args, abort_event)
            fn_response_parts.append(genai_types.Part.from_function_response(name=fc_name, response=res))
        contents.append(genai_types.Content(role="user", parts=fn_response_parts))

    return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error


def _build_openai_attempts(
    model_id: str,
    normalized_messages: List[Dict[str, Any]],
    thinking_level: str,
    openai_tools: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build ordered OpenAI chat.completions parameter variants for 400/422 fallback."""
    effort_chain: List[Optional[str]] = []
    if thinking_level and thinking_level != "none":
        effort_chain.append(thinking_level)
        if thinking_level in ("xhigh", "max"):
            effort_chain.append("high")
        elif thinking_level == "minimal":
            effort_chain.append("low")
    effort_chain.append(None)

    tool_modes = [True, False] if openai_tools else [False]
    attempts: List[Dict[str, Any]] = []
    for use_tools in tool_modes:
        for use_stream_opts in (True, False):
            for effort in effort_chain:
                if not use_stream_opts and effort is not None:
                    # Without stream_options we only need the primary effort and no-effort fallback
                    if effort != effort_chain[0]:
                        continue
                kw: Dict[str, Any] = {
                    "model": model_id,
                    "messages": normalized_messages,
                    "stream": True,
                }
                if use_stream_opts:
                    kw["stream_options"] = {"include_usage": True}
                if effort is not None:
                    kw["reasoning_effort"] = effort
                if use_tools:
                    kw["tools"] = openai_tools
                    kw["tool_choice"] = "auto"
                attempts.append(kw)
    return attempts


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
) -> tuple[List[str], List[str], int, int, int, bool]:
    import openai

    full_content: List[str] = []
    full_thinking: List[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    client, err = ClientManager.get_client(provider_id)
    if err or not client:
        _emit_error(bridge, tab_id, conversation_id, err or "Failed to initialize API client.")
        return full_content, full_thinking, 0, 0, 0, True

    _emit_start(bridge, tab_id, conversation_id, assistant_msg_id, provider_id, model_id, thinking_level)

    system_prompt, turns = _prepare_system_and_turns(messages)
    search_enabled = _resolve_search_enabled(web_search_enabled)
    system_prompt = _append_search_system_hint(system_prompt, search_enabled)
    normalized_messages: List[Dict[str, Any]] = (
        [{"role": "system", "content": system_prompt}] if system_prompt else []
    ) + [dict(t) for t in turns]
    openai_tools = [get_openai_tool_spec()] if search_enabled else []

    has_named_chat = False
    stream_had_error = False

    for tool_round in range(MAX_TOOL_ROUNDS + 1):
        if abort_event.is_set():
            break
        if tool_round > 0:
            _ensure_post_tool_separator(full_content, bridge, tab_id, conversation_id)

        round_tools = openai_tools if tool_round < MAX_TOOL_ROUNDS else []
        response_stream = None
        last_api_err = None
        try:
            for call_kwargs in _build_openai_attempts(model_id, normalized_messages, thinking_level, round_tools):
                try:
                    response_stream = client.chat.completions.create(**call_kwargs)
                    break
                except openai.APIStatusError as api_err:
                    if api_err.status_code in (400, 422):
                        last_api_err = api_err
                        continue
                    raise
            if response_stream is None and last_api_err is not None:
                raise last_api_err
        except Exception as e:
            stream_had_error = True
            if full_content or full_thinking:
                _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}")
            else:
                _emit_error(bridge, tab_id, conversation_id, f"API Error ({provider_id} / {model_id}): {e}")
            return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, True

        is_in_think_tag = False
        think_buffer = ""
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

                if not has_named_chat:
                    has_named_chat = True
                    _trigger_auto_naming(bridge, tab_id, conversation_id, user_question, provider_id, model_id)

                if hasattr(chunk, "usage") and chunk.usage:
                    u = chunk.usage
                    u_in = getattr(u, "prompt_tokens", 0) or 0
                    u_out = getattr(u, "completion_tokens", 0) or 0
                    if u_in:
                        round_in = u_in
                    if u_out:
                        round_out = u_out
                    pt_details = getattr(u, "prompt_tokens_details", None)
                    if pt_details:
                        c_tok = getattr(pt_details, "cached_tokens", 0) or 0
                        if c_tok:
                            round_cached = c_tok
                    if not round_cached and hasattr(u, "prompt_cache_hit_tokens"):
                        round_cached = getattr(u, "prompt_cache_hit_tokens", 0) or 0

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                reasoning_chunk = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if isinstance(reasoning_chunk, str) and reasoning_chunk:
                    round_reasoning_parts.append(reasoning_chunk)
                    _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", reasoning_chunk, full_thinking)

                content_chunk = getattr(delta, "content", None)
                if isinstance(content_chunk, str) and content_chunk:
                    round_text_parts.append(content_chunk)
                    think_buffer, is_in_think_tag = _process_text_with_think_tags(
                        content_chunk, think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id
                    )

                for tc in (getattr(delta, "tool_calls", None) or []):
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
            print(f"[Streaming Loop Exception] {e}")
            _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}")
            _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking)
            return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error

        _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking)
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
        if reasoning_str:
            assistant_msg["reasoning_content"] = reasoning_str
        normalized_messages.append(assistant_msg)

        for idx, tc in enumerate(ordered_calls):
            call_id = tc["id"] or f"call_{tool_round}_{idx}"
            res = _run_web_search_tool_call(bridge, tab_id, conversation_id, tc["name"], tc.get("arguments") or "{}", abort_event)
            normalized_messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(res, ensure_ascii=False),
            })

    return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error


_SDK_STREAMERS = {
    "anthropic": _stream_anthropic,
    "gemini": _stream_gemini,
    "openai": _stream_openai,
}


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
):
    """
    Executes streaming completion in a background thread using the appropriate SDK
    (Anthropic SDK for anthropic, Google GenAI SDK for gemini, OpenAI SDK for all others)
    and pushes events to the bridge event queue.
    """
    abort_event = StreamManager.register_stream(tab_id)
    user_question = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    provider = ClientManager.get_provider(provider_id)
    stream_fn = _SDK_STREAMERS.get(ClientManager.get_sdk_type(provider, provider_id), _stream_openai)

    try:
        full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error = stream_fn(
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
            web_search_enabled=bool(web_search_enabled),
        )
    finally:
        StreamManager.unregister_stream(tab_id)

    final_content_str = "".join(full_content).strip()
    final_thinking_str = "".join(full_thinking).strip()

    if stream_had_error and not final_content_str and not final_thinking_str:
        return

    total_output_chars = len(final_content_str) + len(final_thinking_str)
    if completion_tokens == 0 and total_output_chars > 0:
        completion_tokens = max(1, total_output_chars // 4)
    if prompt_tokens == 0 and (final_content_str or final_thinking_str):
        total_prompt_chars = sum(len(m.get("content", "")) for m in messages)
        prompt_tokens = max(1, total_prompt_chars // 4)

    msg_record = None
    if final_content_str or final_thinking_str:
        msg_record = add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=final_content_str,
            thinking_content=final_thinking_str,
            provider_id=provider_id,
            model_id=model_id,
            thinking_level=thinking_level,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            msg_id=assistant_msg_id,
        )

    token_stats = get_token_stats(conversation_id)

    if not stream_had_error:
        _safe_dispatch(
            bridge,
            {
                "type": "done",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "message": msg_record,
                "token_stats": token_stats,
                "was_aborted": abort_event.is_set(),
            },
        )
