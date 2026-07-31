# -*- mode: python ; coding: utf-8 -*-
"""Сборка HH Agent в .app.

Осознанные решения:

1. Браузер (Camoufox, ~700 МБ) НЕ кладём внутрь приложения. С PyInstaller у
   таких бинарников ломаются пути к исполняемому файлу, да и размер
   приложения вырос бы в разы. Вместо этого приложение при первом запуске
   проверяет наличие браузера и предлагает его установить —
   см. first_run.install_camoufox().

2. Драйвер Playwright (node-обёртка, ~128 МБ) нужен обязательно, иначе
   playwright не запустится вообще — Camoufox запускается через тот же
   Playwright API, просто с другим бинарником браузера.

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
# Camoufox (единственный поддерживаемый браузер — см. hh_session.py) и его
# зависимости по генерации отпечатков тащат JSON/JS/шрифты/SQLite — без них
# даже голый `import camoufox` не падает, но подмена отпечатка отваливается
# в рантайме без внятной ошибки.
datas += collect_data_files("camoufox", include_py_files=True)
datas += collect_data_files("browserforge", include_py_files=True)
datas += collect_data_files("apify_fingerprint_datapoints", include_py_files=True)
# Тянется camoufox'ом транзитивно (через browserforge) — без JSON-словарей
# в data/json падает уже на "import camoufox", не только в рантайме.
datas += collect_data_files("language_tags", include_py_files=True)

hiddenimports = [
    "webview.platforms.cocoa",
    "pystray._darwin",
    "desktop_notifier.backends.macos",
    "keyring.backends.macOS",
    "aiohttp",
    "aiogram",
]
hiddenimports += collect_submodules("playwright")
hiddenimports += collect_submodules("camoufox")

a = Analysis(
    ["ui_app.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # numpy убран из исключений: его тянет camoufox прямо на "import camoufox",
    # без него голый импорт модуля падает в собранном .app.
    excludes=["tkinter", "matplotlib", "pytest", "PyInstaller"],
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
