"""Единое хранилище настроек агента.

Раньше настройки были размазаны по config.py (запросы, резюме, фильтры) и
.env (токены). Для приложения с интерфейсом это не годится: настройки должны
меняться из UI и переживать обновление программы.

Здесь — живой объект `settings`, который читается и пишется в JSON в папке
пользователя. Секреты (токен бота, API-ключи) в JSON не попадают: они лежат
в Keychain через keyring.

При первом запуске настройки переносятся из старых config.py и .env, чтобы
консольная версия не потеряла настроенное.
"""
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

APP_NAME = "HHAgent"

# Дефолты намеренно обезличены: это то, что увидит новый пользователь.
# Личные данные подтягиваются миграцией из config.py при первом запуске.
DEFAULTS = {
    "search": {
        "queries": ["Тестировщик", "QA"],
        # Искать слова запроса только в названии вакансии. Если искать ещё и
        # по описанию (поведение HH по умолчанию), в выдачу лезут посторонние
        # вакансии, где нужные слова встретились в тексте.
        "title_only": True,
        # Без лимита агент уходит вглубь первого запроса на сотни вакансий и
        # до остальных не добирается за сеанс.
        "max_pages_per_query": 2,
        "regions": [
            {"name": "Москва (любой график)", "params": "&area=1"},
            {"name": "Вся Россия (только удаленка)", "params": "&area=113&schedule=remote"},
        ],
        "experience": ["between1And3", "between3And6", "moreThan6"],
    },
    "resume": {
        # Должно посимвольно совпадать с названием резюме на hh.ru,
        # иначе агент не найдёт его в момент отклика.
        "target_name": "",
        "summary": "",
    },
    "llm": {
        # ollama | openai_compat | anthropic
        "provider": "ollama",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "gemma4:e4b-it-qat",
        "openai_base_url": "http://localhost:1234/v1",
        "openai_model": "",
        "anthropic_model": "claude-sonnet-5",
        # Профиль (~600 токенов) + длинная вакансия (~2500) + промпт не влезают
        # в дефолтные 4096 у Ollama, а она молча обрезает промпт с начала.
        "num_ctx": 16384,
    },
    "notifications": {
        "telegram_enabled": False,
        "desktop_enabled": True,
        "tg_user_id": "",
    },
    "schedule": {
        # Пауза между полными проходами по всем запросам.
        "cycle_pause_minutes": 10,
        # Длительность сеанса в минутах; 0 — бессрочно.
        "session_minutes": 0,
    },
}

# Ключи секретов в Keychain
SECRET_KEYS = ("tg_bot_token", "anthropic_api_key", "openai_api_key")


def data_dir() -> Path:
    """Папка приложения. Разная на разных ОС, код при этом один."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def _deep_merge(base: dict, override: dict) -> dict:
    """Мерж по разделам: новые ключи из DEFAULTS появляются у существующих
    пользователей после обновления, а их значения не затираются."""
    out = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "settings.json")
        self.data = deepcopy(DEFAULTS)
        self._secret_cache: dict[str, str] = {}

    # ---------- загрузка и сохранение ----------

    def load(self) -> "Settings":
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = _deep_merge(DEFAULTS, json.load(f))
            except (json.JSONDecodeError, OSError) as e:
                print(f"⚠️ Не удалось прочитать {self.path}: {e}. Беру значения по умолчанию.")
        else:
            self.migrate_from_legacy()
            self.save()
        return self

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)  # атомарно: не портим файл при сбое записи

    def migrate_from_legacy(self):
        """Переносит настройки из старых config.py и .env при первом запуске."""
        try:
            import config as legacy
        except Exception:
            return

        s, r, llm, n, sch = (self.data["search"], self.data["resume"],
                             self.data["llm"], self.data["notifications"],
                             self.data["schedule"])

        s["queries"] = list(getattr(legacy, "SEARCH_QUERIES", s["queries"]))
        s["title_only"] = bool(getattr(legacy, "SEARCH_IN_TITLE_ONLY", s["title_only"]))
        s["max_pages_per_query"] = int(getattr(legacy, "MAX_PAGES_PER_QUERY", s["max_pages_per_query"]))
        r["target_name"] = getattr(legacy, "TARGET_RESUME_NAME", "") or ""
        r["summary"] = (getattr(legacy, "MY_RESUME_SUMMARY", "") or "").strip()
        sch["cycle_pause_minutes"] = int(getattr(legacy, "CYCLE_PAUSE_MINUTES", sch["cycle_pause_minutes"]))

        # Старый OLLAMA_URL указывал на /api/generate; провайдеру нужен корень.
        old_url = getattr(legacy, "OLLAMA_URL", "") or ""
        if old_url:
            llm["ollama_url"] = old_url.split("/api/")[0]
        llm["ollama_model"] = getattr(legacy, "OLLAMA_MODEL", llm["ollama_model"])

        tg_id = getattr(legacy, "TG_USER_ID", "") or ""
        if tg_id and not tg_id.startswith("YOUR_"):
            n["tg_user_id"] = tg_id
            n["telegram_enabled"] = True

        token = getattr(legacy, "TG_BOT_TOKEN", "") or ""
        if token and not token.startswith("YOUR_"):
            self.set_secret("tg_bot_token", token)

        print(f"✅ Настройки перенесены из config.py в {self.path}")

    # ---------- секреты ----------

    def get_secret(self, name: str) -> str:
        if name in self._secret_cache:
            return self._secret_cache[name]
        value = ""
        try:
            import keyring
            value = keyring.get_password(APP_NAME, name) or ""
        except Exception:
            # keyring может быть недоступен (нет бэкенда) — не падаем,
            # секреты просто окажутся пустыми, о чём скажет UI.
            pass
        if not value:
            value = os.getenv(name.upper(), "")
        self._secret_cache[name] = value
        return value

    def set_secret(self, name: str, value: str):
        self._secret_cache[name] = value
        try:
            import keyring
            if value:
                keyring.set_password(APP_NAME, name, value)
            else:
                keyring.delete_password(APP_NAME, name)
        except Exception as e:
            print(f"⚠️ Не удалось сохранить «{name}» в Keychain: {e}")

    # ---------- быстрый доступ к часто используемым значениям ----------

    @property
    def search_queries(self) -> list[str]:
        return self.data["search"]["queries"]

    @property
    def title_only(self) -> bool:
        return self.data["search"]["title_only"]

    @property
    def max_pages_per_query(self) -> int:
        return self.data["search"]["max_pages_per_query"]

    @property
    def regions(self) -> list[dict]:
        return self.data["search"]["regions"]

    @property
    def experience(self) -> list[str]:
        return self.data["search"]["experience"]

    @property
    def target_resume_name(self) -> str:
        return self.data["resume"]["target_name"]

    @property
    def resume_summary(self) -> str:
        return self.data["resume"]["summary"]

    @property
    def cycle_pause_minutes(self) -> int:
        return self.data["schedule"]["cycle_pause_minutes"]

    @property
    def tg_user_id(self) -> str:
        return self.data["notifications"]["tg_user_id"]

    @property
    def tg_bot_token(self) -> str:
        return self.get_secret("tg_bot_token")


# Единственный экземпляр на процесс: и CLI, и UI работают с ним.
settings = Settings().load()
