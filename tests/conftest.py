import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import database
from settings import settings


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """agent.db изолированная от настоящей — иначе тесты писали бы в
    реальную папку пользователя (data_dir())."""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    return database
