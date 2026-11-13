"""Repository for AI providers, configured models, and persisted thinking level fallbacks."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from chatint.catalog import normalize_model_entry, strip_model_prefix
from chatint.config import DEFAULT_PROVIDERS
from chatint.logging_setup import get_logger
from chatint.storage.connection import open_db
from chatint.storage.secrets import (
    batch_update_provider_env_keys,
    mask_api_key,
    provider_env_key,
    read_env_dict,
    remove_api_key_from_env,
    sanitize_provider_name,
)

_logger = get_logger("chatint.storage.providers_repo")
BUILTIN_PROVIDER_ORDER = {p["id"]: idx for idx, p in enumerate(DEFAULT_PROVIDERS)}


def get_providers() -> List[Dict[str, Any]]:
    from chatint.client_manager import BackgroundModelSyncer, is_loopback_endpoint

    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT rowid, id, name, base_url, is_enabled, models_json FROM providers")
        rows = list(cursor.fetchall())

    rows.sort(key=lambda r: (BUILTIN_PROVIDER_ORDER.get(r["id"], 999), r["rowid"]))
    env_map = read_env_dict()

    providers = []
    for r in rows:
        pid = r["id"]
        pname = sanitize_provider_name(r["name"]) or "Provider"
        try:
            raw_models = json.loads(r["models_json"] or "[]")
        except Exception as exc:
            _logger.warning("Malformed models_json on provider %s: %s", pid, exc)
            raw_models = []
        models = [normalize_model_entry(m) for m in raw_models if isinstance(m, dict) and m.get("id")]
        env_key_name = provider_env_key(pname)
        raw_env_key = "" if env_key_name == "_API_KEY" else env_map.get(env_key_name, "")
        has_api_key = bool(raw_env_key)
        is_enabled = bool(r["is_enabled"])
        is_verified = BackgroundModelSyncer.is_provider_verified(pid) if has_api_key else False
        verified_ids = BackgroundModelSyncer.get_verified_model_ids(pid) if is_verified else set()
        sync_status, sync_error = BackgroundModelSyncer.get_sync_status(
            pid, has_api_key=has_api_key, is_enabled=is_enabled
        )
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
    normalized_models = [normalize_model_entry(m) for m in (models or []) if isinstance(m, dict) and m.get("id")]
    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE providers SET models_json = ? WHERE id = ?",
            (json.dumps(normalized_models), provider_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def save_providers(providers: List[Dict[str, Any]], prune_missing: bool = True) -> None:
    from chatint.client_manager import BackgroundModelSyncer, is_loopback_endpoint

    env_updates: List[Dict[str, Any]] = []
    env_removals: List[str] = []

    with open_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, base_url, models_json FROM providers")
        existing_rows = {r["id"]: r for r in cursor.fetchall()}

        if prune_missing:
            submitted_ids = {p["id"] for p in providers if isinstance(p, dict) and p.get("id")}
            for old_id, old_r in existing_rows.items():
                if old_id not in submitted_ids and old_id not in BUILTIN_PROVIDER_ORDER:
                    if old_r["name"]:
                        env_removals.append(old_r["name"])
                    cursor.execute("DELETE FROM providers WHERE id = ?", (old_id,))

        for p in providers:
            pid = p["id"]
            old_row = existing_rows.get(pid)
            old_name = old_row["name"] if old_row else None
            clean_name = (
                sanitize_provider_name(p.get("name", ""))
                or sanitize_provider_name(old_name or "")
                or "Provider"
            )

            raw_new_key = p.get("api_key")
            has_new_key = raw_new_key is not None and str(raw_new_key).strip() != ""
            should_clear = bool(p.get("clear_api_key")) and not has_new_key

            if has_new_key:
                env_updates.append({
                    "provider_name": clean_name,
                    "api_key": str(raw_new_key),
                    "old_provider_name": old_name,
                    "clear": False,
                })
            elif should_clear:
                env_updates.append({
                    "provider_name": clean_name,
                    "api_key": "",
                    "old_provider_name": old_name,
                    "clear": True,
                })
            else:
                is_new_loopback = old_row is None and is_loopback_endpoint(str(p.get("base_url") or ""))
                env_updates.append({
                    "provider_name": clean_name,
                    "api_key": None,
                    "old_provider_name": old_name,
                    "clear": False,
                    "default_if_missing": "ollama" if (clean_name.lower() == "ollama" or is_new_loopback) else "",
                })

            normalized_models = [normalize_model_entry(m) for m in (p.get("models") or []) if isinstance(m, dict) and m.get("id")]
            if p.get("models") is not None:
                models_json_str = json.dumps(normalized_models)
            elif old_row and old_row["models_json"]:
                models_json_str = old_row["models_json"]
            else:
                models_json_str = "[]"

            is_native_sdk = pid.lower() in ("anthropic", "gemini") or clean_name.lower() in ("anthropic", "gemini")
            base_url_val = "" if is_native_sdk else str(p.get("base_url") or "").strip()

            key_changed = has_new_key or should_clear
            url_changed = bool(old_row and not is_native_sdk and (base_url_val != old_row["base_url"]))
            if key_changed or url_changed:
                BackgroundModelSyncer.clear_provider_verified(pid)
                if should_clear:
                    BackgroundModelSyncer.set_sync_status(pid, "no_key", "NO API KEY")
                else:
                    BackgroundModelSyncer.set_sync_status(pid, "checking", "CHECKING...")

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

    if env_updates or env_removals:
        batch_update_provider_env_keys(env_updates, removals=env_removals)


def delete_provider(provider_id: str) -> None:
    if provider_id in BUILTIN_PROVIDER_ORDER:
        return
    with open_db() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
        if row and row["name"]:
            remove_api_key_from_env(row["name"])
        cursor.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        conn.commit()


def update_model_resolved_thinking(
    provider_id: str, model_id: str, requested_level: str, resolved_level: str
) -> None:
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
        except Exception as exc:
            _logger.debug("Could not parse models_json in update_model_resolved_thinking: %s", exc)
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
    except Exception as exc:
        _logger.debug("Could not parse models_json in get_model_resolved_thinking: %s", exc)
    return None


def remove_model_resolved_thinking(
    provider_id: str,
    model_id: Optional[str] = None,
    requested_level: Optional[str] = None,
) -> None:
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
        except Exception as exc:
            _logger.debug("Could not update models_json in remove_model_resolved_thinking: %s", exc)
