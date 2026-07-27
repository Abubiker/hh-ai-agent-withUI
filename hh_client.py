import os
import re
import asyncio
import random
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import database
import control
from stats import Stats
from ai_analyzer import is_vacancy_suitable, generate_cover_letter
from settings import settings
from urllib.parse import quote_plus

from settings import user_file

# Файлы лежат в папке пользователя, а не рядом с кодом: внутри собранного
# .app соседняя папка временная и только для чтения.
STATE_FILE = str(user_file("state.json"))
CAPTCHA_FILE = str(user_file("captcha.png"))


class SkipVacancy(Exception):
    """Вакансию нужно пропустить штатно (архив, редирект, капча), это не ошибка."""

# Жёсткий фильтр по названию вакансии: очевидно чужие грейды и профессии,
# чтобы не гонять на них модель. Проверка идёт по ЦЕЛЫМ словам (с учётом
# русских окончаний), а не по подстроке: раньше "intern" отсекал
# "International", "лид" — "валидацию" и "консолидацию", а "hr" — "Chrome".
# Английские слова ищем ЦЕЛИКОМ: иначе "intern" отсекает "International",
# а "hr" — "Chrome".
# Senior/Сеньор намеренно НЕ в списке: на старшие позиции откликаемся тоже.
# Отсекаем только управленческие роли (Lead, Head, руководитель) и чужие профессии.
STOP_WORDS_EN = [
    "lead", "head", "architect", "intern", "trainee",
    "manager", "designer", "hr", "analyst", "1c",
]

# Русские — с любым падежным окончанием ("аналитику", "менеджеров"),
# но обязательно с начала слова, иначе "лид" ловит "валидацию".
STOP_WORDS_RU = [
    "лид", "архитектор", "руководител", "главн", "стажер",
    "стажёр", "стажировк", "менеджер", "дизайнер", "аналитик",
    "преподавател", "педагог", "маркетолог", "продаж", "1с",
    "слесар", "диспетчер", "ассистент", "риелтор", "учител",
]

_STOP_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(w) for w in STOP_WORDS_RU) + r")\w*"
    r"|\b(?:" + "|".join(re.escape(w) for w in STOP_WORDS_EN) + r")\b",
    re.IGNORECASE,
)


def find_stop_word(title: str):
    """Возвращает найденное стоп-слово или None, если название чистое."""
    m = _STOP_RE.search(title.lower())
    return m.group(0) if m else None


# Кнопка/ссылка, раскрывающая поле письма
LETTER_TOGGLE_SELECTORS = [
    '[data-qa*="letter-toggle"]',
    '[data-qa*="letter-informer"]',
    'text="Написать сопроводительное"',
    'text="Добавить сопроводительное"',
    'text="Сопроводительное письмо"',
    'text="Прикрепить сопроводительное"',
    'text=/сопроводительн/i',
]

# Признаки поля письма и поля чата. Раньше поле искалось по списку
# угаданных data-qa — если HH называл его иначе, не находилось ничего
# и отклики уходили пустыми. Теперь страница осматривается целиком,
# а кандидаты отбираются по признакам.
LETTER_HINTS = ("letter", "covering", "сопроводит", "cover")
CHAT_HINTS = ("chat", "chatik", "negotiation", "messag", "сообщени", "topic")

# Собираем все textarea со страницы вместе с контекстом (data-qa родителей),
# чтобы отличить поле письма от поля чата, не завися от точных имён.
_COLLECT_TEXTAREAS_JS = """
() => {
  const out = [];
  document.querySelectorAll('textarea').forEach((el, i) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const visible = r.width > 1 && r.height > 1 &&
                    cs.visibility !== 'hidden' && cs.display !== 'none';
    const ctx = [];
    let p = el;
    for (let d = 0; d < 8 && p; d++, p = p.parentElement) {
      const q = p.getAttribute && p.getAttribute('data-qa');
      if (q) ctx.push(q);
    }
    out.push({
      index: i,
      visible,
      qa: el.getAttribute('data-qa') || '',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      ctx: ctx.join(' '),
      disabled: el.disabled || el.readOnly,
    });
  });
  return out;
}
"""


def _haystack(ta: dict) -> str:
    return " ".join(str(ta.get(k, "")) for k in
                    ("qa", "name", "id", "placeholder", "aria", "ctx")).lower()


async def list_textareas(page) -> list[dict]:
    try:
        return await page.evaluate(_COLLECT_TEXTAREAS_JS)
    except Exception:
        return []


