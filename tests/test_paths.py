import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, os.path.abspath("src"))

from chatint.config import resolve_app_paths


class TestPathResolution(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

        self.fake_localappdata = self.root / "AppData" / "Local"
        self.fake_appdata = self.root / "AppData" / "Roaming"
        self.fake_localappdata.mkdir(parents=True, exist_ok=True)
        self.fake_appdata.mkdir(parents=True, exist_ok=True)

        self.env = {
            "LOCALAPPDATA": str(self.fake_localappdata),
            "APPDATA": str(self.fake_appdata),
        }

    def test_development_mode(self):
        paths = resolve_app_paths(is_frozen=False, env=self.env)
        self.assertEqual(paths["mode"], "development")
        self.assertTrue(paths["data_dir"].name == "data")
        self.assertEqual(paths["db_path"], paths["data_dir"] / "chatint.db")
        self.assertEqual(paths["env_path"], paths["project_root"] / ".env")

    def test_installed_mode(self):
        install_dir = self.fake_localappdata / "Programs" / "ChatInt"
        install_dir.mkdir(parents=True, exist_ok=True)
        exe_path = install_dir / "ChatInt.exe"
        exe_path.touch()

        paths = resolve_app_paths(exe_path=exe_path, is_frozen=True, env=self.env)
        self.assertEqual(paths["mode"], "installed")

        expected_root = self.fake_appdata / "ChatInt"
        self.assertEqual(paths["project_root"].resolve(), expected_root.resolve())
        self.assertEqual(paths["data_dir"].resolve(), (expected_root / "data").resolve())
        self.assertEqual(paths["db_path"].resolve(), (expected_root / "data" / "chatint.db").resolve())
        self.assertEqual(paths["env_path"].resolve(), (expected_root / ".env").resolve())

    def test_installed_mode_case_insensitive(self):
        install_dir = self.fake_localappdata / "programs" / "chatint"
        install_dir.mkdir(parents=True, exist_ok=True)
        exe_path = install_dir / "chatint.exe"
        exe_path.touch()

        paths = resolve_app_paths(exe_path=exe_path, is_frozen=True, env=self.env)
        self.assertEqual(paths["mode"], "installed")
        self.assertEqual(paths["project_root"].resolve(), (self.fake_appdata / "ChatInt").resolve())

    def test_portable_mode_with_existing_data_dir(self):
        custom_dir = self.root / "MyTools" / "ChatInt"
        data_dir = custom_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        exe_path = custom_dir / "ChatInt.exe"
        exe_path.touch()

        paths = resolve_app_paths(exe_path=exe_path, is_frozen=True, env=self.env)
        self.assertEqual(paths["mode"], "portable")
        self.assertEqual(paths["project_root"].resolve(), custom_dir.resolve())
        self.assertEqual(paths["data_dir"].resolve(), data_dir.resolve())
        self.assertEqual(paths["db_path"].resolve(), (data_dir / "chatint.db").resolve())
        self.assertEqual(paths["env_path"].resolve(), (custom_dir / ".env").resolve())

    def test_portable_mode_with_marker_file(self):
        custom_dir = self.root / "MarkerPortable"
        custom_dir.mkdir(parents=True, exist_ok=True)
        (custom_dir / "portable").touch()
        exe_path = custom_dir / "ChatInt.exe"
        exe_path.touch()

        paths = resolve_app_paths(exe_path=exe_path, is_frozen=True, env=self.env)
        self.assertEqual(paths["mode"], "portable")
        self.assertEqual(paths["project_root"].resolve(), custom_dir.resolve())
        self.assertEqual(paths["data_dir"].resolve(), (custom_dir / "data").resolve())

    def test_local_data_dir_overrides_installed_location(self):
        install_dir = self.fake_localappdata / "Programs" / "ChatInt"
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / "data").mkdir(parents=True, exist_ok=True)
        exe_path = install_dir / "ChatInt.exe"
        exe_path.touch()

        paths = resolve_app_paths(exe_path=exe_path, is_frozen=True, env=self.env)
        # Having local 'data' folder forces portable behavior even inside Programs
        self.assertEqual(paths["mode"], "portable")
        self.assertEqual(paths["project_root"].resolve(), install_dir.resolve())

    def test_fallback_portable_mode(self):
        # Fresh folder, no data directory exists yet
        downloads_dir = self.root / "Downloads" / "chatint_extracted"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        exe_path = downloads_dir / "ChatInt.exe"
        exe_path.touch()

        paths = resolve_app_paths(exe_path=exe_path, is_frozen=True, env=self.env)
        self.assertEqual(paths["mode"], "portable")
        self.assertEqual(paths["project_root"].resolve(), downloads_dir.resolve())
        self.assertEqual(paths["data_dir"].resolve(), (downloads_dir / "data").resolve())
        self.assertEqual(paths["db_path"].resolve(), (downloads_dir / "data" / "chatint.db").resolve())


if __name__ == "__main__":
    unittest.main()
