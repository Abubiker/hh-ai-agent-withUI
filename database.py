import sqlite3

import chat_crypto
from settings import user_file

# База в папке пользователя, а не рядом с кодом: в собранном .app соседняя
# папка временная, и история откликов терялась бы при каждом запуске.
DB_PATH = str(user_file("agent.db"))


def _migrate_to_1(cursor: sqlite3.Cursor):
    """Вся схема до введения версионирования — включая колонку style,
    раньше добавлявшуюся отдельным ALTER TABLE с ручной проверкой
    PRAGMA table_info. У новых пользователей применяется целиком за один
    проход; у существующих CREATE TABLE IF NOT EXISTS не трогает уже
    имеющиеся таблицы, а ALTER здесь безопасен — до версии 1 колонки style
    просто не бывает ни у кого, PRAGMA user_version там всегда 0."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applied_jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(applied_jobs)")}
    if "style" not in existing_cols:
        cursor.execute("ALTER TABLE applied_jobs ADD COLUMN style TEXT")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            msg_id TEXT PRIMARY KEY,
            chat_id TEXT,
            text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Вакансии, где отклик не подтвердился. Без счётчика попыток агент возвращался
    # бы к ним при каждом проходе выдачи, каждый раз тратя полный цикл модели.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS failed_responses (
            id TEXT PRIMARY KEY,
            title TEXT,
            attempts INTEGER DEFAULT 0,
            last_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Вакансии, отсеянные стоп-словом, для которых уже была напечатана строка
    # «⏩ Пропускаю». Раньше это жило только в памяти (_skip_logged) — после
    # перезапуска приложения те же ~28 строк печатались заново на каждую
    # уже виденную вакансию. Сам стоп-фильтр по-прежнему проверяет каждую
    # вакансию каждый раз (правки стоп-слов должны действовать и на старые) —
    # эта таблица подавляет только повторный ЛОГ, не саму проверку.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_skips (
            id TEXT PRIMARY KEY,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Вердикт ИИ по хешу (профиль + фильтры + вакансия, см. ai_analyzer.
    # _verdict_cache_key) — настоящий перепост (тот же текст под новым id)
    # не должен каждый раз заново уходить в модель.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS verdict_cache (
            hash TEXT PRIMARY KEY,
            verdict TEXT,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Счётчики воронки (Stats, см. stats.py) — раньше жили только в памяти
    # процесса и обнулялись при каждом перезапуске приложения.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stats_totals (
            field TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    """)
    # История локального чата с ассистентом (вкладка «Чат») — раньше жила
    # только в AgentBridge._chat_history и обнулялась при перезапуске.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            image_ref TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _migrate_to_2(cursor: sqlite3.Cursor):
    """Папки и диалоги для вкладки «Чат» (см. AgentBridge.switch_chat_conversation)
    — раньше вся история была одним плоским списком в chat_turns. Существующая
    история переносится в один диалог «Общий чат» в корне, только если есть
    что переносить (на пустой базе диалог не заводим — первый диалог создаёт
    первое отправленное сообщение, см. AgentBridge.send_chat_message)."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER,
            title TEXT NOT NULL DEFAULT 'Новый диалог',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(chat_turns)")}
    if "conversation_id" not in existing_cols:
        cursor.execute("ALTER TABLE chat_turns ADD COLUMN conversation_id INTEGER")

    n = cursor.execute(
        "SELECT COUNT(*) FROM chat_turns WHERE conversation_id IS NULL").fetchone()[0]
    if n:
        cursor.execute(
            "INSERT INTO chat_conversations (folder_id, title) VALUES (NULL, ?)",
            ("Общий чат",))
        default_id = cursor.lastrowid
        cursor.execute(
            "UPDATE chat_turns SET conversation_id = ? WHERE conversation_id IS NULL",
            (default_id,))


def _migrate_to_3(cursor: sqlite3.Cursor):
    """Шифрует уже накопленный открытый текст переписки (см. chat_crypto) —
    add_chat_turn/load_chat_turns шифруют/расшифровывают на границе начиная
    с этой версии, старые строки до этого шага хранились как есть."""
    rows = cursor.execute("SELECT id, content, image_ref FROM chat_turns").fetchall()
    for row_id, content, image_ref in rows:
        cursor.execute(
            "UPDATE chat_turns SET content = ?, image_ref = ? WHERE id = ?",
            (chat_crypto.encrypt(content),
             chat_crypto.encrypt(image_ref) if image_ref else None,
             row_id))


# Версии схемы — по одной функции на шаг, каждая поднимает ровно на 1.
# Дальнейшие изменения схемы (новая таблица, ALTER TABLE, бэкофилл) — новая
# функция _migrate_to_N и запись здесь, а не правка предыдущих: старые
# версии уже применены у части пользователей, их нельзя менять задним числом.
MIGRATIONS = {
    1: _migrate_to_1,
    2: _migrate_to_2,
    3: _migrate_to_3,
}
SCHEMA_VERSION = max(MIGRATIONS)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # PRAGMA user_version — целое число, встроенное в заголовок файла sqlite;
    # отдельная таблица версий не нужна и не мешает будущим SELECT *.
    # У новой базы 0 по умолчанию — вся цепочка миграций накатывается разом.
    current = cursor.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current + 1, SCHEMA_VERSION + 1):
        # Явный BEGIN: без него DDL/PRAGMA в модуле sqlite3 коммитятся сразу
        # по выполнении (autocommit), и крах между последним шагом миграции
        # и записью версии не откатывался бы — следующий init_db() рестартовал
        # бы неидемпотентную миграцию заново. SQLite поддерживает транзакционный
        # DDL (включая PRAGMA user_version) — оборачиваем миграцию и бамп
        # версии одной транзакцией: либо применилось всё, либо ничего.
        cursor.execute("BEGIN")
        try:
            MIGRATIONS[version](cursor)
            cursor.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    conn.close()

def load_applied_jobs(limit: int = 200) -> list[dict]:
    """Последние отклики, самые свежие первыми — для истории на вкладке
    «Статистика» (название + ссылка на вакансию).

    Сортировка по applied_at DESC, rowid DESC: у CURRENT_TIMESTAMP секундная
    точность — два отклика в одну секунду иначе шли бы в произвольном
    порядке. rowid у applied_jobs растёт по порядку вставки (id — TEXT, а не
    INTEGER PRIMARY KEY, так что implicit rowid никуда не делся)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, url, applied_at FROM applied_jobs "
        "ORDER BY applied_at DESC, rowid DESC LIMIT ?", (limit,))
    jobs = [{"id": row[0], "title": row[1], "url": row[2], "applied_at": row[3]}
            for row in cursor.fetchall()]
    conn.close()
    return jobs


