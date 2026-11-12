"""Recopilador de precios de AliExpress (API de afiliados). De momento solo en modo informe.

Una vez al dia pide a la API los productos mas vendidos de cada categoria del canal (precios y plazos para
España, en euros), anota su precio en state/aliexpress_precios.json con el mismo formato que el feed de PowerPlanet y deja
los candidatos en state/aliexpress_candidates.json con las reglas de feedpicks.py. No publica nada.

Solo interesan los productos que se ENVIAN DESDE Europa (para evitar aduana y tasas), no los que solo se entregan
en España. La API NO dice el origen y su filtro delivery_days no cambia los resultados (comprobado el 12-11-2026:
mismos productos con 3, 5 dias y sin filtro). Por eso el origen queda sin verificar (from_europe = null) y hay que
mirarlo en la ficha antes de publicar. Si algun dia la API trae el origen y no es europeo, el producto se descarta.

El "antes" de AliExpress esta muy inflado y la API no da historial: la bajada se mide contra el historial propio.

Tambien guarda state/aliexpress_probe.json con los NOMBRES de los campos que devuelve la API y unos pocos
productos de muestra (datos publicos, nunca claves), para saber que se puede filtrar (origen del envio,
plazo de entrega...). Este modulo no conoce Telegram.
"""

from __future__ import annotations

import hashlib
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
DETAIL_METHOD = "aliexpress.affiliate.productdetail.get"
PAGE_SIZE = 50
GLOBAL_PAGES = 0          # las paginas generales de "hot products" dan casi solo productos de 2-4 EUR: no se piden
QUERY_METHOD = SEARCH_METHOD  # la busqueda normal da mas resultados que "hot products" (7 contra 1 con la misma palabra)
MAX_CALLS = 75            # tope de llamadas a la API por dia (consultas + reintentos con la primera palabra)
MAX_ERRORS = 3            # errores seguidos tras los cuales se abandona el dia
START_HOUR = 6            # hora de Madrid desde la que se hace la recogida diaria
MIN_PRICE = 10.0          # EUR; por debajo casi todo es accesorio de usar y tirar
MIN_PRICE_CENTS = "1000"  # min_sale_price de la API va en centimos (con 1000 desaparecieron los productos de 9 EUR)
MIN_SALES = 50            # ventas recientes (lastest_volume) minimas para proponer un producto
MIN_RATING = 90.0         # valoracion minima en % (evaluate_rate)
HIST_FILE = "aliexpress_precios.json"
PICKS_FILE = "aliexpress_candidates.json"
PROBE_FILE = "aliexpress_probe.json"
STATUS_FILE = "aliexpress_estado.json"   # por que no hubo productos (la API da errores): se lee sin necesidad del log
ONLY_FROM_EUROPE = True   # descarta del informe los productos cuyo origen conocido no es europeo
EUROPE_NAMES = {  # UE, EEE, Reino Unido y Suiza: codigos ISO y nombres habituales en ingles y español
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
    "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "NO", "IS", "LI", "CH", "GB", "UK",
    "SPAIN", "ESPANA", "ESPAÑA", "GERMANY", "ALEMANIA", "FRANCE", "FRANCIA", "ITALY", "ITALIA", "POLAND", "POLONIA",
    "PORTUGAL", "NETHERLANDS", "PAISES BAJOS", "BELGIUM", "BELGICA", "CZECH REPUBLIC", "CZECHIA", "IRELAND", "IRLANDA",
    "SWEDEN", "SUECIA", "UNITED KINGDOM", "REINO UNIDO", "SWITZERLAND", "SUIZA", "AUSTRIA", "DENMARK", "DINAMARCA",
    "HUNGARY", "HUNGRIA", "ROMANIA", "RUMANIA", "BULGARIA", "GREECE", "GRECIA", "SLOVAKIA", "ESLOVAQUIA",
}
ORIGIN_KEY_RE = re.compile(r"ship\w*from|from_?country|origin(?!al)|warehouse", re.IGNORECASE)  # no "original_price"
PROBE_VERSION = 7         # sube cuando cambian las variantes del sondeo: se repite aunque el dia ya este recogido
SAMPLE_PRICES = 15
SAMPLE = 3                # productos de muestra por consulta en el fichero de sondeo
SAMPLE_CHARS = 160


class ApiError(Exception):
    """La API contesto con un error (o con algo que no se entiende)."""


class EmptyResult(ApiError):
    """La consulta es valida pero no tiene resultados (resp_code 405): no es un fallo de la API."""


SYSTEM_PARAMS = ("format", "app_key", "sign_method", "v", "timestamp", "partner_id", "method")
PARTNER_ID = "taobao-sdk-python-20200924"  # la misma etiqueta que envia el SDK oficial de la plataforma


