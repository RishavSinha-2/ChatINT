import contextlib
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import tomllib
import unittest


def _resolve_dist_exe() -> Path:
    override = os.environ.get("CHATINT_EXE_PATH", "").strip()
    if override:
        return Path(override)
    default_candidate = Path("dist/v0.5.0/ChatInt.exe")
    if default_candidate.exists():
        return default_candidate
    dist_root = Path("dist")
    if dist_root.exists():
        matches = sorted(dist_root.glob("**/ChatInt.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
    return default_candidate


DIST_EXE = _resolve_dist_exe()
RUN_EXE_SMOKE = os.environ.get("CHATINT_RUN_EXE_SMOKE", "").strip().lower() in ("1", "true", "yes") or bool(
    os.environ.get("CHATINT_EXE_PATH", "").strip()
)


class TestDistVersionInfo(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "win32" and Path("dist/v0.5.0/ChatInt.exe").exists(),
        "Requires built dist/v0.5.0/ChatInt.exe on Windows",
    )
    def test_v050_exe_embeds_windows_version_info(self):
        from PyInstaller.utils.win32 import versioninfo

        exe_path = Path("dist/v0.5.0/ChatInt.exe")
        vi = versioninfo.read_version_info_from_executable(str(exe_path))
        self.assertIsNotNone(vi, "ChatInt.exe must embed a VSVersionInfo RT_VERSION resource")

        with Path("pyproject.toml").open("rb") as fh:
            expected_version = tomllib.load(fh)["project"]["version"]

        rendered = str(vi)
        self.assertIn("StringStruct('ProductName', 'ChatInt')", rendered)
        self.assertIn(f"StringStruct('ProductVersion', '{expected_version}')", rendered)
        self.assertIn("StringStruct('OriginalFilename', 'ChatInt.exe')", rendered)


@unittest.skipUnless(
    sys.platform == "win32" and RUN_EXE_SMOKE and DIST_EXE.exists(),
    "Set CHATINT_RUN_EXE_SMOKE=1 (or CHATINT_EXE_PATH) on Windows with built ChatInt.exe to run GUI exe smoke tests",
)
class TestDistExecutable(unittest.TestCase):
    def test_dist_exe_launches_initializes_db_and_enforces_single_instance(self):
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsHungAppWindow.argtypes = [wintypes.HWND]
        user32.IsHungAppWindow.restype = wintypes.BOOL

        existing = user32.FindWindowW(None, "ChatInt")
        if existing and user32.IsWindowVisible(existing):
            self.skipTest("A live ChatInt window is already open; skipping exe smoke test to avoid colliding with user session.")

        dist_dir = DIST_EXE.parent
        dist_db = dist_dir / "data" / "chatint.db"

        proc1 = subprocess.Popen([str(DIST_EXE)], cwd=str(dist_dir))
        try:
            hwnd = None
            for _ in range(120):
                h = user32.FindWindowW(None, "ChatInt")
                if h and user32.IsWindowVisible(h):
                    hwnd = h
                    break
                time.sleep(0.05)

            self.assertIsNotNone(hwnd, "First executable instance must show a visible window")
            # Allow WebView2 NavigationCompleted + inject_pywebview + ES module bootstrap to complete,
            # then verify the WinForms STA UI thread is responsive (not hung).
            time.sleep(1.5)
            self.assertIsNone(proc1.poll(), "Executable must stay alive after WebView2 page load")
            self.assertFalse(bool(user32.IsHungAppWindow(hwnd)), "ChatInt window must remain responsive after WebView2 load")
            self.assertTrue(dist_db.exists(), "Launching ChatInt.exe must initialize data/chatint.db even on a fresh build")

            with contextlib.closing(sqlite3.connect(dist_db)) as conn:
                c = conn.cursor()
                c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {r[0] for r in c.fetchall()}
                for required in ("conversations", "messages", "providers", "token_ledger", "app_settings"):
                    self.assertIn(required, tables)

                c.execute("SELECT COUNT(*) FROM providers")
                prov_count = c.fetchone()[0]
                self.assertGreaterEqual(prov_count, 3)

            proc2 = subprocess.run([str(DIST_EXE)], cwd=str(dist_dir), capture_output=True, timeout=15)
            self.assertEqual(proc2.returncode, 0, "Second instance must exit cleanly with code 0")
        finally:
            # Terminate ONLY the process tree we spawned (never `taskkill /IM ChatInt.exe`).
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc1.pid)], capture_output=True)
            try:
                proc1.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc1.kill()
            time.sleep(0.3)


if __name__ == "__main__":
    unittest.main()
