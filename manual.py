"""Enlaces de PowerPlanet y AliExpress que le mandas al bot por privado (los de Amazon los lleva deals.py).

Solo funciones sin red: deals.py descarga y llama a estas para entender lo descargado.

PowerPlanet: el enlace de la tienda se busca en el feed de Awin (de ahi salen el enlace de afiliado sin tocar,
el PVPR y la foto). Para encontrarlo se usa el titulo de la pagina (og:title) o, si no se pudo abrir, las
palabras de la direccion. El precio de la pagina, si se lee, manda sobre el del feed (es de hoy).
AliExpress: el numero de producto de la direccion se pide a la API de afiliados (productdetail.get).
"""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

import crossamz
import feed

PP_HOSTS = ("powerplanetonline.com",)
AE_HOSTS = ("aliexpress.com", "aliexpress.us", "aliexpress.es")
AE_SHORT_HOSTS = ("s.click.aliexpress.com", "a.aliexpress.com")
URL_RE = re.compile(r"https?://\S+", re.I)
AE_ID_RE = re.compile(r"/item/(?:[^/]*/)?(\d{8,20})\.html", re.I)
MIN_MATCH = 0.6          # parte de las palabras buscadas que tiene que tener el nombre del feed
DELIVERY_RE = re.compile(r"(Rec[ií]belo[^.\n]{0,60}?\d{1,2}\s+\w{3,10}\.?)", re.I)


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def detect(text: str) -> tuple[str, str]:
    """('powerplanet' | 'aliexpress' | '', url) segun el primer enlace del mensaje."""
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(").,;>\"'")
        host = host_of(url)
        if _is(host, PP_HOSTS):
            return "powerplanet", url
        if _is(host, AE_HOSTS):
            return "aliexpress", url
    return "", ""


# --------------------------------------------------------------------------
# PowerPlanet
# --------------------------------------------------------------------------


def slug_words(url: str) -> str:
    """'.../es/ssd-sandisk-extreme-portable-v2-2tb-azul' -> 'ssd sandisk extreme portable v2 2tb azul'."""
    path = unquote(urlparse(url).path).rstrip("/")
    last = path.rsplit("/", 1)[-1]
    last = re.sub(r"\.(html?|php)$", "", last)
    return last.replace("-", " ").replace("_", " ")


