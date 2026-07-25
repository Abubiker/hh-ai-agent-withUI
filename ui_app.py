"""Десктопное приложение: окно с интерфейсом поверх того же агента.

Про потоки (главное здесь). На macOS интерфейс обязан жить на ГЛАВНОМ потоке —
этого требует Cocoa. asyncio на том же потоке ужиться не может, поэтому:

    главный поток          — окно и весь интерфейс
    фоновый поток          — свой event loop, в нём работает агент
    интерфейс → агент      — asyncio.run_coroutine_threadsafe
    агент → интерфейс      — window.evaluate_js

Логи агента (обычные print внутри hh_client) перехватываются и уходят в окно,
поэтому переписывать вывод по всему коду не пришлось.
"""
import asyncio
import base64
import json
import os
import sys
import threading
import traceback
from pathlib import Path

import webview

import control
from settings import settings
from notify_sinks import build_sinks
from stats import Stats

UI_DIR = Path(__file__).parent / "ui"


class LogTee:
    """Дублирует stdout в окно приложения, не ломая обычный вывод в консоль."""

    def __init__(self, original, emit):
        self._original = original
        self._emit = emit
        self._buffer = ""

    def write(self, text):
        self._original.write(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                try:
                    self._emit(line)
                except Exception:
                    pass  # проблемы с окном не должны ронять агента

    def flush(self):
        self._original.flush()

    def isatty(self):
        return False


class AgentBridge:
    """Методы этого класса вызываются из JavaScript как window.pywebview.api.*"""

    def __init__(self):
        self.window = None
        self.loop = None
        self.thread = None
        self.client = None
        self.agent_task = None
        self.running = False
        self._captcha_future = None
        self._stdout_backup = None

    # ---------- служебное ----------

    def _emit(self, event: str, payload=None):
        """Отправляет событие в интерфейс."""
        if not self.window:
            return
        try:
            self.window.evaluate_js(
                f"window.onAgentEvent({json.dumps(event)}, {json.dumps(payload)})")
        except Exception:
            pass

    def _log(self, line: str, level: str = "info"):
        self._emit("log", {"line": line, "level": level})

    def _start_loop(self):
        """Поднимает фоновый поток с собственным циклом событий."""
        if self.loop:
            return
        ready = threading.Event()

        def runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=runner, daemon=True, name="agent-loop")
        self.thread.start()
        ready.wait(timeout=5)

    def _submit(self, coro):
        """Запускает корутину в фоновом цикле, не блокируя интерфейс."""
        self._start_loop()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---------- настройки ----------

    def get_settings(self):
        data = json.loads(json.dumps(settings.data))  # копия для интерфейса
        # Секреты не отдаём целиком: показываем только факт их наличия.
        data["_secrets"] = {
            "tg_bot_token": bool(settings.get_secret("tg_bot_token")),
            "anthropic_api_key": bool(settings.get_secret("anthropic_api_key")),
            "openai_api_key": bool(settings.get_secret("openai_api_key")),
        }
        return data

    def save_settings(self, incoming):
        try:
            secrets = incoming.pop("_secrets", None) or {}
            incoming.pop("_secrets", None)
            for key, value in secrets.items():
                if value:  # пустое поле означает «не менять»
                    settings.set_secret(key, value)
            for section, values in incoming.items():
                if section in settings.data and isinstance(values, dict):
                    settings.data[section].update(values)
            settings.save()
            return {"ok": True, "path": str(settings.path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- модель ----------

    def check_provider(self):
        from llm_providers import get_provider
        try:
            fut = self._submit(get_provider().health())
            ok, msg = fut.result(timeout=60)
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def list_models(self):
        from llm_providers import get_provider
        try:
            fut = self._submit(get_provider().list_models())
            return {"ok": True, "models": fut.result(timeout=30)}
        except Exception as e:
            return {"ok": False, "models": [], "error": str(e)}

    def pull_model(self, name: str):
        """Скачивает модель, показывая прогресс в интерфейсе."""
        from llm_providers import OllamaProvider

        def on_progress(status, done, total):
            pct = int(done / total * 100) if total else 0
            self._emit("pull_progress", {"status": status, "percent": pct})

        async def run():
            try:
                await OllamaProvider().pull_model(name, on_progress=on_progress)
                self._emit("pull_done", {"ok": True, "model": name})
            except Exception as e:
                self._emit("pull_done", {"ok": False, "error": str(e)})

        self._submit(run())
        return {"ok": True}

    # ---------- капча ----------

    async def _ask_captcha(self, image_path: str, prompt: str):
        """Показывает скриншот капчи в окне и ждёт ввода. Вызывается из UISink."""
        loop = asyncio.get_running_loop()
        self._captcha_future = loop.create_future()

        image_data = ""
        try:
            with open(image_path, "rb") as f:
                image_data = "data:image/png;base64," + base64.b64encode(f.read()).decode()
        except Exception as e:
            self._log(f"Не удалось прочитать скриншот капчи: {e}", "warn")

        import re
        self._emit("captcha", {"image": image_data,
                               "prompt": re.sub(r"<[^>]+>", "", prompt)})
        try:
            # Ждём ограниченное время: пользователь мог отойти от компьютера.
            return await asyncio.wait_for(self._captcha_future, timeout=300)
        except asyncio.TimeoutError:
            self._emit("captcha_close", None)
            self._log("Капча не введена за 5 минут — пропускаю вакансию.", "warn")
            return None
        finally:
            self._captcha_future = None

    def submit_captcha(self, text: str):
        """Вызывается из интерфейса, когда пользователь ввёл текст."""
        fut = self._captcha_future
        if fut and not fut.done():
            # Future принадлежит фоновому циклу — трогаем его только оттуда.
            self.loop.call_soon_threadsafe(fut.set_result, text or None)
        return {"ok": True}

    # ---------- запуск и остановка ----------

    def start_agent(self, session_minutes=0):
        if self.running:
            return {"ok": False, "error": "Агент уже работает"}

        from hh_client import HHClient

        control.configure(int(session_minutes) * 60 or None)
        control.set_telegram_enabled(
            bool(settings.data["notifications"]["telegram_enabled"]))

        async def run():
            self.running = True
            self._emit("state", {"running": True})
            client = None
            try:
                # Пересоздаём событие остановки уже внутри нужного цикла
                control.stop_event = asyncio.Event()

                sinks = build_sinks(
                    telegram=control.telegram_enabled,
                    desktop=bool(settings.data["notifications"]["desktop_enabled"]),
                    ui_log=lambda line, level="info": self._log(line, level),
                    ui_captcha=self._ask_captcha,
                )
                client = HHClient(sinks=sinks)
                self.client = client

                await client.start()
                if not await client.login_if_needed():
                    self._log("Не удалось авторизоваться на HH.", "error")
                    return

                control.arm()
                await sinks.notify("🤖 Агент запущен. " + control.duration_text())

                while not control.should_stop():
                    try:
                        await client.search_and_apply(sinks.notify)
                        await client.check_chats(sinks.notify)
                    except Exception as e:
                        self._log(f"Ошибка в цикле агента: {e}", "error")
                    self._emit("stats", client.stats.__dict__)
                    if control.should_stop():
                        break
                    pause = settings.cycle_pause_minutes
                    self._log(f"😴 Круг закончен. Жду {pause} мин до следующего.")
                    await control.sleep_or_stop(pause * 60)
            except Exception:
                self._log(traceback.format_exc(), "error")
            finally:
                if client:
                    self._emit("stats", client.stats.__dict__)
                    self._log(client.stats.summary_plain())
                    try:
                        await client.sinks.notify(client.stats.summary())
                        await client.sinks.close()
                    except Exception:
                        pass
                    try:
                        await client.stop()
                    except Exception:
                        pass
                self.running = False
                self.client = None
                self._emit("state", {"running": False})

        self.agent_task = self._submit(run())
        return {"ok": True}

    def stop_agent(self):
        if not self.running:
            return {"ok": False, "error": "Агент не запущен"}
        self._log("🛑 Останавливаюсь — соберу статистику…")
        if self.loop:
            self.loop.call_soon_threadsafe(control.request_stop)
        return {"ok": True}

    def get_state(self):
        stats = self.client.stats.__dict__ if self.client else Stats().__dict__
        return {"running": self.running, "stats": stats}

    # ---------- прочее ----------

    def open_settings_folder(self):
        try:
            os.system(f'open "{settings.path.parent}"')
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def install_hint(self):
        """Данные для мастера первого запуска."""
        from playwright._impl._driver import compute_driver_executable  # noqa: F401
        browsers = Path.home() / "Library" / "Caches" / "ms-playwright"
        return {
            "browser_installed": browsers.exists() and any(browsers.glob("chromium*")),
            "logged_in": (Path(__file__).parent / "state.json").exists(),
            "resume_set": bool(settings.target_resume_name),
        }


def main():
    bridge = AgentBridge()
    window = webview.create_window(
        "HH Agent",
        str(UI_DIR / "index.html"),
        js_api=bridge,
        width=1080,
        height=760,
        min_size=(880, 620),
    )
    bridge.window = window

    # Перехватываем вывод агента, чтобы он был виден в окне
    bridge._stdout_backup = sys.stdout
    sys.stdout = LogTee(sys.stdout, lambda line: bridge._log(line))

    try:
        webview.start()  # блокирует главный поток до закрытия окна
    finally:
        sys.stdout = bridge._stdout_backup
        if bridge.running and bridge.loop:
            bridge.loop.call_soon_threadsafe(control.request_stop)


if __name__ == "__main__":
    main()
