# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Hantek Bench (Windows onedir)."""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

ROOT = Path(SPECPATH).resolve().parent
block_cipher = None

datas: list = []
binaries: list = []
hiddenimports: list = []

for pkg in (
    "uvicorn",
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic_core",
    "anyio",
    "numpy",
    "matplotlib",
    "mcp",
    "PyHT6022",
    "usb1",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

binaries += collect_dynamic_libs("usb1")

datas += [
    (str(ROOT / "hantek_navigator" / "static"), "hantek_navigator/static"),
]

hiddenimports += [
    "hantek_navigator",
    "hantek_navigator.webapp",
    "hantek_navigator.cli",
    "hantek_navigator.live_stream",
    "hantek_navigator.live_la",
    "hantek_navigator.sigrok_bridge",
    "hantek_navigator.mcp_server",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "multipart",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HantekBench",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="HantekBench",
)
