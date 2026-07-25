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
        self.tray = None
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
        # Иконка в строке меню тоже должна отражать состояние
        if event == "state" and self.tray:
            try:
                self.tray.icon = _make_icon_image(bool(payload.get("running")))
            except Exception:
                pass
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

    def test_notification(self):
        """Проверка уведомлений. Нужна потому, что macOS показывает их только
        от подписанного .app — из запущенного скрипта они молча не появляются."""
        from notify_sinks import DesktopSink
        sink = DesktopSink()
        ok, why = sink._available()
        if not ok:
            return {"ok": False, "message": why}
        try:
            self._submit(sink.notify("HH Agent\nУведомления работают.")).result(timeout=15)
            return {"ok": True, "message": "Уведомление отправлено — проверьте Центр уведомлений."}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def setup_status(self):
        """Что готово к работе, а что нужно доустановить."""
        import first_run
        try:
            return self._submit(first_run.status()).result(timeout=20)
        except Exception as e:
            return {"error": str(e)}

    def install_browser(self):
        """Ставит Chromium: внутрь приложения он не входит осознанно."""
        import first_run

        async def run():
            self._log("Устанавливаю браузер для Playwright…")
            ok, msg = await first_run.install_browser(
                on_progress=lambda line: self._log(line))
            self._log(("✅ " if ok else "❌ ") + msg, "info" if ok else "error")
            self._emit("setup_done", {"ok": ok, "message": msg})

        self._submit(run())
        return {"ok": True}

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


def _make_icon_image(running: bool):
    """Рисует иконку для строки меню: кружок, зелёный когда агент работает."""
    from PIL import Image, ImageDraw
    size = 44
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = (48, 209, 88, 255) if running else (0, 0, 0, 0)
    outline = (48, 209, 88, 255) if running else (110, 110, 115, 255)
    d.ellipse([7, 7, size - 8, size - 8], fill=fill, outline=outline, width=4)
    return img


def build_tray(bridge):
    """Иконка в строке меню. Полноценное окно остаётся основным интерфейсом,
    отсюда — только быстрые действия."""
    import pystray
    from pystray import MenuItem as Item

    def toggle(icon, item):
        if bridge.running:
            bridge.stop_agent()
        else:
            bridge.start_agent(settings.data["schedule"].get("session_minutes", 0))
        icon.icon = _make_icon_image(bridge.running)

    def show_window(icon, item):
        try:
            bridge.window.show()
        except Exception:
            pass

    def quit_app(icon, item):
        try:
            if bridge.running:
                bridge.stop_agent()
        finally:
            icon.stop()
            try:
                bridge.window.destroy()
            except Exception:
                pass

    menu = pystray.Menu(
        Item(lambda i: "Остановить агента" if bridge.running else "Запустить агента", toggle),
        Item("Показать окно", show_window, default=True),
        pystray.Menu.SEPARATOR,
        Item("Выход", quit_app),
    )
    icon = pystray.Icon("hh-agent", _make_icon_image(False), "HH Agent", menu)
    bridge.tray = icon
    return icon


def selftest():
    """Проверка окружения без открытия окна: HHAGENT_SELFTEST=1 или флаг
    --selftest. Нужна, чтобы убедиться, что в собранном .app действительно
    работают уведомления — из обычного скрипта они молча не появляются."""
    import first_run
    from notify_sinks import DesktopSink

    loop = asyncio.new_event_loop()
    try:
        print("bundle_id:", _bundle_id())
        ok, why = DesktopSink()._available()
        print("уведомления:", "доступны" if ok else f"недоступны — {why}")
        if ok:
            loop.run_until_complete(
                DesktopSink().notify("HH Agent\nСамопроверка: уведомления работают."))
            print("тестовое уведомление отправлено")
        print("готовность:", loop.run_until_complete(first_run.status()))
    finally:
        loop.close()
    return 0


def _bundle_id():
    try:
        from rubicon.objc import ObjCClass
        b = ObjCClass("NSBundle").mainBundle
        return str(b.bundleIdentifier) if b and b.bundleIdentifier else None
    except Exception as e:
        return f"(не определить: {e})"


def main():
    if os.environ.get("HHAGENT_SELFTEST") or "--selftest" in sys.argv:
        sys.exit(selftest())

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

    # Иконку в строке меню поднимаем ДО webview.start(): на macOS она не
    # заводит свой цикл событий, а пользуется тем, который создаст интерфейс.
    tray = None
    try:
        tray = build_tray(bridge)
        tray.run_detached()
    except Exception as e:
        print(f"ℹ️ Иконка в строке меню недоступна: {e}")

    try:
        webview.start()  # блокирует главный поток до закрытия окна
    finally:
        if tray:
            try:
                tray.stop()
            except Exception:
                pass
        sys.stdout = bridge._stdout_backup
        if bridge.running and bridge.loop:
            bridge.loop.call_soon_threadsafe(control.request_stop)


if __name__ == "__main__":
    main()