async def find_letter_field(page, verbose: bool = False):
    """Ищет поле сопроводительного письма, осматривая страницу.

    Порядок: явные признаки письма → единственная подходящая textarea,
    не принадлежащая чату. Поле чата исключается всегда: перепутать их —
    значит отправить пустой отклик, о чём агент раньше рапортовал как об
    успехе.
    """
    areas = await list_textareas(page)
    usable = [t for t in areas if t["visible"] and not t["disabled"]]
    if not usable:
        return None

    # 1) Явно поле письма
    for ta in usable:
        hay = _haystack(ta)
        if any(h in hay for h in LETTER_HINTS):
            if verbose:
                print(f"   поле письма: data-qa={ta['qa'] or '—'} name={ta['name'] or '—'}")
            return page.locator("textarea").nth(ta["index"])

    # 2) Всё, что не чат. Если кандидат один — это он.
    non_chat = [t for t in usable if not any(h in _haystack(t) for h in CHAT_HINTS)]
    if len(non_chat) == 1:
        ta = non_chat[0]
        if verbose:
            print(f"   поле письма (единственное подходящее): "
                  f"data-qa={ta['qa'] or '—'} placeholder={ta['placeholder'] or '—'}")
        return page.locator("textarea").nth(ta["index"])

    return None


# Кнопка отправки отклика. Ищем так же, как поле письма: осматриваем
# страницу, а не полагаемся на единственное угаданное имя. Прошлый селектор
# 'button[data-qa*="vacancy-response-submit"]' не совпадал с кнопкой в
# попапе — письмо вписывалось, а отклик не уходил.
BUTTON_SELECTOR = 'button, input[type="submit"]'

SUBMIT_QA_HINTS = ("vacancy-response-submit", "response-submit", "letter-send",
                   "vacancy-response-letter", "submit")
SUBMIT_TEXT_HINTS = ("откликнуться", "отправить отклик", "отправить письмо",
                     "отправить", "подтвердить")
# Кнопки, на которые нажимать нельзя ни при каких условиях
SUBMIT_TEXT_BLOCK = ("отмен", "закрыть", "назад", "не сейчас", "передумал",
                     "пожаловаться", "поделиться")

_COLLECT_BUTTONS_JS = """
() => {
  const out = [];
  document.querySelectorAll('button, input[type="submit"]').forEach((el, i) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const visible = r.width > 1 && r.height > 1 &&
                    cs.visibility !== 'hidden' && cs.display !== 'none';
    const ctx = [];
    let p = el;
    for (let d = 0; d < 8 && p; d++, p = p.parentElement) {
      const q = p.getAttribute && p.getAttribute('data-qa');
      if (q) ctx.push(q);
    }
    out.push({
      index: i,
      visible,
      disabled: el.disabled,
      qa: el.getAttribute('data-qa') || '',
      text: (el.innerText || el.value || '').trim().slice(0, 60),
      ctx: ctx.join(' '),
    });
  });
  return out;
}
"""


async def list_buttons(page) -> list[dict]:
    try:
        return await page.evaluate(_COLLECT_BUTTONS_JS)
    except Exception:
        return []


async def find_submit_button(page, verbose: bool = True):
    """Ищет кнопку отправки отклика, осматривая страницу."""
    buttons = await list_buttons(page)
    usable = [b for b in buttons if b["visible"] and not b["disabled"]]
    if not usable:
        return None

    def blocked(b):
        return any(w in b["text"].lower() for w in SUBMIT_TEXT_BLOCK)

    # 1) По data-qa (своему или родительского блока)
    for b in usable:
        if blocked(b):
            continue
        hay = (b["qa"] + " " + b["ctx"]).lower()
        if any(h in hay for h in SUBMIT_QA_HINTS):
            if verbose:
                print(f"   кнопка отправки: data-qa={b['qa'] or '—'} «{b['text']}»")
            return page.locator(BUTTON_SELECTOR).nth(b["index"])

    # 2) По надписи на кнопке
    for hint in SUBMIT_TEXT_HINTS:
        for b in usable:
            if blocked(b):
                continue
            if hint in b["text"].lower():
                if verbose:
                    print(f"   кнопка отправки по надписи: «{b['text']}»")
                return page.locator(BUTTON_SELECTOR).nth(b["index"])
    return None


async def dump_buttons(page):
    """Печатает кнопки страницы — чтобы было видно, как HH назвал нужную."""
    buttons = await list_buttons(page)
    visible = [b for b in buttons if b["visible"]]
    print(f"   кнопки на странице ({len(visible)} видимых из {len(buttons)}):")
    for b in visible[:25]:
        print(f"     [{b['index']}] data-qa={b['qa'] or '—'} «{b['text'] or '—'}» "
              f"ctx={b['ctx'][:70] or '—'}")


