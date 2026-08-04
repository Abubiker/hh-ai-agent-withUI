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
import re
import socket
import sys
import threading
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

import applog
import control
import database
import sites
from settings import settings
from notify_sinks import build_sinks
from stats import Stats

UI_DIR = Path(__file__).parent / "ui"


def _secret_hint(value: str) -> str | None:
    """«•••••• 4f2a» — видно и что ключ сохранён, и какой именно (хвост
    отличает Groq от Gemini после переключения). Короткие/тестовые значения
    (<8 символов) — только маска, хвост из них раскрывал бы ключ почти
    целиком."""
    if not value:
        return None
    if len(value) < 8:
        return "••••••"
    return f"•••••• {value[-4:]}"


def _openai_key_hint(base_url: str) -> str | None:
    """Ключ OpenAI-совместимого сервиса скоупится по хосту (settings.
    scoped_secret_name); если своего ключа нет, get_scoped_secret молча
    откатывается на общий бесскоупный слот — тогда подсказка помечается,
    что это ключ ДРУГОГО сервиса, а не текущего."""
    scoped_name = settings.scoped_secret_name("openai_api_key", base_url)
    scoped_value = settings.get_secret(scoped_name)
    value = scoped_value or settings.get_secret("openai_api_key")
    hint = _secret_hint(value)
    if hint and not scoped_value and value:
        hint += " — общий ключ"
    return hint


class LogTee:
    """Дублирует stdout в окно приложения, не ломая обычный вывод в консоль."""

    def __init__(self, original, emit):
        self._original = original
        self._emit = emit
        self._buffer = ""

    def write(self, text):
        # В собранном .app (HHAgent.spec: console=False) sys.stdout/stderr
        # могут быть None — раньше это падало прямо на первом print() после
        # старта. Тeéм stderr наравне со stdout, поэтому проверка обязательна.
        if self._original is not None:
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
        if self._original is not None:
            self._original.flush()

    def isatty(self):
        return False


