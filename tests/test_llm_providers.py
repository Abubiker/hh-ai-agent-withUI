import pytest

import llm_providers
from llm_providers import ProviderError, complete_with_retry, chat_with_retry


class FakeProvider:
    """Провайдер, отвечающий по сценарию: список исходов, по одному на
    попытку. Исход — либо строка-ответ, либо исключение-класс/экземпляр."""

    name = "fake"

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def complete(self, prompt, *, deterministic=False, timeout=120):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def chat(self, messages, *, system=None, max_tokens=2000,
                    timeout=180, images=None):
        return await self.complete("", deterministic=False, timeout=timeout)


@pytest.fixture
def no_sleep(monkeypatch):
    """Тесты retry не должны реально ждать между попытками."""
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(llm_providers.asyncio, "sleep", fake_sleep)
    return sleeps


def use_provider(monkeypatch, provider):
    monkeypatch.setattr(llm_providers, "get_provider", lambda *a, **k: provider)


async def test_complete_with_retry_succeeds_after_one_failure(monkeypatch, no_sleep):
    provider = FakeProvider([ProviderError("временный сбой"), "ответ модели"])
    use_provider(monkeypatch, provider)

    result = await complete_with_retry("prompt", attempts=2)

    assert result == "ответ модели"
    assert provider.calls == 2
    assert no_sleep == [5]


async def test_complete_with_retry_raises_after_all_attempts_fail(monkeypatch, no_sleep):
    provider = FakeProvider([ProviderError("сбой 1"), ProviderError("сбой 2")])
    use_provider(monkeypatch, provider)

    with pytest.raises(ProviderError, match="сбой 2"):
        await complete_with_retry("prompt", attempts=2)

    assert provider.calls == 2


async def test_complete_with_retry_treats_empty_answer_as_failure(monkeypatch, no_sleep):
    provider = FakeProvider(["   ", "настоящий ответ"])
    use_provider(monkeypatch, provider)

    result = await complete_with_retry("prompt", attempts=2)

    assert result == "настоящий ответ"
    assert provider.calls == 2


async def test_complete_with_retry_empty_answer_all_attempts_raises(monkeypatch, no_sleep):
    provider = FakeProvider(["", ""])
    use_provider(monkeypatch, provider)

    with pytest.raises(ProviderError):
        await complete_with_retry("prompt", attempts=2)


async def test_complete_with_retry_single_attempt_no_retry_on_success(monkeypatch, no_sleep):
    provider = FakeProvider(["ответ"])
    use_provider(monkeypatch, provider)

    result = await complete_with_retry("prompt", attempts=1)

    assert result == "ответ"
    assert provider.calls == 1
    assert no_sleep == []


async def test_complete_with_retry_single_attempt_raises_immediately(monkeypatch, no_sleep):
    provider = FakeProvider([ProviderError("сбой")])
    use_provider(monkeypatch, provider)

    with pytest.raises(ProviderError):
        await complete_with_retry("prompt", attempts=1)

    assert provider.calls == 1
    assert no_sleep == []


async def test_chat_with_retry_succeeds_after_one_failure(monkeypatch, no_sleep):
    provider = FakeProvider([ProviderError("временный сбой"), "ответ чата"])
    use_provider(monkeypatch, provider)

    result = await chat_with_retry([{"role": "user", "content": "привет"}], attempts=2)

    assert result == "ответ чата"
    assert provider.calls == 2
    assert no_sleep == [2]


async def test_chat_with_retry_raises_after_all_attempts_fail(monkeypatch, no_sleep):
    provider = FakeProvider([ProviderError("сбой 1"), ProviderError("сбой 2")])
    use_provider(monkeypatch, provider)

    with pytest.raises(ProviderError, match="сбой 2"):
        await chat_with_retry([{"role": "user", "content": "привет"}], attempts=2)


# ---------- exponential backoff ----------

async def test_complete_with_retry_backoff_grows_exponentially(monkeypatch, no_sleep):
    provider = FakeProvider([
        ProviderError("сбой 1"), ProviderError("сбой 2"), "ответ",
    ])
    use_provider(monkeypatch, provider)

    result = await complete_with_retry("prompt", attempts=3)

    assert result == "ответ"
    assert no_sleep == [5, 10]  # base=5, затем base*2


async def test_complete_with_retry_backoff_capped(monkeypatch, no_sleep):
    provider = FakeProvider([ProviderError(f"сбой {i}") for i in range(5)] + ["ответ"])
    use_provider(monkeypatch, provider)

    await complete_with_retry("prompt", attempts=6)

    assert no_sleep[-1] <= 30  # cap для complete_with_retry
    assert no_sleep == [5, 10, 20, 30, 30]


# ---------- Retry-After ----------

async def test_complete_with_retry_honors_retry_after(monkeypatch, no_sleep):
    provider = FakeProvider([
        ProviderError("429", retry_after=1.5), "ответ",
    ])
    use_provider(monkeypatch, provider)

    await complete_with_retry("prompt", attempts=2)

    assert no_sleep == [1.5]


async def test_complete_with_retry_retry_after_capped(monkeypatch, no_sleep):
    provider = FakeProvider([
        ProviderError("429", retry_after=999), "ответ",
    ])
    use_provider(monkeypatch, provider)

    await complete_with_retry("prompt", attempts=2)

    assert no_sleep == [30]


async def test_chat_with_retry_honors_retry_after_with_own_cap(monkeypatch, no_sleep):
    provider = FakeProvider([
        ProviderError("429", retry_after=999), "ответ",
    ])
    use_provider(monkeypatch, provider)

    await chat_with_retry([{"role": "user", "content": "привет"}], attempts=2)

    assert no_sleep == [15]  # cap для chat_with_retry ниже, чем для complete


# ---------- _parse_retry_after ----------

def test_parse_retry_after_numeric_seconds():
    assert llm_providers._parse_retry_after("120") == 120.0


def test_parse_retry_after_none_when_missing():
    assert llm_providers._parse_retry_after(None) is None


def test_parse_retry_after_none_when_garbage():
    assert llm_providers._parse_retry_after("не число и не дата") is None


def test_parse_retry_after_http_date(monkeypatch):
    import time as time_module
    fixed_now = 1_700_000_000.0
    monkeypatch.setattr(llm_providers.time, "time", lambda: fixed_now)
    from email.utils import formatdate
    future = formatdate(fixed_now + 42, usegmt=True)
    assert llm_providers._parse_retry_after(future) == pytest.approx(42, abs=1)
