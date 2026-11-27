"""Sandboxed workspace file listing, reading, searching, writing, and editing tools."""
from __future__ import annotations

import difflib
import fnmatch
import inspect
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from chatint.tools.registry import ToolSpec, register_tool

_BLOCKED_FILENAMES = {
    ".env",
    ".env.local",
    ".env.tmp",
    "chatint.db",
    "chatint.db-wal",
    "chatint.db-shm",
    "chatint.db.bak",
}
_WIN_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}
_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", "build", "dist"}
_MAX_READ_LINES = 250
_MAX_READ_BYTES = 32 * 1024
_MAX_SEARCH_FILE_BYTES = 1024 * 1024
_MAX_WRITE_BYTES = 2 * 1024 * 1024
_MAX_DIFF_CHARS = 8000


def _coerce_int(value: Any, default: int, min_val: int, max_val: int) -> int:
    try:
        if isinstance(value, bool) or value is None:
            parsed = default
        else:
            parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return max(min_val, min(parsed, max_val))


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    low = str(value).strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off", ""):
        return False
    return default


def _is_reparse_link(path: Path) -> bool:
    """Return True for symlinks and Windows NTFS junctions."""
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _is_blocked_rel_parts(rel_parts: tuple[str, ...] | List[str]) -> bool:
    """Return True when relative workspace path parts target protected secrets, DB files, or internal logs."""
    if not rel_parts:
        return False
    norm_parts = [p.rstrip(". ").lower() for p in rel_parts]
    for part in norm_parts:
        if (
            part in _BLOCKED_FILENAMES
            or part.endswith((".db-wal", ".db-shm", ".db.bak"))
            or part.startswith(("chatint.db.", "chatint.db-"))
            or part == ".env"
            or part.startswith(".env.")
        ):
            return True
    return len(norm_parts) >= 2 and norm_parts[0] == "data" and (
        norm_parts[1] in ("logs", "conversations") or norm_parts[1].startswith("chatint.db")
    )


def _is_protected_app_path(resolved: Path) -> bool:
    """Block resolved targets inside ChatInt's active DB/conversations/logs directory."""
    try:
        from chatint.storage.connection import _get_db_path, get_conversations_dir

        db_path = _get_db_path().resolve()
        data_dir = db_path.parent
        convs_dir = get_conversations_dir(db_path).resolve()
        logs_dir = (data_dir / "logs").resolve()
        if resolved == convs_dir or resolved.is_relative_to(convs_dir):
            return True
        if resolved == logs_dir or resolved.is_relative_to(logs_dir):
            return True
        if resolved.parent == data_dir and resolved.name.lower().startswith(("chatint.db", ".env")):
            return True
    except Exception:
        pass
    return False


def _validate_rel_parts(parts: tuple[str, ...] | List[str]) -> None:
    """Reject NTFS Alternate Data Streams (`:`) and reserved Windows DOS device names."""
    for part in parts:
        if ":" in part:
            raise PermissionError(f"Alternate data stream or colon path '{part}' is not allowed.")
        stem = part.rstrip(". ").split(":", 1)[0].split(".", 1)[0].upper()
        if stem in _WIN_RESERVED_NAMES:
            raise PermissionError(f"Reserved Windows device name '{part}' is not allowed.")


def _is_allowed_walk_entry(root: Path, entry: Path, *, is_dir: bool) -> bool:
    """Verify walked directories/files stay inside `root`, skip reparse links, and hide protected paths."""
    if is_dir and entry.name in _SKIP_DIRS:
        return False
    if _is_reparse_link(entry):
        return False
    try:
        resolved = entry.resolve()
        if not resolved.is_relative_to(root):
            return False
        rel = resolved.relative_to(root)
        _validate_rel_parts(rel.parts)
        if (rel.parts and _is_blocked_rel_parts(rel.parts)) or _is_protected_app_path(resolved):
            return False
    except (OSError, PermissionError):
        return False
    return True