def _turn_text(turn: dict) -> str:
    """Текст хода разговора, даже если к нему прикреплена картинка (ключ
    image_b64 никак не влияет на то, что здесь возвращается)."""
    return turn.get("content") or ""


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
        self._stderr_backup = None
        # История чата — persisted в agent.db (chat_turns), переживает
        # перезапуск приложения. image_b64 в памяти хранит только тело
        # base64 без data:-префикса (см. send_chat_message), в БД лежит
        # то же самое под именем image_ref.
        self._chat_history: list[dict] = [
            {"role": t["role"], "content": t["content"],
             **({"image_b64": t["image_ref"]} if t["image_ref"] else {})}
            for t in database.load_chat_turns()
        ]
        self._chat_busy = False
        self._chat_future = None
        self._provider_check_future = None
        # Гвард на мастер первого запуска: без него повторный клик «Войти»
        # (пока первая попытка ещё ждёт вход) открывал ВТОРОЙ браузер поверх
        # первого — оба висели и ждали, каждый со своим окном.
        self._wizard_login_busy = False

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
        import applog
        # origin считаем один раз здесь и передаём в applog.log() готовым —
        # обход кадров skip-листами (ui_app.py целиком, LogTee.write/_log)
        # даёт тот же результат что и повторный подсчёт внутри log(), но
        # дважды его вычислять незачем.
        where = applog.origin(depth=2)
        applog.log(line, level, origin_hint=where)
        payload = {"line": line, "level": level}
        # Origin в JS-событие — ТОЛЬКО у ошибок/предупреждений. В line его
        # не добавляем ни в каком виде: app.js разбирает строки регулярками
        # с якорем ^ (LOG_PATTERNS, updateNowFromLog) — префикс их сломает.
        if level in ("error", "warn"):
            payload["origin"] = where
        self._emit("log", payload)

    def _log_stderr(self, line: str):
        """stderr раньше не перехватывался вовсе — необработанный traceback
        не попадал ни в файл, ни в окно. В файл — как error (это и есть
        обычно traceback), в окно — как warn: один traceback на 15+ строк
        покрасил бы половину журнала в красный и раздул счётчик «Ошибки».

        Исключение — вывод модуля `warnings` (origin вида
        "warnings._showwarnmsg_impl:N", например безобидный
        multiprocessing.resource_tracker при остановке агента): это не
        traceback, в файл тоже пишем как warn, а не error."""
        import applog
        where = applog.origin(depth=2)
        file_level = "warn" if where.startswith("warnings.") else "error"
        applog.log(line, file_level, origin_hint=where)
        self._emit("log", {"line": line, "level": "warn", "origin": where})

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

    def get_letter_styles(self):
        """Список стилей сопроводительного для карточки на вкладке «Резюме».
        Названия/описания живут в ai_analyzer.LETTER_STYLES — JS их не
        хардкодит, чтобы нейминг менялся в одном месте."""
        import ai_analyzer
        return {"styles": ai_analyzer.LETTER_STYLES}

    def get_openai_presets(self):
        """Готовые адреса OpenAI-совместимых сервисов для вкладки «Модель».
        Список живёт в llm_providers.OPENAI_PRESETS — тот же список уже
        использует мастер первого запуска, JS его не дублирует."""
        from llm_providers import OPENAI_PRESETS
        return {"presets": [{"name": n, "url": u, "note": note}
                            for n, u, note in OPENAI_PRESETS]}

    def get_settings(self):
        data = json.loads(json.dumps(settings.data))  # копия для интерфейса
        # Секреты не отдаём целиком: показываем только факт их наличия.
        data["_secrets"] = {
            "tg_bot_token": bool(settings.get_secret("tg_bot_token")),
            "anthropic_api_key": bool(settings.get_secret("anthropic_api_key")),
            # Для OpenAI-совместимых ключ свой у каждого сервиса — показываем
            # наличие того, что относится к выбранному сейчас адресу.
            "openai_api_key": bool(settings.get_scoped_secret(
                "openai_api_key", settings.data["llm"]["openai_base_url"])),
        }
        # Хвост ключа — отдельным полем, НЕ подменяет _secrets выше: тот
        # булев контракт двусторонний (save_settings шлёт туда настоящие
        # значения), а хвост — только для отображения.
        data["_secret_hints"] = {
            "tg_bot_token": _secret_hint(settings.get_secret("tg_bot_token")),
            "anthropic_api_key": _secret_hint(settings.get_secret("anthropic_api_key")),
            "openai_api_key": _openai_key_hint(settings.data["llm"]["openai_base_url"]),
        }
        return data

    def get_openai_key_hint(self, base_url):
        """Вкладка «Модель» спрашивает это ДО сохранения, сразу при смене
        сервиса — иначе после Groq → Gemini подпись «сохранён» относилась бы
        к ключу Groq, а не к (пока ещё пустому) ключу Gemini."""
        return {"hint": _openai_key_hint(base_url)}

    def save_settings(self, incoming):
        try:
            secrets = incoming.pop("_secrets", None) or {}
            incoming.pop("_secrets", None)
            # Адрес сервиса берём из этого же сохранения: пользователь мог
            # сменить его и ввести ключ одним действием.
            base_url = (incoming.get("llm") or {}).get(
                "openai_base_url") or settings.data["llm"]["openai_base_url"]
            for key, value in secrets.items():
                if not value:  # пустое поле означает «не менять»
                    continue
                if "•" in value:
                    # Подсказка-хвост случайно попала в поле как значение
                    # (например, автосейв дёрнул поле раньше, чем JS его
                    # очистил) — это не ключ, записывать нельзя.
                    continue
                if key == "tg_bot_token" and not re.match(r"^\d+:\S+$", value):
                    # Токен бота имеет вид "123456:AAExxx" — форма, непохожая
                    # на это, почти всегда обрезок от битого автосейва
                    # (см. фикс #tgToken на смену листенера с input на change).
                    continue
                if key == "openai_api_key":
                    settings.set_scoped_secret(key, base_url, value)
                else:
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
        # Раньше блокировала мост до 60с — если провайдер завис (не ответил
        # и не уронил соединение), окно не отвечало вообще, единственным
        # выходом было убить приложение. Теперь не ждём здесь: результат
        # приходит событием provider_check_done, а зависший запрос можно
        # прервать через cancel_provider_check() (см. там же про отмену).
        self._provider_check_future = self._submit(self._run_provider_check())
        return {"ok": True}

    async def _run_provider_check(self):
        from llm_providers import get_provider
        try:
            ok, msg = await get_provider().health()
            self._emit("provider_check_done", {"ok": ok, "message": msg})
        except asyncio.CancelledError:
            self._emit("provider_check_done", {"ok": False, "message": "Остановлено", "cancelled": True})
            raise
        except Exception as e:
            self._emit("provider_check_done", {"ok": False, "message": str(e)})

    def cancel_provider_check(self):
        # concurrent.futures.Future из run_coroutine_threadsafe: cancel()
        # потокобезопасен и планирует отмену корутины в её собственном
        # цикле — если она сейчас ждёт на aiohttp-запросе, отмена закрывает
        # соединение, а не просто перестаёт слушать результат.
        fut = self._provider_check_future
        if fut and not fut.done():
            fut.cancel()
        return {"ok": True}

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

    def confirm_login(self):
        """Пользователь нажал «Я вошёл — сохранить сейчас»: подстраховка
        поверх авто-детекта входа в login_if_needed(), на случай если
        разметка hh изменится и локатор перестанет находить признак входа.
        Событие принадлежит фоновому циклу — трогаем его только оттуда."""
        if self.client and self.client.login_confirm_event:
            self.loop.call_soon_threadsafe(self.client.login_confirm_event.set)
        return {"ok": True}

    # ---------- чат ----------

    def send_chat_message(self, text: str, image_data_url: str | None = None):
        text = (text or "").strip()
        if not text and not image_data_url:
            return {"ok": False, "error": "Пустое сообщение"}
        if self._chat_busy:
            return {"ok": False, "error": "Дождитесь ответа на предыдущее сообщение"}
        image_b64 = None
        if image_data_url:
            m = re.match(r"^data:image/[^;]+;base64,(.+)$", image_data_url, re.DOTALL)
            if not m:
                return {"ok": False, "error": "Не удалось разобрать изображение"}
            image_b64 = m.group(1)
        turn = {"role": "user", "content": text or "(изображение без подписи)"}
        if image_b64:
            turn["image_b64"] = image_b64
        self._chat_history.append(turn)
        database.add_chat_turn("user", turn["content"], image_b64)
        self._chat_future = self._submit(self._run_chat_turn())
        return {"ok": True}

    def retry_last_chat_message(self):
        if self._chat_busy:
            return {"ok": False, "error": "Дождитесь ответа"}
        if not self._chat_history or self._chat_history[-1]["role"] != "user":
            return {"ok": False, "error": "Нечего повторять"}
        self._chat_future = self._submit(self._run_chat_turn())
        return {"ok": True}

    def stop_chat(self):
        """Прерывает текущий ответ модели в чате — та же отмена через
        Future, что и cancel_provider_check(), см. пояснение там."""
        fut = self._chat_future
        if fut and not fut.done():
            fut.cancel()
        return {"ok": True}

    def reset_chat(self):
        self._chat_history = []
        database.clear_chat_turns()
        return {"ok": True}

    def get_chat_history(self):
        """Для отрисовки переписки при старте — до этого чат считался
        эфемерным и рендерился только по событиям (см. app.js: chat_reply)."""
        def to_data_url(image_b64):
            return f"data:image/png;base64,{image_b64}" if image_b64 else None
        return [
            {"role": t["role"], "text": t["content"], "image": to_data_url(t.get("image_b64"))}
            for t in self._chat_history
        ]

    async def _run_chat_turn(self):
        """Отправляет накопленную историю модели и рассылает результат
        событиями — ответ может занять много секунд (особенно с чтением
        резюме или откликом), поэтому не блокирует мост, как pull_model.
        """
        import resume_reader
        import quick_apply
        from chat_analyzer import build_chat_system_prompt, build_resume_turn, build_vacancy_turn
        from llm_providers import chat_with_retry, ProviderError

        self._chat_busy = True
        try:
            current = self._chat_history[-1]
            last_user = _turn_text(current)
            max_tokens = 2000
            images = None

            # Проводной формат — плоские {role, content}: картинка живёт
            # отдельным ключом в истории и не пересылается повторно на
            # будущих ходах (как текст резюме/вакансии уже сегодня эфемерен).
            turns = [{"role": t["role"], "content": t["content"]} for t in self._chat_history]

            image_b64 = current.get("image_b64")
            vacancy_url = None if image_b64 else quick_apply.find_vacancy_url(last_user)
            resume_url = None if (image_b64 or vacancy_url) else resume_reader.find_resume_url(last_user)

            if image_b64:
                # Скриншот — показываем модели напрямую, без текстовых обёрток.
                self._emit("chat_status", {"text": "Смотрю на скриншот…"})
                images = [image_b64]

            elif resume_url:
                self._emit("chat_status", {"text": "Читаю резюме…"})
                try:
                    resume_text = await resume_reader.fetch_resume_text(resume_url)
                except resume_reader.ResumeReadError as e:
                    self._emit("chat_error", {"error": str(e)})
                    return
                except Exception as e:
                    self._emit("chat_error", {"error": f"Не удалось открыть резюме: {e}"})
                    return
                turns = turns[:-1] + [{
                    "role": "user",
                    "content": build_resume_turn(resume_url, resume_text, last_user),
                }]
                max_tokens = 3000  # разбору резюме нужен запас побольше обычного
                self._emit("chat_status", {"text": "Анализирую…"})

            elif vacancy_url and quick_apply.has_apply_command(last_user):
                # Ссылка на вакансию + явная команда — реальный отклик.
                # Один слот капчи на всё приложение — не лезем во второй.
                if self._captcha_future is not None:
                    self._emit("chat_error", {
                        "error": "Капча уже ждёт ответа — дождитесь и повторите."})
                    return
                try:
                    result = await quick_apply.apply_to_vacancy(
                        vacancy_url, ui_captcha=self._ask_captcha,
                        captcha_busy=lambda: self._captcha_future is not None,
                        on_status=lambda t: self._emit("chat_status", {"text": t}))
                except quick_apply.QuickApplyError as e:
                    self._emit("chat_error", {"error": str(e)})
                    return
                except Exception as e:
                    self._emit("chat_error", {"error": f"Не удалось откликнуться: {e}"})
                    return
                # Детерминированный результат отклика — к модели не ходим,
                # тут нечего сочинять.
                self._chat_history.append({"role": "assistant", "content": result.message})
                database.add_chat_turn("assistant", result.message)
                self._emit("chat_reply", {"text": result.message})
                return

            elif vacancy_url:
                # Просто ссылка на вакансию — читаем и обсуждаем, без отклика.
                self._emit("chat_status", {"text": "Открываю вакансию…"})
                try:
                    info = await quick_apply.fetch_vacancy(vacancy_url)
                except quick_apply.QuickApplyError as e:
                    self._emit("chat_error", {"error": str(e)})
                    return
                except Exception as e:
                    self._emit("chat_error", {"error": f"Не удалось открыть вакансию: {e}"})
                    return
                turns = turns[:-1] + [{
                    "role": "user",
                    "content": build_vacancy_turn(vacancy_url, info.title, info.description, last_user),
                }]
                max_tokens = 3000
                self._emit("chat_status", {"text": "Анализирую…"})

            try:
                reply = await chat_with_retry(turns, system=build_chat_system_prompt(),
                                              max_tokens=max_tokens, timeout=180,
                                              images=images)
            except ProviderError as e:
                self._emit("chat_error", {"error": str(e)})
                return

            self._chat_history.append({"role": "assistant", "content": reply})
            database.add_chat_turn("assistant", reply)
            self._emit("chat_reply", {"text": reply})
        except asyncio.CancelledError:
            # stop_chat() отменяет future — событие через тот же chat_error,
            # что и обычная ошибка: фронт уже умеет по нему снять "печатает…"
            # и разблокировать поле ввода, отдельный тип события не нужен.
            self._emit("chat_error", {"error": "Остановлено"})
            raise
        finally:
            self._chat_busy = False

    # ---------- запуск и остановка ----------

    async def _login_client(self, client) -> bool:
        """Общий вход для start_agent() и мастера первого запуска
        (wizard_login): заводит confirm-event — подстраховку к авто-детекту
        («Я вошёл — сохранить сейчас»), эмитит await_login на время
        ожидания и гасит его после. Возвращает logged_in."""
        client.login_confirm_event = asyncio.Event()
        already_logged_in = sites.is_logged_in(client.site)
        if not already_logged_in:
            self._emit("await_login", {"site": client.site["host"]})
        try:
            return await client.login_if_needed()
        finally:
            if not already_logged_in:
                self._emit("await_login", None)

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
            # applog.exc(), не self._log(traceback...) — тот идёт через
            # applog.log(), который режет строку до 300 симв. (см. applog.py:
            # MAX_LOGGED_LINE) — полная трассировка при падении на старте
            # обрезалась бы в файле, который и пересылают для разбора.
            applog.exc()
            return {"ok": False, "error": msg}

        control.configure(int(session_minutes) * 60 or None)
        control.set_telegram_enabled(
            bool(settings.data["notifications"]["telegram_enabled"]))

        async def run():
            self.running = True
            self._emit("state", {"running": True})
            client = None
            stats_at_start = None
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
                # client.stats теперь накопительный (см. Stats.bump), снепшот
                # нужен, чтобы итоговое сообщение показывало разницу за этот
                # запуск, а не всё время работы агента.
                stats_at_start = dict(client.stats.__dict__)

                await client.start()
                logged_in = await self._login_client(client)
                if not logged_in:
                    self._log("Не удалось авторизоваться на HH.", "error")
                    return

                control.arm()
                import time as _time
                self.started_at = _time.time()
                self._emit("state", {"running": True, "started_at": self.started_at})
                await sinks.notify("🤖 Агент запущен. " + control.duration_text())

                while not control.should_stop():
                    fresh_before = client.stats.fresh
                    db_skipped_before = client.stats.db_skipped
                    try:
                        # Обе фазы цикла ловятся одним except ниже — без
                        # маркера в файловом логе не понять, какая из двух
                        # упала (только file, debug: в UI это был бы шум).
                        applog.log("цикл: search_and_apply", level="debug")
                        await client.search_and_apply(sinks.notify)
                        applog.log("цикл: check_chats", level="debug")
                        await client.check_chats(sinks.notify)
                    except Exception as e:
                        self._log(f"Ошибка в цикле агента: {e}", "error")
                        applog.exc()
                    self._emit("stats", client.stats.__dict__)
                    if control.should_stop():
                        break
                    pause = settings.cycle_pause_minutes
                    next_at = _time.strftime("%H:%M", _time.localtime(_time.time() + pause * 60))
                    # Видимость дедупа: одни и те же вакансии в выдаче — это
                    # нормально (объявления не пропадают), их отсекает база ДО
                    # модели, без токенов. Без счётчика казалось, что агент
                    # заново их обрабатывает.
                    db_skipped = client.stats.db_skipped - db_skipped_before
                    skip_note = f" (пропущено {db_skipped} уже обработанных)" if db_skipped else ""
                    if client.stats.fresh == fresh_before:
                        self._log(f"Новых вакансий не появилось.{skip_note} Следующая проверка в {next_at}.")
                    else:
                        self._log(f"Проверка закончена: новых вакансий {client.stats.fresh - fresh_before},"
                                  f"{skip_note} следующая в {next_at}.")
                    # Отдельное событие для обратного отсчёта в интерфейсе
                    self._emit("pause", {"seconds": pause * 60})
                    await control.sleep_or_stop(pause * 60)
                    self._emit("pause", None)
            except Exception as e:
                self._log(f"Ошибка в работе агента: {type(e).__name__}: {e}", "error")
                applog.exc()
            finally:
                if client:
                    self._emit("stats", client.stats.__dict__)
                    # Отдельный _log не нужен: sinks включают UISink и сами
                    # пишут итоги в окно — иначе статистика дублировалась.
                    try:
                        await client.sinks.notify(
                            client.stats.summary(stats_at_start), kind="summary")
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
        # Stats.load() читает накопленные тотал-счётчики из agent.db — так
        # воронка на экране не обнуляется, пока агент не запущен/после
        # перезапуска приложения (см. stats.py: bump()/load()).
        stats = self.client.stats.__dict__ if self.client else Stats.load().__dict__
        return {"running": self.running, "stats": stats, "started_at": self.started_at}

    def get_applied_jobs(self, limit: int = 200):
        """История откликов для вкладки «Статистика» — название и ссылка на
        вакансию, самые свежие первыми (см. database.load_applied_jobs)."""
        try:
            return {"ok": True, "jobs": database.load_applied_jobs(limit)}
        except Exception as e:
            return {"ok": False, "jobs": [], "error": str(e)}

    # ---------- мастер первого запуска ----------
    #
    # Три моста ниже — все "долгие" (вход до 10 минут, сетевые загрузки
    # страниц, вызов модели), поэтому не блокируют сам мост: сразу отдают
    # {"ok": True} и шлют результат отдельным событием — тот же приём, что
    # у pull_model() ниже по файлу.

    def wizard_login(self):
        """Разовый вход БЕЗ старта поиска вакансий — отдельный throwaway
        HHClient, закрывается сразу после результата. Шаг «Вход» мастера
        первого запуска; обычный «Запустить» логинится сам внутри
        start_agent() и этот мост не использует.

        Гвард на повторный вызов, пока первый ещё не завершился: без него
        нетерпеливый повторный клик «Войти» открывал ВТОРОЙ (и третий)
        браузер поверх ещё не закрывшегося первого — окна множились, и
        confirm_login() мог достучаться только до последнего self.client,
        оставляя более ранние висеть до 10-минутного таймаута."""
        if self._wizard_login_busy:
            return {"ok": False, "error": "Вход уже выполняется — подождите текущее окно."}
        self._wizard_login_busy = True
        from hh_client import HHClient

        async def run():
            client = HHClient()
            self.client = client
            try:
                await client.start()
                logged_in = await self._login_client(client)
                self._emit("wizard_login_done", {"ok": logged_in, "site": client.site["host"]})
            except Exception as e:
                self._emit("wizard_login_done", {"ok": False, "error": str(e)})
            finally:
                self._wizard_login_busy = False
                try:
                    await client.stop()
                except Exception:
                    pass
                if self.client is client:
                    self.client = None

        self._submit(run())
        return {"ok": True}

    def wizard_list_resumes(self):
        """Резюме соискателя для шага «Резюме» мастера — результат
        событием wizard_resumes_done: {"ok","resumes":[{title,url}],"error"}."""
        import resume_reader

        async def run():
            try:
                resumes = await resume_reader.list_my_resumes()
                self._emit("wizard_resumes_done", {"ok": True, "resumes": resumes})
            except Exception as e:
                self._emit("wizard_resumes_done", {"ok": False, "error": str(e)})

        self._submit(run())
        return {"ok": True}

    def wizard_condense_resume(self, url: str):
        """Читает выбранное резюме и просит модель собрать из него профиль
        (черновик — пользователь проверяет/правит его на шаге «Профиль»,
        прежде чем он попадёт в настройки). Результат событием
        wizard_profile_done: {"ok","summary","error"}."""
        import resume_reader
        import ai_analyzer

        async def run():
            try:
                text = await resume_reader.fetch_resume_text(url)
                summary = await ai_analyzer.condense_resume(text)
                self._emit("wizard_profile_done", {"ok": True, "summary": summary})
            except Exception as e:
                self._emit("wizard_profile_done", {"ok": False, "error": str(e)})

        self._submit(run())
        return {"ok": True}

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
                        "AbuHH\nТестовое уведомление — всё работает.")).result(timeout=20)
                    results.append(("Рабочий стол", True, "отправлено"))
                except Exception as e:
                    results.append(("Рабочий стол", False, str(e)))

        if n.get("telegram_enabled"):
            import tg_bot
            if not tg_bot.is_configured():
                results.append(("Telegram", False, "не задан токен бота"))
            elif not n.get("tg_user_id"):
                results.append(("Telegram", False, "не указан ваш Telegram ID"))
            else:
                try:
                    control.set_telegram_enabled(True)
                    self._submit(TelegramSink().notify(
                        "🔔 <b>Тестовое уведомление</b>\nAbuHH на связи.")).result(timeout=25)
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
        """Справочник регионов активной площадки для модалки выбора. При
        недоступном API (не-РФ сеть отдаёт 403) интерфейс переключается на
        ручной ввод. Список сужается до страны активной площадки, если кэш
        уже содержит нужную для этого разметку (см. hh_api.country_subtree)."""
        import hh_api
        try:
            areas, source = self._submit(hh_api.fetch_areas()).result(timeout=15)
            areas = hh_api.country_subtree(areas, sites.active_site().get("area"))
            return {"ok": True, "areas": areas, "source": source,
                    "schedules": hh_api.SCHEDULES}
        except Exception as e:
            import hh_api as _h
            return {"ok": False, "areas": [], "source": "none",
                    "schedules": _h.SCHEDULES, "error": str(e)}

    # ---------- площадка (hh.ru / hh.kz / ...) ----------

    def get_sites(self):
        return {"sites": sites.all_sites(), "active": sites.active_site()["id"]}

    def set_active_site(self, site_id: str):
        """Меняет активную площадку. Запрещено во время работы агента: он
        держит открытым браузер и context, привязанные к площадке, с которой
        стартовал, — подмена активной площадки на середине сеанса рассинхронит
        self.client.site с settings.active_site_id, из-за чего фильтр регионов
        начнёт отдавать пустой список для уже бегущего клиента."""
        if self.running:
            return {"ok": False, "error": "Остановите агента, чтобы сменить сайт поиска."}
        if not sites.by_id(site_id):
            return {"ok": False, "error": f"Неизвестный сайт: {site_id}"}
        settings.data.setdefault("site", {})["active"] = site_id
        settings.save()
        return {"ok": True}

    def install_camoufox(self):
        """Качает единственный поддерживаемый браузер — Camoufox (~700 МБ).
        Прогресс идёт строками в общий лог, по готовности переигрывается
        чек-лист готовности (setup_done)."""
        import first_run

        async def run():
            self._log("Устанавливаю Camoufox (~700 МБ, может занять пару минут)…")
            ok, msg = await first_run.install_camoufox(
                on_progress=lambda line: self._log(line))
            self._log(("✅ " if ok else "❌ ") + msg, "info" if ok else "error")
            self._emit("setup_done", {"ok": ok, "message": msg})

        self._submit(run())
        return {"ok": True}

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

    def open_logs_folder(self):
        import subprocess
        import applog
        applog._ensure_handler()  # чтобы папка точно существовала к открытию
        opener = ("open" if sys.platform == "darwin"
                  else "explorer" if os.name == "nt" else "xdg-open")
        try:
            subprocess.Popen([opener, str(applog.LOG_DIR)])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


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
    icon = pystray.Icon("abuhh", _make_icon_image(False), "AbuHH", menu)
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
                DesktopSink().notify("AbuHH\nСамопроверка: уведомления работают."))
            print("тестовое уведомление отправлено")
        print("готовность:", loop.run_until_complete(first_run.status()))

        # Всё, что нужно для кнопки «Запустить». Проверяем именно здесь, потому
        # что модули импортируются лениво и их отсутствие в сборке всплывает
        # только в момент запуска агента.
        print("\nмодули агента:")
        for mod in ("hh_client", "database", "ai_analyzer", "llm_providers",
                    "notify_sinks", "tg_bot", "control",
                    "resume_reader", "chat_analyzer", "quick_apply",
                    "sites", "hh_session", "hh_api", "camoufox"):
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


