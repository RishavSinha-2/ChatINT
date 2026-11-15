from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Dict, Optional


def resolve_app_paths(
    exe_path: Optional[Path] = None,
    is_frozen: Optional[bool] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Deterministically resolve application paths supporting Installed, Portable, and Development modes.
    """
    env_map = os.environ if env is None else env
    frozen = getattr(sys, "frozen", False) if is_frozen is None else is_frozen

    if not frozen:
        dev_root = Path(__file__).resolve().parent.parent.parent
        data_dir = dev_root / "data"
        ui_dir = Path(__file__).resolve().parent / "ui"
        return {
            "mode": "development",
            "project_root": dev_root,
            "data_dir": data_dir,
            "db_path": data_dir / "chatint.db",
            "env_path": dev_root / ".env",
            "ui_dir": ui_dir,
            "base_dir": ui_dir.parent,
        }

    actual_exe = (Path(sys.executable) if exe_path is None else exe_path).resolve()
    exe_dir = actual_exe.parent
    local_data = exe_dir / "data"
    local_env = exe_dir / ".env"
    portable_marker = exe_dir / "portable"

    local_appdata = Path(env_map.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local").resolve()
    standard_install_dir = (local_appdata / "Programs" / "ChatInt").resolve()

    roaming_appdata = Path(env_map.get("APPDATA") or Path.home() / "AppData" / "Roaming").resolve()
    installed_root = (roaming_appdata / "ChatInt").resolve()

    bundle_root = Path(getattr(sys, "_MEIPASS", exe_dir))
    ui_dir = bundle_root / "chatint" / "ui"
    if not ui_dir.exists():
        ui_dir = bundle_root / "ui"

    # 1. Local data folder or portable marker takes immediate priority (Portable Mode)
    if local_data.is_dir() or portable_marker.exists():
        return {
            "mode": "portable",
            "project_root": exe_dir,
            "data_dir": local_data,
            "db_path": local_data / "chatint.db",
            "env_path": local_env,
            "ui_dir": ui_dir,
            "base_dir": ui_dir.parent,
        }

    # 2. Installed in %LOCALAPPDATA%\Programs\ChatInt -> use %APPDATA%\ChatInt (Installed Mode)
    try:
        is_in_install_dir = (
            str(exe_dir).lower() == str(standard_install_dir).lower()
            or str(exe_dir.resolve()).lower() == str(standard_install_dir.resolve()).lower()
        )
    except Exception:
        is_in_install_dir = False

    if is_in_install_dir:
        inst_data = installed_root / "data"
        return {
            "mode": "installed",
            "project_root": installed_root,
            "data_dir": inst_data,
            "db_path": inst_data / "chatint.db",
            "env_path": installed_root / ".env",
            "ui_dir": ui_dir,
            "base_dir": ui_dir.parent,
        }

    # 3. Fallback: custom directory without local data -> default to Portable Mode next to executable
    return {
        "mode": "portable",
        "project_root": exe_dir,
        "data_dir": local_data,
        "db_path": local_data / "chatint.db",
        "env_path": local_env,
        "ui_dir": ui_dir,
        "base_dir": ui_dir.parent,
    }


_RESOLVED_PATHS = resolve_app_paths()

APP_MODE: str = _RESOLVED_PATHS["mode"]
PROJECT_ROOT: Path = _RESOLVED_PATHS["project_root"]
DATA_DIR: Path = _RESOLVED_PATHS["data_dir"]
DB_PATH: Path = _RESOLVED_PATHS["db_path"]
ENV_PATH: Path = _RESOLVED_PATHS["env_path"]
UI_DIR: Path = _RESOLVED_PATHS["ui_dir"]
BASE_DIR: Path = _RESOLVED_PATHS["base_dir"]

DATA_DIR.mkdir(parents=True, exist_ok=True)

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
        "id": "anthropic",
        "name": "Anthropic",
        "base_url": "",
        "api_key": "",
        "is_enabled": 1,
        "models": [],
    },
    {
        "id": "gemini",
        "name": "Gemini",
        "base_url": "",
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
        "api_key": "ollama",
        "is_enabled": 1,
        "models": [],
    },
]
