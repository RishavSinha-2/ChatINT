from __future__ import annotations

import ctypes
from ctypes import wintypes
import mimetypes
import sys
import threading
from typing import Any
from urllib.parse import urlparse

import webview

from chatint.bridge import DesktopBridge, _normalize_file_uri_key
from chatint.config import UI_DIR
from chatint.db import init_db
from chatint.logging_setup import configure_logging, get_logger

DEFAULT_MUTEX_NAME = "ChatInt_SingleInstance_Mutex_Local"
_MUTEX_HANDLE = None
_logger = get_logger("chatint.main")


def _find_existing_chatint_hwnd(user32: Any, kernel32: Any) -> int:
    """Enumerate visible top-level ChatInt windows while ignoring Explorer folders or unrelated apps."""
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    matched_hwnd = 0

    def _callback(hwnd: int, _lparam: int) -> bool:
        nonlocal matched_hwnd
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length != len("ChatInt"):
            return True
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        if title_buf.value != "ChatInt":
            return True

        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cls_name = cls_buf.value
        if cls_name in ("CabinetWClass", "ExploreWClass"):
            return True

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc_ok = False
        proc_check_unavailable = False
        if pid.value:
            proc_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not proc_handle:
                proc_check_unavailable = True
            else:
                try:
                    exe_buf = ctypes.create_unicode_buffer(1024)
                    exe_len = wintypes.DWORD(len(exe_buf))
                    if kernel32.QueryFullProcessImageNameW(proc_handle, 0, exe_buf, ctypes.byref(exe_len)):
                        exe_low = exe_buf.value.lower()
                        if exe_low.endswith(("chatint.exe", "python.exe", "pythonw.exe")):
                            proc_ok = True
                    else:
                        proc_check_unavailable = True
                finally:
                    kernel32.CloseHandle(proc_handle)
        else:
            proc_check_unavailable = True

        # Only fall back to WindowsForms10 class check when OpenProcess/QueryFullProcessImageNameW
        # could not inspect the process—never when it proved the executable is NOT ChatInt/Python (fixes M10).
        if proc_ok or (proc_check_unavailable and cls_name.startswith("WindowsForms10")):
            matched_hwnd = int(hwnd)
            return False
        return True

    cb = EnumWindowsProc(_callback)
    user32.EnumWindows(cb, 0)
    return matched_hwnd


def _ensure_single_instance(mutex_name: str = DEFAULT_MUTEX_NAME) -> bool:
    """
    Enforce a single running instance of ChatInt on Windows.
    Accepts `mutex_name` so unit tests can use an isolated mutex without colliding with a live session (fixes L6).
    """
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True

    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32

        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD

        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        _MUTEX_HANDLE = kernel32.CreateMutexW(None, True, mutex_name)
        # ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == 183:
            if mutex_name == DEFAULT_MUTEX_NAME:
                hwnd = _find_existing_chatint_hwnd(user32, kernel32)
                if hwnd:
                    if user32.IsIconic(hwnd):
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE only if minimized
                    user32.SetForegroundWindow(hwnd)
            return False
    except Exception as exc:
        _logger.warning("Single-instance check skipped: %s", exc)
    return True


def _prewarm_sdks() -> None:
    try:
        import openai  # noqa: F401
    except Exception:
        pass
    try:
        import anthropic  # noqa: F401
    except Exception:
        pass
    try:
        from google import genai  # noqa: F401
    except Exception:
        pass


