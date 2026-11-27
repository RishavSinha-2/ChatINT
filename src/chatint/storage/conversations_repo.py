"""Repository for conversations, messages, branching, and chat imports."""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional
import uuid

from chatint.catalog import strip_model_prefix
from chatint.logging_setup import get_logger
from chatint.storage.connection import open_db
from chatint.storage.ledger_repo import get_token_stats

_logger = get_logger("chatint.storage.conversations_repo")
_DEFAULT_TITLE_RE = re.compile(r"^new chat(?:\s*\(\d+\))?$", re.IGNORECASE)

_ALLOWED_CONV_COLUMNS = {
    "title",
    "system_prompt",
    "max_context_messages",
    "is_imported",
    "is_auto_named",
    "workspace_dir",
    "agent_mode",
}
_ALLOWED_MSG_COLUMNS = {
    "role",
    "content",
    "thinking_content",
    "provider_id",
    "provider_name",
    "model_id",
    "thinking_level",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "duration_ms",
    "worked_ms",
    "tool_trace_json",
}


def _normalize_agent_mode(mode: Any) -> str:
    return "auto" if str(mode or "manual").strip().lower() == "auto" else "manual"


def _parse_tool_trace(raw_json: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_json, list):
        return [item for item in raw_json if isinstance(item, dict)]
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
    except Exception:
        return []


def is_default_conversation_title(title: Optional[str]) -> bool:
    """Return True if title is empty or a default placeholder ('New Chat', 'New Chat (1)', etc.)."""
    cleaned = (title or "").strip()
    if not cleaned:
        return True
    return bool(_DEFAULT_TITLE_RE.match(cleaned))


