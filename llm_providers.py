"""Провайдеры языковых моделей.

Раньше ai_analyzer.py ходил напрямую в Ollama. Здесь единый интерфейс, за
которым может стоять локальная Ollama, любой OpenAI-совместимый сервер
(LM Studio, llama.cpp server, Groq) или Anthropic по ключу.

Настройки, которые важно не потерять при любой реализации:
- классификация вакансии детерминирована (temperature=0): на дефолтной
  температуре одна и та же вакансия давала то YES, то NO;
- у Ollama обязателен явный num_ctx, иначе она МОЛЧА обрезает промпт до 4096
  токенов, выбрасывая профиль из начала;
- у "думающих" моделей отключаем рассуждения, иначе они утекают в текст письма.
"""
import asyncio
import json
import time
from email.utils import parsedate_to_datetime

import aiohttp

from settings import settings


class ProviderError(RuntimeError):
    """Модель не ответила. Осознанно не превращается в 'вакансия не подходит':
    вызывающий код должен отложить вакансию, а не похоронить её в базе.

    retry_after — секунды из заголовка Retry-After ответа 429, если сервис
    его прислал. Без этого retry бил бы по тому же лимиту снова и снова на
    фиксированной паузе, вместо того чтобы подождать ровно столько, сколько
    просит сам сервис."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After — либо число секунд, либо HTTP-дата (RFC 7231)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, (dt.timestamp() - time.time()))
    except (TypeError, ValueError):
        return None


class LLMProvider:
    """Общий интерфейс. Промпты живут в ai_analyzer.py, здесь только доставка."""

    name = "base"

    async def complete(self, prompt: str, *, deterministic: bool = False,
                       timeout: int = 120) -> str:
        raise NotImplementedError

    async def chat(self, messages: list[dict], *, system: str | None = None,
                    max_tokens: int = 2000, timeout: int = 180,
                    images: list[str] | None = None) -> str:
        """Многоходовой диалог с системным промптом — для вкладки «Чат».

        Отдельно от complete(): там один плоский промпт без истории и без
        системной роли, это устраивало классификатор и генератор писем, но
        не годится для разговора в несколько реплик.

        images — base64-строки (без префикса "data:...;base64,"), клеятся
        к ПОСЛЕДНЕМУ сообщению в messages (текущая реплика пользователя).
        None/пустой список — поведение не меняется.
        """
        raise NotImplementedError

    # Короткий запрос для проверки связи. Отвечать модель должна одним словом,
    # чтобы проверка не превращалась в генерацию абзаца.
    PING_PROMPT = "Ответь ровно одним словом: ок"

    async def preflight(self) -> tuple[bool, str]:
        """Дешёвые проверки до запроса: ключ на месте, модель существует.
        Пустая строка в успехе — значит замечаний нет."""
        return True, ""

    async def health(self) -> tuple[bool, str]:
        """(доступен, человекочитаемое описание) — для кнопки «Проверить».

        Проверяем настоящим запросом к модели. Раньше хватало списка моделей,
        но он врёт: модель может быть в списке и при этом не отвечать —
        не открыта на вашем тарифе, исчерпан лимит, отозван ключ.
        """
        ok, why = await self.preflight()
        if not ok:
            return False, why
        note = why
        t0 = time.monotonic()
        try:
            answer = await self.complete(self.PING_PROMPT, deterministic=True,
                                         timeout=45)
        except ProviderError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        dt = time.monotonic() - t0
        clean = " ".join((answer or "").split())
        if not clean:
            # Пустой content — типичный симптом "думающей" модели (Gemini
            # 2.5+, o1/o3, DeepSeek-R1): скрытые рассуждения съели весь
            # max_tokens. Зелёная галочка тут была бы ложью — классификатор
            # с тем же лимитом будет так же молчать на каждой вакансии.
            return False, (f"Модель ответила за {dt:.1f} с, но текст пустой — "
                            "похоже, лимита токенов не хватает на скрытые "
                            "рассуждения модели. Попробуйте другую модель.")
        msg = f"Модель ответила за {dt:.1f} с: «{clean[:40]}»"
        return True, (msg + f". {note}" if note else msg)

    async def list_models(self) -> list[str]:
        return []


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 num_ctx: int | None = None):
        cfg = settings.data["llm"]
        self.base_url = (base_url or cfg["ollama_url"]).rstrip("/")
        self.model = model or cfg["ollama_model"]
        self.num_ctx = num_ctx or cfg.get("num_ctx", 16384)

    async def complete(self, prompt: str, *, deterministic: bool = False,
                       timeout: int = 120) -> str:
        options = {"num_ctx": self.num_ctx}
        if deterministic:
            options["temperature"] = 0

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": options,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/api/generate",
                                        json=payload, timeout=timeout) as r:
                    r.raise_for_status()
                    data = await r.json()
                    return (data.get("response") or "").strip()
        except Exception as e:
            raise ProviderError(f"Ollama: {e}") from e

    async def chat(self, messages: list[dict], *, system: str | None = None,
                    max_tokens: int = 2000, timeout: int = 180,
                    images: list[str] | None = None) -> str:
        # /api/generate не знает ни истории, ни системной роли — для диалога
        # нужен /api/chat, единственный эндпоинт Ollama с массивом messages.
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        if images:
            # Не мутируем чужие dict-ы — они же живут в AgentBridge._chat_history.
            msgs = list(msgs)
            msgs[-1] = {**msgs[-1], "images": images}
        payload = {
            "model": self.model,
            "messages": msgs,
            "stream": False,
            "think": False,
            "options": {"num_ctx": self.num_ctx, "num_predict": max_tokens},
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/api/chat",
                                        json=payload, timeout=timeout) as r:
                    r.raise_for_status()
                    data = await r.json()
                    return (data.get("message", {}).get("content") or "").strip()
        except Exception as e:
            raise ProviderError(f"Ollama: {e}") from e

    async def preflight(self) -> tuple[bool, str]:
        try:
            models = await self.list_models()
        except Exception as e:
            return False, f"Ollama недоступна ({e}). Запущена ли она?"
        if not models:
            return False, "Ollama работает, но модели не установлены."
        if self.model not in models:
            return False, f"Модель «{self.model}» не установлена. Есть: {', '.join(models[:5])}"
        return True, ""

    async def list_models(self) -> list[str]:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}/api/tags", timeout=10) as r:
                r.raise_for_status()
                data = await r.json()
                return [m["name"] for m in data.get("models", [])]

    async def list_models_detail(self) -> list[dict]:
        """Список с размерами и отметкой текущей модели — для экрана «Модель»."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}/api/tags", timeout=10) as r:
                r.raise_for_status()
                data = await r.json()
        return [
            {
                "name": m["name"],
                "size_gb": round(m.get("size", 0) / 1_000_000_000, 1),
                "in_use": m["name"] == self.model,
            }
            for m in data.get("models", [])
        ]

    async def delete_model(self, name: str):
        async with aiohttp.ClientSession() as session:
            async with session.delete(f"{self.base_url}/api/delete",
                                      json={"model": name}, timeout=15) as r:
                if r.status >= 400:
                    raise ProviderError(f"Не удалось удалить: HTTP {r.status}")

    async def pull_model(self, name: str, on_progress=None):
        """Скачивает модель. Принимает и обычные имена (gemma4:e4b-it-qat),
        и ссылки на GGUF с HuggingFace (hf.co/user/repo:quant).

        on_progress(текст, готово, всего) вызывается по ходу загрузки — из него
        интерфейс рисует прогресс.
        """
        payload = {"model": name, "stream": True}
        timeout = aiohttp.ClientTimeout(total=None)  # загрузка может идти долго
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.base_url}/api/pull", json=payload) as r:
                    r.raise_for_status()
                    async for raw in r.content:
                        if not raw.strip():
                            continue
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            raise ProviderError(chunk["error"])
                        if on_progress:
                            on_progress(chunk.get("status", ""),
                                        chunk.get("completed", 0),
                                        chunk.get("total", 0))
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Не удалось скачать модель: {e}") from e


