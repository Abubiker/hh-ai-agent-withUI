import os
import re
import time
import asyncio
import hashlib
import random
import applog
import database
import control
import sites
import hh_session
from stats import Stats
from ai_analyzer import (is_vacancy_suitable, generate_cover_letter, active_style,
                          answer_employer_question, NO_DATA_SENTINEL)
from llm_providers import ProviderError
from settings import settings
from urllib.parse import quote_plus

from settings import user_file

# Файлы лежат в папке пользователя, а не рядом с кодом: внутри собранного
# .app соседняя папка временная и только для чтения.
#
# STATE_FILE — сессия hh.ru конкретно, оставлена ради обратной совместимости
# (это ровно тот путь, что был единственным до появления мультидоменности).
# Для остальных площадок и для кода, который должен работать с ЛЮБОЙ
# активной площадкой, используйте sites.state_file()/sites.active_site().
STATE_FILE = str(user_file("state.json"))
CAPTCHA_FILE = str(user_file("captcha.png"))

# Сколько раз пробовать откликнуться, пока hh не подтвердит отклик.
# Без предела вакансия возвращалась бы в обработку на каждом проходе выдачи,
# каждый раз тратя полный цикл модели.
MAX_RESPONSE_ATTEMPTS = 3

# Сколько ждать поле сопроводительного письма. Форма отклика — модалка
# с анимацией, поле подъезжает не сразу.
LETTER_FIELD_TIMEOUT = 10.0

# Сколько ждать первый ручной вход пользователя (СМС-код, капча и т.п.).
# Даём щедрый запас — торопить тут некого и незачем.
LOGIN_WAIT_TIMEOUT_MS = 600_000  # 10 минут

# page.mouse.move/wheel не принимают timeout (в отличие от локаторов) —
# если браузер завис на уровне протокола, такой вызов виснет НАВСЕГДА и
# вместе с ним весь цикл агента (см. отчёты о «замирании» после генерации
# письма). Оборачиваем в asyncio.wait_for, чтобы зависание стало обычной
# ошибкой вакансии, а не смертью всего сеанса.
MOUSE_ACTION_TIMEOUT = 10.0


def _trace(step: str):
    """Строка только в файловый agent.log (не в интерфейс) — детальный след
    по шагам внутри обработки вакансии, чтобы при зависании было видно,
    на каком именно вызове оно случилось (см. applog.py)."""
    applog.log(step, level="debug")


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
# Поля теста работодателя называются task_<id>_text и стоят в форме ПЕРЕД
# полем письма. Именно из-за них письмо уходило в ответ на первый вопрос
# теста, а сам отклик не создавался.
TEST_FIELD_PREFIX = "task_"

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


