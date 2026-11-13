"""Thread-safe, atomic .env secret management with DPAPI at-rest encryption."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Dict, Iterable, List, Optional

from chatint.config import DEFAULT_PROVIDERS, ENV_PATH as _DEFAULT_ENV_PATH
from chatint.logging_setup import get_logger, replace_known_secrets
from chatint.storage.dpapi import decrypt_secret_from_storage, encrypt_secret_for_storage

_ENV_LOCK = threading.RLock()
_logger = get_logger("chatint.storage.secrets")


def _get_env_path() -> Path:
    db_mod = sys.modules.get("chatint.db")
    if db_mod is not None and hasattr(db_mod, "ENV_PATH"):
        return getattr(db_mod, "ENV_PATH")
    return _DEFAULT_ENV_PATH


def mask_api_key(api_key: str) -> str:
    """Return a non-sensitive masked display hint for a saved API key."""
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 4:
        return "•" * len(key)
    if len(key) <= 10:
        return f"{key[:2]}••••{key[-2:]}"
    return f"{key[:4]}••••••{key[-4:]}"


def sanitize_provider_name(name: str) -> str:
    """Remove any non-English alphabet characters from a provider name."""
    return re.sub(r"[^A-Za-z]", "", (name or "").strip())


def is_valid_provider_name(name: str) -> bool:
    """Provider name must consist exclusively of English alphabet letters (A-Z, a-z)."""
    return bool(name and re.fullmatch(r"[A-Za-z]+", name))


def provider_env_key(provider_name: str) -> str:
    clean = sanitize_provider_name(provider_name)
    return f"{clean.upper()}_API_KEY"


def is_saved_secret_non_placeholder(api_key: Optional[str]) -> bool:
    """Return True if api_key is a real configured secret (not blank or 'ollama')."""
    if not api_key:
        return False
    return api_key.strip().lower() not in ("", "ollama")


def _normalize_env_key_name(raw_key: str) -> str:
    clean = (raw_key or "").strip()
    if clean.upper().endswith("_API_KEY"):
        return clean.upper()
    return clean


def _parse_env_value(raw_val: str) -> str:
    val = (raw_val or "").strip()
    if not val:
        return ""
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        inner = val[1:-1]
        out: List[str] = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt == "n":
                    out.append("\n")
                elif nxt == "r":
                    out.append("\r")
                elif nxt == "t":
                    out.append("\t")
                elif nxt in ('"', "\\"):
                    out.append(nxt)
                else:
                    out.append("\\" + nxt)
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    comment_match = re.search(r"\s+#", val)
    if comment_match:
        val = val[: comment_match.start()].rstrip()
    return val


def _format_env_value(value: Any) -> str:
    val = "" if value is None else str(value).replace("\r\n", "\n")
    if val == "":
        return ""
    needs_quotes = any(ch.isspace() for ch in val) or any(ch in val for ch in ("#", '"', "'", "=", "\\"))
    if not needs_quotes:
        return val
    escaped = (
        val.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _atomic_write_env(env_path: Path, content: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = env_path.parent / f".{env_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, env_path)
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _ensure_env_text_locked(env_path: Path) -> str:
    """Ensure .env exists with all built-in provider entries and return its raw text in at most 1 read."""
    raw_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if not raw_text.strip():
        lines = []
        for p in DEFAULT_PROVIDERS:
            clean = sanitize_provider_name(p.get("name", ""))
            if clean:
                default_val = "ollama" if clean.lower() == "ollama" else (p.get("api_key") or "")
                stored_val = encrypt_secret_for_storage(default_val) if default_val else ""
                lines.append(f"{clean.upper()}_API_KEY={_format_env_value(stored_val)}")
        new_content = "\n".join(lines) + ("\n" if lines else "")
        _atomic_write_env(env_path, new_content)
        return new_content

    existing_keys = set()
    for line in raw_text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            existing_keys.add(s.split("=", 1)[0].strip().upper())

    appended = []
    for p in DEFAULT_PROVIDERS:
        clean = sanitize_provider_name(p.get("name", ""))
        if clean:
            env_k = f"{clean.upper()}_API_KEY"
            if env_k not in existing_keys:
                default_val = "ollama" if clean.lower() == "ollama" else (p.get("api_key") or "")
                stored_val = encrypt_secret_for_storage(default_val) if default_val else ""
                appended.append(f"{env_k}={_format_env_value(stored_val)}")

    if appended:
        prefix = raw_text if raw_text.endswith("\n") else (raw_text + "\n")
        updated_text = prefix + "\n".join(appended) + "\n"
        _atomic_write_env(env_path, updated_text)
        return updated_text

    return raw_text


def ensure_env_file() -> None:
    with _ENV_LOCK:
        _ensure_env_text_locked(_get_env_path())


def _sync_log_redactor(env_map: Dict[str, str]) -> None:
    secrets = [v for k, v in env_map.items() if k.upper().endswith("_API_KEY") and v]
    replace_known_secrets(secrets)


def read_env_dict() -> Dict[str, str]:
    with _ENV_LOCK:
        env_path = _get_env_path()
        raw_text = _ensure_env_text_locked(env_path)
        env_map: Dict[str, str] = {}
        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            k_norm = _normalize_env_key_name(k)
            if not k_norm:
                continue
            parsed_val = _parse_env_value(v)
            if k_norm.endswith("_API_KEY"):
                parsed_val = decrypt_secret_from_storage(parsed_val)
            env_map[k_norm] = parsed_val
        _sync_log_redactor(env_map)
        return env_map


def write_env_dict(env_map: Dict[str, str]) -> None:
    with _ENV_LOCK:
        env_path = _get_env_path()
        raw_text = _ensure_env_text_locked(env_path)
        existing_lines = raw_text.splitlines()

        normalized_remaining: Dict[str, str] = {}
        plain_snapshot: Dict[str, str] = {}
        for raw_k, raw_v in env_map.items():
            k_norm = _normalize_env_key_name(raw_k)
            if not k_norm:
                continue
            str_val = "" if raw_v is None else str(raw_v)
            plain_snapshot[k_norm] = decrypt_secret_from_storage(str_val) if k_norm.endswith("_API_KEY") else str_val
            if k_norm.endswith("_API_KEY"):
                normalized_remaining[k_norm] = encrypt_secret_for_storage(plain_snapshot[k_norm])
            else:
                normalized_remaining[k_norm] = str_val

        out_lines: List[str] = []
        seen_keys = set()

        for raw_line in existing_lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                out_lines.append(raw_line)
                continue
            k, _ = stripped.split("=", 1)
            k_norm = _normalize_env_key_name(k)
            if k_norm in normalized_remaining:
                if k_norm not in seen_keys:
                    out_lines.append(f"{k_norm}={_format_env_value(normalized_remaining.pop(k_norm))}")
                    seen_keys.add(k_norm)

        for k_norm, v_formatted in normalized_remaining.items():
            out_lines.append(f"{k_norm}={_format_env_value(v_formatted)}")

        content = "\n".join(out_lines) + ("\n" if out_lines else "")
        _atomic_write_env(env_path, content)
        _sync_log_redactor(plain_snapshot)


def get_api_key_from_env(provider_name: str) -> str:
    key_name = provider_env_key(provider_name)
    if key_name == "_API_KEY":
        return ""
    env_map = read_env_dict()
    return env_map.get(key_name, "")


def set_api_key_in_env(
    provider_name: str,
    api_key: Optional[str],
    old_provider_name: Optional[str] = None,
    *,
    clear: bool = False,
) -> None:
    """
    Set or clear a provider API key in .env atomically.
    - Preserves intentional whitespace inside api_key (never calls .strip() on secrets).
    - Clears the secret cleanly even when renaming the provider in the same operation.
    """
    with _ENV_LOCK:
        env_map = read_env_dict()
        old_val = ""
        if old_provider_name and sanitize_provider_name(old_provider_name).upper() != sanitize_provider_name(provider_name).upper():
            old_key = provider_env_key(old_provider_name)
            old_val = env_map.pop(old_key, "")
        new_key = provider_env_key(provider_name)
        if new_key != "_API_KEY":
            if clear or api_key == "":
                final_val = ""
            elif api_key is None:
                final_val = old_val
            else:
                final_val = str(api_key)
            env_map[new_key] = final_val
        write_env_dict(env_map)


def remove_api_key_from_env(provider_name: str) -> None:
    with _ENV_LOCK:
        env_map = read_env_dict()
        key_name = provider_env_key(provider_name)
        if key_name in env_map:
            env_map.pop(key_name, None)
            write_env_dict(env_map)


def batch_update_provider_env_keys(
    updates: Iterable[Dict[str, Any]],
    removals: Iterable[str] = (),
) -> None:
    """
    Apply multiple provider key updates/renames/clears/deletions in a single .env read+write cycle.
    Each item in `updates` has keys:
      - provider_name: str
      - api_key: Optional[str] (None means keep existing / migrate old_provider_name)
      - old_provider_name: Optional[str]
      - clear: bool
      - default_if_missing: Optional[str]
    """
    with _ENV_LOCK:
        env_map = read_env_dict()
        dirty = False

        for rem_name in removals:
            rem_key = provider_env_key(rem_name)
            if rem_key != "_API_KEY" and rem_key in env_map:
                env_map.pop(rem_key, None)
                dirty = True

        for item in updates:
            prov_name = item.get("provider_name") or ""
            old_name = item.get("old_provider_name")
            api_key = item.get("api_key")
            clear = bool(item.get("clear", False))
            default_if_missing = item.get("default_if_missing")

            new_key = provider_env_key(prov_name)
            if new_key == "_API_KEY":
                continue

            old_val = ""
            if old_name and sanitize_provider_name(old_name).upper() != sanitize_provider_name(prov_name).upper():
                old_key = provider_env_key(old_name)
                if old_key in env_map:
                    old_val = env_map.pop(old_key, "")
                    dirty = True

            prev_val = env_map.get(new_key)
            if clear:
                target_val = ""
            elif api_key is not None:
                target_val = str(api_key)
            elif old_val:
                target_val = old_val
            elif prev_val is not None and prev_val != "":
                target_val = prev_val
            elif default_if_missing is not None:
                target_val = str(default_if_missing)
            else:
                target_val = prev_val if prev_val is not None else ""

            if env_map.get(new_key) != target_val:
                env_map[new_key] = target_val
                dirty = True

        if dirty:
            write_env_dict(env_map)
