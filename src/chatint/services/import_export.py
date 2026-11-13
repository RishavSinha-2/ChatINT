"""Lossless JSON & Markdown chat import/export service with size bounds and encoding fallbacks."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

MAX_IMPORT_BYTES = 25 * 1024 * 1024  # 25 MB safety limit
_VALID_THINKING_LEVELS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}

_CONV_META_RE = re.compile(r"^<!--\s*chatint-conv-meta:\s*(\{.*\})\s*-->$")
_MSG_META_RE = re.compile(r"^<!--\s*chatint-msg-meta:\s*(\{.*\})\s*-->$")
_THOUGHT_DELIM_RE = re.compile(
    r"<!--\s*chatint-thought-start\s*-->\s*<details>\s*<summary>\s*Thought Process\s*</summary>\s*([\s\S]*?)\s*</details>\s*<!--\s*chatint-thought-end\s*-->",
    re.IGNORECASE,
)
_LEGACY_THOUGHT_RE = re.compile(
    r"<details>\s*<summary>\s*Thought Process\s*</summary>\s*([\s\S]*?)\s*</details>",
    re.IGNORECASE,
)


def _clamp_max_context(value: Any, default: int = 30) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(500, parsed))


def _normalize_thinking_level(level: Any) -> str:
    val = str(level or "none").strip().lower()
    return val if val in _VALID_THINKING_LEVELS else "none"


def _sanitize_msg_role(role: Any) -> str:
    val = str(role or "user").strip().lower()
    return val if val in ("user", "assistant", "system") else "user"


def _safe_nonneg_int(val: Any) -> int:
    try:
        return max(0, int(val or 0))
    except (TypeError, ValueError):
        return 0


def read_chat_file_safely(path: Path) -> str:
    """Read an imported chat file with a 25 MB cap and UTF-8-BOM / CP1252 fallback decoding."""
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("File does not exist.")
    size = path.stat().st_size
    if size > MAX_IMPORT_BYTES:
        raise ValueError("Import file exceeds the 25 MB maximum size limit.")
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _escape_details_for_markdown(thought: str) -> str:
    return re.sub(r"</details>", "&lt;/details&gt;", thought, flags=re.IGNORECASE)


def _unescape_details_from_markdown(thought: str) -> str:
    return re.sub(r"&lt;/details&gt;", "</details>", thought, flags=re.IGNORECASE)


def format_conversation_markdown(conv: Dict[str, Any]) -> str:
    """Export a conversation to Markdown while preserving metadata, system turns, and literal </details>."""
    lines = [f"# {conv.get('title') or 'Conversation'}\n"]

    conv_meta: Dict[str, Any] = {
        "max_context_messages": _clamp_max_context(conv.get("max_context_messages"), default=30),
    }
    if isinstance(conv.get("created_at"), (int, float)):
        conv_meta["created_at"] = float(conv["created_at"])
    lines.append(f"<!-- chatint-conv-meta: {json.dumps(conv_meta, separators=(',', ':'))} -->\n")

    if conv.get("system_prompt"):
        sys_lines = str(conv["system_prompt"]).splitlines() or [""]
        prefixed_sys = [f"> **System Prompt:** {sys_lines[0]}"]
        prefixed_sys.extend(f"> {line}" for line in sys_lines[1:])
        lines.append("\n".join(prefixed_sys) + "\n")

    for msg in conv.get("messages", []):
        role = msg.get("role", "user")
        if role == "user":
            role_title = "User"
        elif role == "system":
            role_title = "System"
        else:
            role_title = f"Assistant ({msg.get('model_id') or 'Model'})"
        lines.append(f"### {role_title}\n")

        msg_meta: Dict[str, Any] = {}
        if msg.get("provider_id"):
            msg_meta["provider_id"] = msg["provider_id"]
        if msg.get("model_id"):
            msg_meta["model_id"] = msg["model_id"]
        if msg.get("thinking_level") and msg.get("thinking_level") != "none":
            msg_meta["thinking_level"] = msg["thinking_level"]
        for tok_k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            if _safe_nonneg_int(msg.get(tok_k)) > 0:
                msg_meta[tok_k] = _safe_nonneg_int(msg.get(tok_k))
        if isinstance(msg.get("created_at"), (int, float)):
            msg_meta["created_at"] = float(msg["created_at"])
        if msg_meta:
            lines.append(f"<!-- chatint-msg-meta: {json.dumps(msg_meta, separators=(',', ':'))} -->\n")

        if msg.get("thinking_content"):
            escaped_thought = _escape_details_for_markdown(str(msg["thinking_content"]))
            lines.append(
                "<!-- chatint-thought-start -->\n"
                f"<details><summary>Thought Process</summary>\n\n{escaped_thought}\n\n</details>\n"
                "<!-- chatint-thought-end -->\n"
            )
        lines.append(f"{msg.get('content', '')}\n")

    return "\n".join(lines)


def _normalize_json_msg(m: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "role": _sanitize_msg_role(m.get("role", "user")),
        "content": str(m.get("content", "") or ""),
        "thinking_content": str(m.get("thinking_content", "") or ""),
        "provider_id": str(m.get("provider_id", "") or ""),
        "model_id": str(m.get("model_id", "") or ""),
        "thinking_level": _normalize_thinking_level(m.get("thinking_level", "none")),
        "prompt_tokens": _safe_nonneg_int(m.get("prompt_tokens")),
        "completion_tokens": _safe_nonneg_int(m.get("completion_tokens")),
        "cached_tokens": _safe_nonneg_int(m.get("cached_tokens")),
    }
    if isinstance(m.get("created_at"), (int, float)):
        out["created_at"] = float(m["created_at"])
    return out


def _has_closing_fence(lines: List[str], start_idx: int, fence_char: str, fence_len: int) -> bool:
    """Return True only if a matching closing code fence exists ahead in the document."""
    for j in range(start_idx + 1, len(lines)):
        s = lines[j].strip()
        m = re.match(r"^(`{3,}|~{3,})$", s)
        if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
            return True
    return False


def parse_chat_file_content(content: str, filename: str = "") -> Dict[str, Any]:
    """
    Parse exported .json or .md chat content into a structured dictionary containing
    title, system_prompt, max_context, optional created_at, and messages.
    """
    if content and len(content.encode("utf-8", errors="ignore")) > MAX_IMPORT_BYTES:
        raise ValueError("Imported chat content exceeds the 25 MB maximum size limit.")

    content_str = (content or "").lstrip("\ufeff").strip()
    if not content_str:
        return {"title": "Imported Chat", "system_prompt": "", "max_context": 30, "messages": []}

    # 1. Try parsing JSON first
    try:
        data = json.loads(content_str)
        if isinstance(data, dict):
            title = data.get("title") or (Path(filename).stem if filename else "") or "Imported Chat"
            system_prompt = data.get("system_prompt") or ""
            max_context = _clamp_max_context(data.get("max_context_messages"), default=30)
            messages = [_normalize_json_msg(m) for m in (data.get("messages") or []) if isinstance(m, dict)]
            res: Dict[str, Any] = {
                "title": title,
                "system_prompt": system_prompt,
                "max_context": max_context,
                "messages": messages,
            }
            if isinstance(data.get("created_at"), (int, float)):
                res["created_at"] = float(data["created_at"])
            return res
        if isinstance(data, list):
            messages = [_normalize_json_msg(m) for m in data if isinstance(m, dict)]
            return {
                "title": (Path(filename).stem if filename else "") or "Imported Chat",
                "system_prompt": "",
                "max_context": 30,
                "messages": messages,
            }
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # 2. Parse Markdown format (fence-aware with unclosed-fence protection, system turns, and metadata)
    title = ""
    max_context = 30
    conv_created_at: Optional[float] = None
    system_lines: List[str] = []
    messages = []

    raw_lines = content_str.splitlines()
    in_fence_char = ""
    in_fence_len = 0
    in_delimited_thought = False
    in_legacy_details = False
    collecting_sys_quote = False
    current_role: Optional[str] = None
    current_model_id = ""
    current_msg_meta: Dict[str, Any] = {}
    current_body_lines: List[str] = []

    def _flush_current_message() -> None:
        if current_role is None:
            return
        body = "\n".join(current_body_lines).strip()
        thinking_content = ""

        delim_match = _THOUGHT_DELIM_RE.search(body)
        if delim_match:
            thinking_content = _unescape_details_from_markdown(delim_match.group(1).strip())
            body = (body[: delim_match.start()] + "\n" + body[delim_match.end() :]).strip()
        else:
            legacy_match = _LEGACY_THOUGHT_RE.search(body)
            if legacy_match:
                thinking_content = _unescape_details_from_markdown(legacy_match.group(1).strip())
                body = (body[: legacy_match.start()] + "\n" + body[legacy_match.end() :]).strip()

        resolved_thinking = _normalize_thinking_level(
            current_msg_meta.get("thinking_level") or ("medium" if thinking_content else "none")
        )
        msg_entry: Dict[str, Any] = {
            "role": current_role,
            "content": body,
            "thinking_content": thinking_content,
            "provider_id": str(current_msg_meta.get("provider_id", "") or ""),
            "model_id": str(current_msg_meta.get("model_id") or current_model_id or ""),
            "thinking_level": resolved_thinking,
            "prompt_tokens": _safe_nonneg_int(current_msg_meta.get("prompt_tokens")),
            "completion_tokens": _safe_nonneg_int(current_msg_meta.get("completion_tokens")),
            "cached_tokens": _safe_nonneg_int(current_msg_meta.get("cached_tokens")),
        }
        if isinstance(current_msg_meta.get("created_at"), (int, float)):
            msg_entry["created_at"] = float(current_msg_meta["created_at"])
        messages.append(msg_entry)

    header_re = re.compile(r"^###\s+(User|System|Assistant(?:\s*\((.+)\))?)\s*$", re.IGNORECASE)
    sys_start_re = re.compile(r"^>\s*\*\*System Prompt:\*\*\s*(.*)$")

    for idx, line in enumerate(raw_lines):
        stripped = line.strip()

        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence_match and not in_delimited_thought:
            f_str = fence_match.group(1)
            if not in_fence_char:
                if _has_closing_fence(raw_lines, idx, f_str[0], len(f_str)):
                    in_fence_char = f_str[0]
                    in_fence_len = len(f_str)
            elif f_str[0] == in_fence_char and len(f_str) >= in_fence_len and stripped == f_str:
                in_fence_char = ""
                in_fence_len = 0

        if not in_fence_char:
            if not in_delimited_thought and "<!-- chatint-thought-start -->" in stripped.lower():
                in_delimited_thought = True
            if in_delimited_thought and "<!-- chatint-thought-end -->" in stripped.lower():
                if current_role is not None:
                    current_body_lines.append(line)
                in_delimited_thought = False
                continue

            if not in_delimited_thought:
                if not in_legacy_details and re.search(
                    r"<details>\s*<summary>\s*Thought Process\s*</summary>", line, re.IGNORECASE
                ):
                    in_legacy_details = True
                if in_legacy_details and "</details>" in line.lower():
                    if current_role is not None:
                        current_body_lines.append(line)
                    in_legacy_details = False
                    continue

            if current_role is None:
                cm_match = _CONV_META_RE.match(stripped)
                if cm_match:
                    try:
                        parsed_cm = json.loads(cm_match.group(1))
                        if isinstance(parsed_cm, dict):
                            max_context = _clamp_max_context(parsed_cm.get("max_context_messages"), default=30)
                            if isinstance(parsed_cm.get("created_at"), (int, float)):
                                conv_created_at = float(parsed_cm["created_at"])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
                    continue

                if not title:
                    t_match = re.match(r"^#\s+(.+)$", line)
                    if t_match:
                        title = t_match.group(1).strip()
                        continue

                s_match = sys_start_re.match(line)
                if s_match:
                    collecting_sys_quote = True
                    first_part = s_match.group(1).strip()
                    if first_part:
                        system_lines.append(first_part)
                    continue
                if collecting_sys_quote:
                    if line.startswith(">"):
                        cont = re.sub(r"^>\s?", "", line)
                        system_lines.append(cont)
                        continue
                    collecting_sys_quote = False

            if not in_delimited_thought and not in_legacy_details:
                if current_role is not None and not any(ln.strip() for ln in current_body_lines):
                    mm_match = _MSG_META_RE.match(stripped)
                    if mm_match:
                        try:
                            parsed_mm = json.loads(mm_match.group(1))
                            if isinstance(parsed_mm, dict):
                                current_msg_meta = parsed_mm
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
                        continue

                h_match = header_re.match(stripped)
                if h_match:
                    collecting_sys_quote = False
                    _flush_current_message()
                    role_token = h_match.group(1).lower()
                    if role_token.startswith("user"):
                        current_role = "user"
                        current_model_id = ""
                    elif role_token.startswith("system"):
                        current_role = "system"
                        current_model_id = ""
                    else:
                        current_role = "assistant"
                        current_model_id = (h_match.group(2) or "").strip()
                    current_msg_meta = {}
                    current_body_lines = []
                    continue

        if current_role is not None:
            current_body_lines.append(line)

    _flush_current_message()

    if not title:
        title = (Path(filename).stem if filename else "") or "Imported Chat"
    system_prompt = "\n".join(system_lines).strip()

    if not messages:
        messages.append({
            "role": "user",
            "content": content_str,
            "thinking_content": "",
            "provider_id": "",
            "model_id": "",
            "thinking_level": "none",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
        })

    result: Dict[str, Any] = {
        "title": title,
        "system_prompt": system_prompt,
        "max_context": max_context,
        "messages": messages,
    }
    if conv_created_at is not None:
        result["created_at"] = conv_created_at
    return result
