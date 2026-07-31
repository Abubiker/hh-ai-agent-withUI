"""Реестр площадок HeadHunter в СНГ — hh.ru и его "сёстры" в других странах.

Все площадки группы работают на одной фронтовой кодовой базе (общие data-qa,
общая структура URL), но у каждой свой домен и свой отдельный логин: сессия
Playwright (state.json), сохранённая на hh.ru, не работает на hh.kz — это
разные аккаунты HH Group. Поэтому у каждой площадки свой файл сессии.

Этот модуль — единственное место, где домен площадки превращается в URL или
путь к файлу. Остальной код обращается сюда, а не собирает "https://hh.ru/..."
руками.
"""
import re
from urllib.parse import urljoin

# area — id страны в дереве api.hh.ru/areas, нужен для сужения выбора региона
# под активную площадку. Подтверждён только "113" (Россия, совпадает с
# дефолтным регионом в settings.py). Остальные — предположение по обычной
# нумерации HH и требуют проверки через hh_api.fetch_areas() перед тем, как
# на них полагаться для чего-то важнее подсказки в UI.
SITES: list[dict] = [
    {"id": "hh.ru", "host": "hh.ru", "name": "hh.ru — Россия",
     "area": "113", "state": "state.json"},
    {"id": "hh.kz", "host": "hh.kz", "name": "hh.kz — Казахстан",
     "area": "40", "state": "state.hh.kz.json"},
    {"id": "hh.uz", "host": "hh.uz", "name": "hh.uz — Узбекистан",
     "area": "97", "state": "state.hh.uz.json"},
    {"id": "rabota.by", "host": "rabota.by", "name": "rabota.by — Беларусь",
     "area": "16", "state": "state.rabota.by.json"},
    {"id": "hh1.az", "host": "hh1.az", "name": "hh1.az — Азербайджан",
     "area": "9", "state": "state.hh1.az.json"},
]

DEFAULT_SITE_ID = "hh.ru"

# Альтернация хостов для регулярок разбора ссылок из чата. Порядок неважен,
# re не жадный по альтернативам одинаковой структуры.
HOSTS_RE = r"(?:hh\.ru|hh\.kz|hh\.uz|rabota\.by|hh1\.az)"

_BY_ID = {s["id"]: s for s in SITES}
_BY_HOST = {s["host"]: s for s in SITES}


def all_sites() -> list[dict]:
    return SITES


def by_id(site_id: str | None) -> dict | None:
    return _BY_ID.get(site_id)


def by_host(host: str | None) -> dict | None:
    if not host:
        return None
    return _BY_HOST.get(host.lower().lstrip("www."))


def detect(url: str | None) -> dict | None:
    """Определяет площадку по ссылке, вставленной пользователем в чат.

    Понимает ссылку с любым префиксом схемы/www, лишь бы хост совпадал с
    одной из площадок группы. Возвращает None для чужих доменов.
    """
    if not url:
        return None
    m = re.search(rf"(?:https?://)?(?:www\.)?({HOSTS_RE})", url, re.IGNORECASE)
    if not m:
        return None
    return by_host(m.group(1))


def active_site() -> dict:
    """Площадка, выбранная в настройках. Фолбэк на hh.ru — так же ведёт себя
    код, писавшийся до появления мультидоменности."""
    from settings import settings
    site_id = settings.data.get("site", {}).get("active", DEFAULT_SITE_ID)
    return by_id(site_id) or _BY_ID[DEFAULT_SITE_ID]


def base_url(site: dict | None = None) -> str:
    site = site or active_site()
    return f"https://{site['host']}"


def url(path: str, site: dict | None = None) -> str:
    """Достраивает путь (относительный или уже абсолютный) до полного URL
    активной (или переданной) площадки."""
    site = site or active_site()
    return urljoin(base_url(site) + "/", path.lstrip("/")) if not path.startswith("http") else path


def state_file(site: dict | None = None) -> str:
    """Путь к файлу сохранённой сессии площадки.

    Для hh.ru — ровно "state.json", как и до появления этого модуля: у
    существующих пользователей сессия не должна считаться потерянной после
    обновления.
    """
    from settings import user_file
    site = site or active_site()
    return str(user_file(site["state"]))


def is_logged_in(site: dict | None = None) -> bool:
    import os
    return os.path.exists(state_file(site))


def known_host(u: str) -> bool:
    return detect(u) is not None
