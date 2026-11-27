"""SQLite connection management, sequential schema migration runner, and .bak backup."""
from __future__ import annotations

import contextlib
import gc
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from chatint.catalog import PRIMARY_WEB_SEARCH_MODEL
from chatint.config import DB_PATH as _DEFAULT_DB_PATH, DEFAULT_PROVIDERS
from chatint.logging_setup import get_logger
from chatint.storage.secrets import (
    batch_update_provider_env_keys,
    ensure_env_file,
    provider_env_key,
    read_env_dict,
    sanitize_provider_name,
)

_logger = get_logger("chatint.storage.connection")
SCHEMA_VERSION = 6
CONV_SCHEMA_VERSION = 1
_VALID_CONV_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_conv_locks_guard = threading.Lock()
_conv_locks: Dict[str, threading.RLock] = {}


def _get_db_path() -> Path:
    db_mod = sys.modules.get("chatint.db")
    if db_mod is not None and hasattr(db_mod, "DB_PATH"):
        return db_mod.DB_PATH
    return _DEFAULT_DB_PATH


def validate_conv_id(conv_id: str) -> str:
    """Validate and return a safe conversation ID for filesystem DB paths."""
    clean = str(conv_id or "").strip()
    if not _VALID_CONV_ID_RE.match(clean):
        raise ValueError(f"Invalid conversation id: {conv_id!r}")
    return clean


def get_conversations_dir(db_path: Optional[Path] = None) -> Path:
    """Return the per-conversation SQLite directory adjacent to chatint.db."""
    base_db = db_path if db_path is not None else _get_db_path()
    return base_db.parent / "conversations"


def get_conv_db_path(conv_id: str, db_path: Optional[Path] = None) -> Path:
    """Return the SQLite file path for a single conversation."""
    clean = validate_conv_id(conv_id)
    return get_conversations_dir(db_path) / f"{clean}.db"


def get_conv_lock(conv_id: str) -> threading.RLock:
    """Return a per-conversation reentrant lock without blocking sibling chats."""
    clean = validate_conv_id(conv_id)
    with _conv_locks_guard:
        lock = _conv_locks.get(clean)
        if lock is None:
            lock = threading.RLock()
            _conv_locks[clean] = lock
        return lock


def _is_live_conv_file(name: str) -> bool:
    low = name.lower()
    if ".corrupted" in low or ".pre_v6_conflict" in low or low.endswith(".bak"):
        return False
    return low.endswith((".db", ".db-wal", ".db-shm"))


def get_conv_db_size_bytes(conv_id: str, db_path: Optional[Path] = None) -> int:
    """Return real disk size in bytes for <conv_id>.db (+ active WAL/SHM if present)."""
    try:
        target = db_path if db_path is not None else get_conv_db_path(conv_id)
    except ValueError:
        return 0
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = target if not suffix else target.with_name(f"{target.name}{suffix}")
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def get_total_conv_dbs_size_bytes(db_path: Optional[Path] = None) -> int:
    """Sum disk bytes across all live per-conversation SQLite DB files."""
    convs_dir = get_conversations_dir(db_path)
    if not convs_dir.is_dir():
        return 0
    total = 0
    try:
        with os.scandir(convs_dir) as it:
            for entry in it:
                try:
                    if entry.is_file() and _is_live_conv_file(entry.name):
                        total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def unlink_conv_db_files(conv_id: str, db_path: Optional[Path] = None) -> None:
    """Delete <conv_id>.db and all sidecar files with brief retries for Windows handle release."""
    try:
        clean = validate_conv_id(conv_id)
    except ValueError:
        return
    convs_dir = get_conversations_dir(db_path)
    if not convs_dir.is_dir():
        return
    prefixes = (f"{clean}.db-", f"{clean}.db.")
    targets = [
        p
        for p in convs_dir.iterdir()
        if p.is_file() and (p.name == f"{clean}.db" or p.name.startswith(prefixes))
    ]
    for p in targets:
        for attempt in range(6):
            try:
                p.unlink(missing_ok=True)
                break
            except PermissionError:
                if attempt == 0:
                    gc.collect()
                if attempt == 5:
                    _logger.warning("Could not delete %s after retries (file still in use)", p)
                else:
                    time.sleep(0.05)
            except OSError as exc:
                _logger.warning("Failed to unlink %s: %s", p, exc)
                break


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_get_db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


