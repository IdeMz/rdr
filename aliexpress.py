"""Recopilador de precios de AliExpress (API de afiliados). De momento solo en modo informe.

Una vez al dia pide a la API los productos mas vendidos de cada categoria del canal (con envio a España y en
euros), anota su precio en state/aliexpress_precios.json con el mismo formato que el feed de PowerPlanet y deja
los candidatos en state/aliexpress_candidates.json con las reglas de feedpicks.py. No publica nada.

El "antes" de AliExpress esta muy inflado y la API no da historial: la bajada se mide contra el historial propio.

Tambien guarda state/aliexpress_probe.json con los NOMBRES de los campos que devuelve la API y unos pocos
productos de muestra (datos publicos, nunca claves), para saber que se puede filtrar (origen del envio,
plazo de entrega...). Este modulo no conoce Telegram.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

import requests

import feed
import feedpicks

log = logging.getLogger("deals")

ENV_KEY = "ALIEXPRESS_APP_KEY"
ENV_SECRET = "ALIEXPRESS_APP_SECRET"
ENV_TRACKING = "ALIEXPRESS_TRACKING_ID"

ENDPOINT = "https://api-sg.aliexpress.com/sync"
HOT_METHOD = "aliexpress.affiliate.hotproduct.query"
SEARCH_METHOD = "aliexpress.affiliate.product.query"
PAGE_SIZE = 50
GLOBAL_PAGES = 3          # paginas de "hot products" sin categoria
MAX_CALLS = 45            # tope de llamadas a la API por dia
MAX_ERRORS = 3            # errores seguidos tras los cuales se abandona el dia
START_HOUR = 6            # hora de Madrid desde la que se hace la recogida diaria
MIN_PRICE = 5.0           # en AliExpress hay ofertas validas por debajo de los 25 EUR de PowerPlanet
HIST_FILE = "aliexpress_precios.json"
PICKS_FILE = "aliexpress_candidates.json"
PROBE_FILE = "aliexpress_probe.json"
SAMPLE = 3                # productos de muestra por consulta en el fichero de sondeo
SAMPLE_CHARS = 160


class ApiError(Exception):
    """La API contesto con un error (o con algo que no se entiende)."""


def sign(params: dict[str, str], secret: str) -> str:
    """Firma HMAC-SHA256 de la plataforma: parametros ordenados y pegados como clave+valor."""
    base = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest().upper()


def call(method: str, params: dict, key: str, secret: str, post=requests.post, now=time.time) -> dict:
    """Una llamada firmada. Devuelve el JSON de la respuesta o lanza ApiError."""
    body = {k: str(v) for k, v in params.items() if v not in (None, "")}
    body.update({"app_key": key, "timestamp": str(int(now() * 1000)), "sign_method": "sha256", "method": method})
    body["sign"] = sign(body, secret)
    try:
        resp = post(ENDPOINT, data=body, timeout=25)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise ApiError(f"{type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ApiError("respuesta inesperada")
    err = data.get("error_response")
    if isinstance(err, dict):
        raise ApiError(f"{err.get('code', '?')}: {str(err.get('msg') or err.get('sub_msg') or '')[:120]}")
    top = data.get(method.replace(".", "_") + "_response")
    if isinstance(top, dict):
        result = top.get("resp_result")
        if isinstance(result, dict) and str(result.get("resp_code", "200")) != "200":
            raise ApiError(f"resp_code {result.get('resp_code')}: {str(result.get('resp_msg', ''))[:120]}")
    return data


def find_products(data) -> list[dict]:
    """Lista de productos de una respuesta, vengan donde vengan (la estructura anidada varia entre metodos)."""
    if isinstance(data, dict):
        for key in ("product", "products"):
            value = data.get(key)
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
                return value
        for value in data.values():
            found = find_products(value)
            if found:
                return found
    elif isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return data
    return []


def _num(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[^\d.,]", "", value)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".") if cleaned.rfind(",") > cleaned.rfind(".") else cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def item_from(product: dict) -> dict | None:
    """Producto de la API en el formato de feedpicks (id, nombre, precio...), o None si no es utilizable."""
    pid = str(product.get("product_id") or "").strip()
    price = _num(product.get("target_sale_price"))
    if not pid or price is None or price <= 0:
        return None
    original = _num(product.get("target_original_price"))
    days = _num(product.get("ship_to_days"))
    return {
        "id": pid,
        "name": str(product.get("product_title") or "").strip(),
        "brand": "",
        "price": round(price, 2),
        "rrp": original if original and original > price else None,
        "link": str(product.get("promotion_link") or product.get("product_detail_url") or ""),
        "image": str(product.get("product_main_image_url") or ""),
        "ean": "",
        "category": str(product.get("second_level_category_name") or product.get("first_level_category_name") or ""),
        "stock": None,
        "valid_to": "",
        "foreign": False,
        "rating": _num(product.get("evaluate_rate")),          # AliExpress lo da en porcentaje (94.5%)
        "reviews": int(v) if (v := _num(product.get("lastest_volume"))) is not None else None,  # ventas
        "ship_days": int(days) if days is not None else None,
    }


def keywords_from_channel(path: Path) -> list[str]:
    """Palabras de busqueda de las categorias del canal (el parametro k= de sus direcciones)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    words: list[str] = []
    for cat in data.get("categories") or []:
        q = parse_qs(urlparse(str(cat.get("url", ""))).query).get("k")
        word = unquote_plus(q[0]).strip() if q else ""
        if word and word not in words:
            words.append(word)
    return words


def base_params(tracking: str) -> dict:
    return {
        "ship_to_country": "ES", "target_currency": "EUR", "target_language": "ES", "tracking_id": tracking,
        "page_size": PAGE_SIZE, "sort": "LAST_VOLUME_DESC",
    }


