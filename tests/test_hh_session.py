import asyncio

import pytest

import hh_session
import sites as _sites


@pytest.fixture
def logged_in_site(tmp_path, monkeypatch):
    """require_login=True требует существующий state.json — подсовываем
    фиктивный файл сессии, чтобы дойти до самого запуска браузера."""
    site = {"name": "hh.ru", "id": "hh.ru"}
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(_sites, "state_file", lambda s: str(state_path))
    return site


async def test_open_session_raises_on_launch_hang(monkeypatch, logged_in_site):
    """Если AsyncNewBrowser зависает (протокол/процесс), open_session не
    должен виснуть НАВСЕГДА — та же логика, что у MOUSE_ACTION_TIMEOUT."""
    monkeypatch.setattr(hh_session, "BROWSER_LAUNCH_TIMEOUT", 0.05)

    async def hangs_forever(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr("camoufox.async_api.AsyncNewBrowser", hangs_forever)

    with pytest.raises(hh_session.SessionError):
        await hh_session.open_session(logged_in_site, headless=True, require_login=True)


async def test_open_session_raises_on_context_hang_and_closes_browser(
        monkeypatch, logged_in_site):
    """Зависание на new_context() после успешного запуска браузера не должно
    утекать процессом браузера — close() обязан быть вызван."""
    monkeypatch.setattr(hh_session, "CONTEXT_OPEN_TIMEOUT", 0.05)

    closed = []

    class FakeBrowser:
        async def new_context(self, **kwargs):
            await asyncio.sleep(3600)

        async def close(self):
            closed.append(True)

    async def fake_new_browser(*a, **k):
        return FakeBrowser()

    monkeypatch.setattr("camoufox.async_api.AsyncNewBrowser", fake_new_browser)

    with pytest.raises(hh_session.SessionError):
        await hh_session.open_session(logged_in_site, headless=True, require_login=True)

    assert closed == [True]
