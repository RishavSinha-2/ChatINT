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
        provider_label = provider_id
        try:
            from chatint.client_manager import ClientManager

            prov = ClientManager.get_provider(provider_id)
            if prov and prov.get("name"):
                provider_label = prov["name"]
        except Exception:
            pass
        _emit_error(
            bridge,
            tab_id,
            conversation_id,
            f"API Error ({provider_label} / {model_id}): {err}",
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


from pathlib import Path
import inspect
import time
import uuid
from chatint.web_search_tool import (
    IMAGE_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    is_gemini_web_search_enabled,
)
from chatint.tools import (
    build_tools_system_hint,
    get_active_tools,
    get_tool_budget_limits,
    get_tool_spec,
    to_anthropic_tools,
    to_gemini_tools,
    to_openai_tools,
)


def _resolve_search_enabled(web_search_enabled: bool) -> bool:
    return bool(web_search_enabled) and is_web_search_available()[0]


def _append_search_system_hint(system_prompt: str, search_enabled: bool) -> str:
    if not search_enabled:
        return system_prompt
    hint = get_web_search_system_hint()
    return f"{system_prompt}\n\n{hint}".strip() if system_prompt else hint


def _parse_tool_args(raw_args: Any) -> Dict[str, Any]:
    """Normalize provider tool arguments (dict, JSON string, or plain text query) into a dictionary."""
    if isinstance(raw_args, dict):
        return dict(raw_args)
    if isinstance(raw_args, str):
        s = raw_args.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {"query": s}
    return {}


def _prepare_tool_session(
    *,
    web_search_enabled: bool,
    agent_enabled: bool,
    workspace_dir: str,
    agent_mode: str,
    system_prompt: str,
    resolve_search_fn: Any = None,
) -> tuple[bool, str, List[Any], str, int, int, Dict[str, Any]]:
    """Build active tools, system hint, budget caps, and mutable runtime approval state."""
    search_fn = resolve_search_fn or _resolve_search_enabled
    search_enabled = bool(search_fn(web_search_enabled))
    raw_ws = str(workspace_dir or "").strip()
    clean_ws = raw_ws if (raw_ws and Path(raw_ws).is_dir()) else ""
    mode_norm = "auto" if str(agent_mode or "manual").strip().lower() == "auto" else "manual"
    specs = get_active_tools(
        web_search_enabled=search_enabled,
        agent_enabled=bool(agent_enabled),
        has_workspace=bool(clean_ws),
    )
    hint = build_tools_system_hint(
        web_search_enabled=search_enabled,
        agent_enabled=bool(agent_enabled),
        workspace_dir=clean_ws,
        agent_mode=mode_norm,
    )
    merged_system = f"{system_prompt}\n\n{hint}".strip() if (system_prompt and hint) else (system_prompt or hint)
    max_rounds, max_calls = get_tool_budget_limits(bool(agent_enabled))
    runtime_state: Dict[str, Any] = {"agent_mode": mode_norm}
    return search_enabled, clean_ws, specs, merged_system, max_rounds, max_calls, runtime_state


def _summarize_tool_step(tool_name: str, args: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> tuple[str, str]:
    """Return human-readable (status_summary, args_preview) for timeline display."""
    if tool_name == WEB_SEARCH_TOOL_NAME:
        arg_prev = str(args.get("query") or "")[:140]
        if result is None:
            return ("Searching Google" if is_gemini_web_search_enabled() else "Searching web"), arg_prev
        count = len(result.get("results") or [])
        label = "Google Search complete" if result.get("provider_used") == "gemini" else f"Web search complete ({count} results)"
        return (label if result.get("ok", True) else str(result.get("error") or "Web search failed")), arg_prev
    if tool_name == IMAGE_SEARCH_TOOL_NAME:
        arg_prev = str(args.get("query") or "")[:140]
        if result is None:
            return "Searching images", arg_prev
        count = len(result.get("images") or result.get("results") or [])
        return (f"Found {count} images" if result.get("ok", True) else str(result.get("error") or "Image search failed")), arg_prev
    if tool_name == "fetch_url":
        arg_prev = str(args.get("url") or "")[:140]
        if result is None:
            return f"Fetching {arg_prev or 'URL'}", arg_prev
        return (f"Fetched {result.get('title') or arg_prev}" if result.get("ok", True) else str(result.get("error") or "Fetch failed")), arg_prev
    if tool_name == "list_dir":
        arg_prev = str(args.get("path") or ".")[:140]
        if result is None:
            return f"Listing {arg_prev}", arg_prev
        return (f"Listed {result.get('count', 0)} items" if result.get("ok", True) else str(result.get("error") or "List failed")), arg_prev
    if tool_name == "read_file":
        path_val = str(args.get("path") or "")
        arg_prev = f"{path_val} ({args.get('start_line', 1)}-{args.get('end_line', 250)})"[:140]
        if result is None:
            return f"Reading {path_val}", arg_prev
        if not result.get("ok", True):
            return str(result.get("error") or "Read failed"), arg_prev
        return f"Read {result.get('path', path_val)} ({result.get('start_line', 1)}-{result.get('end_line', 0)})", arg_prev
    if tool_name == "search_files":
        q_val = str(args.get("query") or "")
        arg_prev = f"{q_val} in {args.get('path') or '.'}"[:140]
        if result is None:
            return f"Searching files for '{q_val[:40]}'", arg_prev
        return (f"Found {result.get('count', 0)} matches" if result.get("ok", True) else str(result.get("error") or "Search failed")), arg_prev
    if tool_name == "write_file":
        path_val = str(args.get("path") or "")
        if result is None:
            return f"Writing {path_val}", path_val[:140]
        if not result.get("ok", True):
            return str(result.get("error") or "Write failed"), path_val[:140]
        verb = "Created" if result.get("created") else "Updated"
        return f"{verb} {result.get('path', path_val)}", path_val[:140]
    if tool_name == "edit_file":
        path_val = str(args.get("path") or "")
        if result is None:
            return f"Editing {path_val}", path_val[:140]
        if not result.get("ok", True):
            return str(result.get("error") or "Edit failed"), path_val[:140]
        return f"Edited {result.get('path', path_val)}", path_val[:140]

    arg_prev = json.dumps(args, ensure_ascii=False)[:140] if args else ""
    if result is None:
        return f"Running {tool_name}", arg_prev
    return (f"Completed {tool_name}" if result.get("ok", True) else str(result.get("error") or f"{tool_name} failed")), arg_prev


def _build_result_preview(tool_name: str, result: Dict[str, Any]) -> str:
    """Create a compact preview string stored in persisted tool_trace records."""
    if not result.get("ok", True):
        return str(result.get("error") or "Tool error")[:300]
    if tool_name == "read_file":
        return str(result.get("content") or "")[:300]
    if tool_name == "list_dir":
        names = [str(e.get("path") or "") for e in (result.get("entries") or [])[:12]]
        return ", ".join(names)[:300]
    if tool_name == "search_files":
        hits = [f"{m.get('path')}:{m.get('line')}" for m in (result.get("matches") or [])[:8]]
        return ", ".join(hits)[:300]
    if tool_name == "fetch_url":
        return str(result.get("content") or "")[:300]
    if tool_name in ("write_file", "edit_file"):
        return str(result.get("diff") or f"Updated {result.get('path', '')}")[:300]
    return json.dumps(result, ensure_ascii=False)[:300]


def _run_registered_tool_call(
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    tool_name: str,
    raw_args: Any,
    abort_event: threading.Event,
    stream_id: Optional[str] = None,
    *,
    call_id: Optional[str] = None,
    workspace_dir: str = "",
    agent_mode: str = "manual",
    runtime_state: Optional[Dict[str, Any]] = None,
    tool_trace: Optional[List[Dict[str, Any]]] = None,
    web_search_enabled: bool = True,
    agent_enabled: bool = False,
) -> Dict[str, Any]:
    """Validate authorization, enforce Manual-mode write/edit approvals, execute tool, and emit trace events."""
    step_call_id = call_id or f"tc-{uuid.uuid4().hex[:10]}"
    args = _parse_tool_args(raw_args)
    spec = get_tool_spec(tool_name)

    def _record_and_emit_failure(err_msg: str, status_val: str = "error", dur_ms: int = 0) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": "tool_status",
            "tab_id": tab_id,
            "conversation_id": conversation_id,
            "tool": tool_name,
            "status": status_val,
            "call_id": step_call_id,
            "summary": err_msg,
            "args_preview": str(args.get("path") or args.get("query") or args.get("url") or "")[:140],
            "result_preview": err_msg[:300],
            "error": err_msg,
            "duration_ms": dur_ms,
        }
        if stream_id:
            payload["stream_id"] = stream_id
        _safe_dispatch(bridge, payload)
        if tool_trace is not None:
            tool_trace.append(
                {
                    "call_id": step_call_id,
                    "tool": tool_name,
                    "status": status_val,
                    "summary": err_msg,
                    "args_preview": payload["args_preview"],
                    "result_preview": err_msg[:300],
                    "diff": "",
                    "duration_ms": dur_ms,
                }
            )
        return {"ok": False, "error": err_msg}

    if not spec:
        return _record_and_emit_failure(f"Unknown tool '{tool_name}'.")
    if spec.name in (WEB_SEARCH_TOOL_NAME, IMAGE_SEARCH_TOOL_NAME) and not web_search_enabled:
        return _record_and_emit_failure(f"Tool '{tool_name}' is not enabled for this message.")
    if spec.name not in (WEB_SEARCH_TOOL_NAME, IMAGE_SEARCH_TOOL_NAME) and not agent_enabled:
        return _record_and_emit_failure(f"Agent tool '{tool_name}' requires Agent Mode to be enabled.")
    clean_ws = str(workspace_dir or "").strip()
    if spec.requires_workspace and (not clean_ws or not Path(clean_ws).is_dir()):
        return _record_and_emit_failure("No valid Workspace folder is selected for this chat.")

    if spec.name == WEB_SEARCH_TOOL_NAME:
        return _run_web_search_tool_call(
            bridge,
            tab_id,
            conversation_id,
            tool_name,
            raw_args,
            abort_event,
            stream_id=stream_id,
            call_id=step_call_id,
            tool_trace=tool_trace,
        )
    if spec.name == IMAGE_SEARCH_TOOL_NAME:
        return _run_web_search_tool_call(
            bridge,
            tab_id,
            conversation_id,
            tool_name,
            raw_args,
            abort_event,
            stream_id=stream_id,
            call_id=step_call_id,
            tool_trace=tool_trace,
        )

    t0 = time.perf_counter()
    running_summary, args_preview = _summarize_tool_step(tool_name, args, None)
    running_ev: Dict[str, Any] = {
        "type": "tool_status",
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "tool": tool_name,
        "status": "running",
        "call_id": step_call_id,
        "summary": running_summary,
        "args_preview": args_preview,
    }
    if stream_id:
        running_ev["stream_id"] = stream_id
    _safe_dispatch(bridge, running_ev)

    effective_mode = "auto" if str((runtime_state or {}).get("agent_mode") or agent_mode).strip().lower() == "auto" else "manual"
    if spec.mutating and effective_mode == "manual":
        preview: Dict[str, Any] = {"ok": True, "path": str(args.get("path") or ""), "summary": running_summary, "diff": ""}
        if spec.preview_fn is not None:
            try:
                preview = spec.preview_fn(args, Path(clean_ws) if clean_ws else None)
            except PermissionError as exc:
                preview = {"ok": False, "error": str(exc)}
            except Exception as exc:
                preview = {"ok": False, "error": str(exc)}

        if not preview.get("ok", True):
            elapsed_ms = max(0, int(round((time.perf_counter() - t0) * 1000)))
            return _record_and_emit_failure(str(preview.get("error") or "Invalid workspace target."), "error", elapsed_ms)

        if not stream_id:
            elapsed_ms = max(0, int(round((time.perf_counter() - t0) * 1000)))
            return _record_and_emit_failure("Manual approval denied (no active stream session).", "denied", elapsed_ms)

        from chatint.db import update_conversation
        from chatint.services.stream_coordinator import StreamManager

        approval_id = f"apr-{uuid.uuid4().hex[:10]}"
        awaiting_ev: Dict[str, Any] = {
            "type": "tool_status",
            "tab_id": tab_id,
            "conversation_id": conversation_id,
            "stream_id": stream_id,
            "tool": tool_name,
            "status": "awaiting_approval",
            "call_id": step_call_id,
            "summary": f"Waiting for approval: {preview.get('summary') or running_summary}",
            "args_preview": args_preview,
        }
        _safe_dispatch(bridge, awaiting_ev)
        _safe_dispatch(
            bridge,
            {
                "type": "tool_approval_required",
                "tab_id": tab_id,
                "conversation_id": conversation_id,
                "stream_id": stream_id,
                "approval_id": approval_id,
                "call_id": step_call_id,
                "tool": tool_name,
                "path": str(preview.get("path") or args.get("path") or ""),
                "summary": str(preview.get("summary") or running_summary),
                "diff": str(preview.get("diff") or ""),
            },
        )
        decision = StreamManager.request_approval(tab_id, stream_id, approval_id, abort_event)
        if abort_event.is_set() or decision == "deny":
            elapsed_ms = max(0, int(round((time.perf_counter() - t0) * 1000)))
            deny_msg = "Aborted while waiting for approval." if abort_event.is_set() else "User denied file modification."
            return _record_and_emit_failure(deny_msg, "denied", elapsed_ms)

        if decision == "allow_all_tab":
            if runtime_state is not None:
                runtime_state["agent_mode"] = "auto"
            try:
                update_conversation(conversation_id, agent_mode="auto")
            except Exception:
                pass
            _safe_dispatch(
                bridge,
                {
                    "type": "agent_mode_changed",
                    "tab_id": tab_id,
                    "conversation_id": conversation_id,
                    "stream_id": stream_id,
                    "agent_mode": "auto",
                },
            )

        resumed_ev: Dict[str, Any] = {
            "type": "tool_status",
            "tab_id": tab_id,
            "conversation_id": conversation_id,
            "stream_id": stream_id,
            "tool": tool_name,
            "status": "running",
            "call_id": step_call_id,
            "summary": running_summary,
            "args_preview": args_preview,
        }
        _safe_dispatch(bridge, resumed_ev)

    try:
        call_kwargs = dict(args)
        if spec.requires_workspace:
            call_kwargs["workspace_dir"] = clean_ws
        if tool_name == WEB_SEARCH_TOOL_NAME:
            call_kwargs["conversation_id"] = conversation_id
        result = spec.handler(**call_kwargs)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    elapsed_ms = max(0, int(round((time.perf_counter() - t0) * 1000)))
    ok = bool(result.get("ok", True))
    status_val = "done" if ok else "error"
    done_summary, _ = _summarize_tool_step(tool_name, args, result)
    result_preview = _build_result_preview(tool_name, result)
    diff_str = str(result.get("diff") or "")

    done_ev: Dict[str, Any] = {
        "type": "tool_status",
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "tool": tool_name,
        "status": status_val,
        "call_id": step_call_id,
        "summary": done_summary,
        "args_preview": args_preview,
        "result_preview": result_preview,
        "diff": diff_str,
        "duration_ms": elapsed_ms,
    }
    if not ok:
        done_ev["error"] = str(result.get("error") or done_summary)
    if stream_id:
        done_ev["stream_id"] = stream_id
    _safe_dispatch(bridge, done_ev)

    if tool_trace is not None:
        tool_trace.append(
            {
                "call_id": step_call_id,
                "tool": tool_name,
                "status": status_val,
                "summary": done_summary,
                "args_preview": args_preview,
                "result_preview": result_preview,
                "diff": diff_str,
                "duration_ms": elapsed_ms,
            }
        )
    return result


def _execute_adapter_tool_call(
    *,
    bridge: Any,
    tab_id: str,
    conversation_id: str,
    tool_name: str,
    raw_args: Any,
    abort_event: threading.Event,
    stream_id: Optional[str],
    call_id: Optional[str],
    workspace_dir: str,
    runtime_state: Dict[str, Any],
    tool_trace: Optional[List[Dict[str, Any]]],
    web_search_enabled: bool,
    agent_enabled: bool,
    web_runner: Any,
) -> Dict[str, Any]:
    """Dispatch web/image tools via adapter `web_runner` (preserving test mocks) and agent tools via registry."""
    if (not agent_enabled) or tool_name in (WEB_SEARCH_TOOL_NAME, IMAGE_SEARCH_TOOL_NAME):
        if not web_search_enabled:
            err_res = {"ok": False, "error": f"Tool '{tool_name}' is not enabled."}
            if tool_trace is not None:
                tool_trace.append(
                    {
                        "call_id": call_id or f"tc-{uuid.uuid4().hex[:10]}",
                        "tool": tool_name,
                        "status": "error",
                        "summary": err_res["error"],
                        "args_preview": _parse_tool_query(raw_args)[:140],
                        "result_preview": err_res["error"],
                        "diff": "",
                        "duration_ms": 0,
                    }
                )
            return err_res
        if web_runner is _run_web_search_tool_call:
            return web_runner(
                bridge,
                tab_id,
                conversation_id,
                tool_name,
                raw_args,
                abort_event,
                stream_id=stream_id,
                call_id=call_id,
                tool_trace=tool_trace,
            )
        t0 = time.perf_counter()
        result = web_runner(
            bridge,
            tab_id,
            conversation_id,
            tool_name,
            raw_args,
            abort_event,
            stream_id=stream_id,
        )
        if tool_trace is not None:
            parsed = _parse_tool_args(raw_args)
            summ, arg_prev = _summarize_tool_step(tool_name, parsed, result)
            tool_trace.append(
                {
                    "call_id": call_id or f"tc-{uuid.uuid4().hex[:10]}",
                    "tool": tool_name,
                    "status": "done" if result.get("ok", True) else "error",
                    "summary": summ,
                    "args_preview": arg_prev,
                    "result_preview": _build_result_preview(tool_name, result),
                    "diff": "",
                    "duration_ms": max(0, int(round((time.perf_counter() - t0) * 1000))),
                }
            )
        return result

    return _run_registered_tool_call(
        bridge,
        tab_id,
        conversation_id,
        tool_name,
        raw_args,
        abort_event,
        stream_id=stream_id,
        call_id=call_id,
        workspace_dir=workspace_dir,
        agent_mode=str(runtime_state.get("agent_mode") or "manual"),
        runtime_state=runtime_state,
        tool_trace=tool_trace,
        web_search_enabled=web_search_enabled,
        agent_enabled=agent_enabled,
    )


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
    *,
    call_id: Optional[str] = None,
    tool_trace: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Execute a web_search or search_images tool call, respecting abort_event before and after execution."""
    if tool_name not in (WEB_SEARCH_TOOL_NAME, IMAGE_SEARCH_TOOL_NAME):
        return {"ok": False, "error": f"Unsupported tool '{tool_name}'."}

    if abort_event.is_set():
        return {"ok": False, "error": "Aborted by user."}

    step_call_id = call_id or f"tc-{uuid.uuid4().hex[:10]}"
    query = _parse_tool_query(raw_args)
    args = _parse_tool_args(raw_args)
    tool_kind = "image_search" if tool_name == IMAGE_SEARCH_TOOL_NAME else "web_search"
    running_summary, args_preview = _summarize_tool_step(tool_name, args, None)

    searching_ev: Dict[str, Any] = {
        "type": "tool_status",
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "status": "searching",
        "tool": tool_kind,
        "query": query,
        "call_id": step_call_id,
        "summary": running_summary,
        "args_preview": args_preview,
    }
    if stream_id:
        searching_ev["stream_id"] = stream_id
    _safe_dispatch(bridge, searching_ev)

    if abort_event.is_set():
        return {"ok": False, "error": "Aborted by user."}

    t0 = time.perf_counter()
    if tool_name == IMAGE_SEARCH_TOOL_NAME:
        result = execute_image_search(query, max_results=3)
        if abort_event.is_set():
            return {"ok": False, "error": "Aborted by user."}
        elapsed_ms = max(0, int(round((time.perf_counter() - t0) * 1000)))
        done_summary, _ = _summarize_tool_step(tool_name, args, result)
        result_preview = _build_result_preview(tool_name, result)
        done_ev: Dict[str, Any] = {
            "type": "tool_status",
            "tab_id": tab_id,
            "conversation_id": conversation_id,
            "status": "done",
            "tool": "image_search",
            "query": query,
            "ok": bool(result.get("ok")),
            "images_found": len(result.get("images", [])),
            "call_id": step_call_id,
            "summary": done_summary,
            "args_preview": args_preview,
            "result_preview": result_preview,
            "diff": "",
            "duration_ms": elapsed_ms,
        }
        if stream_id:
            done_ev["stream_id"] = stream_id
        _safe_dispatch(bridge, done_ev)
        if tool_trace is not None:
            tool_trace.append(
                {
                    "call_id": step_call_id,
                    "tool": tool_name,
                    "status": "done" if result.get("ok") else "error",
                    "summary": done_summary,
                    "args_preview": args_preview,
                    "result_preview": result_preview,
                    "diff": "",
                    "duration_ms": elapsed_ms,
                }
            )
        return result

    result = execute_web_search(query, conversation_id=conversation_id)
    if abort_event.is_set():
        return {"ok": False, "error": "Aborted by user."}
    elapsed_ms = max(0, int(round((time.perf_counter() - t0) * 1000)))
    done_summary, _ = _summarize_tool_step(tool_name, args, result)
    result_preview = _build_result_preview(tool_name, result)
    done_ev = {
        "type": "tool_status",
        "tab_id": tab_id,
        "conversation_id": conversation_id,
        "status": "done",
        "tool": "web_search",
        "query": query,
        "ok": bool(result.get("ok")),
        "provider_used": result.get("provider_used", ""),
        "results_count": len(result.get("results") or result.get("sources") or []),
        "search_requests_used": int(result.get("search_requests_used") or 0),
        "ddg_requests_used": int(result.get("ddg_requests_used") or 0),
        "sources": result.get("sources") or [],
        "call_id": step_call_id,
        "summary": done_summary,
        "args_preview": args_preview,
        "result_preview": result_preview,
        "diff": "",
        "duration_ms": elapsed_ms,
    }
    if stream_id:
        done_ev["stream_id"] = stream_id
    _safe_dispatch(bridge, done_ev)
    if tool_trace is not None:
        tool_trace.append(
            {
                "call_id": step_call_id,
                "tool": tool_name,
                "status": "done" if result.get("ok") else "error",
                "summary": done_summary,
                "args_preview": args_preview,
                "result_preview": result_preview,
                "diff": "",
                "duration_ms": elapsed_ms,
            }
        )
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


def _record_model_ms(timing_stats: Optional[Dict[str, int]], started_at: Optional[float]) -> None:
    """Accumulate elapsed milliseconds for a winning model API round into `timing_stats`."""
    if timing_stats is None or started_at is None:
        return
    elapsed_ms = max(0, int(round((time.perf_counter() - started_at) * 1000)))
    timing_stats["model_ms"] = int(timing_stats.get("model_ms") or 0) + elapsed_ms


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
        agent_enabled: bool = False,
        workspace_dir: str = "",
        agent_mode: str = "manual",
        tool_trace: Optional[List[Dict[str, Any]]] = None,
        timing_stats: Optional[Dict[str, int]] = None,
    ) -> tuple[List[str], List[str], int, int, int, bool, str]:
        """Execute multi-round streaming completion and return collected chunks, usage, error flag, and resolved level."""
