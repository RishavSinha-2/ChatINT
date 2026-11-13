from __future__ import annotations

import itertools
import json
import re
import threading
import uuid
from typing import Any, Dict, List, Optional
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

from chatint.client_manager import ClientManager, is_gemini_3_or_newer
from chatint.db import add_message, get_conversation, get_token_stats, record_token_usage, update_conversation
from chatint.logging_setup import get_logger
from chatint.naming import generate_chat_title

_logger = get_logger("chatint.streamer")
from chatint.web_search_tool import (
    IMAGE_SEARCH_TOOL_NAME,
    MAX_TOOL_ROUNDS,
    WEB_SEARCH_TOOL_NAME,
    execute_image_search,
    execute_web_search,
    get_anthropic_image_tool_spec,
    get_anthropic_tool_spec,
    get_gemini_image_tool_declaration,
    get_gemini_tool_declaration,
    get_openai_image_tool_spec,
    get_openai_tool_spec,
    get_web_search_system_hint,
    is_web_search_available,
)


class StreamManager:
    _streams_by_tab: Dict[str, tuple[str, threading.Event, str]] = {}
    _streams_by_id: Dict[str, tuple[str, threading.Event, str]] = {}
    _lock = threading.Lock()

    @classmethod
    def abort_stream(cls, tab_id: str, stream_id: Optional[str] = None):
        with cls._lock:
            if stream_id and stream_id in cls._streams_by_id:
                cls._streams_by_id[stream_id][1].set()
            entry = cls._streams_by_tab.get(tab_id)
            if entry:
                entry[1].set()

    @classmethod
    def abort_conversation(cls, conversation_id: str):
        if not conversation_id:
            return
        with cls._lock:
            for sid, (tid, ev, cid) in list(cls._streams_by_id.items()):
                if cid == conversation_id:
                    ev.set()
            for tid, (sid, ev, cid) in list(cls._streams_by_tab.items()):
                if cid == conversation_id:
                    ev.set()

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

            event = threading.Event()
            cls._streams_by_tab[tab_id] = (sid, event, conversation_id or "")
            cls._streams_by_id[sid] = (tab_id, event, conversation_id or "")
            return event

    @classmethod
    def unregister_stream(cls, tab_id: str, stream_id: Optional[str] = None):
        with cls._lock:
            if stream_id:
                cls._streams_by_id.pop(stream_id, None)
                cur = cls._streams_by_tab.get(tab_id)
                if cur and cur[0] == stream_id:
                    cls._streams_by_tab.pop(tab_id, None)
            else:
                cur = cls._streams_by_tab.pop(tab_id, None)
                if cur:
                    cls._streams_by_id.pop(cur[0], None)


def _safe_dispatch(bridge: Any, event_data: Dict[str, Any]):
    if not bridge:
        return
    try:
        if hasattr(bridge, "push_event"):
            bridge.push_event(event_data)
    except Exception as e:
        _logger.warning("Streamer dispatch error: %s", e)


def _emit_error(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    message: str,
    stream_id: Optional[str] = None,
):
    payload: Dict[str, Any] = {
        "type": "error",
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "error": message,
    }
    if stream_id:
        payload["stream_id"] = stream_id
    _safe_dispatch(bridge, payload)


def _emit_api_error(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    provider_id: str,
    model_id: str,
    err: Exception,
    full_content: List[str],
    full_thinking: List[str],
    stream_id: Optional[str] = None,
):
    if full_content or full_thinking:
        _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {err}", stream_id=stream_id)
    else:
        _emit_error(bridge, tab_id, conversation_id, f"API Error ({provider_id} / {model_id}): {err}", stream_id=stream_id)


def _emit_start(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    assistant_msg_id: Optional[str],
    provider_id: str,
    model_id: str,
    thinking_level: str,
    stream_id: Optional[str] = None,
):
    payload: Dict[str, Any] = {
        "type": "start",
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "assistant_msg_id": assistant_msg_id,
        "provider_id": provider_id,
        "model_id": model_id,
        "thinking_level": thinking_level,
    }
    if stream_id:
        payload["stream_id"] = stream_id
    _safe_dispatch(bridge, payload)


