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
import shutil
import sys
from copy import deepcopy
from pathlib import Path

APP_NAME = "HHAgent"

# Причины, по которым ИИ отклоняет вакансию. Значение по умолчанию нарочно
# универсальное — оно годится любой профессии. Своё пишется в окне, вкладка
# «Фильтры»: критерии у каждого свои, и угадать их за человека нельзя.
DEFAULT_EXCLUSIONS = """\
- Это управленческая роль, а не работа руками: есть прямые подчинённые, найм и
  оценка сотрудников, ответственность за стратегию всего отдела.
- Требуемый опыт заметно больше моего — как жёсткое требование, а не пожелание.
- Профессия вообще другая, не та, что указана в моём профиле.
- Свободный разговорный английский (B2, C1, Upper-Intermediate) заявлен как
  обязательное требование. Если он «будет плюсом» — это не повод для отказа.
- Основной инструмент или язык — тот, которого нет в моём профиле, и он нужен
  именно как основной, а не «будет плюсом»."""

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
        # enabled управляет тем, ходит ли агент по региону в ЭТОМ сеансе —
        # можно быстро отключить регион с экрана «Работа», не удаляя его
        # настройку (график, параметры) из Фильтров.
        "regions": [
            {"name": "Москва (любой график)", "params": "&area=1", "enabled": True},
            {"name": "Вся Россия (только удаленка)", "params": "&area=113&schedule=remote", "enabled": True},
        ],
        "experience": ["between1And3", "between3And6", "moreThan6"],
        # Отклики без сопроводительного часто не рассматривают, поэтому по
        # умолчанию пустой отклик не отправляется: вакансия уходит в
        # уведомление с готовым письмом, чтобы откликнуться вручную.
        "require_letter": True,
        # Когда ИИ должен отклонить вакансию. Раньше этот список был вшит
        # в промпт и описывал одну конкретную профессию: у любого другого
        # человека классификатор отсеивал как раз то, что ему нужно.
        "exclusions": DEFAULT_EXCLUSIONS,
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
        # О чём именно сообщать. Отклики и капча важны почти всем, а вот
        # поток служебных сообщений часто раздражает — пусть выбирает сам.
        "events": {
            "applied": True,    # отклик отправлен
            "reply": True,      # ответ от работодателя
            "captcha": True,    # нужна помощь с капчей
            "summary": True,    # итоги сеанса
            "error": True,      # ошибки в работе
        },
    },
    "schedule": {
        # Пауза между полными проходами по всем запросам.
        "cycle_pause_minutes": 10,
        # Длительность сеанса в минутах; 0 — бессрочно.
        "session_minutes": 0,
    },
    "security": {
        # False — токены в файле 0600 (без запросов пароля от системы).
        # True  — в Связке ключей: надёжнее, но macOS будет спрашивать пароль
        #         после каждого обновления приложения.
        "use_keychain": False,
    },
}

SECRET_KEYS = ("tg_bot_token", "anthropic_api_key", "openai_api_key")

# Где хранить токены. По умолчанию — файл с правами 0600 рядом с настройками.
#
# Почему не Связка ключей по умолчанию: доступ к записи в Связке привязан к
# подписи программы, а ad-hoc подпись меняется при каждой пересборке. Из-за
# этого macOS после каждого обновления требует пароль от Связки — пугающее
# окно, которое многих отталкивает. Файл 0600 читается только владельцем;
# для токена бота и API-ключей это разумный размен.
#
# Кому нужна Связка — в настройках можно переключить обратно.
SECRETS_FILE = "secrets.json"


