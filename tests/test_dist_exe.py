import ctypes
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path

DIST_DIR = Path("dist/v0.4.0")
DIST_EXE = DIST_DIR / "ChatInt.exe"
DIST_DB = DIST_DIR / "data/chatint.db"


@unittest.skipUnless(
    sys.platform == "win32" and DIST_EXE.exists() and DIST_DB.exists(),
    "Built distribution dist/v0.4.0/ChatInt.exe not present",
)
class TestDistExecutable(unittest.TestCase):
    def test_dist_db_schema_and_providers(self):
        with sqlite3.connect(DIST_DB) as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in c.fetchall()}
            for required in ("conversations", "messages", "providers", "token_ledger", "app_settings"):
                self.assertIn(required, tables)

            c.execute("SELECT COUNT(*) FROM providers")
            prov_count = c.fetchone()[0]
            self.assertGreaterEqual(prov_count, 3)

    def test_dist_exe_single_instance(self):
        user32 = ctypes.windll.user32
        subprocess.run(["taskkill", "/F", "/IM", "ChatInt.exe"], capture_output=True)
        time.sleep(0.5)

        proc1 = subprocess.Popen([str(DIST_EXE)], cwd=str(DIST_DIR))
        try:
            hwnd = None
            for _ in range(100):
                h = user32.FindWindowW(None, "ChatInt")
                if h and user32.IsWindowVisible(h):
                    hwnd = h
                    break
                time.sleep(0.05)

            self.assertIsNotNone(hwnd, "First executable instance must show a visible window")

            proc2 = subprocess.run([str(DIST_EXE)], cwd=str(DIST_DIR), capture_output=True, timeout=15)
            self.assertEqual(proc2.returncode, 0, "Second instance must exit cleanly with code 0")
        finally:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc1.pid)], capture_output=True)
            subprocess.run(["taskkill", "/F", "/IM", "ChatInt.exe"], capture_output=True)
            try:
                proc1.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.3)


if __name__ == "__main__":
    unittest.main()
