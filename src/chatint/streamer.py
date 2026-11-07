import json
import re
import threading
import time
from typing import Any, Dict, List, Optional
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

import anthropic
from google import genai
from google.genai import types as genai_types
import openai
from chatint.client_manager import ClientManager
from chatint.db import add_message, get_conversation, get_token_stats, update_conversation
from chatint.naming import generate_chat_title


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


_ANTHROPIC_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 32000,
}

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

            if is_default and assistant_count == 0:
                final_title = generate_chat_title(
                    user_question,
                    provider_id=provider_id,
                    model_id=model_id,
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
        if not is_in_think_tag:
            if "<think>" in text:
                parts = text.split("<think>", 1)
                if parts[0] and (parts[0].strip() or full_content):
                    full_content.append(parts[0])
                    _safe_dispatch(
                        bridge,
                        {
                            "type": "content_chunk",
                            "tab_id": tab_id,
                            "conversation_id": conversation_id,
                            "chunk": parts[0],
                        },
                    )
                is_in_think_tag = True
                text = parts[1]
            elif any("<think>".startswith(text[-i:]) for i in range(len("<think>") - 1, 0, -1)):
                for i in range(len("<think>") - 1, 0, -1):
                    if "<think>".startswith(text[-i:]):
                        think_buffer = text[-i:]
                        pre = text[:-i]
                        if pre:
                            full_content.append(pre)
                            _safe_dispatch(
                                bridge,
                                {
                                    "type": "content_chunk",
                                    "tab_id": tab_id,
                                    "conversation_id": conversation_id,
                                    "chunk": pre,
                                },
                            )
                        break
                break
            else:
                full_content.append(text)
                _safe_dispatch(
                    bridge,
                    {
                        "type": "content_chunk",
                        "tab_id": tab_id,
                        "conversation_id": conversation_id,
                        "chunk": text,
                    },
                )
                break
        else:
            if "</think>" in text:
                parts = text.split("</think>", 1)
                if parts[0]:
                    full_thinking.append(parts[0])
                    _safe_dispatch(
                        bridge,
                        {
                            "type": "thinking_chunk",
                            "tab_id": tab_id,
                            "conversation_id": conversation_id,
                            "chunk": parts[0],
                        },
                    )
                is_in_think_tag = False
                text = parts[1]
            elif any("</think>".startswith(text[-i:]) for i in range(len("</think>") - 1, 0, -1)):
                for i in range(len("</think>") - 1, 0, -1):
                    if "</think>".startswith(text[-i:]):
                        think_buffer = text[-i:]
                        pre = text[:-i]
                        if pre:
                            full_thinking.append(pre)
                            _safe_dispatch(
                                bridge,
                                {
                                    "type": "thinking_chunk",
                                    "tab_id": tab_id,
                                    "conversation_id": conversation_id,
                                    "chunk": pre,
                                },
                            )
                        break
                break
            else:
                full_thinking.append(text)
                _safe_dispatch(
                    bridge,
                    {
                        "type": "thinking_chunk",
                        "tab_id": tab_id,
                        "conversation_id": conversation_id,
                        "chunk": text,
                    },
                )
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
) -> tuple[List[str], List[str], int, int, int, bool]:
    full_content: List[str] = []
    full_thinking: List[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    client, err = ClientManager.get_anthropic_client(provider_id)
    if err or not client:
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": err or "Failed to initialize Anthropic client.",
            },
        )
        return full_content, full_thinking, 0, 0, 0, True

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

    system_prompt, turns = _prepare_system_and_turns(messages)

    call_kwargs: Dict[str, Any] = {
        "model": model_id,
        "messages": turns,
        "max_tokens": 8192,
        "stream": True,
    }
    if system_prompt:
        call_kwargs["system"] = system_prompt

    budget = 0
    if thinking_level and thinking_level != "none":
        budget = _ANTHROPIC_THINKING_BUDGETS.get(thinking_level, 4096)
        call_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        call_kwargs["max_tokens"] = max(8192, budget + 4096)

    response_stream = None
    try:
        try:
            response_stream = client.messages.create(**call_kwargs)
        except anthropic.APIStatusError as api_err:
            if api_err.status_code in (400, 422) and "thinking" in call_kwargs:
                if budget > 4096:
                    call_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 4096}
                    call_kwargs["max_tokens"] = 8192
                    try:
                        response_stream = client.messages.create(**call_kwargs)
                    except anthropic.APIStatusError:
                        response_stream = None
                if response_stream is None:
                    call_kwargs.pop("thinking", None)
                    call_kwargs["max_tokens"] = 8192
                    try:
                        response_stream = client.messages.create(**call_kwargs)
                    except anthropic.APIStatusError as retry_err:
                        if retry_err.status_code in (400, 422):
                            call_kwargs["max_tokens"] = 4096
                            response_stream = client.messages.create(**call_kwargs)
                        else:
                            raise retry_err
            elif api_err.status_code in (400, 422) and call_kwargs.get("max_tokens", 0) > 4096:
                call_kwargs["max_tokens"] = 4096
                response_stream = client.messages.create(**call_kwargs)
            else:
                raise api_err
    except Exception as e:
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": f"API Error ({provider_id} / {model_id}): {str(e)}",
            },
        )
        return full_content, full_thinking, 0, 0, 0, True

    has_named_chat = False
    is_in_think_tag = False
    think_buffer = ""
    stream_had_error = False

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
                    cached_tokens = cache_read
                    prompt_tokens = in_tok + cache_read + cache_create
                    if out_tok:
                        completion_tokens = out_tok

            elif ev_type == "message_delta":
                usage = getattr(event, "usage", None)
                if usage:
                    out_tok = getattr(usage, "output_tokens", 0) or 0
                    if out_tok:
                        completion_tokens = out_tok
                    in_tok = getattr(usage, "input_tokens", None)
                    cache_read = getattr(usage, "cache_read_input_tokens", None)
                    cache_create = getattr(usage, "cache_creation_input_tokens", None)
                    if in_tok is not None or cache_read is not None:
                        cached_tokens = cache_read or cached_tokens
                        prompt_tokens = (in_tok or 0) + cached_tokens + (cache_create or 0)

            elif ev_type == "content_block_start":
                cb = getattr(event, "content_block", None)
                cb_type = getattr(cb, "type", "") if cb else ""
                if cb_type == "thinking":
                    initial_think = getattr(cb, "thinking", "") or ""
                    if initial_think:
                        full_thinking.append(initial_think)
                        _safe_dispatch(
                            bridge,
                            {
                                "type": "thinking_chunk",
                                "tab_id": tab_id,
                                "conversation_id": conversation_id,
                                "chunk": initial_think,
                            },
                        )
                elif cb_type == "text":
                    initial_text = getattr(cb, "text", "") or ""
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
                        )

            elif ev_type == "content_block_delta":
                delta = getattr(event, "delta", None)
                delta_type = getattr(delta, "type", "") if delta else ""
                if delta_type == "thinking_delta":
                    t_chunk = getattr(delta, "thinking", "") or ""
                    if t_chunk:
                        full_thinking.append(t_chunk)
                        _safe_dispatch(
                            bridge,
                            {
                                "type": "thinking_chunk",
                                "tab_id": tab_id,
                                "conversation_id": conversation_id,
                                "chunk": t_chunk,
                            },
                        )
                elif delta_type == "text_delta":
                    c_chunk = getattr(delta, "text", "") or ""
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
                        )

    except Exception as e:
        stream_had_error = True
        print(f"[Anthropic Streaming Loop Exception] {e}")
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": f"Streaming interrupted: {str(e)}",
            },
        )

    if think_buffer:
        if is_in_think_tag:
            full_thinking.append(think_buffer)
        else:
            full_content.append(think_buffer)

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
) -> tuple[List[str], List[str], int, int, int, bool]:
    full_content: List[str] = []
    full_thinking: List[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    client, err = ClientManager.get_gemini_client(provider_id)
    if err or not client:
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": err or "Failed to initialize Gemini client.",
            },
        )
        return full_content, full_thinking, 0, 0, 0, True

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

    system_prompt, turns = _prepare_system_and_turns(messages)

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

    def _build_config(mode: str) -> genai_types.GenerateContentConfig:
        cfg_kwargs: Dict[str, Any] = {}
        if system_prompt:
            cfg_kwargs["system_instruction"] = system_prompt
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

    is_gemini_3 = "gemini-3" in (model_id or "").lower()
    if thinking_level and thinking_level != "none":
        config_modes = ["level", "budget", "none"] if is_gemini_3 else ["budget", "level", "none"]
    else:
        config_modes = ["none"]

    response_stream = None
    active_mode_idx = 0
    try:
        last_err = None
        for idx, mode in enumerate(config_modes):
            try:
                active_mode_idx = idx
                response_stream = client.models.generate_content_stream(
                    model=model_id,
                    contents=contents,
                    config=_build_config(mode),
                )
                last_err = None
                break
            except Exception as attempt_err:
                last_err = attempt_err
        if last_err is not None and response_stream is None:
            raise last_err
    except Exception as e:
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": f"API Error ({provider_id} / {model_id}): {str(e)}",
            },
        )
        return full_content, full_thinking, 0, 0, 0, True

    has_named_chat = False
    is_in_think_tag = False
    think_buffer = ""
    stream_had_error = False

    def _consume_gemini_stream(stream_iter):
        nonlocal has_named_chat, is_in_think_tag, think_buffer, prompt_tokens, completion_tokens, cached_tokens
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
                    prompt_tokens = p_tok
                if c_tok or t_tok:
                    completion_tokens = c_tok + t_tok
                if cache_tok:
                    cached_tokens = cache_tok

            candidates = getattr(chunk, "candidates", None)
            if not candidates:
                continue

            for cand in candidates:
                content_obj = getattr(cand, "content", None)
                parts = getattr(content_obj, "parts", None) if content_obj else None
                if not parts:
                    continue
                for part in parts:
                    part_text = getattr(part, "text", None)
                    if not isinstance(part_text, str) or not part_text:
                        continue
                    is_thought = bool(getattr(part, "thought", False))
                    if is_thought:
                        full_thinking.append(part_text)
                        _safe_dispatch(
                            bridge,
                            {
                                "type": "thinking_chunk",
                                "tab_id": tab_id,
                                "conversation_id": conversation_id,
                                "chunk": part_text,
                            },
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
                        )

    try:
        try:
            _consume_gemini_stream(response_stream)
        except Exception as stream_err:
            # If the generator raised on the first yield because the model rejected the thinking config mode, try remaining modes
            if active_mode_idx + 1 < len(config_modes) and not full_content and not full_thinking:
                recovered = False
                for next_mode in config_modes[active_mode_idx + 1 :]:
                    try:
                        fallback_stream = client.models.generate_content_stream(
                            model=model_id,
                            contents=contents,
                            config=_build_config(next_mode),
                        )
                        _consume_gemini_stream(fallback_stream)
                        recovered = True
                        break
                    except Exception:
                        if full_content or full_thinking:
                            break
                if not recovered:
                    raise stream_err
            else:
                raise stream_err
    except Exception as e:
        stream_had_error = True
        print(f"[Gemini Streaming Loop Exception] {e}")
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": f"Streaming interrupted: {str(e)}",
            },
        )

    if think_buffer:
        if is_in_think_tag:
            full_thinking.append(think_buffer)
        else:
            full_content.append(think_buffer)

    return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error


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
) -> tuple[List[str], List[str], int, int, int, bool]:
    full_content: List[str] = []
    full_thinking: List[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    client, err = ClientManager.get_client(provider_id)
    if err or not client:
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": err or "Failed to initialize API client.",
            },
        )
        return full_content, full_thinking, 0, 0, 0, True

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

    system_prompt, turns = _prepare_system_and_turns(messages)
    normalized_messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + turns

    call_kwargs: Dict[str, Any] = {
        "model": model_id,
        "messages": normalized_messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    if thinking_level and thinking_level != "none":
        call_kwargs["reasoning_effort"] = thinking_level

    response_stream = None
    try:
        try:
            response_stream = client.chat.completions.create(**call_kwargs)
        except openai.APIStatusError as api_err:
            if api_err.status_code in (400, 422):
                if "reasoning_effort" in call_kwargs:
                    # If extended effort ('xhigh'/'max'/'minimal') was rejected, try standard OpenAI effort ('high'/'low') first
                    fallback_effort = None
                    if call_kwargs["reasoning_effort"] in ("xhigh", "max"):
                        fallback_effort = "high"
                    elif call_kwargs["reasoning_effort"] == "minimal":
                        fallback_effort = "low"

                    if fallback_effort:
                        call_kwargs["reasoning_effort"] = fallback_effort
                        try:
                            response_stream = client.chat.completions.create(**call_kwargs)
                        except openai.APIStatusError:
                            response_stream = None

                    if response_stream is None:
                        call_kwargs.pop("reasoning_effort", None)
                        try:
                            response_stream = client.chat.completions.create(**call_kwargs)
                        except openai.APIStatusError as retry_err:
                            if retry_err.status_code in (400, 422) and "stream_options" in call_kwargs:
                                call_kwargs.pop("stream_options", None)
                                response_stream = client.chat.completions.create(**call_kwargs)
                            else:
                                raise retry_err
                elif "stream_options" in call_kwargs:
                    call_kwargs.pop("stream_options", None)
                    response_stream = client.chat.completions.create(**call_kwargs)
                else:
                    raise api_err
            else:
                raise api_err
    except Exception as e:
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": f"API Error ({provider_id} / {model_id}): {str(e)}",
            },
        )
        return full_content, full_thinking, 0, 0, 0, True

    is_in_think_tag = False
    think_buffer = ""
    has_named_chat = False
    stream_had_error = False

    try:
        for chunk in response_stream:
            if abort_event.is_set():
                break

            if not has_named_chat:
                has_named_chat = True
                _trigger_auto_naming(bridge, tab_id, conversation_id, user_question, provider_id, model_id)

            if hasattr(chunk, "usage") and chunk.usage:
                u = chunk.usage
                prompt_tokens = getattr(u, "prompt_tokens", 0) or 0
                completion_tokens = getattr(u, "completion_tokens", 0) or 0
                pt_details = getattr(u, "prompt_tokens_details", None)
                if pt_details:
                    cached_tokens = getattr(pt_details, "cached_tokens", 0) or 0
                if not cached_tokens and hasattr(u, "prompt_cache_hit_tokens"):
                    cached_tokens = getattr(u, "prompt_cache_hit_tokens", 0) or 0

            if not chunk.choices or len(chunk.choices) == 0:
                continue

            delta = chunk.choices[0].delta

            reasoning_chunk = getattr(delta, "reasoning_content", None)
            if not reasoning_chunk and hasattr(delta, "reasoning"):
                reasoning_chunk = getattr(delta, "reasoning", None)

            if isinstance(reasoning_chunk, str) and reasoning_chunk:
                full_thinking.append(reasoning_chunk)
                _safe_dispatch(
                    bridge,
                    {
                        "type": "thinking_chunk",
                        "tab_id": tab_id,
                        "conversation_id": conversation_id,
                        "chunk": reasoning_chunk,
                    },
                )

            content_chunk = getattr(delta, "content", None)
            if isinstance(content_chunk, str) and content_chunk:
                think_buffer, is_in_think_tag = _process_text_with_think_tags(
                    content_chunk,
                    think_buffer,
                    is_in_think_tag,
                    full_content,
                    full_thinking,
                    bridge,
                    tab_id,
                    conversation_id,
                )

    except Exception as e:
        stream_had_error = True
        print(f"[Streaming Loop Exception] {e}")
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": f"Streaming interrupted: {str(e)}",
            },
        )

    if think_buffer:
        if is_in_think_tag:
            full_thinking.append(think_buffer)
        else:
            full_content.append(think_buffer)

    return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error