def resolve_workspace_path(workspace_root: Path, rel_path: str = ".", *, check_blocked: bool = True) -> Path:
    """
    Resolve a user/model-supplied path strictly inside `workspace_root`,
    blocking traversal, symlink/junction escapes, UNC paths, ADS, DOS device names, and secret/DB files.
    """
    if not workspace_root:
        raise PermissionError("No workspace folder is configured.")
    root = workspace_root.resolve()
    if not root.is_dir():
        raise PermissionError(f"Workspace folder does not exist: {root}")

    raw = (rel_path or ".").strip()
    if not raw or "\x00" in raw:
        raise PermissionError("Invalid file path.")
    if raw.startswith(("\\\\", "//")):
        raise PermissionError("Network/UNC paths are not allowed.")

    candidate_input = Path(raw)
    if candidate_input.drive and not candidate_input.is_absolute():
        raise PermissionError(f"Drive-relative path '{raw}' is not allowed.")
    input_tail = [p for p in candidate_input.parts if p and p != candidate_input.anchor]
    _validate_rel_parts(input_tail)

    candidate = candidate_input if candidate_input.is_absolute() else (root / candidate_input)
    resolved = candidate.resolve(strict=False)

    if not resolved.is_relative_to(root):
        raise PermissionError(f"Path '{raw}' escapes workspace root.")

    rel_parts = resolved.relative_to(root).parts
    _validate_rel_parts(rel_parts)

    if check_blocked and ((rel_parts and _is_blocked_rel_parts(rel_parts)) or _is_protected_app_path(resolved)):
        raise PermissionError(f"Access to protected path '{resolved.name}' is blocked.")

    return resolved


def _rel_display(root: Path, target: Path) -> str:
    rel = target.relative_to(root.resolve())
    return "." if str(rel) == "." else rel.as_posix()


def _unified_diff_preview(rel_label: str, before: str, after: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel_label}",
            tofile=f"b/{rel_label}",
            n=3,
        )
    )
    rendered = "".join(lines)
    if len(rendered) > _MAX_DIFF_CHARS:
        return rendered[:_MAX_DIFF_CHARS] + "\n... [diff truncated]"
    return rendered


def tool_list_dir(workspace_root: Path, path: Optional[str] = ".", max_depth: Any = 2) -> Dict[str, Any]:
    """List files and subdirectories inside a workspace directory up to `max_depth`."""
    safe_path = "." if path is None else str(path)
    try:
        root = workspace_root.resolve()
        target = resolve_workspace_path(root, safe_path or ".", check_blocked=True)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}

    if not target.exists():
        return {"ok": False, "error": f"Path not found: {safe_path}"}
    if not target.is_dir():
        return {"ok": False, "error": f"Not a directory: {safe_path}"}

    depth_cap = _coerce_int(max_depth, 2, 1, 5)
    entries: List[Dict[str, Any]] = []
    truncated = False
    base_depth = len(target.parts)

    for cur_root, dirnames, filenames in os.walk(target, followlinks=False):
        cur_path = Path(cur_root)
        cur_depth = len(cur_path.parts) - base_depth
        dirnames[:] = [d for d in sorted(dirnames) if _is_allowed_walk_entry(root, cur_path / d, is_dir=True)]
        if cur_depth >= depth_cap:
            dirnames[:] = []

        for dname in dirnames:
            dpath = cur_path / dname
            entries.append({"path": _rel_display(root, dpath) + "/", "type": "dir"})
            if len(entries) >= 200:
                truncated = True
                break
        if truncated:
            break

        for fname in sorted(filenames):
            fpath = cur_path / fname
            if not _is_allowed_walk_entry(root, fpath, is_dir=False):
                continue
            try:
                size = fpath.stat().st_size
            except OSError:
                size = 0
            entries.append({"path": _rel_display(root, fpath), "type": "file", "bytes": size})
            if len(entries) >= 200:
                truncated = True
                break
        if truncated:
            break

    return {
        "ok": True,
        "path": _rel_display(root, target),
        "entries": entries,
        "count": len(entries),
        "truncated": truncated,
    }


