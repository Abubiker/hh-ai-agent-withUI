"""Разовое чтение резюме с hh.ru через уже сохранённую сессию Playwright.

Не связано с HHClient: чат должен работать и тогда, когда агент не запущен
(поиск не идёт, HHClient.start() не вызывался). Поэтому здесь — свой,
независимый разовый запуск браузера с той же сохранённой сессией (state.json),
а не переиспользование живого HHClient.

Работает только на чтение: ничего не сохраняет и не отправляет на hh.ru.
"""
import re

import sites
import hh_session
from hh_client import handle_vpn_check, diagnose_page

RESUME_URL_RE = re.compile(
    rf"(?:https?://)?(?:www\.)?{sites.HOSTS_RE}/resume/[a-f0-9]+[^\s]*", re.IGNORECASE)

# Для дедупа ссылок на список резюме — на одной карточке резюме может быть
# несколько ссылок на него же (заголовок, «статистика» и т.п.).
RESUME_ID_RE = re.compile(r"/resume/([a-f0-9]+)", re.IGNORECASE)

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

    site = sites.detect(url) or sites.active_site()
    try:
        async with hh_session.one_shot(site, headless=True, require_login=True) as context:
            page = await hh_session.new_stealth_page(context)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Как и в login_if_needed/check_chats: не networkidle — hh
            # держит вебсокеты и аналитику, событие может не наступить никогда.
            await handle_vpn_check(page)
            await page.wait_for_timeout(2000)

            code, reason = await diagnose_page(page, kind="resume", host=site["host"])
            if code == "captcha":
                raise ResumeReadError(
                    f"{site['host']} запросил капчу при открытии резюме — попробуйте ещё раз через минуту.")
            if code == "vpn_check":
                raise ResumeReadError(
                    f"{site['host']} показал проверку VPN, и её не удалось пройти автоматически.")
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
    except hh_session.SessionError as e:
        raise ResumeReadError(str(e))


async def list_my_resumes(site=None, timeout_ms: int = 30000) -> list[dict]:
    """Резюме соискателя — [{title, url}], в порядке отображения на странице.

    Единственное место в проекте, читающее /applicant/resumes: до этого
    страницу со СПИСКОМ резюме код не открывал вовсе (только детект логина
    по ссылке на неё и чтение ОДНОГО резюме по уже известному URL). Разметка
    не проверялась — поэтому селектор нарочно широкий (тот же паттерн ссылок
    на резюме, что и везде в проекте, см. RESUME_URL_RE), а при нуле находок
    функция падает громко, а не молча возвращает пустой список: решать,
    что делать дальше (предложить ручной ввод), должен вызывающий код, а
    не эта функция притворяться, что резюме нет вообще.
    """
    site = site or sites.active_site()
    base = sites.base_url(site)
    try:
        async with hh_session.one_shot(site, headless=True, require_login=True) as context:
            page = await hh_session.new_stealth_page(context)
            await page.goto(f"{base}/applicant/resumes", wait_until="domcontentloaded",
                             timeout=timeout_ms)
            await handle_vpn_check(page)
            await page.wait_for_timeout(2000)

            links = await page.locator('a[href*="/resume/"]').all()
            titles: dict[str, str] = {}
            urls: dict[str, str] = {}
            order: list[str] = []
            for link in links:
                href = await link.get_attribute("href")
                if not href:
                    continue
                m = RESUME_ID_RE.search(href)
                if not m:
                    continue
                resume_id = m.group(1)
                if resume_id in titles:
                    continue  # дубль ссылки на то же резюме (заголовок + «статистика» и т.п.)
                text = (await link.inner_text()).strip()
                if not text:
                    continue
                titles[resume_id] = text
                urls[resume_id] = href if href.startswith("http") else f"{base}{href}"
                order.append(resume_id)

            resumes = [{"title": titles[rid], "url": urls[rid]} for rid in order]
            if resumes:
                return resumes

            code, reason = await diagnose_page(page, kind="resume", host=site["host"])
            if code == "captcha":
                raise ResumeReadError(
                    f"{site['host']} запросил капчу при открытии списка резюме — попробуйте ещё раз через минуту.")
            if code == "vpn_check":
                raise ResumeReadError(
                    f"{site['host']} показал проверку VPN, и её не удалось пройти автоматически.")
            raise ResumeReadError(
                "Не удалось найти резюме автоматически — впишите название и профиль вручную.")
    except hh_session.SessionError as e:
        raise ResumeReadError(str(e))