# Готовые адреса OpenAI-совместимых сервисов. Список общий для окна и для
# мастера в терминале, чтобы они не разъезжались.
OPENAI_PRESETS = [
    ("OpenRouter", "https://openrouter.ai/api/v1", "есть бесплатные модели, ключ обязателен"),
    ("Mistral", "https://api.mistral.ai/v1", "ключ обязателен"),
    ("Groq", "https://api.groq.com/openai/v1", "быстрый, ключ обязателен"),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
     "есть бесплатный лимит, ключ обязателен"),
    ("NVIDIA Build", "https://integrate.api.nvidia.com/v1", "ключ обязателен, есть бесплатный лимит"),
    ("LM Studio", "http://localhost:1234/v1", "локально, ключ не нужен"),
    ("OpenAI", "https://api.openai.com/v1", "ключ обязателен"),
]

# Классификатору хватает одного токена, но у «думающих» моделей (Gemini
# 2.5+, DeepSeek-R1, o1/o3, QwQ) скрытые рассуждения тратятся ИЗ ЭТОГО ЖЕ
# лимита и съедают его целиком — ответ приходит с content: null. Пустой
# ответ is_vacancy_suitable понимал бы как «не подходит», и вакансия
# уходила бы в базу навсегда. Сервисы берут деньги за фактически выданные
# токены, а не за лимит, поэтому на обычных моделях расход не меняется.
DETERMINISTIC_MAX_TOKENS = 512


