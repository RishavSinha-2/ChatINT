"""Shared streaming primitives, thinking-level cache, <think> parser, and ProviderAdapter ABC."""
from __future__ import annotations

from abc import ABC, abstractmethod
import json
import threading
from typing import Any, Dict, List, Optional

from chatint.logging_setup import get_logger
from chatint.web_search_tool import (
    IMAGE_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    execute_image_search,
    execute_web_search,
    get_web_search_system_hint,
    is_web_search_available,
)

_logger = get_logger("chatint.providers")

UNIFIED_THINKING_CHAINS: Dict[str, List[Optional[str]]] = {
    "minimal": ["minimal", "low", None],
    "low": ["low", None],
    "medium": ["medium", "low", None],
    "high": ["high", None],
    "xhigh": ["xhigh", "high", None],
    "max": ["max", "xhigh", "high", None],
    "none": [None],
}

_RESOLVED_THINKING_CACHE: Dict[tuple[str, str, str], str] = {}


def get_unified_thinking_chain(
    level: Optional[str],
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> List[Optional[str]]:
    """Return the ordered fallback chain of thinking efforts for (provider, model, requested_level)."""
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
            except Exception as exc:
                _logger.debug("Could not load resolved thinking level from DB: %s", exc)

        if cached is not None:
            cached_val = None if cached == "none" else cached
            return [cached_val] + [e for e in full_chain if e != cached_val]

    return full_chain


def record_resolved_thinking_level(
    provider_id: str, model_id: str, requested_level: str, resolved_level: str
) -> None:
    """Cache and persist a verified thinking effort for (provider, model, requested_level)."""
    if provider_id and model_id:
        req = (requested_level or "none").strip().lower()
        res = (resolved_level or "none").strip().lower()
        _RESOLVED_THINKING_CACHE[(provider_id, model_id, req)] = res
        try:
            from chatint.db import update_model_resolved_thinking

            update_model_resolved_thinking(provider_id, model_id, req, res)
        except Exception as exc:
            _logger.debug("Could not persist resolved thinking level: %s", exc)


def clear_resolved_thinking_cache(
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
    requested_level: Optional[str] = None,
) -> None:
    """Clear cached thinking levels in memory and SQLite."""
    if not provider_id and not model_id:
        _RESOLVED_THINKING_CACHE.clear()
        return

    req = requested_level.strip().lower() if requested_level else None
    if provider_id and model_id and req:
        _RESOLVED_THINKING_CACHE.pop((provider_id, model_id, req), None)
    else:
        keys_to_del = [
            k
            for k in _RESOLVED_THINKING_CACHE
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
        except Exception as exc:
            _logger.debug("Could not clear resolved thinking level in DB: %s", exc)


def _safe_dispatch(bridge: Any, event_data: Dict[str, Any]) -> None:
    if not bridge:
        return
    try:
        if hasattr(bridge, "push_event"):
            bridge.push_event(event_data)
        elif hasattr(bridge, "_push_event"):
            bridge._push_event(event_data)
    except Exception as exc:
        _logger.warning("Streamer dispatch error: %s", exc)


def _emit_error(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    message: str,
    stream_id: Optional[str] = None,
) -> None:
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
) -> None:
    if full_content or full_thinking:
        _emit_error(bridge, tab_id, conversation_id, f"Streaming interrupted: {err}", stream_id=stream_id)
    else:
        _emit_error(
            bridge,
            tab_id,
            conversation_id,
            f"API Error ({provider_id} / {model_id}): {err}",
            stream_id=stream_id,
        )


def _emit_start(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    assistant_msg_id: Optional[str],
    provider_id: str,
    model_id: str,
    thinking_level: str,
    stream_id: Optional[str] = None,
) -> None:
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
) -> None:
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
) -> None:
    if think_buffer:
        chunk_type = "thinking_chunk" if is_in_think_tag else "content_chunk"
        target = full_thinking if is_in_think_tag else full_content
        _emit_chunk(bridge, tab_id, conversation_id, chunk_type, think_buffer, target, stream_id=stream_id)


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
    has_round_visible_text: Optional[List[bool]] = None,
) -> tuple[str, bool]:
    """
    Parse leading `<think>...</think>` blocks emitted at the start of an assistant round.
    Once any non-whitespace visible text has been emitted in the current round, literal `<think>`
    mentions in prose or code blocks are never swallowed into `full_thinking` (fixes NEW-M1).
    """
    text = think_buffer + content_chunk
    think_buffer = ""

    if has_round_visible_text is None:
        # Fallback when called standalone: check whether full_content already has non-whitespace text
        visible_started = bool("".join(full_content).strip())
        has_round_visible_text = [visible_started, False]
    elif len(has_round_visible_text) < 2:
        has_round_visible_text.append(False)

    while text:
        if is_in_think_tag:
            close_tag = "</think>"
            if close_tag in text:
                before, rest = text.split(close_tag, 1)
                if before:
                    _emit_chunk(
                        bridge, tab_id, conversation_id, "thinking_chunk", before, full_thinking, stream_id=stream_id
                    )
                is_in_think_tag = False
                has_round_visible_text[1] = True
                text = rest.lstrip("\r\n") if not has_round_visible_text[0] else rest
                continue

            partial_len = next((i for i in range(len(close_tag) - 1, 0, -1) if close_tag.startswith(text[-i:])), 0)
            if partial_len:
                think_buffer = text[-partial_len:]
                pre = text[:-partial_len]
                if pre:
                    _emit_chunk(
                        bridge, tab_id, conversation_id, "thinking_chunk", pre, full_thinking, stream_id=stream_id
                    )
                break

            _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", text, full_thinking, stream_id=stream_id)
            break

        # Not currently inside <think>: only allow opening <think> once at the very beginning of the round
        if has_round_visible_text[0]:
            _emit_chunk(bridge, tab_id, conversation_id, "content_chunk", text, full_content, stream_id=stream_id)
            break

        if has_round_visible_text[1]:
            # Initial <think>...</think> block already closed; strip leading blank lines and emit all subsequent text visibly
            stripped_post_think = text.lstrip("\r\n")
            if stripped_post_think:
                has_round_visible_text[0] = True
                _emit_chunk(
                    bridge,
                    tab_id,
                    conversation_id,
                    "content_chunk",
                    stripped_post_think,
                    full_content,
                    stream_id=stream_id,
                )
            break

        leading_ws_len = len(text) - len(text.lstrip())
        after_ws = text[leading_ws_len:]
        if not after_ws:
            # Pure leading whitespace so far; buffer small prefix until we know whether <think> follows
            if len(text) <= 32:
                think_buffer = text
            else:
                _emit_chunk(bridge, tab_id, conversation_id, "content_chunk", text, full_content, stream_id=stream_id)
            break

        open_tag = "<think>"
        if after_ws.startswith(open_tag):
            if full_thinking and "".join(full_thinking).strip() and not "".join(full_thinking).endswith("\n"):
                _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", "\n\n", full_thinking, stream_id=stream_id)
            is_in_think_tag = True
            text = after_ws[len(open_tag) :]
            continue

        if len(after_ws) < len(open_tag) and open_tag.startswith(after_ws):
            # Incomplete `<thi` at the very start of the round
            think_buffer = text
            break

        # First non-whitespace characters are ordinary visible content (e.g., prose, markdown, ```xml)
        has_round_visible_text[0] = True
        _emit_chunk(bridge, tab_id, conversation_id, "content_chunk", text, full_content, stream_id=stream_id)
        break

    return think_buffer, is_in_think_tag


