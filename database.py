import sqlite3

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


# Версии схемы — по одной функции на шаг, каждая поднимает ровно на 1.
# Дальнейшие изменения схемы (новая таблица, ALTER TABLE, бэкофилл) — новая
# функция _migrate_to_N и запись здесь, а не правка предыдущих: старые
# версии уже применены у части пользователей, их нельзя менять задним числом.
MIGRATIONS = {
    1: _migrate_to_1,
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
        MIGRATIONS[version](cursor)
        cursor.execute(f"PRAGMA user_version = {version}")
    conn.commit()
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


def add_chat_turn(role: str, content: str, image_ref: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_turns (role, content, image_ref) VALUES (?, ?, ?)",
                   (role, content, image_ref))
    conn.commit()
    conn.close()


def load_chat_turns() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT role, content, image_ref FROM chat_turns ORDER BY id")
    turns = [{"role": role, "content": content, "image_ref": image_ref}
             for role, content, image_ref in cursor.fetchall()]
    conn.close()
    return turns


def clear_chat_turns():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_turns")
    conn.commit()
    conn.close()


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