def tool_read_file(
    workspace_root: Path,
    path: Optional[str] = "",
    start_line: Any = 1,
    end_line: Any = 250,
) -> Dict[str, Any]:
    """Read a bounded 1-indexed line range from a UTF-8 text file inside the workspace."""
    safe_path = "" if path is None else str(path)
    try:
        root = workspace_root.resolve()
        target = resolve_workspace_path(root, safe_path)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}

    if not target.exists() or not target.is_file():
        return {"ok": False, "error": f"File not found: {safe_path}"}

    raw_bytes = target.read_bytes()
    if b"\x00" in raw_bytes[:8192]:
        return {"ok": False, "error": f"Cannot read binary file '{_rel_display(root, target)}'."}

    text = raw_bytes.decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    total_lines = len(all_lines)
    if total_lines == 0:
        return {
            "ok": True,
            "path": _rel_display(root, target),
            "start_line": 1,
            "end_line": 0,
            "total_lines": 0,
            "content": "",
            "truncated": False,
        }

    s_line = _coerce_int(start_line, 1, 1, total_lines)
    req_end = _coerce_int(end_line, s_line + _MAX_READ_LINES - 1, s_line, total_lines)
    e_line = min(total_lines, max(s_line, req_end), s_line + _MAX_READ_LINES - 1)

    numbered: List[str] = []
    byte_acc = 0
    actual_end = s_line - 1
    hit_byte_cap = False
    for idx in range(s_line, e_line + 1):
        formatted = f"{idx}: {all_lines[idx - 1]}"
        b_len = len(formatted.encode("utf-8")) + 1
        if numbered and byte_acc + b_len > _MAX_READ_BYTES:
            hit_byte_cap = True
            break
        numbered.append(formatted)
        byte_acc += b_len
        actual_end = idx

    return {
        "ok": True,
        "path": _rel_display(root, target),
        "start_line": s_line,
        "end_line": actual_end,
        "total_lines": total_lines,
        "content": "\n".join(numbered),
        "truncated": actual_end < total_lines or hit_byte_cap,
    }


def tool_search_files(
    workspace_root: Path,
    query: Optional[str] = "",
    path: Optional[str] = ".",
    glob_pattern: Optional[str] = "*",
    is_regex: Any = False,
) -> Dict[str, Any]:
    """Search UTF-8 text files inside the workspace returning up to 50 line-numbered matches."""
    clean_q = "" if query is None else str(query)
    safe_path = "." if path is None else str(path)
    regex_flag = _coerce_bool(is_regex, False)
    if not clean_q:
        return {"ok": False, "error": "Search query cannot be empty."}

    try:
        root = workspace_root.resolve()
        target = resolve_workspace_path(root, safe_path or ".", check_blocked=True)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}

    if not target.exists():
        return {"ok": False, "error": f"Path not found: {safe_path}"}

    pattern: Optional[re.Pattern[str]] = None
    if regex_flag:
        try:
            pattern = re.compile(clean_q)
        except re.error as exc:
            return {"ok": False, "error": f"Invalid regex pattern: {exc}"}

    pat_glob = (glob_pattern or "*").strip() or "*"
    files_to_scan: List[Path] = []
    if target.is_file():
        files_to_scan.append(target)
    else:
        for cur_root, dirnames, filenames in os.walk(target, followlinks=False):
            cur_path = Path(cur_root)
            dirnames[:] = [d for d in sorted(dirnames) if _is_allowed_walk_entry(root, cur_path / d, is_dir=True)]
            for fname in sorted(filenames):
                fpath = cur_path / fname
                if not _is_allowed_walk_entry(root, fpath, is_dir=False):
                    continue
                if pat_glob != "*" and not fnmatch.fnmatch(fname, pat_glob):
                    continue
                files_to_scan.append(fpath)

    matches: List[Dict[str, Any]] = []
    truncated = False
    for fpath in files_to_scan:
        try:
            if not fpath.resolve().is_relative_to(root):
                continue
            if fpath.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                continue
            raw = fpath.read_bytes()
            if b"\x00" in raw[:4096]:
                continue
            rel = _rel_display(root, fpath)
            for line_no, line_txt in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
                matched = bool(pattern.search(line_txt)) if pattern is not None else (clean_q in line_txt)
                if matched:
                    matches.append({"path": rel, "line": line_no, "text": line_txt[:300]})
                    if len(matches) >= 50:
                        truncated = True
                        break
        except OSError:
            continue
        if truncated:
            break

    return {"ok": True, "query": clean_q, "matches": matches, "count": len(matches), "truncated": truncated}


