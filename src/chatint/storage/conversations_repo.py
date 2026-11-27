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
from chatint.storage.connection import (
    get_conv_db_path,
    get_conv_db_size_bytes,
    get_conv_lock,
    get_total_conv_dbs_size_bytes,
    init_conv_db,
    open_conv_db,
    open_db,
    sync_conv_meta_cursor,
    unlink_conv_db_files,
    validate_conv_id,
)
from chatint.storage.ledger_repo import get_token_stats
from chatint.storage.settings_repo import get_app_settings, mb_setting_to_bytes, normalize_storage_limit_mb

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


def _conv_meta_from_row(conv_row: Any) -> Dict[str, Any]:
    keys = set(conv_row.keys())
    return {
        "id": conv_row["id"],
        "title": conv_row["title"],
        "system_prompt": conv_row["system_prompt"] if "system_prompt" in keys else "",
        "max_context_messages": conv_row["max_context_messages"] if "max_context_messages" in keys else 30,
        "workspace_dir": (conv_row["workspace_dir"] or "") if "workspace_dir" in keys else "",
        "agent_mode": _normalize_agent_mode(conv_row["agent_mode"] if "agent_mode" in keys else "manual"),
        "is_imported": bool(conv_row["is_imported"]) if "is_imported" in keys else False,
        "is_auto_named": bool(conv_row["is_auto_named"]) if "is_auto_named" in keys else False,
        "created_at": conv_row["created_at"],
        "updated_at": conv_row["updated_at"],
    }


def _count_messages_in_cursor(cursor: sqlite3.Cursor, conv_id: str) -> tuple[int, int]:
    row = cursor.execute(
        """
        SELECT COUNT(*) as total_cnt,
               COALESCE(SUM(CASE WHEN role = 'assistant' AND (LENGTH(TRIM(content)) > 0 OR LENGTH(TRIM(thinking_content)) > 0) THEN 1 ELSE 0 END), 0) as asst_cnt
        FROM messages
        WHERE conversation_id = ?
        """,
        (conv_id,),
    ).fetchone()
    if not row:
        return 0, 0
    return int(row["total_cnt"] or 0), int(row["asst_cnt"] or 0)


