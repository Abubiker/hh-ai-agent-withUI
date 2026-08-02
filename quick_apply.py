"""Разовый отклик на одну вакансию по вставленной в чат ссылке.

Не связано с HHClient — свой независимый разовый запуск Playwright поверх
сохранённой сессии (state.json), как в resume_reader.py, а не переиспользование
живого HHClient.context (чтобы не мешать идущему поиску, если он запущен).
Переиспользует свободные функции из hh_client.py, но без пагинации, стоп-слов
по заголовку и статистики сессии — только одна конкретная вакансия.

Пропускает is_vacancy_suitable(): пользователь уже сам решил, откликаясь по
имени — незачем спрашивать ИИ то, о чём его не спрашивали.
"""
import re

import database
from ai_analyzer import generate_cover_letter, active_style
from settings import settings, user_file
import sites
import hh_session
from hh_client import (
    TEST_FIELD_PREFIX, MAX_RESPONSE_ATTEMPTS,
    handle_vpn_check, diagnose_page, open_letter_field, fill_letter,
    find_submit_button, response_confirmed, attach_letter_after,
)

VACANCY_URL_RE = re.compile(
    rf"(?:https?://)?(?:www\.)?{sites.HOSTS_RE}/vacancy/\d+[^\s]*", re.IGNORECASE)

# Только повелительное наклонение — чтобы обычный вопрос вроде "стоит ли
# откликаться на эту вакансию?" не спровоцировал реальную отправку отклика.
APPLY_COMMAND_RE = re.compile(
    r"откликнись|откликнитесь|отправ(?:ь|ьте)\s+отклик|"
    r"подай(?:те)?\s+заявку|отправ(?:ь|ьте)\s+заявку|"
    r"примени(?:сь)?|примените|оформи(?:те)?\s+отклик",
    re.IGNORECASE)

QUICK_APPLY_CAPTCHA_FILE = str(user_file("quick_apply_captcha.png"))


class QuickApplyError(RuntimeError):
    """Сообщение уже человекочитаемое — можно показывать в чате как есть."""


class VacancyInfo:
    def __init__(self, url: str, job_id: str, title: str, description: str):
        self.url = url
        self.job_id = job_id
        self.title = title
        self.description = description


class ApplyResult:
    """message — готовый текст для chat_reply, НЕ проходит через модель:
    результат отклика детерминированный, придумывать тут нечего."""
    def __init__(self, message: str, applied: bool):
        self.message = message
        self.applied = applied


def find_vacancy_url(text: str) -> str | None:
    m = VACANCY_URL_RE.search(text or "")
    return m.group(0) if m else None


def has_apply_command(text: str) -> bool:
    return bool(APPLY_COMMAND_RE.search(text or ""))


def extract_job_id(url: str) -> str | None:
    """Мирроит hh_client.py (href.split("vacancy/")[1].split("?")[0]), но
    ссылка из чата приходит без контекста листинга — проверяем, что хвост и
    правда цифры, а не что-то похожее по написанию."""
    if "vacancy/" not in url:
        return None
    tail = url.split("vacancy/")[1].split("?")[0].rstrip("/")
    return tail if tail.isdigit() else None


def _normalize_url(url: str) -> str:
    if not re.match(r"https?://", url, re.IGNORECASE):
        return "https://" + url
    return url


async def _open_context(url: str):
    """Свой playwright+browser+context с сохранённой сессией СВОЕЙ площадки —
    НЕ self.context живого HHClient (чтобы не мешать идущему поиску, если он
    запущен). Площадка определяется по хосту вставленной ссылки; если хост
    не из группы HH (не должно случиться — VACANCY_URL_RE уже это проверил),
    берём активную площадку из настроек.

    Требует, чтобы пользователь хоть раз залогинился на ЭТОЙ площадке (тот же
    порог, что и у resume_reader.fetch_resume_text)."""
    site = sites.detect(url) or sites.active_site()
    try:
        return (*(await hh_session.open_session(site, headless=True, require_login=True)), site)
    except hh_session.SessionError as e:
        raise QuickApplyError(str(e))


