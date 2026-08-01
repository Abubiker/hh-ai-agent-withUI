import sqlite3

from settings import user_file

# База в папке пользователя, а не рядом с кодом: в собранном .app соседняя
# папка временная, и история откликов терялась бы при каждом запуске.
DB_PATH = str(user_file("agent.db"))

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Таблица для откликнутых вакансий
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applied_jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # style — каким стилем было написано сопроводительное (см. ai_analyzer.
    # LETTER_STYLES), NULL если письмо не прикладывалось. Добавлено позже
    # исходной таблицы — ALTER TABLE, а не пересоздание: строки существующих
    # пользователей не трогаем.
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(applied_jobs)")}
    if "style" not in existing_cols:
        cursor.execute("ALTER TABLE applied_jobs ADD COLUMN style TEXT")
    # Таблица для истории сообщений чатов
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
    conn.commit()
    conn.close()

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
