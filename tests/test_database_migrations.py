import sqlite3

import pytest

import database


def test_fresh_db_ends_at_latest_schema_version(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()

    conn = sqlite3.connect(database.DB_PATH)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert version == database.SCHEMA_VERSION


def test_init_db_creates_all_tables(tmp_db):
    conn = sqlite3.connect(tmp_db.DB_PATH)
    tables = {row[0] for row in
              conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()

    assert {"applied_jobs", "chat_messages", "failed_responses", "seen_skips",
            "verdict_cache", "stats_totals", "chat_turns"} <= tables


def test_reinit_does_not_rerun_migrations(tmp_path, monkeypatch):
    """Повторный init_db на уже актуальной базе не должен заново гонять
    миграции — иначе каждый старт приложения платил бы за них снова."""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()

    calls = []
    original = database.MIGRATIONS[1]
    monkeypatch.setitem(database.MIGRATIONS, 1,
                        lambda cursor: (calls.append(1), original(cursor)))

    database.init_db()  # уже на SCHEMA_VERSION — миграция 1 не должна вызваться

    assert calls == []


def test_future_migration_applies_once_and_preserves_data(tmp_path, monkeypatch):
    """Симулирует появление новой версии схемы: данные, записанные на старой
    версии, должны пережить миграцию, а сама миграция — выполниться ровно
    один раз даже при двух последовательных init_db()."""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    database.add_applied_job("job-1", "Title", "https://hh.ru/vacancy/1")

    # future_version — на единицу больше уже реально существующей SCHEMA_VERSION,
    # а не захардкоженная 2: иначе тест ломается каждый раз, когда добавляется
    # очередная настоящая миграция (первый init_db() выше уже доезжает до
    # актуальной версии, дальше симулируем ЕЩЁ одну, пока не существующую).
    future_version = database.SCHEMA_VERSION + 1
    calls = []

    def fake_migration(cursor):
        calls.append(1)
        cursor.execute("ALTER TABLE applied_jobs ADD COLUMN note TEXT")

    monkeypatch.setitem(database.MIGRATIONS, future_version, fake_migration)
    monkeypatch.setattr(database, "SCHEMA_VERSION", future_version)

    database.init_db()
    database.init_db()  # второй вызов — миграция уже применена, не должна повториться

    assert calls == [1]
    assert database.is_job_applied("job-1") is True  # данные не потерялись

    conn = sqlite3.connect(database.DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(applied_jobs)")}
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert "note" in cols
    assert version == future_version


def test_pre_versioning_db_migrates_without_data_loss(tmp_path, monkeypatch):
    """База, созданная до появления PRAGMA user_version (версия 0 по
    умолчанию у sqlite), должна догнаться миграцией 1 без потери уже
    накопленных данных — это реальный сценарий обновления приложения."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE applied_jobs (
            id TEXT PRIMARY KEY, title TEXT, url TEXT,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO applied_jobs (id, title, url) VALUES (?, ?, ?)",
                 ("job-1", "Old Title", "https://hh.ru/vacancy/1"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()

    assert database.is_job_applied("job-1") is True
    conn = sqlite3.connect(str(db_path))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(applied_jobs)")}
    conn.close()
    assert "style" in cols  # миграция 1 доехала и до старой базы


# ---------- _migrate_to_2 (папки и диалоги чата) ----------

def test_migrate_to_2_on_fresh_db_creates_no_default_conversation(tmp_db):
    """Пустая база не должна заводить диалог по умолчанию — первый диалог
    создаёт первое отправленное сообщение (см. AgentBridge.send_chat_message),
    а не миграция."""
    assert database.list_chat_conversations() == []
    assert database.list_chat_folders() == []


def test_migrate_to_2_backfills_flat_history_into_one_conversation(tmp_path, monkeypatch):
    """Симулирует апгрейд с версии 1 (плоский chat_turns без conversation_id)
    — старая переписка должна перекочевать в один диалог «Общий чат» в
    корне, ничего не потеряв."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 1")
    conn.execute("""
        CREATE TABLE chat_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL, content TEXT NOT NULL, image_ref TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO chat_turns (role, content) VALUES ('user', 'привет')")
    conn.execute("INSERT INTO chat_turns (role, content) VALUES ('assistant', 'здравствуйте')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()

    conversations = database.list_chat_conversations()
    assert len(conversations) == 1
    assert conversations[0]["title"] == "Общий чат"
    assert conversations[0]["folder_id"] is None

    turns = database.load_chat_turns(conversations[0]["id"])
    assert [t["content"] for t in turns] == ["привет", "здравствуйте"]


# ---------- _migrate_to_3 (шифрование переписки) ----------

def test_migrate_to_3_encrypts_preexisting_plaintext(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 2")
    conn.execute("""
        CREATE TABLE chat_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL, content TEXT NOT NULL, image_ref TEXT,
            conversation_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE chat_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, folder_id INTEGER,
            title TEXT NOT NULL DEFAULT 'Новый диалог', sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO chat_conversations (id, title) VALUES (1, 'Общий чат')")
    conn.execute(
        "INSERT INTO chat_turns (role, content, conversation_id) VALUES ('user', 'открытый текст', 1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()

    conn = sqlite3.connect(str(db_path))
    raw_content = conn.execute("SELECT content FROM chat_turns").fetchone()[0]
    conn.close()
    assert raw_content != "открытый текст"  # на диске уже не читаемый текст

    turns = database.load_chat_turns(1)
    assert turns[0]["content"] == "открытый текст"  # но через API читается как раньше


# ---------- CRUD: папки и диалоги ----------

def test_chat_folder_and_conversation_crud(tmp_db):
    folder_id = database.create_chat_folder("Резюме QA")
    assert database.list_chat_folders() == [{"id": folder_id, "name": "Резюме QA"}]

    database.rename_chat_folder(folder_id, "QA резюме")
    assert database.list_chat_folders()[0]["name"] == "QA резюме"

    conv_id = database.create_chat_conversation(folder_id, "Собеседования")
    database.add_chat_turn("user", "текст", conversation_id=conv_id)
    assert len(database.load_chat_turns(conv_id)) == 1

    database.rename_chat_conversation(conv_id, "Переговоры об офере")
    assert database.list_chat_conversations()[0]["title"] == "Переговоры об офере"


def test_delete_chat_conversation_removes_its_turns(tmp_db):
    conv_id = database.create_chat_conversation()
    database.add_chat_turn("user", "текст", conversation_id=conv_id)

    database.delete_chat_conversation(conv_id)

    assert database.list_chat_conversations() == []
    assert database.load_chat_turns(conv_id) == []


def test_delete_chat_folder_cascades_to_conversations_and_turns(tmp_db):
    """Пользователь явно выбрал каскадное удаление (папка + всё внутри),
    а не перенос диалогов в корень."""
    folder_id = database.create_chat_folder("Папка")
    other_folder_id = database.create_chat_folder("Другая папка")
    conv_in = database.create_chat_conversation(folder_id, "Внутри")
    conv_other = database.create_chat_conversation(other_folder_id, "В другой папке")
    database.add_chat_turn("user", "текст", conversation_id=conv_in)

    database.delete_chat_folder(folder_id)

    remaining_ids = {c["id"] for c in database.list_chat_conversations()}
    assert conv_in not in remaining_ids
    assert conv_other in remaining_ids  # соседняя папка не пострадала
    assert database.load_chat_turns(conv_in) == []
    assert {f["id"] for f in database.list_chat_folders()} == {other_folder_id}
