import asyncio
import signal
import sys
from database import init_db
from tg_bot import start_bot, shutdown_bot
from notify_sinks import build_sinks
from hh_client import HHClient
from settings import settings
import control

# Варианты длительности сеанса: ключ ввода -> (секунды, подпись)
DURATION_OPTIONS = {
    "1": (900, "15 минут"),
    "2": (1800, "30 минут"),
    "3": (3600, "1 час"),
    "4": (None, "бессрочно"),
}


async def ask_duration():
    """Спрашивает у пользователя, сколько времени работать агенту."""
    print("\nНа сколько запустить агента?")
    print("  1) 15 минут")
    print("  2) 30 минут")
    print("  3) 1 час")
    print("  4) Бессрочно")
    print("  (остановить можно в любой момент — см. подсказку после запуска)")
    choice = (await asyncio.to_thread(input, "Ваш выбор [1-4, по умолчанию 4]: ")).strip()
    seconds, label = DURATION_OPTIONS.get(choice, (None, "бессрочно"))
    print(f"➡️ Режим работы: {label}.")
    control.configure(seconds)


async def ask_telegram():
    """Спрашивает, слать ли отчёты в Telegram. При отказе к Telegram
    не будет вообще никаких обращений."""
    answer = (await asyncio.to_thread(
        input, "\nПрисылать отчёты в Telegram? [Y/n]: ")).strip().lower()
    enabled = answer not in ("n", "no", "н", "нет", "0")
    control.set_telegram_enabled(enabled)
    if enabled:
        print("➡️ Telegram: включён (команда /stop в боте тоже работает).")
    else:
        print("➡️ Telegram: выключен — работаю молча, отчёты в консоль.")


def _install_terminal_stop(loop):
    """Остановка по вводу в терминале: работает всегда, в том числе без Telegram
    и независимо от выбранной длительности сеанса."""
    def on_stdin():
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if not line:
            return
        if line.strip().lower() in ("stop", "стоп", "s", "q", "quit", "exit"):
            print("🛑 Останавливаюсь по команде из терминала — соберу статистику...")
            control.request_stop()
    try:
        loop.add_reader(sys.stdin.fileno(), on_stdin)
        return True
    except Exception:
        return False  # не в интерактивном терминале — обойдёмся Ctrl+C


async def finish(client):
    """Печатает итоговую статистику (всегда) и рассылает её получателям."""
    # Синхронный print выполняется даже во время отмены задачи (Ctrl+C).
    print("\n" + client.stats.summary_plain())
    try:
        await client.sinks.notify(client.stats.summary(), kind="summary")
    except Exception:
        pass
    try:
        await client.sinks.close()
    except Exception:
        pass
    try:
        await client.stop()
    except Exception:
        pass


async def agent_loop():
    # Консольный запуск: строки идут в консоль, уведомления — на рабочий стол,
    # Telegram — если пользователь его включил.
    sinks = build_sinks(telegram=control.telegram_enabled, desktop=True)
    client = HHClient(sinks=sinks)
    await client.start()

    # Первая авторизация (на первом запуске — ручной вход в браузере)
    logged_in = await client.login_if_needed()
    if not logged_in:
        print("Не удалось авторизоваться. Завершение работы.")
        await client.stop()
        return

    # Отсчёт времени стартует только сейчас, чтобы ручной логин не съедал лимит
    control.arm()

    # Слушатель терминала ставим ПОСЛЕ логина: во время входа скрипт сам читает
    # stdin (ждёт Enter), и два читателя конфликтовали бы.
    has_reader = _install_terminal_stop(asyncio.get_running_loop())
    print("\n" + "=" * 46)
    print("⏹️  КАК ОСТАНОВИТЬ В ЛЮБОЙ МОМЕНТ:")
    if has_reader:
        print("   • напишите  stop  и нажмите Enter")
    print("   • или нажмите Ctrl+C")
    if control.telegram_enabled:
        print("   • или команда /stop в Telegram-боте")
    print("=" * 46 + "\n")

    await client.sinks.notify("🤖 ИИ-агент запущен и начал работу!\n" + control.duration_text())

    try:
        import time as _time
        while not control.should_stop():
            fresh_before = client.stats.fresh
            try:
                await client.search_and_apply(client.sinks.notify)
                await client.check_chats(client.sinks.notify)
            except Exception as e:
                print(f"Ошибка в основном цикле агента: {e}")

            if control.should_stop():
                break

            pause = settings.cycle_pause_minutes
            next_at = _time.strftime("%H:%M", _time.localtime(_time.time() + pause * 60))
            if client.stats.fresh == fresh_before:
                print(f"Новых вакансий не появилось. Следующая проверка в {next_at} "
                      f"(остановить можно в любой момент).")
            else:
                print(f"Проверка закончена: новых вакансий "
                      f"{client.stats.fresh - fresh_before}. Следующая в {next_at}.")
            await control.sleep_or_stop(pause * 60)
    finally:
        await finish(client)


def _install_sigint_handler(loop):
    """Первый Ctrl+C — мягкая остановка со сбором статистики. Повторный — обычное
    прерывание. Так браузер закрывается корректно, а не обрывается на полуслове."""
    def handler():
        print("\n🛑 Ctrl+C: останавливаюсь мягко, соберу статистику. "
              "(нажмите ещё раз для принудительного выхода)")
        control.request_stop()
        try:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.default_int_handler)
        except Exception:
            pass
    try:
        loop.add_signal_handler(signal.SIGINT, handler)
    except NotImplementedError:
        pass  # на некоторых платформах add_signal_handler недоступен


async def ensure_configured() -> bool:
    """Не даём запуститься вслепую.

    Без названия резюме и профиля агент отработает вхолостую: письма писать
    не из чего, а резюме для отклика не выбрать. Раньше это выяснялось уже
    в процессе, отдельными ошибками по каждой вакансии.
    """
    import wizard

    if settings.target_resume_name and settings.resume_summary.strip():
        return True

    print("\n⚙️ Похоже, агент ещё не настроен: нет названия резюме "
          "или профиля для писем.")
    if not await wizard.ask_yes("Пройти настройку сейчас?", default=True):
        print("Настроить можно в любой момент: python wizard.py")
        return False
    await wizard.run()
    return bool(settings.target_resume_name and settings.resume_summary.strip())


async def main():
    init_db()
    print("Инициализация завершена.")

    if not await ensure_configured():
        return

    await ask_telegram()
    await ask_duration()
    _install_sigint_handler(asyncio.get_running_loop())

    # Бот поднимается только если Telegram включён — иначе к api.telegram.org
    # не будет ни одного обращения.
    bot_task = asyncio.create_task(start_bot()) if control.telegram_enabled else None
    try:
        await agent_loop()
    finally:
        try:
            asyncio.get_running_loop().remove_reader(sys.stdin.fileno())
        except Exception:
            pass
        if bot_task:
            # Ограничиваем ожидание: aiogram держит длинный long-polling запрос к Telegram
            # (до 30 сек), и безусловный await делал остановку «зависшей».
            await shutdown_bot(bot_task)
    print("Работа завершена.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Принудительная остановка.")