def gather(key: str, secret: str, tracking: str, keywords: list[str], post=requests.post, sleep=time.sleep,
           now=time.time) -> tuple[dict[str, dict], dict]:
    """Productos de hoy {id: item} y cifras para el log. Un error suelto no tumba el dia; varios seguidos si."""
    items: dict[str, dict] = {}
    stats = {"llamadas": 0, "errores": 0, "vacias": 0}
    plan = [{"page_no": n} for n in range(1, GLOBAL_PAGES + 1)] + [{"keywords": w, "page_no": 1} for w in keywords]
    streak = 0
    for extra in plan[:MAX_CALLS]:
        if stats["llamadas"]:
            sleep(1.0)
        stats["llamadas"] += 1
        try:
            data = call(HOT_METHOD, {**base_params(tracking), **extra}, key, secret, post=post, now=now)
            streak = 0
        except ApiError as exc:
            stats["errores"] += 1
            streak += 1
            log.warning("aliexpress: error en la consulta %s: %s", extra.get("keywords") or f"pagina {extra['page_no']}", exc)
            if streak >= MAX_ERRORS:
                log.warning("aliexpress: %d errores seguidos; se deja para otro dia", MAX_ERRORS)
                break
            continue
        products = find_products(data)
        if not products:
            stats["vacias"] += 1
        for product in products:
            item = item_from(product)
            if item:
                items.setdefault(item["id"], item)
    stats["productos"] = len(items)
    return items, stats


def probe(key: str, secret: str, tracking: str, keyword: str, post=requests.post, sleep=time.sleep,
          now=time.time) -> dict:
    """Prueba unas pocas variantes y apunta que campos devuelven (para saber que se puede filtrar)."""
    variants = {
        "hot_base": (HOT_METHOD, {}),
        "hot_entrega_5_dias": (HOT_METHOD, {"delivery_days": "5"}),
        "busqueda_base": (SEARCH_METHOD, {}),
    }
    out: dict = {}
    for name, (method, extra) in variants.items():
        if out:
            sleep(1.0)
        params = {**base_params(tracking), "keywords": keyword, "page_no": 1, **extra}
        entry: dict = {"method": method, "params": {k: v for k, v in params.items() if k != "tracking_id"}}
        try:
            products = find_products(call(method, params, key, secret, post=post, now=now))
            entry.update(ok=True, count=len(products))
            names = sorted({k for p in products for k in p})
            entry["fields"] = names
            entry["sample"] = [{k: str(v)[:SAMPLE_CHARS] for k, v in p.items() if k != "promotion_link"}
                               for p in products[:SAMPLE]]
        except ApiError as exc:
            entry.update(ok=False, error=str(exc))
        out[name] = entry
    return out


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def report(items: dict[str, dict], hist: dict, today: date) -> dict:
    """Candidatos del dia con las mismas reglas que PowerPlanet (otro suelo de precio)."""
    suspicious: list[dict] = []
    to_verify: list[dict] = []
    picks, stats = feedpicks.select(items, hist, today, suspicious, to_verify, min_price=MIN_PRICE)
    suspicious.sort(key=lambda c: c["price"], reverse=True)
    with_days = sum(1 for i in items.values() if i.get("ship_days") is not None)
    return {
        "day": today.isoformat(),
        "mode": "informe",
        "products": len(items),
        "with_ship_days": with_days,
        "stats": stats,
        "to_verify": to_verify[:feedpicks.SUSPICIOUS_LIMIT],
        "suspicious": suspicious[:feedpicks.SUSPICIOUS_LIMIT],
        "candidates": picks,
    }


def run(creds: tuple[str, str, str], state_dir: Path, channel_file: Path, today: date, hour: int,
        post=requests.post, sleep=time.sleep, now=time.time) -> str:
    """Recogida diaria. Devuelve una frase para el log. Nunca lanza por un fallo de la API."""
    key, secret, tracking = creds
    path = state_dir / HIST_FILE
    hist = feed.load_history(path)
    if hist.get("updated") == today.isoformat():
        return "aliexpress: precios de hoy ya recogidos"
    if hour < START_HOUR:
        return f"aliexpress: se recoge a partir de las {START_HOUR}h (Madrid)"
    keywords = keywords_from_channel(channel_file)
    items, stats = gather(key, secret, tracking, keywords, post=post, sleep=sleep, now=now)
    if not items:
        return f"aliexpress: sin productos ({stats['errores']} errores en {stats['llamadas']} llamadas); se reintenta"
    update = feed.update_history(hist, {pid: i["price"] for pid, i in items.items()}, today)
    feed.save_history(path, hist)
    sentence = (f"aliexpress: {stats['productos']} productos ({update['nuevos']} nuevos, {update['cambiaron']} con precio "
                f"distinto, {update['bajaron10']} bajaron un 10% o mas), {stats['llamadas']} llamadas, "
                f"{stats['errores']} errores")
    try:
        data = report(items, hist, today)
        feedpicks.save(state_dir / PICKS_FILE, data)
        sentence += f"; {len(data['candidates'])} candidatos en el informe"
    except Exception as exc:  # el informe es un extra
        log.warning("aliexpress: no se pudo calcular el informe (%s)", type(exc).__name__)
    try:
        write_json(state_dir / PROBE_FILE, {"day": today.isoformat(),
                                            "variants": probe(key, secret, tracking, keywords[0] if keywords else "auriculares",
                                                              post=post, sleep=sleep, now=now)})
    except Exception as exc:
        log.warning("aliexpress: no se pudo guardar el sondeo (%s)", type(exc).__name__)
    return sentence