def data_dir() -> Path:
    """Папка приложения. Разная на разных ОС, код при этом один."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def user_file(name: str) -> Path:
    """Путь к пользовательскому файлу (сессия HH, база откликов).

    Класть их рядом с кодом нельзя: внутри собранного .app это временная
    распакованная папка, доступная только на чтение и стираемая при выходе —
    вход в аккаунт слетал бы при каждом запуске.

    Если файл остался от консольной версии, переносим его один раз.
    """
    target = data_dir() / name
    if not target.exists():
        legacy = Path(__file__).resolve().parent / name
        try:
            if legacy.exists() and legacy != target:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy), str(target))
                print(f"✅ {name} перенесён в {target.parent}")
        except Exception as e:
            print(f"⚠️ Не удалось перенести {name}: {e}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


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
        # Пустое значение — не «перенеси пустоту», а «здесь ничего своего нет»:
        # оставляем модель по умолчанию из DEFAULTS.
        old_model = getattr(legacy, "OLLAMA_MODEL", "") or ""
        if old_model:
            llm["ollama_model"] = old_model

        tg_id = getattr(legacy, "TG_USER_ID", "") or ""
        if tg_id and not tg_id.startswith("YOUR_"):
            n["tg_user_id"] = tg_id
            n["telegram_enabled"] = True

        token = getattr(legacy, "TG_BOT_TOKEN", "") or ""
        if token and not token.startswith("YOUR_"):
            self.set_secret("tg_bot_token", token)

        print(f"✅ Настройки перенесены из config.py в {self.path}")

    # ---------- секреты ----------

    @property
    def use_keychain(self) -> bool:
        return bool(self.data.get("security", {}).get("use_keychain", False))

    def _secrets_path(self) -> Path:
        return self.path.parent / SECRETS_FILE

    def _read_secrets_file(self) -> dict:
        p = self._secrets_path()
        if not p.exists():
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_secrets_file(self, data: dict):
        p = self._secrets_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)  # читает только владелец
        tmp.replace(p)
        os.chmod(p, 0o600)

    @staticmethod
    def scoped_secret_name(base_name: str, url: str) -> str:
        """Имя секрета, привязанное к сервису.

        Ключ у OpenAI-совместимых сервисов был один на всех: настроил Groq,
        переключился на Mistral — ключ Groq затёрт, и обратно уже не вернуться
        без повторного ввода. Привязываем к хосту адреса.
        """
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return f"{base_name}@{host}" if host else base_name

    def get_scoped_secret(self, base_name: str, url: str) -> str:
        """Ключ сервиса, с откатом на общий — он остался у тех, кто настраивал
        приложение до разделения ключей."""
        return (self.get_secret(self.scoped_secret_name(base_name, url))
                or self.get_secret(base_name))

    def set_scoped_secret(self, base_name: str, url: str, value: str):
        self.set_secret(self.scoped_secret_name(base_name, url), value)

    def get_secret(self, name: str) -> str:
        if name in self._secret_cache:
            return self._secret_cache[name]

        value = self._read_secrets_file().get(name, "")

        # Связку опрашиваем, только если пользователь сам её выбрал: иначе
        # macOS покажет запрос пароля, а это пугает больше, чем помогает.
        if not value and self.use_keychain:
            try:
                import keyring
                value = keyring.get_password(APP_NAME, name) or ""
            except Exception:
                pass

        if not value:
            value = os.getenv(name.upper(), "")

        self._secret_cache[name] = value
        return value

    def set_secret(self, name: str, value: str):
        self._secret_cache[name] = value

        if self.use_keychain:
            try:
                import keyring
                if value:
                    keyring.set_password(APP_NAME, name, value)
                else:
                    keyring.delete_password(APP_NAME, name)
                return
            except Exception as e:
                print(f"⚠️ Связка ключей недоступна ({e}); сохраняю в файл.")

        data = self._read_secrets_file()
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        self._write_secrets_file(data)

    def migrate_secrets_from_keychain(self) -> int:
        """Разовый перенос токенов из Связки в файл — чтобы после перехода
        не пришлось вводить их заново."""
        moved = 0
        try:
            import keyring
        except Exception:
            return 0
        data = self._read_secrets_file()
        for name in SECRET_KEYS:
            if data.get(name):
                continue
            try:
                v = keyring.get_password(APP_NAME, name)
            except Exception:
                continue
            if v:
                data[name] = v
                moved += 1
        if moved:
            self._write_secrets_file(data)
            self._secret_cache.clear()
        return moved

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
        """Все настроенные регионы — для экрана управления (Фильтры)."""
        return self.data["search"]["regions"]

    @property
    def active_regions(self) -> list[dict]:
        """Только включённые — то, по чему агент реально ходит в этом сеансе.
        Старые записи без ключа "enabled" (до этой настройки) считаются включёнными."""
        return [r for r in self.data["search"]["regions"] if r.get("enabled", True)]

    @property
    def experience(self) -> list[str]:
        return self.data["search"]["experience"]

    @property
    def require_letter(self) -> bool:
        """Не отправлять отклик, если сопроводительное приложить не удалось."""
        return bool(self.data["search"].get("require_letter", True))

    @property
    def exclusions(self) -> str:
        """Причины отклонить вакансию — текстом, как их пишет пользователь.

        Пустое поле означает «отклонять только по здравому смыслу», поэтому
        подставляем дефолт: без единого критерия классификатор пропускает всё.
        """
        return (self.data["search"].get("exclusions") or DEFAULT_EXCLUSIONS).strip()

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
