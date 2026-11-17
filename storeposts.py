"""Publicacion automatica de ofertas de PowerPlanet y AliExpress entre las de Amazon (canal `concriterio`).

Este modulo solo decide: que tiendas tienen cupo hoy, si esta franja toca una oferta de tienda, y que candidatos
valen (los que el cruce con Amazon confirma como oferta real, y los que has aprobado en la lista de revision).
No hace peticiones ni habla con Telegram: deals.py comprueba la ficha y publica.

Reglas:
  * PowerPlanet: solo `oferta_real` en el cruce con Amazon (hecho hace pocos dias y con el mismo precio de hoy),
    en stock, con la lista de candidatos reciente. Los demas veredictos no se publican. Ademas, el producto de
    Amazon tiene que haberse encontrado por EAN (por nombre o modelo se confunden versiones: una RTX 5060 con una
    5070 Ti, una dashcam con otra) y el ahorro frente a Amazon no puede pasar de MAX_SAVING_VS_AMAZON %.
  * AliExpress: solo los productos aprobados en la lista de revision (✅ tuyo o tienda de confianza).
  * Cupo diario por tienda, repartido sin horas fijas: en cada franja se echa a suertes si toca, con probabilidad
    (cupo que falta) / (franjas que quedan en la ventana).
"""
from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta

STORES = ("powerplanet", "aliexpress")
LABELS = {"powerplanet": "POWERPLANET", "aliexpress": "ALIEXPRESS"}

CANDIDATES_MAX_AGE_DAYS = 1   # la lista de candidatos de PowerPlanet no puede ser mas vieja que esto
CROSS_MAX_AGE_DAYS = 3        # el cruce con Amazon no puede ser mas viejo (se repite cada 2 dias)
MAX_SAVING_VS_AMAZON = 55     # un ahorro mayor frente a Amazon es casi siempre un emparejamiento o un precio erroneo
SAME_PRICE = 0.01             # el precio de hoy no puede haber cambiado mas de un 1 % desde el cruce
APPROVAL_MAX_AGE_DAYS = 4     # un producto de AliExpress aprobado caduca (el precio cambia): se vuelve a pedir
MIN_MATCH_VERSION = 3   # = crossamz.MATCH_VERSION: no se publica con un cruce hecho con reglas viejas
MAX_TRIES = 3                 # candidatos que se comprueban en una franja antes de dejarlo para otra

MONTHS = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10,
          "nov": 11, "dic": 12}
DELIVERY_DATE_RE = re.compile(r"(\d{1,2})\s+(?:de\s+)?(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)\w*", re.I)
SOLD_OUT_RE = re.compile(
    r"schema\.org/(?:OutOfStock|SoldOut|Discontinued)|availability[\"']?\s*[:=]\s*[\"']?(?:out\s*of\s*stock|outofstock)"
    r"|producto\s+agotado|temporalmente\s+agotado", re.I)


def _age_days(iso: str, now: datetime) -> float | None:
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    return (now - then).total_seconds() / 86400


def counts_today(state: dict, today: date, tz) -> dict[str, int]:
    """Publicaciones de hoy de cada tienda (entradas con `st` en el estado del canal)."""
    counts = {s: 0 for s in STORES}
    by_label = {v: k for k, v in LABELS.items()}
    for entry in state.values():
        store = by_label.get(entry.get("st", "")) if isinstance(entry, dict) else None
        if not store or entry.get("manual"):  # lo publicado a mano no gasta el cupo automatico
            continue
        try:
            when = datetime.fromisoformat(entry["t"]).astimezone(tz).date()
        except (KeyError, ValueError, TypeError):
            continue
        if when == today:
            counts[store] += 1
    return counts


def remaining(daily: dict, counts: dict[str, int]) -> dict[str, int]:
    """Cupo que falta hoy de cada tienda (solo las que tienen cupo configurado)."""
    out = {}
    for store in STORES:
        left = int(daily.get(store, 0) or 0) - counts.get(store, 0)
        if left > 0:
            out[store] = left
    return out


def probability(total_left: int, minutes_left: float, avg_interval: float) -> float:
    """Probabilidad de que ESTA franja sea de tienda: cupo que falta / franjas que quedan (la actual incluida)."""
    if total_left <= 0:
        return 0.0
    slots = max(1.0, minutes_left / max(avg_interval, 1.0) + 1)
    return min(1.0, total_left / slots)


def draw(total_left: int, minutes_left: float, avg_interval: float, rng=random.random) -> bool:
    return rng() < probability(total_left, minutes_left, avg_interval)