@contextlib.contextmanager
def open_db():
    conn = get_connection()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _migration_v1(cursor: sqlite3.Cursor) -> None:
    """Base schema + composite message index + historical token_ledger backfill."""
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key TEXT NOT NULL DEFAULT '',
        is_enabled INTEGER NOT NULL DEFAULT 1,
        models_json TEXT NOT NULL DEFAULT '[]'
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT 'New Chat',
        system_prompt TEXT NOT NULL DEFAULT '',
        max_context_messages INTEGER NOT NULL DEFAULT 30,
        is_imported INTEGER NOT NULL DEFAULT 0,
        is_auto_named INTEGER NOT NULL DEFAULT 0,
        workspace_dir TEXT NOT NULL DEFAULT '',
        agent_mode TEXT NOT NULL DEFAULT 'manual',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """)

    conv_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(conversations)").fetchall()}
    if "is_imported" not in conv_cols:
        cursor.execute("ALTER TABLE conversations ADD COLUMN is_imported INTEGER NOT NULL DEFAULT 0")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        thinking_content TEXT NOT NULL DEFAULT '',
        provider_id TEXT NOT NULL DEFAULT '',
        provider_name TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        thinking_level TEXT NOT NULL DEFAULT 'none',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        worked_ms INTEGER NOT NULL DEFAULT 0,
        tool_trace_json TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL,
        FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tab_sessions (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        state_json TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS token_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        provider_id TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        google_search_requests INTEGER NOT NULL DEFAULT 0,
        ddg_search_requests INTEGER NOT NULL DEFAULT 0,
        provider_name TEXT NOT NULL DEFAULT '',
        cost_usd REAL DEFAULT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
    )
    """)

    ledger_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(token_ledger)").fetchall()}
    if "google_search_requests" not in ledger_cols:
        cursor.execute("ALTER TABLE token_ledger ADD COLUMN google_search_requests INTEGER NOT NULL DEFAULT 0")
    if "ddg_search_requests" not in ledger_cols:
        cursor.execute("ALTER TABLE token_ledger ADD COLUMN ddg_search_requests INTEGER NOT NULL DEFAULT 0")
    if "provider_name" not in ledger_cols:
        cursor.execute("ALTER TABLE token_ledger ADD COLUMN provider_name TEXT NOT NULL DEFAULT ''")
    if "cost_usd" not in ledger_cols:
        cursor.execute("ALTER TABLE token_ledger ADD COLUMN cost_usd REAL DEFAULT NULL")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS google_search_usage (
        month_key TEXT PRIMARY KEY,
        request_count INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )
    """)

    t_count = cursor.execute("SELECT COUNT(*) as count FROM token_ledger").fetchone()
    if t_count and t_count["count"] == 0:
        cursor.execute("""
            INSERT INTO token_ledger (
                conversation_id, provider_id, model_id,
                prompt_tokens, completion_tokens, cached_tokens, created_at
            )
            SELECT conversation_id, provider_id, model_id,
                   prompt_tokens, completion_tokens, cached_tokens, created_at
            FROM messages
            WHERE role = 'assistant' AND (prompt_tokens > 0 OR completion_tokens > 0)
        """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_conv_role_created ON messages(conversation_id, role, created_at)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_token_ledger_conv ON token_ledger(conversation_id)")


def _migration_v2(cursor: sqlite3.Cursor) -> None:
    """Add conversations.is_auto_named flag so default placeholders ('New Chat', 'New Chat (N)') auto-name reliably."""
    conv_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(conversations)").fetchall()}
    if "is_auto_named" not in conv_cols:
        cursor.execute("ALTER TABLE conversations ADD COLUMN is_auto_named INTEGER NOT NULL DEFAULT 0")

    # Backfill existing named/imported conversations so already-titled chats never re-trigger auto-naming
    rows = cursor.execute("SELECT id, title, is_imported FROM conversations").fetchall()
    for r in rows:
        raw_t = (r["title"] or "").strip().lower()
        is_default_placeholder = raw_t in ("", "new chat") or (
            raw_t.startswith("new chat (") and raw_t.endswith(")") and raw_t[10:-1].strip().isdigit()
        )
        if r["is_imported"] or not is_default_placeholder:
            cursor.execute("UPDATE conversations SET is_auto_named = 1 WHERE id = ?", (r["id"],))


def _migration_v3(cursor: sqlite3.Cursor) -> None:
    """Add messages.duration_ms and messages.provider_name columns for tok/s and custom provider labels."""
    msg_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(messages)").fetchall()}
    if "duration_ms" not in msg_cols:
        cursor.execute("ALTER TABLE messages ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0")
    if "provider_name" not in msg_cols:
        cursor.execute("ALTER TABLE messages ADD COLUMN provider_name TEXT NOT NULL DEFAULT ''")


def _migration_v4(cursor: sqlite3.Cursor) -> None:
    """Add workspace_dir, agent_mode on conversations and tool_trace_json on messages."""
    conv_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(conversations)").fetchall()}
    if "workspace_dir" not in conv_cols:
        cursor.execute("ALTER TABLE conversations ADD COLUMN workspace_dir TEXT NOT NULL DEFAULT ''")
    if "agent_mode" not in conv_cols:
        cursor.execute("ALTER TABLE conversations ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'manual'")

    msg_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(messages)").fetchall()}
    if "tool_trace_json" not in msg_cols:
        cursor.execute("ALTER TABLE messages ADD COLUMN tool_trace_json TEXT NOT NULL DEFAULT '[]'")


def _migration_v5(cursor: sqlite3.Cursor) -> None:
    """Add messages.worked_ms and token_ledger ddg_search_requests, provider_name, and cost_usd columns."""
    msg_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(messages)").fetchall()}
    if "worked_ms" not in msg_cols:
        cursor.execute("ALTER TABLE messages ADD COLUMN worked_ms INTEGER NOT NULL DEFAULT 0")

    ledger_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(token_ledger)").fetchall()}
    if "ddg_search_requests" not in ledger_cols:
        cursor.execute("ALTER TABLE token_ledger ADD COLUMN ddg_search_requests INTEGER NOT NULL DEFAULT 0")
    if "provider_name" not in ledger_cols:
        cursor.execute("ALTER TABLE token_ledger ADD COLUMN provider_name TEXT NOT NULL DEFAULT ''")
    if "cost_usd" not in ledger_cols:
        cursor.execute("ALTER TABLE token_ledger ADD COLUMN cost_usd REAL DEFAULT NULL")


_REQUIRED_CONV_TABLES = {"conv_meta", "messages", "token_ledger"}


def _create_global_tables_v6(cursor: sqlite3.Cursor) -> None:
    """Ensure global core tables exist on an already-v6 database without legacy message/ledger tables."""
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key TEXT NOT NULL DEFAULT '',
        is_enabled INTEGER NOT NULL DEFAULT 1,
        models_json TEXT NOT NULL DEFAULT '[]'
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT 'New Chat',
        system_prompt TEXT NOT NULL DEFAULT '',
        max_context_messages INTEGER NOT NULL DEFAULT 30,
        is_imported INTEGER NOT NULL DEFAULT 0,
        is_auto_named INTEGER NOT NULL DEFAULT 0,
        workspace_dir TEXT NOT NULL DEFAULT '',
        agent_mode TEXT NOT NULL DEFAULT 'manual',
        message_count INTEGER NOT NULL DEFAULT 0,
        assistant_reply_count INTEGER NOT NULL DEFAULT 0,
        db_size_bytes INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tab_sessions (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        state_json TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS google_search_usage (
        month_key TEXT PRIMARY KEY,
        request_count INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )
    """)


