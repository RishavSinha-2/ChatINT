import os
import sys
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "chatint.db"

BASE_DIR = Path(__file__).resolve().parent
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
        "name": "Ollama (Local)",
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
        "is_enabled": 1,
        "models": [],
    },
]