def _sync_conv_index(
    conv_id: str,
    msg_count: int,
    asst_reply_count: int,
    updated_at: Optional[float] = None,
) -> int:
    size_bytes = get_conv_db_size_bytes(conv_id)
    with open_db() as conn:
        if updated_at is not None:
            conn.execute(
                """
                UPDATE conversations
                SET message_count = ?, assistant_reply_count = ?, db_size_bytes = ?, updated_at = ?
                WHERE id = ?
                """,
                (msg_count, asst_reply_count, size_bytes, updated_at, conv_id),
            )
        else:
            conn.execute(
                """
                UPDATE conversations
                SET message_count = ?, assistant_reply_count = ?, db_size_bytes = ?
                WHERE id = ?
                """,
                (msg_count, asst_reply_count, size_bytes, conv_id),
            )
        conn.commit()
    return size_bytes


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
    conv_id = str(uuid.uuid4())
    now = time.time()

    with get_conv_lock(conv_id):
        with open_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if is_unnamed_placeholder:
                # Keep default placeholder as "New Chat" with is_auto_named=0 so auto-naming runs cleanly on Turn 1
                resolved_title = "New Chat"
                is_auto_named = 0
            else:
                resolved_title = get_unique_conversation_title(raw_title, conn=conn)
                is_auto_named = 1

            meta = {
                "id": conv_id,
                "title": resolved_title,
                "system_prompt": system_prompt,
                "max_context_messages": max_context,
                "workspace_dir": clean_ws,
                "agent_mode": clean_mode,
                "is_imported": 0,
                "is_auto_named": is_auto_named,
                "created_at": now,
                "updated_at": now,
            }
            try:
                init_conv_db(conv_id, meta=meta)
                size_bytes = get_conv_db_size_bytes(conv_id)
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO conversations (
                        id, title, system_prompt, max_context_messages, is_imported, is_auto_named,
                        workspace_dir, agent_mode, message_count, assistant_reply_count, db_size_bytes,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?, 0, 0, ?, ?, ?)
                    """,
                    (
                        conv_id,
                        resolved_title,
                        system_prompt,
                        max_context,
                        is_auto_named,
                        clean_ws,
                        clean_mode,
                        size_bytes,
                        now,
                        now,
                    ),
                )
                conn.commit()
            except Exception:
                unlink_conv_db_files(conv_id)
                raise
    return {
        "id": conv_id,
        "title": resolved_title,
        "system_prompt": system_prompt,
        "max_context_messages": max_context,
        "is_imported": False,
        "is_auto_named": bool(is_auto_named),
        "workspace_dir": clean_ws,
        "agent_mode": clean_mode,
        "db_size_bytes": size_bytes,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }


def get_conversations() -> List[Dict[str, Any]]:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, system_prompt, max_context_messages, is_imported, is_auto_named,
                   workspace_dir, agent_mode, assistant_reply_count as message_count,
                   message_count as total_messages, db_size_bytes, created_at, updated_at
            FROM conversations
            WHERE assistant_reply_count > 0 OR (is_imported = 1 AND message_count > 0)
            ORDER BY updated_at DESC
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
            "db_size_bytes": int(r["db_size_bytes"] or 0),
        }
        for r in rows
    ]


def get_conversation(conv_id: str) -> Optional[Dict[str, Any]]:
    if not conv_id:
        return None
    try:
        validate_conv_id(conv_id)
    except ValueError:
        return None

    with get_conv_lock(conv_id):
        with open_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,))
            conv_row = cursor.fetchone()
            if not conv_row:
                return None
        conv_meta = _conv_meta_from_row(conv_row)

        with open_conv_db(conv_id, meta=conv_meta) as cconn:
            ccur = cconn.cursor()
            ccur.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
                (conv_id,),
            )
            msg_rows = ccur.fetchall()
            ccur.execute(
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
            ledger_rows = ccur.fetchall()

    size_bytes = get_conv_db_size_bytes(conv_id)
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
        "workspace_dir": (conv_row["workspace_dir"] or "") if "workspace_dir" in conv_keys else "",
        "agent_mode": _normalize_agent_mode(conv_row["agent_mode"] if "agent_mode" in conv_keys else "manual"),
        "db_size_bytes": size_bytes,
        "created_at": conv_row["created_at"],
        "updated_at": conv_row["updated_at"],
        "messages": messages,
        "token_ledger": token_ledger,
        "token_stats": get_token_stats(conv_id),
    }


def update_conversation(conv_id: str, only_if_default: bool = False, **kwargs) -> Optional[str]:
    if not conv_id:
        return None
    try:
        validate_conv_id(conv_id)
    except ValueError:
        return None
    invalid_keys = [k for k in kwargs if k not in _ALLOWED_CONV_COLUMNS]
    if invalid_keys:
        raise ValueError(f"Unsupported conversation update column(s): {', '.join(invalid_keys)}")

    with get_conv_lock(conv_id):
        updated_meta: Optional[Dict[str, Any]] = None
        with open_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            row = cursor.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if not row:
                conn.rollback()
                return None
            if only_if_default and (bool(row["is_auto_named"]) or not is_default_conversation_title(row["title"])):
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

            now = time.time()
            fields = [f"{k} = ?" for k in kwargs]
            values = list(kwargs.values())
            values.append(now)
            values.append(conv_id)
            set_clause = f"{', '.join(fields)}, updated_at = ?" if fields else "updated_at = ?"
            cursor.execute(f"UPDATE conversations SET {set_clause} WHERE id = ?", tuple(values))
            fresh_row = cursor.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if fresh_row:
                updated_meta = _conv_meta_from_row(fresh_row)
            conn.commit()

        if updated_meta and get_conv_db_path(conv_id).is_file():
            with open_conv_db(conv_id) as cconn:
                sync_conv_meta_cursor(cconn.cursor(), updated_meta)
                cconn.commit()
    return kwargs.get("title")


def duplicate_conversation(conv_id: str) -> Optional[Dict[str, Any]]:
    """
    Create an exact duplicate/branch of the conversation under a unique branch title.
    Token usage and cost in token_ledger start at 0 on the branch.
    """
    conv = get_conversation(conv_id)
    if not conv:
        return None

    new_conv_id = str(uuid.uuid4())
    now = time.time()

    with get_conv_lock(new_conv_id):
        with open_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            branch_title = get_branch_title(conv["title"], conn=conn)
            clean_ws = str(conv.get("workspace_dir", "") or "").strip()
            clean_mode = _normalize_agent_mode(conv.get("agent_mode", "manual"))
            is_imp = 1 if conv.get("is_imported") else 0
            meta = {
                "id": new_conv_id,
                "title": branch_title,
                "system_prompt": conv.get("system_prompt", ""),
                "max_context_messages": conv.get("max_context_messages", 30),
                "workspace_dir": clean_ws,
                "agent_mode": clean_mode,
                "is_imported": is_imp,
                "is_auto_named": 1,
                "created_at": now,
                "updated_at": now,
            }
            try:
                init_conv_db(new_conv_id, meta=meta)
                with open_conv_db(new_conv_id, meta=meta) as cconn:
                    ccur = cconn.cursor()
                    for idx, m in enumerate(conv.get("messages", [])):
                        new_msg_id = str(uuid.uuid4())
                        msg_time = now + (idx * 0.001)
                        trace_json = json.dumps(_parse_tool_trace(m.get("tool_trace")))
                        ccur.execute(
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
                    msg_cnt, asst_cnt = _count_messages_in_cursor(ccur, new_conv_id)
                    cconn.commit()

                size_bytes = get_conv_db_size_bytes(new_conv_id)
                cursor.execute(
                    """
                    INSERT INTO conversations (
                        id, title, system_prompt, max_context_messages, is_imported, is_auto_named,
                        workspace_dir, agent_mode, message_count, assistant_reply_count, db_size_bytes,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_conv_id,
                        branch_title,
                        conv.get("system_prompt", ""),
                        conv.get("max_context_messages", 30),
                        is_imp,
                        clean_ws,
                        clean_mode,
                        msg_cnt,
                        asst_cnt,
                        size_bytes,
                        now,
                        now,
                    ),
                )
                conn.commit()
            except Exception:
                unlink_conv_db_files(new_conv_id)
                raise
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
    new_conv_id = str(uuid.uuid4())
    now = time.time()
    conv_created = float(created_at) if isinstance(created_at, (int, float)) and created_at > 0 else now
    conv_updated = float(updated_at) if isinstance(updated_at, (int, float)) and updated_at > 0 else now
    clean_ws = str(workspace_dir or "").strip()
    clean_mode = _normalize_agent_mode(agent_mode)
    clamped_ctx = max(1, min(500, int(max_context or 30)))
    auto_named_val = 1 if (is_auto_named is None or is_auto_named) else 0

    with get_conv_lock(new_conv_id):
        with open_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            unique_title = get_unique_conversation_title(title or "Imported Chat", conn=conn)
            meta = {
                "id": new_conv_id,
                "title": unique_title,
                "system_prompt": system_prompt or "",
                "max_context_messages": clamped_ctx,
                "workspace_dir": clean_ws,
                "agent_mode": clean_mode,
                "is_imported": 1,
                "is_auto_named": auto_named_val,
                "created_at": conv_created,
                "updated_at": conv_updated,
            }
            try:
                init_conv_db(new_conv_id, meta=meta)
                with open_conv_db(new_conv_id, meta=meta) as cconn:
                    ccur = cconn.cursor()
                    if messages:
                        for idx, m in enumerate(messages):
                            new_msg_id = str(uuid.uuid4())
                            raw_role = str(m.get("role", "user") or "user").strip().lower()
                            role = raw_role if raw_role in ("user", "assistant", "system") else "user"
                            raw_ts = m.get("created_at")
                            msg_time = (
                                float(raw_ts)
                                if isinstance(raw_ts, (int, float)) and raw_ts > 0
                                else (now + idx * 0.001)
                            )
                            p_tok = max(0, int(m.get("prompt_tokens") or 0))
                            c_tok = max(0, int(m.get("completion_tokens") or 0))
                            cache_tok = max(0, int(m.get("cached_tokens") or 0))
                            dur_ms = max(0, int(m.get("duration_ms") or 0))
                            wrk_ms = max(0, int(m.get("worked_ms") or 0))
                            trace_json = json.dumps(_parse_tool_trace(m.get("tool_trace")))
                            ccur.execute(
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

                    _restore_imported_ledger(ccur, new_conv_id, token_ledger, token_stats, conv_created)
                    msg_cnt, asst_cnt = _count_messages_in_cursor(ccur, new_conv_id)
                    cconn.commit()

                size_bytes = get_conv_db_size_bytes(new_conv_id)
                cursor.execute(
                    """
                    INSERT INTO conversations (
                        id, title, system_prompt, max_context_messages, is_imported, is_auto_named,
                        workspace_dir, agent_mode, message_count, assistant_reply_count, db_size_bytes,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_conv_id,
                        unique_title,
                        system_prompt or "",
                        clamped_ctx,
                        auto_named_val,
                        clean_ws,
                        clean_mode,
                        msg_cnt,
                        asst_cnt,
                        size_bytes,
                        conv_created,
                        conv_updated,
                    ),
                )
                conn.commit()
            except Exception:
                unlink_conv_db_files(new_conv_id)
                raise
    return get_conversation(new_conv_id)


def delete_conversation(conv_id: str, only_if_empty: bool = False) -> bool:
    if not conv_id:
        return False
    try:
        validate_conv_id(conv_id)
    except ValueError:
        return False

    with get_conv_lock(conv_id):
        with open_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            if only_if_empty:
                conv_row = cursor.execute(
                    "SELECT is_imported, message_count, assistant_reply_count FROM conversations WHERE id = ?",
                    (conv_id,),
                ).fetchone()
                if not conv_row:
                    conn.rollback()
                    return False
                if conv_row["is_imported"]:
                    if int(conv_row["message_count"] or 0) > 0:
                        conn.rollback()
                        return False
                else:
                    if int(conv_row["assistant_reply_count"] or 0) > 0:
                        conn.rollback()
                        return False

            cursor.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            deleted = cursor.rowcount > 0
            conn.commit()

        if deleted:
            unlink_conv_db_files(conv_id)
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
    try:
        validate_conv_id(conversation_id)
    except ValueError:
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
        with get_conv_lock(conversation_id):
            with open_db() as conn:
                conv_row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
                if not conv_row:
                    _logger.debug("Skipped add_message; conversation %s no longer exists", conversation_id)
                    return None
                conv_meta = _conv_meta_from_row(conv_row)
                if not resolved_provider_name and provider_id:
                    prov_row = conn.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
                    if prov_row and prov_row["name"]:
                        resolved_provider_name = prov_row["name"]

            with open_conv_db(conversation_id, meta=conv_meta) as cconn:
                ccur = cconn.cursor()
                ccur.execute(
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

                if prompt_tokens > 0 or completion_tokens > 0 or cached_tokens > 0:
                    ccur.execute(
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
                msg_cnt, asst_cnt = _count_messages_in_cursor(ccur, conversation_id)
                cconn.commit()

            _sync_conv_index(conversation_id, msg_cnt, asst_cnt, updated_at=now)
    except sqlite3.Error:
        _logger.debug("Skipped add_message due to DB error or delete race on conversation %s", conversation_id)
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


def update_message(msg_id: str, *, conversation_id: Optional[str] = None, **kwargs) -> None:
    if not kwargs:
        return
    invalid_keys = [k for k in kwargs if k not in _ALLOWED_MSG_COLUMNS]
    if invalid_keys:
        raise ValueError(f"Unsupported message update column(s): {', '.join(invalid_keys)}")
    if not msg_id:
        return

    fields = [f"{k} = ?" for k in kwargs]
    values = list(kwargs.values())
    values.append(msg_id)

    if conversation_id:
        try:
            candidate_ids = [validate_conv_id(conversation_id)]
        except ValueError:
            return
    else:
        with open_db() as conn:
            candidate_ids = [r["id"] for r in conn.execute("SELECT id FROM conversations").fetchall()]

    for cid in candidate_ids:
        with get_conv_lock(cid):
            conv_db = get_conv_db_path(cid)
            if not conv_db.is_file():
                continue
            updated_counts: Optional[tuple[int, int]] = None
            with open_conv_db(cid) as cconn:
                ccur = cconn.cursor()
                ccur.execute(f"UPDATE messages SET {', '.join(fields)} WHERE id = ?", tuple(values))
                if ccur.rowcount > 0:
                    updated_counts = _count_messages_in_cursor(ccur, cid)
                    cconn.commit()
            if updated_counts is not None:
                _sync_conv_index(cid, updated_counts[0], updated_counts[1])
                break


def delete_messages_from(conversation_id: str, from_msg_id: str, include_self: bool = True) -> None:
    if not conversation_id or not from_msg_id:
        return
    try:
        validate_conv_id(conversation_id)
    except ValueError:
        return

    with get_conv_lock(conversation_id):
        with open_db() as conn:
            conv_row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not conv_row:
                return
            conv_meta = _conv_meta_from_row(conv_row)

        deleted_count = 0
        msg_cnt = int(conv_row["message_count"] or 0)
        asst_cnt = int(conv_row["assistant_reply_count"] or 0)
        with open_conv_db(conversation_id, meta=conv_meta) as cconn:
            ccur = cconn.cursor()
            ccur.execute(
                "SELECT rowid, created_at FROM messages WHERE id = ? AND conversation_id = ?",
                (from_msg_id, conversation_id),
            )
            target = ccur.fetchone()
            if not target and from_msg_id.startswith("user_temp_"):
                ccur.execute(
                    "SELECT rowid, id, created_at FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY rowid DESC LIMIT 1",
                    (conversation_id,),
                )
                target = ccur.fetchone()

            if target:
                target_rowid = target["rowid"]
                target_time = target["created_at"]
                op = ">=" if include_self else ">"
                ccur.execute(
                    f"DELETE FROM messages WHERE conversation_id = ? AND (created_at > ? OR (created_at = ? AND rowid {op} ?))",
                    (conversation_id, target_time, target_time, target_rowid),
                )
                deleted_count = ccur.rowcount
                msg_cnt, asst_cnt = _count_messages_in_cursor(ccur, conversation_id)
                cconn.commit()
                if deleted_count > 0:
                    try:
                        cconn.execute("PRAGMA incremental_vacuum;")
                        cconn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    except sqlite3.Error:
                        pass

        if deleted_count > 0:
            _sync_conv_index(conversation_id, msg_cnt, asst_cnt, updated_at=time.time())


def resolve_message_id(conv_id: str, msg_id: str) -> str:
    """Resolve explicit message ID or latest user message for optimistic `user_temp_*` placeholders."""
    if not conv_id:
        return ""
    try:
        validate_conv_id(conv_id)
    except ValueError:
        return ""
    if msg_id and not msg_id.startswith("user_temp_"):
        return msg_id

    with get_conv_lock(conv_id):
        with open_db() as conn:
            if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone():
                return ""
        if not get_conv_db_path(conv_id).is_file():
            return ""
        with open_conv_db(conv_id) as cconn:
            row = cconn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY rowid DESC LIMIT 1",
                (conv_id,),
            ).fetchone()
            return str(row["id"]) if row else ""


def message_exists(conv_id: str, msg_id: str) -> bool:
    """Return True when `msg_id` belongs to `conv_id`."""
    if not conv_id or not msg_id:
        return False
    try:
        validate_conv_id(conv_id)
    except ValueError:
        return False

    with get_conv_lock(conv_id):
        with open_db() as conn:
            if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone():
                return False
        if not get_conv_db_path(conv_id).is_file():
            return False
        with open_conv_db(conv_id) as cconn:
            row = cconn.execute(
                "SELECT 1 FROM messages WHERE id = ? AND conversation_id = ?",
                (msg_id, conv_id),
            ).fetchone()
            return bool(row)


def _format_mb_label(limit_bytes: int) -> str:
    mb_val = limit_bytes / 1_048_576
    if mb_val >= 1024 and abs(mb_val - round(mb_val)) < 1e-6 and int(round(mb_val)) % 1024 == 0:
        return f"{int(round(mb_val)) // 1024} GB"
    if abs(mb_val - round(mb_val)) < 1e-6:
        return f"{int(round(mb_val))} MB"
    return f"{mb_val:.2f}".rstrip("0").rstrip(".") + " MB"


def check_storage_quota(conv_id: Optional[str] = None, additional_bytes: int = 0) -> Optional[str]:
    """Return an actionable quota error if configured per-chat or total byte limits are exceeded."""
    settings = get_app_settings()
    max_chat_bytes = mb_setting_to_bytes(settings.get("max_chat_size_mb", "50"))
    max_total_bytes = mb_setting_to_bytes(settings.get("max_total_chats_size_mb", "2048"))
    extra = max(0, int(additional_bytes or 0))

    if conv_id and max_chat_bytes > 0:
        cur_chat_bytes = get_conv_db_size_bytes(conv_id)
        if cur_chat_bytes >= max_chat_bytes or (extra > 0 and cur_chat_bytes + extra > max_chat_bytes):
            return (
                f"This chat has reached its {_format_mb_label(max_chat_bytes)} storage limit "
                f"({cur_chat_bytes / 1_048_576:.1f} MB used). Start a new chat or raise the limit in Settings -> General."
            )

    if max_total_bytes > 0:
        total_bytes = get_total_conv_dbs_size_bytes()
        if total_bytes >= max_total_bytes or (extra > 0 and total_bytes + extra > max_total_bytes):
            return (
                f"Total conversation storage has reached its {_format_mb_label(max_total_bytes)} limit "
                f"({total_bytes / 1_048_576:.1f} MB used). Delete unused chats or raise the limit in Settings -> General."
            )
    return None


def get_storage_summary() -> Dict[str, Any]:
    """Return live storage usage and sorted conversation breakdown for Settings & quota UI."""
    settings = get_app_settings()
    max_chat_str = normalize_storage_limit_mb(settings.get("max_chat_size_mb", "50"))
    max_total_str = normalize_storage_limit_mb(settings.get("max_total_chats_size_mb", "2048"))

    with open_db() as conn:
        rows = conn.execute("""
            SELECT id, title, db_size_bytes, message_count, assistant_reply_count, is_imported, updated_at
            FROM conversations
            WHERE assistant_reply_count > 0 OR (is_imported = 1 AND message_count > 0)
            ORDER BY db_size_bytes DESC, updated_at DESC
        """).fetchall()

    convs = [
        {
            "id": r["id"],
            "title": r["title"],
            "db_size_bytes": int(r["db_size_bytes"] or 0),
            "message_count": int(r["assistant_reply_count"] if not r["is_imported"] else r["message_count"] or 0),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]
    total_bytes = get_total_conv_dbs_size_bytes()
    return {
        "total_bytes": total_bytes,
        "chat_count": len(convs),
        "max_chat_size_mb": max_chat_str,
        "max_total_chats_size_mb": max_total_str,
        "max_chat_bytes": mb_setting_to_bytes(max_chat_str),
        "max_total_chats_bytes": mb_setting_to_bytes(max_total_str),
        "conversations": convs,
    }
