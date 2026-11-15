import os
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, os.path.abspath("src"))

import chatint.main as main_mod
from chatint.main import _ensure_single_instance, _release_single_instance


@unittest.skipUnless(sys.platform == "win32", "Windows single-instance mutex test requires win32")
class TestSingleInstanceMutex(unittest.TestCase):
    def tearDown(self):
        _release_single_instance()

    def test_mutex_prevents_second_instance(self):
        test_mutex = f"Local\\ChatInt_Test_Mutex_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        first = _ensure_single_instance(mutex_name=test_mutex)
        self.assertTrue(first, "First instance must acquire the isolated single-instance mutex")

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, 'src'); "
                    "from chatint.main import _ensure_single_instance; "
                    f"res = _ensure_single_instance(mutex_name={test_mutex!r}); "
                    "sys.exit(0 if res is False else 1)"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Second instance must return False (stdout={proc.stdout!r}, stderr={proc.stderr!r})",
        )

    def test_release_single_instance_allows_immediate_reacquisition(self):
        test_mutex = f"Local\\ChatInt_Test_Release_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        first = _ensure_single_instance(mutex_name=test_mutex)
        self.assertTrue(first, "Initial instance must acquire the mutex")
        self.assertIsNotNone(main_mod._MUTEX_HANDLE)

        _release_single_instance()
        self.assertIsNone(main_mod._MUTEX_HANDLE, "_release_single_instance must clear _MUTEX_HANDLE")

        second = _ensure_single_instance(mutex_name=test_mutex)
        self.assertTrue(second, "Subsequent instance must immediately acquire mutex after release without delay")
        self.assertIsNotNone(main_mod._MUTEX_HANDLE)


if __name__ == "__main__":
    unittest.main()
