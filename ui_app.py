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

# ВАЖНО: до любого импорта playwright. В собранном .app он по умолчанию ищет
# браузер внутри себя (Contents/Resources/playwright/driver/.local-browsers),
# где его нет и быть не может — Chromium мы намеренно не бутылим. Указываем
# обычное пользовательское расположение, куда его ставит `playwright install`.
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    if sys.platform == "darwin":
        _browsers = Path.home() / "Library" / "Caches" / "ms-playwright"
    elif os.name == "nt":
        _browsers = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    else:
        _browsers = Path.home() / ".cache" / "ms-playwright"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers)

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
        self.started_at = None
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

    def list_models_detail(self):
        from llm_providers import OllamaProvider
        try:
            fut = self._submit(OllamaProvider().list_models_detail())
            return {"ok": True, "models": fut.result(timeout=30)}
        except Exception as e:
            return {"ok": False, "models": [], "error": str(e)}

    def delete_model(self, name: str):
        from llm_providers import OllamaProvider
        try:
            self._submit(OllamaProvider().delete_model(name)).result(timeout=30)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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

        # Импорт здесь, а не наверху — но любая ошибка обязана дойти до окна.
        # Раньше исключение улетало в отклонённый промис, и нажатие «Запустить»
        # выглядело как «ничего не происходит».
        try:
            from hh_client import HHClient  # noqa: F401
        except Exception as e:
            msg = f"Не удалось загрузить модуль агента: {type(e).__name__}: {e}"
            self._log(msg, "error")
            self._log(traceback.format_exc(), "error")
            return {"ok": False, "error": msg}

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
                import time as _time
                self.started_at = _time.time()
                self._emit("state", {"running": True, "started_at": self.started_at})
                await sinks.notify("🤖 Агент запущен. " + control.duration_text())

                while not control.should_stop():
                    fresh_before = client.stats.fresh
                    try:
                        await client.search_and_apply(sinks.notify)
                        await client.check_chats(sinks.notify)
                    except Exception as e:
                        self._log(f"Ошибка в цикле агента: {e}", "error")
                    self._emit("stats", client.stats.__dict__)
                    if control.should_stop():
                        break
                    pause = settings.cycle_pause_minutes
                    next_at = _time.strftime("%H:%M", _time.localtime(_time.time() + pause * 60))
                    if client.stats.fresh == fresh_before:
                        self._log(f"Новых вакансий не появилось. Следующая проверка в {next_at}.")
                    else:
                        self._log(f"Проверка закончена: новых вакансий {client.stats.fresh - fresh_before}, "
                                  f"следующая в {next_at}.")
                    # Отдельное событие для обратного отсчёта в интерфейсе
                    self._emit("pause", {"seconds": pause * 60})
                    await control.sleep_or_stop(pause * 60)
                    self._emit("pause", None)
            except Exception:
                self._log(traceback.format_exc(), "error")
            finally:
                if client:
                    self._emit("stats", client.stats.__dict__)
                    # Отдельный _log не нужен: sinks включают UISink и сами
                    # пишут итоги в окно — иначе статистика дублировалась.
                    try:
                        await client.sinks.notify(client.stats.summary(), kind="summary")
                        await client.sinks.close()
                    except Exception:
                        pass
                    try:
                        await client.stop()
                    except Exception:
                        pass
                self.running = False
                self.client = None
                self.started_at = None
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
        return {"running": self.running, "stats": stats, "started_at": self.started_at}

    # ---------- прочее ----------

    def test_notification(self):
        """Шлёт тестовое во ВСЕ включённые каналы — и на рабочий стол, и в
        Telegram, чтобы проверить каждый настроенный способ разом."""
        from notify_sinks import DesktopSink, TelegramSink

        n = settings.data["notifications"]
        results = []

        if n.get("desktop_enabled"):
            sink = DesktopSink()
            ok, why = sink._available()
            if not ok:
                results.append(("Рабочий стол", False, why))
            else:
                try:
                    self._submit(sink.notify(
                        "HH Agent\nТестовое уведомление — всё работает.")).result(timeout=20)
                    results.append(("Рабочий стол", True, "отправлено"))
                except Exception as e:
                    results.append(("Рабочий стол", False, str(e)))

        if n.get("telegram_enabled"):
            import tg_bot
            if not tg_bot.bot:
                results.append(("Telegram", False, "не задан токен бота"))
            elif not n.get("tg_user_id"):
                results.append(("Telegram", False, "не указан ваш Telegram ID"))
            else:
                try:
                    control.set_telegram_enabled(True)
                    self._submit(TelegramSink().notify(
                        "🔔 <b>Тестовое уведомление</b>\nHH Agent на связи.")).result(timeout=25)
                    results.append(("Telegram", True, "отправлено"))
                except Exception as e:
                    results.append(("Telegram", False, str(e)))

        if not results:
            return {"ok": False, "message": "Ни один способ уведомлений не включён."}

        ok_all = all(r[1] for r in results)
        text = "; ".join(f"{name} — {'ОК' if good else 'ошибка: ' + why}"
                         for name, good, why in results)
        return {"ok": ok_all, "message": text}

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

    def open_url(self, url: str):
        """Открывает ссылку в браузере пользователя (например, страницу Ollama)."""
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "недопустимая ссылка"}
        try:
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def copy_to_clipboard(self, text: str):
        """Копирование через системную утилиту: navigator.clipboard в WKWebView
        требует защищённого контекста и на file:// не работает."""
        import subprocess
        if sys.platform == "darwin":
            cmds = [["pbcopy"]]
        elif os.name == "nt":
            cmds = [["clip"]]
        else:
            # На Wayland работает wl-copy, на X11 — xclip/xsel; какой из них
            # стоит в системе, заранее не известно, поэтому пробуем по очереди.
            cmds = [["wl-copy"], ["xclip", "-selection", "clipboard"],
                    ["xsel", "--clipboard", "--input"]]
        last = "нет подходящей утилиты"
        for cmd in cmds:
            try:
                subprocess.run(cmd, input=(text or "").encode("utf-8"), check=True)
                return {"ok": True}
            except Exception as e:
                last = str(e)
        return {"ok": False, "error": last}

    def get_areas(self):
        """Справочник регионов hh.ru для модалки выбора. При недоступном API
        (не-РФ сеть отдаёт 403) интерфейс переключается на ручной ввод."""
        import hh_api
        try:
            areas, source = self._submit(hh_api.fetch_areas()).result(timeout=15)
            return {"ok": True, "areas": areas, "source": source,
                    "schedules": hh_api.SCHEDULES}
        except Exception as e:
            import hh_api as _h
            return {"ok": False, "areas": [], "source": "none",
                    "schedules": _h.SCHEDULES, "error": str(e)}

    def open_ollama_app(self):
        """Поднимает Ollama — используется в баннере «модель не отвечает».

        На macOS это обычная программа, на Linux — служба systemd, а если
        служба не заведена, остаётся запустить сервер самим.
        """
        import subprocess
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", "-a", "Ollama"], check=True, timeout=10)
                return {"ok": True}
            if os.name == "nt":
                subprocess.Popen(["ollama", "app.exe"])
                return {"ok": True}
            for cmd in (["systemctl", "--user", "start", "ollama"],
                        ["systemctl", "start", "ollama"]):
                try:
                    if subprocess.run(cmd, timeout=15).returncode == 0:
                        return {"ok": True}
                except Exception:
                    continue
            # Службы нет — просто держим сервер, пока открыто окно.
            subprocess.Popen(["ollama", "serve"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_settings_folder(self):
        import subprocess
        opener = ("open" if sys.platform == "darwin"
                  else "explorer" if os.name == "nt" else "xdg-open")
        try:
            subprocess.Popen([opener, str(settings.path.parent)])
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


def _tray_supported() -> bool:
    """Есть ли куда поставить иконку.

    На Linux pystray без AppIndicator откатывается на голый X11, не находит
    менеджер трея и роняет AssertionError уже в своём потоке — снаружи её
    не поймать, в журнал летит трейсбек. Проверяем заранее.
    """
    if not sys.platform.startswith("linux"):
        return True
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        for lib, ver in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
            try:
                gi.require_version(lib, ver)
                return True
            except ValueError:
                continue
    except Exception:
        pass
    return False


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
        # bundle_id — понятие macOS: там от него зависит, покажет ли система
        # уведомление. На других системах строка только сбивала бы с толку.
        if sys.platform == "darwin":
            print("bundle_id:", _bundle_id())
        ok, why = DesktopSink()._available()
        print("уведомления:", "доступны" if ok else f"недоступны — {why}")
        if ok:
            loop.run_until_complete(
                DesktopSink().notify("HH Agent\nСамопроверка: уведомления работают."))
            print("тестовое уведомление отправлено")
        print("готовность:", loop.run_until_complete(first_run.status()))

        # Всё, что нужно для кнопки «Запустить». Проверяем именно здесь, потому
        # что модули импортируются лениво и их отсутствие в сборке всплывает
        # только в момент запуска агента.
        print("\nмодули агента:")
        for mod in ("hh_client", "database", "ai_analyzer", "llm_providers",
                    "notify_sinks", "tg_bot", "control", "playwright_stealth"):
            try:
                __import__(mod)
                print(f"  ✅ {mod}")
            except Exception as e:
                print(f"  ❌ {mod}: {type(e).__name__}: {e}")

        print("\nзапуск браузера:")
        async def try_browser():
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            b = await pw.chromium.launch(headless=True)
            page = await b.new_page()
            await page.goto("about:blank")
            await b.close()
            await pw.stop()
        try:
            loop.run_until_complete(asyncio.wait_for(try_browser(), timeout=60))
            print("  ✅ браузер поднимается")
        except Exception as e:
            print(f"  ❌ {type(e).__name__}: {str(e)[:300]}")
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
        # По умолчанию pywebview впрыскивает на body `user-select: none` —
        # тогда в журнале нельзя выделить и скопировать ни строчки.
        # Выделение отключается точечно в CSS (кнопки, меню), а текст
        # журнала и полей остаётся выделяемым; Cmd+C работает через
        # штатное меню «Правка».
        text_select=True,
    )
    bridge.window = window

    # Перехватываем вывод агента, чтобы он был виден в окне
    bridge._stdout_backup = sys.stdout
    sys.stdout = LogTee(sys.stdout, lambda line: bridge._log(line))

    # Иконку в строке меню поднимаем ДО webview.start(): на macOS она не
    # заводит свой цикл событий, а пользуется тем, который создаст интерфейс.
    tray = None
    if _tray_supported():
        try:
            tray = build_tray(bridge)
            tray.run_detached()
        except Exception as e:
            print(f"ℹ️ Иконка в строке меню недоступна: {e}")
    else:
        print("ℹ️ Иконка в трее недоступна: нет поддержки AppIndicator. "
              "На работу агента это не влияет — пользуйтесь окном.")

    try:
        # На Linux движок называем явно. Иначе pywebview сперва пробует GTK,
        # не находит его (ставим мы Qt) и печатает трейсбек, из-за которого
        # кажется, что приложение упало, — хотя оно спокойно работает дальше.
        gui = "qt" if sys.platform.startswith("linux") else None
        webview.start(gui=gui)  # блокирует главный поток до закрытия окна
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
