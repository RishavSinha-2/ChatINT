import json
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from chatint.config import DB_PATH, DEFAULT_PROVIDERS, ENV_PATH

_ENV_LOCK = threading.Lock()


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


def ensure_env_file():
    if not ENV_PATH.exists() or ENV_PATH.read_text(encoding="utf-8").strip() == "":
        lines = []
        for p in DEFAULT_PROVIDERS:
            clean = sanitize_provider_name(p.get("name", ""))
            if clean:
                default_val = "ollama" if clean.lower() == "ollama" else (p.get("api_key") or "")
                lines.append(f"{clean.upper()}_API_KEY={default_val}")
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
                    appended.append(f"{env_k}={default_val}")
        if appended:
            prefix = raw_text if raw_text.endswith("\n") else (raw_text + "\n")
            ENV_PATH.write_text(prefix + "\n".join(appended) + "\n", encoding="utf-8")


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
            v = v.strip()
            if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
                v = v[1:-1]
            env_map[k] = v
        return env_map


def write_env_dict(env_map: Dict[str, str]):
    with _ENV_LOCK:
        ensure_env_file()
        lines = [f"{k}={v}" for k, v in env_map.items()]
        content = "\n".join(lines) + ("\n" if lines else "")
        ENV_PATH.write_text(content, encoding="utf-8")


def get_api_key_from_env(provider_name: str) -> str:
    key_name = provider_env_key(provider_name)
    if key_name == "_API_KEY":
        return ""
    env_map = read_env_dict()
    return env_map.get(key_name, "")


