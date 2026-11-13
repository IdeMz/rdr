"""Cruce de los candidatos de PowerPlanet con Amazon.es (modo informe: no publica nada).

Para cada candidato (primero los `to_verify`, luego los demas por nota) se busca su EAN en Amazon, se confirma
que el resultado es el mismo producto (marca, modelo, que no sea un accesorio), se abre la ficha y se compara:

  amazon_mas_barato  Amazon lo tiene igual o mas barato: no es oferta de verdad
  oferta_real        PowerPlanet esta al menos un MIN_SAVING % por debajo del precio habitual en Amazon
  pvpr_inflado       la "bajada" de PowerPlanet es contra un precio anterior muy por encima del de Amazon
  dudoso             algo mas barato que Amazon, pero poco: para revision manual
  no_esta            no se encuentra en Amazon (por EAN ni por nombre)
  sin_precio         esta en Amazon pero sin precio o no disponible

El par EAN -> ASIN se guarda para siempre (un "no encontrado" se vuelve a buscar a los NOT_FOUND_DAYS).
Este modulo no hace peticiones: deals.py le pasa `search(texto) -> [Product]` y `page(asin) -> html`, con
la sesion que ya tiene fijada la entrega en España.
"""
from __future__ import annotations

import json
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

FILE = "cruce_amazon.json"
SOURCE = "powerplanet_candidates.json"
PER_TICK = 2             # productos cruzados por ejecucion (cada uno: 1-2 busquedas + 1 ficha)
MAX_REQUESTS = 5         # peticiones a Amazon por ejecucion, como mucho
RECHECK_DAYS = 2         # un cruce se repite pasado este tiempo (o antes, si cambia el precio de PowerPlanet)
SIN_PRECIO_HOURS = 6     # una ficha sin precio puede ser algo pasajero: se vuelve a mirar antes
NOT_FOUND_DAYS = 7       # un EAN que no se encontro se vuelve a buscar pasado este tiempo
KEEP_DAYS = 14           # cuanto se recuerda un cruce de un producto que ya no es candidato
NIGHT_PER_TICK = 3       # de noche (fuera del horario de publicacion) se cruzan mas por ejecucion...
NIGHT_MAX_REQUESTS = 8
NIGHT_EVERY_MIN = 15     # ...pero como mucho una vez cada 15 minutos
SAME_PRICE = 3           # % : Amazon hasta un 3 % mas caro cuenta como "mismo precio"
MIN_SAVING = 10          # % por debajo del precio habitual de Amazon para ser "oferta real"
INFLATED = 15            # % : precio anterior de PowerPlanet por encima del habitual de Amazon = PVPR inflado

VERDICTS = ("amazon_mas_barato", "oferta_real", "pvpr_inflado", "dudoso", "no_esta", "sin_precio")

ACCESSORY_WORDS = {
    "funda", "fundas", "carcasa", "protector", "cristal", "templado", "correa", "correas", "soporte", "cable",
    "adaptador", "repuesto", "recambio", "compatible", "pegatina", "skin", "estuche", "bolsa", "mando",
    "cargador", "base", "dock", "filtro", "filtros", "bateria", "almohadillas",
}
STOP = {
    "de", "del", "la", "el", "los", "las", "y", "con", "para", "en", "a", "por", "un", "una", "negro", "negra",
    "blanco", "blanca", "azul", "rojo", "verde", "gris", "plata", "oro", "rosa", "color",
}

PRICE_SELECTORS = (
    "#corePrice_feature_div .a-price .a-offscreen",
    "#corePrice_desktop .a-price .a-offscreen",
    ".priceToPay .a-offscreen",
    ".apexPriceToPay .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .aok-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "#price_inside_buybox",
)
CAPACITY_RE = re.compile(r"\d+(?:gb|tb)")
UNAVAILABLE_RE =re.compile(r"no disponible|actualmente no disponible|sin stock", re.I)


# --------------------------------------------------------------------------
# Comparacion de titulos
# --------------------------------------------------------------------------


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return text.lower()


UNIT_RE = re.compile(r"(\d)\s+(gb|tb|mb|mah|mm|cm|hz|kg|ml|l|w|v)\b")


def tokens(text: str) -> list[str]:
    """Palabras normalizadas. '7 L' y '7L' son lo mismo (Amazon y PowerPlanet escriben las unidades distinto)."""
    text = UNIT_RE.sub(r"\1\2", norm(text))
    return [t for t in re.findall(r"[a-z0-9]+", text) if t not in STOP and len(t) > 1]


