import ctypes
import sys
import threading
from urllib.parse import urlparse
import webview
from chatint.bridge import DesktopBridge
from chatint.config import UI_DIR
from chatint.db import init_db

# Windows Single-Instance Mutex
_MUTEX_HANDLE = None


def _ensure_single_instance() -> bool:
    """
    Enforces a single running instance of ChatInt on Windows.
    If another instance is already running, restores and brings its window
    to the foreground, then exits this instance cleanly.
    """
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True

    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        mutex_name = "ChatInt_SingleInstance_Mutex_Local"

        _MUTEX_HANDLE = kernel32.CreateMutexW(None, True, mutex_name)
        # ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == 183:
            hwnd = user32.FindWindowW(None, "ChatInt")
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            return False
    except Exception:
        pass
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


_EXTERNAL_NAV_BAR_JS = r"""
(() => {
  if (document.getElementById('app-container') && document.getElementById('panes-container')) return;
  if (document.getElementById('chatint-ext-nav-host')) return;
  const host = document.createElement('div');
  host.id = 'chatint-ext-nav-host';
  host.style.cssText = 'position:fixed;top:12px;right:16px;z-index:2147483647;pointer-events:auto;';
  const shadow = host.attachShadow({ mode: 'open' });
  const domain = (() => {
    try { return new URL(window.location.href).hostname || 'Web Page'; }
    catch { return 'Web Page'; }
  })();
  const style = document.createElement('style');
  style.textContent = `
    .bar {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 8px;
      border-radius: 999px;
      background: rgba(19, 19, 20, 0.92);
      border: 1px solid rgba(255, 255, 255, 0.16);
      box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45);
      backdrop-filter: blur(8px);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #f0f4f9;
      font-size: 12px;
    }
    .domain {
      max-width: 150px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: #9aa0a6;
      padding: 0 4px;
      font-size: 11px;
    }
    button {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border: 1px solid rgba(255, 255, 255, 0.14);
      background: #282a2c;
      color: #f0f4f9;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 11.5px;
      font-weight: 500;
      font-family: inherit;
      cursor: pointer;
      transition: background 0.14s, border-color 0.14s;
    }
    button:hover {
      background: #333538;
      border-color: #60b8ff;
    }
    .back-btn {
      background: rgba(96, 184, 255, 0.16);
      border-color: rgba(96, 184, 255, 0.45);
      color: #8fd0ff;
    }
    .back-btn:hover {
      background: rgba(96, 184, 255, 0.26);
    }
  `;

  const bar = document.createElement('div');
  bar.className = 'bar';

  const backBtn = document.createElement('button');
  backBtn.type = 'button';
  backBtn.className = 'back-btn';
  backBtn.title = 'Back to Chat';
  backBtn.textContent = '\u2190 Back to Chat';

  const domainSpan = document.createElement('span');
  domainSpan.className = 'domain';
  domainSpan.textContent = domain;

  const extBtn = document.createElement('button');
  extBtn.type = 'button';
  extBtn.title = 'Open in Default Browser';
  extBtn.textContent = 'Open in Browser \u2197';

  bar.appendChild(backBtn);
  bar.appendChild(domainSpan);
  bar.appendChild(extBtn);
  shadow.appendChild(style);
  shadow.appendChild(bar);

  const callBridge = async (method, ...args) => {
    for (let i = 0; i < 30; i++) {
      if (window.pywebview && window.pywebview.api && typeof window.pywebview.api[method] === 'function') {
        return window.pywebview.api[method](...args);
      }
      await new Promise((r) => setTimeout(r, 35));
    }
    return null;
  };

  backBtn.onclick = () => callBridge('return_to_chat');
  extBtn.onclick = () => callBridge('open_external_url', window.location.href);
  (document.body || document.documentElement).appendChild(host);
})();
"""


def _attach_external_navigation_overlay(window: webview.Window, bridge: DesktopBridge, index_html_path):
    """Inject a floating Back + Open in Browser toolbar only when an external web page opens inside the app."""
    app_origin: list[tuple[str, str] | None] = [None]

    def _on_loaded():
        try:
            current_url = str(window.get_current_url() or "").strip()
        except Exception:
            return
        if not current_url:
            return

        parsed = urlparse(current_url)
        origin = (parsed.scheme.lower(), parsed.netloc.lower())

        # Capture the actual local pywebview server URL/origin (e.g. http://127.0.0.1:<port>) on first load
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
                bridge.attach_window(window, current_url)
                return

        if app_origin[0] is not None and origin == app_origin[0]:
            return

        if parsed.scheme.lower() in ("http", "https"):
            try:
                window.evaluate_js(_EXTERNAL_NAV_BAR_JS)
            except Exception as e:
                print(f"[External Nav Overlay Error] {e}")

    window.events.loaded += _on_loaded


def main():
    # Enforce single instance: if another window is already open, focus it and exit
    if not _ensure_single_instance():
        sys.exit(0)

    # Start SDK background pre-warming thread immediately
    threading.Thread(target=_prewarm_sdks, daemon=True, name="sdk-prewarm").start()

    # Initialize SQLite database
    init_db()

    # Instantiate JS API Bridge
    bridge = DesktopBridge()

    index_html = UI_DIR / "index.html"
    if not index_html.exists():
        print(f"Error: UI file not found at {index_html}")
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
    _attach_external_navigation_overlay(window, bridge, index_html)

    # Launch desktop application loop
    icon_path = UI_DIR / "assets" / "logo.ico"
    webview.start(debug=False, icon=str(icon_path) if icon_path.exists() else None)


if __name__ == "__main__":
    main()


