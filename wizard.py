"""Мастер настройки для терминала.

Оконная версия ведёт человека по чек-листу готовности; в консоли такого не
было — приходилось править settings.json руками, а до этого config.py.
Здесь те же шаги вопросами: модель, резюме, запросы, уведомления.

Пишет в тот же settings.json, что и окно, поэтому настроить можно где
удобнее, а запускать — где угодно.

    python wizard.py          # полный проход
    python wizard.py --model  # только выбор модели
"""
import asyncio
import sys

from settings import settings, DEFAULT_EXCLUSIONS
from llm_providers import OPENAI_PRESETS, get_provider, ProviderError

DIM, BOLD, OK, WARN, OFF = "\033[2m", "\033[1m", "\033[32m", "\033[33m", "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty() and sys.platform != "win32"


def c(text: str, code: str) -> str:
    return f"{code}{text}{OFF}" if _supports_color() else text


def head(title: str, step: int | None = None, total: int | None = None):
    prefix = f"[{step}/{total}] " if step else ""
    print(f"\n{c(prefix + title, BOLD)}")
    print(c("─" * (len(prefix) + len(title)), DIM))


async def ask(prompt: str, default: str = "") -> str:
    """input() в отдельном потоке: мастер живёт в асинхронном коде."""
    hint = f" [{default}]" if default else ""
    raw = (await asyncio.to_thread(input, f"{prompt}{hint}: ")).strip()
    return raw or default


async def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    """Число в заданных границах. Опечатка не должна ронять мастер посреди
    настройки — переспрашиваем, пока не получим осмысленный ответ."""
    while True:
        raw = await ask(prompt, str(default))
        try:
            n = int(raw)
        except ValueError:
            print(c(f"  Нужно число от {lo} до {hi}.", WARN))
            continue
        if lo <= n <= hi:
            return n
        print(c(f"  Допустимо от {lo} до {hi}.", WARN))


async def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    raw = (await asyncio.to_thread(input, f"{prompt} [{d}]: ")).strip().lower()
    if not raw:
        return default
    return raw[0] in ("y", "д", "1")


async def ask_multiline(prompt: str, current: str = "") -> str:
    """Многострочный ввод: резюме в одну строку не помещается."""
    if current:
        print(c(f"  сейчас: {len(current)} символов, первая строка — "
                f"{current.strip().splitlines()[0][:60]}…", DIM))
        if not await ask_yes("  Заменить?", default=False):
            return current
    print(f"{prompt}")
    print(c("  Вводите текст. Пустая строка дважды подряд — закончить.", DIM))
    lines, blanks = [], 0
    while True:
        line = await asyncio.to_thread(input, "  ")
        if line.strip():
            blanks = 0
            lines.append(line)
        else:
            blanks += 1
            if blanks >= 2 or (lines and blanks == 1 and not lines[-1].strip()):
                break
            lines.append("")
    return "\n".join(lines).strip()


async def choose(prompt: str, options: list[tuple[str, str]], default: int = 1) -> int:
    """Пронумерованный выбор. Возвращает индекс с нуля."""
    for i, (label, note) in enumerate(options, 1):
        tail = c(f" — {note}", DIM) if note else ""
        print(f"  {i}) {label}{tail}")
    return await ask_int(f"{prompt} [1-{len(options)}]",
                         default, 1, len(options)) - 1


# ---------------------------------------------------------------- шаги


async def step_model(step=None, total=None):
    head("Модель, которая читает вакансии и пишет письма", step, total)
    llm = settings.data["llm"]
    idx = await choose("Чем пользуемся?", [
        ("Локальная (Ollama)", "бесплатно, нужно 16 ГБ памяти"),
        ("OpenAI-совместимый сервис", "OpenRouter, Mistral, Groq, LM Studio…"),
        ("Anthropic", "по ключу"),
    ], default={"ollama": 1, "openai_compat": 2, "anthropic": 3}.get(
        llm.get("provider"), 1))

    if idx == 0:
        llm["provider"] = "ollama"
        llm["ollama_url"] = await ask("Адрес сервера Ollama", llm["ollama_url"])
        llm["ollama_model"] = await ask("Модель", llm["ollama_model"])

    elif idx == 1:
        llm["provider"] = "openai_compat"
        opts = [(name, note) for name, _, note in OPENAI_PRESETS] + [("Свой адрес", "")]
        p = await choose("Сервис", opts)
        llm["openai_base_url"] = (OPENAI_PRESETS[p][1] if p < len(OPENAI_PRESETS)
                                  else await ask("Базовый адрес", llm["openai_base_url"]))
        key = await ask("API-ключ (пусто — оставить прежний)")
        if key:
            settings.set_secret("openai_api_key", key)
        settings.save()  # ключ нужен провайдеру уже сейчас, для списка моделей
        llm["openai_model"] = await _pick_model(llm.get("openai_model", ""))

    else:
        llm["provider"] = "anthropic"
        key = await ask("API-ключ Anthropic (пусто — оставить прежний)")
        if key:
            settings.set_secret("anthropic_api_key", key)
        llm["anthropic_model"] = await ask("Модель", llm["anthropic_model"])

    settings.save()
    await _check_provider()


