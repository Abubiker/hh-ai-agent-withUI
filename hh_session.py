"""Единая точка запуска браузера поверх сохранённой сессии площадки HH.

До этого модуля один и тот же блок (запустить браузер, подцепить
state.json) был продублирован трижды — в HHClient.start(),
resume_reader.fetch_resume_text() и quick_apply._open_context(). Здесь он
один.

Браузер — Camoufox (антидетект-сборка Firefox, см. https://camoufox.com):
подставляет реалистичный отпечаток сам, поэтому здесь нет ни user-agent'а,
ни playwright_stealth — оба были нужны только для голого Chromium.

HHClient — долгоживущий объект (держит браузер открытым весь сеанс поиска),
поэтому ему нужны сырые playwright/browser/context, которые он сам закроет
в close(). Разовым чтениям (resume_reader, quick_apply, resume_stats) удобнее
контекст-менеджер one_shot(), закрывающий всё сам.
"""
import os
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

import sites as _sites


class SessionError(RuntimeError):
    """Сообщение уже человекочитаемое — можно показывать пользователю как есть."""


async def open_session(site: dict | None = None, *, headless: bool = True,
                        require_login: bool = True):
    """Возвращает (playwright, browser, context).

    Вызывающий отвечает за закрытие в обратном порядке: context, browser,
    playwright — так удобнее HHClient, который держит их как self.* на весь
    сеанс поиска. Для разового использования см. one_shot() ниже.
    """
    site = site or _sites.active_site()
    state_path = _sites.state_file(site)
    if require_login and not os.path.exists(state_path):
        raise SessionError(
            f"Вы ещё не входили в аккаунт {site['name']}. Запустите агента "
            "один раз, чтобы приложение запомнило вход.")

    playwright = await async_playwright().start()
    # persistent_context=False — получаем обычный Playwright Browser, на
    # котором storage_state работает как у любого другого движка.
    from camoufox.async_api import AsyncNewBrowser
    browser = await AsyncNewBrowser(playwright, headless=headless, humanize=True,
                                     persistent_context=False)
    kwargs = {}
    if os.path.exists(state_path):
        kwargs["storage_state"] = state_path
    context = await browser.new_context(**kwargs)
    return playwright, browser, context


@asynccontextmanager
async def one_shot(site: dict | None = None, *, headless: bool = True,
                    require_login: bool = True):
    """Контекст-менеджер для разовых запусков: fetch_vacancy, apply_to_vacancy,
    fetch_resume_text, resume_stats.fetch_snapshots. Гарантированно закрывает
    браузер и playwright даже при исключении внутри блока."""
    playwright, browser, context = await open_session(
        site, headless=headless, require_login=require_login)
    try:
        yield context
    finally:
        await context.close()
        await browser.close()
        await playwright.stop()


async def new_stealth_page(context):
    """Имя сохранено ради обратной совместимости вызовов (hh_client.py и
    др.) — маскировка теперь встроена в сам Camoufox, отдельного шага не
    требуется."""
    return await context.new_page()
