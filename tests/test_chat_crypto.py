import chat_crypto


def test_key_is_deterministic_across_calls():
    # _isolate_chat_crypto (conftest.py) уже сбрасывает _key_cache и держит
    # соль в tmp_path — второй вызов должен вернуть тот же ключ, не новый.
    key1 = chat_crypto._get_key()
    chat_crypto._key_cache = None  # эмулирует новый процесс/перезапуск
    key2 = chat_crypto._get_key()
    assert key1 == key2


def test_encrypt_then_decrypt_roundtrips():
    token = chat_crypto.encrypt("привет")
    assert token != "привет"
    assert chat_crypto.decrypt(token) == "привет"


def test_decrypt_of_garbage_returns_placeholder_not_exception():
    assert chat_crypto.decrypt("not-a-valid-fernet-token") == chat_crypto.UNREADABLE_PLACEHOLDER
