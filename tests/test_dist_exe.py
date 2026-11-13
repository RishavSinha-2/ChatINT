import sqlite3
import subprocess
import time
import ctypes
from pathlib import Path

# 1. Check DB initialization
db_path = Path("dist/v0.4.0/data/chatint.db")
assert db_path.exists(), "DB file must exist"

conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in c.fetchall()]
print("Tables in dist db:", tables)
assert "conversations" in tables
assert "messages" in tables
assert "providers" in tables
assert "token_ledger" in tables
assert "app_settings" in tables

c.execute("SELECT COUNT(*) FROM providers")
prov_count = c.fetchone()[0]
print("Providers count in dist db:", prov_count)
assert prov_count >= 3
conn.close()
print("[PASS] Dist DB successfully initialized with all tables and providers!")

# 2. Test Single-Instance Mutex Enforcement on the built Executable
user32 = ctypes.windll.user32
exe_path = r"dist\v0.4.0\ChatInt.exe"

# Make sure no previous instance is running
subprocess.run(["taskkill", "/F", "/IM", "ChatInt.exe"], capture_output=True)
time.sleep(1.0)

print("\nTesting Executable Single-Instance Behavior...")
proc1 = subprocess.Popen([exe_path], cwd=r"dist\v0.4.0")
hwnd = None
for _ in range(100):
    h = user32.FindWindowW(None, "ChatInt")
    if h and user32.IsWindowVisible(h):
        hwnd = h
        break
    time.sleep(0.05)

assert hwnd is not None, "First executable instance must show a visible window"
print(f"First executable instance running with HWND: {hwnd}")

# Now launch a second instance of the executable
proc2 = subprocess.run([exe_path], cwd=r"dist\v0.4.0", capture_output=True)
print(f"Second executable instance exit code: {proc2.returncode} (Expected: 0)")
assert proc2.returncode == 0, "Second instance must exit cleanly with code 0"

# Verify that only ONE main window exists
window_count = 0
def enum_windows_callback(h, lparam):
    global window_count
    length = user32.GetWindowTextLengthW(h)
    if length > 0:
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(h, buff, length + 1)
        if buff.value == "ChatInt" and user32.IsWindowVisible(h):
            window_count += 1
    return True

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
user32.EnumWindows(EnumWindowsProc(enum_windows_callback), 0)

print(f"Total visible 'ChatInt' windows open: {window_count} (Expected: 1)")
assert window_count == 1, "There must be exactly ONE visible ChatInt window"

# Clean up
proc1.terminate()
proc1.wait()
subprocess.run(["taskkill", "/F", "/IM", "ChatInt.exe"], capture_output=True)
print("[PASS] Single-Instance Executable Guard Test Passed!")