def store_order(left: dict[str, int], daily: dict, counts: dict[str, int]) -> list[str]:
    """Tiendas con cupo, la mas atrasada primero (menos hecha respecto a su cupo); a igualdad, PowerPlanet."""
    def done(store: str) -> float:
        return counts.get(store, 0) / max(1, int(daily.get(store, 1) or 1))
    return sorted(left, key=lambda s: (done(s), STORES.index(s)))


def delivery_days(text: str, today: date) -> int | None:
    """Dias hasta la entrega que dice la ficha ('Recibelo el martes 24 nov' -> 7). None si no se entiende."""
    m = DELIVERY_DATE_RE.search(text or "")
    if not m:
        return None
    day, month = int(m.group(1)), MONTHS[m.group(2).lower()[:3]]
    for year in (today.year, today.year + 1):
        try:
            when = date(year, month, day)
        except ValueError:
            return None
        if when >= today - timedelta(days=1):
            return (when - today).days
    return None


def page_sold_out(html: str) -> bool:
    """La ficha dice que esta agotado (texto visible o disponibilidad de los datos estructurados)."""
    return bool(SOLD_OUT_RE.search(html or ""))


def pp_candidates(pp: dict, cross: dict, state: dict, now: datetime) -> list[dict]:
    """Candidatos de PowerPlanet publicables, mejores primero. Cada uno lleva `key` (pp:<id>), el titulo, precios,
    imagen, enlace de afiliado (sin tocar), la ficha de la web si se conoce y la nota del cruce."""
    age = _age_days(f"{pp.get('day', '')}T23:59:59+00:00", now)
    if age is None or age > CANDIDATES_MAX_AGE_DAYS + 1:
        return []
    items = (cross or {}).get("items") or {}
    out = []
    for cand in pp.get("candidates") or []:
        pid = str(cand.get("id", "")).strip()
        key = f"pp:{pid}"
        entry = items.get(pid)
        if not pid or key in state or not entry or entry.get("veredicto") != "oferta_real":
            continue
        if cand.get("foreign") or cand.get("stock") == 0 or not cand.get("link") or not cand.get("price"):
            continue
        if entry.get("v", 1) < MIN_MATCH_VERSION:  # cruce hecho con reglas antiguas de emparejamiento: se espera al nuevo
            continue
        if entry.get("encontrado_por") != "ean" or (entry.get("ahorro_vs_amazon") or 0) > MAX_SAVING_VS_AMAZON:
            continue
        cross_age = _age_days(entry.get("t", ""), now)
        if cross_age is None or cross_age > CROSS_MAX_AGE_DAYS:
            continue
        seen = entry.get("pp_price")
        if not seen or abs(seen - cand["price"]) / cand["price"] > SAME_PRICE:
            continue  # el precio cambio despues del cruce: hay que volver a cruzarlo
        out.append({
            "store": "powerplanet", "key": key, "id": pid, "title": cand.get("name", "").strip(),
            "price": cand["price"], "old": cand.get("rrp") or cand.get("median"), "image": cand.get("image", ""),
            "url": cand["link"], "page": cand.get("page", ""), "rating": None, "reviews": None,
            "score": cand.get("score") or 0, "note": entry.get("motivo", ""),
        })
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def ae_candidates(rev: dict, state: dict, now: datetime) -> list[dict]:
    """Productos de AliExpress aprobados en la lista de revision (y aun sin publicar), mejores primero."""
    out = []
    for rid, item in ((rev or {}).get("items") or {}).items():
        if item.get("st") != "ok" or item.get("src") != "a":
            continue
        key = f"ae:{item.get('id', '')}"
        age = _age_days(item.get("t", ""), now)
        if key in state or not item.get("id") or age is None or age > APPROVAL_MAX_AGE_DAYS or not item.get("price"):
            continue
        rate = item.get("rating")
        median = None
        if item.get("drop") and item.get("price"):
            median = round(item["price"] / (1 - item["drop"] / 100), 2) if item["drop"] < 100 else None
        out.append({
            "store": "aliexpress", "key": key, "id": str(item["id"]), "rid": rid, "title": item.get("title", ""),
            "price": item["price"], "old": median, "image": item.get("image", ""), "url": item.get("link", ""),
            "page": "", "rating": round(rate / 20, 1) if rate else None, "reviews": None,
            "score": item.get("score") or 0, "note": "",
        })
    out.sort(key=lambda c: c["score"], reverse=True)
    return out
