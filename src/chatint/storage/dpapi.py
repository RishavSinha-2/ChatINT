"""Windows DPAPI at-rest secret encryption with transparent plaintext fallback."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import os
import sys

from chatint.logging_setup import get_logger

_logger = get_logger("chatint.storage.dpapi")
DPAPI_PREFIX = "dpapi:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_PLAIN_PLACEHOLDERS = {"", "ollama"}


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def is_dpapi_enabled() -> bool:
    if sys.platform != "win32":
        return False
    return os.environ.get("CHATINT_DISABLE_DPAPI", "").strip() != "1"


def encrypt_secret_for_storage(plaintext: str) -> str:
    """Encrypt an API key using Windows user-scoped DPAPI when available."""
    if not plaintext:
        return ""
    if plaintext.startswith(DPAPI_PREFIX):
        return plaintext
    if plaintext.strip().lower() in _PLAIN_PLACEHOLDERS:
        return plaintext
    if not is_dpapi_enabled():
        return plaintext

    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        raw_bytes = plaintext.encode("utf-8")
        in_buf = ctypes.create_string_buffer(raw_bytes, len(raw_bytes))
        in_blob = _DATA_BLOB(len(raw_bytes), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = _DATA_BLOB()

        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "ChatInt API Key",
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok or not out_blob.pbData:
            _logger.warning("DPAPI CryptProtectData returned False; keeping plaintext fallback.")
            return plaintext

        try:
            encrypted_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))

        return DPAPI_PREFIX + base64.b64encode(encrypted_bytes).decode("ascii")
    except Exception as exc:
        _logger.warning("DPAPI encryption fallback to plaintext: %s", exc)
        return plaintext


def decrypt_secret_from_storage(stored: str) -> str:
    """Decrypt a DPAPI-protected secret or return plaintext .env entries unchanged."""
    if not stored or not stored.startswith(DPAPI_PREFIX):
        return stored or ""
    if sys.platform != "win32":
        _logger.warning("Cannot decrypt DPAPI-protected secret on non-Windows platform (%s).", sys.platform)
        return ""

    b64_payload = stored[len(DPAPI_PREFIX) :].strip()
    if not b64_payload:
        return ""

    try:
        encrypted_bytes = base64.b64decode(b64_payload)
    except Exception as exc:
        _logger.warning("Invalid base64 in DPAPI secret payload: %s", exc)
        return ""

    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        in_buf = ctypes.create_string_buffer(encrypted_bytes, len(encrypted_bytes))
        in_blob = _DATA_BLOB(len(encrypted_bytes), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = _DATA_BLOB()

        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok or not out_blob.pbData:
            _logger.warning("DPAPI CryptUnprotectData failed for stored secret (different Windows user or machine?).")
            return ""

        try:
            plain_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))

        return plain_bytes.decode("utf-8")
    except Exception as exc:
        _logger.warning("DPAPI decryption failed: %s", exc)
        return ""
