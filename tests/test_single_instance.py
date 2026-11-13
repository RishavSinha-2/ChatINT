import sys
import os
import subprocess

sys.path.insert(0, os.path.abspath("src"))

from chatint.main import _ensure_single_instance
import chatint.main as main_mod

print("Testing Single-Instance Mutex Enforcement...")

# In the current process:
res1 = _ensure_single_instance()
print(f"First instance check result: {res1} (Expected: True)")
assert res1 is True, "First instance must succeed"

# Now run a second Python process invoking _ensure_single_instance() via uv run
test_cmd = [
    "uv", "run", "python", "-c",
    "import sys; sys.path.insert(0, 'src'); from chatint.main import _ensure_single_instance; res = _ensure_single_instance(); print('Second process result:', res); sys.exit(0 if res is False else 1)"
]

proc = subprocess.run(test_cmd, capture_output=True, text=True)
print(f"Second process output: {proc.stdout.strip()}")
print(f"Second process return code: {proc.returncode} (Expected: 0)")
assert proc.returncode == 0, "Second instance must detect running mutex and return False"

print("\nSINGLE INSTANCE MUTEX TEST PASSED SUCCESSFULLY!")
