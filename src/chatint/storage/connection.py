"""SQLite connection management, sequential schema migration runner, and .bak backup."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Callable, List, Tuple

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
SCHEMA_VERSION = 3


def _get_db_path() -> Path:
    db_mod = sys.modules.get("chatint.db")
    if db_mod is not None and hasattr(db_mod, "DB_PATH"):
        return db_mod.DB_PATH
    return _DEFAULT_DB_PATH


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
        created_at REAL NOT NULL,
        FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
    )
    """)

    ledger_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(token_ledger)").fetchall()}
    if "google_search_requests" not in ledger_cols:
        cursor.execute("ALTER TABLE token_ledger ADD COLUMN google_search_requests INTEGER NOT NULL DEFAULT 0")

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


MIGRATIONS: List[Tuple[int, str, Callable[[sqlite3.Cursor], None]]] = [
    (1, "Base schema, composite indexes, and token ledger backfill", _migration_v1),
    (2, "Add conversations.is_auto_named column and backfill existing titled conversations", _migration_v2),
    (3, "Add messages.duration_ms and messages.provider_name columns", _migration_v3),
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
    "messages",
    "tab_sessions",
    "app_settings",
    "token_ledger",
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
        if cur_ver < SCHEMA_VERSION or not has_all_core_tables:
            if pre_existed and has_any_table and cur_ver < SCHEMA_VERSION:
                _create_pre_migration_backup(conn, db_path, cur_ver, SCHEMA_VERSION)

            if not has_all_core_tables:
                _migration_v1(cursor)

            effective_from = 0 if not has_any_table else cur_ver
            for version, desc, migrate_fn in MIGRATIONS:
                if effective_from < version:
                    _logger.info("Applying DB migration v%d: %s", version, desc)
                    migrate_fn(cursor)
                    cursor.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()

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
