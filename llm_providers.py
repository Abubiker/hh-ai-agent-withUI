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

import aiohttp

from settings import settings


class ProviderError(RuntimeError):
    """Модель не ответила. Осознанно не превращается в 'вакансия не подходит':
    вызывающий код должен отложить вакансию, а не похоронить её в базе."""


class LLMProvider:
    """Общий интерфейс. Промпты живут в ai_analyzer.py, здесь только доставка."""

    name = "base"

    async def complete(self, prompt: str, *, deterministic: bool = False,
                       timeout: int = 120) -> str:
        raise NotImplementedError

    async def health(self) -> tuple[bool, str]:
        """(доступен, человекочитаемое описание) — для индикатора в интерфейсе."""
        raise NotImplementedError

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

    async def health(self) -> tuple[bool, str]:
        try:
            models = await self.list_models()
        except Exception as e:
            return False, f"Ollama недоступна ({e}). Запущена ли она?"
        if not models:
            return False, "Ollama работает, но модели не установлены."
        if self.model not in models:
            return False, f"Модель «{self.model}» не установлена. Есть: {', '.join(models[:5])}"
        return True, f"Ollama готова, модель {self.model}"

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
    ("LM Studio", "http://localhost:1234/v1", "локально, ключ не нужен"),
    ("OpenAI", "https://api.openai.com/v1", "ключ обязателен"),
]


class OpenAICompatProvider(LLMProvider):
    """Покрывает LM Studio, llama.cpp server, LocalAI, Groq, Mistral, OpenAI —
    всё, что говорит по протоколу /v1/chat/completions."""

    name = "openai_compat"

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None):
        cfg = settings.data["llm"]
        self.base_url = (base_url or cfg["openai_base_url"]).rstrip("/")
        self.model = model or cfg["openai_model"]
        self.api_key = api_key if api_key is not None else settings.get_secret("openai_api_key")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def complete(self, prompt: str, *, deterministic: bool = False,
                       timeout: int = 120) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0 if deterministic else 0.7,
            # Классификатору хватает пары токенов, письму нужен запас.
            "max_tokens": 16 if deterministic else 1500,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/chat/completions",
                                        json=payload, headers=self._headers(),
                                        timeout=timeout) as r:
                    if r.status >= 400:
                        raise ProviderError(f"HTTP {r.status}: {(await r.text())[:200]}")
                    data = await r.json()
                    return (data["choices"][0]["message"]["content"] or "").strip()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"OpenAI-совместимый сервер: {e}") from e

    async def health(self) -> tuple[bool, str]:
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
                               "проверьте, что адрес заканчивается на /v1")
            return False, f"Сервер недоступен по адресу {self.base_url} ({e})"
        if models and self.model not in models:
            return False, f"Модель «{self.model}» не найдена. Есть: {', '.join(models[:5])}"
        return True, f"Сервер отвечает, модель {self.model}"

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

    async def complete(self, prompt: str, *, deterministic: bool = False,
                       timeout: int = 120) -> str:
        if not self.api_key:
            raise ProviderError("Не задан API-ключ Anthropic.")
        payload = {
            "model": self.model,
            "max_tokens": 16 if deterministic else 1500,
            "temperature": 0 if deterministic else 1,
            "messages": [{"role": "user", "content": prompt}],
        }
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
                        raise ProviderError(f"HTTP {r.status}: {(await r.text())[:200]}")
                    data = await r.json()
                    parts = [b.get("text", "") for b in data.get("content", [])
                             if b.get("type") == "text"]
                    return "".join(parts).strip()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Anthropic: {e}") from e

    async def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "Не задан API-ключ Anthropic."
        try:
            await self.complete("Ответь одним словом: ок", deterministic=True, timeout=30)
            return True, f"Ключ работает, модель {self.model}"
        except ProviderError as e:
            return False, str(e)


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


async def complete_with_retry(prompt: str, *, deterministic: bool = False,
                              timeout: int = 120, attempts: int = 2) -> str:
    """Запрос с повтором. Если все попытки провалились — бросает ProviderError.

    Важно: раньше сбой связи возвращал False и был неотличим от честного
    «вакансия не подходит», из-за чего подходящая вакансия помечалась
    обработанной и терялась навсегда.
    """
    provider = get_provider()
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return await provider.complete(prompt, deterministic=deterministic,
                                           timeout=timeout)
        except ProviderError as e:
            last = e
            print(f"Ошибка обращения к модели, попытка {attempt}/{attempts}: {e}")
            if attempt < attempts:
                await asyncio.sleep(5)
    raise ProviderError(f"Модель не ответила после {attempts} попыток: {last}")
