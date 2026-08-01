"""Проверки и установка того, что нельзя положить внутрь приложения.

Camoufox (~700 МБ: браузер + geoip-базы + аддон) намеренно не входит в
.app: с PyInstaller у бинарников такого рода ломаются пути к исполняемому
файлу, да и размер приложения вырос бы в разы. Вместо этого проверяем
наличие браузера и ставим его по кнопке.
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path


def camoufox_dir() -> Path:
    """Camoufox сам решает, куда класть бинарник (~/Library/Caches/camoufox
    на macOS) — тут просто дублируем его правило, чтобы проверить наличие
    без импорта самого camoufox (он тяжёлый и нужен не всем)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "camoufox"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "camoufox"
    return Path.home() / ".cache" / "camoufox"


def camoufox_installed() -> bool:
    d = camoufox_dir() / "browsers"
    return d.exists() and any(d.iterdir())


async def install_camoufox(on_progress=None) -> tuple[bool, str]:
    """Ставит бинарник Camoufox (~700 МБ: браузер + geoip-базы + аддон) по
    кнопке в мастере первого запуска.

    Раньше это делал subprocess [sys.executable, "-m", "camoufox", "fetch"].
    В собранном .app sys.executable — это сам AbuHH, а не python: команда
    вместо установки браузера открывала вторую копию приложения (второе
    окно поверх первого). Поэтому ставим прямо в этом процессе, в фоновом
    потоке, вызывая ту же функцию, что стоит за `camoufox fetch`."""
    def run():
        import contextlib
        from camoufox.__main__ import fetch as fetch_cmd

        class _Tee:
            def __init__(self):
                self._buf = ""

            def write(self, s):
                self._buf += s
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    if line and on_progress:
                        on_progress(line)

            def flush(self):
                pass

        with contextlib.redirect_stdout(_Tee()):
            fetch_cmd.callback(None)

    try:
        await asyncio.to_thread(run)
        if camoufox_installed():
            return True, "Camoufox установлен."
        return False, "Установка завершилась без ошибок, но браузер не найден — см. лог выше."
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
    """Есть ли сохранённая сессия АКТИВНОЙ площадки HH (hh.ru/hh.kz/...)."""
    import sites
    return sites.is_logged_in()


async def status() -> dict:
    """Сводка для мастера первого запуска.

    Готовность модели считается по выбранному провайдеру: на облачной модели
    Ollama не нужна вовсе, и требовать её запуска — значит держать чек-лист
    вечно незакрытым.
    """
    import sites
    from settings import settings
    llm = settings.data["llm"]
    provider = llm.get("provider", "ollama")

    if provider == "ollama":
        running = await ollama_running(llm["ollama_url"])
        model_ready = running
        model_note = "" if running else (
            "не запущена" if ollama_installed() else "не установлена")
    elif provider == "anthropic":
        model_ready = bool(settings.get_secret("anthropic_api_key"))
        model_note = "" if model_ready else "нужен ключ Anthropic"
    else:  # любой OpenAI-совместимый сервер: OpenRouter, LM Studio, Groq…
        model_ready = bool(llm.get("openai_model"))
        model_note = "" if model_ready else "выберите модель"

    return {
        "browser": camoufox_installed(),
        "provider": provider,
        "ollama_installed": ollama_installed(),
        "ollama_running": provider != "ollama" or model_ready,
        "model_ready": model_ready,
        "model_note": model_note,
        "logged_in": logged_in(),
        "site": sites.active_site()["name"],
        "resume": bool(settings.target_resume_name),
        "summary": bool(settings.resume_summary.strip()),
    }