async def dump_textareas(page, title: str = ""):
    """Печатает, какие поля есть на странице. Нужно, когда поле письма не
    нашлось: по этому выводу видно, как HH назвал его на самом деле."""
    areas = await list_textareas(page)
    if not areas:
        print("   на странице нет ни одного textarea")
        return
    print(f"   поля на странице ({len(areas)}):")
    for ta in areas:
        state = "видимое" if ta["visible"] else "скрытое"
        print(f"     [{ta['index']}] {state} data-qa={ta['qa'] or '—'} "
              f"name={ta['name'] or '—'} placeholder={ta['placeholder'] or '—'} "
              f"ctx={ta['ctx'][:80] or '—'}")


async def open_letter_field(page, verbose: bool = True):
    """Раскрывает поле письма, если оно спрятано за кнопкой, и возвращает его."""
    field = await find_letter_field(page, verbose=verbose)
    if field:
        return field

    # Поле часто скрыто за ссылкой «Написать сопроводительное»
    for selector in LETTER_TOGGLE_SELECTORS:
        try:
            toggle = page.locator(selector).first
            if await toggle.is_visible(timeout=800):
                await toggle.click()
                await asyncio.sleep(1.2)
                field = await find_letter_field(page, verbose=verbose)
                if field:
                    return field
        except Exception:
            continue
    return None


async def fill_letter(field, text: str) -> bool:
    """Вписывает письмо и проверяет, что оно осталось в поле.

    HH — реактивное приложение: fill() иногда не доходит до состояния формы,
    и поле сбрасывается. Поэтому после заполнения значение читается обратно,
    а при неудаче текст набирается посимвольно, как это делал бы человек.
    """
    try:
        await field.fill(text)
        await asyncio.sleep(0.4)
        if (await field.input_value()).strip():
            return True
    except Exception:
        pass

    # Запасной путь: клик + набор текста (некоторые формы слушают только события ввода)
    try:
        await field.click()
        await asyncio.sleep(0.2)
        await field.type(text[:2000], delay=1)
        await asyncio.sleep(0.4)
        return bool((await field.input_value()).strip())
    except Exception as e:
        print(f"   не удалось вписать письмо: {e}")
        return False


async def handle_vpn_check(page) -> bool:
    """HH подменяет страницу проверкой «VPN мешает работе сайта» (/vpncheeck).
    Именно это раньше принималось за капчу. Жмём «Я не использую VPN».

    Возвращает True, если проверка была и её удалось пройти.
    """
    if "vpncheeck" not in page.url:
        return False

    print("🔒 HH показал проверку VPN — нажимаю «Я не использую VPN»...")
    for attempt in (1, 2):
        # Проверка VPN с повторами занимает секунды — на остановке бросаем сразу,
        # иначе агент продолжает стучаться к HH уже после нажатия «Остановить».
        if control.should_stop():
            return False
        try:
            btn = page.locator('text="Я не использую VPN"').first
            await btn.click(timeout=5000)
            await control.sleep_or_stop(random.uniform(2.5, 4.0))
            if "vpncheeck" not in page.url:
                print("   ✅ Проверка пройдена, продолжаю.")
                return True
        except Exception:
            pass
        if attempt == 1:
            await control.sleep_or_stop(3)

    print("   ⚠️ Пройти проверку не удалось. Скорее всего включён VPN — "
          "отключите его, HH блокирует такие подключения.")
    return False


async def diagnose_page(page):
    """Разбирается, почему на странице нет описания вакансии.

    Исходный код считал капчей ЛЮБОЕ отсутствие описания, хотя чаще это
    архивная вакансия или редирект. Возвращает (код, человекочитаемая причина).
    Код: captcha | archived | not_found | redirect | unknown
    """
    try:
        url = page.url
        try:
            body = (await page.locator("body").inner_text(timeout=3000))[:2000].lower()
        except Exception:
            body = ""
        title = (await page.title()).lower()
        probe = title + " " + body

        if "vpncheeck" in url or "vpn мешает работе" in (title + body):
            return "vpn_check", "HH требует отключить VPN (страница /vpncheeck)"

        markers = [
            ("captcha", ["подтвердите, что вы не робот", "вы не робот", "captcha",
                         "капча", "just a moment", "проверка браузера",
                         "необычн", "подозрительн"]),
            ("archived", ["вакансия в архиве", "в архиве", "вакансия закрыта",
                          "уже не размещ"]),
            ("not_found", ["такой вакансии больше нет", "страница не найдена",
                           "404", "вакансия не найдена"]),
        ]
        for code, words in markers:
            if any(w in probe for w in words):
                return code, f"{code} (по тексту страницы)"

        if "hh.ru/vacancy/" not in url:
            return "redirect", f"редирект на {url[:80]}"
        return "unknown", f"описание не найдено, заголовок: {title[:60]!r}"
    except Exception as e:
        return "unknown", f"не удалось разобрать страницу: {e}"

