import os
import sys
from pathlib import Path

# Paths (supports both normal Python execution and frozen PyInstaller --onefile executable)
if getattr(sys, "frozen", False):
    # Persistent user data (data/chatint.db and .env) lives right next to ChatInt.exe
    PROJECT_ROOT = Path(sys.executable).resolve().parent
    # Bundled read-only UI assets live inside PyInstaller's _MEIPASS extraction dir
    _BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    BASE_DIR = _BUNDLE_ROOT / "chatint"
    if not (BASE_DIR / "ui").exists() and (_BUNDLE_ROOT / "ui").exists():
        BASE_DIR = _BUNDLE_ROOT
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "chatint.db"
ENV_PATH = PROJECT_ROOT / ".env"

UI_DIR = BASE_DIR / "ui"

DEFAULT_PROVIDERS = [
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "is_enabled": 1,
        "models": [],
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "",
        "is_enabled": 1,
        "models": [],
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "is_enabled": 1,
        "models": [],
    },
    {
        "id": "ollama",
        "name": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
        "is_enabled": 1,
        "models": [],
    },
]
