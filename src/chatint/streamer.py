import json
import re
import threading
import time
from typing import Any, Dict, List, Optional
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
    Executes streaming completion in a background thread and pushes events to the bridge event queue.
    """
    abort_event = StreamManager.register_stream(tab_id)

    client, err = ClientManager.get_client(provider_id)
    if err or not client:
        StreamManager.unregister_stream(tab_id)
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": err or "Failed to initialize API client.",
            },
        )
        return

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

    call_kwargs: Dict[str, Any] = {
        "model": model_id,
        "messages": messages,
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
        StreamManager.unregister_stream(tab_id)
        _safe_dispatch(
            bridge,
            {
                "type": "error",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "error": f"API Error ({provider_id} / {model_id}): {str(e)}",
            },
        )
        return

    user_question = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_question = m.get("content", "")
            break

    full_content = []
    full_thinking = []
    is_in_think_tag = False
    think_buffer = ""
    has_named_chat = False

    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

    stream_had_error = False
    try:
        for chunk in response_stream:
            if abort_event.is_set():
                break

            # Trigger naming check on first streaming chunk
            if not has_named_chat:
                has_named_chat = True
                try:
                    conv = get_conversation(conversation_id)
                    current_title = (conv.get("title") or "").strip() if conv else ""
                    # Check if previous assistant responses exist in this chat
                    assistant_count = sum(1 for m in conv.get("messages", []) if m.get("role") == "assistant") if conv else 0
                    is_default = current_title.lower() in ["new chat", ""]

                    # If the name of the chat has NOT been changed from its default "New Chat",
                    # only change its name after the first turn using the naming script.
                    if is_default and assistant_count == 0:
                        final_title = generate_chat_title(user_question)
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
    finally:
        StreamManager.unregister_stream(tab_id)

    if think_buffer:
        if is_in_think_tag:
            full_thinking.append(think_buffer)
        else:
            full_content.append(think_buffer)

    final_content_str = "".join(full_content).strip()
    final_thinking_str = "".join(full_thinking).strip()

    total_output_chars = len(final_content_str) + len(final_thinking_str)
    if completion_tokens == 0 and total_output_chars > 0:
        completion_tokens = max(1, total_output_chars // 4)
    if prompt_tokens == 0:
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
