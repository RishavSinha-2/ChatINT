# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

datas = [('src/chatint/ui', 'chatint/ui')]
binaries = []
hiddenimports = ['chatint.web_search_tool', 'ctypes', 'ctypes.wintypes', 'sqlite3']

for pkg in ('webview', 'anthropic', 'openai', 'google.genai', 'google.auth', 'httpx2', 'httpx'):
    try:
        tmp_ret = collect_all(pkg)
        # Filter datas: do not extract thousands of python source files, docs, or tests to disk.
        # Python modules belong in the internal pyz bytecode archive (via hiddenimports).
        clean_datas = [
            item for item in tmp_ret[0]
            if not item[0].lower().endswith(('.py', '.pyc', '.pyi', '.md', '.txt', '.rst', '.keep', 'license', 'notice'))
            and 'test' not in item[0].lower()
        ]
        datas += clean_datas
        binaries += tmp_ret[1]
        hiddenimports += tmp_ret[2]
    except Exception:
        hiddenimports += collect_submodules(pkg)

for dist_name in ('anthropic', 'google-genai', 'google-auth', 'openai', 'httpx', 'httpx2', 'pydantic', 'certifi'):
    try:
        datas += copy_metadata(dist_name)
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
    excludes=['pytest', 'unittest', 'test', 'tkinter'],
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
    icon=['logo.ico'],
)

