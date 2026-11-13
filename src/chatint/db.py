import contextlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from chatint.config import DB_PATH, DEFAULT_PROVIDERS, ENV_PATH
from chatint.logging_setup import get_logger

_ENV_LOCK = threading.RLock()
_logger = get_logger("chatint.db")


def mask_api_key(api_key: str) -> str:
    """Return a non-sensitive masked display hint for a saved API key."""
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 4:
        return "•" * len(key)
    if len(key) <= 10:
        return f"{key[:2]}••••{key[-2:]}"
    return f"{key[:4]}••••••{key[-4:]}"


def strip_model_prefix(model_id: str) -> str:
    return model_id[7:] if model_id.startswith("models/") else model_id


def sanitize_provider_name(name: str) -> str:
    """
    Removes any non-English alphabet characters from a provider name.
    """
    return re.sub(r"[^A-Za-z]", "", (name or "").strip())


def is_valid_provider_name(name: str) -> bool:
    """
    Provider name must consist exclusively of English alphabet letters (A-Z, a-z).
    """
    return bool(name and re.fullmatch(r"[A-Za-z]+", name))


def provider_env_key(provider_name: str) -> str:
    clean = sanitize_provider_name(provider_name)
    return f"{clean.upper()}_API_KEY"


def _parse_env_value(raw_val: str) -> str:
    val = (raw_val or "").strip()
    if not val:
        return ""
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        inner = val[1:-1]
        out: List[str] = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt == "n":
                    out.append("\n")
                elif nxt == "r":
                    out.append("\r")
                elif nxt == "t":
                    out.append("\t")
                elif nxt in ('"', "\\"):
                    out.append(nxt)
                else:
                    out.append("\\" + nxt)
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    # Unquoted value: strip trailing inline comment if preceded by whitespace
    comment_match = re.search(r"\s+#", val)
    if comment_match:
        val = val[: comment_match.start()].rstrip()
    return val


