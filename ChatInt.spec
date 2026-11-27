# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path
import re
import sys
import tomllib
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata


def _load_project_metadata() -> tuple[str, tuple[int, int, int, int], str, str]:
    spec_dir = Path(globals().get("SPECPATH", ".")).resolve()
    pyproject_path = spec_dir / "pyproject.toml"
    project = {}
    if pyproject_path.exists():
        with pyproject_path.open("rb") as fh:
            project = tomllib.load(fh).get("project", {})

    raw_version = os.environ.get("CHATINT_VERSION", "").strip() or str(project.get("version", "0.5.2")).strip()
    clean_version = raw_version.lstrip("vV")
    nums = [int(x) for x in re.findall(r"\d+", clean_version)[:4]]
    while len(nums) < 4:
        nums.append(0)
    ver_tuple = (nums[0], nums[1], nums[2], nums[3])

    description = str(
        project.get("description") or "ChatInt App"
    )
    authors = project.get("authors") or []
    author_name = (
        str(authors[0].get("name", "Rishav Sinha"))
        if authors and isinstance(authors[0], dict)
        else "Rishav Sinha"
    )
    return clean_version, ver_tuple, description, author_name


def _build_windows_version_info():
    if sys.platform != "win32":
        return None
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    ver_str, ver_tuple, description, author_name = _load_project_metadata()
    file_ver_str = ".".join(str(n) for n in ver_tuple)
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=ver_tuple,
            prodvers=ver_tuple,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", author_name),
                            StringStruct("FileDescription", description),
                            StringStruct("FileVersion", file_ver_str),
                            StringStruct("InternalName", "ChatInt"),
                            StringStruct("LegalCopyright", f"Copyright (c) 2026 {author_name}"),
                            StringStruct("OriginalFilename", "ChatInt.exe"),
                            StringStruct("ProductName", "ChatInt"),
                            StringStruct("ProductVersion", ver_str),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )


datas = [('src/chatint/ui', 'chatint/ui')]

spec_dir = Path(globals().get("SPECPATH", ".")).resolve()
default_db_file = spec_dir / "build" / "default_data" / "chatint.db"
if not default_db_file.is_file():
    default_db_file = spec_dir / "data" / "chatint.db"
if default_db_file.is_file():
    datas.append((str(default_db_file), 'chatint/default_data'))

binaries = []
hiddenimports = [
    'chatint.catalog',
    'chatint.web_search_tool',
    'chatint.storage',
    'chatint.storage.connection',
    'chatint.storage.conversations_repo',
    'chatint.storage.dpapi',
    'chatint.storage.ledger_repo',
    'chatint.storage.providers_repo',
    'chatint.storage.secrets',
    'chatint.storage.settings_repo',
    'chatint.providers',
    'chatint.providers.base',
    'chatint.providers.openai_adapter',
    'chatint.providers.anthropic_adapter',
    'chatint.providers.gemini_adapter',
    'chatint.services',
    'chatint.services.import_export',
    'chatint.services.stream_coordinator',
    'chatint.tools',
    'chatint.tools.registry',
    'chatint.tools.web_tools',
    'chatint.tools.workspace_tools',
    'ctypes',
    'ctypes.wintypes',
    'ipaddress',
    'sqlite3',
]

for pkg in ('webview', 'anthropic', 'openai', 'google.genai', 'google.auth', 'httpx2', 'httpx', 'certifi', 'ddgs', 'primp', 'lxml'):
    try:
        tmp_ret = collect_all(pkg)
        # Filter datas using the bundle destination path (item[1]) rather than the host source path (item[0])
        # so directory names on the build machine (e.g. "latest", "testing") never drop required package assets.
        clean_datas = [
            item for item in tmp_ret[0]
            if not item[0].lower().endswith(('.py', '.pyc', '.pyi', '.md', '.txt', '.rst', '.keep', 'license', 'notice', 'record', 'installer', 'direct_url.json'))
            and not any(part in ('test', 'tests', '__pycache__') for part in item[1].replace('\\', '/').lower().split('/'))
        ]
        datas += clean_datas
        binaries += [
            b for b in tmp_ret[1]
            if not any(arch in b[0].replace('\\', '/').lower() for arch in ('/win-arm64/', '/win-x86/'))
        ]
        clean_hidden = [
            m for m in tmp_ret[2]
            if not any(m.startswith(prefix) for prefix in (
                'openai.cli',
                'openai.resources.audio',
                'openai.resources.fine_tuning',
                'openai.resources.evals',
                'openai.resources.batches',
                'openai.resources.containers',
                'openai.resources.uploads',
                'openai.resources.vector_stores',
                'anthropic.lib.tools._toolsets._browser',
                'anthropic.lib.tools._toolsets._computer',
            ))
        ]
        hiddenimports += clean_hidden
    except Exception:
        hiddenimports += collect_submodules(pkg)

for dist_name in ('anthropic', 'google-genai', 'google-auth', 'openai', 'httpx', 'httpx2', 'pydantic', 'certifi', 'ddgs', 'primp', 'lxml'):
    try:
        datas += [
            item for item in copy_metadata(dist_name)
            if not item[0].lower().endswith(('record', 'installer', 'direct_url.json'))
        ]
    except Exception:
        pass


a = Analysis(
    ['src/chatint/main.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pytest', 'unittest', 'test', 'tests', 'tkinter', 'tcl', 'mypy', 'ast_serialize', 'librt',
        'pydoc', 'pydoc_data', 'doctest', 'argcomplete',
        'profile', 'cProfile', 'pdb', 'timeit', 'trace',
        'pefile', 'PyInstaller',
        'sqlite3.test', 'ctypes.test', 'email.test',
        'smtplib', 'poplib', 'imaplib', 'ftplib', 'nntplib', 'telnetlib', 'xmlrpc',
        'wave', 'sndhdr', 'sunau', 'aifc',
        'turtle', 'turtledemo', 'idlelib',
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ChatInt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['src/chatint/ui/assets/logo.ico'],
    version=_build_windows_version_info(),
)
