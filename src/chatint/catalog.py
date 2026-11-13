from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

PRIMARY_WEB_SEARCH_MODEL = "gemini-3.1-flash-lite"


def strip_model_prefix(model_id: str) -> str:
    val = model_id or ""
    return val[7:] if val.startswith("models/") else val


def is_gemini_3_or_newer(model_id: str) -> bool:
    """Return True if Gemini model ID is generation 3 or newer (or unversioned modern alias)."""
    mid = (model_id or "").lower()
    m_ver = re.search(r"gemini[-_](\d+)", mid)
    if m_ver:
        return int(m_ver.group(1)) >= 3
    return not any(legacy in mid for legacy in ("gemini-1", "gemini-2", "gemini/1", "gemini/2"))


def model_recency_key(m: Any) -> tuple:
    """
    Ranks model dictionaries or IDs by estimated recency (newer models first).
    Considers explicit creation timestamp, release date patterns (YYYY-MM-DD),
    version numbers (e.g. 4.5 > 3.8 > 2.5), and capability tiers.
    """
    if isinstance(m, dict):
        mid = (m.get("id") or "").lower()
        created = m.get("created", 0)
    else:
        mid = str(m or "").lower()
        created = 0

    if not isinstance(created, (int, float)):
        created = 0

    date_val = 0
    m_date = re.search(r"(202\d)[-_]?([01]\d)[-_]?([0-3]\d)", mid)
    if m_date:
        try:
            date_val = int(m_date.group(1) + m_date.group(2) + m_date.group(3))
        except ValueError:
            date_val = 0

    ver_val = 0.0
    m_dec = re.search(r"[-_v]?(\d+\.\d+)", mid)
    if m_dec:
        try:
            ver_val = float(m_dec.group(1))
        except ValueError:
            ver_val = 0.0
    else:
        m_dash = re.search(r"[-_](\d+)[-_](\d+)(?:[-_]|$)", mid)
        if m_dash and int(m_dash.group(1)) <= 10:
            ver_val = float(f"{m_dash.group(1)}.{m_dash.group(2)}")
        else:
            m_single = re.search(r"(?:gpt|gemini|claude|llama|deepseek|qwen|v)[-_]?(\d+)", mid)
            if m_single:
                try:
                    ver_val = float(m_single.group(1))
                except ValueError:
                    ver_val = 0.0

    bonus = 0
    if any(k in mid for k in ("o3", "o1", "r1", "reasoner")):
        bonus += 50
    if any(k in mid for k in ("4o", "4.5", "argon", "carbon")):
        bonus += 40
    if any(k in mid for k in ("latest", "preview")):
        bonus += 20
    if any(k in mid for k in ("deprecated", "legacy", "0301", "0314", "0613")):
        bonus -= 100
    if any(k in mid for k in ("embedding", "moderation", "tts", "whisper", "audio", "realtime")):
        bonus -= 500

    return (created, date_val, ver_val, bonus)


def sort_models_newer_first(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(models, key=model_recency_key, reverse=True)


def normalize_search_model_entry(m: Dict[str, Any]) -> Dict[str, Any]:
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
        entry = normalize_search_model_entry(raw)
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


def normalize_model_entry(m: Dict[str, Any]) -> Dict[str, Any]:
    mid = (m.get("id") or "").strip()
    entry: Dict[str, Any] = {
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
