from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

from chatint import __version__
from chatint.catalog import is_gemini_3_or_newer, strip_model_prefix
from chatint.client_manager import BackgroundModelSyncer, ClientManager
from chatint.db import (
    PRIMARY_WEB_SEARCH_MODEL,
    get_api_key_from_env,
    get_app_settings,
    get_web_search_models,
    record_google_search_requests,
    record_token_usage,
)
from chatint.logging_setup import get_logger

_logger = get_logger("chatint.web_search")

SEARCH_MODEL_ID = PRIMARY_WEB_SEARCH_MODEL
WEB_SEARCH_TOOL_NAME = "web_search"
IMAGE_SEARCH_TOOL_NAME = "search_images"
MAX_TOOL_ROUNDS = 10
MAX_TOOL_CALLS_PER_PROMPT = 10

_TOOL_DESCRIPTION = (
    "Search the live web for current events, real-time data, recent facts, "
    "prices, versions, or facts you are uncertain about. Provide a concise search query."
)

_TOOL_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Concise search query to look up on the web.",
        }
    },
    "required": ["query"],
}

_IMAGE_TOOL_DESCRIPTION = (
    "Search and retrieve relevant, high-quality images to visually illustrate and explain concepts, "
    "landmarks, historical events, scientific topics, people, animals, diagrams, or products in your response, "
    "as well as whenever the user explicitly asks for images or photos. "
    "Returns verified image URLs and titles. "
    "You should embed the returned images into your response using standard Markdown format: ![Title](image_url)."
)

_IMAGE_TOOL_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Specific search query for the image (e.g. 'Eiffel Tower Paris', 'photosynthesis diagram', 'golden retriever puppy').",
        }
    },
    "required": ["query"],
}


def get_web_search_system_hint() -> str:
    """Short hint appended to the chat model's system prompt only when Web Search is enabled."""
    today = time.strftime("%Y-%m-%d")
    return (
        f"Today's date is {today}. When search is enabled, you have access to live web tools:\n"
        f"1. `{WEB_SEARCH_TOOL_NAME}`: Live web search for current events, news, prices, versions, or verifiable facts.\n"
        f"2. `{IMAGE_SEARCH_TOOL_NAME}`: Search for and retrieve relevant, high-quality images to visually illustrate and explain "
        f"concepts, landmarks, historical events, science, animals, diagrams, or products (limit: up to 3 images per call). "
        f"Use `{IMAGE_SEARCH_TOOL_NAME}` proactively whenever adding an illustration or photo helps explain the topic, as well as when the user asks for images.\n"
        f"When presenting images in your answer, always embed them using Markdown format: `![Description](image_url)`.\n"
        f"Total tool-call budget: up to {MAX_TOOL_ROUNDS} tool calls per response."
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


def get_openai_image_tool_spec() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": IMAGE_SEARCH_TOOL_NAME,
            "description": _IMAGE_TOOL_DESCRIPTION,
            "parameters": _IMAGE_TOOL_PARAMETERS,
        },
    }


def get_anthropic_tool_spec() -> Dict[str, Any]:
    return {
        "name": WEB_SEARCH_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "input_schema": _TOOL_PARAMETERS,
    }


def get_anthropic_image_tool_spec() -> Dict[str, Any]:
    return {
        "name": IMAGE_SEARCH_TOOL_NAME,
        "description": _IMAGE_TOOL_DESCRIPTION,
        "input_schema": _IMAGE_TOOL_PARAMETERS,
    }


def get_gemini_tool_declaration() -> Any:
    from google.genai import types as genai_types

    return genai_types.FunctionDeclaration(
        name=WEB_SEARCH_TOOL_NAME,
        description=_TOOL_DESCRIPTION,
        parameters_json_schema=_TOOL_PARAMETERS,
    )


def get_gemini_image_tool_declaration() -> Any:
    from google.genai import types as genai_types

    return genai_types.FunctionDeclaration(
        name=IMAGE_SEARCH_TOOL_NAME,
        description=_IMAGE_TOOL_DESCRIPTION,
        parameters_json_schema=_IMAGE_TOOL_PARAMETERS,
    )


