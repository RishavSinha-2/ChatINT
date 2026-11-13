import ctypes
from ctypes import wintypes
import sys
import threading
from typing import Any
from urllib.parse import urlparse
import webview
from chatint.bridge import DesktopBridge
from chatint.config import UI_DIR
from chatint.db import init_db
from chatint.logging_setup import configure_logging, get_logger

# Windows Single-Instance Mutex
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
        if pid.value:
            proc_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if proc_handle:
                try:
                    exe_buf = ctypes.create_unicode_buffer(1024)
                    exe_len = wintypes.DWORD(len(exe_buf))
                    if kernel32.QueryFullProcessImageNameW(proc_handle, 0, exe_buf, ctypes.byref(exe_len)):
                        exe_low = exe_buf.value.lower()
                        if exe_low.endswith(("chatint.exe", "python.exe", "pythonw.exe")):
                            proc_ok = True
                finally:
                    kernel32.CloseHandle(proc_handle)

        if proc_ok or cls_name.startswith("WindowsForms10"):
            matched_hwnd = int(hwnd)
            return False
        return True

    cb = EnumWindowsProc(_callback)
    user32.EnumWindows(cb, 0)
    return matched_hwnd


def _ensure_single_instance() -> bool:
    """
    Enforces a single running instance of ChatInt on Windows.
    If another instance is already running, restores (only if minimized) and brings its window
    to the foreground, then exits this instance cleanly.
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

        mutex_name = "ChatInt_SingleInstance_Mutex_Local"
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, True, mutex_name)
        # ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == 183:
            hwnd = _find_existing_chatint_hwnd(user32, kernel32)
            if hwnd:
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE only if minimized
                user32.SetForegroundWindow(hwnd)
            return False
    except Exception as exc:
        _logger.warning("Single-instance check skipped: %s", exc)
    return True


def _prewarm_sdks():
    """
    Pre-warm AI SDK libraries in a background daemon thread
    so that the native desktop window opens instantaneously
    while packages compile and cache in the background.
    """
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


def _attach_origin_guard(window: webview.Window, bridge: DesktopBridge, index_html_path):
    """Pin the bridge to the exact local app origin and never leave the privileged window on external URLs."""
    app_origin: list[tuple[str, str] | None] = [None]
    app_url: list[str] = [index_html_path.resolve().as_uri()]

    def _on_loaded():
        try:
            current_url = str(window.get_current_url() or "").strip()
        except Exception:
            return
        if not current_url:
            return

        parsed = urlparse(current_url)
        origin = (parsed.scheme.lower(), parsed.netloc.lower())

        if app_origin[0] is None:
            is_local_app = (
                parsed.scheme.lower() == "file"
                or (
                    parsed.hostname in ("127.0.0.1", "localhost")
                    and parsed.path.rstrip("/").endswith(("/index.html", ""))
                )
            )
            if is_local_app:
                app_origin[0] = origin
                app_url[0] = current_url
                bridge.attach_window(window, current_url, allowed_origin=origin)
                bridge.set_origin_trusted(True)
                return

        if app_origin[0] is not None and origin == app_origin[0]:
            bridge.set_origin_trusted(True)
            return

        # Unexpected navigation away from local app origin -> revoke RPC trust and restore app URL
        bridge.set_origin_trusted(False)
        _logger.warning("Blocked navigation to external/untrusted URL: %s", current_url)
        threading.Timer(0.01, window.load_url, args=(app_url[0],)).start()

    window.events.loaded += _on_loaded


def main():
    configure_logging()

    # Enforce single instance: if another window is already open, focus it and exit
    if not _ensure_single_instance():
        sys.exit(0)

    # Ensure pywebview routes any external target=_blank/link clicks to the OS browser
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    except Exception:
        pass

    # Start SDK background pre-warming thread immediately
    threading.Thread(target=_prewarm_sdks, daemon=True, name="sdk-prewarm").start()

    # Initialize SQLite database
    init_db()

    # Instantiate JS API Bridge
    bridge = DesktopBridge()

    index_html = UI_DIR / "index.html"
    if not index_html.exists():
        _logger.error("UI file not found at %s", index_html)
        sys.exit(1)

    # Create native standalone window
    window = webview.create_window(
        title="ChatInt",
        url=str(index_html),
        js_api=bridge,
        width=1220,
        height=820,
        min_size=(800, 580),
        background_color="#131314",
    )
    bridge.attach_window(window, index_html.resolve().as_uri())
    _attach_origin_guard(window, bridge, index_html)

    # Launch desktop application loop
    icon_path = UI_DIR / "assets" / "logo.ico"
    webview.start(debug=False, icon=str(icon_path) if icon_path.exists() else None)


if __name__ == "__main__":
    main()


