import sys
from pathlib import Path
import webview
from chatint.bridge import DesktopBridge
from chatint.config import UI_DIR
from chatint.db import init_db


def main():
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

    # Launch desktop application loop
    icon_path = UI_DIR / "assets" / "logo.ico"
    webview.start(debug=False, icon=str(icon_path) if icon_path.exists() else None)


if __name__ == "__main__":
    main()

