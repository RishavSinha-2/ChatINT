import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, os.path.abspath("src"))

import chatint.storage.connection as conn_mod
from chatint.storage.connection import (
    _handle_corrupted_database,
    get_connection,
    init_db,
    open_db,
)


class TestDatabaseRecovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_dir = Path(self.temp_dir.name)
        self.db_path = self.db_dir / "chatint.db"

        # Patch _DEFAULT_DB_PATH in conn_mod
        self.orig_db_path = conn_mod._DEFAULT_DB_PATH
        conn_mod._DEFAULT_DB_PATH = self.db_path

        # If chatint.db module is imported and has DB_PATH, patch it too
        self.db_mod = sys.modules.get("chatint.db")
        if self.db_mod and hasattr(self.db_mod, "DB_PATH"):
            self.orig_mod_db_path = self.db_mod.DB_PATH
            self.db_mod.DB_PATH = self.db_path
        else:
            self.orig_mod_db_path = None

    def tearDown(self):
        conn_mod._DEFAULT_DB_PATH = self.orig_db_path
        if self.db_mod and self.orig_mod_db_path is not None:
            self.db_mod.DB_PATH = self.orig_mod_db_path

    def test_corrupted_db_without_backup(self):
        # 1. Write junk bytes to simulate corrupted database
        self.db_path.write_bytes(b"CORRUPTED_SQLITE_DATA_HEADER\x00\xff\xee\xdd" * 100)

        # 2. Call init_db - must not crash
        init_db()

        # 3. Verify corrupt file was quarantined
        corrupt_files = list(self.db_dir.glob("chatint.db.corrupted.*"))
        self.assertGreaterEqual(len(corrupt_files), 1)
        self.assertTrue(corrupt_files[0].read_bytes().startswith(b"CORRUPTED_SQLITE_DATA_HEADER"))

        # 4. Verify new db is functional and has core tables
        with open_db() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("conversations", tables)
            self.assertIn("providers", tables)
            self.assertIn("app_settings", tables)

    def test_corrupted_db_with_intact_backup(self):
        # 1. Initialize a valid DB first and insert a test marker
        init_db()
        with open_db() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('test-c1', 'Restored Chat', 1000, 1000)"
            )
            conn.commit()

        # 2. Create pre-migration backup safely closing all connections
        bak_path = self.db_path.with_name(f"{self.db_path.name}.bak")
        conn = get_connection()
        try:
            import sqlite3
            bconn = sqlite3.connect(bak_path)
            try:
                conn.backup(bconn)
            finally:
                bconn.close()
        finally:
            conn.close()

        # 3. Corrupt primary database
        self.db_path.write_bytes(b"CORRUPTED_AFTER_BACKUP\x00\x00\xff\xff" * 50)

        # 4. Call init_db
        init_db()

        # 5. Verify corrupt file was quarantined
        corrupt_files = list(self.db_dir.glob("chatint.db.corrupted.*"))
        self.assertGreaterEqual(len(corrupt_files), 1)

        # 6. Verify data was successfully restored from backup
        with open_db() as conn:
            row = conn.execute("SELECT title FROM conversations WHERE id = 'test-c1'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["title"], "Restored Chat")

    def test_corrupted_db_with_corrupted_backup(self):
        # 1. Corrupt both primary db and backup file
        self.db_path.write_bytes(b"CORRUPT_PRIMARY" * 50)
        bak_path = self.db_path.with_name(f"{self.db_path.name}.bak")
        bak_path.write_bytes(b"CORRUPT_BACKUP" * 50)

        # 2. Call init_db - must handle gracefully by creating fresh db
        init_db()

        # 3. Verify primary db was quarantined and a clean new db created
        corrupt_files = list(self.db_dir.glob("chatint.db.corrupted.*"))
        self.assertGreaterEqual(len(corrupt_files), 1)

        with open_db() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("conversations", tables)
            self.assertIn("providers", tables)

    def test_handle_corrupted_quarantines_wal_and_shm(self):
        self.db_path.write_bytes(b"CORRUPTED_DB" * 50)
        wal_path = self.db_path.with_name(f"{self.db_path.name}-wal")
        shm_path = self.db_path.with_name(f"{self.db_path.name}-shm")
        wal_path.write_bytes(b"DUMMY_WAL" * 10)
        shm_path.write_bytes(b"DUMMY_SHM" * 10)

        # Direct test of _handle_corrupted_database
        _handle_corrupted_database(self.db_path, RuntimeError("Simulated crash"))

        corrupt_wal = list(self.db_dir.glob("chatint.db.corrupted.*-wal"))
        corrupt_shm = list(self.db_dir.glob("chatint.db.corrupted.*-shm"))
        self.assertEqual(len(corrupt_wal), 1)
        self.assertEqual(len(corrupt_shm), 1)
        self.assertEqual(corrupt_wal[0].read_bytes(), b"DUMMY_WAL" * 10)
        self.assertEqual(corrupt_shm[0].read_bytes(), b"DUMMY_SHM" * 10)

    def test_seed_bundled_default_db(self):
        from unittest.mock import patch

        template_db = self.db_dir / "template.db"
        init_db()  # init clean db
        with open_db() as conn:
            conn.execute(
                "INSERT INTO providers (id, name, base_url, api_key, is_enabled, models_json) VALUES ('custom-p', 'CustomProvider', '', '', 1, '[]')"
            )
            conn.commit()

        import shutil
        shutil.copy2(self.db_path, template_db)
        self.db_path.unlink()  # remove target db

        # Mock sys._MEIPASS and sys.frozen
        meipass_dir = self.db_dir / "meipass"
        bundled_dir = meipass_dir / "chatint" / "default_data"
        bundled_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template_db, bundled_dir / "chatint.db")

        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(meipass_dir), create=True):
            init_db()

        self.assertTrue(self.db_path.exists())
        with open_db() as conn:
            row = conn.execute("SELECT name FROM providers WHERE id = 'custom-p'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["name"], "CustomProvider")


if __name__ == "__main__":
    unittest.main()
