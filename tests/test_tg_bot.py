import tg_bot
from settings import settings


def _reset_bot_cache(monkeypatch):
    monkeypatch.setitem(tg_bot._bot_cache, "token", None)
    monkeypatch.setitem(tg_bot._bot_cache, "bot", None)


def test_get_bot_none_without_token(monkeypatch):
    _reset_bot_cache(monkeypatch)
    monkeypatch.setattr(type(settings), "tg_bot_token", property(lambda self: ""))

    assert tg_bot.get_bot() is None


def test_is_configured_reflects_live_settings(monkeypatch):
    monkeypatch.setattr(type(settings), "tg_bot_token", property(lambda self: ""))
    assert tg_bot.is_configured() is False

    monkeypatch.setattr(type(settings), "tg_bot_token", property(lambda self: "123:abc"))
    assert tg_bot.is_configured() is True


def test_get_bot_picks_up_token_set_after_first_call(monkeypatch):
    """Тот самый баг: токен появился в настройках ПОСЛЕ того, как модуль уже
    был импортирован и/или get_bot() уже вызывался с пустым токеном — второй
    вызов обязан подхватить новый токен без перезапуска процесса."""
    _reset_bot_cache(monkeypatch)
    monkeypatch.setattr(type(settings), "tg_bot_token", property(lambda self: ""))
    assert tg_bot.get_bot() is None

    monkeypatch.setattr(type(settings), "tg_bot_token", property(lambda self: "111:token-a"))
    bot_a = tg_bot.get_bot()
    assert bot_a is not None


def test_get_bot_caches_same_instance_for_unchanged_token(monkeypatch):
    _reset_bot_cache(monkeypatch)
    monkeypatch.setattr(type(settings), "tg_bot_token", property(lambda self: "111:token-a"))

    bot1 = tg_bot.get_bot()
    bot2 = tg_bot.get_bot()

    assert bot1 is bot2


def test_get_bot_recreates_when_token_changes(monkeypatch):
    _reset_bot_cache(monkeypatch)
    monkeypatch.setattr(type(settings), "tg_bot_token", property(lambda self: "111:token-a"))
    bot_a = tg_bot.get_bot()

    monkeypatch.setattr(type(settings), "tg_bot_token", property(lambda self: "222:token-b"))
    bot_b = tg_bot.get_bot()

    assert bot_a is not bot_b