def _attach_origin_guard(window: webview.Window, bridge: DesktopBridge, index_html_path) -> None:
    """
    Pin the bridge to the exact local app URL/origin and block navigation to external or unrelated local URLs
    before any untrusted document can execute (fixes C1).
    """
    resolved_index_uri = index_html_path.resolve().as_uri()
    resolved_index_key = _normalize_file_uri_key(resolved_index_uri)
    pinned_origin: list[tuple[str, str, int | None] | None] = [None]
    pinned_url: list[str] = [resolved_index_uri]

    def _is_initial_app_url(candidate_url: str) -> bool:
        if not candidate_url:
            return False
        parsed = urlparse(candidate_url)
        scheme = (parsed.scheme or "").lower()
        if scheme == "file":
            return _normalize_file_uri_key(candidate_url) == resolved_index_key
        real_url = str(getattr(window, "real_url", "") or "").strip()
        if real_url:
            rp = urlparse(real_url)
            return (
                scheme == (rp.scheme or "").lower()
                and (parsed.hostname or "").lower() == (rp.hostname or "").lower()
                and parsed.port == rp.port
                and (parsed.path or "") in ("", "/", "/index.html")
            )
        return False

    def _matches_pinned(candidate_url: str) -> bool:
        if not candidate_url or pinned_origin[0] is None:
            return False
        parsed = urlparse(candidate_url)
        scheme = (parsed.scheme or "").lower()
        p_scheme, p_host, p_port = pinned_origin[0]
        if p_scheme == "file":
            return scheme == "file" and _normalize_file_uri_key(candidate_url) == resolved_index_key
        return (
            scheme == p_scheme
            and (parsed.hostname or "").lower() == p_host
            and parsed.port == p_port
            and (parsed.path or "") in ("", "/", "/index.html")
        )

    def _pin_from_real_url() -> None:
        real_url = str(getattr(window, "real_url", "") or "").strip()
        if real_url and pinned_origin[0] is None and _is_initial_app_url(real_url):
            rp = urlparse(real_url)
            origin = ((rp.scheme or "").lower(), (rp.hostname or "").lower(), rp.port)
            pinned_origin[0] = origin
            pinned_url[0] = real_url
            bridge._attach_window(window, real_url, allowed_origin=origin)
            bridge._set_origin_trusted(True)

    nav_guard_attached = [False]

    def _ensure_wv2_nav_guard() -> None:
        if nav_guard_attached[0]:
            return
        try:
            from webview.platforms import edgechromium  # type: ignore

            view_instance = edgechromium.BrowserView.instances.get(window.uid)
            wv2_ctrl = getattr(view_instance, "webview", None)
            if wv2_ctrl is not None and hasattr(wv2_ctrl, "NavigationStarting"):

                def _on_nav_starting(_sender, nav_args):
                    try:
                        uri = str(getattr(nav_args, "Uri", "") or "").strip()
                        if not uri or uri == "about:blank":
                            return
                        _pin_from_real_url()
                        allowed = _matches_pinned(uri) if pinned_origin[0] is not None else _is_initial_app_url(uri)
                        if not allowed:
                            nav_args.Cancel = True
                            _logger.warning("Cancelled WebView2 navigation to untrusted URI: %s", uri)
                    except Exception as nav_exc:
                        _logger.debug("NavigationStarting check error: %s", nav_exc)

                wv2_ctrl.NavigationStarting += _on_nav_starting
                nav_guard_attached[0] = True
        except Exception as guard_exc:
            _logger.debug("WebView2 NavigationStarting hook deferred: %s", guard_exc)

    def _on_initialized(*_args) -> None:
        _pin_from_real_url()
        _ensure_wv2_nav_guard()

    def _on_shown(*_args) -> None:
        _pin_from_real_url()
        _ensure_wv2_nav_guard()

    def _verify_current_url() -> None:
        _pin_from_real_url()
        _ensure_wv2_nav_guard()
        try:
            current_url = str(window.get_current_url() or "").strip()
        except Exception:
            bridge._set_origin_trusted(False)
            return
        if not current_url:
            return

        if pinned_origin[0] is None and _is_initial_app_url(current_url):
            parsed = urlparse(current_url)
            origin = ((parsed.scheme or "").lower(), (parsed.hostname or "").lower(), parsed.port)
            pinned_origin[0] = origin
            pinned_url[0] = current_url
            bridge._attach_window(window, current_url, allowed_origin=origin)
            bridge._set_origin_trusted(True)
            return

        if _matches_pinned(current_url):
            bridge._set_origin_trusted(True)
            return

        bridge._set_origin_trusted(False)
        _logger.warning("Blocked navigation to external/untrusted URL: %s", current_url)
        threading.Timer(0.01, window.load_url, args=(pinned_url[0],)).start()

    window.events.initialized += _on_initialized
    window.events.shown += _on_shown
    window.events.before_load += _verify_current_url
    window.events.loaded += _verify_current_url
    window.events.closing += bridge.flush_tabs_state


def main() -> None:
    configure_logging()

    if not _ensure_single_instance():
        sys.exit(0)

    # Ensure Windows Registry HKCR overrides (e.g. .js -> text/plain) never break native ES modules in WebView2
    mimetypes.init()
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/javascript", ".mjs")
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("font/woff2", ".woff2")

    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.settings["ALLOW_FILE_URLS"] = False
    except Exception:
        pass

    threading.Thread(target=_prewarm_sdks, daemon=True, name="sdk-prewarm").start()

    init_db()

    bridge = DesktopBridge(allow_headless_rpc=False)

    index_html = UI_DIR / "index.html"
    if not index_html.exists():
        _logger.error("UI file not found at %s", index_html)
        sys.exit(1)

    window = webview.create_window(
        title="ChatInt",
        url=str(index_html),
        js_api=bridge.as_public_api(),
        width=1220,
        height=820,
        min_size=(800, 580),
        background_color="#131314",
    )
    bridge._attach_window(window, index_html.resolve().as_uri())
    _attach_origin_guard(window, bridge, index_html)

    icon_path = UI_DIR / "assets" / "logo.ico"
    webview.start(debug=False, icon=str(icon_path) if icon_path.exists() else None)


if __name__ == "__main__":
    main()