def get_unique_conversation_title(
    title: str,
    exclude_conv_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """
    Ensure no two named conversations share the exact same title (case-insensitive).
    If a conflict exists, appends (1), (2), etc.
    """
    candidate = (title or "New Chat").strip() or "New Chat"

    ctx = contextlib.nullcontext(conn) if conn is not None else open_db()
    with ctx as active_conn:
        cursor = active_conn.cursor()
        if exclude_conv_id:
            cursor.execute("SELECT title FROM conversations WHERE id != ?", (exclude_conv_id,))
        else:
            cursor.execute("SELECT title FROM conversations")
        rows = cursor.fetchall()

    existing_titles = {r["title"].strip().lower() for r in rows if r["title"]}

    if candidate.lower() not in existing_titles:
        return candidate

    match = re.search(r"^(.*?)(?:\s*\((\d+)\))?$", candidate)
    if match and match.group(1) and match.group(1).strip() and match.group(2) and int(match.group(2)) <= 500:
        base = match.group(1).strip()
        start_num = int(match.group(2)) + 1
    else:
        base = candidate
        start_num = 1

    i = start_num
    while True:
        suffixed = f"{base} ({i})"
        if suffixed.lower() not in existing_titles:
            return suffixed
        i += 1


def get_branch_title(
    original_title: str,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Generate a unique branch title: 'Original Title (Branch)', 'Original Title (Branch 1)', etc."""
    raw_title = (original_title or "New Chat").strip()
    clean_title = re.sub(r"\s*\(Branch(?:\s*\d+)?\)$", "", raw_title, flags=re.IGNORECASE).strip() or "New Chat"

    ctx = contextlib.nullcontext(conn) if conn is not None else open_db()
    with ctx as active_conn:
        cursor = active_conn.cursor()
        cursor.execute("SELECT title FROM conversations")
        rows = cursor.fetchall()

    existing_titles = {r["title"].strip().lower() for r in rows if r["title"]}

    candidate = f"{clean_title} (Branch)"
    if candidate.lower() not in existing_titles:
        return candidate

    i = 1
    while True:
        candidate_n = f"{clean_title} (Branch {i})"
        if candidate_n.lower() not in existing_titles:
            return candidate_n
        i += 1


def create_conversation(
    title: str = "New Chat",
    system_prompt: str = "",
    max_context: int = 30,
    workspace_dir: str = "",
    agent_mode: str = "manual",
) -> Dict[str, Any]:
    raw_title = (title or "").strip()
    is_unnamed_placeholder = raw_title.lower() in ("", "new chat")
    clean_ws = str(workspace_dir or "").strip()
    clean_mode = _normalize_agent_mode(agent_mode)

    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if is_unnamed_placeholder:
            # Keep default placeholder as "New Chat" with is_auto_named=0 so auto-naming runs cleanly on Turn 1
            resolved_title = "New Chat"
            is_auto_named = 0
        else:
            resolved_title = get_unique_conversation_title(raw_title, conn=conn)
            is_auto_named = 1

        cursor = conn.cursor()
        conv_id = str(uuid.uuid4())
        now = time.time()
        cursor.execute(
            """
            INSERT INTO conversations (
                id, title, system_prompt, max_context_messages, is_imported, is_auto_named,
                workspace_dir, agent_mode, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (conv_id, resolved_title, system_prompt, max_context, is_auto_named, clean_ws, clean_mode, now, now),
        )
        conn.commit()
    return {
        "id": conv_id,
        "title": resolved_title,
        "system_prompt": system_prompt,
        "max_context_messages": max_context,
        "is_imported": False,
        "is_auto_named": bool(is_auto_named),
        "workspace_dir": clean_ws,
        "agent_mode": clean_mode,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }


def get_conversations() -> List[Dict[str, Any]]:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.id, c.title, c.system_prompt, c.max_context_messages, c.is_imported, c.is_auto_named,
                   c.workspace_dir, c.agent_mode, c.created_at, c.updated_at,
                   COALESCE(SUM(CASE WHEN m.role = 'assistant' AND (LENGTH(TRIM(m.content)) > 0 OR LENGTH(TRIM(m.thinking_content)) > 0) THEN 1 ELSE 0 END), 0) as message_count,
                   COUNT(m.id) as total_messages
            FROM conversations c
            LEFT JOIN messages m ON c.id = m.conversation_id
            GROUP BY c.id
            HAVING message_count > 0 OR (c.is_imported = 1 AND total_messages > 0)
            ORDER BY c.updated_at DESC
        """)
        rows = cursor.fetchall()

    return [
        {
            "id": r["id"],
            "title": r["title"],
            "system_prompt": r["system_prompt"],
            "max_context_messages": r["max_context_messages"],
            "is_imported": bool(r["is_imported"]),
            "is_auto_named": bool(r["is_auto_named"]),
            "workspace_dir": r["workspace_dir"] or "",
            "agent_mode": _normalize_agent_mode(r["agent_mode"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "message_count": r["message_count"],
        }
        for r in rows
    ]


def get_conversation(conv_id: str) -> Optional[Dict[str, Any]]:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        conv_row = cursor.fetchone()
        if not conv_row:
            return None

        cursor.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
            (conv_id,),
        )
        msg_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT provider_id, provider_name, model_id,
                   prompt_tokens, completion_tokens, cached_tokens,
                   google_search_requests, ddg_search_requests, cost_usd, created_at
            FROM token_ledger
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conv_id,),
        )
        ledger_rows = cursor.fetchall()

    messages = [
        {
            "id": m["id"],
            "conversation_id": m["conversation_id"],
            "role": m["role"],
            "content": m["content"],
            "thinking_content": m["thinking_content"],
            "provider_id": m["provider_id"],
            "provider_name": m["provider_name"],
            "model_id": m["model_id"],
            "thinking_level": m["thinking_level"],
            "prompt_tokens": m["prompt_tokens"],
            "completion_tokens": m["completion_tokens"],
            "cached_tokens": m["cached_tokens"],
            "duration_ms": m["duration_ms"],
            "worked_ms": m["worked_ms"] if "worked_ms" in m.keys() else 0,
            "tool_trace": _parse_tool_trace(m["tool_trace_json"] if "tool_trace_json" in m.keys() else "[]"),
            "created_at": m["created_at"],
        }
        for m in msg_rows
    ]
    token_ledger = [
        {
            "provider_id": r["provider_id"],
            "provider_name": r["provider_name"],
            "model_id": r["model_id"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "cached_tokens": r["cached_tokens"],
            "google_search_requests": r["google_search_requests"],
            "ddg_search_requests": r["ddg_search_requests"],
            "cost_usd": r["cost_usd"],
            "created_at": r["created_at"],
        }
        for r in ledger_rows
    ]

    conv_keys = conv_row.keys()
    return {
        "id": conv_row["id"],
        "title": conv_row["title"],
        "system_prompt": conv_row["system_prompt"],
        "max_context_messages": conv_row["max_context_messages"],
        "is_imported": bool(conv_row["is_imported"]) if "is_imported" in conv_keys else False,
        "is_auto_named": bool(conv_row["is_auto_named"]) if "is_auto_named" in conv_keys else False,
        "workspace_dir": (conv_row["workspace_dir"] or "") if "is_imported" in conv_keys and "workspace_dir" in conv_keys else ((conv_row["workspace_dir"] or "") if "workspace_dir" in conv_keys else ""),
        "agent_mode": _normalize_agent_mode(conv_row["agent_mode"] if "agent_mode" in conv_keys else "manual"),
        "created_at": conv_row["created_at"],
        "updated_at": conv_row["updated_at"],
        "messages": messages,
        "token_ledger": token_ledger,
        "token_stats": get_token_stats(conv_id),
    }


def update_conversation(conv_id: str, only_if_default: bool = False, **kwargs) -> Optional[str]:
    invalid_keys = [k for k in kwargs if k not in _ALLOWED_CONV_COLUMNS]
    if invalid_keys:
        raise ValueError(f"Unsupported conversation update column(s): {', '.join(invalid_keys)}")

    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        if only_if_default:
            row = cursor.execute(
                "SELECT title, is_auto_named FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            if not row or bool(row["is_auto_named"]) or not is_default_conversation_title(row["title"]):
                conn.rollback()
                return None

        if "title" in kwargs:
            raw_title = (kwargs["title"] or "").strip() if isinstance(kwargs["title"], str) else ""
            clean_title = raw_title or "New Chat"
            kwargs["title"] = get_unique_conversation_title(clean_title, exclude_conv_id=conv_id, conn=conn)
            kwargs.setdefault("is_auto_named", 1)
        if "workspace_dir" in kwargs:
            kwargs["workspace_dir"] = str(kwargs["workspace_dir"] or "").strip()
        if "agent_mode" in kwargs:
            kwargs["agent_mode"] = _normalize_agent_mode(kwargs["agent_mode"])

        fields = [f"{k} = ?" for k in kwargs]
        values = list(kwargs.values())
        values.append(time.time())
        values.append(conv_id)
        set_clause = f"{', '.join(fields)}, updated_at = ?" if fields else "updated_at = ?"
        cursor.execute(f"UPDATE conversations SET {set_clause} WHERE id = ?", tuple(values))
        conn.commit()
    return kwargs.get("title")


def duplicate_conversation(conv_id: str) -> Optional[Dict[str, Any]]:
    """
    Create an exact duplicate/branch of the conversation under a unique branch title.
    Token usage and cost in token_ledger start at 0 on the branch.
    """
    conv = get_conversation(conv_id)
    if not conv:
        return None

    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        new_conv_id = str(uuid.uuid4())
        branch_title = get_branch_title(conv["title"], conn=conn)
        now = time.time()

        cursor.execute(
            """
            INSERT INTO conversations (
                id, title, system_prompt, max_context_messages, is_imported, is_auto_named,
                workspace_dir, agent_mode, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                new_conv_id,
                branch_title,
                conv.get("system_prompt", ""),
                conv.get("max_context_messages", 30),
                1 if conv.get("is_imported") else 0,
                conv.get("workspace_dir", ""),
                _normalize_agent_mode(conv.get("agent_mode", "manual")),
                now,
                now,
            ),
        )

        for idx, m in enumerate(conv.get("messages", [])):
            new_msg_id = str(uuid.uuid4())
            msg_time = now + (idx * 0.001)
            trace_json = json.dumps(_parse_tool_trace(m.get("tool_trace")))
            cursor.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, role, content, thinking_content,
                    provider_id, provider_name, model_id, thinking_level,
                    prompt_tokens, completion_tokens, cached_tokens, duration_ms, worked_ms, tool_trace_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?, ?)
                """,
                (
                    new_msg_id,
                    new_conv_id,
                    m["role"],
                    m.get("content", ""),
                    m.get("thinking_content", ""),
                    m.get("provider_id", ""),
                    m.get("provider_name", ""),
                    m.get("model_id", ""),
                    m.get("thinking_level", "none"),
                    trace_json,
                    msg_time,
                ),
            )

        conn.commit()
    return get_conversation(new_conv_id)


def _restore_imported_ledger(
    cursor: sqlite3.Cursor,
    conv_id: str,
    token_ledger: Optional[List[Dict[str, Any]]],
    token_stats: Optional[Dict[str, Any]],
    fallback_ts: float,
) -> None:
    """Restore exported token_ledger rows and/or token_stats summary on chat import."""
    per_model_meta: Dict[tuple[str, str], Dict[str, Any]] = {}
    if isinstance(token_stats, dict):
        for pm in token_stats.get("per_model") or []:
            if not isinstance(pm, dict):
                continue
            pid = str(pm.get("provider_id") or "unknown")
            mid = strip_model_prefix(str(pm.get("model_id") or "default"))
            per_model_meta[(pid, mid)] = pm

    raw_rows = [r for r in (token_ledger or []) if isinstance(r, dict)]
    inserted_g_req = 0
    inserted_d_req = 0

    if raw_rows:
        assigned_cost_keys: set[tuple[str, str]] = set()
        explicit_cost_keys = {
            (str(r.get("provider_id") or "unknown"), strip_model_prefix(str(r.get("model_id") or "default")))
            for r in raw_rows
            if r.get("cost_usd") is not None
        }
        for idx, r in enumerate(raw_rows):
            pid = str(r.get("provider_id") or "")
            mid = str(r.get("model_id") or "")
            clean_mid = strip_model_prefix(mid or "default")
            key = (pid or "unknown", clean_mid)
            pm = per_model_meta.get(key)
            pname = str(r.get("provider_name") or (pm.get("provider_name") if pm else "") or "").strip()
            cost_val = r.get("cost_usd")
            if cost_val is None and pm is not None and key not in explicit_cost_keys and (mid or r.get("prompt_tokens")):
                if key not in assigned_cost_keys:
                    assigned_cost_keys.add(key)
                    cost_val = float(pm.get("cost") or 0.0)
                else:
                    cost_val = 0.0
            clean_cost = float(cost_val) if cost_val is not None else None
            g_req = max(0, int(r.get("google_search_requests") or 0))
            d_req = max(0, int(r.get("ddg_search_requests") or 0))
            inserted_g_req += g_req
            inserted_d_req += d_req
            ts_val = r.get("created_at")
            row_ts = float(ts_val) if isinstance(ts_val, (int, float)) and ts_val > 0 else (fallback_ts + idx * 0.0001)
            cursor.execute(
                """
                INSERT INTO token_ledger (
                    conversation_id, provider_id, provider_name, model_id,
                    prompt_tokens, completion_tokens, cached_tokens,
                    google_search_requests, ddg_search_requests, cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conv_id,
                    pid,
                    pname,
                    mid,
                    max(0, int(r.get("prompt_tokens") or 0)),
                    max(0, int(r.get("completion_tokens") or 0)),
                    max(0, int(r.get("cached_tokens") or 0)),
                    g_req,
                    d_req,
                    clean_cost,
                    row_ts,
                ),
            )
    elif isinstance(token_stats, dict):
        for idx, pm in enumerate(token_stats.get("per_model") or []):
            if not isinstance(pm, dict):
                continue
            cursor.execute(
                """
                INSERT INTO token_ledger (
                    conversation_id, provider_id, provider_name, model_id,
                    prompt_tokens, completion_tokens, cached_tokens,
                    google_search_requests, ddg_search_requests, cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    conv_id,
                    str(pm.get("provider_id") or ""),
                    str(pm.get("provider_name") or "").strip(),
                    str(pm.get("model_id") or ""),
                    max(0, int(pm.get("prompt_tokens") or 0)),
                    max(0, int(pm.get("completion_tokens") or 0)),
                    max(0, int(pm.get("cached_tokens") or 0)),
                    float(pm.get("cost") or 0.0),
                    fallback_ts + idx * 0.0001,
                ),
            )

    if isinstance(token_stats, dict):
        gt = token_stats.get("grand_total") or {}
        if isinstance(gt, dict):
            g_delta = max(0, int(gt.get("google_search_requests") or 0) - inserted_g_req)
            d_delta = max(0, int(gt.get("ddg_search_requests") or 0) - inserted_d_req)
            if g_delta > 0 or d_delta > 0:
                cursor.execute(
                    """
                    INSERT INTO token_ledger (
                        conversation_id, provider_id, provider_name, model_id,
                        prompt_tokens, completion_tokens, cached_tokens,
                        google_search_requests, ddg_search_requests, cost_usd, created_at
                    ) VALUES (?, 'websearch', '', '', 0, 0, 0, ?, ?, NULL, ?)
                    """,
                    (conv_id, g_delta, d_delta, fallback_ts),
                )


def import_conversation_data(
    title: str,
    system_prompt: str = "",
    max_context: int = 30,
    messages: Optional[List[Dict[str, Any]]] = None,
    created_at: Optional[float] = None,
    workspace_dir: str = "",
    agent_mode: str = "manual",
    is_auto_named: Optional[bool] = None,
    updated_at: Optional[float] = None,
    token_ledger: Optional[List[Dict[str, Any]]] = None,
    token_stats: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Import a conversation from parsed JSON/Markdown data with a deduplicated title.
    Restores conversation settings, per-message metrics/traces, and exported token/cost/search ledger data.
    """
    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        unique_title = get_unique_conversation_title(title or "Imported Chat", conn=conn)
        new_conv_id = str(uuid.uuid4())
        now = time.time()
        conv_created = float(created_at) if isinstance(created_at, (int, float)) and created_at > 0 else now
        conv_updated = float(updated_at) if isinstance(updated_at, (int, float)) and updated_at > 0 else now
        clean_ws = str(workspace_dir or "").strip()
        clean_mode = _normalize_agent_mode(agent_mode)
        auto_named_val = 1 if (is_auto_named is None or is_auto_named) else 0

        cursor.execute(
            """
            INSERT INTO conversations (
                id, title, system_prompt, max_context_messages, is_imported, is_auto_named,
                workspace_dir, agent_mode, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                new_conv_id,
                unique_title,
                system_prompt or "",
                max(1, min(500, int(max_context or 30))),
                auto_named_val,
                clean_ws,
                clean_mode,
                conv_created,
                conv_updated,
            ),
        )

        if messages:
            for idx, m in enumerate(messages):
                new_msg_id = str(uuid.uuid4())
                raw_role = str(m.get("role", "user") or "user").strip().lower()
                role = raw_role if raw_role in ("user", "assistant", "system") else "user"
                raw_ts = m.get("created_at")
                msg_time = float(raw_ts) if isinstance(raw_ts, (int, float)) and raw_ts > 0 else (now + idx * 0.001)
                p_tok = max(0, int(m.get("prompt_tokens") or 0))
                c_tok = max(0, int(m.get("completion_tokens") or 0))
                cache_tok = max(0, int(m.get("cached_tokens") or 0))
                dur_ms = max(0, int(m.get("duration_ms") or 0))
                wrk_ms = max(0, int(m.get("worked_ms") or 0))
                trace_json = json.dumps(_parse_tool_trace(m.get("tool_trace")))
                cursor.execute(
                    """
                    INSERT INTO messages (
                        id, conversation_id, role, content, thinking_content,
                        provider_id, provider_name, model_id, thinking_level,
                        prompt_tokens, completion_tokens, cached_tokens, duration_ms, worked_ms, tool_trace_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_msg_id,
                        new_conv_id,
                        role,
                        m.get("content", ""),
                        m.get("thinking_content", ""),
                        m.get("provider_id", ""),
                        str(m.get("provider_name", "") or ""),
                        m.get("model_id", ""),
                        m.get("thinking_level", "none"),
                        p_tok,
                        c_tok,
                        cache_tok,
                        dur_ms,
                        wrk_ms,
                        trace_json,
                        msg_time,
                    ),
                )

        _restore_imported_ledger(cursor, new_conv_id, token_ledger, token_stats, conv_created)
        conn.commit()
    return get_conversation(new_conv_id)


def delete_conversation(conv_id: str, only_if_empty: bool = False) -> bool:
    if not conv_id:
        return False
    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        if only_if_empty:
            conv_row = cursor.execute(
                "SELECT is_imported FROM conversations WHERE id = ?",
                (conv_id,),
            ).fetchone()
            if not conv_row:
                conn.rollback()
                return False
            if conv_row["is_imported"]:
                has_msgs = cursor.execute(
                    "SELECT 1 FROM messages WHERE conversation_id = ? LIMIT 1",
                    (conv_id,),
                ).fetchone()
                if has_msgs:
                    conn.rollback()
                    return False
            else:
                has_reply = cursor.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE conversation_id = ? AND role = 'assistant'
                      AND (LENGTH(TRIM(content)) > 0 OR LENGTH(TRIM(thinking_content)) > 0)
                    LIMIT 1
                    """,
                    (conv_id,),
                ).fetchone()
                if has_reply:
                    conn.rollback()
                    return False

        cursor.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        cursor.execute("DELETE FROM token_ledger WHERE conversation_id = ?", (conv_id,))
        cursor.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        return deleted


def add_message(
    conversation_id: str,
    role: str,
    content: str,
    thinking_content: str = "",
    provider_id: str = "",
    model_id: str = "",
    thinking_level: str = "none",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    msg_id: Optional[str] = None,
    duration_ms: int = 0,
    worked_ms: int = 0,
    provider_name: str = "",
    tool_trace: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    if not conversation_id:
        return None
    if not msg_id:
        msg_id = str(uuid.uuid4())
    now = time.time()
    clean_duration_ms = max(0, int(duration_ms or 0))
    clean_worked_ms = max(0, int(worked_ms or 0))
    resolved_provider_name = (provider_name or "").strip()
    clean_trace = _parse_tool_trace(tool_trace)
    trace_json = json.dumps(clean_trace)

    try:
        with open_db() as conn:
            cursor = conn.cursor()
            if not cursor.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
                _logger.debug("Skipped add_message; conversation %s no longer exists", conversation_id)
                return None

            if not resolved_provider_name and provider_id:
                prov_row = cursor.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
                if prov_row and prov_row["name"]:
                    resolved_provider_name = prov_row["name"]

            cursor.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, role, content, thinking_content,
                    provider_id, provider_name, model_id, thinking_level,
                    prompt_tokens, completion_tokens, cached_tokens, duration_ms, worked_ms, tool_trace_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    conversation_id,
                    role,
                    content,
                    thinking_content,
                    provider_id,
                    resolved_provider_name,
                    model_id,
                    thinking_level,
                    prompt_tokens,
                    completion_tokens,
                    cached_tokens,
                    clean_duration_ms,
                    clean_worked_ms,
                    trace_json,
                    now,
                ),
            )

            cursor.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))

            if prompt_tokens > 0 or completion_tokens > 0 or cached_tokens > 0:
                cursor.execute(
                    """
                    INSERT INTO token_ledger (
                        conversation_id, provider_id, provider_name, model_id,
                        prompt_tokens, completion_tokens, cached_tokens, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        provider_id or "",
                        resolved_provider_name,
                        model_id or "",
                        prompt_tokens,
                        completion_tokens,
                        cached_tokens,
                        now,
                    ),
                )

            conn.commit()
    except sqlite3.IntegrityError:
        _logger.debug("Skipped add_message due to foreign key delete race on conversation %s", conversation_id)
        return None

    return {
        "id": msg_id,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "thinking_content": thinking_content,
        "provider_id": provider_id,
        "provider_name": resolved_provider_name,
        "model_id": model_id,
        "thinking_level": thinking_level,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "duration_ms": clean_duration_ms,
        "worked_ms": clean_worked_ms,
        "tool_trace": clean_trace,
        "created_at": now,
    }


