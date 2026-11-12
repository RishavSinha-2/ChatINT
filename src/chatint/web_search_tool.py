from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from chatint.client_manager import BackgroundModelSyncer, ClientManager, strip_model_prefix
from chatint.db import (
    PRIMARY_WEB_SEARCH_MODEL,
    get_api_key_from_env,
    get_web_search_models,
    record_google_search_requests,
    record_token_usage,
)

SEARCH_MODEL_ID = PRIMARY_WEB_SEARCH_MODEL
WEB_SEARCH_TOOL_NAME = "web_search"
MAX_TOOL_ROUNDS = 5

_TOOL_DESCRIPTION = (
    "Search the live web using Google Search for current events, real-time data, recent facts, "
    "prices, versions, or facts you are uncertain about. Provide a concise search query."
)

_TOOL_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Concise search query to look up on Google Search.",
        }
    },
    "required": ["query"],
}


def get_web_search_system_hint() -> str:
    """Short hint appended to the chat model's system prompt only when Web Search is enabled."""
    today = time.strftime("%Y-%m-%d")
    return (
        f"Today's date is {today}. You have access to the `{WEB_SEARCH_TOOL_NAME}` tool for live Google Search "
        f"(limit: up to {MAX_TOOL_ROUNDS} tool-call rounds per response, so batch or combine queries efficiently). "
        "Use it whenever a question requires current events, recent updates, prices, versions, or verifiable real-time facts; "
        "do not guess live facts when unsure. When citing findings, mention relevant details and source links returned by the tool."
    )


def get_openai_tool_spec() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": WEB_SEARCH_TOOL_NAME,
            "description": _TOOL_DESCRIPTION,
            "parameters": _TOOL_PARAMETERS,
        },
    }


def get_anthropic_tool_spec() -> Dict[str, Any]:
    return {
        "name": WEB_SEARCH_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "input_schema": _TOOL_PARAMETERS,
    }


def get_gemini_tool_declaration() -> Any:
    from google.genai import types as genai_types

    return genai_types.FunctionDeclaration(
        name=WEB_SEARCH_TOOL_NAME,
        description=_TOOL_DESCRIPTION,
        parameters_json_schema=_TOOL_PARAMETERS,
    )


def is_web_search_available() -> tuple[bool, str]:
    """Check whether Gemini credentials and at least one configured Web Search model are ready."""
    provider = ClientManager.get_provider("gemini")
    if not provider:
        return False, "NO API KEY"
    is_enabled = bool(provider.get("is_enabled", True))
    has_key = bool(get_api_key_from_env(provider.get("name", "Gemini")))
    status, brief = BackgroundModelSyncer.get_sync_status("gemini", has_api_key=has_key, is_enabled=is_enabled)
    if status in ("no_key", "disabled", "invalid_key", "invalid_model", "network_error", "error"):
        return False, brief or "UNAVAILABLE"

    if BackgroundModelSyncer.is_provider_verified("gemini"):
        verified_ids = BackgroundModelSyncer.get_verified_model_ids("gemini")
        clean_ids = {strip_model_prefix(mid).lower() for mid in verified_ids}
        configured_ids = [strip_model_prefix(m.get("id", "")).lower() for m in get_web_search_models() if m.get("id")]
        if not any(cid in clean_ids for cid in configured_ids):
            return False, "INVALID MODEL"
    return True, ""


def _build_search_configs(genai_types: Any, model_id: str, search_sys_prompt: str, search_tool: Any) -> List[Any]:
    """Build ordered minimal-thinking GenerateContentConfig attempts for a Gemini search model."""
    m_ver = re.search(r"gemini[-_](\d+)", (model_id or "").lower())
    if m_ver:
        is_gemini_3_or_newer = int(m_ver.group(1)) >= 3
    else:
        is_gemini_3_or_newer = not any(legacy in (model_id or "").lower() for legacy in ("gemini-1", "gemini-2", "gemini/1", "gemini/2"))

    # Hardcode minimal thinking per Gemini generation; keep plain/bullet output so google_search is not suppressed.
    level_cfg = genai_types.GenerateContentConfig(
        system_instruction=search_sys_prompt,
        tools=[search_tool],
        temperature=0.1,
        max_output_tokens=600,
        thinking_config=genai_types.ThinkingConfig(thinking_level="MINIMAL"),
    )
    budget_cfg = genai_types.GenerateContentConfig(
        system_instruction=search_sys_prompt,
        tools=[search_tool],
        temperature=0.1,
        max_output_tokens=600,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=512),
    )
    plain_cfg = genai_types.GenerateContentConfig(
        system_instruction=search_sys_prompt,
        tools=[search_tool],
        temperature=0.1,
        max_output_tokens=600,
    )
    return [level_cfg, budget_cfg, plain_cfg] if is_gemini_3_or_newer else [budget_cfg, level_cfg, plain_cfg]