def _emit_chunk(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    chunk_type: str,
    text: str,
    collector: List[str],
    stream_id: Optional[str] = None,
):
    if not text:
        return
    collector.append(text)
    payload: Dict[str, Any] = {
        "type": chunk_type,
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "chunk": text,
    }
    if stream_id:
        payload["stream_id"] = stream_id
    _safe_dispatch(bridge, payload)


def _flush_think_buffer(
    think_buffer: str,
    is_in_think_tag: bool,
    full_content: List[str],
    full_thinking: List[str],
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    stream_id: Optional[str] = None,
):
    if think_buffer:
        chunk_type = "thinking_chunk" if is_in_think_tag else "content_chunk"
        target = full_thinking if is_in_think_tag else full_content
        _emit_chunk(bridge, tab_id, conversation_id, chunk_type, think_buffer, target, stream_id=stream_id)


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
    stream_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a web_search or search_images tool call from any provider, emitting live UI status events."""
    if tool_name not in (WEB_SEARCH_TOOL_NAME, IMAGE_SEARCH_TOOL_NAME):
        return {"ok": False, "error": f"Unsupported tool '{tool_name}'."}

    query = _parse_tool_query(raw_args)
    tool_kind = "image_search" if tool_name == IMAGE_SEARCH_TOOL_NAME else "web_search"

    searching_ev: Dict[str, Any] = {
        "type": "tool_status",
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "status": "searching",
        "tool": tool_kind,
        "query": query,
    }
    if stream_id:
        searching_ev["stream_id"] = stream_id
    _safe_dispatch(bridge, searching_ev)

    if abort_event.is_set():
        return {"ok": False, "error": "Aborted by user."}

    if tool_name == IMAGE_SEARCH_TOOL_NAME:
        result = execute_image_search(query, max_results=3)
        done_ev: Dict[str, Any] = {
            "type": "tool_status",
            "tab_id": tab_id,
            "conversation_id": conversation_id,
            "status": "done",
            "tool": "image_search",
            "query": query,
            "ok": bool(result.get("ok")),
            "images_found": len(result.get("images", [])),
        }
        if stream_id:
            done_ev["stream_id"] = stream_id
        _safe_dispatch(bridge, done_ev)
        return result

    result = execute_web_search(query, conversation_id=conversation_id)
    done_ev = {
        "type": "tool_status",
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "status": "done",
        "tool": "web_search",
        "query": query,
        "ok": bool(result.get("ok")),
        "search_requests_used": int(result.get("search_requests_used") or 0),
        "sources": result.get("sources") or [],
    }
    if stream_id:
        done_ev["stream_id"] = stream_id
    _safe_dispatch(bridge, done_ev)
    return result


def _ensure_post_tool_separator(
    full_content: List[str],
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    stream_id: Optional[str] = None,
):
    """Ensure a blank line separates pre-tool text from post-tool response text."""
    if full_content and "".join(full_content).strip() and not "".join(full_content).endswith(("\n", " ")):
        _emit_chunk(bridge, tab_id, conversation_id, "content_chunk", "\n\n", full_content, stream_id=stream_id)


# Unified thinking fallback chains:
# minimal -> "minimal" -> "low" -> none/plain
# low -> "low" -> none/plain
# medium -> "medium" -> "low" -> none/plain
# high -> "high" -> none/plain
# xhigh -> "xhigh" -> "high" -> none/plain
# max -> "max" -> "xhigh" -> "high" -> none/plain
# none -> parameter omitted
UNIFIED_THINKING_CHAINS: Dict[str, List[Optional[str]]] = {
    "minimal": ["minimal", "low", None],
    "low": ["low", None],
    "medium": ["medium", "low", None],
    "high": ["high", None],
    "xhigh": ["xhigh", "high", None],
    "max": ["max", "xhigh", "high", None],
    "none": [None],
}

# In-memory cache remembering the resolved working thinking effort for (provider, model, requested_effort)
# so subsequent prompts skip redundant 400 retries until the model or dropdown setting changes.
_RESOLVED_THINKING_CACHE: Dict[tuple[str, str, str], str] = {}


def get_unified_thinking_chain(
    level: Optional[str],
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> List[Optional[str]]:
    """Return the fallback chain of thinking efforts for any provider.
    None represents omitting the thinking/effort parameter (plain/none).
    If a previous check already verified the working effort for
    (provider, model, requested_level), it prioritizes that verified effort first
    to eliminate redundant 400 roundtrips.
    """
    key = (level or "none").strip().lower()
    full_chain = list(UNIFIED_THINKING_CHAINS.get(key, [None]))

    if provider_id and model_id:
        cached = _RESOLVED_THINKING_CACHE.get((provider_id, model_id, key))
        if cached is None:
            try:
                from chatint.db import get_model_resolved_thinking
                stored = get_model_resolved_thinking(provider_id, model_id, key)
                if stored is not None:
                    _RESOLVED_THINKING_CACHE[(provider_id, model_id, key)] = stored
                    cached = stored
            except Exception:
                pass

        if cached is not None:
            cached_val = None if cached == "none" else cached
            return [cached_val] + [e for e in full_chain if e != cached_val]

    return full_chain


def record_resolved_thinking_level(provider_id: str, model_id: str, requested_level: str, resolved_level: str):
    """Cache and persist the working effort for (provider, model, requested_level) so subsequent prompts
    use the verified level immediately until the model or thinking level changes.
    """
    if provider_id and model_id:
        req = (requested_level or "none").strip().lower()
        res = (resolved_level or "none").strip().lower()
        _RESOLVED_THINKING_CACHE[(provider_id, model_id, req)] = res
        try:
            from chatint.db import update_model_resolved_thinking
            update_model_resolved_thinking(provider_id, model_id, req, res)
        except Exception:
            pass


def clear_resolved_thinking_cache(
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
    requested_level: Optional[str] = None,
):
    """Clear cached thinking levels in memory and database."""
    if not provider_id and not model_id:
        _RESOLVED_THINKING_CACHE.clear()
        return

    req = requested_level.strip().lower() if requested_level else None
    if provider_id and model_id and req:
        _RESOLVED_THINKING_CACHE.pop((provider_id, model_id, req), None)
    else:
        keys_to_del = [
            k for k in _RESOLVED_THINKING_CACHE
            if (not provider_id or k[0] == provider_id)
            and (not model_id or k[1] == model_id)
            and (not req or k[2] == req)
        ]
        for k in keys_to_del:
            _RESOLVED_THINKING_CACHE.pop(k, None)

    if provider_id:
        try:
            from chatint.db import remove_model_resolved_thinking

            remove_model_resolved_thinking(provider_id, model_id, req)
        except Exception:
            pass


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
    stream_id: Optional[str] = None,
):
    def _run_naming():
        try:
            conv = get_conversation(conversation_id)
            if not conv:
                return
            current_title = (conv.get("title") or "").strip()
            assistant_count = sum(1 for m in conv.get("messages", []) if m.get("role") == "assistant")
            if assistant_count < 1:
                return
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
                resolved_title = update_conversation(conversation_id, only_if_default=True, title=final_title)
                if resolved_title:
                    final_title = resolved_title
                else:
                    latest_conv = get_conversation(conversation_id)
                    if not latest_conv:
                        return
                    final_title = (latest_conv.get("title") or "").strip() or "New Chat"
            else:
                final_title = current_title or "New Chat"

            ev_payload: Dict[str, Any] = {
                "type": "chat_named",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "title": final_title,
                "token_stats": get_token_stats(conversation_id),
            }
            if stream_id:
                ev_payload["stream_id"] = stream_id
            _safe_dispatch(bridge, ev_payload)
        except Exception as ex:
            _logger.warning("Auto-naming failed for %s: %s", conversation_id, ex)

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
    stream_id: Optional[str] = None,
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
                _emit_chunk(bridge, tab_id, conversation_id, chunk_type, before, target_list, stream_id=stream_id)
            is_in_think_tag = not is_in_think_tag
            text = rest
            continue

        # Check if trailing characters could be an incomplete <think> or </think> prefix
        partial_len = next((i for i in range(len(tag) - 1, 0, -1) if tag.startswith(text[-i:])), 0)
        if partial_len:
            think_buffer = text[-partial_len:]
            pre = text[:-partial_len]
            if pre:
                _emit_chunk(bridge, tab_id, conversation_id, chunk_type, pre, target_list, stream_id=stream_id)
            break

        _emit_chunk(bridge, tab_id, conversation_id, chunk_type, text, target_list, stream_id=stream_id)
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
        _emit_error(bridge, tab_id, conversation_id, err or "Failed to initialize Anthropic client.", stream_id=stream_id)
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
                _emit_api_error(bridge, tab_id, conversation_id, provider_id, model_id, e, full_content, full_thinking, stream_id=stream_id)
                return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, True, actual_thinking_level

            if not has_emitted_start:
                has_emitted_start = True
                _emit_start(bridge, tab_id, conversation_id, assistant_msg_id, provider_id, model_id, actual_thinking_level, stream_id=stream_id)

            is_in_think_tag = False
            think_buffer = ""
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
                                _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", init_think, full_thinking, stream_id=stream_id)
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
                                    initial_text, think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id
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
                                _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", chunk_t, full_thinking, stream_id=stream_id)
                        elif delta_type == "signature_delta":
                            if block is not None:
                                block["signature"] = block.get("signature", "") + (getattr(delta, "signature", "") or "")
                        elif delta_type == "text_delta":
                            c_chunk = getattr(delta, "text", "") or ""
                            if block is not None:
                                block["text"] = block.get("text", "") + c_chunk
                            if c_chunk:
                                think_buffer, is_in_think_tag = _process_text_with_think_tags(
                                    c_chunk, think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id
                                )
                        elif delta_type == "input_json_delta":
                            if block is not None:
                                block.setdefault("input_parts", []).append(getattr(delta, "partial_json", "") or "")

            except Exception as e:
                stream_had_error = True
                _logger.warning("Anthropic streaming loop exception: %s", e)
                _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}", stream_id=stream_id)
                _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id)
                return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error, actual_thinking_level

            _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id)
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
                res = _run_web_search_tool_call(bridge, tab_id, conversation_id, tb["name"], tb.get("input") or {}, abort_event, stream_id=stream_id)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tb["id"],
                    "content": json.dumps(res, ensure_ascii=False),
                })

            turns.append({"role": "assistant", "content": assistant_blocks})
            turns.append({"role": "user", "content": tool_results})

        return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error, actual_thinking_level
    finally:
        ClientManager.close_client(client)


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
        _emit_error(bridge, tab_id, conversation_id, err or "Failed to initialize Gemini client.", stream_id=stream_id)
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

        def _build_config(effort: Optional[str], mode: str = "level", with_tools: bool = True) -> genai_types.GenerateContentConfig:
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
            if any(tok in low for tok in ("401", "403", "404", "429", "unauthorized", "permission denied", "api_key_invalid", "api key not valid", "resource_exhausted", "quota", "timeout", "timed out", "connect", "dns", "network")):
                return False
            return any(tok in low for tok in ("400", "422", "invalid_argument", "thinking", "budget", "level", "unsupported", "not supported", "tool", "function calling"))

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
                                record_resolved_thinking_level(provider_id, model_id, thinking_level, actual_thinking_level)
                                last_err = None
                                effort_connected = True
                                break
                            except Exception as attempt_err:
                                if not _is_gemini_retryable_config_error(attempt_err):
                                    raise attempt_err
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
                _emit_api_error(bridge, tab_id, conversation_id, provider_id, model_id, e, full_content, full_thinking, stream_id=stream_id)
                return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, True, actual_thinking_level

            if not has_emitted_start:
                has_emitted_start = True
                _emit_start(bridge, tab_id, conversation_id, assistant_msg_id, provider_id, model_id, actual_thinking_level, stream_id=stream_id)

            is_in_think_tag = False
            think_buffer = ""
            round_in = 0
            round_out = 0
            round_cached = 0
            round_parts: List[Any] = []
            round_fn_calls: List[Any] = []

            def _consume_gemini_stream(stream_iter):
                nonlocal is_in_think_tag, think_buffer, round_in, round_out, round_cached
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
                                _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", part_text, full_thinking, stream_id=stream_id)
                            else:
                                think_buffer, is_in_think_tag = _process_text_with_think_tags(
                                    part_text, think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id
                                )

            try:
                _consume_gemini_stream(active_stream)
            except Exception as e:
                stream_had_error = True
                _logger.warning("Gemini streaming loop exception: %s", e)
                _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}", stream_id=stream_id)
                _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id)
                return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error, actual_thinking_level

            _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id)
            prompt_tokens += round_in
            completion_tokens += round_out
            cached_tokens += round_cached

            if not round_fn_calls or abort_event.is_set() or tool_round >= MAX_TOOL_ROUNDS:
                break

            # Preserve exact non-empty model Parts (including thought/thought_signature) in turn history
            valid_parts = [
                p for p in round_parts
                if getattr(p, "text", None)
                or getattr(p, "thought", False)
                or getattr(p, "function_call", None)
                or getattr(p, "thought_signature", None)
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
                res = _run_web_search_tool_call(bridge, tab_id, conversation_id, fc_name, fc_args, abort_event, stream_id=stream_id)
                fn_response_parts.append(genai_types.Part.from_function_response(name=fc_name, response=res))
            contents.append(genai_types.Content(role="user", parts=fn_response_parts))

        return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error, actual_thinking_level
    finally:
        ClientManager.close_client(client)


def _is_openai_retryable_param_error(api_err: Any) -> bool:
    return getattr(api_err, "status_code", None) in (400, 422)


def _clamp_openai_effort(provider_id: str, model_id: str, effort: Optional[str]) -> Optional[str]:
    if effort is None:
        return None
    if (provider_id or "").strip().lower() == "openai":
        mid = (model_id or "").strip().lower()
        if re.search(r"(?:^|/|-)(?:o1|o3|o4)(?:$|[-_])", mid):
            if effort == "minimal":
                return "low"
            if effort in ("xhigh", "max"):
                return "high"
    return effort


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
                    stream_obj = client.chat.completions.create(**kw)
                    actual_thinking_level = eff_val if eff_val is not None else "none"
                    record_resolved_thinking_level(provider_id, model_id, thinking_level, actual_thinking_level)
                    last_api_err = None
                    return stream_obj
                except openai.APIStatusError as api_err:
                    if not _is_openai_retryable_param_error(api_err):
                        raise
                    clear_resolved_thinking_cache(provider_id, model_id, thinking_level)
                    last_api_err = api_err
                    if include_stream_options and "stream_options" in str(api_err).lower():
                        include_stream_options = False
                        kw.pop("stream_options", None)
                        try:
                            stream_obj = client.chat.completions.create(**kw)
                            actual_thinking_level = eff_val if eff_val is not None else "none"
                            record_resolved_thinking_level(provider_id, model_id, thinking_level, actual_thinking_level)
                            last_api_err = None
                            return stream_obj
                        except openai.APIStatusError as retry_err:
                            if not _is_openai_retryable_param_error(retry_err):
                                raise
                            last_api_err = retry_err
                    return None

            try:
                for effort in effort_chain:
                    if round_tools:
                        response_stream = _try_openai_create(effort, True)
                        if response_stream is not None:
                            break
                        err_lower = str(last_api_err or "").lower()
                        if "tool" in err_lower or "function" in err_lower or effort == effort_chain[-1]:
                            response_stream = _try_openai_create(effort, False)
                            if response_stream is not None:
                                break
                    else:
                        response_stream = _try_openai_create(effort, False)
                        if response_stream is not None:
                            break

                # If all with-tools efforts failed with 400/422, try primary effort without tools before giving up
                if response_stream is None and round_tools and last_api_err is not None:
                    for effort in effort_chain:
                        response_stream = _try_openai_create(effort, False)
                        if response_stream is not None:
                            break

                if response_stream is None and last_api_err is not None:
                    raise last_api_err
            except Exception as e:
                stream_had_error = True
                _emit_api_error(bridge, tab_id, conversation_id, provider_id, model_id, e, full_content, full_thinking, stream_id=stream_id)
                return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, True, actual_thinking_level

            if not has_emitted_start:
                has_emitted_start = True
                _emit_start(bridge, tab_id, conversation_id, assistant_msg_id, provider_id, model_id, actual_thinking_level, stream_id=stream_id)

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
                        round_reasoning_parts.append(reasoning_chunk)
                        _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", reasoning_chunk, full_thinking, stream_id=stream_id)

                    content_chunk = getattr(delta, "content", None)
                    if isinstance(content_chunk, str) and content_chunk:
                        round_text_parts.append(content_chunk)
                        think_buffer, is_in_think_tag = _process_text_with_think_tags(
                            content_chunk, think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id
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
                _logger.warning("OpenAI streaming loop exception: %s", e)
                _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {e}", stream_id=stream_id)
                _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id)
                return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error, actual_thinking_level

            _flush_think_buffer(think_buffer, is_in_think_tag, full_content, full_thinking, bridge, tab_id, conversation_id, stream_id=stream_id)
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
                res = _run_web_search_tool_call(bridge, tab_id, conversation_id, tc["name"], tc.get("arguments") or "{}", abort_event, stream_id=stream_id)
                normalized_messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(res, ensure_ascii=False),
                })

        return full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error, actual_thinking_level
    finally:
        ClientManager.close_client(client)


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
    stream_id: Optional[str] = None,
):
    """
    Executes streaming completion in a background thread using the appropriate SDK
    (Anthropic SDK for anthropic, Google GenAI SDK for gemini, OpenAI SDK for all others)
    and pushes events to the bridge event queue.
    """
    active_stream_id = stream_id or str(uuid.uuid4())
    abort_event = StreamManager.register_stream(tab_id, stream_id=active_stream_id, conversation_id=conversation_id)
    user_question = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    provider = ClientManager.get_provider(provider_id)
    stream_fn = _SDK_STREAMERS.get(ClientManager.get_sdk_type(provider, provider_id), _stream_openai)

    try:
        try:
            full_content, full_thinking, prompt_tokens, completion_tokens, cached_tokens, stream_had_error, actual_thinking_level = stream_fn(
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
                stream_id=active_stream_id,
            )
        except Exception as e:
            _logger.exception("Unhandled streaming exception on tab=%s conv=%s", tab_id, conversation_id)
            _emit_error(bridge, tab_id, conversation_id, f"Unexpected streaming error: {e}", stream_id=active_stream_id)
            return
    finally:
        StreamManager.unregister_stream(tab_id, stream_id=active_stream_id)

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
            thinking_level=actual_thinking_level,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            msg_id=assistant_msg_id,
        )
        if msg_record is None:
            # Conversation was deleted while stream was running/unwinding
            return

    if msg_record is not None and not stream_had_error:
        _trigger_auto_naming(
            bridge,
            tab_id,
            conversation_id,
            user_question,
            provider_id,
            model_id,
            stream_id=active_stream_id,
        )

    token_stats = get_token_stats(conversation_id)

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
                "was_aborted": abort_event.is_set(),
            },
        )