def update_message(msg_id: str, **kwargs) -> None:
    if not kwargs:
        return
    invalid_keys = [k for k in kwargs if k not in _ALLOWED_MSG_COLUMNS]
    if invalid_keys:
        raise ValueError(f"Unsupported message update column(s): {', '.join(invalid_keys)}")

    fields = [f"{k} = ?" for k in kwargs]
    values = list(kwargs.values())
    values.append(msg_id)
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE messages SET {', '.join(fields)} WHERE id = ?", tuple(values))
        conn.commit()


def delete_messages_from(conversation_id: str, from_msg_id: str, include_self: bool = True) -> None:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT rowid, created_at FROM messages WHERE id = ? AND conversation_id = ?",
            (from_msg_id, conversation_id),
        )
        target = cursor.fetchone()
        if not target and from_msg_id and from_msg_id.startswith("user_temp_"):
            cursor.execute(
                "SELECT rowid, id, created_at FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY rowid DESC LIMIT 1",
                (conversation_id,),
            )
            target = cursor.fetchone()

        if target:
            target_rowid = target["rowid"]
            target_time = target["created_at"]
            op = ">=" if include_self else ">"
            cursor.execute(
                f"DELETE FROM messages WHERE conversation_id = ? AND (created_at > ? OR (created_at = ? AND rowid {op} ?))",
                (conversation_id, target_time, target_time, target_rowid),
            )
            cursor.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (time.time(), conversation_id),
            )
            conn.commit()
