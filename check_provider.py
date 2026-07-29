"""Проверка облачного провайдера на настоящих задачах агента.

Health-check говорит только «сервер ответил». Здесь прогоняются те же два
запроса, что делает агент: классификация вакансии и сопроводительное письмо.
Именно на них вылезают различия между сервисами — где-то модель игнорирует
просьбу ответить одним словом, где-то отвечает по-английски.

Ключ берётся из переменной окружения и в выводе не появляется:

    OPENAI_API_KEY=... python check_provider.py https://api.groq.com/openai/v1 llama-3.3-70b-versatile

Без аргументов проверяется то, что настроено в приложении.
"""
import asyncio
import os
import sys
import time

from settings import settings
from llm_providers import OpenAICompatProvider, ProviderError

VACANCIES = [
    (True, "QA Engineer", "Ручное тестирование web и API, Postman, SQL. Английский будет плюсом."),
    (False, "Head of QA", "Руководитель отдела тестирования, в подчинении 12 человек, найм, 1-1."),
    (False, "Python-разработчик", "Разработка микросервисов на FastAPI, PostgreSQL, Kafka."),
]


def mask(key: str) -> str:
    """Ключ в выводе не показываем — только признак, что он есть."""
    if not key:
        return "нет"
    return f"есть, {len(key)} символов, оканчивается на …{key[-4:]}"


async def main():
    args = sys.argv[1:]
    if args:
        base_url, model = args[0], (args[1] if len(args) > 1 else "")
        key = os.getenv("OPENAI_API_KEY") or settings.get_scoped_secret(
            "openai_api_key", base_url)
    else:
        cfg = settings.data["llm"]
        base_url, model = cfg["openai_base_url"], cfg["openai_model"]
        key = settings.get_scoped_secret("openai_api_key", base_url)

    print(f"Сервис:  {base_url}")
    print(f"Ключ:    {mask(key)}")

    p = OpenAICompatProvider(base_url=base_url, model=model or "x", api_key=key)

    print("\n[1/4] Список моделей")
    try:
        models = await p.list_models()
        print(f"  ✅ доступно {len(models)}")
        if not model:
            print("  Модель не указана. Первые 10:")
            for m in models[:10]:
                print(f"    {m}")
            return
        print(f"  {'✅' if model in models else '⚠️'} запрошенная «{model}» "
              f"{'есть в списке' if model in models else 'в списке не значится'}")
    except Exception as e:
        print(f"  ❌ {e}")
        return

    print("\n[2/4] Проверка связи")
    ok, msg = await p.health()
    print(f"  {'✅' if ok else '❌'} {msg}")
    if not ok:
        return

    print("\n[3/4] Классификация вакансий (нужен ответ строго YES или NO)")
    import ai_analyzer
    saved = settings.data["llm"].copy()
    settings.data["llm"].update(provider="openai_compat",
                                openai_base_url=base_url, openai_model=model)
    if key:
        settings._secret_cache["openai_api_key"] = key
        settings._secret_cache[
            settings.scoped_secret_name("openai_api_key", base_url)] = key
    try:
        hits, times = 0, []
        for want, title, desc in VACANCIES:
            t0 = time.monotonic()
            try:
                got = await ai_analyzer.is_vacancy_suitable(title, desc)
            except ProviderError as e:
                print(f"  ❌ {title}: {e}")
                continue
            dt = time.monotonic() - t0
            times.append(dt)
            hits += got == want
            print(f"  {'✅' if got == want else '❌'} {'YES' if got else 'NO ':3} "
                  f"(ждали {'YES' if want else 'NO'})  {title}  —  {dt:.1f} с")
        if times:
            print(f"  Итог {hits}/{len(VACANCIES)}, "
                  f"среднее {sum(times)/len(times):.1f} с на вакансию")

        print("\n[4/4] Сопроводительное письмо")
        t0 = time.monotonic()
        try:
            letter = await ai_analyzer.generate_cover_letter(
                "QA Engineer", "Ручное тестирование web и API, Postman, REST. Удалённо.")
        except ProviderError as e:
            print(f"  ❌ {e}")
            return
        dt = time.monotonic() - t0
        latin = sum(c.isascii() and c.isalpha() for c in letter)
        print(f"  Написано за {dt:.1f} с, {len(letter)} символов, "
              f"{len(letter.split(chr(10)+chr(10)))} абзаца(ев)")
        print(f"  Латиницы: {latin} симв. "
              f"({'нормально — термины' if latin < len(letter) * 0.15 else 'МНОГО, возможен английский'})")
        print(f"  Подпись: {letter.strip().splitlines()[-1].strip()!r}")
        print("\n" + "─" * 60)
        print(letter)
        print("─" * 60)
    finally:
        settings.data["llm"].update(saved)
        settings._secret_cache.clear()


if __name__ == "__main__":
    asyncio.run(main())
