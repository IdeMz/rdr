"""Selector de ofertas del feed de Awin (PowerPlanet). De momento solo en modo informe.

Cada dia, cuando feed.py guarda el historial de precios, este modulo busca los productos que de verdad
acaban de bajar y deja la lista en state/powerplanet_candidates.json. No publica nada y no conoce Telegram.

El "Antes" del feed (rrp_price) es el PVPR y casi todo aparece rebajado, asi que no se usa para decidir.
Un producto es candidato cuando, segun el historial propio del feed:
  1. lleva al menos MIN_TRACK_DAYS dias registrado,
  2. su precio actual es un MINIMO NUEVO de la ventana: al menos NEW_LOW_MARGIN % por debajo de todos los
     precios anteriores (descarta "subir el precio y volver a bajarlo": el precio de antes ya existia),
  3. esta al menos MIN_DROP % por debajo de su mediana de 30 dias,
  4. la bajada es reciente (el precio actual empezo hace como mucho FRESH_DAYS dias). Si pasa de MAX_DROP %
     no se descarta: va a la lista "to_verify" para contrastarla con Amazon antes de publicar. Solo por
     encima de ABSURD_DROP % (6.118 a 700 EUR) se considera un error de datos y se aparta en "suspicious",
  5. cuesta al menos MIN_PRICE, hay stock y la oferta no ha caducado.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pricehist

# En modo informe se aflojan para ver la mecanica. Antes de publicar de verdad: MIN_TRACK_DAYS = 8 o mas.
MIN_TRACK_DAYS = 3
NEW_LOW_MARGIN = 3     # % por debajo del minimo anterior
MIN_DROP = 10          # % por debajo de la mediana de 30 dias
MAX_DROP = 50          # una bajada mayor no se propone sin mas: va a "to_verify" (se contrasta con Amazon)
ABSURD_DROP = 80       # una bajada mayor es casi seguro un error de datos y solo se lista en "suspicious"
FRESH_DAYS = 7         # dias desde que empezo el precio actual
MIN_PRICE = 25.0
LIMIT = 80             # candidatos que se guardan
SUSPICIOUS_LIMIT = 30  # bajadas "a comprobar" y sospechosas que se guardan (cada lista) para revisarlas
SIMILARITY = 0.5       # nombres tan parecidos se consideran el mismo producto (otro color, por ejemplo)


def _text(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def _num(value: str) -> float | None:
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def items_from_rows(rows: list[dict]) -> dict[str, dict]:
    """{id: datos del producto} de las filas nuevas, en stock y con precio (las mismas que entran al historial)."""
    import feed  # aqui para no importar en circulo: feed llama a este modulo

    items: dict[str, dict] = {}
    for row in rows:
        pid = _text(row, "aw_product_id")
        price = feed.row_price(row)
        if not pid or price is None or price <= 0 or not feed.row_usable(row):
            continue
        rrp = _num(_text(row, "rrp_price"))
        stock = _num(_text(row, "stock_quantity"))
        items[pid] = {
            "id": pid,
            "name": _text(row, "product_name"),
            "brand": _text(row, "brand_name"),
            "price": price,
            "rrp": rrp if rrp and rrp > price else None,
            "link": _text(row, "aw_deep_link"),
            "image": _text(row, "large_image") or _text(row, "merchant_image_url") or _text(row, "aw_image_url"),
            "ean": _text(row, "ean"),
            "category": _text(row, "merchant_product_category_path") or _text(row, "category_name"),
            "commission_group": _text(row, "commission_group"),
            "stock": int(stock) if stock is not None else None,
            "valid_to": _text(row, "valid_to"),
            "rating": _num(_text(row, "average_rating")),
            "reviews": int(r) if (r := _num(_text(row, "reviews"))) is not None else None,
        }
    return items


def _expired(valid_to: str, today: date) -> bool:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", valid_to or "")
    if not m:
        return False
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) < today
    except ValueError:
        return False


def judge(item: dict, entry: list | None, today: date) -> tuple[dict | None, str]:
    """(datos de la bajada, "") si el producto es candidato; (None, motivo) si no.

    Solo la "bajada sospechosa" devuelve tambien los datos junto al motivo. Una bajada de MAX_DROP a ABSURD_DROP %
    es candidata con info["verify"] = True."""
    t = pricehist.day_no(today)
    if not entry or len(entry) < 3 or entry[0] != t:
        return None, "sin historial de hoy"
    pairs = [(entry[i], entry[i + 1]) for i in range(1, len(entry) - 1, 2)]
    if not pairs:
        return None, "sin historial de hoy"
    tracked = t - pairs[0][0] + 1
    if tracked < MIN_TRACK_DAYS:
        return None, "historial corto"
    if len(pairs) < 2:
        return None, "precio sin cambios"
    price = item["price"]
    if price < MIN_PRICE:
        return None, "precio bajo"
    if item["stock"] is not None and item["stock"] <= 0:
        return None, "sin stock"
    if _expired(item["valid_to"], today):
        return None, "oferta caducada"
    age = t - pairs[-1][0]
    if age > FRESH_DAYS:
        return None, "bajada antigua"
    prior = pricehist.window_stats(pairs[:-1], t)
    now = pricehist.window_stats(pairs, t)
    if prior is None or now is None:
        return None, "sin historial de hoy"
    if price > prior["low"] * (1 - NEW_LOW_MARGIN / 100):
        return None, "no es minimo nuevo"
    drop = round((1 - price / now["median"]) * 100)
    if drop < MIN_DROP:
        return None, "bajada pequena"
    info = {
        "median": now["median"],
        "prior_low": prior["low"],
        "drop": drop,
        "drop_vs_low": round((1 - price / prior["low"]) * 100),
        "tracked_days": tracked,
        "level_days": age + 1,
    }
    worst = max(drop, info["drop_vs_low"])
    if worst > ABSURD_DROP:
        return info, "bajada sospechosa"  # se devuelven los datos para poder revisarla en el informe
    if worst > MAX_DROP:
        info["verify"] = True  # candidato, pero hay que contrastarlo antes de publicarlo
    return info, ""


def _tokens(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9áéíóúüñ]+", name.lower()) if len(w) > 2}


def _same_product(a: str, b: str) -> bool:
    return _same_tokens(_tokens(a), _tokens(b))


def _same_tokens(ta: set[str], tb: set[str]) -> bool:
    if not ta or not tb:
        return False
    common = len(ta & tb)
    if common / len(ta | tb) >= SIMILARITY:
        return True
    # una variante suele ser el mismo nombre con una o dos palabras cambiadas
    smaller = min(len(ta), len(tb))
    return smaller >= 4 and common / smaller >= 0.8


def select(items: dict[str, dict], hist: dict, today: date, suspicious: list | None = None,
           to_verify: list | None = None) -> tuple[list[dict], dict]:
    """Candidatos ordenados de mejor a peor y recuento de por que se descartaron los demas.

    Las bajadas sospechosas (probables errores de precio) se anaden a `suspicious` y las que hay que contrastar
    con otra tienda antes de publicarlas (bajada de 50 a 80 %) a `to_verify`, si se pasan esas listas."""
    why: Counter = Counter()
    found: list[dict] = []
    check: list[dict] = []
    products = hist.get("p", {})
    for pid, item in items.items():
        info, reason = judge(item, products.get(pid), today)
        if reason:
            why[reason] += 1
            if reason == "bajada sospechosa" and suspicious is not None:
                suspicious.append({**item, **info})
            continue
        cand = {**item, **info, "score": round(info["drop"] * math.log10(item["price"]), 2)}
        if info.get("verify"):
            why["a comprobar"] += 1
            check.append(cand)
        else:
            found.append(cand)
    found.sort(key=lambda c: c["score"], reverse=True)
    picks: list[dict] = []
    for cand in found:
        if any(_same_product(cand["name"], p["name"]) for p in picks):
            why["variante de otro candidato"] += 1
            continue
        picks.append(cand)
        if len(picks) >= LIMIT:
            break
    check.sort(key=lambda c: c["score"], reverse=True)
    if to_verify is not None:
        to_verify.extend(check)
    why["candidatos"] = len(picks)
    return picks, dict(why)


def build(rows: list[dict], hist: dict, today: date) -> dict:
    items = items_from_rows(rows)
    suspicious: list[dict] = []
    to_verify: list[dict] = []
    picks, stats = select(items, hist, today, suspicious, to_verify)
    suspicious.sort(key=lambda c: c["price"], reverse=True)
    return {
        "day": today.isoformat(),
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "informe",
        "thresholds": {
            "min_track_days": MIN_TRACK_DAYS, "new_low_margin": NEW_LOW_MARGIN, "min_drop": MIN_DROP,
            "max_drop": MAX_DROP, "absurd_drop": ABSURD_DROP, "fresh_days": FRESH_DAYS, "min_price": MIN_PRICE,
        },
        "products": len(items),
        "stats": stats,
        "to_verify": to_verify[:SUSPICIOUS_LIMIT],
        "suspicious": suspicious[:SUSPICIOUS_LIMIT],
        "candidates": picks,
    }


LIST_KEYS = ("to_verify", "suspicious", "candidates")


def save(path: Path, data: dict) -> None:
    """Un producto por linea en cada lista (se lee mejor y git guarda solo lo que cambia)."""
    head = {k: v for k, v in data.items() if k not in LIST_KEYS}
    lines = [json.dumps(head, ensure_ascii=False, separators=(",", ":"))[:-1]]
    present = [k for k in LIST_KEYS if k in data]
    for n, key in enumerate(present):
        rows = data[key]
        lines[-1] += f',"{key}":['
        for i, row in enumerate(rows):
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + ("," if i < len(rows) - 1 else ""))
        lines.append("]")
    lines[-1] += "}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
