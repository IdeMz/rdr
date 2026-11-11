"""Historial propio de precios de Amazon.

Amazon enseña la grafica de 30 dias con un asistente (Rufus) que el bot no puede consultar, y solo ~55 % de
las fichas traen escrito "El precio mas bajo de los ultimos 30 dias". Para el resto, el bot anota el precio
de cada producto que ve en los listados y con el tiempo sabe si una oferta esta cerca de su minimo o no.

Formato (state/precios_amazon.json, un producto por linea para que git guarde solo lo que cambia):

    {"p":{"B0XXXXXXXX":[ultimo_dia_visto, dias_vistos, dia1, precio1, dia2, precio2, ...], ...}}

Los dias son numeros (dias desde EPOCH). Cada pareja (dia, precio) es el precio MAS BAJO visto ese dia y se
anade solo cuando cambia, igual que en feed.py. Este modulo no conoce Telegram ni la red.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

EPOCH = date(2026, 1, 1)
KEEP_DAYS = 35      # sin verse tanto tiempo, el producto se olvida
WINDOW_DAYS = 30    # ventana del minimo y la mediana (como la de Amazon)
MIN_SEEN_DAYS = 3   # dias distintos en que hay que haber visto el producto...
MIN_SPAN_DAYS = 5   # ...repartidos al menos en tantos dias, para fiarse del historial


def day_no(d: date) -> int:
    return (d - EPOCH).days


def load(path: Path) -> dict:
    """Historial guardado, o uno vacio si no existe o esta danado."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"p": {}}
    if not isinstance(data, dict) or not isinstance(data.get("p"), dict):
        return {"p": {}}
    data["p"] = {k: v for k, v in data["p"].items() if _valid(v)}
    return data


def _valid(entry) -> bool:
    return (
        isinstance(entry, list)
        and len(entry) >= 4
        and len(entry) % 2 == 0
        and all(isinstance(x, (int, float)) for x in entry)
    )


def save(path: Path, hist: dict) -> None:
    """Un producto por linea y ordenado; escritura atomica."""
    head = {k: v for k, v in hist.items() if k != "p"}
    opening = json.dumps(head, ensure_ascii=False, separators=(",", ":"))[:-1]
    lines = [opening + (',"p":{' if head else '"p":{')]
    items = sorted(hist["p"].items())
    for i, (asin, entry) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f'{json.dumps(asin)}:{json.dumps(entry, separators=(",", ":"))}{comma}')
    lines.append("}}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def record(hist: dict, prices: dict[str, float], today: date) -> dict:
    """Anota los precios vistos hoy. Devuelve cifras para el log."""
    t = day_no(today)
    cutoff = t - KEEP_DAYS
    products = hist.setdefault("p", {})
    stats = {"vistos": 0, "nuevos": 0, "podados": 0}
    for asin, price in prices.items():
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        price = round(float(price), 2)
        stats["vistos"] += 1
        entry = products.get(asin)
        if not entry:
            products[asin] = [t, 1, t, price]
            stats["nuevos"] += 1
            continue
        if entry[0] != t:
            entry[0] = t
            entry[1] += 1
        if entry[-2] == t:
            if price < entry[-1]:  # el mismo dia solo cuenta el precio mas bajo
                entry[-1] = price
        elif price != entry[-1]:
            entry += [t, price]
    for asin in list(products):
        entry = products[asin]
        if entry[0] < cutoff:
            del products[asin]
            stats["podados"] += 1
            continue
        # quita parejas antiguas pero conserva la ultima anterior al corte: es el precio vigente al empezar
        pairs = [(entry[i], entry[i + 1]) for i in range(2, len(entry), 2)]
        while len(pairs) > 1 and pairs[1][0] <= cutoff:
            pairs.pop(0)
        entry[2:] = [x for pair in pairs for x in pair]
    return stats


def reference(entry: list | None, today: date) -> dict | None:
    """Minimo y mediana de los ultimos 30 dias segun lo que ha visto el bot, o None si aun no basta.

    Devuelve {'low30', 'median', 'seen', 'span', 'low_ago'}. 'low_ago' son los dias desde el ultimo
    dia en que se vio ese minimo.
    """
    if not _valid(entry):
        return None
    t = day_no(today)
    seen = entry[1]
    pairs = [(entry[i], entry[i + 1]) for i in range(2, len(entry), 2)]
    span = t - pairs[0][0]
    if seen < MIN_SEEN_DAYS or span < MIN_SPAN_DAYS:
        return None
    cutoff = t - WINDOW_DAYS
    # parejas que cuentan: las de la ventana y la vigente al empezar
    first = max((i for i, (d, _) in enumerate(pairs) if d <= cutoff), default=0)
    active = pairs[first:]
    low = min(p for _, p in active)
    # ultimo dia en que se vio ese minimo: el dia anterior al siguiente cambio (o hoy)
    low_day = max(
        (active[i + 1][0] - 1 if i + 1 < len(active) else t) for i, (_, p) in enumerate(active) if p == low
    )
    # mediana ponderada por los dias que duro cada precio (el ultimo llega hasta hoy incluido)
    weighted: list[tuple[float, int]] = []
    for i, (d, p) in enumerate(active):
        start = max(d, cutoff)
        end = active[i + 1][0] if i + 1 < len(active) else t + 1
        weighted.append((p, max(end - start, 0)))
    total = sum(w for _, w in weighted)
    if total <= 0:
        return None
    half, acc, median = total / 2, 0, low
    for p, w in sorted(weighted):
        acc += w
        if acc >= half:
            median = p
            break
    return {"low30": low, "median": median, "seen": seen, "span": span, "low_ago": max(t - low_day, 0)}
