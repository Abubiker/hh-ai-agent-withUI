"""Куда агент сообщает о событиях: консоль, Telegram, нативные уведомления, окно.

Раньше всё было прибито к Telegram: без него не приходили ни отчёты, ни капча.
Здесь несколько получателей работают одновременно, и любой из них может
отсутствовать.

Отдельная роль — «решатель капчи». Уведомление на рабочем столе может только
привлечь внимание, а ввести текст можно из Telegram или из окна приложения.
Группа опрашивает получателей и берёт первый пришедший ответ.
"""
import asyncio
import re
import sys

HTML_TAG = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Telegram понимает HTML-разметку, консоль и уведомления — нет."""
    return HTML_TAG.sub("", text)


class Sink:
    name = "base"
    can_solve_captcha = False

    async def notify(self, text: str, level: str = "info"):
        raise NotImplementedError

    async def solve_captcha(self, image_path: str, prompt: str) -> str | None:
        """Вернуть введённый пользователем текст или None, если этот
        получатель решать капчу не умеет."""
        return None

    async def close(self):
        pass


class ConsoleSink(Sink):
    name = "console"

    async def notify(self, text: str, level: str = "info"):
        prefix = {"info": "[отчёт]", "warn": "[!]", "error": "[ошибка]"}.get(level, "[отчёт]")
        print(f"{prefix} {strip_html(text)}")


class DesktopSink(Sink):
    """Нативные уведомления macOS/Windows/Linux.

    ВАЖНО про macOS: Notification Center принимает уведомления только от
    подписанного .app. При запуске обычным скриптом библиотека молча ничего
    не показывает — поэтому предупреждаем об этом один раз, чтобы это не
    выглядело как поломка.
    """
    name = "desktop"

    def __init__(self, app_name: str = "AbuHH"):
        self._notifier = None
        self._warned = False
        self.app_name = app_name

    def _available(self) -> tuple[bool, str]:
        if sys.platform.startswith("linux"):
            # Уведомления идут через D-Bus сессии рабочего стола. По ssh или
            # в контейнере её нет, и desktop-notifier роняет трейсбек изнутри
            # чужой корутины — своим try/except его уже не поймать.
            import os
            if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
                return False, ("нет сессии D-Bus — так бывает при запуске "
                               "по ssh или в контейнере")
            return True, ""
        if sys.platform != "darwin":
            return True, ""
        try:
            from rubicon.objc import ObjCClass
            bundle = ObjCClass("NSBundle").mainBundle
            if bundle is None or bundle.bundleIdentifier is None:
                return False, ("уведомления macOS работают только из собранного "
                               "и подписанного приложения")
        except Exception:
            pass
        return True, ""

    def _get(self):
        if self._notifier is None:
            from desktop_notifier import DesktopNotifier
            self._notifier = DesktopNotifier(app_name=self.app_name)
        return self._notifier

    async def notify(self, text: str, level: str = "info"):
        ok, why = self._available()
        if not ok:
            if not self._warned:
                print(f"ℹ️ Уведомления на рабочем столе отключены: {why}.")
                self._warned = True
            return
        try:
            plain = strip_html(text)
            title, _, body = plain.partition("\n")
            await self._get().send(title=title[:120] or self.app_name,
                                   message=body.strip()[:500] or " ")
        except Exception as e:
            if not self._warned:
                print(f"⚠️ Не удалось показать уведомление: {e}")
                self._warned = True

    async def close(self):
        try:
            if self._notifier is not None:
                await self._notifier.clear_all()
        except Exception:
            pass


class TelegramSink(Sink):
    """Обёртка над существующим tg_bot. Умеет и сообщать, и принимать капчу."""
    name = "telegram"
    can_solve_captcha = True

    async def notify(self, text: str, level: str = "info"):
        import tg_bot
        await tg_bot.send_notification(text)

    async def solve_captcha(self, image_path: str, prompt: str) -> str | None:
        import tg_bot
        if not tg_bot.is_configured():
            return None
        try:
            await tg_bot.send_captcha_request(image_path, prompt)
            await asyncio.wait_for(tg_bot.captcha_event.wait(), timeout=300)
            return tg_bot.captcha_solution
        except asyncio.TimeoutError:
            print("⏭️ Ответ на капчу из Telegram не пришёл за 5 минут.")
            return None
        except Exception as e:
            print(f"⚠️ Ошибка при запросе капчи в Telegram: {e}")
            return None


class UISink(Sink):
    """Окно приложения. Логи уходят колбэком, капча показывается прямо в окне —
    это надёжнее, чем интерактивные ответы в уведомлениях macOS, которым нужен
    свой цикл событий на главном потоке (а его занимает интерфейс)."""
    name = "ui"
    can_solve_captcha = True

    def __init__(self, on_log=None, on_captcha=None):
        self.on_log = on_log
        self.on_captcha = on_captcha  # async (image_path, prompt) -> str | None

    async def notify(self, text: str, level: str = "info"):
        if self.on_log:
            try:
                self.on_log(strip_html(text), level)
            except Exception as e:
                print(f"⚠️ Не удалось отправить строку в интерфейс: {e}")

    async def solve_captcha(self, image_path: str, prompt: str) -> str | None:
        if not self.on_captcha:
            return None
        try:
            return await self.on_captcha(image_path, prompt)
        except Exception as e:
            print(f"⚠️ Ошибка ввода капчи в окне: {e}")
            return None


class SinkGroup(Sink):
    """Раздаёт событие всем получателям; сбой одного не мешает остальным."""
    name = "group"

    def __init__(self, sinks: list[Sink] | None = None):
        self.sinks = sinks or []

    @property
    def can_solve_captcha(self) -> bool:
        return any(s.can_solve_captcha for s in self.sinks)

    @staticmethod
    def _event_enabled(kind: str | None) -> bool:
        """Пользователь выбирает, о чём его беспокоить. Журнал в окне это
        не затрагивает — там видно всё."""
        if not kind:
            return True
        from settings import settings
        return bool(settings.data["notifications"].get("events", {}).get(kind, True))

    async def notify(self, text: str, level: str = "info", kind: str | None = None):
        allowed = self._event_enabled(kind)
        for sink in self.sinks:
            # Окно показывает всё: это журнал работы, а не уведомление.
            if not allowed and sink.name != "ui":
                continue
            try:
                await sink.notify(text, level)
            except Exception as e:
                print(f"⚠️ Получатель «{sink.name}» не принял сообщение: {e}")

    async def solve_captcha(self, image_path: str, prompt: str) -> str | None:
        """Спрашиваем тех, кто умеет принимать ввод, по очереди. Сначала окно
        приложения (быстрее всего), потом Telegram."""
        solvers = [s for s in self.sinks if s.can_solve_captcha]
        if not solvers:
            print("⏭️ Некому ввести капчу — пропускаю вакансию.")
            return None
        for sink in solvers:
            solution = await sink.solve_captcha(image_path, prompt)
            if solution:
                return solution
        return None

    async def close(self):
        for sink in self.sinks:
            try:
                await sink.close()
            except Exception:
                pass


def build_sinks(*, telegram: bool = False, desktop: bool = True,
                ui_log=None, ui_captcha=None) -> SinkGroup:
    """Собирает набор получателей под текущий режим запуска."""
    sinks: list[Sink] = []
    # Окно — первым: если приложение открыто, капчу удобнее ввести там.
    if ui_log or ui_captcha:
        sinks.append(UISink(on_log=ui_log, on_captcha=ui_captcha))
    else:
        sinks.append(ConsoleSink())
    if desktop:
        sinks.append(DesktopSink())
    if telegram:
        sinks.append(TelegramSink())
    return SinkGroup(sinks)