def _format_env_value(value: Any) -> str:
    val = "" if value is None else str(value).replace("\r\n", "\n")
    if val == "":
        return ""
    needs_quotes = (
        any(ch.isspace() for ch in val)
        or any(ch in val for ch in ('#', '"', "'", "=", "\\"))
    )
    if not needs_quotes:
        return val
    escaped = (
        val.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _atomic_write_env(content: str):
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = ENV_PATH.parent / f".{ENV_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, ENV_PATH)
        try:
            os.chmod(ENV_PATH, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def ensure_env_file():
    with _ENV_LOCK:
        if not ENV_PATH.exists() or ENV_PATH.read_text(encoding="utf-8").strip() == "":
            lines = []
            for p in DEFAULT_PROVIDERS:
                clean = sanitize_provider_name(p.get("name", ""))
                if clean:
                    default_val = "ollama" if clean.lower() == "ollama" else (p.get("api_key") or "")
                    lines.append(f"{clean.upper()}_API_KEY={_format_env_value(default_val)}")
            _atomic_write_env("\n".join(lines) + ("\n" if lines else ""))
        else:
            raw_text = ENV_PATH.read_text(encoding="utf-8")
            existing_keys = set()
            for line in raw_text.splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    existing_keys.add(s.split("=", 1)[0].strip().upper())
            appended = []
            for p in DEFAULT_PROVIDERS:
                clean = sanitize_provider_name(p.get("name", ""))
                if clean:
                    env_k = f"{clean.upper()}_API_KEY"
                    if env_k not in existing_keys:
                        default_val = "ollama" if clean.lower() == "ollama" else (p.get("api_key") or "")
                        appended.append(f"{env_k}={_format_env_value(default_val)}")
            if appended:
                prefix = raw_text if raw_text.endswith("\n") else (raw_text + "\n")
                _atomic_write_env(prefix + "\n".join(appended) + "\n")


def read_env_dict() -> Dict[str, str]:
    with _ENV_LOCK:
        ensure_env_file()
        env_map: Dict[str, str] = {}
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            k = k.strip()
            if not k:
                continue
            env_map[k] = _parse_env_value(v)
        return env_map


def write_env_dict(env_map: Dict[str, str]):
    with _ENV_LOCK:
        ensure_env_file()
        existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
        remaining = dict(env_map)
        out_lines: List[str] = []
        seen_keys = set()

        for raw_line in existing_lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                out_lines.append(raw_line)
                continue
            k, _ = stripped.split("=", 1)
            k_clean = k.strip()
            if k_clean in remaining:
                if k_clean not in seen_keys:
                    out_lines.append(f"{k_clean}={_format_env_value(remaining.pop(k_clean))}")
                    seen_keys.add(k_clean)
            # Deleted keys are omitted cleanly while preserving comments and blank lines

        for k, v in remaining.items():
            out_lines.append(f"{k}={_format_env_value(v)}")

        content = "\n".join(out_lines) + ("\n" if out_lines else "")
        _atomic_write_env(content)


def get_api_key_from_env(provider_name: str) -> str:
    key_name = provider_env_key(provider_name)
    if key_name == "_API_KEY":
        return ""
    env_map = read_env_dict()
    return env_map.get(key_name, "")


def set_api_key_in_env(provider_name: str, api_key: str, old_provider_name: Optional[str] = None):
    with _ENV_LOCK:
        env_map = read_env_dict()
        old_val = ""
        if old_provider_name and sanitize_provider_name(old_provider_name).upper() != sanitize_provider_name(provider_name).upper():
            old_key = provider_env_key(old_provider_name)
            old_val = env_map.pop(old_key, "")
        new_key = provider_env_key(provider_name)
        if new_key != "_API_KEY":
            final_val = (api_key if api_key is not None else "").strip() or old_val
            env_map[new_key] = final_val
        write_env_dict(env_map)


def remove_api_key_from_env(provider_name: str):
    with _ENV_LOCK:
        env_map = read_env_dict()
        key_name = provider_env_key(provider_name)
        if key_name in env_map:
            env_map.pop(key_name, None)
            write_env_dict(env_map)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=10000;")
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


PRIMARY_WEB_SEARCH_MODEL = "gemini-3.1-flash-lite"
SCHEMA_VERSION = 1


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.Error as exc:
            _logger.warning("Could not enable SQLite WAL mode: %s", exc)

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
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """)

        cursor.execute("PRAGMA table_info(conversations)")
        conv_cols = {r["name"] for r in cursor.fetchall()}
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
            model_id TEXT NOT NULL DEFAULT '',
            thinking_level TEXT NOT NULL DEFAULT 'none',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens INTEGER NOT NULL DEFAULT 0,
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

        cursor.execute("PRAGMA table_info(token_ledger)")
        ledger_cols = {r["name"] for r in cursor.fetchall()}
        if "google_search_requests" not in ledger_cols:
            cursor.execute("ALTER TABLE token_ledger ADD COLUMN google_search_requests INTEGER NOT NULL DEFAULT 0")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS google_search_usage (
            month_key TEXT PRIMARY KEY,
            request_count INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        )
        """)

        # Populate token_ledger from historical messages if token_ledger is empty
        cursor.execute("SELECT COUNT(*) as count FROM token_ledger")
        t_count = cursor.fetchone()
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

        cur_ver_row = cursor.execute("PRAGMA user_version").fetchone()
        cur_ver = int(cur_ver_row[0]) if cur_ver_row else 0
        if cur_ver < SCHEMA_VERSION:
            cursor.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        # Clean up legacy 'custom' provider if present from earlier runs
        cursor.execute("DELETE FROM providers WHERE id = 'custom'")

        # Ensure .env exists
        ensure_env_file()

        # Seed default providers if none exist, and ensure newly added built-in providers (e.g. anthropic, gemini) exist
        cursor.execute("SELECT id, name FROM providers")
        existing_prov_rows = cursor.fetchall()
        existing_ids = {r["id"] for r in existing_prov_rows}
        existing_upper_names = {sanitize_provider_name(r["name"]).upper() for r in existing_prov_rows if r["name"]}

        for p in DEFAULT_PROVIDERS:
            clean_name = sanitize_provider_name(p["name"]) or "Provider"
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
                default_env_key = get_api_key_from_env(clean_name) or (
                    "ollama" if clean_name.lower() == "ollama" else (p.get("api_key") or "")
                )
                set_api_key_in_env(clean_name, default_env_key)
                existing_ids.add(p["id"])
                existing_upper_names.add(clean_name.upper())

        # Ensure anthropic and gemini providers have empty base_url since they use native SDKs without user endpoints
        cursor.execute(
            "UPDATE providers SET base_url = '' WHERE id IN ('anthropic', 'gemini') OR LOWER(name) IN ('anthropic', 'gemini')"
        )

        # Migrate any legacy API keys out of SQLite into .env, sanitize provider names to English letters only,
        # deduplicate case-insensitively, and wipe api_key column in DB.
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

            db_key = (r["api_key"] or "").strip()
            env_key_val = get_api_key_from_env(clean_name) or (
                get_api_key_from_env(raw_name) if raw_name != clean_name else ""
            )
            final_key = env_key_val or db_key or ("ollama" if clean_name.lower() == "ollama" else "")
            set_api_key_in_env(
                clean_name, final_key, old_provider_name=raw_name if raw_name != clean_name else None
            )

            cursor.execute(
                "UPDATE providers SET name = ?, api_key = '' WHERE id = ?",
                (clean_name, pid),
            )

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


def _normalize_search_model_entry(m: Dict[str, Any]) -> Dict[str, Any]:
    mid = strip_model_prefix((m.get("id") or "").strip())
    return {
        "id": mid,
        "name": strip_model_prefix((m.get("name") or mid).strip()) or mid,
        "input_price": max(0.0, float(m.get("input_price") or 0.0)),
        "cached_input_price": max(0.0, float(m.get("cached_input_price") or 0.0)),
        "output_price": max(0.0, float(m.get("output_price") or 0.0)),
    }


def normalize_web_search_models(models: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Normalize Web Search models (max 5): gemini-3.1-flash-lite is always fixed at index 0
    (preserving any user-configured pricing), followed by up to 4 unique fallback Gemini models.
    """
    primary_entry: Optional[Dict[str, Any]] = None
    fallbacks: List[Dict[str, Any]] = []
    seen = set()

    for raw in models or []:
        if not isinstance(raw, dict):
            continue
        entry = _normalize_search_model_entry(raw)
        mid = entry["id"]
        if not mid:
            continue
        low = mid.lower()
        if low == PRIMARY_WEB_SEARCH_MODEL:
            if primary_entry is None:
                entry["id"] = PRIMARY_WEB_SEARCH_MODEL
                entry["name"] = PRIMARY_WEB_SEARCH_MODEL
                primary_entry = entry
            continue
        if low in seen:
            continue
        seen.add(low)
        fallbacks.append(entry)

    if primary_entry is None:
        primary_entry = {
            "id": PRIMARY_WEB_SEARCH_MODEL,
            "name": PRIMARY_WEB_SEARCH_MODEL,
            "input_price": 0.0,
            "cached_input_price": 0.0,
            "output_price": 0.0,
        }

    return [primary_entry, *fallbacks[:4]]


def get_web_search_models() -> List[Dict[str, Any]]:
    settings = get_app_settings()
    raw = settings.get("web_search_models") or "[]"
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = []
    return normalize_web_search_models(parsed if isinstance(parsed, list) else [])


def save_web_search_models(models: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    normalized = normalize_web_search_models(models)
    save_app_settings({"web_search_models": json.dumps(normalized)})
    return normalized


def _normalize_model_entry(m: Dict[str, Any]) -> Dict[str, Any]:
    mid = (m.get("id") or "").strip()
    entry = {
        "id": mid,
        "name": (m.get("name") or mid).strip(),
        "supports_thinking": 1 if m.get("supports_thinking") else 0,
        "input_price": float(m.get("input_price") or 0.0),
        "cached_input_price": float(m.get("cached_input_price") or 0.0),
        "output_price": float(m.get("output_price") or 0.0),
    }
    raw_res = m.get("resolved_thinking")
    if isinstance(raw_res, dict):
        entry["resolved_thinking"] = {
            str(k).lower(): str(v).lower()
            for k, v in raw_res.items()
            if isinstance(k, str) and isinstance(v, str)
        }
    return entry


_BUILTIN_PROVIDER_ORDER = {p["id"]: idx for idx, p in enumerate(DEFAULT_PROVIDERS)}


def get_providers() -> List[Dict[str, Any]]:
    from chatint.client_manager import BackgroundModelSyncer

    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT rowid, id, name, base_url, is_enabled, models_json FROM providers")
        rows = list(cursor.fetchall())

    rows.sort(key=lambda r: (_BUILTIN_PROVIDER_ORDER.get(r["id"], 999), r["rowid"]))
    env_map = read_env_dict()

    providers = []
    for r in rows:
        pid = r["id"]
        pname = sanitize_provider_name(r["name"]) or "Provider"
        raw_models = json.loads(r["models_json"] or "[]")
        models = [_normalize_model_entry(m) for m in raw_models if m.get("id")]
        env_key_name = provider_env_key(pname)
        raw_env_key = "" if env_key_name == "_API_KEY" else env_map.get(env_key_name, "")
        has_api_key = bool(raw_env_key)
        is_enabled = bool(r["is_enabled"])
        is_verified = BackgroundModelSyncer.is_provider_verified(pid) if has_api_key else False
        verified_ids = BackgroundModelSyncer.get_verified_model_ids(pid) if is_verified else set()
        sync_status, sync_error = BackgroundModelSyncer.get_sync_status(pid, has_api_key=has_api_key, is_enabled=is_enabled)
        providers.append({
            "id": pid,
            "name": pname,
            "base_url": r["base_url"],
            "has_api_key": has_api_key,
            "api_key_mask": mask_api_key(raw_env_key) if has_api_key else "",
            "is_enabled": is_enabled,
            "models": models,
            "fetched_model_ids": sorted(list(verified_ids)),
            "is_accessible": bool(is_enabled and has_api_key and is_verified and len(models) > 0),
            "sync_status": sync_status,
            "sync_error": sync_error,
        })
    return providers


def update_provider_models(provider_id: str, models: List[Dict[str, Any]]) -> bool:
    """Safely update only models_json for a single provider without touching or pruning other providers."""
    if not provider_id:
        return False
    normalized_models = [_normalize_model_entry(m) for m in (models or []) if isinstance(m, dict) and m.get("id")]
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE providers SET models_json = ? WHERE id = ?",
            (json.dumps(normalized_models), provider_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def save_providers(providers: List[Dict[str, Any]], prune_missing: bool = True):
    from chatint.client_manager import BackgroundModelSyncer

    with open_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT id, name, base_url, models_json FROM providers")
        existing_rows = {r["id"]: r for r in cursor.fetchall()}

        # Clean up any custom (non-builtin) providers that were deleted by the user
        if prune_missing:
            submitted_ids = {p["id"] for p in providers}
            for old_id, old_r in existing_rows.items():
                if old_id not in submitted_ids and old_id not in _BUILTIN_PROVIDER_ORDER:
                    if old_r["name"]:
                        remove_api_key_from_env(old_r["name"])
                    cursor.execute("DELETE FROM providers WHERE id = ?", (old_id,))

        for p in providers:
            pid = p["id"]
            old_row = existing_rows.get(pid)
            old_name = old_row["name"] if old_row else None
            clean_name = sanitize_provider_name(p.get("name", "")) or sanitize_provider_name(old_name or "") or "Provider"

            # If provider was renamed or a new api_key was explicitly passed in payload, update .env ONLY
            raw_new_key = p.get("api_key")
            if raw_new_key is not None and str(raw_new_key).strip() != "":
                set_api_key_in_env(clean_name, str(raw_new_key).strip(), old_provider_name=old_name)
            elif p.get("clear_api_key"):
                set_api_key_in_env(clean_name, "", old_provider_name=old_name)
            elif old_name and sanitize_provider_name(old_name).upper() != clean_name.upper():
                # Renamed without changing key -> migrate existing .env entry to new name
                existing_val = get_api_key_from_env(old_name)
                set_api_key_in_env(clean_name, existing_val, old_provider_name=old_name)
            elif not old_name:
                # Newly added provider -> ensure entry exists in .env
                default_env_key = get_api_key_from_env(clean_name) or ("ollama" if clean_name.lower() == "ollama" else (p.get("api_key") or ""))
                set_api_key_in_env(clean_name, default_env_key)

            normalized_models = [_normalize_model_entry(m) for m in (p.get("models") or []) if m.get("id")]
            if p.get("models") is not None:
                models_json_str = json.dumps(normalized_models)
            elif old_row and old_row["models_json"]:
                models_json_str = old_row["models_json"]
            else:
                models_json_str = "[]"

            is_native_sdk = pid.lower() in ("anthropic", "gemini") or clean_name.lower() in ("anthropic", "gemini")
            base_url_val = "" if is_native_sdk else p.get("base_url", "")

            key_changed = (raw_new_key is not None and str(raw_new_key).strip() != "") or bool(p.get("clear_api_key"))
            url_changed = bool(old_row and not is_native_sdk and (base_url_val != old_row["base_url"]))
            if key_changed or url_changed:
                BackgroundModelSyncer.clear_provider_verified(pid)
                if p.get("clear_api_key"):
                    BackgroundModelSyncer.set_sync_status(pid, "no_key", "NO API KEY")
                else:
                    BackgroundModelSyncer.set_sync_status(pid, "checking", "CHECKING...")

            # Never store api_key in the database (always '')
            cursor.execute(
                """
                INSERT INTO providers (id, name, base_url, api_key, is_enabled, models_json)
                VALUES (?, ?, ?, '', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    base_url=excluded.base_url,
                    api_key='',
                    is_enabled=excluded.is_enabled,
                    models_json=excluded.models_json
            """,
                (
                    pid,
                    clean_name,
                    base_url_val,
                    1 if p.get("is_enabled", True) else 0,
                    models_json_str,
                ),
            )
        conn.commit()


def update_model_resolved_thinking(provider_id: str, model_id: str, requested_level: str, resolved_level: str):
    """Store the verified thinking fallback for a specific model under its provider entry."""
    if not provider_id or not model_id or not requested_level:
        return
    req = requested_level.strip().lower()
    res = (resolved_level or "none").strip().lower()

    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT models_json FROM providers WHERE id = ?", (provider_id,))
        row = cursor.fetchone()
        if not row:
            return

        try:
            models = json.loads(row["models_json"] or "[]")
        except Exception:
            models = []

        model_found = False
        for m in models:
            if m.get("id") == model_id:
                m_resolved = m.setdefault("resolved_thinking", {})
                if m_resolved.get(req) != res:
                    m_resolved[req] = res
                    model_found = True
                break

        if model_found:
            cursor.execute(
                "UPDATE providers SET models_json = ? WHERE id = ?",
                (json.dumps(models), provider_id),
            )
            conn.commit()


def get_model_resolved_thinking(provider_id: str, model_id: str, requested_level: str) -> Optional[str]:
    """Retrieve the stored verified thinking fallback for a specific model."""
    if not provider_id or not model_id or not requested_level:
        return None
    req = requested_level.strip().lower()

    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT models_json FROM providers WHERE id = ?", (provider_id,))
        row = cursor.fetchone()
    if not row:
        return None

    try:
        models = json.loads(row["models_json"] or "[]")
        for m in models:
            if m.get("id") == model_id:
                m_resolved = m.get("resolved_thinking") or {}
                return m_resolved.get(req)
    except Exception:
        pass
    return None


def remove_model_resolved_thinking(
    provider_id: str,
    model_id: Optional[str] = None,
    requested_level: Optional[str] = None,
):
    """Remove cached thinking resolution(s) for a provider/model/level if stale or invalid."""
    if not provider_id:
        return
    req = requested_level.strip().lower() if requested_level else None
    clean_mid = strip_model_prefix(model_id) if model_id else None

    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT models_json FROM providers WHERE id = ?", (provider_id,))
        row = cursor.fetchone()
        if not row:
            return

        try:
            models = json.loads(row["models_json"] or "[]")
            changed = False
            for m in models:
                mid = m.get("id") or ""
                if model_id is None or mid == model_id or strip_model_prefix(mid) == clean_mid:
                    if "resolved_thinking" in m:
                        if req is None:
                            del m["resolved_thinking"]
                            changed = True
                        elif isinstance(m["resolved_thinking"], dict) and req in m["resolved_thinking"]:
                            del m["resolved_thinking"][req]
                            changed = True
            if changed:
                cursor.execute(
                    "UPDATE providers SET models_json = ? WHERE id = ?",
                    (json.dumps(models), provider_id),
                )
                conn.commit()
        except Exception:
            pass


def get_app_settings() -> Dict[str, str]:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM app_settings")
        rows = cursor.fetchall()
    return {r["key"]: r["value"] for r in rows}


def save_app_settings(settings: Dict[str, str]):
    with open_db() as conn:
        cursor = conn.cursor()
        for k, v in settings.items():
            cursor.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
                (k, str(v)),
            )
        conn.commit()


def get_unique_conversation_title(
    title: str,
    exclude_conv_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """
    Ensures that no two conversations share the exact same title (case-insensitive).
    If a conflict exists, appends (1), (2), etc.
    """
    candidate = (title or "New Chat").strip()
    if not candidate:
        candidate = "New Chat"

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

    # If duplicate, check if candidate already ends with `(N)` (where N <= 500)
    match = re.search(r"^(.*?)(?:\s*\((\d+)\))?$", candidate)
    if match and match.group(2) and int(match.group(2)) <= 500:
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
    """
    Generates a unique branch title: 'Original Title (Branch)', 'Original Title (Branch 1)', etc.
    """
    raw_title = (original_title or "New Chat").strip()
    clean_title = re.sub(r"\s*\(Branch(?:\s*\d+)?\)$", "", raw_title, flags=re.IGNORECASE).strip()
    if not clean_title:
        clean_title = "New Chat"

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
    title: str = "New Chat", system_prompt: str = "", max_context: int = 30
) -> Dict[str, Any]:
    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        title = get_unique_conversation_title(title, conn=conn)
        cursor = conn.cursor()
        conv_id = str(uuid.uuid4())
        now = time.time()
        cursor.execute(
            """
            INSERT INTO conversations (id, title, system_prompt, max_context_messages, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (conv_id, title, system_prompt, max_context, now, now),
        )
        conn.commit()
    return {
        "id": conv_id,
        "title": title,
        "system_prompt": system_prompt,
        "max_context_messages": max_context,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }


def get_conversations() -> List[Dict[str, Any]]:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.id, c.title, c.system_prompt, c.max_context_messages, c.is_imported,
                   c.created_at, c.updated_at,
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
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "message_count": r["message_count"],
        }
        for r in rows
    ]


def delete_provider(provider_id: str):
    if provider_id in _BUILTIN_PROVIDER_ORDER:
        return
    with open_db() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
        if row and row["name"]:
            remove_api_key_from_env(row["name"])
        cursor.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        conn.commit()


def get_conversation(conv_id: str) -> Optional[Dict[str, Any]]:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        conv_row = cursor.fetchone()
        if not conv_row:
            return None

        cursor.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC", (conv_id,)
        )
        msg_rows = cursor.fetchall()

    messages = [
        {
            "id": m["id"],
            "conversation_id": m["conversation_id"],
            "role": m["role"],
            "content": m["content"],
            "thinking_content": m["thinking_content"],
            "provider_id": m["provider_id"],
            "model_id": m["model_id"],
            "thinking_level": m["thinking_level"],
            "prompt_tokens": m["prompt_tokens"],
            "completion_tokens": m["completion_tokens"],
            "cached_tokens": m["cached_tokens"],
            "created_at": m["created_at"],
        }
        for m in msg_rows
    ]

    return {
        "id": conv_row["id"],
        "title": conv_row["title"],
        "system_prompt": conv_row["system_prompt"],
        "max_context_messages": conv_row["max_context_messages"],
        "is_imported": bool(conv_row["is_imported"]) if "is_imported" in conv_row.keys() else False,
        "created_at": conv_row["created_at"],
        "updated_at": conv_row["updated_at"],
        "messages": messages,
        "token_stats": get_token_stats(conv_id),
    }


