"""Web search, image search, and SSRF-guarded URL fetch tools."""
from __future__ import annotations

from html.parser import HTMLParser
import ipaddress
import re
import socket
from typing import Any, Dict, List
import urllib.parse

import httpx

from chatint.logging_setup import get_logger
from chatint.tools.registry import ToolSpec, register_tool
import chatint.web_search_tool as _ws_mod
from chatint.web_search_tool import (
    IMAGE_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    _IMAGE_TOOL_DESCRIPTION,
    _IMAGE_TOOL_PARAMETERS,
    _TOOL_DESCRIPTION,
    _TOOL_PARAMETERS,
)

_logger = get_logger("chatint.tools.web")
FETCH_URL_TOOL_NAME = "fetch_url"
_MAX_FETCH_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 4


class _TextExtractor(HTMLParser):
    """Lightweight stdlib HTML-to-text extractor skipping script/style/nav blocks."""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        if low in self._SKIP_TAGS:
            self._skip_depth += 1
        elif low == "title" and self._skip_depth == 0:
            self._in_title = True
        elif low in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"):
            if self._skip_depth == 0:
                self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif low == "title":
            self._in_title = False
        elif low in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            if self._skip_depth == 0:
                self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or not data:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)


def _is_forbidden_ip(ip_obj: ipaddress._BaseAddress) -> bool:
    mapped = getattr(ip_obj, "ipv4_mapped", None)
    if mapped is not None and _is_forbidden_ip(mapped):
        return True
    return (
        not ip_obj.is_global
        or ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _validate_public_http_url(raw_url: str) -> tuple[bool, str]:
    """Allow only public http/https URLs and block private, loopback, or metadata IP targets."""
    target = (raw_url or "").strip()
    if not target or any(ch.isspace() or ord(ch) < 32 for ch in target):
        return False, "Invalid URL."
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return False, "Only public http:// and https:// URLs are allowed."

    host = parsed.hostname.strip().strip("[]").lower()
    if host in ("localhost", "localhost.localdomain"):
        return False, f"Blocked local/private host '{host}'."

    try:
        literal_ip = ipaddress.ip_address(host)
        if _is_forbidden_ip(literal_ip):
            return False, f"Blocked non-public IP address '{host}'."
        return True, ""
    except ValueError:
        pass

    try:
        addr_info = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80))
    except socket.gaierror as exc:
        return False, f"DNS resolution failed for '{host}': {exc}"

    if not addr_info:
        return False, f"Could not resolve host '{host}'."
    for entry in addr_info:
        ip_str = entry[4][0].split("%", 1)[0]
        try:
            resolved_ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_forbidden_ip(resolved_ip):
            return False, f"Blocked non-public resolved IP '{ip_str}' for '{host}'."
    return True, ""


def _extract_readable_text(raw_text: str, content_type: str) -> tuple[str, str]:
    ct = (content_type or "").lower()
    is_html = "html" in ct or (not ct and raw_text.lstrip()[:64].lower().startswith(("<html", "<!doctype")))
    if not is_html:
        return "", raw_text
    parser = _TextExtractor()
    parser.feed(raw_text)
    parser.close()
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    joined = "".join(parser.text_parts)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in joined.splitlines()]
    collapsed = "\n".join(ln for ln in lines if ln)
    return title, collapsed