class HHClient:
    def __init__(self, sinks=None):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.stats = Stats()
        # Память на сеанс: какие вакансии уже встречались (для раннего обрыва
        # пагинации на повторных проверках) и о каких пропусках уже сообщали
        # (чтобы не спамить лог одними и теми же строками каждую проверку).
        self._seen_ids = set()
        self._skip_logged = set()
        # Получатели уведомлений. Нужны в том числе для ввода капчи: её может
        # принять окно приложения или Telegram, смотря что настроено.
        if sinks is None:
            from notify_sinks import build_sinks
            sinks = build_sinks(telegram=control.telegram_enabled)
        self.sinks = sinks

    async def start(self):
        self.playwright = await async_playwright().start()
        # Запуск в headless=False для того, чтобы в первый раз пользователь мог войти (ввести смс/пароль),
        # либо полностью headless, если state.json существует.
        headless = os.path.exists(STATE_FILE)
        self.browser = await self.playwright.chromium.launch(headless=headless)
        
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        if os.path.exists(STATE_FILE):
            self.context = await self.browser.new_context(storage_state=STATE_FILE, user_agent=user_agent)
        else:
            self.context = await self.browser.new_context(user_agent=user_agent)
        
        self.page = await self.context.new_page()
        await Stealth().apply_stealth_async(self.page)

    async def login_if_needed(self):
        print("Переходим на HH.ru для проверки авторизации...")
        await self.page.goto("https://hh.ru/")
        await asyncio.sleep(3)
        
        # Ждем, пока страница реально прогрузится, чтобы не ловить "пустой" экран
        await self.page.wait_for_load_state('networkidle')
        await asyncio.sleep(2)
        
        # Ищем любую ссылку или кнопку с текстом "Войти"
        login_link = self.page.locator('a:has-text("Войти")')
        login_button = self.page.locator('button:has-text("Войти")')
        
        if not await login_link.count() and not await login_button.count():
            print("Уже авторизованы (кнопка 'Войти' не найдена).")
            return True

        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print("❌ Файл сессии (state.json) недействителен. Я его удалил.")
            print("Пожалуйста, перезапустите скрипт (python main.py), чтобы открылось окно браузера для входа.")
            return False

        print("=========================================")
        print("❗ НУЖНА АВТОРИЗАЦИЯ ❗")
        print("1. В открывшемся браузере войдите в свой аккаунт HH.ru.")
        print("2. Дождитесь, пока загрузится ваш профиль.")
        print("3. ВЕРНИТЕСЬ В ЭТО ОКНО КОНСОЛИ И НАЖМИТЕ КЛАВИШУ ENTER.")
        print("=========================================")
        
        try:
            # Ожидаем нажатия Enter (в отдельном потоке, чтобы не блокировать асинхронность)
            await asyncio.to_thread(input, "👉 Нажмите ENTER здесь, когда войдете в аккаунт: ")
            
            print("⏳ Сохраняем сессию...")
            await asyncio.sleep(2) # На всякий случай даем странице загрузиться
            await self.context.storage_state(path=STATE_FILE)
            print("✅ Авторизация успешна, состояние сохранено!")
            return True
        except Exception as e:
            print(f"❌ Произошла ошибка при сохранении авторизации: {e}")
            return False

    async def search_and_apply(self, send_notification_func):
        print("Начинаем поиск вакансий...")

        # Регионы включаются/выключаются с экрана «Работа» без похода
        # в Фильтры — проверяем один раз на весь проход, а не на каждый запрос.
        active_regions = settings.active_regions
        if not active_regions:
            print("⚠️ Нет ни одного включённого региона — включите хотя бы один "
                  "на экране «Работа» или в Фильтрах.")
            return

        for query in settings.search_queries:
            if control.should_stop():
                print("⏹️ Получен сигнал остановки — прерываю поиск.")
                return
            print(f"\n======================================")
            print(f"🔍 Поиск по запросу: {query}")
            print(f"======================================")

            for config in active_regions:
                # Проверяем и здесь: без этого после остановки агент успевал
                # перейти к следующему региону и снова пойти на hh.ru.
                if control.should_stop():
                    print("⏹️ Получен сигнал остановки — прерываю поиск.")
                    return
                print(f"📍 Режим: {config['name']}")
                # quote_plus: в запросах есть пробелы и кириллица — кодируем явно,
                # чтобы URL не зависел от того, как их нормализует браузер.
                # moreThan6 — чтобы в выдачу попадали и старшие позиции (Senior/Ведущий),
                # на них теперь тоже откликаемся. Слишком высокие требования отсеет ИИ.
                # search_field=name — искать слова запроса только в названии вакансии.
                field = "&search_field=name" if settings.title_only else ""
                exp = "".join(f"&experience={e}" for e in settings.experience)
                url = (f"https://hh.ru/search/vacancy?text={quote_plus(query)}"
                       f"{field}&order_by=publication_time"
                       f"{exp}{config['params']}")
                await self.page.goto(url)
                await control.sleep_or_stop(3)
                await handle_vpn_check(self.page)
                if control.should_stop():
                    print("⏹️ Получен сигнал остановки — прерываю поиск.")
                    return
                page_num = 1
                while True:
                    # Проверка до печати: иначе после остановки в журнал успевала
                    # попасть строка «Смотрю страницу N» уже ненужной страницы.
                    if control.should_stop():
                        print("⏹️ Получен сигнал остановки — прерываю поиск.")
                        return
                    print(f"📄 Смотрю страницу {page_num} по запросу '{query}' ({config['name']})...")
                    vacancies = await self.page.locator('a[data-qa="serp-item__title"]').all()

                    # Собираем ссылки заранее, чтобы избежать ошибки Detached Node при долгом парсинге
                    links_to_process = []
                    for v in vacancies:
                        href = await v.get_attribute("href")
                        title = await v.inner_text()
                        if href:
                            links_to_process.append((title, href))

                    # Сколько на этой странице вакансий, которых мы ещё не видели.
                    # Выдача отсортирована по дате публикации, поэтому если новых
                    # нет — дальше листать бессмысленно, там только более старые.
                    new_on_page = 0

                    for title, href in links_to_process:
                        # Парсим ID вакансии из URL (https://hh.ru/vacancy/123456?...)
                        job_id = None
                        if "vacancy/" in href:
                            job_id = href.split("vacancy/")[1].split("?")[0]

                        if not job_id or database.is_job_applied(job_id):
                            continue

                        # Вакансия встречена впервые за сеанс — даже если её сейчас
                        # отсеет стоп-фильтр, страница считается «свежей» и пагинация
                        # продолжится: релевантные новые могут быть глубже. На повторных
                        # проверках уже виденное не считается — страницы без новинок
                        # обрываются сразу.
                        if job_id not in self._seen_ids:
                            self._seen_ids.add(job_id)
                            self.stats.fresh += 1
                            new_on_page += 1

                        if control.should_stop():
                            print("⏹️ Получен сигнал остановки — прерываю поиск.")
                            return

                        # Жёсткий фильтр по названию — ДО открытия страницы: названия
                        # хватает, а загрузка вакансии стоит ~4 секунды на каждую.
                        hit = find_stop_word(title)
                        if hit:
                            # В базу не пишем (правки стоп-слов должны действовать и на
                            # уже виденные вакансии), но и не спамим одной и той же
                            # строкой каждую проверку — только при первой встрече.
                            if job_id not in self._skip_logged:
                                self._skip_logged.add(job_id)
                                self.stats.hard_skipped += 1
                                print(f"⏩ Пропускаю (не тот грейд/профессия — '{hit}'): {title}")
                            continue

                        self.stats.viewed += 1
                        print(f"👁️ Открываем вакансию: {title}")
                        page = await self.context.new_page()
                        await Stealth().apply_stealth_async(page)
                        try:
                            await page.goto(href)
                            await asyncio.sleep(2)
                            # Проверка VPN может подменить и страницу вакансии
                            if await handle_vpn_check(page):
                                await page.goto(href)
                                await asyncio.sleep(2)

                            desc_loc = page.locator('div[data-qa="vacancy-description"]')
                            # Если описания нет — разбираемся, ЧТО именно на странице.
                            # Раньше любое отсутствие описания считалось капчей.
                            while not await desc_loc.is_visible():
                                reason_code, reason_text = await diagnose_page(page)

                                # Архив, удалённая вакансия или чужая вёрстка — не капча,
                                # решать нечего. Помечаем обработанной и идём дальше.
                                if reason_code != "captcha":
                                    print(f"⏭️ Пропускаю ({reason_text}): {title}")
                                    database.add_applied_job(job_id, title, href)
                                    raise SkipVacancy(reason_code)

                                print(f"🚨 Похоже на капчу/антибот: {title}")

                                try:
                                    # Скриншот видимой области (без full_page, чтобы не
                                    # триггерить ресайз окна)
                                    await page.screenshot(path=CAPTCHA_FILE)

                                    # Ввести текст может окно приложения или Telegram —
                                    # смотря что настроено. Если некому, получим None.
                                    solution = await self.sinks.solve_captcha(
                                        CAPTCHA_FILE,
                                        f"🚨 <b>Похоже на капчу</b>\nАгент застрял на вакансии "
                                        f"<i>{title}</i>.\n\nВведите текст с картинки "
                                        f"(если там два слова — через пробел):")

                                    if not solution:
                                        print(f"   Скриншот сохранён в {CAPTCHA_FILE}")
                                        print("⏭️ Капча не решена — пропускаю вакансию.")
                                        raise SkipVacancy("captcha")

                                    print(f"Вводим решение: {solution}")

                                    input_field = page.locator('input[type="text"]').first
                                    if await input_field.is_visible():
                                        await input_field.click()
                                        await asyncio.sleep(random.uniform(0.5, 1.2))
                                        
                                        for char in solution:
                                            if char == " ":
                                                await asyncio.sleep(random.uniform(0.6, 1.5)) # Медленный пробел между словами
                                            await input_field.type(char, delay=random.randint(150, 400)) # Человечный ввод
                                            
                                        await asyncio.sleep(random.uniform(1.0, 2.5))
                                        await input_field.press('Enter')
                                        await asyncio.sleep(5) # Ждем прогрузки после ввода
                                    else:
                                        # Если поля ввода нет (возможно это галочка Cloudflare или вы уже решили её в другом браузере)
                                        # Просто обновляем страницу, чтобы проверить, не снят ли бан по IP
                                        print("Поле ввода не найдено. Обновляем страницу...")
                                        await page.reload()
                                        await asyncio.sleep(4)
                                    
                                    # Проверяем, появилось ли описание
                                    desc_loc = page.locator('div[data-qa="vacancy-description"]')
                                    if await desc_loc.is_visible():
                                        try:
                                            await send_notification_func("✅ Капча успешно пройдена! Бот продолжает работу.", kind="captcha")
                                        except:
                                            pass
                                        print("✅ Капча пройдена!")
                                        break # Выходим из цикла решения капчи
                                    else:
                                        try:
                                            await send_notification_func("❌ Капча решена неверно (или появилась новая). Пробуем ещё раз!", kind="captcha")
                                        except Exception:
                                            pass
                                        print("❌ Капча не пройдена. Повторная попытка...")
                                        # Цикл while начнется заново: сделает новый скриншот и попросит ввод
                                        
                                except SkipVacancy:
                                    raise  # штатный пропуск, не глушим
                                except Exception as e:
                                    print(f"Ошибка при обработке капчи: {e}")
                                    raise SkipVacancy("captcha_error")
                            description = await desc_loc.inner_text()

                            # Анализ ИИ
                            if await is_vacancy_suitable(title, description):
                                self.stats.ai_pass += 1
                                print(f"✨ Вакансия подходит: {title}")

                                # Письмо пишется ~12 секунд — без этой строки в логе
                                # было полное затишье, интерфейсу нечем показать прогресс.
                                print(f"✍️ Пишу сопроводительное — {title}")
                                cover_letter = await generate_cover_letter(title, description)
                                self.stats.letters += 1

                                # Пробуем откликнуться
                                apply_btn = page.locator('a[data-qa="vacancy-response-link-top"]').first
                                if await apply_btn.is_visible():
                                    # Имитируем поведение человека перед откликом
                                    await page.mouse.move(random.randint(100, 700), random.randint(100, 500))
                                    await page.mouse.wheel(0, random.randint(200, 600))
                                    await asyncio.sleep(random.uniform(0.8, 1.5))
                                    await page.mouse.wheel(0, random.randint(-200, 100))
                                    await asyncio.sleep(random.uniform(0.5, 1.0))
                                    
                                    await apply_btn.click()
                                    # Даем время на открытие попапа ИЛИ загрузку новой страницы отклика
                                    await asyncio.sleep(3)
                                
                                    # Шаг 0: Выбор нужного резюме (если их несколько)
                                    try:
                                        TARGET_RESUME_NAME = settings.target_resume_name
                                        if TARGET_RESUME_NAME:
                                            resume_dropdown = page.locator('[data-qa*="resume-select"], [data-qa*="resume-selector"], [data-qa="vacancy-response-resume-selector"]').first
                                            if await resume_dropdown.is_visible():
                                                await resume_dropdown.click()
                                                await asyncio.sleep(1)
                                                # Кликаем по нужному резюме из выпадающего списка
                                                target_resume_btn = page.locator(f'text="{TARGET_RESUME_NAME}"').first
                                                if await target_resume_btn.is_visible():
                                                    await target_resume_btn.click()
                                                    await asyncio.sleep(1)
                                    except Exception as e:
                                        print(f"⚠️ Ошибка при выборе резюме: {e}")
                                
                                    # Шаг 1-2: находим поле письма (при необходимости раскрыв его)
                                    # и убеждаемся, что текст реально в него попал.
                                    letter_sent = False
                                    letter_field = await open_letter_field(page)
                                    if letter_field is None:
                                        print(f"⚠️ Поле сопроводительного не найдено: {title}")
                                        # Печатаем, что вообще есть на странице: по этому выводу
                                        # видно, как HH назвал поле, если разметка изменилась.
                                        await dump_textareas(page, title)
                                    else:
                                        letter_sent = await fill_letter(letter_field, cover_letter)
                                        if letter_sent:
                                            print("   ✅ письмо вписано в форму отклика")
                                        else:
                                            print(f"⚠️ Письмо не удержалось в поле: {title}")

                                    # Без письма отклики часто не рассматривают, поэтому по
                                    # умолчанию пустой отклик не отправляем: вакансия уходит
                                    # в уведомление вместе с готовым письмом — откликнуться
                                    # вручную дешевле, чем сжечь вакансию впустую.
                                    if not letter_sent and settings.require_letter:
                                        self.stats.skipped_no_letter += 1
                                        database.add_applied_job(job_id, title, href)
                                        import html as _html
                                        await send_notification_func(
                                            f"⚠️ <b>Не смог приложить письмо</b>: <a href='{href}'>{title}</a>\n"
                                            f"Отклик не отправлен. Письмо готово — можно откликнуться вручную:\n\n"
                                            f"<i>{_html.escape(cover_letter)}</i>", kind="error")
                                        print(f"⏭️ Отклик не отправлен (нет письма): {title}")
                                        raise SkipVacancy("no_letter")

                                    # Шаг 3: отправка отклика
                                    submit_btn = await find_submit_button(page)
                                    if submit_btn is not None:
                                        await submit_btn.click() # РЕАЛЬНЫЙ ОТКЛИК
                                        await asyncio.sleep(2)

                                        # Если формы письма не было (HH отправляет такие отклики
                                        # сразу по клику), он сам предлагает дослать письмо —
                                        # пользуемся этим, чтобы вакансия не осталась пустой.
                                        if not letter_sent:
                                            letter_sent = await self._attach_letter_after(page, cover_letter)

                                        database.add_applied_job(job_id, title, href)
                                        self.stats.applied += 1
                                        if not letter_sent:
                                            self.stats.applied_no_letter += 1

                                        import html
                                        safe_cover_letter = html.escape(cover_letter)

                                        if letter_sent:
                                            await send_notification_func(f"✅ Успешный отклик: <a href='{href}'>{title}</a>\n\n<b>Письмо:</b>\n<i>{safe_cover_letter}</i>", kind="applied")
                                            print(f"✅ Отклик отправлен с письмом: {title}")
                                        else:
                                            await send_notification_func(f"✅ Отклик <b>без письма</b>: <a href='{href}'>{title}</a>\n\n<i>(HH не дал приложить сопроводительное к этой вакансии)</i>", kind="applied")
                                            print(f"✅ Отклик отправлен БЕЗ письма: {title}")
                                    else:
                                        self.stats.apply_failed += 1
                                        print(f"⚠️ Не нашёл кнопку отправки отклика: {title}")
                                        # Показываем, что за кнопки на странице — по этому
                                        # выводу видно, как HH назвал нужную.
                                        await dump_buttons(page)
                                else:
                                    self.stats.already += 1
                                    print(f"Кнопка отклика не найдена (возможно, уже откликались): {title}")
                                    database.add_applied_job(job_id, title, href)
                            else:
                                self.stats.ai_reject += 1
                                print(f"❌ ИИ отклонил: {title}")
                                database.add_applied_job(job_id, title, href) # Добавляем, чтобы больше не анализировать

                        except SkipVacancy as skip:
                            # Пропуск из-за ненайденного письма уже посчитан отдельно —
                            # иначе вакансия попала бы сразу в два счётчика.
                            if str(skip) != "no_letter":
                                self.stats.skipped_page += 1
                        except Exception as e:
                            # Сюда попадает и сбой связи с моделью (is_vacancy_suitable бросает
                            # исключение). Вакансию НЕ записываем в базу — вернёмся к ней позже.
                            print(f"Ошибка при обработке вакансии {title}: {e}")
                        finally:
                            await page.close()
                    
                    # Выдача отсортирована по дате публикации: если на странице не было
                    # ни одной невиданной вакансии, глубже — только ещё более старые.
                    # Обрываем пагинацию и не тратим время на пустые страницы.
                    if new_on_page == 0:
                        break

                    # Лимит страниц на запрос, чтобы успеть пройтись по всем запросам
                    # из настроек, а не закопаться в первом же.
                    if page_num >= settings.max_pages_per_query:
                        print(f"📑 Просмотрено {page_num} стр. — лимит на запрос, иду дальше.")
                        break

                    if control.should_stop():
                        print("⏹️ Получен сигнал остановки — прерываю поиск.")
                        return

                    # После того как все вакансии на странице обработаны, проверяем кнопку "Дальше"
                    next_btn = self.page.locator('a[data-qa="pager-next"]')
                    if await next_btn.count() > 0 and await next_btn.is_visible():
                        print("➡️ Перехожу на следующую страницу...")
                        await next_btn.click()
                        await control.sleep_or_stop(4)
                        page_num += 1
                    else:
                        break

    async def _attach_letter_after(self, page, cover_letter: str) -> bool:
        """Дописывает сопроводительное уже после отправленного отклика.

        Часть вакансий на HH откликается в один клик, без формы письма —
        зато после отклика он сам показывает «Добавить сопроводительное».
        Раньше агент этого не делал и отклик навсегда оставался пустым.
        """
        try:
            await asyncio.sleep(1.5)  # даём отрисоваться экрану после отклика
            link = page.locator(
                'text="Добавить сопроводительное"').or_(
                page.locator('text="Написать сопроводительное"')).first
            if not await link.is_visible(timeout=3000):
                return False

            print("✍️ Досылаю сопроводительное после отклика…")
            await link.click()
            await asyncio.sleep(1)

            field = await find_letter_field(page)
            if field is None:
                # Здесь письмо отправляется как сообщение в чат отклика
                field = page.locator('textarea:visible').first
                if not await field.is_visible(timeout=2000):
                    return False

            await field.fill(cover_letter)
            await asyncio.sleep(0.3)
            if not (await field.input_value()).strip():
                return False

            send = page.locator(
                'button[data-qa*="letter-send"]').or_(
                page.locator('button[data-qa*="chat-form-submit"]')).or_(
                page.locator('button:has-text("Отправить")')).first
            if not await send.is_visible(timeout=2000):
                return False
            await send.click()
            await asyncio.sleep(1.5)
            print("✅ Сопроводительное дослано после отклика")
            return True
        except Exception as e:
            print(f"⚠️ Не удалось дослать сопроводительное: {e}")
            return False

    async def check_chats(self, send_notification_func):
        # Вызывается сразу после поиска — без этой проверки агент шёл на hh.ru
        # уже после нажатия «Остановить».
        if control.should_stop():
            return
        print("Проверяю новые сообщения в чатах HH...")
        await self.page.goto("https://hh.ru/applicant/negotiations")
        await control.sleep_or_stop(3)
        
        # Находим список откликов с бейджем непрочитанных сообщений (надежный поиск через filter(has=...))
        chat_cards = await self.page.locator('div[data-qa="negotiations-item"]').filter(has=self.page.locator('span[data-qa="negotiations-item-badge"]')).all()
        
        for chat_card in chat_cards:
            
            title_loc = chat_card.locator('a[data-qa="negotiations-item-vacancy-link"]')
            title = await title_loc.inner_text() if await title_loc.is_visible() else "Неизвестно"
            
            # Переходим в чат
            chat_link = await title_loc.get_attribute("href")
            if chat_link:
                chat_page = await self.context.new_page()
                await Stealth().apply_stealth_async(chat_page)
                await chat_page.goto(f"https://hh.ru{chat_link}")
                await asyncio.sleep(3)
                
                # Получаем последнее сообщение
                messages = await chat_page.locator('div[data-qa="chat-message-text"]').all()
                if messages:
                    last_msg = await messages[-1].inner_text()
                    msg_id = f"{chat_link}_{len(messages)}" # Примитивный ID
                    
                    if not database.is_message_processed(msg_id):
                        database.add_processed_message(msg_id, chat_link, last_msg)
                        await send_notification_func(f"🔔 <b>Новое сообщение от работодателя!</b>\nВакансия: {title}\n\n<i>{last_msg}</i>\n<a href='https://hh.ru{chat_link}'>Перейти к чату</a>", kind="reply")
                
                await chat_page.close()

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