def set_api_key_in_env(provider_name: str, api_key: str, old_provider_name: Optional[str] = None):
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
    env_map = read_env_dict()
    key_name = provider_env_key(provider_name)
    if key_name in env_map:
        env_map.pop(key_name, None)
        write_env_dict(env_map)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

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
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
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
            default_env_key = get_api_key_from_env(clean_name) or ("ollama" if clean_name.lower() == "ollama" else (p.get("api_key") or ""))
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
            # Append letters (A, B, C...) if duplicate
            letter = chr(ord("A") + (suffix_idx % 26))
            clean_name = f"{base_clean}{letter}"
            suffix_idx += 1
        seen_upper_names.add(clean_name.upper())

        db_key = (r["api_key"] or "").strip()
        env_key_val = get_api_key_from_env(clean_name) or (get_api_key_from_env(raw_name) if raw_name != clean_name else "")
        final_key = env_key_val or db_key or ("ollama" if clean_name.lower() == "ollama" else "")
        set_api_key_in_env(clean_name, final_key, old_provider_name=raw_name if raw_name != clean_name else None)

        cursor.execute(
            "UPDATE providers SET name = ?, api_key = '' WHERE id = ?",
            (clean_name, pid),
        )

    # Clear models in DB for any provider that has no API key in .env
    cursor.execute("SELECT id, name FROM providers")
    for r in cursor.fetchall():
        if not get_api_key_from_env(r["name"]):
            cursor.execute("UPDATE providers SET models_json = '[]' WHERE id = ?", (r["id"],))

    # Seed default app settings if none exist
    cursor.execute("SELECT COUNT(*) FROM app_settings")
    if cursor.fetchone()[0] == 0:
        cursor.executemany(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
        """,
            [
                ("theme", "dark"),
                ("global_system_prompt", "You are a helpful, brilliant, and precise assistant."),
                ("default_provider", "openai"),
                ("default_model", "gpt-4o"),
            ],
        )

    conn.commit()
    conn.close()


def _normalize_model_entry(m: Dict[str, Any]) -> Dict[str, Any]:
    mid = (m.get("id") or "").strip()
    return {
        "id": mid,
        "name": (m.get("name") or mid).strip(),
        "supports_thinking": 1 if m.get("supports_thinking") else 0,
        "input_price": float(m.get("input_price") or 0.0),
        "cached_input_price": float(m.get("cached_input_price") or 0.0),
        "output_price": float(m.get("output_price") or 0.0),
    }


_BUILTIN_PROVIDER_ORDER = {p["id"]: idx for idx, p in enumerate(DEFAULT_PROVIDERS)}


def get_providers() -> List[Dict[str, Any]]:
    from chatint.client_manager import BackgroundModelSyncer

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT rowid, id, name, base_url, is_enabled, models_json FROM providers")
    rows = list(cursor.fetchall())
    conn.close()

    rows.sort(key=lambda r: (_BUILTIN_PROVIDER_ORDER.get(r["id"], 999), r["rowid"]))

    providers = []
    for r in rows:
        pid = r["id"]
        pname = sanitize_provider_name(r["name"]) or "Provider"
        raw_models = json.loads(r["models_json"] or "[]")
        models = [_normalize_model_entry(m) for m in raw_models if m.get("id")]
        has_api_key = bool(get_api_key_from_env(pname))
        is_enabled = bool(r["is_enabled"])
        is_verified = BackgroundModelSyncer.is_provider_verified(pid) if has_api_key else False
        verified_ids = BackgroundModelSyncer.get_verified_model_ids(pid) if is_verified else set()
        effective_models = [m for m in models if m.get("id") in verified_ids] if is_verified else []
        providers.append({
            "id": pid,
            "name": pname,
            "base_url": r["base_url"],
            "has_api_key": has_api_key,
            "is_enabled": is_enabled,
            "models": effective_models,
            "fetched_model_ids": sorted(list(verified_ids)),
            "is_accessible": bool(is_enabled and has_api_key and is_verified and len(effective_models) > 0),
        })
    return providers


def save_providers(providers: List[Dict[str, Any]]):
    from chatint.client_manager import BackgroundModelSyncer

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id, name, models_json FROM providers")
    existing_rows = {r["id"]: r for r in cursor.fetchall()}

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
        has_api_key = bool(get_api_key_from_env(clean_name))
        if (
            not normalized_models
            and has_api_key
            and not p.get("clear_api_key")
            and not BackgroundModelSyncer.is_provider_verified(pid)
            and old_row
            and old_row["models_json"]
        ):
            # Preserve existing stored models in DB if provider simply hasn't finished background verification yet
            models_json_str = old_row["models_json"]
        elif not has_api_key:
            models_json_str = "[]"
        else:
            models_json_str = json.dumps(normalized_models)

        is_native_sdk = pid.lower() in ("anthropic", "gemini") or clean_name.lower() in ("anthropic", "gemini")
        base_url_val = "" if is_native_sdk else p.get("base_url", "")

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
    conn.close()


def get_app_settings() -> Dict[str, str]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM app_settings")
    rows = cursor.fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def save_app_settings(settings: Dict[str, str]):
    conn = get_connection()
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
    conn.close()


def create_conversation(
    title: str = "New Chat", system_prompt: str = "", max_context: int = 30
) -> Dict[str, Any]:
    conn = get_connection()
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
    conn.close()
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.title, c.system_prompt, c.max_context_messages, c.created_at, c.updated_at,
               COUNT(m.id) as message_count
        FROM conversations c
        INNER JOIN messages m ON c.id = m.conversation_id
        WHERE m.role = 'assistant' AND (LENGTH(TRIM(m.content)) > 0 OR LENGTH(TRIM(m.thinking_content)) > 0)
        GROUP BY c.id
        ORDER BY c.updated_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "title": r["title"],
            "system_prompt": r["system_prompt"],
            "max_context_messages": r["max_context_messages"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "message_count": r["message_count"],
        }
        for r in rows
    ]


def delete_provider(provider_id: str):
    if provider_id in _BUILTIN_PROVIDER_ORDER:
        return
    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
    if row and row["name"]:
        remove_api_key_from_env(row["name"])
    cursor.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
    conn.commit()
    conn.close()


def get_conversation(conv_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    conv_row = cursor.fetchone()
    if not conv_row:
        conn.close()
        return None

    cursor.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC", (conv_id,)
    )
    msg_rows = cursor.fetchall()
    conn.close()

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
        "created_at": conv_row["created_at"],
        "updated_at": conv_row["updated_at"],
        "messages": messages,
    }


def update_conversation(conv_id: str, **kwargs):
    conn = get_connection()
    cursor = conn.cursor()
    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        values.append(v)
    values.append(time.time())
    values.append(conv_id)
    query = f"UPDATE conversations SET {', '.join(fields)}, updated_at = ? WHERE id = ?"
    cursor.execute(query, tuple(values))
    conn.commit()
    conn.close()


def delete_conversation(conv_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    cursor.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    conn.commit()
    conn.close()


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
) -> Dict[str, Any]:
    conn = get_connection()
    cursor = conn.cursor()
    if not msg_id:
        msg_id = str(uuid.uuid4())
    now = time.time()

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

    conn.commit()
    conn.close()

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


def update_message(msg_id: str, **kwargs):
    conn = get_connection()
    cursor = conn.cursor()
    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        values.append(v)
    values.append(msg_id)
    query = f"UPDATE messages SET {', '.join(fields)} WHERE id = ?"
    cursor.execute(query, tuple(values))
    conn.commit()
    conn.close()


def delete_messages_from(conversation_id: str, from_msg_id: str, include_self: bool = True):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rowid, created_at FROM messages WHERE id = ? AND conversation_id = ?",
        (from_msg_id, conversation_id),
    )
    target = cursor.fetchone()
    if target:
        target_rowid = target["rowid"]
        target_time = target["created_at"]
        if include_self:
            cursor.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND (created_at > ? OR (created_at = ? AND rowid >= ?))",
                (conversation_id, target_time, target_time, target_rowid),
            )
        else:
            cursor.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND (created_at > ? OR (created_at = ? AND rowid > ?))",
                (conversation_id, target_time, target_time, target_rowid),
            )
        cursor.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (time.time(), conversation_id)
        )
        conn.commit()
    conn.close()


def get_token_stats(conversation_id: str) -> Dict[str, Any]:
    """
    Returns aggregated token stats and estimated cost per model and grand total for the conversation.
    """
    conn = get_connection()
    cursor = conn.cursor()
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
    conn.close()

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
                    pricing_map[(pid, mid)] = rates
                    model_fallback_map[mid] = rates
        except Exception:
            pass

    per_model = []
    grand_in = 0
    grand_out = 0
    grand_cached = 0
    grand_cost = 0.0

    for r in rows:
        mid = r["model_id"] or "default"
        pid = r["provider_id"] or "unknown"
        tin = r["total_in"] or 0
        tout = r["total_out"] or 0
        tcached = r["total_cached"] or 0
        tmiss = max(0, tin - tcached)
        hit_rate = round((tcached / tin * 100), 1) if tin > 0 else 0.0

        rates = pricing_map.get((pid, mid)) or model_fallback_map.get(mid) or {
            "input_price": 0.0,
            "cached_input_price": 0.0,
            "output_price": 0.0,
        }
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
        },
    }


def get_tabs_state() -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT state_json FROM tab_sessions WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return json.loads(row["state_json"])
    return None


def save_tabs_state(state: Dict[str, Any]):
    conn = get_connection()
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
    conn.close()

