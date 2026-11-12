"""Delivery price by distance. The cafe marks its point on the map and sets zones — "up to 3 km: 10 000, up to 7 km:
15 000" — and the guest drops a pin in the Mini App. Distance is a straight line between the two points (no map
service, no keys, nothing to pay); owners set zones with that in mind. Without zones the flat delivery_fee applies
and the pin is only for the courier."""

from math import asin, cos, radians, sin, sqrt

from .models import Cafe

EARTH_KM = 6371.0
TASHKENT = (41.3111, 69.2797)  # where the map opens when nothing better is known


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in kilometres (haversine)."""
    lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * asin(sqrt(h))


def by_zones(cafe: Cafe) -> bool:
    """The price depends on where the guest is: then a pin is required."""
    return bool(cafe.delivery_zones and cafe.point)


def fee(cafe: Cafe, point: tuple[float, float] | None) -> int | None:
    """What delivery to this point costs; None — no delivery there (or no pin while the price depends on it)."""
    if not by_zones(cafe):
        return cafe.delivery_fee
    if point is None:
        return None
    distance = km(cafe.point, point)
    return next((price for limit, price in cafe.delivery_zones if distance <= limit), None)


def map_link(point: tuple[float, float]) -> str:
    """Yandex Maps with a pin: what couriers in Tashkent open; it also offers Navigator and Yandex Go."""
    lat, lon = point
    return f"https://yandex.uz/maps/?pt={lon:.6f},{lat:.6f}&z=17&l=map"


def parse_zones(rows: list[tuple[str, str]]) -> list[tuple[float, int]] | None:
    """Zones from the admin form, as (km, price) text pairs; blank rows are skipped. None if a row is wrong."""
    zones = []
    for limit, price in rows:
        limit, price = limit.strip().replace(",", "."), price.strip().replace(" ", "")
        if not limit and not price:
            continue
        try:
            zone = (float(limit), int(price))
        except ValueError:
            return None
        if zone[0] <= 0 or zone[1] < 0:
            return None
        zones.append(zone)
    return sorted(zones)