def execute_image_search(query: str, max_results: int = 3) -> Dict[str, Any]:
    """
    Searches Wikipedia PageImages and Wikimedia Commons for verified high-resolution images.
    Returns up to max_results (default 3) image objects with title, URL, and formatted markdown.
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return {"ok": False, "query": "", "images": [], "error": "Query is empty."}

    max_count = max(1, min(int(max_results or 3), 3))
    headers = {"User-Agent": f"ChatInt/{__version__} (Desktop Chat App; https://github.com/RishavSinha-2/ChatINT)"}
    images: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    # Step 1: Wikipedia PageImages (best for landmark, historical, scientific, biographical topics)
    try:
        import httpx
        r = httpx.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": clean_query,
                "gsrlimit": max_count,
                "prop": "pageimages",
                "piprop": "thumbnail|original",
                "pithumbsize": 960,
                "format": "json",
            },
            headers=headers,
            timeout=5.0,
        )
        if r.status_code == 200:
            q1 = (r.json() or {}).get("query") or {}
            pages = q1.get("pages") or {}
            for _pid, p in sorted(pages.items(), key=lambda x: (x[1] or {}).get("index", 99)):
                if not isinstance(p, dict):
                    continue
                thumb = (p.get("thumbnail") or {}).get("source") or (p.get("original") or {}).get("source")
                title = p.get("title", clean_query)
                if thumb and thumb not in seen_urls:
                    seen_urls.add(thumb)
                    images.append({
                        "title": title,
                        "url": thumb,
                        "markdown": f"![{title}]({thumb})",
                    })
                if len(images) >= max_count:
                    break
    except Exception as exc:
        _logger.debug("Wikipedia image search lookup failed for %r: %s", clean_query, exc)

    # Step 2: Wikimedia Commons Search (for animals, diagrams, modern objects, etc.)
    if len(images) < max_count:
        try:
            import httpx
            r2 = httpx.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": clean_query,
                    "gsrnamespace": 6,
                    "gsrlimit": max_count * 2,
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "iiurlwidth": 960,
                    "format": "json",
                },
                headers=headers,
                timeout=5.0,
            )
            if r2.status_code == 200:
                q2 = (r2.json() or {}).get("query") or {}
                pages2 = q2.get("pages") or {}
                for _pid, p in sorted(pages2.items(), key=lambda x: (x[1] or {}).get("index", 99)):
                    if not isinstance(p, dict):
                        continue
                    imageinfo = p.get("imageinfo") or [{}]
                    info = imageinfo[0] if isinstance(imageinfo, list) and imageinfo else {}
                    if not isinstance(info, dict):
                        info = {}
                    url = info.get("thumburl") or info.get("url")
                    raw_title = p.get("title", "").replace("File:", "")
                    if "." in raw_title:
                        raw_title = raw_title.rsplit(".", 1)[0]
                    title = raw_title.strip() or clean_query
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        images.append({
                            "title": title,
                            "url": url,
                            "markdown": f"![{title}]({url})",
                        })
                    if len(images) >= max_count:
                        break
        except Exception as exc:
            _logger.debug("Wikimedia Commons image search lookup failed for %r: %s", clean_query, exc)

    return {
        "ok": len(images) > 0,
        "query": clean_query,
        "images": images[:max_count],
        "count": len(images[:max_count]),
    }


def _is_network_exception_text(low: str) -> bool:
    return any(k in low for k in ("connect", "dns", "network", "timeout", "timed out", "unreachable", "refused", "reset"))


def _is_auth_exception_text(low: str) -> bool:
    return any(k in low for k in ("api_key_invalid", "api key not valid", "401", "403", "unauthorized", "permission denied"))


def is_gemini_web_search_enabled() -> bool:
    """Return True only when the user explicitly opts into Gemini-grounded Google Search."""
    return str(get_app_settings().get("use_gemini_web_search", "false")).strip().lower() == "true"


def is_web_search_available() -> tuple[bool, str]:
    """Check whether Web Search is ready (DuckDuckGo by default, or Gemini when opted in)."""
    if not is_gemini_web_search_enabled():
        return True, ""

    provider = ClientManager.get_provider("gemini")
    if not provider:
        return False, "NO API KEY"
    is_enabled = bool(provider.get("is_enabled", True))
    has_key = bool(get_api_key_from_env(provider.get("name", "Gemini")))
    status, brief = BackgroundModelSyncer.get_sync_status("gemini", has_api_key=has_key, is_enabled=is_enabled)
    if status in ("no_key", "disabled", "invalid_key", "network_error", "error"):
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
    min_lvl = getattr(genai_types.ThinkingLevel, "MINIMAL", None)
    level_cfg = genai_types.GenerateContentConfig(
        system_instruction=search_sys_prompt,
        tools=[search_tool],
        temperature=0.1,
        max_output_tokens=600,
        thinking_config=genai_types.ThinkingConfig(thinking_level=min_lvl) if min_lvl is not None else None,
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
    return [level_cfg, budget_cfg, plain_cfg] if is_gemini_3_or_newer(model_id) else [budget_cfg, level_cfg, plain_cfg]


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
            parsed_facts: List[str] = [
                str(f).strip()
                for f in (parsed.get("key_facts") or parsed.get("facts") or [])
                if str(f).strip()
            ][:6]
            return summary, parsed_facts
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    fallback_facts: List[str] = []
    other_lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[0] in ("-", "*", "•"):
            item = stripped.lstrip("-*• ").strip()
            if item:
                fallback_facts.append(item)
        else:
            other_lines.append(stripped)
    summary = " ".join(other_lines).strip()
    if not summary and fallback_facts:
        summary = fallback_facts[0]
    return summary, fallback_facts[:6]


def execute_ddg_web_search(
    query: str,
    max_results: int = 6,
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a keyless web search using ddgs and track actual per-chat HTTP requests sent."""
    clean_query = (query or "").strip()[:300]
    if not clean_query:
        return {
            "ok": False,
            "query": "",
            "provider_used": "duckduckgo",
            "queries_executed": [],
            "search_requests_used": 0,
            "ddg_requests_used": 0,
            "error": "Empty search query.",
            "sources": [],
            "results": [],
        }

    limit = max(1, min(int(max_results or 6), 10))
    raw_results: List[Dict[str, Any]] = []
    last_exc: Optional[Exception] = None
    http_req_count = 0
    text_attempts = 0
    req_lock = threading.Lock()
    try:
        from ddgs import DDGS

        ddg_client = DDGS(timeout=12)
        if callable(getattr(type(ddg_client), "_get_engines", None)):
            orig_get_engines = ddg_client._get_engines

            def _counting_get_engines(category: str, backend: str, _orig=orig_get_engines):
                engines = _orig(category, backend)
                for eng in engines or ():
                    hc = getattr(eng, "http_client", None)
                    if hc is not None and not getattr(hc, "_chatint_counted", False):
                        orig_req = getattr(hc, "request", None)
                        if callable(orig_req):
                            def _counted_req(*args: Any, _orig_req=orig_req, **kwargs: Any):
                                nonlocal http_req_count
                                with req_lock:
                                    http_req_count += 1
                                return _orig_req(*args, **kwargs)

                            hc.request = _counted_req
                            hc._chatint_counted = True
                return engines

            ddg_client._get_engines = _counting_get_engines

        for backend_group in ("auto", "startpage, yahoo, duckduckgo, wikipedia"):
            text_attempts += 1
            try:
                candidate = ddg_client.text(
                    clean_query,
                    max_results=max(limit, 8),
                    backend=backend_group,
                )
                if candidate:
                    raw_results = list(candidate)[:limit]
                    last_exc = None
                    break
            except Exception as bk_exc:
                last_exc = bk_exc
        if not raw_results and last_exc is not None:
            raise last_exc
    except Exception as exc:
        ddg_requests_used = http_req_count if http_req_count > 0 else text_attempts
        if conversation_id and ddg_requests_used > 0:
            record_token_usage(
                conversation_id=conversation_id,
                provider_id="websearch",
                model_id="",
                prompt_tokens=0,
                completion_tokens=0,
                cached_tokens=0,
                ddg_search_requests=ddg_requests_used,
            )
        _logger.warning("DuckDuckGo metasearch failed for %r: %s", clean_query, exc)
        return {
            "ok": False,
            "query": clean_query,
            "provider_used": "duckduckgo",
            "queries_executed": [clean_query],
            "search_requests_used": 0,
            "ddg_requests_used": ddg_requests_used,
            "error": str(exc) or "Web search failed.",
            "sources": [],
            "results": [],
        }

    ddg_requests_used = http_req_count if http_req_count > 0 else text_attempts
    if conversation_id and ddg_requests_used > 0:
        record_token_usage(
            conversation_id=conversation_id,
            provider_id="websearch",
            model_id="",
            prompt_tokens=0,
            completion_tokens=0,
            cached_tokens=0,
            ddg_search_requests=ddg_requests_used,
        )

    results: List[Dict[str, str]] = []
    sources: List[Dict[str, str]] = []
    key_facts: List[str] = []
    seen_urls: set[str] = set()

    for item in raw_results or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("href") or item.get("url") or "").strip()
        snippet = str(item.get("body") or item.get("snippet") or "").strip()
        if not title and not snippet and not url:
            continue
        results.append({"title": title or url, "url": url, "snippet": snippet})
        if url and url not in seen_urls:
            seen_urls.add(url)
            sources.append({"title": title or url, "url": url})
        fact_line = f"{title}: {snippet}".strip(": ") if title else snippet
        if fact_line:
            key_facts.append(fact_line)

    summary = key_facts[0] if key_facts else ""
    return {
        "ok": len(results) > 0,
        "query": clean_query,
        "provider_used": "duckduckgo",
        "queries_executed": [clean_query],
        "search_requests_used": 0,
        "ddg_requests_used": ddg_requests_used,
        "answer": summary,
        "key_facts": key_facts[:6],
        "results": results,
        "sources": sources[:6],
    }