def is_job_applied(job_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM applied_jobs WHERE id = ?", (job_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_applied_job(job_id: str, title: str, url: str, style: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO applied_jobs (id, title, url, style) VALUES (?, ?, ?, ?)",
                   (job_id, title, url, style))
    conn.commit()
    conn.close()

def bump_failed_response(job_id: str, title: str) -> int:
    """Отмечает неудачную попытку отклика и возвращает их общее число."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO failed_responses (id, title, attempts) VALUES (?, ?, 1)
        ON CONFLICT(id) DO UPDATE SET attempts = attempts + 1, last_at = CURRENT_TIMESTAMP
    """, (job_id, title))
    conn.commit()
    attempts = cursor.execute(
        "SELECT attempts FROM failed_responses WHERE id = ?", (job_id,)).fetchone()[0]
    conn.close()
    return attempts


def load_seen_skips() -> set[str]:
    """Все id, для которых строка «⏩ Пропускаю» уже была напечатана раньше —
    загружается один раз при старте клиента, дальше живёт в памяти как и
    раньше (per-vacancy запрос к sqlite на каждый проход выдачи был бы
    лишним расходом ради строки, которая и так не пишется в базу)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM seen_skips")
    ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    return ids

def mark_skip_seen(job_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO seen_skips (id) VALUES (?)", (job_id,))
    conn.commit()
    conn.close()


def get_cached_verdict(cache_key: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT verdict FROM verdict_cache WHERE hash = ?", (cache_key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_cached_verdict(cache_key: str, verdict: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO verdict_cache (hash, verdict) VALUES (?, ?)",
                   (cache_key, verdict))
    conn.commit()
    conn.close()


def bump_stat(field: str, amount: int = 1):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO stats_totals (field, value) VALUES (?, ?)
        ON CONFLICT(field) DO UPDATE SET value = value + excluded.value
    """, (field, amount))
    conn.commit()
    conn.close()