def _extract_concise_facts(raw_text: str) -> tuple[str, List[str]]:
    """Parse concise bullet points (or JSON if returned) into clean summary + fact list."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            summary = str(parsed.get("answer") or parsed.get("summary") or "").strip()
            facts = [str(f).strip() for f in (parsed.get("key_facts") or parsed.get("facts") or []) if str(f).strip()][:6]
            return summary, facts
    except Exception:
        pass

    facts: List[str] = []
    other_lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[0] in ("-", "*", "•"):
            item = stripped.lstrip("-*• ").strip()
            if item:
                facts.append(item)
        else:
            other_lines.append(stripped)
    summary = " ".join(other_lines).strip()
    if not summary and facts:
        summary = facts[0]
    return summary, facts[:6]


def execute_web_search(query: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Execute a Google Search using gemini-3.1-flash-lite (with native GoogleSearch grounding)
    and return a compact structured JSON-ready dict for the active chat model.
    """
    from google.genai import types as genai_types

    clean_query = (query or "").strip()[:300]
    if not clean_query:
        return {
            "ok": False,
            "query": "",
            "queries_executed": [],
            "search_requests_used": 0,
            "error": "Empty search query.",
            "sources": [],
        }

    client, err = ClientManager.get_gemini_client("gemini", timeout=30.0)
    if err or not client:
        BackgroundModelSyncer.report_runtime_error("gemini", err or "Missing Gemini API key.", False)
        return {
            "ok": False,
            "query": clean_query,
            "queries_executed": [],
            "search_requests_used": 0,
            "error": err or "Gemini client unavailable.",
            "sources": [],
        }

    today = time.strftime("%Y-%m-%d")
    search_sys_prompt = (
        f"Today is {today}. You are a web search retriever. "
        "Always run Google Search for the query and return only 3-5 ultra-concise verified bullet facts "
        "with exact numbers and dates. No greetings, markdown headers, or filler."
    )

    search_tool = genai_types.Tool(google_search=genai_types.GoogleSearch())

    configured_ids = [strip_model_prefix(m.get("id", "")) for m in get_web_search_models() if m.get("id")]
    if not configured_ids:
        configured_ids = [SEARCH_MODEL_ID]

    if BackgroundModelSyncer.is_provider_verified("gemini"):
        clean_verified = {strip_model_prefix(mid).lower() for mid in BackgroundModelSyncer.get_verified_model_ids("gemini")}
        candidates_to_try = [mid for mid in configured_ids if mid.lower() in clean_verified]
    else:
        candidates_to_try = list(configured_ids)

    response = None
    used_model_id: Optional[str] = None
    last_exc: Optional[Exception] = None
    auth_or_net_fatal = False

    for candidate_model in candidates_to_try:
        attempt_configs = _build_search_configs(genai_types, candidate_model, search_sys_prompt, search_tool)
        for cfg in attempt_configs:
            try:
                response = client.models.generate_content(
                    model=candidate_model,
                    contents=f"Google Search: {clean_query}",
                    config=cfg,
                )
                used_model_id = candidate_model
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                low = str(exc).lower()
                is_net = any(k in low for k in ("connect", "dns", "network", "timeout", "timed out", "unreachable", "refused", "reset"))
                is_auth = any(k in low for k in ("api_key_invalid", "api key not valid", "401", "403", "unauthorized", "permission denied"))
                if is_net or is_auth:
                    auth_or_net_fatal = True
                    break
                # Retry thinking config fallback only when the request rejected thinking settings for this model
                is_thinking_arg_err = ("400" in low or "invalid_argument" in low) and "thinking" in low
                if not is_thinking_arg_err:
                    # Model missing/unavailable/quota/unsupported -> try next priority model in configured list
                    break
        if response is not None or auth_or_net_fatal:
            break

    if response is None or not used_model_id:
        err_msg = str(last_exc or "No configured web search model available on Gemini endpoint.")
        low = err_msg.lower()
        is_net = any(k in low for k in ("connect", "dns", "network", "timeout", "timed out", "unreachable", "refused", "reset"))
        if last_exc is None or (not auth_or_net_fatal and ("not found" in low or "404" in low)):
            BackgroundModelSyncer.set_sync_status("gemini", "invalid_model", "INVALID MODEL")
            from chatint.db import get_providers

            prov = next((p for p in get_providers() if p["id"] == "gemini"), None)
            if prov:
                BackgroundModelSyncer._emit_provider_update(prov)
        else:
            BackgroundModelSyncer.report_runtime_error("gemini", err_msg, is_net)
        return {
            "ok": False,
            "query": clean_query,
            "queries_executed": [],
            "search_requests_used": 0,
            "error": err_msg,
            "sources": [],
        }

    # Extract actual executed Google Search queries and verified source URLs from grounding metadata
    queries_executed: List[str] = []
    sources: List[Dict[str, str]] = []
    seen_urls = set()

    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        gm = getattr(cand, "grounding_metadata", None)
        if not gm:
            continue
        for q in (getattr(gm, "web_search_queries", None) or []):
            if isinstance(q, str) and q.strip():
                queries_executed.append(q.strip())
        for chunk in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(chunk, "web", None)
            if not web:
                continue
            url = (getattr(web, "uri", None) or "").strip()
            title = (getattr(web, "title", None) or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append({"title": title or url, "url": url})
                if len(sources) >= 6:
                    break

    search_requests_used = len(queries_executed)

    # Record Flash Lite tokens & actual Google Search request count
    usage = getattr(response, "usage_metadata", None)
    p_tok = (getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
    c_tok = (
        ((getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0))
        if usage
        else 0
    )
    cache_tok = (getattr(usage, "cached_content_token_count", 0) or 0) if usage else 0

    if conversation_id:
        record_token_usage(
            conversation_id=conversation_id,
            provider_id="websearch",
            model_id=used_model_id,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cached_tokens=cache_tok,
            google_search_requests=search_requests_used,
        )
    if search_requests_used > 0:
        record_google_search_requests(search_requests_used)

    summary, facts = _extract_concise_facts(getattr(response, "text", "") or "")
    return {
        "ok": True,
        "query": clean_query,
        "model_used": used_model_id,
        "queries_executed": queries_executed,
        "search_requests_used": search_requests_used,
        "answer": summary,
        "key_facts": facts,
        "sources": sources,
    }