def sign(params: dict[str, str], secret: str) -> str:
    """Firma MD5 de la pasarela: secreto + (clave+valor de todos los parametros ordenados) + secreto, en mayusculas."""
    base = secret + "".join(f"{k}{params[k]}" for k in sorted(params)) + secret
    return hashlib.md5(base.encode("utf-8")).hexdigest().upper()


def call(method: str, params: dict, key: str, secret: str, post=requests.post, now=time.time) -> dict:
    """Una llamada firmada. Devuelve el JSON de la respuesta o lanza ApiError.

    Como el SDK oficial: los parametros de sistema van en la URL y los de la consulta en el cuerpo; la firma
    cubre todos."""
    body = {k: str(v) for k, v in params.items() if v not in (None, "")}
    system = {"format": "json", "app_key": key, "sign_method": "md5", "v": "2.0",
              "timestamp": str(int(now() * 1000)), "partner_id": PARTNER_ID, "method": method}
    system["sign"] = sign({**system, **body}, secret)
    try:
        resp = post(ENDPOINT, params=system, data=body, timeout=25)
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
        if isinstance(result, dict) and str(result.get("resp_code", "200")) == "405":
            raise EmptyResult(f"resp_code 405: {str(result.get('resp_msg', ''))[:120]}")
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


def origin_of(product: dict) -> str | None:
    """Pais desde el que se envia, si la API lo dice en algun campo (el nombre del campo no esta confirmado)."""
    for key, value in product.items():
        if ORIGIN_KEY_RE.search(str(key)) and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def item_from(product: dict) -> dict | None:
    """Producto de la API en el formato de feedpicks (id, nombre, precio...), o None si no es utilizable."""
    pid = str(product.get("product_id") or "").strip()
    price = _num(product.get("target_sale_price"))
    if not pid or price is None or price <= 0:
        return None
    original = _num(product.get("target_original_price"))
    days = _num(product.get("ship_to_days"))
    origin = origin_of(product)
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
        "ship_from": origin,
        "from_europe": (origin.upper() in EUROPE_NAMES) if origin else None,  # None = la API no lo dice
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
        "page_size": PAGE_SIZE, "sort": "LAST_VOLUME_DESC", "min_sale_price": MIN_PRICE_CENTS,
    }


def gather(key: str, secret: str, tracking: str, keywords: list[str], post=requests.post, sleep=time.sleep,
           now=time.time) -> tuple[dict[str, dict], dict]:
    """Productos de hoy {id: item} y cifras para el log. Un error suelto no tumba el dia; varios seguidos si.

    Una consulta sin resultados no es un error. Si una frase de varias palabras no da nada se repite con su primera
    palabra (AliExpress exige que salgan todas: "smartphone libre" da 0 y "smartphone" no). stats["consultas"]
    dice cuantos productos dio cada una y stats["campos"] / stats["muestra_productos"] son los campos y unos
    productos tal como los devuelve la API."""
    items: dict[str, dict] = {}
    stats: dict = {"llamadas": 0, "errores": 0, "vacias": 0, "muestra": [], "consultas": {}, "campos": [],
                   "muestra_productos": []}
    state = {"streak": 0}
    fields: set[str] = set()

    def query(extra: dict, label: str) -> int | None:
        """Productos que da la consulta (0 si esta vacia) o None si fallo."""
        if stats["llamadas"]:
            sleep(1.0)
        stats["llamadas"] += 1
        try:
            data = call(QUERY_METHOD, {**base_params(tracking), **extra}, key, secret, post=post, now=now)
            state["streak"] = 0
        except EmptyResult:
            stats["vacias"] += 1
            stats["consultas"][label] = 0
            state["streak"] = 0
            return 0
        except ApiError as exc:
            stats["errores"] += 1
            state["streak"] += 1
            if str(exc) not in stats["muestra"] and len(stats["muestra"]) < 3:
                stats["muestra"].append(str(exc))
            log.warning("aliexpress: error en la consulta %s: %s", label, exc)
            return None
        products = find_products(data)
        stats["consultas"][label] = len(products)
        if not products:
            stats["vacias"] += 1
        for product in products:
            fields.update(product)
            if len(stats["muestra_productos"]) < SAMPLE:
                stats["muestra_productos"].append({k: str(v)[:SAMPLE_CHARS] for k, v in product.items()
                                                   if k != "promotion_link"})
            item = item_from(product)
            if item:
                items.setdefault(item["id"], item)
        return len(products)

    plan = [({"page_no": n}, f"general p{n}") for n in range(1, GLOBAL_PAGES + 1)]
    plan += [({"keywords": w, "page_no": 1}, w) for w in keywords]
    empty_phrases: list[str] = []
    for extra, label in plan[:MAX_CALLS]:
        got = query(extra, label)
        if got == 0 and " " in label.strip():
            empty_phrases.append(label)
        if state["streak"] >= MAX_ERRORS:
            log.warning("aliexpress: %d errores seguidos; se deja para otro dia", MAX_ERRORS)
            break
    for phrase in empty_phrases:
        if state["streak"] >= MAX_ERRORS or stats["llamadas"] >= MAX_CALLS:
            break
        first = phrase.split()[0]
        if first not in stats["consultas"]:
            query({"keywords": first, "page_no": 1}, first)
    stats["campos"] = sorted(fields)
    stats["productos"] = len(items)
    return items, stats


