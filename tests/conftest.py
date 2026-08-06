import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import chat_crypto
import database
from settings import settings


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """agent.db изолированная от настоящей — иначе тесты писали бы в
    реальную папку пользователя (data_dir())."""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    return database


@pytest.fixture(autouse=True)
def _isolate_chat_crypto(tmp_path, monkeypatch):
    """chat_crypto.user_file("chat.salt") иначе писал бы в реальную папку
    пользователя (data_dir()), как и agent.db — см. tmp_db выше. Ключ
    кэшируется в модуле на процесс (chat_crypto._key_cache), поэтому между
    тестами с разными tmp_path его тоже нужно сбрасывать — иначе тест,
    запущенный после другого, шифровал бы под чужим ключом/солью."""
    monkeypatch.setattr(chat_crypto, "user_file", lambda name: tmp_path / name)
    chat_crypto._key_cache = None
    yield
    chat_crypto._key_cache = None