# Собираем вопросы теста работодателя (task_<id>_text) вместе с текстом
# самого вопроса — нужен для автоответа (см. answer_employer_questions).
# Подпись ищем тремя способами по убыванию надёжности: явная связка
# label[for=id] → aria-label/aria-labelledby → текст ближайшего контейнера.
# Если разметка hh.ru не совпадёт ни с одним из них, label останется пустым
# и вызывающий код честно откажется от автоответа (см. answer_employer_questions).
_COLLECT_TASK_FIELDS_JS = """
() => {
  const out = [];
  document.querySelectorAll('textarea[name^="task_"]').forEach((el, i) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const visible = r.width > 1 && r.height > 1 &&
                    cs.visibility !== 'hidden' && cs.display !== 'none';
    let label = '';
    if (el.id) {
      const lbl = document.querySelector(`label[for="${el.id}"]`);
      if (lbl) label = lbl.textContent.trim();
    }
    if (!label) {
      label = el.getAttribute('aria-label') || '';
      const by = el.getAttribute('aria-labelledby');
      if (!label && by) {
        const ref = document.getElementById(by);
        if (ref) label = ref.textContent.trim();
      }
    }
    if (!label) {
      const container = el.closest('[data-qa*="task"], [data-qa*="question"]') || el.parentElement;
      if (container) {
        const clone = container.cloneNode(true);
        clone.querySelectorAll('textarea, script, style').forEach(n => n.remove());
        label = clone.textContent.replace(/\\s+/g, ' ').trim();
      }
    }
    out.push({
      index: i, name: el.getAttribute('name') || '', id: el.id || '',
      visible, disabled: el.disabled || el.readOnly, label,
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
    usable = [t for t in areas
              if t["visible"] and not t["disabled"]
              and not t["name"].startswith(TEST_FIELD_PREFIX)]
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


async def answer_employer_questions(page, vacancy_title: str, vacancy_description: str,
                                     *, verbose: bool = False) -> bool:
    """Пытается ответить на текстовые вопросы теста работодателя (task_<id>_text)
    через ИИ и вписать ответы в форму.

    True  — вопросов нет (или все успешно отвечены и вписаны): вызывающий код
            продолжает обычный флоу (письмо, отправка).
    False — хотя бы один вопрос не удалось обработать (нет подписи, ИИ вернул
            NO_DATA, сбой модели, поле не приняло текст): вызывающий код
            должен целиком откатиться на сценарий «тест — вручную», БЕЗ
            частичного заполнения — hh.ru вряд ли примет форму с частью
            обязательных полей теста пустыми, а частичный автоответ рядом с
            пустыми полями хуже, чем честно отдать вакансию человеку целиком.
    """
    fields = await page.evaluate(_COLLECT_TASK_FIELDS_JS)
    usable = [f for f in fields if f["visible"] and not f["disabled"]]
    if not usable:
        if verbose:
            print("   поля теста в DOM есть, но ни одно не видимо/доступно")
        return False

    answers: dict[int, str] = {}
    for f in usable:
        if not f["label"]:
            if verbose:
                print(f"   не нашёл подпись вопроса (name={f['name']!r})")
            return False
        try:
            answer = await answer_employer_question(
                vacancy_title, vacancy_description, f["label"])
        except ProviderError as e:
            print(f"⚠️ Не удалось получить ответ на вопрос теста: {e}")
            applog.exc()
            return False
        if answer.strip().upper() == NO_DATA_SENTINEL:
            if verbose:
                print(f"   недостаточно данных для ответа: {f['label'][:80]!r}")
            return False
        answers[f["index"]] = answer

    # Вписываем только после того, как ВСЕ вопросы получили ответ — так
    # неудача на последнем вопросе не оставляет форму в наполовину
    # заполненном состоянии перед откатом на «вручную».
    for index, answer in answers.items():
        field = page.locator(f'textarea[name^="{TEST_FIELD_PREFIX}"]').nth(index)
        if not await fill_letter(field, answer):
            if verbose:
                print(f"   не удалось вписать ответ в поле #{index}")
            return False
    return True


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


async def wait_letter_field(page, timeout: float = LETTER_FIELD_TIMEOUT,
                            verbose: bool = False):
    """Ждёт появления поля письма, а не снимает мгновенный снимок страницы.

    Форма отклика открывается модалкой с анимацией, и поле письма нередко
    подъезжает через секунду-другую после выбора резюме. Разовая проверка
    попадала в это окно и решала, что поля нет вовсе, — вакансия уходила
    в пропуск с «не удалось приложить письмо», хотя поле просто ещё
    не отрисовалось.
    """
    deadline = time.monotonic() + timeout
    field = await find_letter_field(page, verbose=verbose)
    while field is None and time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        field = await find_letter_field(page)
    if field is not None and verbose:
        waited = timeout - max(0.0, deadline - time.monotonic())
        if waited > 0.5:
            print(f"   поле письма появилось через {waited:.1f} с")
    return field


async def open_letter_field(page, verbose: bool = True):
    """Раскрывает поле письма, если оно спрятано за кнопкой, и возвращает его."""
    field = await wait_letter_field(page, verbose=verbose)
    if field:
        return field

    # Поле часто скрыто за ссылкой «Написать сопроводительное»
    for selector in LETTER_TOGGLE_SELECTORS:
        try:
            toggle = page.locator(selector).first
            if await toggle.is_visible(timeout=800):
                await toggle.click()
                field = await wait_letter_field(page, verbose=verbose)
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

    HH обрезает сопроводительное до 2000 символов. Раньше лимит применялся
    только на запасном пути (type()) — быстрый путь (fill()) отправлял
    полный текст, и уведомление пользователю показывало не то, что реально
    приняла форма. Обрезаем один раз, до обеих попыток.
    """
    text = text[:2000]
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
        await field.type(text, delay=1)
        await asyncio.sleep(0.4)
        return bool((await field.input_value()).strip())
    except Exception as e:
        print(f"   не удалось вписать письмо: {e}")
        applog.exc()
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


async def diagnose_page(page, kind: str = "vacancy", host: str | None = None):
    """Разбирается, почему на странице нет описания вакансии.

    Исходный код считал капчей ЛЮБОЕ отсутствие описания, хотя чаще это
    архивная вакансия или редирект. Возвращает (код, человекочитаемая причина).
    Код: captcha | archived | not_found | redirect | unknown

    kind различает, какую страницу ждём: финальная проверка URL для вакансии
    ищет "{host}/vacancy/", а для резюме — "{host}/resume/". Без этого параметра
    любая успешно открывшаяся страница резюме считалась бы редиректом, потому
    что её адрес никогда не содержит "/vacancy/".

    host — домен площадки (hh.ru, hh.kz, ...), по умолчанию активная площадка
    из настроек. Явно передавайте host, когда страница открыта НЕ на активной
    площадке (например, ссылка из чата на другой домен группы).
    """
    host = host or sites.active_site()["host"]
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

        url_marker = f"{host}/vacancy/" if kind == "vacancy" else f"{host}/resume/"
        if url_marker not in url:
            return "redirect", f"редирект на {url[:80]}"
        return "unknown", f"описание не найдено, заголовок: {title[:60]!r}"
    except Exception as e:
        return "unknown", f"не удалось разобрать страницу: {e}"