def model_tokens(text: str) -> set[str]:
    """Palabras con letras y numeros (k65, vx2758a, l305): las que distinguen un modelo de otro.

    Solo cifras no cuentan (27, 2tb si): '27' pulgadas puede estar escrito de mil formas."""
    return {t for t in tokens(text) if re.search(r"\d", t) and re.search(r"[a-z]", t)}


SPEC_RE = re.compile(
    r"ipx?\d+k?|(?:usb|wifi|bt|g?ddr\d?|lpddr|cl|pcie)\d+[a-z]?|\d+x\d+(?:cm|mm)?"
    r"|\d+(?:p|w|hz|ghz|mhz|mah|mm|cm|gb|tb|mb|ms|v|l|kg|ml|k|mbs|mbps|th|a|pa|atm|dpi|nits|mp|db|hp|ppm|fps|rpm|km|kmh|g)?")


def strong_models(text: str, brand: str = "") -> set[str]:
    """Codigos de modelo de verdad (qh12a, vx2758a, xs2000): 4+ caracteres y no una especificacion (720p, 3w)."""
    return {t for t in model_tokens(text) if len(t) >= 4 and not SPEC_RE.fullmatch(t)} - set(tokens(brand))


def same_product(pp_name: str, brand: str, amz_title: str, strict: bool = False) -> bool:
    """¿El resultado de Amazon es el mismo producto que el de PowerPlanet?

    Siempre: la marca aparece en el titulo de Amazon y no es un accesorio que el original no menciona.
    Por EAN (strict=False): al menos un codigo de modelo en comun, o la mitad de las palabras si no hay codigos.
    Por nombre (strict=True): al menos la mitad de los codigos de modelo y la mitad de las palabras.
    En los dos casos, la capacidad (2tb, 128gb) tiene que coincidir."""
    amz = set(tokens(amz_title))
    if not amz:
        return False
    strong = strong_models(pp_name, brand) & amz  # el codigo de modelo vale tanto como la marca (Acer vende como AOPEN)
    brand_toks = tokens(brand)[:1]  # "Kingston Technology" -> "kingston"
    if brand_toks and brand_toks[0] not in amz and not strong:
        return False
    pp = set(tokens(pp_name))
    if (amz & ACCESSORY_WORDS) - pp:
        return False
    models = model_tokens(pp_name)
    if {t for t in models if CAPACITY_RE.fullmatch(t)} - amz:  # 2TB y 1TB son productos distintos
        return False
    overlap = len(pp & amz) / len(pp) if pp else 0.0
    if strict:
        if strong:
            return overlap >= 0.3
        return (not models or len(models & amz) * 2 >= len(models)) and overlap >= 0.5
    if models:
        return bool(models & amz) or overlap >= 0.6
    return overlap >= 0.5


def model_query(item: dict) -> str:
    """Busqueda por marca + codigo de modelo ('Acer QH12a'), o '' si el nombre no trae un codigo claro."""
    brand_name = item.get("brand") or ""
    codes = [w for w in re.findall(r"\S+", item.get("name") or "") if strong_models(w, brand_name)]
    if not codes:
        return ""
    brand = (item.get("brand") or "").split()[:1]
    return " ".join(brand + codes[:2])


def name_query(item: dict) -> str:
    """Busqueda por nombre: la marca y las primeras palabras con mas informacion."""
    words = [w for w in re.findall(r"\S+", item.get("name") or "") if norm(w) not in STOP]
    return " ".join(words[:7])


# --------------------------------------------------------------------------
# Ficha de Amazon
# --------------------------------------------------------------------------


