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


async def handle_vpn_check(page) -> bool:
    """HH подменяет страницу проверкой «VPN мешает работе сайта» (/vpncheeck).
    Именно это раньше принималось за капчу. Жмём «Я не использую VPN».

    Возвращает True, если проверка была и её удалось пройти.
    """
    if "vpncheeck" not in page.url:
        return False

    print("🔒 HH показал проверку VPN — нажимаю «Я не использую VPN»...")
    for attempt in (1, 2):
        try:
            btn = page.locator('text="Я не использую VPN"').first
            await btn.click(timeout=5000)
            await asyncio.sleep(random.uniform(2.5, 4.0))
            if "vpncheeck" not in page.url:
                print("   ✅ Проверка пройдена, продолжаю.")
                return True
        except Exception:
            pass
        if attempt == 1:
            await asyncio.sleep(3)

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
        for query in settings.search_queries:
            if control.should_stop():
                print("⏹️ Получен сигнал остановки — прерываю поиск.")
                return
            print(f"\n======================================")
            print(f"🔍 Поиск по запросу: {query}")
            print(f"======================================")

            # Регионы и режимы поиска задаются в настройках
            search_configs = settings.regions

            for config in search_configs:
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
                await asyncio.sleep(3)
                await handle_vpn_check(self.page)
                page_num = 1
                while True:
                    print(f"📄 Парсим страницу {page_num} по запросу '{query}' ({config['name']})...")
                    vacancies = await self.page.locator('a[data-qa="serp-item__title"]').all()
                
                    # Собираем ссылки заранее, чтобы избежать ошибки Detached Node при долгом парсинге
                    links_to_process = []
                    for v in vacancies:
                        href = await v.get_attribute("href")
                        title = await v.inner_text()
                        if href:
                            links_to_process.append((title, href))
                        
                    for title, href in links_to_process:
                        # Парсим ID вакансии из URL (https://hh.ru/vacancy/123456?...)
                        job_id = None
                        if "vacancy/" in href:
                            job_id = href.split("vacancy/")[1].split("?")[0]
                    
                        if not job_id or database.is_job_applied(job_id):
                            # print(f"Пропускаем (уже обработано): {title}") # Раскомментировать, если нужно видеть все пропуски
                            continue

                        if control.should_stop():
                            print("⏹️ Получен сигнал остановки — прерываю поиск.")
                            return

                        # Жёсткий фильтр по названию — ДО открытия страницы: названия
                        # хватает, а загрузка вакансии стоит ~4 секунды на каждую.
                        hit = find_stop_word(title)
                        if hit:
                            self.stats.hard_skipped += 1
                            print(f"⏩ Пропускаем (Неподходящий грейд/профессия — '{hit}'): {title}")
                            # В базу НЕ пишем: проверка названия бесплатная, зато правки
                            # списка стоп-слов подействуют и на уже виденные вакансии.
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
                                
                                    # Шаг 1: Ищем кнопку "Написать/Добавить сопроводительное" (если поле изначально скрыто)
                                    toggle_btn = page.locator('[data-qa*="letter-toggle"]').or_(
                                        page.locator('text="Написать сопроводительное"')
                                    ).or_(
                                        page.locator('text="Добавить сопроводительное"')
                                    ).first
                                    if await toggle_btn.is_visible():
                                        try:
                                            await toggle_btn.click()
                                            await asyncio.sleep(1)
                                        except:
                                            pass
                                
                                    # Шаг 2: Ищем ЛЮБОЕ многострочное поле (textarea) и ждем его появления (до 3 сек)
                                    letter_sent = False
                                    try:
                                        letter_textarea = page.locator('textarea').first
                                        await letter_textarea.wait_for(state="visible", timeout=3000)
                                        await letter_textarea.fill(cover_letter)
                                        letter_sent = True
                                    except:
                                        print(f"⚠️ Не удалось найти видимое поле (textarea) для письма: {title}")
                                    
                                    # Шаг 3: Отправка отклика (ищем любую видимую кнопку отправки)
                                    submit_btn = page.locator('button[data-qa*="vacancy-response-submit"]:visible').first
                                    if await submit_btn.is_visible():
                                        await submit_btn.click() # РЕАЛЬНЫЙ ОТКЛИК
                                        await asyncio.sleep(2)

                                        database.add_applied_job(job_id, title, href)
                                        self.stats.applied += 1

                                        import html
                                        safe_cover_letter = html.escape(cover_letter)

                                        if letter_sent:
                                            await send_notification_func(f"✅ Успешный отклик: <a href='{href}'>{title}</a>\n\n<b>Письмо:</b>\n<i>{safe_cover_letter}</i>", kind="applied")
                                        else:
                                            await send_notification_func(f"✅ Отклик без письма: <a href='{href}'>{title}</a>\n\n<i>(Работодатель отключил возможность отправки писем для этой вакансии)</i>", kind="applied")
                                        print(f"✅ Отклик отправлен: {title}")
                                    else:
                                        self.stats.apply_failed += 1
                                        print(f"⚠️ Не нашёл кнопку отправки отклика: {title}")
                                else:
                                    self.stats.already += 1
                                    print(f"Кнопка отклика не найдена (возможно, уже откликались): {title}")
                                    database.add_applied_job(job_id, title, href)
                            else:
                                self.stats.ai_reject += 1
                                print(f"❌ ИИ отклонил: {title}")
                                database.add_applied_job(job_id, title, href) # Добавляем, чтобы больше не анализировать

                        except SkipVacancy:
                            self.stats.skipped_page += 1  # штатный пропуск, уже залогирован
                        except Exception as e:
                            # Сюда попадает и сбой связи с моделью (is_vacancy_suitable бросает
                            # исключение). Вакансию НЕ записываем в базу — вернёмся к ней позже.
                            print(f"Ошибка при обработке вакансии {title}: {e}")
                        finally:
                            await page.close()
                    
                    # Лимит страниц на запрос, чтобы успеть пройтись по всем запросам
                    # из настроек, а не закопаться в первом же.
                    if page_num >= settings.max_pages_per_query:
                        print(f"📑 Разобрано {page_num} стр. — лимит на запрос, иду дальше.")
                        break

                    # После того как все вакансии на странице обработаны, проверяем кнопку "Дальше"
                    next_btn = self.page.locator('a[data-qa="pager-next"]')
                    if await next_btn.count() > 0 and await next_btn.is_visible():
                        print("➡️ Переходим на следующую страницу...")
                        await next_btn.click()
                        await asyncio.sleep(4)
                        page_num += 1
                    else:
                        print("🛑 Больше страниц нет, переходим к следующему запросу.")
                        break

    async def check_chats(self, send_notification_func):
        print("Проверка новых сообщений в чатах HH...")
        await self.page.goto("https://hh.ru/applicant/negotiations")
        await asyncio.sleep(3)
        
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