async def submit_captcha_solution(page, solution: str):
    """Вводит решение капчи и отправляет форму — общая механика для основного
    цикла поиска (HHClient.search_and_apply) и разового отклика по ссылке
    (quick_apply.apply_to_vacancy). Раньше была продублирована в обоих местах
    почти дословно, с разными по значению, но не по смыслу паузами — здесь
    один источник правды, и обе стороны ведут себя одинаково «по-человечески»
    перед антибот-защитой hh.ru.

    Ничего не проверяет и не решает сама — вызывающий код после неё сам
    смотрит, появилось ли описание вакансии (капча могла быть решена неверно,
    или это вообще не капча, а Cloudflare-галочка)."""
    input_field = page.locator('input[type="text"]').first
    if await input_field.is_visible():
        await input_field.click()
        await asyncio.sleep(random.uniform(0.5, 1.2))

        for char in solution:
            if char == " ":
                await asyncio.sleep(random.uniform(0.6, 1.5))  # Медленный пробел между словами
            await input_field.type(char, delay=random.randint(150, 400))  # Человечный ввод

        await asyncio.sleep(random.uniform(1.0, 2.5))
        # На форме капчи есть кнопка «Отправить»; Enter в React-форме её не
        # сабмитит, и верно введённый код никуда не уходит — цикл решения
        # капчи крутился бы, запрашивая новую картинку.
        submit_captcha = page.locator(
            'button[type="submit"]:visible, button:has-text("Отправить"):visible'
        ).first
        try:
            if await submit_captcha.is_visible():
                await submit_captcha.click()
            else:
                await input_field.press('Enter')
        except Exception:
            await input_field.press('Enter')
        await asyncio.sleep(6)  # Ждём прогрузки после ввода
    else:
        # Поля ввода нет — возможно, это галочка Cloudflare, или капча уже
        # решена в другом окне. Обновляем страницу — проверить, не снят ли бан.
        print("Поле ввода не найдено. Обновляем страницу...")
        await page.reload()
        await asyncio.sleep(4)


async def response_confirmed(page, href: str) -> bool:
    """Спрашивает у самого hh.ru, создан ли отклик на самом деле.

    Признак: на вакансии с существующим откликом hh убирает кнопку
    «Откликнуться». Само отсутствие кнопки ничего не доказывает — её нет
    и на капче, и на архивной вакансии, поэтому сначала убеждаемся, что
    перед нами действительно страница вакансии.

    Свободная функция (не метод HHClient) — не использует self, поэтому
    также переиспользуется из quick_apply.py для одиночного отклика из чата.
    """
    try:
        await page.goto(href.split("?")[0], wait_until="domcontentloaded")
        await page.locator('div[data-qa="vacancy-description"]').wait_for(
            state="visible", timeout=20000)
    except Exception as e:
        # Не смогли посмотреть страницу — подтверждения нет. Лучше повторить
        # попытку, чем записать несуществующий отклик как успешный.
        print(f"⚠️ Не удалось проверить статус отклика: {e}")
        applog.exc()
        return False

    return await page.locator('a[data-qa="vacancy-response-link-top"]').count() == 0