def preview_write_file(args: Dict[str, Any], workspace_root: Optional[Path]) -> Dict[str, Any]:
    """Compute pre-approval validation and diff preview for `write_file`."""
    if not workspace_root:
        return {"ok": False, "error": "No workspace folder is configured."}
    path = "" if args.get("path") is None else str(args.get("path"))
    content = "" if args.get("content") is None else str(args.get("content"))
    overwrite = _coerce_bool(args.get("overwrite"), True)
    if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
        return {"ok": False, "error": "File content exceeds 2 MB limit."}

    root = workspace_root.resolve()
    target = resolve_workspace_path(root, path)
    if target.exists() and target.is_dir():
        return {"ok": False, "error": f"Target path is a directory: {_rel_display(root, target)}"}
    rel = _rel_display(root, target)
    existed = target.exists()
    if existed and not overwrite:
        return {"ok": False, "error": f"File already exists and overwrite=False: {rel}"}
    before = target.read_text(encoding="utf-8", errors="replace") if existed and target.is_file() else ""
    diff = _unified_diff_preview(rel, before, content)
    action = "Overwrite" if existed else "Create"
    return {"ok": True, "path": rel, "summary": f"{action} {rel}", "diff": diff}


def tool_write_file(
    workspace_root: Path,
    path: Optional[str] = "",
    content: Optional[str] = "",
    overwrite: Any = True,
) -> Dict[str, Any]:
    """Create or overwrite a UTF-8 file inside the workspace and return diff metadata."""
    safe_path = "" if path is None else str(path)
    safe_content = "" if content is None else str(content)
    allow_overwrite = _coerce_bool(overwrite, True)
    try:
        root = workspace_root.resolve()
        target = resolve_workspace_path(root, safe_path)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}

    if len(safe_content.encode("utf-8")) > _MAX_WRITE_BYTES:
        return {"ok": False, "error": "File content exceeds 2 MB limit."}

    rel = _rel_display(root, target)
    existed = target.exists()
    if existed and not allow_overwrite:
        return {"ok": False, "error": f"File already exists and overwrite=False: {rel}"}
    if existed and target.is_dir():
        return {"ok": False, "error": f"Target path is a directory: {rel}"}

    before = target.read_text(encoding="utf-8", errors="replace") if existed else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(safe_content.encode("utf-8"))
    return {
        "ok": True,
        "path": rel,
        "created": not existed,
        "bytes_written": len(safe_content.encode("utf-8")),
        "diff": _unified_diff_preview(rel, before, safe_content),
    }


def _compute_edit_result(
    root: Path, path: str, target_text: str, replacement: str, replace_all: bool
) -> tuple[Optional[Path], str, str, str, Optional[str]]:
    if not target_text:
        return None, "", "", "", "Target text cannot be empty."
    resolved = resolve_workspace_path(root, path)
    rel = _rel_display(root, resolved)
    if not resolved.exists() or not resolved.is_file():
        return None, rel, "", "", f"File not found: {rel}"
    raw = resolved.read_bytes()
    if b"\x00" in raw[:8192]:
        return None, rel, "", "", f"Cannot edit binary file: {rel}"

    before = raw.decode("utf-8", errors="replace")
    effective_target = target_text
    effective_replacement = replacement
    occurrences = before.count(effective_target)
    if occurrences == 0 and "\r\n" in before and "\r\n" not in target_text and "\n" in target_text:
        crlf_target = target_text.replace("\n", "\r\n")
        crlf_count = before.count(crlf_target)
        if crlf_count > 0:
            effective_target = crlf_target
            effective_replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
            occurrences = crlf_count

    if occurrences == 0:
        return None, rel, before, "", f"Target snippet not found in {rel}."
    if occurrences > 1 and not replace_all:
        return None, rel, before, "", f"Target snippet matched {occurrences} times in {rel}; set replace_all=true or pass more context."

    after = (
        before.replace(effective_target, effective_replacement)
        if replace_all
        else before.replace(effective_target, effective_replacement, 1)
    )
    return resolved, rel, before, after, None


def preview_edit_file(args: Dict[str, Any], workspace_root: Optional[Path]) -> Dict[str, Any]:
    """Compute pre-approval validation and diff preview for `edit_file`."""
    if not workspace_root:
        return {"ok": False, "error": "No workspace folder is configured."}
    root = workspace_root.resolve()
    _res, rel, before, after, err = _compute_edit_result(
        root,
        "" if args.get("path") is None else str(args.get("path")),
        "" if args.get("target") is None else str(args.get("target")),
        "" if args.get("replacement") is None else str(args.get("replacement")),
        _coerce_bool(args.get("replace_all"), False),
    )
    if err:
        return {"ok": False, "path": rel, "error": err}
    return {"ok": True, "path": rel, "summary": f"Edit {rel}", "diff": _unified_diff_preview(rel, before, after)}