def stream_chat_completion(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    messages: List[Dict[str, str]],
    provider_id: str,
    model_id: str,
    thinking_level: str = "none",
    assistant_msg_id: Optional[str] = None,
):
    """
    Executes streaming completion in a background thread using the appropriate SDK
    (Anthropic SDK for anthropic, Google GenAI SDK for gemini, OpenAI SDK for all others)
    and pushes events to the bridge event queue.
    """
    abort_event = StreamManager.register_stream(tab_id)

    user_question = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_question = m.get("content", "")
            break

    provider = ClientManager.get_provider(provider_id)
    sdk_type = ClientManager.get_sdk_type(provider, provider_id)

    try:
        if sdk_type == "anthropic":
            (
                full_content,
                full_thinking,
                prompt_tokens,
                completion_tokens,
                cached_tokens,
                stream_had_error,
            ) = _stream_anthropic(
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
            )
        elif sdk_type == "gemini":
            (
                full_content,
                full_thinking,
                prompt_tokens,
                completion_tokens,
                cached_tokens,
                stream_had_error,
            ) = _stream_gemini(
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
            )
        else:
            (
                full_content,
                full_thinking,
                prompt_tokens,
                completion_tokens,
                cached_tokens,
                stream_had_error,
            ) = _stream_openai(
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