def probe(key: str, secret: str, tracking: str, keyword: str, post=requests.post, sleep=time.sleep,
          now=time.time, empty_keyword: str = "") -> dict:
    """Compara variantes de la consulta para saber que filtros funcionan de verdad.

    `keyword` es una palabra con muchos resultados y `empty_keyword` una que dio 0 con los filtros de la
    recogida (puede que sin ellos si de resultados). Para cada variante se guardan los ids de los productos:
    "resumen" dice si el filtro de entrega cambia algo."""
    variants = {
        "busqueda_actual": (keyword, {}),                                         # lo que usa la recogida (sin filtro de entrega)
        "entrega_5_dias": (keyword, {"delivery_days": "5"}),
        "entrega_1_dia": (keyword, {"delivery_days": "1"}),                      # valor extremo: si no cambia nada, no filtra
        "sin_precio_minimo": (keyword, {"min_sale_price": ""}),
        "sin_ningun_filtro": (keyword, {"min_sale_price": ""}),
    }
    out: dict = {}
    for name, (word, extra) in variants.items():
        if out:
            sleep(1.0)
        params = {**base_params(tracking), "keywords": word, "page_no": 1, **extra}
        entry: dict = {"method": SEARCH_METHOD,
                       "params": {k: v for k, v in params.items() if k != "tracking_id" and v != ""}}
        try:
            products = find_products(call(SEARCH_METHOD, params, key, secret, post=post, now=now))
            entry.update(ok=True, count=len(products), ids=[str(p.get("product_id")) for p in products])
            entry["fields"] = sorted({k for p in products for k in p})
            entry["precios"] = [str(p.get("target_sale_price")) for p in products[:SAMPLE_PRICES]]
            entry["sample"] = [{k: str(v)[:SAMPLE_CHARS] for k, v in p.items() if k != "promotion_link"}
                               for p in products[:SAMPLE]]
        except ApiError as exc:
            entry.update(ok=False, error=str(exc))
        out[name] = entry

    def ids(name: str):
        return out[name].get("ids") if out[name].get("ok") else None
    base_ids = ids("busqueda_actual")
    out["resumen"] = {
        "entrega_5_dias_igual_que_sin_filtro": (ids("entrega_5_dias") == base_ids) if base_ids is not None else None,
        "entrega_1_dia_igual_que_sin_filtro": (ids("entrega_1_dia") == base_ids) if base_ids is not None else None,
        "productos_sin_filtro_de_entrega": out["busqueda_actual"].get("count"),
        "productos_entrega_1_dia": out["entrega_1_dia"].get("count"),
    }
    out["detalle_de_producto"] = product_detail(base_ids or [], key, secret, tracking, post, now)
    return out


