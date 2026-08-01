"""Публичные справочники hh.ru для селектов в интерфейсе.

api.hh.ru не требует авторизации, но требует осмысленный заголовок
HH-User-Agent и доступен не из всех сетей: из-за рубежа отдаёт 403.
Поэтому всё здесь строится вокруг деградации: сеть → кэш → пусто,
а интерфейс при пустом справочнике переключается на ручной ввод.
"""
import json
import time

import aiohttp

from settings import data_dir

AREAS_URL = "https://api.hh.ru/areas"
# Требование API: заголовок вида "имя-приложения/версия (контакт)".
# Контакт — репозиторий проекта, а не личная почта разработчика: этот
# заголовок уходит с каждым запросом каждого пользователя приложения.
HH_USER_AGENT = "AbuHH/0.1 (+https://github.com/fikstt2/hh-ai-agent)"
# v2: старый кэш хранил записи без поля "root" (страна узла), появившегося
# вместе с country_subtree() для мультидоменности. Смена имени файла — самый
# простой способ заставить существующих пользователей переполучить дерево
# в новом формате, не трогая CACHE_TTL и не изобретая миграцию на месте.
CACHE_FILE = "hh_areas_v2.json"
CACHE_TTL = 30 * 24 * 3600  # регионы меняются редко

# График работы — из /dictionaries. Захардкожен осознанно: этот словарь
# меняется годами, а тащить сеть ради пяти значений незачем.
SCHEDULES = [
    {"id": "", "name": "Любой график"},
    {"id": "remote", "name": "Только удалёнка"},
    {"id": "fullDay", "name": "Полный день"},
    {"id": "flexible", "name": "Гибкий график"},
    {"id": "shift", "name": "Сменный график"},
]


def _cache_path():
    return data_dir() / CACHE_FILE


def _read_cache():
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data.get("saved_at", 0), data.get("areas", [])
    except (OSError, json.JSONDecodeError):
        return 0, []


def _write_cache(areas):
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.time(), "areas": areas}, f, ensure_ascii=False)
        tmp.replace(path)
    except OSError:
        pass  # кэш — оптимизация, не обязанность


def _flatten(nodes, parent_name="", root_name=None, out=None):
    """Дерево регионов → плоский список для поиска по подстроке.

    root_name — имя корневого узла (страны) для каждой записи, чтобы потом
    можно было сузить список одной страной (country_subtree) без похода
    обратно в дерево, которое после flatten уже потеряно."""
    if out is None:
        out = []
    for node in nodes:
        this_root = root_name if root_name is not None else node["name"]
        out.append({
            "id": node["id"],
            "name": node["name"],
            "parent": parent_name,
            "root": this_root,
        })
        if node.get("areas"):
            _flatten(node["areas"], node["name"], this_root, out)
    return out


def country_subtree(areas: list, area_id: str | None) -> list:
    """Сужает список регионов до одной страны — по id её корневого узла
    (см. sites.py, поле "area").

    Кэш, сохранённый до появления мультидоменности, не содержит "root" —
    в этом случае возвращаем список как есть: лучше показать все страны,
    чем внезапно пустой справочник у существующего пользователя."""
    if not area_id or not areas or "root" not in areas[0]:
        return areas
    root_name = next((a["name"] for a in areas if a["id"] == area_id), None)
    if root_name is None:
        return areas
    return [a for a in areas if a["root"] == root_name]


async def fetch_areas() -> tuple[list, str]:
    """Возвращает (areas, source), где source: network | cache | none.

    Свежий кэш избавляет от сетевого запроса вовсе; при недоступной сети
    годится кэш любой давности — регионы не та вещь, что устаревает.
    """
    saved_at, cached = _read_cache()
    if cached and time.time() - saved_at < CACHE_TTL:
        return cached, "cache"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                AREAS_URL,
                headers={"HH-User-Agent": HH_USER_AGENT, "User-Agent": HH_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=6),
            ) as r:
                r.raise_for_status()
                tree = await r.json()
        areas = _flatten(tree)
        _write_cache(areas)
        return areas, "network"
    except Exception:
        if cached:
            return cached, "cache"
        return [], "none"
