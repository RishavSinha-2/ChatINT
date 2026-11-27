"""Repository for durable token_ledger and Google Search request accounting."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Optional

from chatint.catalog import strip_model_prefix
from chatint.logging_setup import get_logger
from chatint.storage.connection import (
    get_conv_db_size_bytes,
    get_conv_lock,
    open_conv_db,
    open_db,
    validate_conv_id,
)
from chatint.storage.settings_repo import get_web_search_models

_logger = get_logger("chatint.storage.ledger_repo")


def record_google_search_requests(query_count: int) -> None:
    """Increment the durable monthly Google Search request counter."""
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
    ddg_search_requests: int = 0,
    provider_name: str = "",
    cost_usd: Optional[float] = None,
) -> bool:
    g_cnt = max(0, int(google_search_requests or 0))
    ddg_cnt = max(0, int(ddg_search_requests or 0))
    clean_cost = float(cost_usd) if cost_usd is not None else None
    if not conversation_id or (
        prompt_tokens == 0
        and completion_tokens == 0
        and cached_tokens == 0
        and g_cnt == 0
        and ddg_cnt == 0
        and clean_cost is None
    ):
        return False
    try:
        validate_conv_id(conversation_id)
    except ValueError:
        return False
    try:
        with get_conv_lock(conversation_id):
            with open_db() as conn:
                cursor = conn.cursor()
                if not cursor.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
                    return False
                resolved_prov_name = (provider_name or "").strip()
                if not resolved_prov_name and provider_id:
                    prov_row = cursor.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
                    if prov_row and prov_row["name"]:
                        resolved_prov_name = prov_row["name"]

            now = time.time()
            with open_conv_db(conversation_id) as cconn:
                ccur = cconn.cursor()
                ccur.execute(
                    """
                    INSERT INTO token_ledger (
                        conversation_id, provider_id, provider_name, model_id,
                        prompt_tokens, completion_tokens, cached_tokens,
                        google_search_requests, ddg_search_requests, cost_usd, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        provider_id or "",
                        resolved_prov_name,
                        model_id or "",
                        prompt_tokens,
                        completion_tokens,
                        cached_tokens,
                        g_cnt,
                        ddg_cnt,
                        clean_cost,
                        now,
                    ),
                )
                cconn.commit()

            size_bytes = get_conv_db_size_bytes(conversation_id)
            with open_db() as conn:
                conn.execute("UPDATE conversations SET db_size_bytes = ? WHERE id = ?", (size_bytes, conversation_id))
                conn.commit()
            return True
    except sqlite3.Error:
        _logger.debug("Skipped token_ledger insert; conversation %s could not be updated", conversation_id)
        return False


