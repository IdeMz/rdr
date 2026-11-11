"""Historial de precios del feed de Awin (PowerPlanet).

Una vez al dia descarga el feed, anota el precio de cada producto y guarda solo los CAMBIOS de precio
de los ultimos 45 dias en state/feed_powerplanet.json. Con ese historial propio se podra distinguir una
bajada real de un descuento contra un PVPR inflado (el feed no trae historial de precios).

Este modulo no publica nada y no conoce Telegram: solo recopila. La URL del feed lleva la clave de Awin,
por eso nunca se escribe en los logs ni en los errores (las excepciones se resumen sin la URL).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import feedpicks

log = logging.getLogger("deals")

FEED_ENV = "AWIN_FEED_URL"
KEEP_DAYS = 45                 # cuanto historial se conserva
RETRY_MINUTES = 60             # espera tras una descarga fallida
MAX_UNZIPPED = 400 * 1024 * 1024  # tope al descomprimir (el feed real ronda los 18 MB)
MIN_PRODUCTS = 500             # un feed con menos productos se considera roto
MIN_RATIO = 0.5                # ... igual que uno que pierde mas de la mitad del dia anterior
START_HOUR = 5                 # hora de Madrid desde la que se intenta la descarga del dia
GIVE_UP_HOUR = 14              # pasada esta hora, un feed igual que el de ayer se da por bueno
EPOCH = date(2026, 1, 1)       # los dias se guardan como numero de dias desde esta fecha
DELIMITERS = (",", "\t", ";", "|")
sleep = time.sleep             # sustituible en tests


class FeedError(Exception):
    """Fallo al descargar o leer el feed. El mensaje nunca lleva la URL."""


def day_no(d: date) -> int:
    return (d - EPOCH).days


# --------------------------------------------------------------------------
# Descarga y lectura
# --------------------------------------------------------------------------


def download(url: str, attempts: int = 3) -> bytes:
    """Descarga el feed. Los errores se resumen sin URL porque lleva la clave de Awin."""
    last = "sin respuesta"
    for i in range(1, attempts + 1):
        try:
            resp = requests.get(url, timeout=(15, 180))
            if resp.status_code == 200 and resp.content:
                return resp.content
            last = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last = type(exc).__name__
        if i < attempts:
            sleep(5 * i)
    raise FeedError(f"no se pudo descargar el feed ({last})")


def describe(raw: bytes, header: str) -> str:
    """Resumen seguro de lo que llego, para el log: tamano, tipo aparente y el principio de la primera linea."""
    head = raw[:200].lstrip().lower()
    kind = ("HTML" if head.startswith((b"<!doctype html", b"<html")) else
            "XML" if head.startswith(b"<?xml") or head.startswith(b"<") else
            "JSON" if head.startswith((b"{", b"[")) else "texto")
    return f"{len(raw)} bytes, parece {kind}, empieza por {header[:150]!r}"


# columnas que se conservan de cada fila (el feed trae muchas mas y no hacen falta)
KEEP_COLUMNS = (
    "aw_product_id", "search_price", "condition", "in_stock", "product_name", "brand_name", "aw_deep_link",
    "rrp_price", "merchant_image_url", "aw_image_url", "large_image", "ean", "merchant_product_category_path",
    "category_name", "commission_group", "stock_quantity", "valid_to", "average_rating", "reviews",
)


def read_rows(raw: bytes) -> list[dict]:
    """Filas del feed (solo KEEP_COLUMNS). Acepta CSV normal, comprimido en zip o gzip."""
    if raw[:4] == b"PK\x03\x04":  # Awin entrega un .zip cuando la URL lleva /compression/zip/
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                members = [i for i in zf.infolist() if not i.is_dir()]
                members.sort(key=lambda i: not i.filename.lower().endswith((".csv", ".txt")))
                if not members or members[0].file_size > MAX_UNZIPPED:
                    raise FeedError("el zip del feed esta vacio o es demasiado grande")
                raw = zf.read(members[0])
        except zipfile.BadZipFile as exc:
            raise FeedError("el feed es un zip danado") from exc
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise FeedError("el feed esta comprimido pero no se puede descomprimir") from exc
    text = raw.decode("utf-8-sig", errors="replace")
    header = text.split("\n", 1)[0].rstrip("\r")
    delim = None
    for d in DELIMITERS:
        names = [c.strip().strip('"').strip("'") for c in next(csv.reader([header], delimiter=d), [])]
        if "aw_product_id" in names:
            delim = d
            break
    if delim is None:
        raise FeedError(f"el feed no tiene el formato esperado (falta aw_product_id): {describe(raw, header)}")
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delim, quotechar='"')
    reader.fieldnames = [(c or "").strip().strip('"') for c in (reader.fieldnames or [])]
    if "search_price" not in reader.fieldnames:
        raise FeedError(f"el feed no trae la columna search_price (columnas: {', '.join(reader.fieldnames)[:200]})")
    return [{k: row.get(k) or "" for k in KEEP_COLUMNS if k in reader.fieldnames} for row in reader]


def row_price(row: dict) -> float | None:
    try:
        return round(float((row.get("search_price") or "").replace(",", ".")), 2)
    except ValueError:
        return None


def row_usable(row: dict) -> bool:
    """Producto nuevo y en stock (los usados y reacondicionados no entran)."""
    if (row.get("condition") or "new").strip().lower() not in ("new", ""):
        return False
    return (row.get("in_stock") or "1").strip().lower() not in ("0", "false", "no")


def prices_from_rows(rows: list[dict]) -> dict[str, float]:
    """{id de producto: precio} de los productos nuevos y con precio."""
    prices: dict[str, float] = {}
    for row in rows:
        pid = (row.get("aw_product_id") or "").strip()
        price = row_price(row)
        if not pid or price is None or price <= 0 or not row_usable(row):
            continue
        prices[pid] = price
    return prices


def parse_feed(raw: bytes) -> dict[str, float]:
    """{id de producto: precio} de los productos nuevos y con precio. Acepta CSV normal o comprimido."""
    return prices_from_rows(read_rows(raw))


# --------------------------------------------------------------------------
# Historial
# --------------------------------------------------------------------------


def load_history(path: Path) -> dict:
    """Historial guardado, o uno vacio si no existe o esta danado."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"p": {}}
    if not isinstance(data, dict) or not isinstance(data.get("p"), dict):
        return {"p": {}}
    return data