async def _pick_model(current: str) -> str:
    """Показывает модели, которые отдаёт сам сервис. Список бывает длинным,
    поэтому есть поиск по подстроке."""
    print(c("  Запрашиваю список моделей…", DIM))
    try:
        models = await get_provider("openai_compat").list_models()
    except Exception as e:
        print(c(f"  Не удалось получить список ({e}).", WARN))
        return await ask("Название модели", current)

    if not models:
        return await ask("Название модели", current)

    print(f"  Доступно моделей: {len(models)}")
    while True:
        q = await ask("Поиск по названию (пусто — показать первые 20)")
        hits = [m for m in models if q.lower() in m.lower()] if q else models
        if not hits:
            print(c("  Ничего не найдено.", WARN))
            continue
        for i, m in enumerate(hits[:20], 1):
            print(f"  {i:2}) {m}")
        if len(hits) > 20:
            print(c(f"  …ещё {len(hits) - 20}. Уточните поиск.", DIM))
        raw = await ask("Номер модели (или пустая строка — искать заново)", "")
        if raw.isdigit() and 1 <= int(raw) <= min(len(hits), 20):
            chosen = hits[int(raw) - 1]
            print(c(f"  Выбрана {chosen}", OK))
            return chosen


async def _check_provider():
    print(c("  Проверяю связь…", DIM))
    try:
        ok, msg = await get_provider().health()
    except ProviderError as e:
        ok, msg = False, str(e)
    print(("  " + c("✅ ", OK) if ok else "  " + c("⚠️ ", WARN)) + msg)


async def step_resume(step=None, total=None):
    head("Резюме", step, total)
    r = settings.data["resume"]
    print(c("  Название должно посимвольно совпадать с заголовком резюме "
            "на hh.ru — по нему агент выбирает, чем откликаться.", DIM))
    r["target_name"] = await ask("Название резюме", r["target_name"])
    r["summary"] = await ask_multiline(
        "  Профиль для писем — опыт, стек, достижения.\n"
        "  Модель пишет строго по этому тексту и ничего не выдумывает.",
        r["summary"])
    settings.save()


async def step_search(step=None, total=None):
    head("Что и где искать", step, total)
    s = settings.data["search"]
    print(c(f"  Сейчас запросы: {', '.join(s['queries']) or '—'}", DIM))
    raw = await ask("Запросы через запятую (пусто — оставить)")
    if raw:
        s["queries"] = [q.strip() for q in raw.split(",") if q.strip()]

    print(c(f"  Регионы: {', '.join(r['name'] for r in s['regions']) or '—'}", DIM))
    print(c("  Регионы удобнее править в окне приложения — там справочник "
            "hh.ru с поиском.", DIM))

    s["max_pages_per_query"] = await ask_int(
        "Страниц выдачи на запрос", s["max_pages_per_query"], 1, 10)
    settings.data["schedule"]["cycle_pause_minutes"] = await ask_int(
        "Пауза между проверками, минут",
        settings.data["schedule"]["cycle_pause_minutes"], 3, 240)

    if await ask_yes("Настроить, когда отклонять вакансию?", default=False):
        s["exclusions"] = await ask_multiline(
            "  Причины для отказа, по одной на строку.",
            s.get("exclusions") or DEFAULT_EXCLUSIONS)
    settings.save()


async def step_notify(step=None, total=None):
    head("Уведомления", step, total)
    n = settings.data["notifications"]
    n["desktop_enabled"] = await ask_yes("Показывать уведомления на рабочем столе?",
                                         n.get("desktop_enabled", True))
    n["telegram_enabled"] = await ask_yes("Присылать отчёты в Telegram?",
                                          n.get("telegram_enabled", False))
    if n["telegram_enabled"]:
        n["tg_user_id"] = await ask("Ваш Telegram user id", n.get("tg_user_id", ""))
        token = await ask("Токен бота (пусто — оставить прежний)")
        if token:
            settings.set_secret("tg_bot_token", token)
    settings.save()


async def step_ready(step=None, total=None):
    head("Готовность", step, total)
    import first_run
    st = await first_run.status()
    rows = [
        ("Браузер для Playwright", st["browser"],
         "поставьте: playwright install chromium"),
        ("Модель отвечает", st["model_ready"], st.get("model_note", "")),
        ("Вход в аккаунт hh.ru", st["logged_in"],
         "выполнится при первом запуске — откроется окно браузера"),
        ("Название резюме", st["resume"], "шаг «Резюме»"),
        ("Профиль для писем", st["summary"], "шаг «Резюме»"),
    ]
    for label, good, hint in rows:
        mark = c("✅", OK) if good else c("•", WARN)
        tail = "" if good else c(f"  — {hint}", DIM)
        print(f"  {mark} {label}{tail}")
    done = sum(1 for _, g, _ in rows if g)
    print(f"\n  Готово {done} из {len(rows)}.")
    return done == len(rows)


# ---------------------------------------------------------------- вход


async def run(only: str | None = None) -> bool:
    """Полный проход или один шаг. Возвращает готовность к запуску."""
    steps = {
        "model": step_model, "resume": step_resume,
        "search": step_search, "notify": step_notify,
    }
    if only:
        if only not in steps:
            print(f"Неизвестный шаг: {only}. Доступны: {', '.join(steps)}")
            return False
        await steps[only]()
        return await step_ready()

    print(c("\nНастройка HH Agent", BOLD))
    print(c(f"Всё сохраняется в {settings.path}", DIM))
    print(c("Enter оставляет значение в скобках без изменений.", DIM))
    total = len(steps)
    for i, fn in enumerate(steps.values(), 1):
        await fn(i, total)
    ready = await step_ready()
    print(c("\nГотово. Запуск: python main.py", BOLD) if ready else
          c("\nОстались незакрытые пункты — их можно добить и позже.", WARN))
    return ready


def main():
    only = None
    for arg in sys.argv[1:]:
        if arg.startswith("--"):
            only = arg[2:]
    try:
        asyncio.run(run(only))
    except (KeyboardInterrupt, EOFError):
        print("\nПрервано. Что успели ответить — сохранено.")


if __name__ == "__main__":
    main()
