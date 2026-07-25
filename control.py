"""Управление временем работы и остановкой агента.

Хранит выбранную длительность сеанса и общий флаг остановки. Используется
и главным циклом (main.py), и обходом вакансий (hh_client.py), и командой
/stop из Telegram (tg_bot.py), чтобы агент мог корректно завершиться:
по истечении времени, по команде или по Ctrl+C.
"""
import asyncio
import time

# Флаг «пора останавливаться». Ставится командой /stop, из терминала или по таймеру.
stop_event = asyncio.Event()

# Пользоваться ли Telegram вообще. Если False — бот не запускается, соединений
# с api.telegram.org не будет совсем, а уведомления просто печатаются в консоль.
telegram_enabled = True


def set_telegram_enabled(value: bool):
    global telegram_enabled
    telegram_enabled = value

_chosen_seconds = None   # выбранная длительность в секундах (None = бессрочно)
_deadline = None         # момент времени (monotonic), когда пора стоп


def configure(seconds):
    """Запомнить выбранную длительность (None — работать бессрочно)."""
    global _chosen_seconds
    _chosen_seconds = seconds


def arm():
    """Запустить отсчёт времени. Вызывается после успешного логина,
    чтобы ручной вход в аккаунт не съедал отведённое время."""
    global _deadline
    _deadline = (time.monotonic() + _chosen_seconds) if _chosen_seconds else None


def should_stop() -> bool:
    if stop_event.is_set():
        return True
    if _deadline is not None and time.monotonic() >= _deadline:
        return True
    return False


def request_stop():
    stop_event.set()


def duration_text() -> str:
    if _chosen_seconds is None:
        return "Режим: бессрочно (до /stop или Ctrl+C)."
    mins = _chosen_seconds // 60
    return f"Режим: {mins} мин, затем автоостановка."


async def sleep_or_stop(seconds: float):
    """Пауза, которую можно прервать остановкой. Не спит дольше, чем осталось
    до дедлайна."""
    if _deadline is not None:
        seconds = min(seconds, max(0.0, _deadline - time.monotonic()))
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
