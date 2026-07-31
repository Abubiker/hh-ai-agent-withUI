"""Файловый журнал работы агента — переживает закрытие/падение окна.

Единая точка: UI-панель «журнал» и весь print() уже стекаются в
AgentBridge._log() (см. ui_app.py), а она теперь дублирует каждую строку
сюда. Так детальность лога в файле = детальности того, что человек видит
в интерфейсе, без отдельной инструментации hh_client.py и т.п.

Два правила для файла (в отличие от UI-панели, которая живёт только на
время сеанса):
  1. Новый файл на каждый запуск приложения, а не один растущий agent.log —
     чтобы у конкретного сеанса (в т.ч. который завис) были свои логи,
     а не хвост чужого запуска. Старые сеансы подчищаются, чтобы папка не
     росла бесконечно.
  2. Секреты (API-ключи, токен телеграм-бота) вычищаются перед записью —
     файл может быть отправлен разработчику для разбора зависания.
"""
import logging
import logging.handlers
import os
import re
import sys
import time
import traceback

from settings import data_dir

LOG_DIR = data_dir() / "logs"

# Сколько последних файлов сеансов хранить — старше удаляются при старте.
MAX_SESSION_LOGS = 20

# Имя файла фиксируется в момент импорта модуля = в момент старта приложения.
LOG_FILE = LOG_DIR / f"agent-{time.strftime('%Y%m%d-%H%M%S')}.log"

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_logger = logging.getLogger("hhagent")
_logger.setLevel(logging.DEBUG)

# Кадры обвязки, которые origin() пропускает, поднимаясь по стеку — не
# настоящий вызывающий код, а внутренняя механика доставки строки в лог.
# Файлы целиком: ui_app.py даёт всю цепочку print → LogTee.write → _log →
# log, applog.py — сам себя (log/exc/origin). Отдельные имена функций — на
# случай, если из hh_client.py однажды вызовут log()/exc() не напрямую,
# а через свою обёртку с тем же именем в другом файле.
_SKIP_FILES = {"applog.py", "ui_app.py"}
_SKIP_FUNCS = {"log", "_log", "_log_stderr", "write", "flush", "_trace", "exc"}


def origin(depth: int = 1, max_hops: int = 12) -> str:
    """"модуль.функция:строка" настоящего вызывающего кода — для колонки
    origin в файловом логе.

    Почему не stacklevel=: у него фиксированная глубина, а обёртка разная —
    print → LogTee.write → _log → log (4 кадра) против прямого applog.log()
    из hh_client._trace (2 кадра). Поэтому вместо счёта кадров поднимаемся
    по sys._getframe().f_back, пропуская кадры из _SKIP_FILES/_SKIP_FUNCS,
    пока не найдём первый кадр настоящего кода.
    """
    try:
        frame = sys._getframe(depth)
    except ValueError:
        return "-"
    hops = 0
    while frame is not None and hops < max_hops:
        filename = os.path.basename(frame.f_code.co_filename)
        funcname = frame.f_code.co_name
        if filename not in _SKIP_FILES and funcname not in _SKIP_FUNCS:
            module = filename[:-3] if filename.endswith(".py") else filename
            return f"{module}.{funcname}:{frame.f_lineno}"
        frame = frame.f_back
        hops += 1
    return "-"


class _OriginFormatter(logging.Formatter):
    """Записи, пришедшие в обход log()/exc() (в теории — не бывает в этом
    модуле, но formatter обязан быть готов), не должны ронять всю строку
    KeyError'ом на отсутствующем %(origin)s."""

    def format(self, record):
        if not hasattr(record, "origin"):
            record.origin = "-"
        return super().format(record)

# Типичные форматы токенов — на случай ключа, который приложение само не
# хранит (например, провайдер, добавленный вручную через переменные окружения).
# Известные секреты из secrets.json гасятся по значению в _redact() отдельно —
# это точнее, но не покрывает ключи, о которых settings ничего не знает.
_GENERIC_SECRET_RE = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{10,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[A-Za-z0-9_-]{20,}"
    r"|Bearer\s+[A-Za-z0-9._-]{10,}"
    r"|\d{8,10}:[A-Za-z0-9_-]{30,}"  # токен телеграм-бота вида 123456:AA...
)

_secret_cache = {"values": (), "at": 0.0}
_SECRET_CACHE_TTL = 5.0  # секунды — не перечитывать secrets.json на каждую строку


def _known_secret_values():
    """Значения секретов, реально сохранённых пользователем (любых, не
    только по известным именам) — гасить их по точному значению надёжнее,
    чем угадывать формат."""
    now = time.monotonic()
    if now - _secret_cache["at"] > _SECRET_CACHE_TTL:
        try:
            from settings import settings
            _secret_cache["values"] = tuple(
                v for v in settings._read_secrets_file().values() if v)
        except Exception:
            _secret_cache["values"] = ()
        _secret_cache["at"] = now
    return _secret_cache["values"]


def _redact(line: str) -> str:
    for value in _known_secret_values():
        if len(value) >= 6:  # короче не трогаем — риск случайных совпадений
            line = line.replace(value, "[REDACTED]")
    return _GENERIC_SECRET_RE.sub("[REDACTED]", line)


def _prune_old_sessions():
    try:
        files = sorted(LOG_DIR.glob("agent-*.log*"))
    except Exception:
        return
    if len(files) <= MAX_SESSION_LOGS:
        return
    for f in files[:-MAX_SESSION_LOGS]:
        try:
            f.unlink()
        except Exception:
            pass


def _ensure_handler():
    if _logger.handlers:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _prune_old_sessions()
    # backupCount=2: подстраховка на случай гигантского единичного сеанса,
    # а не основной механизм ротации — тот теперь per-launch (см. LOG_FILE).
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(_OriginFormatter(
        "%(asctime)s %(levelname)-5s %(origin)-38s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(handler)


_infra_failed = False  # чтобы «лог сломался» написать раз, а не на каждую строку


def log(line: str, level: str = "info", origin_hint: str | None = None):
    """Пишет строку в agent-<сеанс>.log. Не должна кидать исключений наружу —
    вызывается из UI-потока на каждый print/_log, лог не должен уронить их.

    origin_hint — готовый "модуль.функция:строка", если вызывающий код (см.
    ui_app.py._log) уже посчитал его сам для события в UI: иначе пришлось
    бы обходить кадры дважды на одну и ту же строку."""
    global _infra_failed
    try:
        _ensure_handler()
        o = origin_hint if origin_hint is not None else origin(depth=2)
        _logger.log(_LEVELS.get(level, logging.INFO), _redact(line),
                    extra={"origin": o})
    except Exception as e:
        if not _infra_failed:
            _infra_failed = True
            try:
                sys.__stdout__.write(f"[applog] файловый лог недоступен, дальше пишем молча: {e}\n")
                sys.__stdout__.flush()
            except Exception:
                pass


def exc(note: str = ""):
    """Трейсбек текущего исключения — только в файл, уровень debug (в UI не
    всплывает никогда: один traceback на 10+ строк залил бы журнал целиком).
    Звать сразу внутри except, пока sys.exc_info() ещё не очищен."""
    try:
        _ensure_handler()
        tb = traceback.format_exc()
        msg = f"{note}\n{tb}" if note else tb
        _logger.log(logging.DEBUG, _redact(msg), extra={"origin": origin(depth=2)})
    except Exception:
        pass