def tool_edit_file(
    workspace_root: Path,
    path: Optional[str] = "",
    target: Optional[str] = "",
    replacement: Optional[str] = "",
    replace_all: Any = False,
) -> Dict[str, Any]:
    """Apply an exact substring replacement inside a workspace file and return unified diff."""
    safe_path = "" if path is None else str(path)
    safe_target = "" if target is None else str(target)
    safe_replacement = "" if replacement is None else str(replacement)
    allow_all = _coerce_bool(replace_all, False)
    try:
        root = workspace_root.resolve()
        resolved, rel, before, after, err = _compute_edit_result(root, safe_path, safe_target, safe_replacement, allow_all)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}

    if err or resolved is None:
        return {"ok": False, "error": err or "Edit failed."}
    resolved.write_bytes(after.encode("utf-8"))
    applied_count = before.count(safe_target) or before.count(safe_target.replace("\n", "\r\n"))
    return {
        "ok": True,
        "path": rel,
        "replacements": applied_count if allow_all else 1,
        "diff": _unified_diff_preview(rel, before, after),
    }


def _ws_handler(fn):
    accepted = {k for k in inspect.signature(fn).parameters if k != "workspace_root"}

    def _wrapped(workspace_dir: str = "", **kwargs: Any) -> Dict[str, Any]:
        if not workspace_dir:
            return {"ok": False, "error": "No workspace folder is configured."}
        filtered = {k: v for k, v in kwargs.items() if k in accepted}
        return fn(Path(workspace_dir), **filtered)

    return _wrapped


def register_workspace_tools() -> None:
    """Register sandboxed read and write workspace tools in the unified registry."""
    register_tool(
        ToolSpec(
            name="list_dir",
            description="List files and subdirectories inside the active workspace folder.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path inside workspace (default '.')."},
                    "max_depth": {"type": "integer", "description": "Directory depth between 1 and 5 (default 2)."},
                },
                "required": [],
            },
            category="workspace_read",
            requires_workspace=True,
            mutating=False,
            handler=_ws_handler(tool_list_dir),
        )
    )
    register_tool(
        ToolSpec(
            name="read_file",
            description="Read a line-numbered text slice (up to 250 lines) from a file inside the workspace.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file inside workspace."},
                    "start_line": {"type": "integer", "description": "1-based starting line number (default 1)."},
                    "end_line": {"type": "integer", "description": "1-based inclusive end line number (default 250)."},
                },
                "required": ["path"],
            },
            category="workspace_read",
            requires_workspace=True,
            mutating=False,
            handler=_ws_handler(tool_read_file),
        )
    )
    register_tool(
        ToolSpec(
            name="search_files",
            description="Search text or regex patterns across files inside the workspace.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring or regex pattern to search for."},
                    "path": {"type": "string", "description": "Relative directory or file path (default '.')."},
                    "glob_pattern": {"type": "string", "description": "Optional filename glob filter (e.g. '*.py')."},
                    "is_regex": {"type": "boolean", "description": "Treat query as regular expression (default false)."},
                },
                "required": ["query"],
            },
            category="workspace_read",
            requires_workspace=True,
            mutating=False,
            handler=_ws_handler(tool_search_files),
        )
    )
    register_tool(
        ToolSpec(
            name="write_file",
            description="Create or overwrite a text file inside the workspace.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative target file path inside workspace."},
                    "content": {"type": "string", "description": "Full text content to write."},
                    "overwrite": {"type": "boolean", "description": "Allow overwriting an existing file (default true)."},
                },
                "required": ["path", "content"],
            },
            category="workspace_write",
            requires_workspace=True,
            mutating=True,
            handler=_ws_handler(tool_write_file),
            preview_fn=preview_write_file,
        )
    )
    register_tool(
        ToolSpec(
            name="edit_file",
            description="Replace an exact existing text block in a workspace file.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative target file path inside workspace."},
                    "target": {"type": "string", "description": "Exact existing text to replace."},
                    "replacement": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of requiring a unique match."},
                },
                "required": ["path", "target", "replacement"],
            },
            category="workspace_write",
            requires_workspace=True,
            mutating=True,
            handler=_ws_handler(tool_edit_file),
            preview_fn=preview_edit_file,
        )
    )
