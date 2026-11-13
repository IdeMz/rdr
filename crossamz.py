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
NOT_FOUND_DAYS = 7       # un EAN que no se encontro se vuelve a buscar pasado este tiempo
KEEP_DAYS = 14           # cuanto se recuerda un cruce de un producto que ya no es candidato
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


def same_product(pp_name: str, brand: str, amz_title: str, strict: bool = False) -> bool:
    """¿El resultado de Amazon es el mismo producto que el de PowerPlanet?

    Siempre: la marca aparece en el titulo de Amazon y no es un accesorio que el original no menciona.
    Por EAN (strict=False): al menos un codigo de modelo en comun, o la mitad de las palabras si no hay codigos.
    Por nombre (strict=True): al menos la mitad de los codigos de modelo y la mitad de las palabras.
    En los dos casos, la capacidad (2tb, 128gb) tiene que coincidir."""
    amz = set(tokens(amz_title))
    if not amz:
        return False
    brand_toks = tokens(brand)[:1]  # "Kingston Technology" -> "kingston"
    if brand_toks and brand_toks[0] not in amz:
        return False
    pp = set(tokens(pp_name))
    if (amz & ACCESSORY_WORDS) - pp:
        return False
    models = model_tokens(pp_name)
    if {t for t in models if CAPACITY_RE.fullmatch(t)} - amz:  # 2TB y 1TB son productos distintos
        return False
    overlap = len(pp & amz) / len(pp) if pp else 0.0
    if strict:
        return (not models or len(models & amz) * 2 >= len(models)) and overlap >= 0.5
    if models:
        return bool(models & amz) or overlap >= 0.6
    return overlap >= 0.5


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
    return None, False if avail is None else available


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
    return t is None or now - t >= timedelta(days=RECHECK_DAYS)


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
    for via, query, strict in (("ean", ean, False), ("nombre", name_query(item), True)):
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
        entry.update(veredicto="no_esta", motivo="no se encuentra en Amazon por EAN ni por nombre")
        return entry
    spend()
    html = page(asin)
    price, available = page_price(html)
    if price is None and available:
        price = search_price
    ref = reference(html) or {}
    entry.update(asin=asin, encontrado_por=via, amz_title=data["ean_asin"].get(str(item["ean"]), {}).get("title", ""),
                 amz_price=price if available else None, amz_low30=ref.get("low30"), amz_median=ref.get("median"),
                 disponible=available)
    v, why, saving = verdict(pp_price, pp_before, price if available else None, ref)
    entry.update(veredicto=v, motivo=why, ahorro_vs_amazon=saving)
    return entry


def run(state_dir: Path, search, page, reference, now: datetime, sleep=time.sleep) -> str:
    """Cruza unos pocos candidatos pendientes. Devuelve una frase para el log. No lanza salvo por bugs."""
    path = state_dir / FILE
    data = load(path)
    cands = load_candidates(state_dir / SOURCE)
    live = {str(i["id"]) for _, i in cands}
    pending = [(k, i) for k, i in cands if due(data, i, now)]
    used = 0

    def spend():
        nonlocal used
        if used >= MAX_REQUESTS:
            raise Budget()
        if used:
            sleep(random.uniform(2, 4))
        used += 1

    done, stop = 0, ""
    for list_name, item in pending[:PER_TICK]:
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
    return f"cruce amazon: {done} cruzados, {left} pendientes, {used} peticiones{stop}" + (f" ({counts})" if counts else "")