def save_history(path: Path, hist: dict) -> None:
    """Un producto por linea y ordenado: asi git guarda solo lo que cambia cada dia."""
    head = {k: v for k, v in hist.items() if k != "p"}
    opening = json.dumps(head, ensure_ascii=False, separators=(",", ":"))[:-1]
    lines = [opening + (',"p":{' if head else '"p":{')]
    items = sorted(hist["p"].items())
    for i, (pid, entry) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f'{json.dumps(pid)}:{json.dumps(entry, separators=(",", ":"))}{comma}')
    lines.append("}}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def update_history(hist: dict, prices: dict[str, float], today: date) -> dict:
    """Anota los precios de hoy. Cada producto: [ultimo dia visto, dia, precio, dia, precio, ...].

    Solo se anade una pareja cuando el precio cambia. Devuelve estadisticas para el log.
    """
    today_no = day_no(today)
    cutoff = today_no - KEEP_DAYS
    products = hist.setdefault("p", {})
    stats = {"total": len(prices), "nuevos": 0, "cambiaron": 0, "bajaron10": 0, "podados": 0}
    for pid, price in prices.items():
        entry = products.get(pid)
        if not entry:
            products[pid] = [today_no, today_no, price]
            stats["nuevos"] += 1
            continue
        entry[0] = today_no
        last = entry[-1]
        if last != price:
            entry += [today_no, price]
            stats["cambiaron"] += 1
            if price <= last * 0.9:
                stats["bajaron10"] += 1
    for pid in list(products):
        entry = products[pid]
        if entry[0] < cutoff:  # lleva mas de KEEP_DAYS sin aparecer en el feed
            del products[pid]
            stats["podados"] += 1
            continue
        # quita parejas antiguas pero conserva la ultima anterior al corte: es el precio vigente al empezar
        pairs = [(entry[i], entry[i + 1]) for i in range(1, len(entry), 2)]
        while len(pairs) > 1 and pairs[1][0] <= cutoff:
            pairs.pop(0)
        entry[1:] = [x for pair in pairs for x in pair]
    hist.setdefault("first", today.isoformat())
    hist["updated"] = today.isoformat()
    hist["n"] = len(prices)
    hist["retry_at"] = None
    return stats


def history_days(hist: dict, today: date) -> int:
    """Dias que lleva recopilandose el historial."""
    try:
        return (today - date.fromisoformat(hist["first"])).days + 1
    except (KeyError, TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Punto de entrada: una vez al dia
# --------------------------------------------------------------------------


def digest(prices: dict[str, float]) -> str:
    """Huella del contenido: solo cambia si cambia algun precio o algun producto (no depende del zip)."""
    return hashlib.sha1(repr(sorted(prices.items())).encode()).hexdigest()


PICKS_FILE = "powerplanet_candidates.json"


def picks_summary(rows: list[dict], hist: dict, today: date, path: Path) -> str:
    """Calcula y guarda los candidatos de hoy (modo informe: no se publica nada). Nunca lanza excepciones."""
    try:
        data = feedpicks.build(rows, hist, today)
        feedpicks.save(path.with_name(PICKS_FILE), data)
        return f"; ofertas: {len(data['candidates'])} candidatos en el informe"
    except Exception as exc:  # es un extra: que falle no debe tumbar el historial
        log.warning("feed: no se pudieron calcular los candidatos (%s)", type(exc).__name__)
        return ""


def collect(url: str, path: Path, today: date, now: datetime | None = None, fetch=download,
            hour: int | None = None) -> str:
    """Actualiza el historial si hoy aun no se ha hecho. Devuelve una frase para el log.

    `hour` es la hora actual en Madrid. Awin regenera el feed a una hora que no conocemos, asi que el bot
    espera a START_HOUR y, mientras el contenido sea identico al del dia anterior, vuelve a mirar cada hora
    hasta GIVE_UP_HOUR; la hora del cambio queda en el log. Con hour=None no se espera (modo sin horario).

    Nunca lanza excepciones por culpa del feed: si falla, se reintenta pasada una hora.
    """
    now = now or datetime.now(timezone.utc)
    hist = load_history(path)
    if hist.get("updated") == today.isoformat():
        return "feed ya recopilado hoy"
    if hour is not None and hour < START_HOUR and hist.get("updated"):
        return f"feed: aun no es hora (desde las {START_HOUR}:00)"
    retry_at = hist.get("retry_at")
    if retry_at:
        try:
            if datetime.fromisoformat(retry_at) > now:
                return "feed: reintento aplazado"
        except ValueError:
            pass
    try:
        rows = read_rows(fetch(url))
        prices = prices_from_rows(rows)
        previous = hist.get("n") or 0
        if len(prices) < MIN_PRODUCTS or (previous and len(prices) < previous * MIN_RATIO):
            raise FeedError(f"el feed trae solo {len(prices)} productos (ayer {previous}); se ignora")
    except FeedError as exc:
        hist["retry_at"] = (now + timedelta(minutes=RETRY_MINUTES)).isoformat()
        save_history(path, hist)
        return f"feed: {exc}"
    fingerprint = digest(prices)
    same = hist.get("digest") == fingerprint
    if same and hour is not None and hour < GIVE_UP_HOUR:
        hist["retry_at"] = (now + timedelta(minutes=RETRY_MINUTES)).isoformat()
        save_history(path, hist)
        return f"feed: igual que el ultimo recopilado (hora {hour}h en Madrid); se vuelve a mirar en 1 h"
    stats = update_history(hist, prices, today)
    hist["digest"] = fingerprint
    save_history(path, hist)
    picks = picks_summary(rows, hist, today, path)
    when = "sin cambios, se da por bueno" if same else (f"cambio detectado a las {hour}h (Madrid)" if hour is not None else "")
    return (
        f"feed: {stats['total']} productos, {stats['nuevos']} nuevos, {stats['cambiaron']} con otro precio "
        f"({stats['bajaron10']} bajaron un 10 % o mas), {stats['podados']} podados; "
        f"{history_days(hist, today)} dias de historial" + (f"; {when}" if when else "") + picks
    )