def fetch_url(url: Optional[str], max_chars: Any = 20_000) -> Dict[str, Any]:
    """Fetch a public HTTP/HTTPS webpage or text document with SSRF, redirect, and size bounds."""
    clean_url = ("" if url is None else str(url)).strip()
    ok, err = _validate_public_http_url(clean_url)
    if not ok:
        return {"ok": False, "url": clean_url, "error": err}

    try:
        parsed_cap = int(float(max_chars)) if (max_chars is not None and not isinstance(max_chars, bool)) else 20_000
    except (TypeError, ValueError):
        parsed_cap = 20_000
    char_cap = max(500, min(parsed_cap, 25_000))
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ChatInt/1.0; +https://github.com/RishavSinha-2/ChatINT)"}
    current_url = clean_url

    try:
        with httpx.Client(timeout=15.0, follow_redirects=False, headers=headers, trust_env=False) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                with client.stream("GET", current_url) as r:
                    extensions = getattr(r, "extensions", None)
                    net_stream = extensions.get("network_stream") if isinstance(extensions, dict) else None
                    get_extra = getattr(net_stream, "get_extra_info", None)
                    if callable(get_extra):
                        peer = get_extra("peername")
                        if isinstance(peer, tuple) and peer and isinstance(peer[0], str):
                            peer_ip_str = peer[0].split("%", 1)[0]
                            try:
                                if _is_forbidden_ip(ipaddress.ip_address(peer_ip_str)):
                                    return {
                                        "ok": False,
                                        "url": current_url,
                                        "error": f"Blocked non-public peer IP '{peer_ip_str}'.",
                                    }
                            except ValueError:
                                pass

                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = (r.headers.get("location") or "").strip()
                        if not loc:
                            return {"ok": False, "url": current_url, "error": "Redirect missing Location header."}
                        next_url = urllib.parse.urljoin(current_url, loc)
                        redir_ok, redir_err = _validate_public_http_url(next_url)
                        if not redir_ok:
                            return {"ok": False, "url": next_url, "error": redir_err}
                        current_url = next_url
                        continue

                    if r.status_code >= 400:
                        return {"ok": False, "url": current_url, "error": f"HTTP {r.status_code}"}

                    content_type = (r.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                    if content_type and not (
                        content_type.startswith("text/")
                        or content_type in ("application/json", "application/xml", "application/xhtml+xml")
                        or content_type.endswith("+json")
                        or content_type.endswith("+xml")
                    ):
                        return {
                            "ok": False,
                            "url": current_url,
                            "error": f"Unsupported binary or non-text content-type '{content_type}'.",
                        }

                    chunks: List[bytes] = []
                    total = 0
                    for chunk in r.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= _MAX_FETCH_BYTES:
                            break
                    raw_bytes = b"".join(chunks)[:_MAX_FETCH_BYTES]
                    encoding = r.encoding or "utf-8"
                    text_body = raw_bytes.decode(encoding, errors="replace")
                    title, extracted = _extract_readable_text(text_body, content_type)
                    truncated = len(extracted) > char_cap or total >= _MAX_FETCH_BYTES
                    return {
                        "ok": True,
                        "url": current_url,
                        "title": title,
                        "content_type": content_type or "text/plain",
                        "content": extracted[:char_cap],
                        "truncated": truncated,
                    }

            return {"ok": False, "url": current_url, "error": "Too many redirects."}
    except Exception as exc:
        _logger.debug("fetch_url failed for %r: %s", current_url, exc)
        return {"ok": False, "url": current_url, "error": str(exc)}


def _handle_web_search(query: str = "", conversation_id: str | None = None, **_kwargs: Any) -> Dict[str, Any]:
    return _ws_mod.execute_web_search(query, conversation_id=conversation_id)


def _handle_image_search(query: str = "", max_results: int = 3, **_kwargs: Any) -> Dict[str, Any]:
    return _ws_mod.execute_image_search(query, max_results=max_results)


def _handle_fetch_url(url: str = "", max_chars: int = 20_000, **_kwargs: Any) -> Dict[str, Any]:
    return fetch_url(url, max_chars=max_chars)


def register_web_tools() -> None:
    """Register web search, image search, and fetch_url tools in the unified registry."""
    register_tool(
        ToolSpec(
            name=WEB_SEARCH_TOOL_NAME,
            description=_TOOL_DESCRIPTION,
            parameters_schema=_TOOL_PARAMETERS,
            category="web",
            requires_workspace=False,
            mutating=False,
            handler=_handle_web_search,
        )
    )
    register_tool(
        ToolSpec(
            name=IMAGE_SEARCH_TOOL_NAME,
            description=_IMAGE_TOOL_DESCRIPTION,
            parameters_schema=_IMAGE_TOOL_PARAMETERS,
            category="web",
            requires_workspace=False,
            mutating=False,
            handler=_handle_image_search,
        )
    )
    register_tool(
        ToolSpec(
            name=FETCH_URL_TOOL_NAME,
            description="Fetch and extract readable text from a public http:// or https:// URL.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public http:// or https:// URL to fetch."},
                    "max_chars": {"type": "integer", "description": "Optional character cap (default 20000)."},
                },
                "required": ["url"],
            },
            category="web",
            requires_workspace=False,
            mutating=False,
            handler=_handle_fetch_url,
        )
    )