def _create_conv_schema(cursor: sqlite3.Cursor) -> None:
    """Create schema tables for an isolated per-conversation SQLite database."""
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS conv_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        thinking_content TEXT NOT NULL DEFAULT '',
        provider_id TEXT NOT NULL DEFAULT '',
        provider_name TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        thinking_level TEXT NOT NULL DEFAULT 'none',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        worked_ms INTEGER NOT NULL DEFAULT 0,
        tool_trace_json TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL
    )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_conv_messages_created ON messages(conversation_id, created_at ASC)"
    )
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS token_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        provider_id TEXT NOT NULL DEFAULT '',
        provider_name TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        google_search_requests INTEGER NOT NULL DEFAULT 0,
        ddg_search_requests INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL DEFAULT NULL,
        created_at REAL NOT NULL
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_conv_ledger_conv ON token_ledger(conversation_id)")


def sync_conv_meta_cursor(cursor: sqlite3.Cursor, meta: Dict[str, Any]) -> None:
    """Write or update self-describing metadata keys inside a conversation DB."""
    if not meta:
        return
    pairs = []
    for k in (
        "id",
        "title",
        "system_prompt",
        "max_context_messages",
        "workspace_dir",
        "agent_mode",
        "is_imported",
        "is_auto_named",
        "created_at",
        "updated_at",
    ):
        if k not in meta or meta[k] is None:
            continue
        val = meta[k]
        if k in ("is_imported", "is_auto_named"):
            sval = "1" if bool(int(val) if isinstance(val, (int, bool)) else str(val).lower() in ("1", "true")) else "0"
        else:
            sval = str(val)
        pairs.append((k, sval))
    if pairs:
        cursor.executemany(
            """
            INSERT INTO conv_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            pairs,
        )


def _connect_conv_sqlite(conv_db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(conv_db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _run_conv_db_init(conv_id: str, meta: Optional[Dict[str, Any]] = None, db_path: Optional[Path] = None) -> Path:
    conv_db_path = get_conv_db_path(conv_id, db_path=db_path)
    conv_db_path.parent.mkdir(parents=True, exist_ok=True)
    pre_existed = conv_db_path.exists() and conv_db_path.stat().st_size > 0
    conn = _connect_conv_sqlite(conv_db_path)
    try:
        cursor = conn.cursor()
        if not pre_existed:
            # Must run before creating tables so incremental auto_vacuum takes effect permanently
            cursor.execute("PRAGMA auto_vacuum = INCREMENTAL;")
        try:
            cursor.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.Error as exc:
            _logger.warning("Could not enable WAL mode on %s: %s", conv_db_path, exc)

        if pre_existed:
            check_row = cursor.execute("PRAGMA quick_check(1);").fetchone()
            if not check_row or check_row[0] != "ok":
                raise sqlite3.DatabaseError(f"Conversation DB failed quick_check: {check_row[0] if check_row else 'empty'}")

        cur_ver_row = cursor.execute("PRAGMA user_version").fetchone()
        cur_ver = int(cur_ver_row[0]) if cur_ver_row else 0
        existing_tables = {
            r["name"] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        has_all = _REQUIRED_CONV_TABLES.issubset(existing_tables)
        if cur_ver < CONV_SCHEMA_VERSION or not has_all:
            _create_conv_schema(cursor)
            cursor.execute(f"PRAGMA user_version = {int(CONV_SCHEMA_VERSION)}")

        if meta:
            sync_conv_meta_cursor(cursor, meta)
        conn.commit()
    finally:
        conn.close()
    return conv_db_path


def init_conv_db(conv_id: str, meta: Optional[Dict[str, Any]] = None, db_path: Optional[Path] = None) -> Path:
    """Initialize or recover an isolated per-conversation SQLite database."""
    validate_conv_id(conv_id)
    conv_db_path = get_conv_db_path(conv_id, db_path=db_path)
    with get_conv_lock(conv_id):
        try:
            return _run_conv_db_init(conv_id, meta=meta, db_path=db_path)
        except sqlite3.DatabaseError as exc:
            _logger.error("Conversation DB corruption detected for %s: %s", conv_id, exc)
            _handle_corrupted_database(conv_db_path, exc, try_restore=True)
            try:
                return _run_conv_db_init(conv_id, meta=meta, db_path=db_path)
            except sqlite3.DatabaseError as second_exc:
                _logger.critical("Restored conversation DB failed for %s: %s. Reinitializing.", conv_id, second_exc)
                _handle_corrupted_database(conv_db_path, second_exc, try_restore=False)
                return _run_conv_db_init(conv_id, meta=meta, db_path=db_path)


@contextlib.contextmanager
def open_conv_db(conv_id: str, meta: Optional[Dict[str, Any]] = None):
    """Open an isolated conversation DB, self-healing transparently if missing or corrupted."""
    validate_conv_id(conv_id)
    conv_db_path = get_conv_db_path(conv_id)
    if not conv_db_path.is_file() or conv_db_path.stat().st_size == 0:
        init_conv_db(conv_id, meta=meta)

    conn = _connect_conv_sqlite(conv_db_path)
    needs_recovery = False
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not _REQUIRED_CONV_TABLES.issubset(tables):
            needs_recovery = True
    except sqlite3.DatabaseError:
        needs_recovery = True

    if needs_recovery:
        conn.close()
        init_conv_db(conv_id, meta=meta)
        conn = _connect_conv_sqlite(conv_db_path)

    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.Error:
        pass

    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _migration_v6(cursor: sqlite3.Cursor, db_path: Optional[Path] = None) -> None:
    """Split per-conversation messages & token_ledger out into data/conversations/<conv_id>.db."""
    conv_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(conversations)").fetchall()}
    if "message_count" not in conv_cols:
        cursor.execute("ALTER TABLE conversations ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0")
    if "assistant_reply_count" not in conv_cols:
        cursor.execute("ALTER TABLE conversations ADD COLUMN assistant_reply_count INTEGER NOT NULL DEFAULT 0")
    if "db_size_bytes" not in conv_cols:
        cursor.execute("ALTER TABLE conversations ADD COLUMN db_size_bytes INTEGER NOT NULL DEFAULT 0")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)")

    existing_tables = {
        r["name"] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    has_messages = "messages" in existing_tables
    has_ledger = "token_ledger" in existing_tables
    base_db = db_path if db_path is not None else _get_db_path()

    if has_messages or has_ledger:
        convs_dir = get_conversations_dir(base_db)
        convs_dir.mkdir(parents=True, exist_ok=True)
        conv_rows = cursor.execute("SELECT * FROM conversations").fetchall()
        valid_ids = {r["id"] for r in conv_rows}

        if has_messages:
            orphan_msgs = cursor.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id NOT IN (SELECT id FROM conversations)"
            ).fetchone()[0]
            if orphan_msgs:
                _logger.warning("Skipping %d orphan legacy message(s) without parent conversation", orphan_msgs)
        if has_ledger:
            orphan_ledger = cursor.execute(
                "SELECT COUNT(*) FROM token_ledger WHERE conversation_id NOT IN (SELECT id FROM conversations)"
            ).fetchone()[0]
            if orphan_ledger:
                _logger.warning("Skipping %d orphan legacy token_ledger row(s) without parent conversation", orphan_ledger)

        conflict_ts = int(time.time())
        for crow in conv_rows:
            cid = crow["id"]
            if cid not in valid_ids:
                continue
            target_db = get_conv_db_path(cid, db_path=base_db)
            if target_db.exists() and target_db.stat().st_size > 0:
                conflict_dest = target_db.with_name(f"{target_db.name}.pre_v6_conflict.{conflict_ts}")
                _logger.warning("Archiving pre-existing colliding conversation DB %s -> %s", target_db, conflict_dest)
                for suf in ("-wal", "-shm", ""):
                    src_f = target_db if not suf else target_db.with_name(f"{target_db.name}{suf}")
                    dst_f = conflict_dest if not suf else conflict_dest.with_name(f"{conflict_dest.name}{suf}")
                    if src_f.exists():
                        _quarantine_file(src_f, dst_f)

            msgs = (
                cursor.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
                    (cid,),
                ).fetchall()
                if has_messages
                else []
            )
            ledger_rows = (
                cursor.execute(
                    "SELECT * FROM token_ledger WHERE conversation_id = ? ORDER BY id ASC",
                    (cid,),
                ).fetchall()
                if has_ledger
                else []
            )

            conv_meta = {k: crow[k] for k in crow.keys()}
            cconn = _connect_conv_sqlite(target_db)
            try:
                ccur = cconn.cursor()
                ccur.execute("PRAGMA auto_vacuum = INCREMENTAL;")
                _create_conv_schema(ccur)
                ccur.execute(f"PRAGMA user_version = {int(CONV_SCHEMA_VERSION)}")
                sync_conv_meta_cursor(ccur, conv_meta)

                if msgs:
                    msg_payload = []
                    for m in msgs:
                        mkeys = set(m.keys())
                        msg_payload.append((
                            m["id"],
                            m["conversation_id"],
                            m["role"],
                            m["content"],
                            m["thinking_content"] if "thinking_content" in mkeys else "",
                            m["provider_id"] if "provider_id" in mkeys else "",
                            m["provider_name"] if "provider_name" in mkeys else "",
                            m["model_id"] if "model_id" in mkeys else "",
                            m["thinking_level"] if "thinking_level" in mkeys else "none",
                            int(m["prompt_tokens"] or 0) if "prompt_tokens" in mkeys else 0,
                            int(m["completion_tokens"] or 0) if "completion_tokens" in mkeys else 0,
                            int(m["cached_tokens"] or 0) if "cached_tokens" in mkeys else 0,
                            int(m["duration_ms"] or 0) if "duration_ms" in mkeys else 0,
                            int(m["worked_ms"] or 0) if "worked_ms" in mkeys else 0,
                            m["tool_trace_json"] if "tool_trace_json" in mkeys else "[]",
                            float(m["created_at"] or 0.0),
                        ))
                    ccur.executemany(
                        """
                        INSERT INTO messages (
                            id, conversation_id, role, content, thinking_content,
                            provider_id, provider_name, model_id, thinking_level,
                            prompt_tokens, completion_tokens, cached_tokens,
                            duration_ms, worked_ms, tool_trace_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        msg_payload,
                    )

                if ledger_rows:
                    ledger_payload = []
                    for lr in ledger_rows:
                        lkeys = set(lr.keys())
                        ledger_payload.append((
                            lr["conversation_id"],
                            lr["provider_id"] if "provider_id" in lkeys else "",
                            lr["provider_name"] if "provider_name" in lkeys else "",
                            lr["model_id"] if "model_id" in lkeys else "",
                            int(lr["prompt_tokens"] or 0) if "prompt_tokens" in lkeys else 0,
                            int(lr["completion_tokens"] or 0) if "completion_tokens" in lkeys else 0,
                            int(lr["cached_tokens"] or 0) if "cached_tokens" in lkeys else 0,
                            int(lr["google_search_requests"] or 0) if "google_search_requests" in lkeys else 0,
                            int(lr["ddg_search_requests"] or 0) if "ddg_search_requests" in lkeys else 0,
                            lr["cost_usd"] if "cost_usd" in lkeys else None,
                            float(lr["created_at"] or 0.0),
                        ))
                    ccur.executemany(
                        """
                        INSERT INTO token_ledger (
                            conversation_id, provider_id, provider_name, model_id,
                            prompt_tokens, completion_tokens, cached_tokens,
                            google_search_requests, ddg_search_requests, cost_usd, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        ledger_payload,
                    )

                cconn.commit()
                migrated_msgs = ccur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                migrated_ledger = ccur.execute("SELECT COUNT(*) FROM token_ledger").fetchone()[0]
                if migrated_msgs != len(msgs) or migrated_ledger != len(ledger_rows):
                    raise sqlite3.DatabaseError(
                        f"v6 migration count mismatch for {cid}: msgs {migrated_msgs}!={len(msgs)}, ledger {migrated_ledger}!={len(ledger_rows)}"
                    )
            finally:
                cconn.close()

            msg_cnt = len(msgs)
            asst_cnt = sum(
                1
                for m in msgs
                if m["role"] == "assistant"
                and ((m["content"] or "").strip() or (m["thinking_content"] or "").strip())
            )
            size_bytes = get_conv_db_size_bytes(cid, db_path=target_db)
            cursor.execute(
                """
                UPDATE conversations
                SET message_count = ?, assistant_reply_count = ?, db_size_bytes = ?
                WHERE id = ?
                """,
                (msg_cnt, asst_cnt, size_bytes, cid),
            )

        cursor.execute("DROP TABLE IF EXISTS messages")
        cursor.execute("DROP TABLE IF EXISTS token_ledger")


MIGRATIONS: List[Tuple[int, str, Callable[[sqlite3.Cursor], None]]] = [
    (1, "Base schema, composite indexes, and token ledger backfill", _migration_v1),
    (2, "Add conversations.is_auto_named column and backfill existing titled conversations", _migration_v2),
    (3, "Add messages.duration_ms and messages.provider_name columns", _migration_v3),
    (4, "Add agent workspace_dir, agent_mode, and messages.tool_trace_json", _migration_v4),
    (5, "Add messages.worked_ms and token_ledger ddg/provider_name/cost_usd columns", _migration_v5),
    (6, "Split conversations into per-conversation DB files and add index/storage metrics", _migration_v6),
]


def _create_pre_migration_backup(conn: sqlite3.Connection, db_path: Path, from_ver: int, to_ver: int) -> Path | None:
    """Create an online WAL-safe backup (`<db_name>.bak`) before upgrading an existing populated database."""
    bak_path = db_path.with_name(f"{db_path.name}.bak")
    try:
        with contextlib.closing(sqlite3.connect(bak_path, timeout=30.0)) as bak_conn:
            conn.backup(bak_conn)
        _logger.info("Created pre-migration database backup at %s (v%d -> v%d)", bak_path, from_ver, to_ver)
        return bak_path
    except Exception as exc:
        _logger.warning("Failed to create pre-migration backup at %s: %s", bak_path, exc)
        return None


_REQUIRED_CORE_TABLES = {
    "providers",
    "conversations",
    "tab_sessions",
    "app_settings",
    "google_search_usage",
}


def _quarantine_file(src: Path, dest: Path) -> None:
    if not src.exists():
        return
    try:
        src.rename(dest)
    except Exception:
        try:
            shutil.move(str(src), str(dest))
        except Exception as exc:
            _logger.warning("Could not quarantine file %s -> %s: %s", src, dest, exc)


def _restore_from_backup(db_path: Path) -> bool:
    """Attempt to restore the database from an intact backup."""
    candidates: List[Path] = []
    default_bak = db_path.with_name(f"{db_path.name}.bak")
    if default_bak.is_file():
        candidates.append(default_bak)

    if db_path.parent.is_dir():
        for p in sorted(db_path.parent.glob(f"{db_path.name}*.bak"), key=lambda x: x.stat().st_mtime, reverse=True):
            if p not in candidates and p.is_file():
                candidates.append(p)

    for cand in candidates:
        if cand.stat().st_size == 0:
            continue
        try:
            with contextlib.closing(sqlite3.connect(cand, timeout=5.0)) as test_conn:
                res = test_conn.execute("PRAGMA quick_check(1);").fetchone()
                if res and res[0] == "ok":
                    shutil.copy2(cand, db_path)
                    _logger.info("Successfully restored database from intact backup %s -> %s", cand, db_path)
                    return True
        except Exception as exc:
            _logger.warning("Candidate backup %s failed check: %s", cand, exc)
    return False


def _handle_corrupted_database(db_path: Path, error: Exception, try_restore: bool = True) -> bool:
    import gc
    gc.collect()

    ts = int(time.time())
    corrupt_dest = db_path.with_name(f"{db_path.name}.corrupted.{ts}")
    _logger.critical("Database corruption detected at %s (%s). Quarantining to %s", db_path, error, corrupt_dest)

    wal = db_path.with_name(f"{db_path.name}-wal")
    shm = db_path.with_name(f"{db_path.name}-shm")
    if wal.exists():
        _quarantine_file(wal, corrupt_dest.with_name(f"{corrupt_dest.name}-wal"))
    if shm.exists():
        _quarantine_file(shm, corrupt_dest.with_name(f"{corrupt_dest.name}-shm"))
    if db_path.exists():
        _quarantine_file(db_path, corrupt_dest)

    if try_restore:
        restored = _restore_from_backup(db_path)
        if restored:
            return True

    _logger.warning("No intact backup available to restore %s. Clean database will be initialized.", db_path)
    return False


def _seed_bundled_default_db(target_path: Path) -> bool:
    """If target_path does not exist, copy from the bundled default database template."""
    candidate_paths: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate_paths.append(Path(meipass) / "chatint" / "default_data" / "chatint.db")
        exe_dir = Path(sys.executable).resolve().parent
        candidate_paths.append(exe_dir / "data" / "chatint.db")

    for cand in candidate_paths:
        if cand.is_file() and cand.stat().st_size > 0:
            try:
                if cand.resolve() != target_path.resolve():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cand, target_path)
                    _logger.info("Initialized default database from template: %s -> %s", cand, target_path)
                    return True
            except Exception as exc:
                _logger.warning("Could not copy bundled default database from %s: %s", cand, exc)
    return False


def _run_db_init(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists() or db_path.stat().st_size == 0:
        _seed_bundled_default_db(db_path)
    pre_existed = db_path.exists() and db_path.stat().st_size > 0

    with open_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.Error as exc:
            _logger.warning("Could not enable SQLite WAL mode: %s", exc)

        if pre_existed:
            check_row = cursor.execute("PRAGMA quick_check(1);").fetchone()
            if not check_row or check_row[0] != "ok":
                raise sqlite3.DatabaseError(f"Database failed quick_check: {check_row[0] if check_row else 'empty'}")

        cur_ver_row = cursor.execute("PRAGMA user_version").fetchone()
        cur_ver = int(cur_ver_row[0]) if cur_ver_row else 0
        existing_tables = {
            r["name"] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        has_any_table = bool(existing_tables)
        has_all_core_tables = _REQUIRED_CORE_TABLES.issubset(existing_tables)

        # Ensure tables always exist even if individual tables were dropped or DB was partially created
        ran_v6_migration = False
        if cur_ver < SCHEMA_VERSION or not has_all_core_tables:
            if pre_existed and has_any_table and cur_ver < SCHEMA_VERSION:
                _create_pre_migration_backup(conn, db_path, cur_ver, SCHEMA_VERSION)

            if not has_all_core_tables:
                if cur_ver >= SCHEMA_VERSION:
                    _create_global_tables_v6(cursor)
                else:
                    _migration_v1(cursor)

            effective_from = 0 if not has_any_table else cur_ver
            for version, desc, migrate_fn in MIGRATIONS:
                if effective_from < version:
                    _logger.info("Applying DB migration v%d: %s", version, desc)
                    if version == 6:
                        migrate_fn(cursor, db_path)
                        ran_v6_migration = True
                    else:
                        migrate_fn(cursor)
                    cursor.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
            if ran_v6_migration:
                try:
                    conn.execute("VACUUM")
                except sqlite3.Error as exc:
                    _logger.debug("Skipping post-v6 VACUUM: %s", exc)

        # Clean up legacy 'custom' provider if present from earlier runs
        cursor.execute("DELETE FROM providers WHERE id = 'custom'")

        # Ensure .env exists and read current map once
        ensure_env_file()
        env_map = read_env_dict()
        env_updates = []

        # Seed default providers if missing
        cursor.execute("SELECT id, name FROM providers")
        existing_prov_rows = cursor.fetchall()
        existing_ids = {r["id"] for r in existing_prov_rows}
        existing_upper_names = {sanitize_provider_name(r["name"]).upper() for r in existing_prov_rows if r["name"]}

        for p in DEFAULT_PROVIDERS:
            clean_name = sanitize_provider_name(str(p.get("name") or "")) or "Provider"
            if p["id"] not in existing_ids and clean_name.upper() not in existing_upper_names:
                cursor.execute(
                    """
                    INSERT INTO providers (id, name, base_url, api_key, is_enabled, models_json)
                    VALUES (?, ?, ?, '', ?, ?)
                    """,
                    (
                        p["id"],
                        clean_name,
                        p["base_url"],
                        p["is_enabled"],
                        json.dumps(p["models"]),
                    ),
                )
                default_val = "ollama" if clean_name.lower() == "ollama" else (p.get("api_key") or "")
                env_updates.append({
                    "provider_name": clean_name,
                    "api_key": None,
                    "default_if_missing": default_val,
                })
                existing_ids.add(p["id"])
                existing_upper_names.add(clean_name.upper())

        # Ensure anthropic and gemini providers have empty base_url since they use native SDKs without user endpoints
        cursor.execute(
            "UPDATE providers SET base_url = '' WHERE id IN ('anthropic', 'gemini') OR LOWER(name) IN ('anthropic', 'gemini')"
        )

        # Migrate any legacy API keys out of SQLite into .env, sanitize provider names, deduplicate, and wipe api_key in DB
        cursor.execute("SELECT id, name, api_key FROM providers")
        existing_rows = cursor.fetchall()
        seen_upper_names = set()
        for r in existing_rows:
            pid = r["id"]
            raw_name = r["name"] or ""
            if raw_name.strip().lower() in ("ollama (local)", "ollamalocal"):
                clean_name = "Ollama"
            else:
                clean_name = sanitize_provider_name(raw_name) or "Provider"
            base_clean = clean_name
            suffix_idx = 0
            while clean_name.upper() in seen_upper_names:
                letter = chr(ord("A") + (suffix_idx % 26))
                clean_name = f"{base_clean}{letter}"
                suffix_idx += 1
            seen_upper_names.add(clean_name.upper())

            db_key = r["api_key"] or ""
            env_k_clean = provider_env_key(clean_name)
            env_k_raw = provider_env_key(raw_name) if raw_name != clean_name else ""
            existing_env_val = env_map.get(env_k_clean, "") or (env_map.get(env_k_raw, "") if env_k_raw else "")
            final_key = existing_env_val or db_key.strip() or ("ollama" if clean_name.lower() == "ollama" else "")

            if raw_name != clean_name or db_key or (env_k_clean not in env_map):
                env_updates.append({
                    "provider_name": clean_name,
                    "api_key": final_key if not existing_env_val else None,
                    "old_provider_name": raw_name if raw_name != clean_name else None,
                    "default_if_missing": "ollama" if clean_name.lower() == "ollama" else "",
                })

            if raw_name != clean_name or db_key != "":
                cursor.execute(
                    "UPDATE providers SET name = ?, api_key = '' WHERE id = ?",
                    (clean_name, pid),
                )

        if env_updates:
            batch_update_provider_env_keys(env_updates)

        # Seed default app settings if missing
        default_ws_models = json.dumps([
            {
                "id": PRIMARY_WEB_SEARCH_MODEL,
                "name": PRIMARY_WEB_SEARCH_MODEL,
                "input_price": 0.0,
                "cached_input_price": 0.0,
                "output_price": 0.0,
            }
        ])
        cursor.executemany(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            [
                ("theme", "dark"),
                ("global_system_prompt", "You are a helpful, brilliant, and precise assistant."),
                ("default_provider", "openai"),
                ("default_model", "gpt-4o"),
                ("default_thinking", "medium"),
                ("web_search_models", default_ws_models),
                ("use_gemini_web_search", "false"),
                ("default_workspace_dir", ""),
                ("default_agent_mode", "manual"),
                ("max_chat_size_mb", "50"),
                ("max_total_chats_size_mb", "2048"),
            ],
        )

        conn.commit()


def init_db() -> None:
    """Initialize SQLite database, safely recovering from corruption via backup or quarantine."""
    db_path = _get_db_path()
    try:
        _run_db_init(db_path)
    except sqlite3.DatabaseError as exc:
        _logger.error("Database initialization failed due to corruption or database error: %s", exc)
        _handle_corrupted_database(db_path, exc, try_restore=True)
        try:
            _run_db_init(db_path)
        except sqlite3.DatabaseError as second_exc:
            _logger.critical("Restored database also failed: %s. Quarantining and starting fresh.", second_exc)
            _handle_corrupted_database(db_path, second_exc, try_restore=False)
            _run_db_init(db_path)
