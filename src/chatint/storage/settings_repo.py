"""Repository for app_settings, web_search_models, and persisted tab_sessions."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import time
from typing import Any, Dict, List, Optional

from chatint.catalog import normalize_web_search_models
from chatint.logging_setup import get_logger
from chatint.storage.connection import open_db

_logger = get_logger("chatint.storage.settings_repo")

ALLOWED_APP_SETTING_KEYS = {
    "theme",
    "global_system_prompt",
    "default_provider",
    "default_model",
    "default_thinking",
    "web_search_models",
    "use_gemini_web_search",
    "default_workspace_dir",
    "default_agent_mode",
    "max_chat_size_mb",
    "max_total_chats_size_mb",
}
_VALID_THINKING_LEVELS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
MAX_SETTING_VALUE_CHARS = 65_536
_BYTES_PER_MB = Decimal(1_048_576)


def normalize_storage_limit_mb(value: Any, setting_name: str = "storage limit") -> str:
    """
    Normalize a storage quota setting (in MB) with no upper architectural cap.
    Accepts any finite non-negative number (int/decimal string) or '0'/'unlimited'/'no limit'.
    """
    raw = "" if value is None else str(value).strip()
    if not raw or raw.lower() in ("0", "none", "unlimited", "no limit", "nolimit", "off"):
        return "0"
    try:
        dec = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid value '{raw}' for {setting_name}.") from exc
    if not dec.is_finite() or dec < 0:
        raise ValueError(f"Storage limit '{setting_name}' must be 0 ('No limit') or a positive number.")
    if dec == 0:
        return "0"
    norm = dec.normalize()
    text = format(norm, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def mb_setting_to_bytes(value: Any) -> int:
    """Convert a normalized MB quota string to exact integer bytes (`0` = No limit)."""
    try:
        clean = normalize_storage_limit_mb(value)
    except ValueError:
        return 0
    if clean == "0":
        return 0
    return int((Decimal(clean) * _BYTES_PER_MB).to_integral_value())


def get_app_settings() -> Dict[str, str]:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM app_settings")
        rows = cursor.fetchall()
    return {r["key"]: r["value"] for r in rows}


def save_app_settings(settings: Dict[str, Any]) -> None:
    if not isinstance(settings, dict):
        raise ValueError("settings must be a dictionary.")
    invalid_keys = [str(k) for k in settings.keys() if k not in ALLOWED_APP_SETTING_KEYS]
    if invalid_keys:
        raise ValueError(f"Unsupported app setting key(s): {', '.join(invalid_keys)}")

    normalized_pairs: List[tuple[str, str]] = []
    for k, raw_v in settings.items():
        val = "" if raw_v is None else str(raw_v)
        if len(val) > MAX_SETTING_VALUE_CHARS:
            raise ValueError(f"Setting '{k}' exceeds maximum allowed length ({MAX_SETTING_VALUE_CHARS} chars).")
        if k == "theme":
            val = "light" if val.strip().lower() == "light" else "dark"
        elif k == "default_thinking":
            low = val.strip().lower()
            val = low if low in _VALID_THINKING_LEVELS else "medium"
        elif k == "use_gemini_web_search":
            val = "true" if val.strip().lower() in ("1", "true", "yes", "on") else "false"
        elif k == "default_agent_mode":
            val = "auto" if val.strip().lower() == "auto" else "manual"
        elif k in ("max_chat_size_mb", "max_total_chats_size_mb"):
            val = normalize_storage_limit_mb(val, setting_name=k)
        elif k in ("default_provider", "default_model", "default_workspace_dir"):
            val = val.strip()
        normalized_pairs.append((k, val))

    if not normalized_pairs:
        return

    with open_db() as conn:
        cursor = conn.cursor()
        for k, val in normalized_pairs:
            cursor.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (k, val),
            )
        conn.commit()


def get_web_search_models() -> List[Dict[str, Any]]:
    settings = get_app_settings()
    raw = settings.get("web_search_models") or "[]"
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        _logger.debug("Failed to parse web_search_models JSON; resetting defaults: %s", exc)
        parsed = []
    return normalize_web_search_models(parsed if isinstance(parsed, list) else [])


def save_web_search_models(models: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    normalized = normalize_web_search_models(models)
    save_app_settings({"web_search_models": json.dumps(normalized)})
    return normalized


def get_tabs_state() -> Optional[Dict[str, Any]]:
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT state_json FROM tab_sessions WHERE id = 1")
        row = cursor.fetchone()
    if row and row["state_json"]:
        try:
            parsed = json.loads(row["state_json"])
            return parsed if isinstance(parsed, dict) else None
        except Exception as exc:
            _logger.warning("Corrupted tab_sessions JSON ignored: %s", exc)
    return None


def save_tabs_state(state: Dict[str, Any]) -> None:
    if not isinstance(state, dict):
        return
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
