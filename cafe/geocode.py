"""The street and house under the guest's pin, so the address field fills itself. OpenStreetMap's Nominatim: free,
no key; its rules — at most one request a second, a real User-Agent, cache what you can — are kept here. Tashkent is
mapped well for streets, less so for house numbers: the guest can always correct the line. Any failure just means
an empty suggestion; ordering never depends on it. With many cafes, run an own Nominatim or a paid geocoder here."""

import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)
URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "limybot/1.0 (delivery address suggestions)"}
SPACING = 1.1  # seconds between requests: Nominatim allows one a second
CACHE_MAX = 5000
MAX_WAITING = 5  # lookups queued at once; more get no suggestion (the guest types) rather than a long queue

_cache: dict[tuple[float, float, str], str | None] = {}
_lock = asyncio.Lock()
_last = 0.0
_waiting = 0


async def fetch(lat: float, lon: float, lang: str) -> dict | None:
    params = {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "addressdetails": 1, "accept-language": lang}
    timeout = aiohttp.ClientTimeout(total=6)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as s, s.get(URL, params=params) as r:
        return await r.json() if r.status == 200 else None


def line(data: dict) -> str | None:
    """ "улица Амира Темура, 15" — the street and the house, or the closest thing OpenStreetMap knows there."""
    a = data.get("address") or {}
    street = next(
        (a[k] for k in ("road", "pedestrian", "residential", "living_street", "footway", "path") if a.get(k)), None
    )
    area = next((a[k] for k in ("neighbourhood", "quarter", "suburb", "city_district") if a.get(k)), None)
    if street:
        return f"{street}, {a['house_number']}" if a.get("house_number") else street
    if area:
        return f"{area}, {a['house_number']}" if a.get("house_number") else area
    name = (data.get("display_name") or "").split(",")
    return ", ".join(p.strip() for p in name[:2]) or None


async def address(lat: float, lon: float, lang: str = "ru") -> str | None:
    """A suggestion for the address line, or None. ~10 m precision is plenty for a street and a house."""
    global _waiting
    key = (round(lat, 4), round(lon, 4), lang)
    if key in _cache:
        return _cache[key]
    if _waiting >= MAX_WAITING:  # someone floods it, or it is very busy: nobody should wait minutes
        return None
    _waiting += 1
    try:
        return await _lookup(key, lat, lon, lang)
    finally:
        _waiting -= 1


async def _lookup(key: tuple, lat: float, lon: float, lang: str) -> str | None:
    global _last
    async with _lock:  # one request at a time, spaced out
        if key in _cache:
            return _cache[key]
        wait = _last + SPACING - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last = time.monotonic()
        try:
            data = await fetch(lat, lon, lang)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            log.warning("address lookup failed: %s", e)
            return None  # not cached: maybe it works next time
        result = line(data) if data else None
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[key] = result
        return result