def execute_web_search(query: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Execute a live web search: uses keyless DuckDuckGo metasearch by default,
    or Gemini-grounded Google Search when `use_gemini_web_search` is enabled.
    """
    if not is_gemini_web_search_enabled():
        return execute_ddg_web_search(query, conversation_id=conversation_id)

    from google.genai import types as genai_types

    clean_query = (query or "").strip()[:300]
    if not clean_query:
        return {
            "ok": False,
            "query": "",
            "provider_used": "gemini",
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

    try:
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
                    if _is_network_exception_text(low) or _is_auth_exception_text(low):
                        auth_or_net_fatal = True
                        break
                    # Retry thinking config fallback only when the request rejected thinking settings for this model
                    is_thinking_arg_err = ("400" in low or "invalid_argument" in low) and "thinking" in low
                    if not is_thinking_arg_err:
                        # Model missing/unavailable/quota/unsupported -> try next priority model in configured list
                        break
            if response is not None or auth_or_net_fatal:
                break
    finally:
        ClientManager.close_client(client)

    if response is None or not used_model_id:
        err_msg = str(last_exc or "No configured web search model available on Gemini endpoint.")
        low = err_msg.lower()
        is_net = _is_network_exception_text(low)
        is_model_missing = last_exc is None or (
            not auth_or_net_fatal and ("not found" in low or "404" in low)
        )
        # Only report auth/network errors globally against the Gemini provider so model-level search misses
        # do not poison Gemini chat sync_status or block Web Search after adding a fallback model.
        if not is_model_missing:
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
        if len(sources) < 6:
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
        "provider_used": "gemini",
        "model_used": used_model_id,
        "queries_executed": queries_executed,
        "search_requests_used": search_requests_used,
        "answer": summary,
        "key_facts": facts,
        "sources": sources,
    }
