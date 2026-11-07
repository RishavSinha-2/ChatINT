import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional
from chatint.config import DB_PATH, DEFAULT_PROVIDERS


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

    # Reset any unverified models for providers that have no API key
    cursor.execute("UPDATE providers SET models_json = '[]' WHERE (api_key = '' OR api_key IS NULL)")

    # Seed default providers if none exist
    cursor.execute("SELECT COUNT(*) FROM providers")
    if cursor.fetchone()[0] == 0:
        for p in DEFAULT_PROVIDERS:
            cursor.execute(
                """
                INSERT INTO providers (id, name, base_url, api_key, is_enabled, models_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    p["id"],
                    p["name"],
                    p["base_url"],
                    p["api_key"],
                    p["is_enabled"],
                    json.dumps(p["models"]),
                ),
            )

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


def get_providers() -> List[Dict[str, Any]]:
    from chatint.client_manager import BackgroundModelSyncer

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, base_url, api_key, is_enabled, models_json FROM providers")
    rows = cursor.fetchall()
    conn.close()

    providers = []
    for r in rows:
        pid = r["id"]
        models = json.loads(r["models_json"])
        api_key = (r["api_key"] or "").strip()
        has_api_key = bool(api_key)
        is_enabled = bool(r["is_enabled"])
        is_verified = BackgroundModelSyncer.is_provider_verified(pid) if has_api_key else False
        verified_ids = BackgroundModelSyncer.get_verified_model_ids(pid) if is_verified else set()
        effective_models = [m for m in models if m.get("id") in verified_ids] if is_verified else []
        providers.append({
            "id": pid,
            "name": r["name"],
            "base_url": r["base_url"],
            "api_key": r["api_key"],
            "is_enabled": is_enabled,
            "models": effective_models,
            "fetched_model_ids": sorted(list(verified_ids)),
            "is_accessible": bool(is_enabled and has_api_key and is_verified and len(effective_models) > 0),
        })
    return providers


def save_providers(providers: List[Dict[str, Any]]):
    conn = get_connection()
    cursor = conn.cursor()
    for p in providers:
        cursor.execute(
            """
            INSERT INTO providers (id, name, base_url, api_key, is_enabled, models_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                base_url=excluded.base_url,
                api_key=excluded.api_key,
                is_enabled=excluded.is_enabled,
                models_json=excluded.models_json
        """,
            (
                p["id"],
                p["name"],
                p["base_url"],
                p.get("api_key", ""),
                1 if p.get("is_enabled", True) else 0,
                json.dumps(p.get("models", [])),
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
    conn = get_connection()
    cursor = conn.cursor()
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
    Returns aggregated token stats per model and grand total for the conversation.
    Input tokens, output tokens, cache hits, cache misses per model!
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
    conn.close()

    per_model = []
    grand_in = 0
    grand_out = 0
    grand_cached = 0

    for r in rows:
        tin = r["total_in"] or 0
        tout = r["total_out"] or 0
        tcached = r["total_cached"] or 0
        tmiss = max(0, tin - tcached)
        hit_rate = round((tcached / tin * 100), 1) if tin > 0 else 0.0

        grand_in += tin
        grand_out += tout
        grand_cached += tcached

        per_model.append({
            "model_id": r["model_id"] or "default",
            "provider_id": r["provider_id"] or "unknown",
            "prompt_tokens": tin,
            "completion_tokens": tout,
            "cached_tokens": tcached,
            "cache_miss_tokens": tmiss,
            "cache_hit_rate": hit_rate,
            "total_tokens": tin + tout,
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