def get_token_stats(conversation_id: str) -> Dict[str, Any]:
    """
    Return aggregated token stats and estimated cost per model and grand total for the conversation.
    Uses token_ledger so token and cost counts are never lost when branching, editing, or regenerating.
    """
    global_search_totals = get_google_search_request_totals()
    empty_stats = {
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
            "ddg_search_requests": 0,
            "web_search_requests": 0,
        },
        "active_turn_tokens": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        },
        "google_search_totals": global_search_totals,
    }
    if not conversation_id:
        return empty_stats
    try:
        validate_conv_id(conversation_id)
    except ValueError:
        return empty_stats

    with get_conv_lock(conversation_id):
        with open_db() as conn:
            cursor = conn.cursor()
            conv_row = cursor.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conv_row:
                return empty_stats
            is_imported = bool(conv_row["is_imported"])
            prov_rows = cursor.execute("SELECT id, name, models_json FROM providers").fetchall()
            conv_meta = {k: conv_row[k] for k in conv_row.keys()}

        with open_conv_db(conversation_id, meta=conv_meta) as cconn:
            ccur = cconn.cursor()
            conv_search_row = ccur.execute(
                """
                SELECT COALESCE(SUM(google_search_requests), 0) as conv_google_searches,
                       COALESCE(SUM(ddg_search_requests), 0) as conv_ddg_searches
                FROM token_ledger
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            conv_google_requests = int((conv_search_row["conv_google_searches"] if conv_search_row else 0) or 0)
            conv_ddg_requests = int((conv_search_row["conv_ddg_searches"] if conv_search_row else 0) or 0)
            conv_web_requests = conv_google_requests + conv_ddg_requests

            ccur.execute(
                """
                SELECT model_id, provider_id,
                       MAX(provider_name) as ledger_provider_name,
                       SUM(prompt_tokens) as total_in,
                       SUM(completion_tokens) as total_out,
                       SUM(cached_tokens) as total_cached,
                       SUM(CASE WHEN cost_usd IS NULL THEN prompt_tokens ELSE 0 END) as live_in,
                       SUM(CASE WHEN cost_usd IS NULL THEN completion_tokens ELSE 0 END) as live_out,
                       SUM(CASE WHEN cost_usd IS NULL THEN cached_tokens ELSE 0 END) as live_cached,
                       COALESCE(SUM(cost_usd), 0.0) as fixed_cost
                FROM token_ledger
                WHERE conversation_id = ? AND (model_id != '' OR prompt_tokens > 0)
                GROUP BY model_id, provider_id
                """,
                (conversation_id,),
            )
            rows = ccur.fetchall()

            # Fallback to messages table only for historical pre-ledger non-imported conversations
            if not rows and not is_imported:
                ccur.execute(
                    """
                    SELECT model_id, provider_id,
                           MAX(provider_name) as ledger_provider_name,
                           SUM(prompt_tokens) as total_in,
                           SUM(completion_tokens) as total_out,
                           SUM(cached_tokens) as total_cached,
                           SUM(prompt_tokens) as live_in,
                           SUM(completion_tokens) as live_out,
                           SUM(cached_tokens) as live_cached,
                           0.0 as fixed_cost
                    FROM messages
                    WHERE conversation_id = ? AND role = 'assistant' AND (model_id != '' OR prompt_tokens > 0)
                    GROUP BY model_id, provider_id
                    """,
                    (conversation_id,),
                )
                rows = ccur.fetchall()

            ccur.execute(
                """
                SELECT COALESCE(SUM(prompt_tokens), 0) as act_in,
                       COALESCE(SUM(completion_tokens), 0) as act_out,
                       COALESCE(SUM(cached_tokens), 0) as act_cached
                FROM messages
                WHERE conversation_id = ? AND role = 'assistant'
                """,
                (conversation_id,),
            )
            act_row = ccur.fetchone()
            active_in = int((act_row["act_in"] if act_row else 0) or 0)
            active_out = int((act_row["act_out"] if act_row else 0) or 0)
            active_cached = int((act_row["act_cached"] if act_row else 0) or 0)
            active_turn_tokens = {
                "prompt_tokens": active_in,
                "completion_tokens": active_out,
                "cached_tokens": active_cached,
                "total_tokens": active_in + active_out,
            }

    provider_name_map: Dict[str, str] = {}
    pricing_map: Dict[tuple[str, str], Dict[str, float]] = {}
    model_fallback_map: Dict[str, Dict[str, float]] = {}
    for pr in prov_rows:
        pid = pr["id"]
        if pr["name"]:
            provider_name_map[pid] = pr["name"]
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
        except Exception as exc:
            _logger.debug("Skipping malformed models_json during pricing lookup on %s: %s", pid, exc)

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
                "provider_name": "",
                "total_in": 0,
                "total_out": 0,
                "total_cached": 0,
                "live_in": 0,
                "live_out": 0,
                "live_cached": 0,
                "fixed_cost": 0.0,
            }
        if not aggregated_models[key]["provider_name"] and r["ledger_provider_name"]:
            aggregated_models[key]["provider_name"] = str(r["ledger_provider_name"]).strip()
        aggregated_models[key]["total_in"] += r["total_in"] or 0
        aggregated_models[key]["total_out"] += r["total_out"] or 0
        aggregated_models[key]["total_cached"] += r["total_cached"] or 0
        aggregated_models[key]["live_in"] += r["live_in"] or 0
        aggregated_models[key]["live_out"] += r["live_out"] or 0
        aggregated_models[key]["live_cached"] += r["live_cached"] or 0
        aggregated_models[key]["fixed_cost"] += float(r["fixed_cost"] or 0.0)

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

        live_in = item["live_in"]
        live_cached = item["live_cached"]
        live_miss = max(0, live_in - live_cached)
        live_out = item["live_out"]
        model_cost = (
            (live_miss / 1_000_000.0) * in_price
            + (live_cached / 1_000_000.0) * cached_price
            + (live_out / 1_000_000.0) * out_price
            + item["fixed_cost"]
        )

        grand_in += tin
        grand_out += tout
        grand_cached += tcached
        grand_cost += model_cost

        per_model.append({
            "model_id": mid,
            "provider_id": pid,
            "provider_name": provider_name_map.get(pid, "") or item["provider_name"],
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
            "google_search_requests": conv_google_requests,
            "ddg_search_requests": conv_ddg_requests,
            "web_search_requests": conv_web_requests,
        },
        "active_turn_tokens": active_turn_tokens,
        "google_search_totals": global_search_totals,
    }