def _jsonld_offers(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if node.get("@type") in ("Product", ["Product"]) or "offers" in node:
                    yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


def _price_value(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", text):  # 266.95 (formato de datos, con punto)
            v = float(text)
        else:
            v = crossamz.parse_eur(text)
        return v if v and v > 0 else None
    return None


def parse_pp_page(html: str) -> dict:
    """{'title', 'price', 'image', 'delivery'} de la pagina de producto (solo las claves que se encuentran)."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}

    def meta(*names):
        for n in names:
            el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
                or soup.find("meta", attrs={"itemprop": n})
            if el and el.get("content"):
                return el["content"].strip()
        return None

    title = meta("og:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None
    if title:
        out["title"] = re.sub(r"\s*[|\-–]\s*Power\s*Planet.*$", "", title, flags=re.I).strip()
    image = meta("og:image")
    if image:
        out["image"] = image
    price = _price_value(meta("product:price:amount", "og:price:amount", "price"))
    if price is None:
        for node in _jsonld_offers(soup):
            offers = node.get("offers")
            for offer in offers if isinstance(offers, list) else [offers]:
                if isinstance(offer, dict):
                    price = _price_value(offer.get("price") or offer.get("lowPrice"))
                    if price:
                        break
            if price:
                break
    if price:
        out["price"] = price
    m = DELIVERY_RE.search(soup.get_text(" ", strip=True))
    if m:
        out["delivery"] = re.sub(r"\s+", " ", m.group(1)).strip()
    return out


def best_row(rows: list[dict], query: str) -> tuple[dict | None, float]:
    """Fila del feed cuyo nombre contiene mas palabras de la busqueda: (fila, parte de palabras encontradas)."""
    wanted = set(crossamz.tokens(query))
    if not wanted:
        return None, 0.0
    best, best_score, best_extra = None, 0.0, 0
    for row in rows:
        name = set(crossamz.tokens(row.get("product_name") or ""))
        if not name:
            continue
        hit = len(wanted & name)
        if not hit:
            continue
        score = hit / len(wanted)
        extra = len(name - wanted)  # a igualdad, el nombre con menos palabras de sobra (la variante justa)
        if score > best_score or (score == best_score and best is not None and extra < best_extra):
            best, best_score, best_extra = row, score, extra
    return best, best_score


def pp_history_line(entry: list | None, today: date) -> str:
    """Resumen de nuestro historial del feed: 'historial propio: minimo 249,95 € en 12 dias'."""
    if not entry or len(entry) < 3:
        return ""
    prices = [p for p in entry[2::2] if isinstance(p, (int, float))]
    days = [d for d in entry[1::2] if isinstance(d, int)]
    if not prices or not days:
        return ""
    span = feed.day_no(today) - min(days) + 1
    low = min(prices)
    return f"historial propio: mínimo {low:.2f} € en {span} días".replace(".", ",")


def pp_item(rows: list[dict], url: str, page: dict, hist_entry: list | None, today: date) -> tuple[dict | None, list[str]]:
    """(producto para la vista previa, lineas de aviso). Producto None si no se encuentra en el feed."""
    query = page.get("title") or slug_words(url)
    row, score = best_row(rows, query)
    if row is None or score < MIN_MATCH:
        return None, [f"No encuentro «{query[:80]}» en el feed de PowerPlanet."]
    feed_price = feed.row_price(row)
    price = page.get("price") or feed_price
    try:
        rrp = round(float((row.get("rrp_price") or "").replace(",", ".")), 2)
    except ValueError:
        rrp = None
    info = [f"🔎 En el feed: {row.get('product_name', '')[:90]} (coincide un {round(score * 100)} %)"]
    if page.get("price") and feed_price and abs(page["price"] - feed_price) >= 0.01:
        info.append(f"💶 Precio de la web {page['price']:.2f} € · en el feed {feed_price:.2f} €".replace(".", ","))
    elif not page:
        info.append("ℹ️ No se pudo abrir la página: precio del feed (puede ser de ayer).")
    hist = pp_history_line(hist_entry, today)
    if hist:
        info.append("📈 " + hist[0].upper() + hist[1:])
    if page.get("delivery"):
        info.append(f"🚚 {page['delivery']}")
    if row.get("foreign"):
        info.append("⚠️ La descripción avisa de que es una versión de otro mercado.")
    image = row.get("merchant_image_url") or row.get("large_image") or row.get("aw_image_url") or page.get("image")
    item = {
        "key": f"pp:{(row.get('aw_product_id') or '').strip()}",
        "title": (row.get("product_name") or page.get("title") or "").strip(),
        "price": price,
        "old": rrp if rrp and price and rrp > price else None,
        "image": image,
        "url": (row.get("aw_deep_link") or "").strip(),
        "store": "POWERPLANET",
        "rating": None,
        "reviews": None,
    }
    if not item["url"] or not price or item["key"] == "pp:":
        return None, [f"El producto del feed no trae enlace o precio: {item['title'][:80]}"]
    return item, info


# --------------------------------------------------------------------------
# AliExpress
# --------------------------------------------------------------------------


def ae_id(url: str) -> str | None:
    m = AE_ID_RE.search(urlparse(url).path)
    if m:
        return m.group(1)
    query = parse_qs(urlparse(url).query)
    for key in ("productIds", "productId", "itemId"):
        if query.get(key) and re.fullmatch(r"\d{8,20}", query[key][0]):
            return query[key][0]
    return None


def is_ae_short(url: str) -> bool:
    return _is(host_of(url), AE_SHORT_HOSTS)


def ae_item(api_item: dict, trusted: dict, blocked: dict) -> tuple[dict, list[str]]:
    """Producto de la API (formato aliexpress.item_from) para la vista previa, con sus avisos."""
    info = []
    shop_id, shop = api_item.get("shop_id") or "", api_item.get("shop") or "tienda sin nombre"
    if shop_id in blocked:
        info.append(f"🚫 Tienda bloqueada en la revisión: {shop}")
    elif shop_id in trusted:
        info.append(f"🏪 {shop} · tienda de confianza ✅")
    else:
        info.append(f"🏪 {shop} · tienda sin revisar")
    info.append("⚠️ Comprueba en la ficha que se envía desde Europa antes de publicar.")
    rate, sold = api_item.get("rating"), api_item.get("reviews")
    bits = []
    if rate:
        bits.append(f"{rate:g} % positivas".replace(".", ","))
    if sold:
        bits.append(f"{sold:,} vendidos".replace(",", "."))
    if bits:
        info.append("📦 " + " · ".join(bits))
    item = {
        "key": f"ae:{api_item['id']}",
        "title": api_item.get("name") or "",
        "price": api_item.get("price"),
        "old": api_item.get("rrp"),
        "image": api_item.get("image"),
        "url": api_item.get("link") or "",
        "store": "ALIEXPRESS",
        "rating": round(rate / 20, 1) if rate else None,  # 96 % positivas -> 4,8/5
        "reviews": None,
    }
    return item, info
