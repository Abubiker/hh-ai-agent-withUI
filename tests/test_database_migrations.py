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

    calls = []

    def fake_migration_2(cursor):
        calls.append(1)
        cursor.execute("ALTER TABLE applied_jobs ADD COLUMN note TEXT")

    monkeypatch.setitem(database.MIGRATIONS, 2, fake_migration_2)
    monkeypatch.setattr(database, "SCHEMA_VERSION", 2)

    database.init_db()
    database.init_db()  # второй вызов — миграция 2 уже применена, не должна повториться

    assert calls == [1]
    assert database.is_job_applied("job-1") is True  # данные не потерялись

    conn = sqlite3.connect(database.DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(applied_jobs)")}
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert "note" in cols
    assert version == 2


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
