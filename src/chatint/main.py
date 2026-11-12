import sys
import threading
from urllib.parse import urlparse
import webview
from chatint.bridge import DesktopBridge
from chatint.config import UI_DIR
from chatint.db import init_db


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
  shadow.innerHTML = `
    <style>
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
    </style>
    <div class="bar">
      <button type="button" class="back-btn" id="ci-back-btn" title="Back to Chat">&#8592; Back to Chat</button>
      <span class="domain" id="ci-domain"></span>
      <button type="button" id="ci-ext-btn" title="Open in Default Browser">Open in Browser &#8599;</button>
    </div>
  `;
  shadow.getElementById('ci-domain').textContent = domain;

  const callBridge = async (method, ...args) => {
    for (let i = 0; i < 30; i++) {
      if (window.pywebview && window.pywebview.api && typeof window.pywebview.api[method] === 'function') {
        return window.pywebview.api[method](...args);
      }
      await new Promise((r) => setTimeout(r, 35));
    }
    return null;
  };

  shadow.getElementById('ci-back-btn').onclick = () => callBridge('return_to_chat');
  shadow.getElementById('ci-ext-btn').onclick = () => callBridge('open_external_url', window.location.href);
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