def product_detail(ids: list[str], key: str, secret: str, tracking: str, post, now) -> dict:
    """Pide el detalle de unos productos: puede traer campos que la busqueda no trae (origen, plazo real)."""
    params = {"product_ids": ",".join(ids[:5]), "country": "ES", "target_currency": "EUR", "target_language": "ES",
              "tracking_id": tracking}
    entry: dict = {"method": DETAIL_METHOD}
    if not ids:
        return {**entry, "ok": False, "error": "sin productos para pedir el detalle"}
    try:
        products = find_products(call(DETAIL_METHOD, params, key, secret, post=post, now=now))
        entry.update(ok=True, count=len(products), fields=sorted({k for p in products for k in p}),
                     sample=[{k: str(v)[:SAMPLE_CHARS] for k, v in p.items() if k != "promotion_link"}
                             for p in products[:SAMPLE]])
    except ApiError as exc:
        entry.update(ok=False, error=str(exc))
    return entry


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def report(items: dict[str, dict], hist: dict, today: date) -> dict:
    """Candidatos del dia con las mismas reglas que PowerPlanet (otro suelo de precio)."""
    suspicious: list[dict] = []
    to_verify: list[dict] = []
    foreign = {pid for pid, i in items.items() if ONLY_FROM_EUROPE and i.get("from_europe") is False}
    few_sales = {pid for pid, i in items.items() if (i.get("reviews") or 0) < MIN_SALES}
    low_rating = {pid for pid, i in items.items() if pid not in few_sales and (i.get("rating") or 0) < MIN_RATING}
    usable = {pid: i for pid, i in items.items() if pid not in foreign | few_sales | low_rating}
    picks, stats = feedpicks.select(usable, hist, today, suspicious, to_verify, min_price=MIN_PRICE)
    for label, group in (("no envia desde Europa", foreign), ("pocas ventas", few_sales), ("valoracion baja", low_rating)):
        if group:
            stats[label] = len(group)
    suspicious.sort(key=lambda c: c["price"], reverse=True)
    with_days = sum(1 for i in items.values() if i.get("ship_days") is not None)
    return {
        "day": today.isoformat(),
        "mode": "informe",
        "products": len(items),
        "with_ship_days": with_days,
        "origin_known": sum(1 for i in items.values() if i.get("from_europe") is not None),
        "from_europe": sum(1 for i in items.values() if i.get("from_europe")),
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
    if hour < START_HOUR:
        return f"aliexpress: se recoge a partir de las {START_HOUR}h (Madrid)"
    keywords = keywords_from_channel(channel_file)
    if hist.get("updated") == today.isoformat():
        if not probe_is_current(state_dir):  # el sondeo de hoy es de una version anterior: se repite una vez
            items, stats = gather(key, secret, tracking, keywords, post=post, sleep=sleep, now=now)
            write_status(state_dir, today, stats)
            fresh = {pid: i["price"] for pid, i in items.items() if pid not in hist["p"]}
            if fresh:  # productos que esta consulta ve por primera vez: solo se anaden, los ya anotados no se tocan
                total = hist.get("n", 0) + len(fresh)
                feed.update_history(hist, fresh, today)
                hist["n"] = total
                feed.save_history(path, hist)
            save_probe(creds, state_dir, probe_words(keywords, stats["consultas"]), today, post, sleep, now)
            return "aliexpress: precios de hoy ya recogidos; sondeo actualizado"
        return "aliexpress: precios de hoy ya recogidos"
    items, stats = gather(key, secret, tracking, keywords, post=post, sleep=sleep, now=now)
    if not items:
        write_status(state_dir, today, stats)
        return f"aliexpress: sin productos ({stats['errores']} errores en {stats['llamadas']} llamadas); se reintenta"
    write_status(state_dir, today, stats)
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
    save_probe(creds, state_dir, probe_words(keywords, stats["consultas"]), today, post, sleep, now)
    return sentence


def probe_words(keywords: list[str], consultas: dict) -> tuple[str, str]:
    """(palabra con mas resultados, primera palabra sin resultados) segun la recogida de hoy."""
    best = max(keywords, key=lambda w: consultas.get(w) or 0, default="")
    empty = next((w for w in keywords if consultas.get(w) == 0), "")
    return best, empty


def save_probe(creds: tuple[str, str, str], state_dir: Path, words: tuple[str, str], today: date, post, sleep, now) -> None:
    key, secret, tracking = creds
    try:
        variants = probe(key, secret, tracking, words[0], post=post, sleep=sleep, now=now, empty_keyword=words[1])
        write_json(state_dir / PROBE_FILE, {"version": PROBE_VERSION, "day": today.isoformat(), "variants": variants})
    except Exception as exc:
        log.warning("aliexpress: no se pudo guardar el sondeo (%s)", type(exc).__name__)


def write_status(state_dir: Path, today: date, stats: dict) -> None:
    """Deja el motivo del fallo en un fichero. Solo se reescribe si cambia, para no llenar el historial de commits."""
    data = {"day": today.isoformat(), "llamadas": stats.get("llamadas"), "errores": stats.get("errores"),
            "vacias": stats.get("vacias"), "muestra_de_errores": stats.get("muestra", []),
            "consultas": stats.get("consultas", {}), "campos": stats.get("campos", []),
            "muestra_productos": stats.get("muestra_productos", [])}
    path = state_dir / STATUS_FILE
    try:
        if json.loads(path.read_text(encoding="utf-8")) == data:
            return
    except (OSError, ValueError):
        pass
    try:
        write_json(path, data)
    except OSError as exc:
        log.warning("aliexpress: no se pudo guardar el estado (%s)", type(exc).__name__)


def probe_is_current(state_dir: Path) -> bool:
    try:
        return json.loads((state_dir / PROBE_FILE).read_text(encoding="utf-8")).get("version") == PROBE_VERSION
    except (OSError, ValueError):
        return False
