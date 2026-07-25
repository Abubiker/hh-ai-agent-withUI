# -*- mode: python ; coding: utf-8 -*-
"""Сборка HH Agent в .app.

Осознанные решения:

1. Браузер Chromium НЕ кладём внутрь приложения. Playwright плохо дружит с
   PyInstaller (пути к браузеру ломаются), да и это +150 МБ. Вместо этого
   приложение при первом запуске проверяет наличие браузера и предлагает его
   установить — см. ensure_browser() в first_run.py.

2. Драйвер Playwright (node-обёртка, ~128 МБ) нужен обязательно, иначе
   playwright не запустится вообще.

3. После сборки .app ОБЯЗАТЕЛЬНО подписать (хотя бы ad-hoc): macOS не
   показывает уведомления от неподписанных программ. Этим занимается
   build_app.sh.
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(os.getcwd())

datas = [
    (str(ROOT / "ui"), "ui"),
]
# Драйвер Playwright целиком — без него не поднимется браузер
datas += collect_data_files("playwright", include_py_files=True)
# Звук и иконка уведомлений: без них desktop_notifier падает с
# "No module named 'desktop_notifier.resources'"
datas += collect_data_files("desktop_notifier", include_py_files=True)
# playwright_stealth тащит за собой JS-скрипты; без них падает даже импорт,
# а вместе с ним и весь модуль агента
datas += collect_data_files("playwright_stealth", include_py_files=True)

hiddenimports = [
    "playwright_stealth",
    "webview.platforms.cocoa",
    "pystray._darwin",
    "desktop_notifier.backends.macos",
    "keyring.backends.macOS",
    "aiohttp",
    "aiogram",
]
hiddenimports += collect_submodules("playwright")

a = Analysis(
    ["ui_app.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Тянуть за собой не нужно: это тяжёлые зависимости, которых нет в коде
    excludes=["tkinter", "matplotlib", "numpy", "pytest", "PyInstaller"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HHAgent",
    debug=False,
    strip=False,
    upx=False,          # upx ломает подпись на macOS
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HHAgent",
)

app = BUNDLE(
    coll,
    name="HH Agent.app",
    icon=str(ROOT / "build_assets" / "icon.icns"),
    bundle_identifier="com.dmitrobuber.hhagent",
    info_plist={
        "CFBundleName": "HH Agent",
        "CFBundleDisplayName": "HH Agent",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        # Приложение долго работает в фоне и шлёт уведомления
        "LSMinimumSystemVersion": "11.0",
        "NSSupportsAutomaticTermination": False,
        "NSSupportsSuddenTermination": False,
    },
)
