import asyncio
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command
from settings import settings
import control

dp = Dispatcher()

# Кэш Bot по токену, а не module-level константа: settings.tg_bot_token
# читается заново на каждый вызов get_bot(), как и у llm_providers.
# get_provider(). Раньше TG_BOT_TOKEN/TG_USER_ID/bot вычислялись РОВНО ОДИН
# РАЗ при первом импорте модуля — если пользователь настраивал Telegram
# ПОСЛЕ этого (мастер первого запуска в main.py импортирует tg_bot до своего
# запуска; в окне — любой более ранний вызов notify/test_notification), все
# последующие сообщения молча уходили в «токен не задан» до перезапуска
# приложения.
_bot_cache: dict[str, str | Bot | None] = {"token": None, "bot": None}


def get_bot() -> Bot | None:
    """Бот для текущего токена из настроек. None, если токен не задан —
    Bot("") падает с ошибкой валидации, а приложение должно спокойно
    работать и без настроенного Telegram."""
    token = settings.tg_bot_token
    if not token:
        return None
    if _bot_cache["token"] != token:
        _bot_cache["bot"] = Bot(token=token)
        _bot_cache["token"] = token
    return _bot_cache["bot"]


def is_configured() -> bool:
    """Есть ли токен бота — для UI (кнопка «Проверить», состояние вкладки)."""
    return bool(settings.tg_bot_token)


async def send_notification(text: str):
    """Отправляет уведомление пользователю."""
    # Режим без Telegram: никаких сетевых обращений, только консоль.
    # HTML-разметку вырезаем, иначе в терминале мешанина из тегов.
    if not control.telegram_enabled:
        import re
        plain = re.sub(r"<[^>]+>", "", text)
        print(f"[отчёт] {plain}")
        return

    bot = get_bot()
    user_id = settings.tg_user_id
    if not bot or not user_id:
        print("ОШИБКА: Не настроен Telegram (нет токена или ID). Уведомление:")
        print(text)
        return

    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при отправке сообщения в TG: {e}")

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if str(message.from_user.id) == settings.tg_user_id:
        await message.answer("Привет! Я ваш ИИ-агент для поиска работы на HH.ru. Я буду присылать сюда уведомления.\n\nКоманды:\n/stop — остановить агента после текущей вакансии.")
    else:
        await message.answer(f"Извините, у вас нет доступа к этому боту.\nВаш ID: <code>{message.from_user.id}</code>\nУкажите его в настройках приложения (раздел «Уведомления») и перезапустите агента.")

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    """Мягкая остановка агента по команде из Telegram."""
    if str(message.from_user.id) != settings.tg_user_id:
        return
    control.request_stop()
    await message.answer("🛑 Принял. Останавливаюсь после текущей вакансии и пришлю итоговую статистику.")

captcha_event = asyncio.Event()
captcha_solution = ""

async def send_captcha_request(filepath: str, text: str):
    """Отправляет фото капчи пользователю."""
    bot = get_bot()
    user_id = settings.tg_user_id
    if not bot or not user_id:
        print("ОШИБКА: Не настроен Telegram. Капча сохранена в", filepath)
        return

    try:
        from aiogram.types import FSInputFile
        photo = FSInputFile(filepath)
        captcha_event.clear() # Блокируем процесс
        await bot.send_photo(chat_id=user_id, photo=photo, caption=text, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при отправке капчи в TG: {e}")

@dp.message()
async def handle_text(message: Message):
    """Принимает текст капчи от пользователя."""
    if str(message.from_user.id) != settings.tg_user_id:
        return

    global captcha_solution
    if not captcha_event.is_set():
        captcha_solution = message.text.strip()
        captcha_event.set()
        await message.answer("✅ Код принят, пробую ввести...")

async def start_bot():
    """Запускает бота (long-polling) с автопереподключением.

    Раньше любой сетевой сбой при обращении к Telegram (частое дело в РФ)
    поднимал исключение, которое через asyncio.gather в main.py валило всю
    программу целиком — вместе с поиском вакансий и уже начатым логином на HH.
    Теперь обрыв связи с Telegram лишь приводит к паузе и повторной попытке,
    а основной агент продолжает работать.
    """
    bot = get_bot()
    if not bot:
        print("⚠️ Telegram включён, но токен бота не задан — бот не запущен.")
        return
    print("Запуск Telegram-бота...")
    while True:
        try:
            await dp.start_polling(bot, handle_signals=False)
            break  # штатное завершение polling — выходим
        except asyncio.CancelledError:
            raise  # корректная остановка (Ctrl+C) — пробрасываем дальше
        except Exception as e:
            print(f"⚠️ Связь с Telegram потеряна ({type(e).__name__}: {e}). "
                  f"Повтор через 15 секунд...")
            await asyncio.sleep(15)

async def shutdown_bot(bot_task, timeout: float = 5.0):
    """Быстро гасит бота при остановке агента.

    aiogram висит на long-polling запросе к Telegram (до ~30 секунд), поэтому
    безусловный await отменённой задачи выглядел как зависание. Просим
    диспетчер остановиться, ждём ограниченное время и закрываем сессию.
    """
    try:
        await asyncio.wait_for(dp.stop_polling(), timeout=2)
    except Exception:
        pass

    bot_task.cancel()
    try:
        await asyncio.wait_for(bot_task, timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:
        pass

    bot = get_bot()
    if bot:
        try:
            await asyncio.wait_for(bot.session.close(), timeout=2)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(start_bot())