class OpenAICompatProvider(LLMProvider):
    """Покрывает LM Studio, llama.cpp server, LocalAI, Groq, Mistral, OpenAI —
    всё, что говорит по протоколу /v1/chat/completions."""

    name = "openai_compat"

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None):
        cfg = settings.data["llm"]
        self.base_url = (base_url or cfg["openai_base_url"]).rstrip("/")
        self.model = model or cfg["openai_model"]
        # Ключ ищем по адресу сервиса: у Groq, Mistral и OpenRouter они разные,
        # и общий на всех означал бы, что переключение стирает предыдущий.
        self.api_key = (api_key if api_key is not None
                        else settings.get_scoped_secret("openai_api_key", self.base_url))

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _request(self, messages: list[dict], *, temperature: float,
                       max_tokens: int, timeout: int) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/chat/completions",
                                        json=payload, headers=self._headers(),
                                        timeout=timeout) as r:
                    if r.status >= 400:
                        retry_after = (_parse_retry_after(r.headers.get("Retry-After"))
                                      if r.status == 429 else None)
                        raise ProviderError(f"HTTP {r.status}: {(await r.text())[:200]}",
                                            retry_after=retry_after)
                    data = await r.json()
                    return (data["choices"][0]["message"]["content"] or "").strip()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"OpenAI-совместимый сервер: {e}") from e

    async def complete(self, prompt: str, *, deterministic: bool = False,
                       timeout: int = 120) -> str:
        return await self._request(
            [{"role": "user", "content": prompt}],
            temperature=0 if deterministic else 0.7,
            max_tokens=DETERMINISTIC_MAX_TOKENS if deterministic else 1500,
            timeout=timeout)

    async def chat(self, messages: list[dict], *, system: str | None = None,
                    max_tokens: int = 2000, timeout: int = 180,
                    images: list[str] | None = None) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        if images:
            # OpenAI-совместимый content — либо строка, либо список блоков
            # {"type":"text"/"image_url"}. Картинку клеим только к последнему
            # ходу, не трогая более раннюю историю.
            msgs = list(msgs)
            last = msgs[-1]
            text = last.get("content") or ""
            blocks = ([{"type": "text", "text": text}] if text else []) + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}}
                for b in images
            ]
            msgs[-1] = {**last, "content": blocks}
        return await self._request(msgs, temperature=0.7, max_tokens=max_tokens,
                                   timeout=timeout)

    async def preflight(self) -> tuple[bool, str]:
        if not self.model:
            return False, "Не указана модель."
        try:
            models = await self.list_models()
        except Exception as e:
            # «401 Unauthorized» формально верно, но человеку ничего не говорит:
            # почти всегда это забытый или неверный ключ.
            code = getattr(e, "status", None)
            if code in (401, 403):
                return False, ("Сервис не принял ключ — проверьте API-ключ"
                               + (" (для этого сервиса он обязателен)"
                                  if not self.api_key else ""))
            if code == 404:
                return False, (f"По адресу {self.base_url} нет метода /models — "
                               "проверьте адрес (обычно /v1, у Google Gemini — "
                               "/v1beta/openai)")
            return False, f"Сервер недоступен по адресу {self.base_url} ({e})"
        if models and self.model not in models:
            return False, f"Модель «{self.model}» не найдена. Есть: {', '.join(models[:5])}"
        return True, ""

    async def list_models(self) -> list[str]:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}/models",
                                   headers=self._headers(), timeout=10) as r:
                r.raise_for_status()
                data = await r.json()
                return [m["id"] for m in data.get("data", [])]


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    API = "https://api.anthropic.com/v1/messages"
    VERSION = "2023-06-01"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        cfg = settings.data["llm"]
        self.model = model or cfg["anthropic_model"]
        self.api_key = api_key if api_key is not None else settings.get_secret("anthropic_api_key")

    async def _request(self, messages: list[dict], *, system: str | None,
                       temperature: float, max_tokens: int, timeout: int) -> str:
        if not self.api_key:
            raise ProviderError("Не задан API-ключ Anthropic.")
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        # У Anthropic системный промпт — отдельное top-level поле, а не
        # сообщение с ролью system внутри messages.
        if system:
            payload["system"] = system
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.VERSION,
            "content-type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.API, json=payload,
                                        headers=headers, timeout=timeout) as r:
                    if r.status >= 400:
                        retry_after = (_parse_retry_after(r.headers.get("Retry-After"))
                                      if r.status == 429 else None)
                        raise ProviderError(f"HTTP {r.status}: {(await r.text())[:200]}",
                                            retry_after=retry_after)
                    data = await r.json()
                    parts = [b.get("text", "") for b in data.get("content", [])
                             if b.get("type") == "text"]
                    return "".join(parts).strip()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Anthropic: {e}") from e

    async def complete(self, prompt: str, *, deterministic: bool = False,
                       timeout: int = 120) -> str:
        return await self._request(
            [{"role": "user", "content": prompt}], system=None,
            temperature=0 if deterministic else 1,
            max_tokens=DETERMINISTIC_MAX_TOKENS if deterministic else 1500,
            timeout=timeout)

    async def chat(self, messages: list[dict], *, system: str | None = None,
                    max_tokens: int = 2000, timeout: int = 180,
                    images: list[str] | None = None) -> str:
        msgs = list(messages)
        if images:
            # Anthropic content — либо строка, либо список блоков
            # {"type":"text"/"image"}; system остаётся отдельным полем.
            last = msgs[-1]
            text = last.get("content") or ""
            blocks = ([{"type": "text", "text": text}] if text else []) + [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}}
                for b in images
            ]
            msgs = msgs[:-1] + [{**last, "content": blocks}]
        return await self._request(msgs, system=system, temperature=1,
                                   max_tokens=max_tokens, timeout=timeout)

    async def preflight(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "Не задан API-ключ Anthropic."
        return True, ""


PROVIDERS = {
    "ollama": OllamaProvider,
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
}


def get_provider(name: str | None = None) -> LLMProvider:
    """Возвращает провайдера согласно настройкам. Создаётся заново на каждый
    вызов, чтобы смена настроек в интерфейсе подхватывалась без перезапуска."""
    name = name or settings.data["llm"]["provider"]
    cls = PROVIDERS.get(name)
    if not cls:
        raise ProviderError(f"Неизвестный провайдер: {name}")
    return cls()


def _backoff_delay(attempt: int, *, base: float, cap: float,
                   retry_after: float | None, retry_after_cap: float | None = None) -> float:
    """Сервис сам сказал, сколько ждать (Retry-After на 429) — это точнее
    угадывания и вежливее по отношению к чужому лимиту. Без него —
    экспоненциальный рост (base, base*2, base*4, ...), а не одна и та же
    пауза на каждой попытке: наивный фиксированный sleep бьёт по тому же
    лимиту с той же частотой и не даёт ему восстановиться. cap — чтобы
    сломанный сервис с огромным Retry-After не подвесил агента на часы.

    retry_after_cap — отдельный, обычно более высокий потолок именно для
    настоящего Retry-After (сервер сказал реальное число — ему стоит
    доверять больше, чем собственной угадайке экспоненты). None — вести
    себя как раньше, единый cap на оба случая."""
    if retry_after is not None:
        return min(retry_after, retry_after_cap if retry_after_cap is not None else cap)
    return min(base * (2 ** (attempt - 1)), cap)


async def complete_with_retry(prompt: str, *, deterministic: bool = False,
                              timeout: int = 120, attempts: int = 3) -> str:
    """Запрос с повтором. Если все попытки провалились — бросает ProviderError.

    Важно: раньше сбой связи возвращал False и был неотличим от честного
    «вакансия не подходит», из-за чего подходящая вакансия помечалась
    обработанной и терялась навсегда.
    """
    provider = get_provider()
    last = None
    for attempt in range(1, attempts + 1):
        try:
            answer = await provider.complete(prompt, deterministic=deterministic,
                                             timeout=timeout)
            if not answer.strip():
                # Пустой content — тот же класс ошибки, что и сбой связи:
                # "думающая" модель съела max_tokens на скрытые рассуждения.
                # Молча считать это честным "нет" уже стоило потерянных
                # вакансий (см. DETERMINISTIC_MAX_TOKENS выше) — повторяем.
                raise ProviderError(f"{provider.name}: модель вернула пустой ответ")
            return answer
        except ProviderError as e:
            last = e
            print(f"Ошибка обращения к модели, попытка {attempt}/{attempts}: {e}")
            if attempt < attempts:
                await asyncio.sleep(_backoff_delay(attempt, base=5, cap=30,
                                                   retry_after=e.retry_after,
                                                   retry_after_cap=120))
    raise ProviderError(f"Модель не ответила после {attempts} попыток: {last}")


async def chat_with_retry(messages: list[dict], *, system: str | None = None,
                          max_tokens: int = 2000, timeout: int = 180,
                          attempts: int = 2, images: list[str] | None = None) -> str:
    """Как complete_with_retry, но для диалога и с более коротким бэкоффом:
    пользователь смотрит на индикатор «Печатает…», не стоит удваивать паузу.

    В отличие от вызовов из ai_analyzer.py, финальная ProviderError здесь не
    гасится — в чате её показывают пользователю как ошибку с кнопкой
    «Повторить», а не подменяют заглушкой посреди немого автоматического цикла.
    """
    provider = get_provider()
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return await provider.chat(messages, system=system,
                                       max_tokens=max_tokens, timeout=timeout,
                                       images=images)
        except ProviderError as e:
            last = e
            print(f"Ошибка обращения к модели (чат), попытка {attempt}/{attempts}: {e}")
            if attempt < attempts:
                await asyncio.sleep(_backoff_delay(attempt, base=2, cap=15,
                                                   retry_after=e.retry_after))
    raise ProviderError(f"Модель не ответила после {attempts} попыток: {last}")
