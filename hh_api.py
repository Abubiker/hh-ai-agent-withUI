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
# Требование API: заголовок вида "имя-приложения/версия (контакт)"
HH_USER_AGENT = "HHAgent/0.1 (dmitrobuber@gmail.com)"
CACHE_FILE = "hh_areas.json"
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


def _flatten(nodes, parent_name="", out=None):
    """Дерево регионов → плоский список для поиска по подстроке."""
    if out is None:
        out = []
    for node in nodes:
        out.append({
            "id": node["id"],
            "name": node["name"],
            "parent": parent_name,
        })
        if node.get("areas"):
            _flatten(node["areas"], node["name"], out)
    return out


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