def load_stats_totals() -> dict[str, int]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT field, value FROM stats_totals")
    totals = {field: value for field, value in cursor.fetchall()}
    conn.close()
    return totals


def add_chat_turn(role: str, content: str, image_ref: str | None = None, *, conversation_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO chat_turns (role, content, image_ref, conversation_id) VALUES (?, ?, ?, ?)",
        (role, chat_crypto.encrypt(content),
         chat_crypto.encrypt(image_ref) if image_ref else None, conversation_id))
    conn.commit()
    conn.close()


def load_chat_turns(conversation_id: int) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT role, content, image_ref FROM chat_turns "
        "WHERE conversation_id = ? ORDER BY id", (conversation_id,))
    turns = [{"role": role, "content": chat_crypto.decrypt(content),
              "image_ref": chat_crypto.decrypt(image_ref) if image_ref else None}
             for role, content, image_ref in cursor.fetchall()]
    conn.close()
    return turns


def clear_chat_turns(conversation_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_turns WHERE conversation_id = ?", (conversation_id,))
    conn.commit()
    conn.close()


def create_chat_folder(name: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_folders (name) VALUES (?)", (name,))
    conn.commit()
    folder_id = cursor.lastrowid
    conn.close()
    return folder_id


def rename_chat_folder(folder_id: int, name: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE chat_folders SET name = ? WHERE id = ?", (name, folder_id))
    conn.commit()
    conn.close()


def list_chat_folders() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM chat_folders ORDER BY sort_order, id")
    folders = [{"id": row[0], "name": row[1]} for row in cursor.fetchall()]
    conn.close()
    return folders


def delete_chat_folder(folder_id: int):
    """Каскадно: все ходы всех диалогов папки, потом сами диалоги, потом
    папка — пользователь явно выбрал удалять папку целиком со всем внутри,
    а не переносить диалоги в корень (см. AskUserQuestion в плане фичи)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM chat_turns WHERE conversation_id IN (
            SELECT id FROM chat_conversations WHERE folder_id = ?
        )
    """, (folder_id,))
    cursor.execute("DELETE FROM chat_conversations WHERE folder_id = ?", (folder_id,))
    cursor.execute("DELETE FROM chat_folders WHERE id = ?", (folder_id,))
    conn.commit()
    conn.close()


def create_chat_conversation(folder_id: int | None = None, title: str = "Новый диалог") -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO chat_conversations (folder_id, title) VALUES (?, ?)", (folder_id, title))
    conn.commit()
    conversation_id = cursor.lastrowid
    conn.close()
    return conversation_id


def rename_chat_conversation(conversation_id: int, title: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE chat_conversations SET title = ? WHERE id = ?", (title, conversation_id))
    conn.commit()
    conn.close()


def delete_chat_conversation(conversation_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_turns WHERE conversation_id = ?", (conversation_id,))
    cursor.execute("DELETE FROM chat_conversations WHERE id = ?", (conversation_id,))
    conn.commit()
    conn.close()


def list_chat_conversations() -> list[dict]:
    """Все диалоги разом (с folder_id) — группировку по папкам делает
    фронтенд, тот же паттерн, что уже используется для регионов/моделей."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, folder_id, title FROM chat_conversations ORDER BY sort_order, id")
    conversations = [{"id": row[0], "folder_id": row[1], "title": row[2]}
                      for row in cursor.fetchall()]
    conn.close()
    return conversations


def is_message_processed(msg_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM chat_messages WHERE msg_id = ?", (msg_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_processed_message(msg_id: str, chat_id: str, text: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_messages (msg_id, chat_id, text) VALUES (?, ?, ?)", (msg_id, chat_id, text))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized.")