async def _scrape_title(page) -> str:
    """Ни одна страница вакансии раньше не читалась ради заголовка — везде
    он берётся из карточки в выдаче. Здесь листинга нет, поэтому пробуем
    несколько путей от специфичного к общему."""
    for selector in ('h1[data-qa="vacancy-title"]', "h1"):
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=2000):
                text = (await loc.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    title = (await page.title()).strip()
    return title or "вакансия"


async def fetch_vacancy(url: str, timeout_ms: int = 30000) -> VacancyInfo:
    """Только чтение — название и описание, для обсуждения по ссылке без
    команды на отклик и как первый шаг apply_to_vacancy()."""
    url = _normalize_url(url)
    job_id = extract_job_id(url)
    if not job_id:
        raise QuickApplyError("Не похоже на ссылку на вакансию hh.")

    playwright, browser, context, site = await _open_context(url)
    try:
        page = await hh_session.new_stealth_page(context)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await handle_vpn_check(page)
        await page.wait_for_timeout(1500)

        desc_loc = page.locator('div[data-qa="vacancy-description"]')
        if not await desc_loc.is_visible(timeout=8000):
            code, reason = await diagnose_page(page, kind="vacancy", host=site["host"])
            if code == "captcha":
                raise QuickApplyError(
                    f"{site['host']} запросил капчу при открытии вакансии — попробуйте ещё раз через минуту.")
            if code == "vpn_check":
                raise QuickApplyError(
                    f"{site['host']} показал проверку VPN, и её не удалось пройти автоматически.")
            if code == "archived":
                raise QuickApplyError("Эта вакансия уже в архиве — отклик закрыт.")
            if code == "not_found":
                raise QuickApplyError("По этой ссылке вакансия не найдена.")
            raise QuickApplyError(f"Не удалось открыть описание вакансии ({reason}).")

        title = await _scrape_title(page)
        description = (await desc_loc.inner_text()).strip()
        return VacancyInfo(url=url, job_id=job_id, title=title, description=description)
    finally:
        await context.close()
        await browser.close()
        await playwright.stop()


async def apply_to_vacancy(url: str, *, ui_captcha, captcha_busy,
                           on_status=None, timeout_ms: int = 30000) -> ApplyResult:
    """Пишущий флоу на ОДНУ вакансию — урезанная копия инлайновой логики из
    hh_client.py (без стоп-слов по заголовку, статистики сессии, цикла
    решения капчи через несколько вакансий подряд: здесь одна вакансия и
    одна попытка на вызов; счётчик неудач в БД тот же самый, чтобы основной
    цикл поиска не начинал retry-серию заново для этой же вакансии).

    ui_captcha(image_path, prompt) -> str | None — тот же контракт, что
    AgentBridge._ask_captcha.
    captcha_busy() -> bool — True, если капча уже кем-то решается (основной
    цикл поиска или другой вызов чата) — единственный слот на приложение,
    лезть во второй бессмысленно.
    """
    def status(text: str):
        if on_status:
            on_status(text)

    url = _normalize_url(url)
    job_id = extract_job_id(url)
    if not job_id:
        raise QuickApplyError("Не похоже на ссылку на вакансию hh.")

    if database.is_job_applied(job_id):
        return ApplyResult("На эту вакансию отклик уже был отправлен раньше.", applied=True)

    status("Открываю вакансию…")
    playwright, browser, context, site = await _open_context(url)
    try:
        page = await hh_session.new_stealth_page(context)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await handle_vpn_check(page)
        await page.wait_for_timeout(1500)

        desc_loc = page.locator('div[data-qa="vacancy-description"]')
        while not await desc_loc.is_visible(timeout=8000):
            code, reason = await diagnose_page(page, kind="vacancy", host=site["host"])
            if code != "captcha":
                if code == "archived":
                    raise QuickApplyError("Эта вакансия уже в архиве — отклик закрыт.")
                if code == "not_found":
                    raise QuickApplyError("По этой ссылке вакансия не найдена.")
                raise QuickApplyError(f"Не удалось открыть вакансию ({reason}).")

            if captcha_busy():
                raise QuickApplyError(
                    "Капча уже ждёт ответа в другом окне — дождитесь и повторите.")

            await page.screenshot(path=QUICK_APPLY_CAPTCHA_FILE)
            solution = await ui_captcha(
                QUICK_APPLY_CAPTCHA_FILE,
                "🚨 Похоже на капчу при отклике по ссылке из чата. "
                "Введите текст с картинки:")
            if not solution:
                raise QuickApplyError("Капча не решена — отклик не отправлен.")

            input_field = page.locator('input[type="text"]').first
            if await input_field.is_visible():
                await input_field.click()
                await input_field.type(solution, delay=120)
                submit_captcha = page.locator(
                    'button[type="submit"]:visible, button:has-text("Отправить"):visible'
                ).first
                try:
                    if await submit_captcha.is_visible():
                        await submit_captcha.click()
                    else:
                        await input_field.press("Enter")
                except Exception:
                    await input_field.press("Enter")
                await page.wait_for_timeout(4000)
            else:
                await page.reload()
                await page.wait_for_timeout(3000)

        # Пока решали капчу, вакансию могли обработать другим путём.
        if database.is_job_applied(job_id):
            return ApplyResult("На эту вакансию отклик уже был отправлен раньше.", applied=True)

        title = await _scrape_title(page)
        description = (await desc_loc.inner_text()).strip()

        status("Пишу сопроводительное…")
        # Стиль читаем заранее — записываем его вместе с откликом в БД.
        letter_style = active_style()
        cover_letter = await generate_cover_letter(title, description, style=letter_style)

        apply_btn = page.locator('a[data-qa="vacancy-response-link-top"]').first
        if not await apply_btn.is_visible():
            # Кнопки нет — обычно потому, что отклик уже есть.
            if await response_confirmed(page, url):
                database.add_applied_job(job_id, title, url)
                return ApplyResult(
                    f"На вакансию «{title}» отклик уже был отправлен ранее.", applied=True)
            raise QuickApplyError(
                f"На вакансии «{title}» нет кнопки «Откликнуться», и отклика тоже нет — "
                f"возможно, резюме не подходит под требования {site['host']} к этой вакансии.")

        await apply_btn.click()
        await page.wait_for_timeout(2500)

        # Выбор резюме, если их несколько — как в основном цикле.
        try:
            target_resume_name = settings.target_resume_name
            if target_resume_name:
                resume_dropdown = page.locator(
                    '[data-qa*="resume-select"], [data-qa*="resume-selector"], '
                    '[data-qa="vacancy-response-resume-selector"]').first
                if await resume_dropdown.is_visible():
                    await resume_dropdown.click()
                    await page.wait_for_timeout(800)
                    target_resume_btn = page.locator(f'text="{target_resume_name}"').first
                    if await target_resume_btn.is_visible():
                        await target_resume_btn.click()
                        await page.wait_for_timeout(800)
        except Exception:
            pass  # необязательный шаг — единственное резюме и так выбрано

        # Тест работодателя — его должен пройти человек, как в основном цикле.
        if await page.locator(f'textarea[name^="{TEST_FIELD_PREFIX}"]').count() > 0:
            database.add_applied_job(job_id, title, url)
            raise QuickApplyError(
                f"У вакансии «{title}» есть тест работодателя — на него нужно ответить "
                f"вручную на {site['host']}, отклик оттуда не пройдёт автоматически. "
                f"Сопроводительное письмо уже готово:\n\n⟦letter⟧{cover_letter}⟦/letter⟧")

        letter_sent = False
        letter_field = await open_letter_field(page)
        if letter_field is not None:
            letter_sent = await fill_letter(letter_field, cover_letter)

        if not letter_sent and settings.require_letter:
            database.add_applied_job(job_id, title, url)
            raise QuickApplyError(
                f"Не удалось приложить сопроводительное к «{title}» — отклик не отправлен. "
                f"Письмо уже готово, можно откликнуться вручную:\n\n⟦letter⟧{cover_letter}⟦/letter⟧")

        status("Проверяю отклик…")
        submit_btn = await find_submit_button(page)
        if submit_btn is None:
            attempts = database.bump_failed_response(job_id, title)
            if attempts >= MAX_RESPONSE_ATTEMPTS:
                database.add_applied_job(job_id, title, url)
            raise QuickApplyError(f"Не нашёл кнопку отправки отклика на «{title}».")

        await submit_btn.click()
        try:
            await submit_btn.wait_for(state="hidden", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        if not letter_sent:
            letter_sent = await attach_letter_after(page, cover_letter)

        if not await response_confirmed(page, url):
            attempts = database.bump_failed_response(job_id, title)
            if attempts >= MAX_RESPONSE_ATTEMPTS:
                database.add_applied_job(job_id, title, url)
            raise QuickApplyError(
                f"{site['host']} не подтвердил отправку отклика на «{title}» (попытка {attempts}).")

        # Обязательно: иначе основной цикл поиска потом сам найдёт эту
        # вакансию и откликнется на неё повторно.
        database.add_applied_job(job_id, title, url, style=letter_style if letter_sent else None)
        if letter_sent:
            return ApplyResult(
                f"Готово — откликнулась на «{title}» с сопроводительным письмом:\n\n"
                f"⟦letter⟧{cover_letter}⟦/letter⟧",
                applied=True)
        return ApplyResult(
            f"Готово — откликнулась на «{title}», но без письма ({site['host']} не дал его приложить).",
            applied=True)
    finally:
        await context.close()
        await browser.close()
        await playwright.stop()
