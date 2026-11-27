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


def _dump_meta_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":")).replace("-->", "--\\u003e")


def format_conversation_markdown(conv: Dict[str, Any]) -> str:
    """Export a conversation to Markdown while preserving settings, per-message metrics, traces, and ledger totals."""
    lines = [f"# {conv.get('title') or 'Conversation'}\n"]

    conv_meta: Dict[str, Any] = {
        "max_context_messages": _clamp_max_context(conv.get("max_context_messages"), default=30),
        "agent_mode": "auto" if str(conv.get("agent_mode") or "manual").strip().lower() == "auto" else "manual",
        "is_auto_named": bool(conv.get("is_auto_named", True)),
    }
    if conv.get("workspace_dir"):
        conv_meta["workspace_dir"] = str(conv["workspace_dir"])
    if isinstance(conv.get("created_at"), (int, float)):
        conv_meta["created_at"] = float(conv["created_at"])
    if isinstance(conv.get("updated_at"), (int, float)):
        conv_meta["updated_at"] = float(conv["updated_at"])
    if isinstance(conv.get("token_stats"), dict):
        conv_meta["token_stats"] = conv["token_stats"]
    if isinstance(conv.get("token_ledger"), list) and conv["token_ledger"]:
        conv_meta["token_ledger"] = conv["token_ledger"]
    lines.append(f"<!-- chatint-conv-meta: {_dump_meta_json(conv_meta)} -->\n")

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
        if msg.get("provider_name"):
            msg_meta["provider_name"] = msg["provider_name"]
        if msg.get("model_id"):
            msg_meta["model_id"] = msg["model_id"]
        if msg.get("thinking_level") and msg.get("thinking_level") != "none":
            msg_meta["thinking_level"] = msg["thinking_level"]
        for tok_k in ("prompt_tokens", "completion_tokens", "cached_tokens", "duration_ms", "worked_ms"):
            if _safe_nonneg_int(msg.get(tok_k)) > 0:
                msg_meta[tok_k] = _safe_nonneg_int(msg.get(tok_k))
        if isinstance(msg.get("tool_trace"), list) and msg["tool_trace"]:
            msg_meta["tool_trace"] = [item for item in msg["tool_trace"] if isinstance(item, dict)]
        if isinstance(msg.get("created_at"), (int, float)):
            msg_meta["created_at"] = float(msg["created_at"])
        if msg_meta:
            lines.append(f"<!-- chatint-msg-meta: {_dump_meta_json(msg_meta)} -->\n")

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
    raw_trace = m.get("tool_trace")
    tool_trace = [item for item in raw_trace if isinstance(item, dict)] if isinstance(raw_trace, list) else []
    out: Dict[str, Any] = {
        "role": _sanitize_msg_role(m.get("role", "user")),
        "content": str(m.get("content", "") or ""),
        "thinking_content": str(m.get("thinking_content", "") or ""),
        "provider_id": str(m.get("provider_id", "") or ""),
        "provider_name": str(m.get("provider_name", "") or ""),
        "model_id": str(m.get("model_id", "") or ""),
        "thinking_level": _normalize_thinking_level(m.get("thinking_level", "none")),
        "prompt_tokens": _safe_nonneg_int(m.get("prompt_tokens")),
        "completion_tokens": _safe_nonneg_int(m.get("completion_tokens")),
        "cached_tokens": _safe_nonneg_int(m.get("cached_tokens")),
        "duration_ms": _safe_nonneg_int(m.get("duration_ms")),
        "worked_ms": _safe_nonneg_int(m.get("worked_ms")),
        "tool_trace": tool_trace,
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


def _has_closing_marker(lines: List[str], start_idx: int, end_marker: str) -> bool:
    """Return True if a closing delimiter marker appears on or after start_idx."""
    needle = end_marker.lower()
    return any(needle in lines[j].lower() for j in range(start_idx, len(lines)))


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
                "workspace_dir": str(data.get("workspace_dir") or "").strip(),
                "agent_mode": "auto" if str(data.get("agent_mode") or "manual").strip().lower() == "auto" else "manual",
                "messages": messages,
            }
            if isinstance(data.get("is_auto_named"), bool):
                res["is_auto_named"] = data["is_auto_named"]
            if isinstance(data.get("created_at"), (int, float)):
                res["created_at"] = float(data["created_at"])
            if isinstance(data.get("updated_at"), (int, float)):
                res["updated_at"] = float(data["updated_at"])
            if isinstance(data.get("token_stats"), dict):
                res["token_stats"] = data["token_stats"]
            if isinstance(data.get("token_ledger"), list):
                res["token_ledger"] = [r for r in data["token_ledger"] if isinstance(r, dict)]
            return res
        if isinstance(data, list):
            messages = [_normalize_json_msg(m) for m in data if isinstance(m, dict)]
            return {
                "title": (Path(filename).stem if filename else "") or "Imported Chat",
                "system_prompt": "",
                "max_context": 30,
                "workspace_dir": "",
                "agent_mode": "manual",
                "messages": messages,
            }
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # 2. Parse Markdown format (fence-aware with unclosed-fence protection, system turns, and metadata)
    title = ""
    max_context = 30
    conv_workspace_dir = ""
    conv_agent_mode = "manual"
    conv_is_auto_named: Optional[bool] = None
    conv_created_at: Optional[float] = None
    conv_updated_at: Optional[float] = None
    conv_token_stats: Optional[Dict[str, Any]] = None
    conv_token_ledger: Optional[List[Dict[str, Any]]] = None
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
        raw_trace = current_msg_meta.get("tool_trace")
        msg_trace = [item for item in raw_trace if isinstance(item, dict)] if isinstance(raw_trace, list) else []
        msg_entry: Dict[str, Any] = {
            "role": current_role,
            "content": body,
            "thinking_content": thinking_content,
            "provider_id": str(current_msg_meta.get("provider_id", "") or ""),
            "provider_name": str(current_msg_meta.get("provider_name", "") or ""),
            "model_id": str(current_msg_meta.get("model_id") or current_model_id or ""),
            "thinking_level": resolved_thinking,
            "prompt_tokens": _safe_nonneg_int(current_msg_meta.get("prompt_tokens")),
            "completion_tokens": _safe_nonneg_int(current_msg_meta.get("completion_tokens")),
            "cached_tokens": _safe_nonneg_int(current_msg_meta.get("cached_tokens")),
            "duration_ms": _safe_nonneg_int(current_msg_meta.get("duration_ms")),
            "worked_ms": _safe_nonneg_int(current_msg_meta.get("worked_ms")),
            "tool_trace": msg_trace,
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
                if _has_closing_marker(raw_lines, idx, "<!-- chatint-thought-end -->"):
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
                    if _has_closing_marker(raw_lines, idx, "</details>"):
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
                            if parsed_cm.get("workspace_dir"):
                                conv_workspace_dir = str(parsed_cm["workspace_dir"]).strip()
                            if parsed_cm.get("agent_mode"):
                                conv_agent_mode = (
                                    "auto" if str(parsed_cm["agent_mode"]).strip().lower() == "auto" else "manual"
                                )
                            if isinstance(parsed_cm.get("is_auto_named"), bool):
                                conv_is_auto_named = parsed_cm["is_auto_named"]
                            if isinstance(parsed_cm.get("created_at"), (int, float)):
                                conv_created_at = float(parsed_cm["created_at"])
                            if isinstance(parsed_cm.get("updated_at"), (int, float)):
                                conv_updated_at = float(parsed_cm["updated_at"])
                            if isinstance(parsed_cm.get("token_stats"), dict):
                                conv_token_stats = parsed_cm["token_stats"]
                            if isinstance(parsed_cm.get("token_ledger"), list):
                                conv_token_ledger = [r for r in parsed_cm["token_ledger"] if isinstance(r, dict)]
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
            "provider_name": "",
            "model_id": "",
            "thinking_level": "none",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "duration_ms": 0,
            "worked_ms": 0,
            "tool_trace": [],
        })

    result: Dict[str, Any] = {
        "title": title,
        "system_prompt": system_prompt,
        "max_context": max_context,
        "workspace_dir": conv_workspace_dir,
        "agent_mode": conv_agent_mode,
        "messages": messages,
    }
    if conv_is_auto_named is not None:
        result["is_auto_named"] = conv_is_auto_named
    if conv_created_at is not None:
        result["created_at"] = conv_created_at
    if conv_updated_at is not None:
        result["updated_at"] = conv_updated_at
    if conv_token_stats is not None:
        result["token_stats"] = conv_token_stats
    if conv_token_ledger is not None:
        result["token_ledger"] = conv_token_ledger
    return result