_ALLOWED_CONV_COLUMNS = {"title", "system_prompt", "max_context_messages", "is_imported"}


def update_conversation(conv_id: str, only_if_default: bool = False, **kwargs) -> Optional[str]:
    invalid_keys = [k for k in kwargs if k not in _ALLOWED_CONV_COLUMNS]
    if invalid_keys:
        raise ValueError(f"Unsupported conversation update column(s): {', '.join(invalid_keys)}")

    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        if only_if_default:
            row = cursor.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if not row or (row["title"] or "").strip().lower() not in ("new chat", ""):
                conn.rollback()
                return None

        if "title" in kwargs and kwargs["title"]:
            kwargs["title"] = get_unique_conversation_title(kwargs["title"], exclude_conv_id=conv_id, conn=conn)
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
    Creates an exact duplicate/branch of the conversation under a unique branch title:
    'Original Title (Branch)', 'Original Title (Branch 1)', etc.
    All messages (content, role, thinking, model) are duplicated,
    while token usage and cost in token_ledger start at 0.
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
            INSERT INTO conversations (id, title, system_prompt, max_context_messages, is_imported, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_conv_id,
                branch_title,
                conv.get("system_prompt", ""),
                conv.get("max_context_messages", 30),
                1 if conv.get("is_imported") else 0,
                now,
                now,
            ),
        )

        # Duplicate all messages; set per-message tokens to 0 so the ledger & bottom panel start from 0
        for idx, m in enumerate(conv.get("messages", [])):
            new_msg_id = str(uuid.uuid4())
            msg_time = now + (idx * 0.001)
            cursor.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, role, content, thinking_content,
                    provider_id, model_id, thinking_level,
                    prompt_tokens, completion_tokens, cached_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?)
                """,
                (
                    new_msg_id,
                    new_conv_id,
                    m["role"],
                    m.get("content", ""),
                    m.get("thinking_content", ""),
                    m.get("provider_id", ""),
                    m.get("model_id", ""),
                    m.get("thinking_level", "none"),
                    msg_time,
                ),
            )

        conn.commit()
    return get_conversation(new_conv_id)


def import_conversation_data(
    title: str,
    system_prompt: str = "",
    max_context: int = 30,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Imports a conversation from parsed data with a deduplicated title.
    Starts with 0 in token ledger so bottom analytics start clean.
    """
    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        unique_title = get_unique_conversation_title(title or "Imported Chat", conn=conn)
        new_conv_id = str(uuid.uuid4())
        now = time.time()

        cursor.execute(
            """
            INSERT INTO conversations (id, title, system_prompt, max_context_messages, is_imported, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (
                new_conv_id,
                unique_title,
                system_prompt or "",
                max(1, min(500, int(max_context or 30))),
                now,
                now,
            ),
        )

        if messages:
            for idx, m in enumerate(messages):
                new_msg_id = str(uuid.uuid4())
                raw_role = str(m.get("role", "user") or "user").strip().lower()
                role = raw_role if raw_role in ("user", "assistant", "system") else "user"
                raw_ts = m.get("created_at")
                if isinstance(raw_ts, (int, float)) and raw_ts > 0:
                    msg_time = float(raw_ts)
                else:
                    msg_time = now + (idx * 0.001)
                cursor.execute(
                    """
                    INSERT INTO messages (
                        id, conversation_id, role, content, thinking_content,
                        provider_id, model_id, thinking_level,
                        prompt_tokens, completion_tokens, cached_tokens, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?)
                    """,
                    (
                        new_msg_id,
                        new_conv_id,
                        role,
                        m.get("content", ""),
                        m.get("thinking_content", ""),
                        m.get("provider_id", ""),
                        m.get("model_id", ""),
                        m.get("thinking_level", "none"),
                        msg_time,
                    ),
                )

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


def record_google_search_requests(query_count: int):
    """Increment the durable monthly Google Search request counter (actual queries run by Flash Lite)."""
    count = max(0, int(query_count or 0))
    if count <= 0:
        return
    month_key = time.strftime("%Y-%m")
    now = time.time()
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO google_search_usage (month_key, request_count, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(month_key) DO UPDATE SET
                request_count = request_count + excluded.request_count,
                updated_at = excluded.updated_at
            """,
            (month_key, count, now),
        )
        conn.commit()