async def attach_letter_after(page, cover_letter: str) -> bool:
    """Дописывает сопроводительное уже после отправленного отклика.

    Часть вакансий на HH откликается в один клик, без формы письма —
    зато после отклика он сам показывает «Добавить сопроводительное».
    Раньше агент этого не делал и отклик навсегда оставался пустым.

    Свободная функция — см. response_confirmed() выше, та же причина.
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

        field = await wait_letter_field(page)
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


class HHClient:
    def __init__(self, sinks=None, site=None):
        self.site = site or sites.active_site()
        self.state_file = sites.state_file(self.site)
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.stats = Stats.load()
        # Память на сеанс: какие вакансии уже встречались (для раннего обрыва
        # пагинации на повторных проверках) и о каких пропусках уже сообщали
        # (чтобы не спамить лог одними и теми же строками каждую проверку).
        self._seen_ids = set()
        # Загружается из agent.db (таблица seen_skips), а не только из памяти:
        # иначе после перезапуска приложения агент заново печатал те же ~28
        # строк «⏩ Пропускаю» на каждую уже виденную отсеянную вакансию.
        # Сам стоп-фильтр всё равно проверяет каждую вакансию каждый раз —
        # это подавляет только повторный лог, не проверку.
        self._skip_logged = database.load_seen_skips()
        # Получатели уведомлений. Нужны в том числе для ввода капчи: её может
        # принять окно приложения или Telegram, смотря что настроено.
        if sinks is None:
            from notify_sinks import build_sinks
            sinks = build_sinks(telegram=control.telegram_enabled)
        self.sinks = sinks
        # Подстраховка к авто-детекту первого входа (см. login_if_needed):
        # интерфейс может выставить это событие сам, если пользователь нажал
        # «Я вошёл — сохранить сейчас», не дожидаясь, пока страница сама
        # покажет признак входа. В CLI не используется и остаётся None.
        self.login_confirm_event: asyncio.Event | None = None

    async def start(self):
        # Запуск в headless=False для того, чтобы в первый раз пользователь мог войти (ввести смс/пароль),
        # либо полностью headless, если сессия этой площадки уже сохранена.
        headless = os.path.exists(self.state_file)
        self.playwright, self.browser, self.context = await hh_session.open_session(
            self.site, headless=headless, require_login=False)
        self.page = await hh_session.new_stealth_page(self.context)

    async def login_if_needed(self):
        base = sites.base_url(self.site)
        print(f"Переходим на {self.site['host']} для проверки авторизации...")
        # domcontentloaded, а не load: hh держит websocket чатов и аналитику,
        # событие load может не наступить вовсе и уронить весь запуск по таймауту.
        await self.page.goto(f"{base}/", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Ждём не networkidle (по той же причине он может не наступить никогда),
        # а конкретный маркер отрисованной шапки: ссылку на резюме у авторизованного
        # либо кнопку входа у гостя.
        try:
            await self.page.locator(
                'a[href*="/applicant/resumes"], a:has-text("Войти"), button:has-text("Войти")'
            ).first.wait_for(timeout=30000)
        except Exception:
            print(f"⚠️ Шапка {self.site['host']} не отрисовалась за 30 с — проверяю страницу как есть.")
        await asyncio.sleep(2)

        # Положительный маркер входа — ссылка на резюме соискателя. Кнопка
        # "Войти" использовалась раньше как единственный признак, но её текст
        # зависит от языка интерфейса, а язык площадки может быть не русским.
        resumes_link = self.page.locator('a[href*="/applicant/resumes"]')
        if await resumes_link.count():
            print("Уже авторизованы (найдена ссылка на резюме).")
            return True

        # Вторичное подтверждение на русском — для решения "удалить протухший
        # файл сессии или создать новый".
        login_link = self.page.locator('a:has-text("Войти")')
        login_button = self.page.locator('button:has-text("Войти")')

        if not await login_link.count() and not await login_button.count():
            print("Уже авторизованы (кнопка 'Войти' не найдена).")
            return True

        if os.path.exists(self.state_file):
            os.remove(self.state_file)
            print(f"❌ Файл сессии {self.site['host']} недействителен. Я его удалил.")
            print("Пожалуйста, перезапустите скрипт (python main.py), чтобы открылось окно браузера для входа.")
            return False

        print("=========================================")
        print("❗ НУЖНА АВТОРИЗАЦИЯ ❗")
        print(f"В открывшемся браузере войдите в свой аккаунт {self.site['host']}.")
        print("Дальше не нужно ничего нажимать здесь — как только вход будет виден на странице, работа продолжится сама.")
        print("=========================================")

        try:
            # Раньше здесь ждали нажатия Enter в терминале. В собранном .app
            # терминала нет: input() падает по EOFError почти мгновенно, из-за
            # чего окно браузера открывалось и тут же закрывалось, а вход
            # никогда не завершался. Вместо этого ждём сам факт входа прямо
            # на странице — работает одинаково из консоли и из приложения.
            #
            # login_confirm_event — подстраховка поверх авто-детекта: если
            # интерфейс выставил его (пользователь нажал «Я вошёл — сохранить
            # сейчас»), сохраняем сразу, не дожидаясь и не требуя, чтобы
            # сработал именно локатор — на случай, если разметку hh изменят.
            detect_task = asyncio.ensure_future(
                self.page.locator('a[href*="/applicant/resumes"]').first.wait_for(
                    timeout=LOGIN_WAIT_TIMEOUT_MS))
            confirm_task = (asyncio.ensure_future(self.login_confirm_event.wait())
                            if self.login_confirm_event is not None else None)
            waiters = [detect_task] + ([confirm_task] if confirm_task else [])

            done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if confirm_task not in done:
                detect_task.result()  # None при успехе, бросает при таймауте

            print("⏳ Сохраняем сессию...")
            await asyncio.sleep(2) # На всякий случай даем странице загрузиться
            await self.context.storage_state(path=self.state_file)
            print("✅ Авторизация успешна, состояние сохранено!")
            return True
        except Exception as e:
            print(f"❌ Не дождались входа в аккаунт: {e}")
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
                url = (f"{sites.base_url(self.site)}/search/vacancy?text={quote_plus(query)}"
                       f"{field}&order_by=publication_time"
                       f"{exp}{config['params']}")
                await self.page.goto(url, wait_until="domcontentloaded")
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

                        if not job_id:
                            continue
                        if database.is_job_applied(job_id):
                            # Молча continue — токенов это не стоит (проверка ДО
                            # модели), но пользователю казалось, что агент
                            # «обрабатывает одни и те же вакансии заново». Счётчик
                            # делает это видимым в итоге цикла (см. ui_app.py).
                            self.stats.bump("db_skipped")
                            continue

                        # Вакансия встречена впервые за сеанс — даже если её сейчас
                        # отсеет стоп-фильтр, страница считается «свежей» и пагинация
                        # продолжится: релевантные новые могут быть глубже. На повторных
                        # проверках уже виденное не считается — страницы без новинок
                        # обрываются сразу.
                        if job_id not in self._seen_ids:
                            self._seen_ids.add(job_id)
                            self.stats.bump("fresh")
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
                                database.mark_skip_seen(job_id)
                                self.stats.bump("hard_skipped")
                                print(f"⏩ Пропускаю (не тот грейд/профессия — '{hit}'): {title}")
                            continue

                        self.stats.bump("viewed")
                        print(f"👁️ Открываем вакансию: {title}")
                        page = await hh_session.new_stealth_page(self.context)
                        try:
                            await page.goto(href, wait_until="domcontentloaded")
                            await asyncio.sleep(2)
                            # Проверка VPN может подменить и страницу вакансии
                            if await handle_vpn_check(page):
                                await page.goto(href, wait_until="domcontentloaded")
                                await asyncio.sleep(2)

                            desc_loc = page.locator('div[data-qa="vacancy-description"]')
                            # Если описания нет — разбираемся, ЧТО именно на странице.
                            # Раньше любое отсутствие описания считалось капчей.
                            while not await desc_loc.is_visible():
                                if control.should_stop():
                                    print("⏹️ Получен сигнал остановки — прерываю обработку вакансии.")
                                    return
                                reason_code, reason_text = await diagnose_page(page, host=self.site["host"])

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
                                    await submit_captcha_solution(page, solution)

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
                                    applog.exc()
                                    raise SkipVacancy("captcha_error")
                            description = await desc_loc.inner_text()

                            # Анализ ИИ
                            if await is_vacancy_suitable(title, description):
                                # За классификатором внутри одной вакансии могут идти
                                # ещё генерация письма, резюме-пикер, тест работодателя,
                                # капча и сам клик по отправке — десятки секунд без единой
                                # проверки стопа раньше. Проверяем ЗДЕСЬ (до письма — не
                                # тратим лишний вызов модели) и ещё раз прямо перед кликом
                                # отправки ниже — это и есть точка, где «Стоп» обязан
                                # реально остановить агента, а не просто отложить это на
                                # следующую вакансию. Вакансию в базу НЕ пишем — как и на
                                # любой другой стоп-проверке, следующий сеанс рассмотрит
                                # её заново с нуля.
                                if control.should_stop():
                                    print("⏹️ Получен сигнал остановки — прерываю обработку вакансии.")
                                    return
                                self.stats.bump("ai_pass")
                                print(f"✨ Вакансия подходит: {title}")

                                # Письмо пишется ~12 секунд — без этой строки в логе
                                # было полное затишье, интерфейсу нечем показать прогресс.
                                print(f"✍️ Пишу сопроводительное — {title}")
                                # Стиль читаем ЗАРАНЕЕ (а не отдаём генератору выбирать
                                # молча), чтобы записать его вместе с откликом в БД —
                                # иначе конверсию по стилям потом не с чем сравнивать.
                                letter_style = active_style()
                                _trace(f"letter: генерация начата ({title})")
                                cover_letter = await generate_cover_letter(
                                    title, description, style=letter_style)
                                self.stats.bump("letters")
                                _trace("letter: получено, ищу кнопку отклика")

                                # Пробуем откликнуться
                                apply_btn = page.locator('a[data-qa="vacancy-response-link-top"]').first
                                apply_visible = await apply_btn.is_visible()
                                _trace(f"apply: кнопка видима={apply_visible}")
                                if apply_visible:
                                    # Имитируем поведение человека перед откликом.
                                    # mouse.move/wheel не поддерживают timeout — на завис
                                    # браузера/протокола это раньше вешало весь цикл агента.
                                    _trace("apply: имитация поведения — движение мыши")
                                    await asyncio.wait_for(
                                        page.mouse.move(random.randint(100, 700), random.randint(100, 500)),
                                        timeout=MOUSE_ACTION_TIMEOUT)
                                    await asyncio.wait_for(
                                        page.mouse.wheel(0, random.randint(200, 600)),
                                        timeout=MOUSE_ACTION_TIMEOUT)
                                    await asyncio.sleep(random.uniform(0.8, 1.5))
                                    await asyncio.wait_for(
                                        page.mouse.wheel(0, random.randint(-200, 100)),
                                        timeout=MOUSE_ACTION_TIMEOUT)
                                    await asyncio.sleep(random.uniform(0.5, 1.0))

                                    _trace("apply: клик по кнопке отклика")
                                    await apply_btn.click()
                                    # Даем время на открытие попапа ИЛИ загрузку новой страницы отклика
                                    await asyncio.sleep(3)
                                    _trace("apply: попап/страница осели — шаг 0, резюме")

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
                                        applog.exc()

                                    _trace("apply: шаг 0.5 — проверка теста работодателя")
                                    # Шаг 0.5: тест работодателя. Его поля называются
                                    # task_<id>_text и стоят в форме ПЕРЕД полем письма,
                                    # поэтому письмо уходило в ответ на первый вопрос
                                    # теста, а отклик не создавался вовсе. Сначала пробуем
                                    # ответить автоматически (только текстовые вопросы) —
                                    # см. answer_employer_questions; если не вышло, тест
                                    # по-прежнему уходит человеку целиком, как раньше.
                                    if await page.locator(f'textarea[name^="{TEST_FIELD_PREFIX}"]').count() > 0:
                                        if await answer_employer_questions(page, title, description):
                                            self.stats.bump("questions_answered")
                                            print(f"📝 Тест работодателя пройден автоматически: {title}")
                                        else:
                                            self.stats.bump("needs_manual")
                                            database.add_applied_job(job_id, title, href)
                                            print(f"📝 Вакансия с тестом работодателя, нужен ручной отклик: "
                                                  f"{title} — {href}")
                                            import html as _html
                                            await send_notification_func(
                                                f"📝 <b>Тестовое задание</b>: <a href='{href}'>{title}</a>\n\n"
                                                f"<i>Работодатель просит ответить на вопросы — откликнитесь "
                                                f"вручную.</i>\n\n"
                                                f"Сопроводительное письмо уже готово:\n\n"
                                                f"<i>{_html.escape(cover_letter)}</i>",
                                                kind="applied")
                                            raise SkipVacancy("employer_test")

                                    # Шаг 1-2: находим поле письма (при необходимости раскрыв его)
                                    # и убеждаемся, что текст реально в него попал.
                                    _trace("apply: шаг 1-2 — поиск поля письма")
                                    letter_sent = False
                                    letter_field = await open_letter_field(page)
                                    _trace(f"apply: поле письма найдено={letter_field is not None}")
                                    if letter_field is None:
                                        print(f"⚠️ Поле сопроводительного не найдено: {title}")
                                        # Печатаем, что вообще есть на странице: по этому выводу
                                        # видно, как HH назвал поле, если разметка изменилась.
                                        await dump_textareas(page, title)
                                    else:
                                        letter_sent = await fill_letter(letter_field, cover_letter)
                                        _trace(f"apply: письмо вписано={letter_sent}")
                                        if letter_sent:
                                            print("   ✅ письмо вписано в форму отклика")
                                        else:
                                            print(f"⚠️ Письмо не удержалось в поле: {title}")

                                    # Без письма отклики часто не рассматривают, поэтому по
                                    # умолчанию пустой отклик не отправляем: вакансия уходит
                                    # в уведомление вместе с готовым письмом — откликнуться
                                    # вручную дешевле, чем сжечь вакансию впустую.
                                    if not letter_sent and settings.require_letter:
                                        self.stats.bump("skipped_no_letter")
                                        database.add_applied_job(job_id, title, href)
                                        import html as _html
                                        await send_notification_func(
                                            f"⚠️ <b>Не смог приложить письмо</b>: <a href='{href}'>{title}</a>\n"
                                            f"Отклик не отправлен. Письмо готово — можно откликнуться вручную:\n\n"
                                            f"<i>{_html.escape(cover_letter)}</i>", kind="error")
                                        print(f"⏭️ Отклик не отправлен (нет письма): {title}")
                                        raise SkipVacancy("no_letter")

                                    # Шаг 3: отправка отклика
                                    _trace("apply: шаг 3 — поиск кнопки отправки")
                                    submit_btn = await find_submit_button(page)
                                    _trace(f"apply: кнопка отправки найдена={submit_btn is not None}")
                                    if submit_btn is not None:
                                        if control.should_stop():
                                            # Форма уже заполнена, но НЕ отправлена — «Стоп»
                                            # должен реально останавливать, а не дожимать
                                            # последний клик, начатый до команды.
                                            print(f"⏹️ Остановка перед отправкой — отклик НЕ отправлен: {title}")
                                            return
                                        await submit_btn.click() # РЕАЛЬНЫЙ ОТКЛИК
                                        _trace("apply: клик по отправке сделан, жду закрытия формы")
                                        # Ждём закрытия формы, а не спим вслепую: уйти со
                                        # страницы раньше — значит оборвать сам запрос отклика.
                                        try:
                                            await submit_btn.wait_for(state="hidden", timeout=20000)
                                        except Exception:
                                            pass
                                        await asyncio.sleep(2)

                                        # Если формы письма не было (HH отправляет такие отклики
                                        # сразу по клику), он сам предлагает дослать письмо —
                                        # пользуемся этим, чтобы вакансия не осталась пустой.
                                        if not letter_sent:
                                            letter_sent = await attach_letter_after(page, cover_letter)

                                        # Клик ≠ отправленный отклик: hh может потребовать
                                        # доп. шаг или молча ничего не сделать. Спрашиваем сам
                                        # сайт, иначе несуществующий отклик попадает в базу как
                                        # успешный и вакансия теряется навсегда.
                                        _trace("apply: проверяю подтверждение отклика сайтом")
                                        if not await response_confirmed(page, href):
                                            attempts = database.bump_failed_response(job_id, title)
                                            self.stats.bump("apply_failed")
                                            print(f"❗ Отклик НЕ подтверждён сайтом (попытка {attempts}): {title}")
                                            if attempts >= MAX_RESPONSE_ATTEMPTS:
                                                # Хватит: помечаем обработанной, иначе вакансия
                                                # будет возвращаться при каждом проходе выдачи.
                                                database.add_applied_job(job_id, title, href)
                                                await send_notification_func(
                                                    f"❗ Отклик так и не прошёл ({attempts} попытки): "
                                                    f"<a href='{href}'>{title}</a>\n\n"
                                                    f"<i>{self.site['host']} не подтвердил отправку — нужен ручной отклик.</i>",
                                                    kind="error")
                                            raise SkipVacancy("not_confirmed")

                                        database.add_applied_job(
                                            job_id, title, href,
                                            style=letter_style if letter_sent else None)
                                        self.stats.bump("applied")
                                        if not letter_sent:
                                            self.stats.bump("applied_no_letter")

                                        import html
                                        safe_cover_letter = html.escape(cover_letter)

                                        if letter_sent:
                                            await send_notification_func(f"✅ Успешный отклик: <a href='{href}'>{title}</a>\n\n<b>Письмо:</b>\n<i>{safe_cover_letter}</i>", kind="applied")
                                            print(f"✅ Отклик отправлен с письмом: {title}")
                                        else:
                                            await send_notification_func(f"✅ Отклик <b>без письма</b>: <a href='{href}'>{title}</a>\n\n<i>(HH не дал приложить сопроводительное к этой вакансии)</i>", kind="applied")
                                            print(f"✅ Отклик отправлен БЕЗ письма: {title}")
                                    else:
                                        # Форма открылась, но кнопки отправки в ней нет. Без
                                        # счётчика вакансия молча возвращалась в обработку на
                                        # каждом проходе и каждый раз тратила цикл модели.
                                        attempts = database.bump_failed_response(job_id, title)
                                        self.stats.bump("apply_failed")
                                        print(f"❗ Кнопка отправки не найдена (попытка {attempts}): {title}")
                                        await dump_buttons(page)
                                        if attempts >= MAX_RESPONSE_ATTEMPTS:
                                            database.add_applied_job(job_id, title, href)
                                else:
                                    # Кнопки нет — обычно потому, что отклик уже есть. Но так же
                                    # выглядит недогруженная страница, поэтому не гадаем, а
                                    # спрашиваем hh.
                                    if await response_confirmed(page, href):
                                        self.stats.bump("already")
                                        print(f"Отклик уже был отправлен ранее: {title}")
                                        database.add_applied_job(job_id, title, href)
                                    else:
                                        attempts = database.bump_failed_response(job_id, title)
                                        print(f"❗ Кнопки отклика нет, и отклика нет (попытка {attempts}): {title}")
                                        if attempts >= MAX_RESPONSE_ATTEMPTS:
                                            database.add_applied_job(job_id, title, href)
                            else:
                                self.stats.bump("ai_reject")
                                print(f"❌ ИИ отклонил: {title}")
                                database.add_applied_job(job_id, title, href) # Добавляем, чтобы больше не анализировать

                        except SkipVacancy as skip:
                            # Эти случаи уже посчитаны своими счётчиками — иначе
                            # вакансия попала бы сразу в два.
                            if str(skip) not in ("no_letter", "employer_test", "not_confirmed"):
                                self.stats.bump("skipped_page")
                        except Exception as e:
                            # Сюда попадает и сбой связи с моделью (is_vacancy_suitable бросает
                            # исключение). Вакансию НЕ записываем в базу — вернёмся к ней позже.
                            print(f"Ошибка при обработке вакансии {title}: {e}")
                            applog.exc()
                        finally:
                            await page.close()
                    
                    # Выдача отсортирована по дате публикации: если на странице не было
                    # ни одной невиданной вакансии, глубже — только ещё более старые.
                    # Обрываем пагинацию и не тратим время на пустые страницы.
                    if new_on_page == 0:
                        break

                    # Лимит страниц на запрос. Ноль — без предела: агент идёт вглубь,
                    # пока не кончатся новые вакансии, не истечёт время сеанса или
                    # его не остановят. На повторных проходах это почти бесплатно —
                    # выше сработает выход по «ни одной новой вакансии на странице».
                    limit = settings.max_pages_per_query
                    if limit and page_num >= limit:
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

    async def check_chats(self, send_notification_func):
        # Вызывается сразу после поиска — без этой проверки агент шёл на hh.ru
        # уже после нажатия «Остановить».
        if control.should_stop():
            return
        print("Проверяю новые сообщения в чатах HH...")
        base = sites.base_url(self.site)
        await self.page.goto(f"{base}/applicant/negotiations", wait_until="domcontentloaded")
        await control.sleep_or_stop(3)
        
        # Находим список откликов с бейджем непрочитанных сообщений (надежный поиск через filter(has=...))
        chat_cards = await self.page.locator('div[data-qa="negotiations-item"]').filter(has=self.page.locator('span[data-qa="negotiations-item-badge"]')).all()
        
        for chat_card in chat_cards:
            # Без этой проверки много чатов с бейджем = агент продолжает
            # открывать страницу за страницей уже после команды «Стоп» —
            # тот же класс бага, что и в самом поиске (см. should_stop() в
            # search_and_apply), просто в отдельном методе с отдельным циклом.
            if control.should_stop():
                return

            title_loc = chat_card.locator('a[data-qa="negotiations-item-vacancy-link"]')
            title = await title_loc.inner_text() if await title_loc.is_visible() else "Неизвестно"

            # Переходим в чат
            chat_link = await title_loc.get_attribute("href")
            if chat_link:
                chat_page = await hh_session.new_stealth_page(self.context)
                try:
                    await chat_page.goto(f"{base}{chat_link}", wait_until="domcontentloaded")
                    await asyncio.sleep(3)

                    # Получаем последнее сообщение
                    messages = await chat_page.locator('div[data-qa="chat-message-text"]').all()
                    if messages:
                        last_msg = await messages[-1].inner_text()
                        # Хеш ТЕКСТА последнего сообщения — не только позиции: id
                        # вида f"{chat_link}_{len(messages)}" ломался, если число
                        # сообщений менялось не так, как ожидалось (история на
                        # hh.ru может подрезаться подгрузкой). Но и чистый хеш без
                        # счётчика даёт свой сбой: один и тот же текст от
                        # работодателя дважды подряд («Ок», «Ждём») получает
                        # одинаковый id и второе уведомление молча теряется.
                        # Счётчик + хеш вместе: расхождение счётчика при подрезке
                        # истории даст лишнее повторное уведомление (терпимо),
                        # а не потерянное сообщение (не терпимо) — та же
                        # асимметрия, ради которой хеш добавляли изначально.
                        msg_id = (f"{chat_link}_{len(messages)}_"
                                  f"{hashlib.sha256(last_msg.encode('utf-8')).hexdigest()[:16]}")

                        if not database.is_message_processed(msg_id):
                            database.add_processed_message(msg_id, chat_link, last_msg)
                            await send_notification_func(f"🔔 <b>Новое сообщение от работодателя!</b>\nВакансия: {title}\n\n<i>{last_msg}</i>\n<a href='{base}{chat_link}'>Перейти к чату</a>", kind="reply")
                finally:
                    await chat_page.close()

    async def stop(self):
        # safe_close — тот же класс защиты, что и при запуске (см.
        # hh_session.BROWSER_LAUNCH_TIMEOUT): если браузер уже завис, само
        # закрытие может зависнуть так же, без таймаута на cleanup.
        if self.browser:
            await hh_session.safe_close(self.browser.close(), "браузер")
        if self.playwright:
            await hh_session.safe_close(self.playwright.stop(), "playwright")