# Порт-маркер единственного экземпляра. Без него каждый повторный запуск
# .app (например, повторный клик по иконке, пока предыдущий процесс ещё жив —
# в том числе завис после сбоя) открывал ЕЩЁ ОДНО окно поверх старого,
# и создавалось впечатление, что при каждом старте появляется новое окно.
SINGLE_INSTANCE_PORT = 47821


def _wake_running_instance() -> bool:
    """True, если приложение уже запущено (порт-маркер занят) — уже
    работающему окну послано «покажись», а этот процесс должен просто выйти,
    не открывая своего окна."""
    try:
        with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=0.5) as s:
            s.sendall(b"show")
        return True
    except OSError:
        return False


def _listen_for_second_launch(bridge):
    """Слушает порт-маркер и по любому подключению поднимает окно наверх —
    см. _wake_running_instance()."""
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        server.listen(1)
    except OSError:
        return  # порт занят чем-то посторонним — не критично, просто без будильника

    def loop():
        while True:
            try:
                conn, _ = server.accept()
                conn.close()
                if bridge.window:
                    bridge.window.show()
            except Exception:
                break

    threading.Thread(target=loop, daemon=True, name="single-instance-listener").start()


def main():
    if os.environ.get("HHAGENT_SELFTEST") or "--selftest" in sys.argv:
        sys.exit(selftest())

    if _wake_running_instance():
        print("ℹ️ Приложение уже запущено — показываю существующее окно вместо нового.")
        return

    # CLI (main.py) делает это первой строкой; здесь его не было вовсе —
    # agent.db создавался только у тех, кто хоть раз запускал main.py
    # руками. У остальных первое обращение к applied_jobs падало с
    # "no such table" прямо в середине первого поиска.
    from database import init_db
    init_db()

    bridge = AgentBridge()
    window = webview.create_window(
        "AbuHH",
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
    _listen_for_second_launch(bridge)

    # Перехватываем вывод агента, чтобы он был виден в окне
    bridge._stdout_backup = sys.stdout
    sys.stdout = LogTee(sys.stdout, lambda line: bridge._log(line))
    # stderr раньше не перехватывался вовсе — необработанный traceback не
    # попадал ни в файл, ни в окно, и о зависании/падении было не узнать
    # иначе как через Console.app. _log_stderr пишет его в файл как error,
    # в окно — как warn (без этого один traceback красит весь журнал).
    bridge._stderr_backup = sys.stderr
    sys.stderr = LogTee(sys.stderr, lambda line: bridge._log_stderr(line))

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
        sys.stderr = bridge._stderr_backup
        if bridge.running and bridge.loop:
            bridge.loop.call_soon_threadsafe(control.request_stop)


if __name__ == "__main__":
    main()