def get_google_search_request_totals() -> Dict[str, Any]:
    """Return current month and all-time actual Google Search request counts."""
    month_key = time.strftime("%Y-%m")
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT month_key, request_count FROM google_search_usage")
        rows = cursor.fetchall()
    monthly = 0
    all_time = 0
    for r in rows:
        cnt = int(r["request_count"] or 0)
        all_time += cnt
        if r["month_key"] == month_key:
            monthly = cnt
    return {"month": month_key, "monthly": monthly, "all_time": all_time}


def record_token_usage(
    conversation_id: str,
    provider_id: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    google_search_requests: int = 0,
) -> bool:
    search_cnt = max(0, int(google_search_requests or 0))
    if not conversation_id or (prompt_tokens == 0 and completion_tokens == 0 and cached_tokens == 0 and search_cnt == 0):
        return False
    try:
        with open_db() as conn:
            cursor = conn.cursor()
            if not cursor.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
                return False
            now = time.time()
            cursor.execute(
                """
                INSERT INTO token_ledger (
                    conversation_id, provider_id, model_id,
                    prompt_tokens, completion_tokens, cached_tokens, google_search_requests, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    provider_id or "",
                    model_id or "",
                    prompt_tokens,
                    completion_tokens,
                    cached_tokens,
                    search_cnt,
                    now,
                ),
            )
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        _logger.debug("Skipped token_ledger insert; conversation %s was deleted", conversation_id)
        return False


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
) -> Optional[Dict[str, Any]]:
    if not conversation_id:
        return None
    if not msg_id:
        msg_id = str(uuid.uuid4())
    now = time.time()

    try:
        with open_db() as conn:
            cursor = conn.cursor()
            if not cursor.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
                _logger.debug("Skipped add_message; conversation %s no longer exists", conversation_id)
                return None

            cursor.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, role, content, thinking_content,
                    provider_id, model_id, thinking_level,
                    prompt_tokens, completion_tokens, cached_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    msg_id,
                    conversation_id,
                    role,
                    content,
                    thinking_content,
                    provider_id,
                    model_id,
                    thinking_level,
                    prompt_tokens,
                    completion_tokens,
                    cached_tokens,
                    now,
                ),
            )

            # Touch conversation updated_at
            cursor.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))

            if prompt_tokens > 0 or completion_tokens > 0 or cached_tokens > 0:
                cursor.execute(
                    """
                    INSERT INTO token_ledger (
                        conversation_id, provider_id, model_id,
                        prompt_tokens, completion_tokens, cached_tokens, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        provider_id or "",
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
        "model_id": model_id,
        "thinking_level": thinking_level,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "created_at": now,
    }


_ALLOWED_MSG_COLUMNS = {
    "role",
    "content",
    "thinking_content",
    "provider_id",
    "model_id",
    "thinking_level",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
}


def update_message(msg_id: str, **kwargs):
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


def delete_messages_from(conversation_id: str, from_msg_id: str, include_self: bool = True):
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT rowid, created_at FROM messages WHERE id = ? AND conversation_id = ?",
            (from_msg_id, conversation_id),
        )
        target = cursor.fetchone()
        if not target and (from_msg_id.startswith("user_temp_") or not from_msg_id):
            # Fallback: if client passed a temporary user message ID, match the latest user message
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
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (time.time(), conversation_id)
            )
            conn.commit()


