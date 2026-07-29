"""Разовое чтение резюме с hh.ru через уже сохранённую сессию Playwright.

Не связано с HHClient: чат должен работать и тогда, когда агент не запущен
(поиск не идёт, HHClient.start() не вызывался). Поэтому здесь — свой,
независимый разовый запуск браузера с той же сохранённой сессией (state.json),
а не переиспользование живого HHClient.

Работает только на чтение: ничего не сохраняет и не отправляет на hh.ru.
"""
import os
import re

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from hh_client import STATE_FILE, handle_vpn_check, diagnose_page

RESUME_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?hh\.ru/resume/[a-f0-9]+[^\s]*", re.IGNORECASE)

# Резюме — длинный текст, но не бесконечный: ограничиваем, чтобы не раздувать
# контекст модели поверх системного промпта, профиля пользователя и истории.
MAX_RESUME_CHARS = 8000


class ResumeReadError(RuntimeError):
    """Сообщение уже человекочитаемое — можно показывать пользователю как есть."""


def find_resume_url(text: str) -> str | None:
    """Ищет в сообщении пользователя ссылку на его резюме на hh.ru."""
    m = RESUME_URL_RE.search(text or "")
    return m.group(0) if m else None


async def fetch_resume_text(url: str, timeout_ms: int = 30000) -> str:
    # find_resume_url() намеренно ловит и ссылки без схемы (люди часто
    # вставляют "hh.ru/resume/..." как есть) — Playwright такой адрес не
    # откроет, поэтому достраиваем протокол здесь, а не в самом регэкспе.
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url

    if not os.path.exists(STATE_FILE):
        raise ResumeReadError(
            "Сначала войдите в аккаунт hh.ru — запустите агента один раз "
            "(вкладка «Работа» → «Запустить»), чтобы приложение запомнило вход.")

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(headless=True)
        try:
            user_agent = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/142.0.0.0 Safari/537.36")
            context = await browser.new_context(storage_state=STATE_FILE,
                                                 user_agent=user_agent)
            try:
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                # Как и в login_if_needed/check_chats: не networkidle — hh.ru
                # держит вебсокеты и аналитику, событие может не наступить никогда.
                await handle_vpn_check(page)
                await page.wait_for_timeout(2000)

                code, reason = await diagnose_page(page, kind="resume")
                if code == "captcha":
                    raise ResumeReadError(
                        "hh.ru запросил капчу при открытии резюме — попробуйте ещё раз через минуту.")
                if code == "vpn_check":
                    raise ResumeReadError(
                        "hh.ru показал проверку VPN, и её не удалось пройти автоматически.")
                # archived/not_found/redirect/unknown для резюме не надёжны:
                # маркеры и финальная проверка URL в diagnose_page заточены под
                # вакансии. Не считаем их фатальными — просто пробуем достать
                # текст и проверяем его длину как признак того, что страница реальна.

                text = (await page.locator("body").inner_text(timeout=10000)).strip()
                if len(text) < 200:
                    raise ResumeReadError(
                        "Страница резюме открылась, но текста почти нет — "
                        "возможно, это не та ссылка или резюме скрыто.")
                return text[:MAX_RESUME_CHARS]
            finally:
                await context.close()
        finally:
            await browser.close()
    finally:
        await playwright.stop()
