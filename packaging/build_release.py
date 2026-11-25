"""
Build script for ChatInt dual-publishing:
1. Builds standalone ChatInt.exe with PyInstaller into dist/v<version>/
2. Creates portable distribution zip: dist/v<version>/ChatInt-Portable-v<version>.zip
3. Checks for Inno Setup compiler (ISCC) to build installer: dist/installer/ChatInt-Setup-x64.exe
4. Outputs SHA-256 hashes for GitHub Releases and WinGet manifest submission.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_version() -> str:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return str(data.get("project", {}).get("version", "0.5.2"))


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def find_iscc() -> Path | None:
    # 1. Check PATH
    cmd = shutil.which("iscc") or shutil.which("ISCC")
    if cmd:
        return Path(cmd)
    # 2. Check standard installation directories on Windows
    common_paths = [
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    for p in common_paths:
        if p.is_file():
            return p
    return None


def prepare_clean_default_db() -> Path | None:
    """Create a clean baseline chatint.db preserving curated providers/models/pricing and settings while scrubbing personal/runtime state."""
    import contextlib
    import sqlite3

    clean_dir = PROJECT_ROOT / "build" / "default_data"
    clean_dir.mkdir(parents=True, exist_ok=True)
    clean_db = clean_dir / "chatint.db"
    temp_env = clean_dir / ".env.tmp"
    for candidate in (clean_db, temp_env):
        if candidate.exists():
            candidate.unlink()

    source_db = PROJECT_ROOT / "data" / "chatint.db"
    if source_db.is_file():
        with contextlib.closing(sqlite3.connect(source_db)) as src_conn, contextlib.closing(sqlite3.connect(clean_db)) as dst_conn:
            src_conn.backup(dst_conn)
        with contextlib.closing(sqlite3.connect(clean_db)) as conn:
            for tbl in ("messages", "conversations", "token_ledger", "tab_sessions", "google_search_usage"):
                conn.execute(f"DELETE FROM {tbl}")
            conn.execute("UPDATE providers SET api_key = ''")
            conn.commit()
            conn.execute("VACUUM")
    else:
        src_dir = PROJECT_ROOT / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))

        import chatint.config as cfg
        import chatint.db as db_mod
        import chatint.storage.connection as conn_mod
        import chatint.storage.secrets as sec_mod

        orig_attrs = (
            cfg.DB_PATH,
            cfg.ENV_PATH,
            db_mod.DB_PATH,
            db_mod.ENV_PATH,
            conn_mod._DEFAULT_DB_PATH,
            sec_mod._DEFAULT_ENV_PATH,
        )
        try:
            cfg.DB_PATH = db_mod.DB_PATH = conn_mod._DEFAULT_DB_PATH = clean_db
            cfg.ENV_PATH = db_mod.ENV_PATH = sec_mod._DEFAULT_ENV_PATH = temp_env
            db_mod.init_db()
        finally:
            (
                cfg.DB_PATH,
                cfg.ENV_PATH,
                db_mod.DB_PATH,
                db_mod.ENV_PATH,
                conn_mod._DEFAULT_DB_PATH,
                sec_mod._DEFAULT_ENV_PATH,
            ) = orig_attrs
            if temp_env.exists():
                temp_env.unlink()

    print(f" -> Prepared clean baseline template database: {clean_db} ({clean_db.stat().st_size / 1024:.1f} KB)")
    return clean_db


def build_pyinstaller(version: str) -> Path:
    dist_dir = PROJECT_ROOT / "dist" / f"v{version}"
    build_dir = PROJECT_ROOT / "build" / f"v{version}"
    dist_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[1/4] Preparing template data & building ChatInt v{version} via PyInstaller...")
    prepare_clean_default_db()

    cmd = [
        "uv",
        "run",
        "pyinstaller",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "ChatInt.spec",
        "-y",
    ]
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)

    exe_path = dist_dir / "ChatInt.exe"
    if not exe_path.is_file():
        raise FileNotFoundError(f"Expected executable not found at {exe_path}")
    print(f" -> Standalone executable built: {exe_path} ({exe_path.stat().st_size / (1024*1024):.2f} MB)")
    return exe_path


def build_portable_zip(version: str, exe_path: Path) -> Path:
    print(f"\n[2/4] Packaging portable ZIP archive...")
    dist_dir = exe_path.parent
    zip_path = dist_dir / f"ChatInt-Portable-v{version}.zip"

    clean_db = PROJECT_ROOT / "build" / "default_data" / "chatint.db"
    raw_db = PROJECT_ROOT / "data" / "chatint.db"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # ChatInt.exe
        zf.write(exe_path, arcname="ChatInt.exe")
        # Baseline data directory with default database
        if clean_db.is_file():
            zf.write(clean_db, arcname="data/chatint.db")
        elif raw_db.is_file():
            zf.write(raw_db, arcname="data/chatint.db")
        else:
            zf.writestr("data/.gitkeep", "")
        # Default .env template
        default_env = (
            "# ChatInt Portable Configuration\n"
            "# Keys entered in UI are DPAPI-encrypted automatically.\n"
            "OPENAI_API_KEY=\n"
            "ANTHROPIC_API_KEY=\n"
            "GEMINI_API_KEY=\n"
            "OLLAMA_API_KEY=ollama\n"
        )
        zf.writestr(".env", default_env)
        # Include license & readme if present
        for doc in ("LICENSE", "README.md"):
            doc_path = PROJECT_ROOT / doc
            if doc_path.is_file():
                zf.write(doc_path, arcname=doc)

    print(f" -> Portable ZIP package created: {zip_path} ({zip_path.stat().st_size / (1024*1024):.2f} MB)")
    return zip_path


def build_installer() -> Path | None:
    print(f"\n[3/4] Checking Inno Setup compiler for WinGet installer...")
    iscc_path = find_iscc()
    if not iscc_path:
        print(" [NOTE] Inno Setup compiler (ISCC.exe) not found on host machine.")
        print("        To compile the installer locally, install Inno Setup 6:")
        print("          winget install JRSoftware.InnoSetup")
        print("        Or compile in CI using the authored packaging/ChatInt.iss script.")
        return None

    iss_file = PROJECT_ROOT / "packaging" / "ChatInt.iss"
    installer_out = PROJECT_ROOT / "dist" / "installer" / "ChatInt-Setup-x64.exe"
    print(f" -> Compiling installer with: {iscc_path} {iss_file}")
    subprocess.run([str(iscc_path), str(iss_file)], cwd=PROJECT_ROOT / "packaging", check=True)

    if installer_out.is_file():
        print(f" -> Installer built successfully: {installer_out} ({installer_out.stat().st_size / (1024*1024):.2f} MB)")
        return installer_out
    return None


def main() -> None:
    version = get_version()
    print(f"=== ChatInt Release Packaging Workflow (v{version}) ===")

    exe_path = build_pyinstaller(version)
    zip_path = build_portable_zip(version, exe_path)
    installer_path = build_installer()

    print("\n[4/4] Release Artifacts & SHA-256 Checksums:")
    print("-" * 80)
    artifacts = [("Portable Zip", zip_path), ("Standalone Executable", exe_path)]
    if installer_path and installer_path.is_file():
        artifacts.insert(0, ("WinGet Installer", installer_path))

    for label, file_path in artifacts:
        sha = compute_sha256(file_path)
        size_mb = file_path.stat().st_size / (1024 * 1024)
        print(f"{label:22} : {file_path.name} ({size_mb:.2f} MB)")
        print(f"  SHA-256              : {sha}")
    print("-" * 80)


if __name__ == "__main__":
    main()