def get_token_stats(conversation_id: str) -> Dict[str, Any]:
    """
    Returns aggregated token stats and estimated cost per model and grand total for the conversation.
    Uses token_ledger so token and cost counts are never lost when branching, editing, or regenerating.
    """
    global_search_totals = get_google_search_request_totals()
    if not conversation_id:
        return {
            "per_model": [],
            "grand_total": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "cache_miss_tokens": 0,
                "cache_hit_rate": 0.0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "google_search_requests": 0,
            },
            "google_search_totals": global_search_totals,
        }

    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(SUM(google_search_requests), 0) as conv_searches
            FROM token_ledger
            WHERE conversation_id = ?
        """,
            (conversation_id,),
        )
        conv_search_row = cursor.fetchone()
        conv_search_requests = int((conv_search_row["conv_searches"] if conv_search_row else 0) or 0)

        cursor.execute(
            """
            SELECT model_id, provider_id,
                   SUM(prompt_tokens) as total_in,
                   SUM(completion_tokens) as total_out,
                   SUM(cached_tokens) as total_cached
            FROM token_ledger
            WHERE conversation_id = ? AND (model_id != '' OR prompt_tokens > 0)
            GROUP BY model_id, provider_id
        """,
            (conversation_id,),
        )
        rows = cursor.fetchall()

        # Fallback to messages table if token_ledger has no records for this conversation
        if not rows:
            cursor.execute(
                """
                SELECT model_id, provider_id,
                       SUM(prompt_tokens) as total_in,
                       SUM(completion_tokens) as total_out,
                       SUM(cached_tokens) as total_cached
                FROM messages
                WHERE conversation_id = ? AND role = 'assistant' AND (model_id != '' OR prompt_tokens > 0)
                GROUP BY model_id, provider_id
            """,
                (conversation_id,),
            )
            rows = cursor.fetchall()

        cursor.execute("SELECT id, models_json FROM providers")
        prov_rows = cursor.fetchall()

    pricing_map: Dict[tuple[str, str], Dict[str, float]] = {}
    model_fallback_map: Dict[str, Dict[str, float]] = {}
    for pr in prov_rows:
        pid = pr["id"]
        try:
            mlist = json.loads(pr["models_json"] or "[]")
            for m in mlist:
                mid = m.get("id")
                if mid:
                    rates = {
                        "input_price": float(m.get("input_price") or 0.0),
                        "cached_input_price": float(m.get("cached_input_price") or 0.0),
                        "output_price": float(m.get("output_price") or 0.0),
                    }
                    clean_id = strip_model_prefix(mid)
                    pricing_map[(pid, mid)] = rates
                    pricing_map[(pid, clean_id)] = rates
                    model_fallback_map[mid] = rates
                    model_fallback_map[clean_id] = rates
        except Exception:
            pass

    for wm in get_web_search_models():
        mid = wm.get("id")
        if not mid:
            continue
        clean_id = strip_model_prefix(mid)
        rates = {
            "input_price": float(wm.get("input_price") or 0.0),
            "cached_input_price": float(wm.get("cached_input_price") or 0.0),
            "output_price": float(wm.get("output_price") or 0.0),
        }
        has_nonzero = any(rates.values())
        # Map under 'websearch' provider (and fill missing/zero 'gemini' or global fallback rates)
        existing_gem = pricing_map.get(("gemini", clean_id))
        if has_nonzero or not existing_gem:
            pricing_map[("websearch", mid)] = rates
            pricing_map[("websearch", clean_id)] = rates
        else:
            pricing_map[("websearch", mid)] = existing_gem
            pricing_map[("websearch", clean_id)] = existing_gem
        if not existing_gem or (has_nonzero and not any(existing_gem.values())):
            pricing_map[("gemini", clean_id)] = rates
        if clean_id not in model_fallback_map or (has_nonzero and not any(model_fallback_map[clean_id].values())):
            model_fallback_map[clean_id] = rates

    # Consolidate models that only differ by prefix (e.g. 'models/gemini-2.5-flash' vs 'gemini-2.5-flash')
    aggregated_models: Dict[tuple[str, str], Dict[str, Any]] = {}
    for r in rows:
        raw_mid = r["model_id"] or "default"
        pid = r["provider_id"] or "unknown"
        clean_mid = strip_model_prefix(raw_mid)
        key = (pid, clean_mid)
        if key not in aggregated_models:
            aggregated_models[key] = {
                "model_id": clean_mid,
                "provider_id": pid,
                "total_in": 0,
                "total_out": 0,
                "total_cached": 0,
            }
        aggregated_models[key]["total_in"] += (r["total_in"] or 0)
        aggregated_models[key]["total_out"] += (r["total_out"] or 0)
        aggregated_models[key]["total_cached"] += (r["total_cached"] or 0)

    per_model = []
    grand_in = 0
    grand_out = 0
    grand_cached = 0
    grand_cost = 0.0

    for (pid, mid), item in aggregated_models.items():
        tin = item["total_in"]
        tout = item["total_out"]
        tcached = item["total_cached"]
        tmiss = max(0, tin - tcached)
        hit_rate = round((tcached / tin * 100), 1) if tin > 0 else 0.0

        rates = (
            pricing_map.get((pid, mid))
            or model_fallback_map.get(mid)
            or {
                "input_price": 0.0,
                "cached_input_price": 0.0,
                "output_price": 0.0,
            }
        )
        in_price = rates["input_price"]
        cached_price = rates["cached_input_price"]
        out_price = rates["output_price"]

        # Prices are per 1M tokens ($ / 1,000,000 tokens); default is $0
        model_cost = (
            (tmiss / 1_000_000.0) * in_price
            + (tcached / 1_000_000.0) * cached_price
            + (tout / 1_000_000.0) * out_price
        )

        grand_in += tin
        grand_out += tout
        grand_cached += tcached
        grand_cost += model_cost

        per_model.append({
            "model_id": mid,
            "provider_id": pid,
            "prompt_tokens": tin,
            "completion_tokens": tout,
            "cached_tokens": tcached,
            "cache_miss_tokens": tmiss,
            "cache_hit_rate": hit_rate,
            "total_tokens": tin + tout,
            "cost": model_cost,
        })

    grand_miss = max(0, grand_in - grand_cached)
    grand_hit_rate = round((grand_cached / grand_in * 100), 1) if grand_in > 0 else 0.0

    return {
        "per_model": per_model,
        "grand_total": {
            "prompt_tokens": grand_in,
            "completion_tokens": grand_out,
            "cached_tokens": grand_cached,
            "cache_miss_tokens": grand_miss,
            "cache_hit_rate": grand_hit_rate,
            "total_tokens": grand_in + grand_out,
            "total_cost": grand_cost,
            "google_search_requests": conv_search_requests,
        },
        "google_search_totals": global_search_totals,
    }


def get_tabs_state() -> Optional[Dict[str, Any]]:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT state_json FROM tab_sessions WHERE id = 1")
        row = cursor.fetchone()
    if row:
        return json.loads(row["state_json"])
    return None


def save_tabs_state(state: Dict[str, Any]):
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO tab_sessions (id, state_json, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at
        """,
            (json.dumps(state), time.time()),
        )
        conn.commit()