def parse_eur(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


HIDDEN_PRICE_IDS = ("twister-plus-price-data-price", "attach-base-product-price", "priceValue")
PRICE_BOXES = ("#corePriceDisplay_desktop_feature_div", "#corePrice_feature_div", "#corePrice_desktop", "#apex_desktop")
NO_BUYBOX_RE = re.compile(r"ver todas las opciones de compra|no hay ofertas destacadas", re.I)


def _whole_fraction(box) -> float | None:
    """Precio partido en '.a-price-whole' + '.a-price-fraction' (cuando '.a-offscreen' viene vacio)."""
    whole = box.select_one(".a-price-whole")
    if whole is None:
        return None
    frac = box.select_one(".a-price-fraction")
    digits = re.sub(r"\D", "", whole.get_text())
    cents = re.sub(r"\D", "", frac.get_text()) if frac else "00"
    return parse_eur(f"{digits},{cents or '00'}") if digits else None


def page_price(html: str) -> tuple[float | None, bool]:
    """(precio actual, disponible) de la ficha."""
    soup = BeautifulSoup(html, "html.parser")
    avail = soup.select_one("#availability")
    available = not (avail and UNAVAILABLE_RE.search(avail.get_text(" ", strip=True)))
    for sel in PRICE_SELECTORS:
        el = soup.select_one(sel)
        price = parse_eur(el.get_text(strip=True)) if el else None
        if price:
            return price, available
    for box_sel in PRICE_BOXES:
        box = soup.select_one(box_sel)
        price = _whole_fraction(box) if box else None
        if price:
            return price, available
    for hid in HIDDEN_PRICE_IDS:  # campos ocultos del formulario de compra: el valor va con punto decimal
        el = soup.find(id=hid)
        try:
            price = float(el.get("value")) if el and el.get("value") else None
        except ValueError:
            price = None
        if price and price > 0:
            return price, available
    return None, False if avail is None else available


def page_hint(html: str) -> str:
    """Pista corta de por que una ficha no trae precio, para revisarlo en el fichero."""
    soup = BeautifulSoup(html, "html.parser")
    avail = soup.select_one("#availability")
    bits = [f"{len(html)} bytes", f"{len(soup.select('.a-price'))} a-price",
            "caja de precio" if any(soup.select_one(b) for b in PRICE_BOXES) else "sin caja de precio"]
    if avail:
        bits.append("disponibilidad: " + avail.get_text(" ", strip=True)[:60])
    if NO_BUYBOX_RE.search(soup.get_text(" ", strip=True)):
        bits.append("sin vendedor destacado")
    return "; ".join(bits)


# --------------------------------------------------------------------------
# Veredicto
# --------------------------------------------------------------------------


def verdict(pp_price: float, pp_before: float | None, amz_price: float | None, ref: dict) -> tuple[str, str, int | None]:
    """(veredicto, motivo, ahorro frente al precio habitual de Amazon en %)."""
    if not amz_price:
        return "sin_precio", "en Amazon pero sin precio o no disponible", None
    usual = ref.get("median") or max(amz_price, ref.get("low30") or 0)
    saving = round((1 - pp_price / usual) * 100)
    if amz_price <= pp_price * (1 + SAME_PRICE / 100):
        return "amazon_mas_barato", f"Amazon {amz_price:.2f} frente a {pp_price:.2f}", saving
    if saving >= MIN_SAVING:
        return "oferta_real", f"{saving} % por debajo del habitual en Amazon ({usual:.2f})", saving
    if pp_before and pp_before > usual * (1 + INFLATED / 100):
        return ("pvpr_inflado", f"antes {pp_before:.2f} en PowerPlanet, pero en Amazon suele estar a {usual:.2f}",
                saving)
    return "dudoso", f"solo {saving} % por debajo del habitual en Amazon ({usual:.2f})", saving


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------


def empty() -> dict:
    return {"ean_asin": {}, "items": {}}


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict):
        return empty()
    data.setdefault("ean_asin", {})
    data.setdefault("items", {})
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ts(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def load_candidates(path: Path) -> list[tuple[str, dict]]:
    """[(lista, item)] con EAN: primero los `to_verify`, luego los candidatos por nota."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[tuple[str, dict]] = []
    for key in ("to_verify", "candidates"):
        items = [i for i in data.get(key) or [] if isinstance(i, dict) and i.get("id") and i.get("ean")]
        if key == "candidates":
            items.sort(key=lambda i: -(i.get("score") or 0))
        out += [(key, i) for i in items]
    return out


def due(data: dict, item: dict, now: datetime) -> bool:
    old = data["items"].get(str(item["id"]))
    if not old:
        return True
    if old.get("pp_price") != item.get("price"):
        return True
    t = _ts(old.get("t"))
    wait = timedelta(hours=SIN_PRECIO_HOURS) if old.get("veredicto") == "sin_precio" else timedelta(days=RECHECK_DAYS)
    return t is None or now - t >= wait


def prune(data: dict, live: set[str], now: datetime) -> None:
    for pid in list(data["items"]):
        t = _ts(data["items"][pid].get("t"))
        if pid not in live and (t is None or now - t > timedelta(days=KEEP_DAYS)):
            del data["items"][pid]


def summary(data: dict, live: set[str]) -> dict:
    counts = {v: 0 for v in VERDICTS}
    for pid, entry in data["items"].items():
        if pid in live and entry.get("veredicto") in counts:
            counts[entry["veredicto"]] += 1
    return counts


# --------------------------------------------------------------------------
# Cruce
# --------------------------------------------------------------------------


class Budget(Exception):
    """Se acabaron las peticiones de esta ejecucion."""


def find_asin(data: dict, item: dict, search, now: datetime, spend) -> tuple[str, str, float | None]:
    """(asin, como se encontro, precio del resultado de busqueda). asin '' si no esta en Amazon."""
    ean = str(item["ean"])
    known = data["ean_asin"].get(ean)
    if known:
        t = _ts(known.get("t"))
        if known.get("a") or (t and now - t < timedelta(days=NOT_FOUND_DAYS)):
            return known.get("a", ""), known.get("via", "ean"), None
    name, brand = item.get("name") or "", item.get("brand") or ""
    queries = (("ean", ean, False), ("modelo", model_query(item), True), ("nombre", name_query(item), True))
    for via, query, strict in queries:
        if not query:
            continue
        spend()
        for p in search(query):
            if getattr(p, "sponsored", False):
                continue
            if same_product(name, brand, p.title, strict=strict):
                data["ean_asin"][ean] = {"a": p.asin, "t": now.isoformat(), "via": via, "title": p.title[:160]}
                return p.asin, via, p.price
    data["ean_asin"][ean] = {"a": "", "t": now.isoformat()}
    return "", "", None


def cross_one(data: dict, list_name: str, item: dict, search, page, reference, now: datetime, spend) -> dict:
    asin, via, search_price = find_asin(data, item, search, now, spend)
    pp_price = float(item["price"])
    pp_before = item.get("median") or item.get("prior_low")
    entry = {"t": now.isoformat(), "lista": list_name, "name": (item.get("name") or "")[:160], "ean": str(item["ean"]),
             "pp_price": item.get("price"), "pp_median": item.get("median"), "pp_drop": item.get("drop")}
    if not asin:
        entry.update(veredicto="no_esta", motivo="no se encuentra en Amazon por EAN, modelo ni nombre")
        return entry
    spend()
    html = page(asin)
    price, available = page_price(html)
    if price is None and available:
        price = search_price
    ref = reference(html) or {}
    if price is None:
        entry["pista"] = page_hint(html)
    entry.update(asin=asin, encontrado_por=via, amz_title=data["ean_asin"].get(str(item["ean"]), {}).get("title", ""),
                 amz_price=price if available else None, amz_low30=ref.get("low30"), amz_median=ref.get("median"),
                 disponible=available)
    v, why, saving = verdict(pp_price, pp_before, price if available else None, ref)
    entry.update(veredicto=v, motivo=why, ahorro_vs_amazon=saving)
    return entry


def night_due(state_dir: Path, now: datetime) -> bool:
    """¿Toca cruzar de noche? Si hay pendientes y han pasado NIGHT_EVERY_MIN desde la ultima vez."""
    data = load(state_dir / FILE)
    last = _ts(data.get("night_at"))
    if last and now - last < timedelta(minutes=NIGHT_EVERY_MIN):
        return False
    return any(due(data, i, now) for _, i in load_candidates(state_dir / SOURCE))


def run(state_dir: Path, search, page, reference, now: datetime, sleep=time.sleep, night: bool = False) -> str:
    """Cruza unos pocos candidatos pendientes. Devuelve una frase para el log. No lanza salvo por bugs."""
    path = state_dir / FILE
    data = load(path)
    cands = load_candidates(state_dir / SOURCE)
    live = {str(i["id"]) for _, i in cands}
    pending = [(k, i) for k, i in cands if due(data, i, now)]
    per_tick, max_requests = (NIGHT_PER_TICK, NIGHT_MAX_REQUESTS) if night else (PER_TICK, MAX_REQUESTS)
    if night:
        data["night_at"] = now.isoformat()
    used = 0

    def spend():
        nonlocal used
        if used >= max_requests:
            raise Budget()
        if used:
            sleep(random.uniform(2, 4))
        used += 1

    done, stop = 0, ""
    for list_name, item in pending[:per_tick]:
        try:
            data["items"][str(item["id"])] = cross_one(data, list_name, item, search, page, reference, now, spend)
            done += 1
        except Budget:
            break
        except Exception as exc:  # Amazon no responde o bloquea: se deja para la siguiente ejecucion
            stop = f"; parado por {type(exc).__name__}"
            break
    prune(data, live, now)
    data["updated"] = now.isoformat()
    data["resumen"] = summary(data, live)
    save(path, data)
    left = len(pending) - done
    counts = ", ".join(f"{k} {v}" for k, v in data["resumen"].items() if v)
    return f"cruce amazon{' (noche)' if night else ''}: {done} cruzados, {left} pendientes, {used} peticiones{stop}" + (f" ({counts})" if counts else "")
