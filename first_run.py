"""Проверки и установка того, что нельзя положить внутрь приложения.

Chromium для Playwright (~150 МБ) намеренно не входит в .app: с PyInstaller
у него ломаются пути к исполняемому файлу, да и размер приложения вырос бы
втрое. Вместо этого проверяем наличие браузера и ставим его по кнопке.
"""
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path


def browsers_dir() -> Path:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def browser_installed() -> bool:
    d = browsers_dir()
    return d.exists() and any(d.glob("chromium-*"))


async def install_browser(on_progress=None) -> tuple[bool, str]:
    """Ставит Chromium через сам Playwright. Прогресс отдаём построчно."""
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    # В собранном .app sys.executable — это само приложение, а не Python,
    # поэтому используем встроенный CLI Playwright напрямую.
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "playwright", "install", "chromium"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line and on_progress:
                on_progress(line)
        await proc.wait()
        if proc.returncode == 0 and browser_installed():
            return True, "Браузер установлен."
        return False, f"Установка завершилась с кодом {proc.returncode}."
    except Exception as e:
        return False, f"Не удалось запустить установку: {e}"


def ollama_installed() -> bool:
    return shutil.which("ollama") is not None or Path("/Applications/Ollama.app").exists()


async def ollama_running(url: str = "http://localhost:11434") -> bool:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/api/tags", timeout=3) as r:
                return r.status == 200
    except Exception:
        return False


def logged_in() -> bool:
    """Есть ли сохранённая сессия HH."""
    from settings import data_dir
    return (data_dir() / "state.json").exists()


async def status() -> dict:
    """Сводка для мастера первого запуска."""
    from settings import settings
    return {
        "browser": browser_installed(),
        "ollama_installed": ollama_installed(),
        "ollama_running": await ollama_running(settings.data["llm"]["ollama_url"]),
        "logged_in": logged_in(),
        "resume": bool(settings.target_resume_name),
        "summary": bool(settings.resume_summary.strip()),
    }
