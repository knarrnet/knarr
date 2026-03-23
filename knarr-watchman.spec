# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for knarr-watchman single-file binary."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# SPECPATH is set by PyInstaller; __file__ is not available in spec context
project_root = Path(SPECPATH)

datas = collect_data_files("knarr")
hiddenimports = collect_submodules("nacl") + collect_submodules("cryptography")

a = Analysis(
    ["watchman_entry.py"],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="knarr-watchman",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