def _ensure_post_tool_separator(
    full_content: List[str],
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    stream_id: Optional[str] = None,
) -> None:
    """Ensure a blank line separates pre-tool text from post-tool response text."""
    joined = "".join(full_content)
    if joined.strip() and not joined.endswith(("\n", " ")):
        _emit_chunk(bridge, tab_id, conversation_id, "content_chunk", "\n\n", full_content, stream_id=stream_id)


def _ensure_post_tool_thinking_separator(
    full_thinking: List[str],
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    stream_id: Optional[str] = None,
) -> None:
    """Ensure a blank line separates Round N reasoning from Round N+1 reasoning (fixes NEW-M2)."""
    joined = "".join(full_thinking)
    if joined.strip() and not joined.endswith("\n\n"):
        sep = "\n" if joined.endswith("\n") else "\n\n"
        _emit_chunk(bridge, tab_id, conversation_id, "thinking_chunk", sep, full_thinking, stream_id=stream_id)


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
    """Execute a web_search or search_images tool call, respecting abort_event before and after execution."""
    if tool_name not in (WEB_SEARCH_TOOL_NAME, IMAGE_SEARCH_TOOL_NAME):
        return {"ok": False, "error": f"Unsupported tool '{tool_name}'."}

    if abort_event.is_set():
        return {"ok": False, "error": "Aborted by user."}

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
        if abort_event.is_set():
            return {"ok": False, "error": "Aborted by user."}
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
    if abort_event.is_set():
        return {"ok": False, "error": "Aborted by user."}
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


def _prepare_system_and_turns(messages: List[Dict[str, str]]) -> tuple[str, List[Dict[str, str]]]:
    """Extract system prompt and normalize alternating user/assistant turns across all providers."""
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

    while len(turns) > 1 and turns[0]["role"] == "assistant":
        turns = turns[1:]
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "Hello"})

    while len(turns) > 1 and turns[-1]["role"] == "assistant":
        turns.pop()

    return "\n\n".join(system_parts).strip(), turns


class ProviderAdapter(ABC):
    """Unified provider adapter contract for listing models, short completions, and multi-round streaming."""

    sdk_type: str = "openai"

    @abstractmethod
    def list_raw_models(self, client: Any) -> List[Any]:
        """Fetch raw model entries from an initialized SDK client."""

    @abstractmethod
    def complete_title(
        self, client: Any, model_id: str, system_instruction: str, user_snippet: str
    ) -> Optional[tuple[str, int, int, int]]:
        """Generate a short conversation title and return (raw_text, prompt_tok, completion_tok, cached_tok)."""

    @abstractmethod
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
        """Execute multi-round streaming completion and return collected chunks, usage, error flag, and resolved level."""
