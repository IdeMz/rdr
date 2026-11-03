#!/usr/bin/env python3
"""Publica ofertas de Amazon.es en canales de Telegram.

Cada canal es un JSON en channels/. El estado anti-repeticion vive en state/.

    python deals.py --all
    python deals.py --channel NOMBRE [--dry-run]
    python deals.py --channel NOMBRE --html pagina.html     # prueba sin red
    python deals.py --all --force                            # ignora horario e intervalo
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
BASE = "https://www.amazon.es"
TELEGRAM_API = "https://api.telegram.org"
PHOTO_SIZE = 1200  # lado del cuadrado: con fotos siempre cuadradas, todos los posts miden igual de ancho
PHOTO_MARGIN = 40  # aire alrededor del producto, en pixeles
MAX_IMAGE_BYTES = 12 * 1024 * 1024
IMAGE_HOST_SUFFIXES = (".media-amazon.com", ".ssl-images-amazon.com")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

BLOCK_MARKERS = (
    "Introduce los caracteres que ves",
    "validateCaptcha",
    "api-services-support@amazon",
    "Enter the characters you see",
    "To discuss automated access",
)

log = logging.getLogger("deals")
sleep = time.sleep  # sustituible en tests


# --------------------------------------------------------------------------
# Secretos: nunca deben acabar en logs ni en mensajes de error
# --------------------------------------------------------------------------

TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _secrets.add(value)


def scrub(text: str) -> str:
    """Tapa cualquier token de Telegram y cualquier secreto registrado."""
    text = TOKEN_RE.sub("***", text)
    for secret in _secrets:
        text = text.replace(secret, "***")
    return text


class _ScrubFormatter(logging.Formatter):
    """Tapa secretos en el texto final, incluidas excepciones, sin tocar el registro original."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ScrubFormatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_secret(name: str) -> str | None:
    """Lee un secreto del entorno o del JSON ALL_SECRETS (workflow de Actions)."""
    value = os.environ.get(name)
    if not value:
        raw = os.environ.get("ALL_SECRETS")
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict):
                value = data.get(name)
    if value:
        register_secret(value)
    return value or None


def load_dotenv(path: Path | None = None) -> None:
    """Carga un .env local (solo para pruebas en tu ordenador)."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------
# Configuracion de canal
# --------------------------------------------------------------------------


class ConfigError(Exception):
    pass


@dataclass
class Channel:
    name: str
    token_env: str
    chat_env: str
    queries: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    pages: int = 1
    search_params: str = ""
    min_discount: int = 20
    max_discount: int = 0  # 0 = sin tope; util contra precios recomendados inflados
    min_listing_days: int = 60  # dias minimos desde el alta del producto en Amazon.es (0 = no comprobar)
    min_history_drop: int = 10  # % minimo por debajo del precio mas alto reciente de la ficha (0 = no comprobar)
    min_price: float = 0
    max_price: float = 0  # 0 = sin tope
    min_rating: float = 4.0
    min_reviews: int = 50
    max_posts: int = 1
    repeat_days: int = 14
    similarity: float = 0.5
    skip_sponsored: bool = True
    footer: str = ""
    enabled: bool = True
    # Categorias en rotacion: cada publicacion mira primero la que lleva mas tiempo sin visitarse.
    # Cada una: {"nombre", "url" o "query", y opcionalmente min_discount, max_discount, min_price,
    # max_price, min_rating, min_reviews, pages, search_params}. "emoji" se admite y se ignora.
    categories: list = field(default_factory=list)
    searches_per_run: int = 3  # categorias que se prueban como maximo en una ejecucion
    # Ubicacion de entrega: sin ella Amazon enseña precios sin IVA a los servidores de fuera de España
    postal_code: str = "28001"
    require_spain: bool = True
    # Horario y ritmo de publicacion
    timezone: str = "Europe/Madrid"
    window_start: str = "08:00"
    window_end: str = "23:00"
    interval_min: int = 30
    interval_max: int = 35
    retry_minutes: int = 10  # espera tras un intento sin candidatos o con bloqueo
    # Avisos privados por Telegram cuando algo falla varias veces seguidas
    alert_chat_env: str = "TG_ALERT_CHAT"  # secreto con tu ID de chat (si no existe, no se avisa)
    alert_after: int = 3  # fallos seguidos (bloqueo, ubicacion, Telegram, error) antes de avisar
    alert_after_empty: int = 6  # intentos seguidos sin ningun producto que publicar
    alert_repeat_min: int = 180  # minutos entre avisos mientras siga el problema


def load_channel(path: Path) -> Channel:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path.name}: no se puede leer ({type(exc).__name__})") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path.name}: debe ser un objeto JSON")
    known = {f.name for f in fields(Channel)}
    unknown = set(cfg) - known
    if unknown:
        raise ConfigError(f"{path.name}: claves desconocidas: {', '.join(sorted(unknown))}")
    missing = [k for k in ("name", "token_env", "chat_env") if not cfg.get(k)]
    if missing:
        raise ConfigError(f"{path.name}: faltan claves: {', '.join(missing)}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(cfg["name"])):
        raise ConfigError(f"{path.name}: 'name' solo admite letras, numeros, _ y -")
    ch = Channel(**cfg)
    if not ch.queries and not ch.urls and not ch.categories:
        raise ConfigError(f"{path.name}: hace falta 'queries', 'urls' o 'categories'")
    validate_categories(ch, path.name)
    if min(ch.alert_after, ch.alert_after_empty, ch.alert_repeat_min) < 1:
        raise ConfigError(f"{path.name}: los valores 'alert_*' deben ser al menos 1")
    if ch.searches_per_run < 1:
        raise ConfigError(f"{path.name}: 'searches_per_run' debe ser al menos 1")
    for url in ch.urls:
        if not url.startswith(BASE + "/"):
            raise ConfigError(f"{path.name}: las urls deben empezar por {BASE}/")
    if ch.search_params and not ch.search_params.startswith("&"):
        raise ConfigError(f"{path.name}: 'search_params' debe empezar por &")
    if not re.fullmatch(r"\d{5}", str(ch.postal_code)):
        raise ConfigError(f"{path.name}: 'postal_code' debe tener 5 cifras")
    try:
        ZoneInfo(ch.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{path.name}: zona horaria desconocida: {ch.timezone}") from None
    try:
        start, end = hhmm(ch.window_start), hhmm(ch.window_end)
    except ValueError:
        raise ConfigError(f"{path.name}: 'window_start' y 'window_end' deben ser HH:MM") from None
    if start >= end:
        raise ConfigError(f"{path.name}: 'window_start' debe ser anterior a 'window_end'")
    if not 0 < ch.interval_min <= ch.interval_max:
        raise ConfigError(f"{path.name}: intervalo no valido (interval_min <= interval_max, mayores que 0)")
    if ch.retry_minutes < 1:
        raise ConfigError(f"{path.name}: 'retry_minutes' debe ser al menos 1")
    if ch.max_discount and ch.max_discount <= ch.min_discount:
        raise ConfigError(f"{path.name}: 'max_discount' debe ser mayor que 'min_discount' (o 0)")
    if not 0 <= ch.min_history_drop <= 90:
        raise ConfigError(f"{path.name}: 'min_history_drop' debe estar entre 0 y 90")
    if ch.min_listing_days < 0:
        raise ConfigError(f"{path.name}: 'min_listing_days' no puede ser negativo")
    return ch


CATEGORY_OVERRIDES = (
    "min_discount", "max_discount", "min_price", "max_price", "min_rating", "min_reviews", "pages",
    "search_params",
)
CATEGORY_KEYS = {"nombre", "emoji", "url", "query", *CATEGORY_OVERRIDES}


def category_target(cat: dict) -> dict:
    """Parte de la configuracion del canal que cambia para esta categoria."""
    if cat.get("url"):
        url = cat["url"]
        return {"queries": [], "urls": [url if url.startswith("http") else BASE + url]}
    return {"queries": [cat["query"]], "urls": []}


def category_channel(ch: Channel, cat: dict) -> Channel:
    """El canal con los ajustes propios de la categoria (descuentos, precios, paginas...)."""
    over = {k: cat[k] for k in CATEGORY_OVERRIDES if k in cat}
    return replace(ch, categories=[], **category_target(cat), **over)


def validate_categories(ch: Channel, fname: str) -> None:
    if not isinstance(ch.categories, list):
        raise ConfigError(f"{fname}: 'categories' debe ser una lista")
    seen = set()
    for cat in ch.categories:
        if not isinstance(cat, dict) or not cat.get("nombre"):
            raise ConfigError(f"{fname}: cada categoria necesita 'nombre'")
        name = cat["nombre"]
        if name in seen:
            raise ConfigError(f"{fname}: categoria repetida: {name}")
        seen.add(name)
        extra = set(cat) - CATEGORY_KEYS
        if extra:
            raise ConfigError(f"{fname}: categoria {name}: claves desconocidas: {', '.join(sorted(extra))}")
        if bool(cat.get("url")) == bool(cat.get("query")):
            raise ConfigError(f"{fname}: categoria {name}: indica 'url' o 'query' (solo una)")
        url = cat.get("url")
        if url and not (url.startswith("/") or url.startswith(BASE + "/")):
            raise ConfigError(f"{fname}: categoria {name}: la url debe empezar por / o {BASE}/")
        try:
            cch = category_channel(ch, cat)
        except TypeError:
            raise ConfigError(f"{fname}: categoria {name}: ajustes no validos") from None
        if cch.max_discount and cch.max_discount <= cch.min_discount:
            raise ConfigError(f"{fname}: categoria {name}: 'max_discount' debe ser mayor que 'min_discount'")


def category_order(ch: Channel, visited: dict) -> list[dict]:
    """Las que nunca se han visitado primero (en el orden del archivo) y luego la visita mas antigua."""
    return sorted(ch.categories, key=lambda c: visited.get(c["nombre"], ""))


def hhmm(value: str) -> int:
    """'08:30' -> minutos desde medianoche."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(value)
    return int(m.group(1)) * 60 + int(m.group(2))


def in_window(now: datetime, ch: Channel) -> bool:
    local = now.astimezone(ZoneInfo(ch.timezone))
    minutes = local.hour * 60 + local.minute
    return hhmm(ch.window_start) <= minutes <= hhmm(ch.window_end)


# --------------------------------------------------------------------------
# Parseo de Amazon
# --------------------------------------------------------------------------


@dataclass
class Product:
    asin: str
    title: str
    price: float | None
    old_price: float | None = None
    rating: float | None = None
    reviews: int | None = None
    image: str | None = None
    sponsored: bool = False

    @property
    def discount(self) -> int:
        if self.price and self.old_price and self.old_price > self.price:
            return round((1 - self.price / self.old_price) * 100)
        return 0


def parse_price(text: str | None) -> float | None:
    """'1.299,99 €' -> 1299.99"""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_rating(text: str | None) -> float | None:
    """'4,5 de 5 estrellas' -> 4.5"""
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:de|/)\s*5", text)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def parse_count(text: str | None) -> int | None:
    """'1.234' -> 1234 ; '(2,5 mil)' -> 2500"""
    if not text:
        return None
    m = re.search(r"(\d[\d.,]*)\s*(mil|k|m)?\b", text, re.I)
    if not m:
        return None
    number, suffix = m.group(1), (m.group(2) or "").lower()
    if suffix in ("mil", "k", "m"):
        try:
            value = float(number.replace(".", "").replace(",", "."))
        except ValueError:
            return None
        return int(round(value * (1_000_000 if suffix == "m" else 1000)))
    digits = re.sub(r"[.,]", "", number)
    return int(digits) if digits else None


def _text(el) -> str:
    if el is None:
        return ""
    return el.get_text(" ", strip=True) or el.get("aria-label", "") or ""


def parse_results(html: str) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[Product] = []
    for card in soup.select('div[data-component-type="s-search-result"][data-asin]'):
        asin = (card.get("data-asin") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        h2 = card.select_one("h2")
        title = ""
        if h2 is not None:
            title = (h2.get("aria-label") or h2.get_text(" ", strip=True) or "").strip()
        price = parse_price(_text(card.select_one("span.a-price:not(.a-text-price) span.a-offscreen")))
        old_price = parse_price(_text(card.select_one("span.a-price.a-text-price span.a-offscreen")))

        rating_el = card.select_one("span.a-icon-alt") or card.select_one('[aria-label*="de 5 estrellas"]')
        rating = parse_rating(_text(rating_el))

        reviews = None
        for selector in (
            "span.s-underline-text",
            'a[href*="customerReviews"] span',
            'span[aria-label*="valoraciones"]',
        ):
            reviews = parse_count(_text(card.select_one(selector)))
            if reviews is not None:
                break

        img = card.select_one("img.s-image")
        sponsored = card.select_one(
            ".puis-sponsored-label-text, .s-sponsored-label-text, [aria-label*='Patrocinado']"
        ) is not None

        products.append(
            Product(
                asin=asin,
                title=title,
                price=price,
                old_price=old_price,
                rating=rating,
                reviews=reviews,
                image=img.get("src") if img else None,
                sponsored=sponsored,
            )
        )
    return products


def is_blocked(html: str, n_results: int) -> bool:
    return n_results == 0 and any(marker in html for marker in BLOCK_MARKERS)


# --------------------------------------------------------------------------
# Red
# --------------------------------------------------------------------------


class Blocked(Exception):
    pass


class LocationError(Exception):
    """No se ha podido fijar la entrega en España: es mejor no publicar que publicar precios sin IVA."""


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9",
            "Referer": BASE + "/",
        }
    )
    return s


def fetch(session: requests.Session, url: str, retries: int = 3) -> str:
    last = "sin respuesta"
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=20)
        except requests.RequestException as exc:
            last = f"error de red ({type(exc).__name__})"
        else:
            if r.status_code == 200:
                return follow_refresh(session, r.text, url)
            last = f"HTTP {r.status_code}"
        log.warning("Amazon: %s, intento %d/%d", last, attempt, retries)
        if attempt < retries:
            sleep(random.uniform(3, 7) * attempt)
    raise Blocked(last)


REFRESH_RE = re.compile(r"""http-equiv=["']refresh["']\s+content="(\d+);\s*URL='?([^"']+)""", re.I)


def follow_refresh(session: requests.Session, text: str, referer: str, hops: int = 3) -> str:
    """Amazon a veces responde con una pagina corta que redirige a los pocos segundos."""
    for _ in range(hops):
        m = REFRESH_RE.search(text)
        if not m or len(text) > 20000:
            break
        sleep(int(m.group(1)) + 1)
        try:
            r = session.get(
                urljoin(BASE + "/", unescape(m.group(2))), headers={"Referer": referer}, timeout=20
            )
        except requests.RequestException as exc:
            raise Blocked(f"error de red ({type(exc).__name__})") from None
        if r.status_code != 200:
            raise Blocked(f"HTTP {r.status_code}")
        text = r.text
    return text


# --------------------------------------------------------------------------
# Ubicacion de entrega (precios con IVA)
# --------------------------------------------------------------------------

LOCATION_MODAL = "#nav-global-location-data-modal-action"
ADDRESS_CHANGE = BASE + "/portal-migration/hz/glow/address-change?actionSource=glow"


def location_ok(html: str, postal_code: str) -> bool:
    """True si el 'Enviar a' de la pagina muestra el codigo postal pedido."""
    el = BeautifulSoup(html, "html.parser").find(id="glow-ingress-line2")
    return bool(el and postal_code in el.get_text())


def set_location(session: requests.Session, referer: str, page_html: str, postal_code: str) -> None:
    """Fija el codigo postal de entrega en la sesion, como hace la ventana 'Enviar a' de la web."""
    el = BeautifulSoup(page_html, "html.parser").select_one(LOCATION_MODAL)
    if el is None or not el.get("data-a-modal"):
        raise LocationError("no se encuentra el selector de ubicacion en la pagina")
    try:
        modal = json.loads(el["data-a-modal"])
    except ValueError:
        raise LocationError("selector de ubicacion ilegible") from None
    token = (modal.get("ajaxHeaders") or {}).get("anti-csrftoken-a2z")
    modal_url = modal.get("url")
    if not token or not modal_url:
        raise LocationError("faltan el token o la direccion de la ventana de ubicacion")

    xhr = {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE,
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        sleep(random.uniform(2, 4))
        r = session.get(
            urljoin(BASE + "/", modal_url),
            headers={**xhr, "Accept": "text/html,*/*", "anti-csrftoken-a2z": token},
            timeout=20,
        )
        r.raise_for_status()
        m = re.search(r'"?CSRF_TOKEN"?\s*[:=]\s*"([^"]+)"', unescape(r.text))
        post_token = m.group(1) if m else token

        query = {k: v[0] for k, v in parse_qs(urlparse(modal_url).query).items()}
        payload = {
            "locationType": "LOCATION_INPUT",
            "zipCode": postal_code,
            "storeContext": query.get("storeContext", "generic"),
            "deviceType": "web",
            "pageType": query.get("pageType", "Gateway"),
            "actionSource": "glow",
        }
        sleep(random.uniform(2, 4))
        r2 = session.post(
            ADDRESS_CHANGE, json=payload, headers={**xhr, "anti-csrftoken-a2z": post_token}, timeout=20
        )
        r2.raise_for_status()
        try:
            data = r2.json()
        except ValueError:
            snippet = BeautifulSoup(r2.text, "html.parser").get_text(" ", strip=True)[:120]
            ctype = r2.headers.get("content-type", "?")
            raise LocationError(
                f"respuesta no JSON al fijar la ubicacion (HTTP {r2.status_code}, {ctype}, "
                f"{len(r2.text)} bytes: {snippet!r})"
            ) from None
    except (requests.RequestException, ValueError) as exc:
        raise LocationError(f"fallo al fijar la ubicacion ({type(exc).__name__})") from None
    if not isinstance(data, dict) or data.get("isAddressUpdated") != 1:
        raise LocationError("Amazon no confirmo el cambio de codigo postal")
    if (data.get("address") or {}).get("countryCode") != "ES":
        raise LocationError("Amazon no acepto el codigo postal como direccion de España")


def search_urls(ch: Channel) -> list[str]:
    urls: list[str] = []
    for q in ch.queries:
        for page in range(1, ch.pages + 1):
            urls.append(f"{BASE}/s?k={quote_plus(q)}&page={page}{ch.search_params}")
    for url in ch.urls:
        for page in range(1, ch.pages + 1):
            if page == 1:
                urls.append(url)
            else:
                urls.append(url + ("&" if "?" in url else "?") + f"page={page}")
    return urls


LOCATION_ATTEMPTS = 3  # veces que se intenta fijar el codigo postal antes de rendirse


def collect(
    ch: Channel, html_file: Path | None = None, session: requests.Session | None = None
) -> list[Product]:
    if html_file is not None:
        return parse_results(html_file.read_text(encoding="utf-8"))
    session = session or make_session()
    found: dict[str, Product] = {}
    for i, url in enumerate(search_urls(ch)):
        if i:
            sleep(random.uniform(2, 5))
        html = fetch(session, url)
        results = parse_results(html)
        if is_blocked(html, len(results)):
            raise Blocked("captcha o bloqueo")
        if ch.require_spain and not location_ok(html, ch.postal_code):
            for attempt in range(1, LOCATION_ATTEMPTS + 1):
                log.info("%s: la entrega no esta en España; fijando CP %s (intento %d)", ch.name, ch.postal_code, attempt)
                try:
                    set_location(session, url, html, ch.postal_code)
                    sleep(random.uniform(2, 4))
                    html = fetch(session, url)
                    results = parse_results(html)
                    if is_blocked(html, len(results)):
                        raise Blocked("captcha o bloqueo")
                    if not location_ok(html, ch.postal_code):
                        raise LocationError("tras fijar el codigo postal la pagina sigue sin mostrar España")
                    break
                except LocationError as exc:
                    if attempt == LOCATION_ATTEMPTS:
                        raise
                    log.info("%s: intento %d fallido (%s); se reintenta", ch.name, attempt, exc)
                    sleep(random.uniform(5, 10))
                    html = fetch(session, url)
        log.info("%s: %d productos en la busqueda %d de %d", ch.name, len(results), i + 1, len(search_urls(ch)))
        for p in results:
            found.setdefault(p.asin, p)
    return list(found.values())


# --------------------------------------------------------------------------
# Antiguedad del producto: descarta altas recientes con precio recomendado inflado
# --------------------------------------------------------------------------

SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
LISTING_LABELS = ("Producto en Amazon.es desde", "Fecha de primera disponibilidad", "Disponible en Amazon.es desde")
NEW_BADGE = "Nuevo en Amazon"  # etiqueta 'Nuevo en Amazon en el ultimo mes' de la ficha
NEW_BADGE_AGE_DAYS = 29       # la etiqueta solo dice 'menos de un mes'
MAX_PAGE_CHECKS = 6           # fichas que se miran como maximo en cada ejecucion
REJECT_DAYS = 7               # cuanto se recuerda un descarte antes de volver a mirar el producto


def parse_spanish_date(text: str) -> date | None:
    """'12 de octubre de 2026' / '3 noviembre 2025' -> date"""
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([A-Za-zÁÉÍÓÚáéíóú]+)\s+(?:de\s+)?(\d{4})", text)
    if not m:
        return None
    month = SPANISH_MONTHS.get(m.group(2).lower())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def listing_age_days(html: str, today: date) -> int | None:
    """Dias desde que el producto esta en Amazon.es segun su ficha, o None si no se puede saber."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    for label in LISTING_LABELS:
        idx = text.find(label)
        if idx >= 0:
            listed = parse_spanish_date(text[idx + len(label): idx + len(label) + 60])
            if listed:
                return (today - listed).days
    if NEW_BADGE in text:
        return NEW_BADGE_AGE_DAYS
    return None


DATE_RE = re.compile(r"\d{1,2}\s+(?:de\s+)?[A-Za-zÁÉÍÓÚáéíóú]{4,10}\s+(?:de\s+)?\d{4}")


def date_hints(html: str, limit: int = 4) -> list[str]:
    """Fechas que aparecen en la ficha con el texto que las precede (para ajustar las etiquetas)."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    hints = []
    for m in DATE_RE.finditer(text):
        if parse_spanish_date(m.group(0)):
            hints.append(text[max(0, m.start() - 50): m.end()])
        if len(hints) >= limit:
            break
    return hints


HISTORY_RE = re.compile(
    r"ha variado entre\s*([\d.,]+)\s*€\s*y\s*([\d.,]+)\s*€", re.I
)
HISTORY_DAYS_RE = re.compile(r"[ÚúUu]ltimos\s+(\d+)\s+d[ií]as", re.I)


def price_history(html: str) -> tuple[float, float, int | None] | None:
    """(minimo, maximo, dias) del bloque 'Historial de precios' de la ficha, o None si no esta."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = HISTORY_RE.search(text)
    if not m:
        return None
    lo, hi = parse_price(m.group(1)), parse_price(m.group(2))
    if lo is None or hi is None:
        return None
    before = text[max(0, m.start() - 160): m.start()]
    days = HISTORY_DAYS_RE.findall(before)
    return min(lo, hi), max(lo, hi), int(days[-1]) if days else None


def listing_ok(session: requests.Session, p: Product, ch: Channel, today: date) -> tuple[bool, str]:
    """Mira la ficha: descarta altas recientes y descuentos que el historial de precios desmiente.

    Si la ficha no trae la informacion, acepta el producto y lo anota en el log.
    """
    sleep(random.uniform(2, 4))
    html = fetch(session, f"{BASE}/dp/{p.asin}")

    if ch.min_listing_days > 0:
        age = listing_age_days(html, today)
        if age is not None and age < ch.min_listing_days:
            return False, f"alta hace {age} dias"

    if ch.min_history_drop > 0:
        hist = price_history(html)
        if hist is None:
            log.info("%s: %s sin historial de precios en la ficha; se acepta (%d bytes)", ch.name, p.asin, len(html))
        else:
            lo, hi, days = hist
            hi = max(hi, p.price)
            drop = round((1 - p.price / hi) * 100)
            span = f"{days} dias" if days else "el periodo"
            if drop < ch.min_history_drop:
                return False, (
                    f"precio habitual: entre {fmt_eur(lo)} y {fmt_eur(hi)} en {span}; "
                    f"hoy solo {drop}% menos que el maximo"
                )
    return True, ""


# --------------------------------------------------------------------------
# Seleccion y anti-duplicados
# --------------------------------------------------------------------------


def filter_steps(ch: Channel) -> list[tuple[str, callable]]:
    """Filtros en orden. Los usan 'eligible' y el embudo del log, para que nunca se desincronicen."""
    steps = [
        ("con precio", lambda p: bool(p.title and p.price)),
        ("no patrocinados", lambda p: not (ch.skip_sponsored and p.sponsored)),
    ]
    if ch.min_discount > 0:
        steps.append(("con precio anterior", lambda p: p.discount > 0))
    steps.append((f"con >={ch.min_discount}% dto", lambda p: p.discount >= ch.min_discount))
    if ch.max_discount:
        steps.append((f"con <={ch.max_discount}% dto", lambda p: p.discount <= ch.max_discount))
    steps += [
        (
            "en rango de precio",
            lambda p: p.price >= ch.min_price and not (ch.max_price and p.price > ch.max_price),
        ),
        (
            f"con nota >={ch.min_rating}",
            lambda p: not ch.min_rating or (p.rating is not None and p.rating >= ch.min_rating),
        ),
        (
            f"con >={ch.min_reviews} valoraciones",
            lambda p: not ch.min_reviews or (p.reviews is not None and p.reviews >= ch.min_reviews),
        ),
    ]
    return steps


def eligible(p: Product, ch: Channel) -> bool:
    return all(keep(p) for _, keep in filter_steps(ch))


def funnel(products: list[Product], ch: Channel, state: dict, skip: frozenset | set = frozenset()) -> str:
    """Resume cuantos productos sobreviven a cada filtro: 'N leidos -> N con precio -> ...'."""
    parts = [f"{len(products)} leidos"]
    current = products
    for label, keep in filter_steps(ch):
        current = [p for p in current if keep(p)]
        parts.append(f"{len(current)} {label}")
    parts.append(f"{len(pick(current, ch, state, limit=len(current) or 1, skip=skip))} nuevos")
    return " -> ".join(parts)


def explain(products: list[Product], ch: Channel, state: dict, skip=frozenset(), chosen=()) -> list[tuple[Product, str]]:
    """Para cada producto leido, el motivo por el que se descarta (o 'candidato' / 'elegido')."""
    steps = filter_steps(ch)
    reasons: dict[str, str] = {}
    survivors: list[Product] = []
    for p in products:
        for label, keep in steps:
            if not keep(p):
                reasons[p.asin] = f"no cumple: {label}"
                break
        else:
            survivors.append(p)
    fresh = {p.asin for p in pick(survivors, ch, state, limit=len(survivors) or 1, skip=skip)}
    picked = {p.asin for p in chosen}
    for p in survivors:
        if p.asin in picked:
            reasons[p.asin] = "elegido"
        elif p.asin in state:
            reasons[p.asin] = "ya publicado"
        elif p.asin in skip:
            reasons[p.asin] = f"descartado por la ficha: {skip[p.asin].get('why', '') if isinstance(skip, dict) else ''}".strip(": ")
        elif p.asin not in fresh:
            reasons[p.asin] = "parecido a otro ya publicado"
        else:
            reasons[p.asin] = "candidato (no elegido esta vez)"
    return [(p, reasons[p.asin]) for p in products]


def write_report(path: Path, rows: list[tuple]) -> None:
    """CSV con todos los productos leidos y su motivo; se abre con Excel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["asin", "categoria", "motivo", "descuento_%", "precio", "pvpr", "nota", "valoraciones", "titulo"])
        for p, why, *cat in sorted(rows, key=lambda r: (r[2] if len(r) > 2 else "", r[1])):
            w.writerow([p.asin, cat[0] if cat else "", why, p.discount, p.price, p.old_price, p.rating,
                        p.reviews, p.title[:120]])


def score(p: Product) -> float:
    rating = (p.rating or 4.0) / 5
    return p.discount * rating * (1 + math.log10(1 + (p.reviews or 0)))


def _tokens(title: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9áéíóúüñ]+", title.lower()) if len(t) > 2}


def similar(a: str, b: str, threshold: float) -> bool:
    """True si dos titulos parecen el mismo producto (por ejemplo, en otro color o talla)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    common = len(ta & tb)
    if common / len(ta | tb) >= threshold:
        return True
    # una variante suele ser el mismo titulo con una o dos palabras cambiadas
    smaller = min(len(ta), len(tb))
    return smaller >= 4 and common / smaller >= 0.8


def pick(
    products: list[Product],
    ch: Channel,
    state: dict,
    limit: int | None = None,
    skip: frozenset | set = frozenset(),
) -> list[Product]:
    """Mejores ofertas que cumplen los filtros y no se han publicado (ni una variante parecida).

    'skip' son los ASIN descartados hace poco (por ejemplo, por ser altas muy recientes).
    """
    limit = ch.max_posts if limit is None else limit
    recent_titles = [v.get("title", "") for v in state.values()]
    chosen: list[Product] = []
    for p in sorted((p for p in products if eligible(p, ch)), key=score, reverse=True):
        if p.asin in state or p.asin in skip:
            continue
        if any(similar(p.title, t, ch.similarity) for t in recent_titles):
            continue
        chosen.append(p)
        recent_titles.append(p.title)
        if len(chosen) >= limit:
            break
    return chosen


def _ts(entry: dict) -> datetime:
    try:
        dt = datetime.fromisoformat(entry["t"])
    except (KeyError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read_state_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_state(path: Path, repeat_days: int, now: datetime) -> dict:
    """Ofertas ya publicadas dentro de la ventana de repeticion (acepta el formato antiguo, plano)."""
    data = _read_state_file(path)
    posted = data["posted"] if isinstance(data.get("posted"), dict) else data
    cutoff = now - timedelta(days=repeat_days)
    return {a: v for a, v in posted.items() if isinstance(v, dict) and _ts(v) > cutoff}


def load_rejected(path: Path, now: datetime) -> dict:
    """Productos descartados hace menos de REJECT_DAYS dias."""
    rejected = _read_state_file(path).get("rejected")
    if not isinstance(rejected, dict):
        return {}
    cutoff = now - timedelta(days=REJECT_DAYS)
    return {a: v for a, v in rejected.items() if isinstance(v, dict) and _ts(v) > cutoff}


def load_next_at(path: Path) -> datetime | None:
    """Momento a partir del cual toca la siguiente publicacion."""
    raw = _read_state_file(path).get("next_at")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_cats(path: Path) -> dict:
    """Cuando se visito por ultima vez cada categoria (nombre -> ISO)."""
    cats = _read_state_file(path).get("cats")
    return {k: v for k, v in cats.items() if isinstance(v, str)} if isinstance(cats, dict) else {}


def load_problem(path: Path) -> dict:
    """Contadores de fallos seguidos y cuando se envio el ultimo aviso."""
    problem = _read_state_file(path).get("problem")
    return dict(problem) if isinstance(problem, dict) else {}


def set_state_key(path: Path, key: str, value) -> None:
    """Cambia una sola clave del archivo de estado sin tocar el resto."""
    data = _read_state_file(path)
    if "posted" not in data:  # formato antiguo (plano): se conserva como 'posted'
        data = {"posted": data}
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def save_state(
    path: Path,
    posted: dict,
    next_at: datetime | None = None,
    rejected: dict | None = None,
    cats: dict | None = None,
    problem: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "next_at": next_at.isoformat() if next_at else None,
        "posted": posted,
        "rejected": rejected or {},
        "cats": load_cats(path) if cats is None else cats,
        "problem": load_problem(path) if problem is None else problem,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Mensaje y Telegram
# --------------------------------------------------------------------------


def fmt_eur(value: float) -> str:
    """1299.9 -> '1.299,90€' ; 23.0 -> '23€'"""
    s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if s.endswith(",00"):
        s = s[:-3]
    return f"{s}€"


def affiliate_url(asin: str, tag: str | None) -> str:
    url = f"{BASE}/dp/{asin}"
    return f"{url}?tag={tag}" if tag else url


def full_size_image(url: str | None) -> str | None:
    if not url:
        return None
    return re.sub(r"\._[A-Za-z0-9_,%-]+_\.", ".", url)


def to_square_jpeg(data: bytes, size: int = PHOTO_SIZE, margin: int = PHOTO_MARGIN) -> bytes:
    """Centra la foto sobre un lienzo blanco size x size, sin recortarla ni deformarla."""
    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            flat = Image.new("RGB", img.size, "white")
            flat.paste(img, mask=img.getchannel("A"))
            img = flat
        else:
            img = img.convert("RGB")
        inner = size - 2 * margin
        scale = min(inner / img.width, inner / img.height)
        resized = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS
        )
    canvas = Image.new("RGB", (size, size), "white")
    canvas.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2))
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def prepare_image(url: str | None) -> bytes | None:
    """Descarga la foto del producto y la devuelve como JPEG cuadrado, o None si algo falla."""
    if not url:
        return None
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme != "https" or not host.endswith(IMAGE_HOST_SUFFIXES):
        log.warning("Imagen ignorada: servidor no permitido (%s)", host or "?")
        return None
    try:
        r = requests.get(url, timeout=20, headers={"Referer": BASE + "/"})
        r.raise_for_status()
        data = r.content
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("imagen demasiado grande")
        return to_square_jpeg(data)
    except (requests.RequestException, OSError, ValueError, Image.DecompressionBombError) as exc:
        log.warning("No se pudo preparar la imagen (%s); se usara la original", type(exc).__name__)
        return None


def build_caption(p: Product, url: str, ch: Channel, category: dict | None = None) -> str:
    title = p.title if len(p.title) <= 110 else p.title[:107].rstrip() + "…"
    lines = []
    if category:
        header = " ".join(x for x in (category.get("emoji"), category["nombre"]) if x)
        lines += [f"<b>{escape(header)}</b>", ""]
    lines += [f"🔥 <b>{escape(title)}</b>", ""]
    if p.old_price and p.discount:
        lines.append(f"PVPR: <s>{fmt_eur(p.old_price)}</s>")
        lines.append(f"Ahora: {fmt_eur(p.price)}")
        lines.append(f"−{p.discount} % · Diferencia {fmt_eur(round(p.old_price - p.price, 2))}")
    else:
        lines.append(f"Ahora: {fmt_eur(p.price)}")
    if p.rating:
        lines.append("")
        rating_line = f"⭐ {str(p.rating).replace('.', ',')}/5"
        if p.reviews:
            rating_line += f" · {p.reviews:,}".replace(",", ".") + " valoraciones"
        lines.append(rating_line)
    lines += ["", f'👉 <a href="{escape(url, quote=True)}">Ver en Amazon</a>']
    if ch.footer:
        lines += ["", f"<i>{escape(ch.footer)}</i>"]
    return "\n".join(lines)


class TelegramError(Exception):
    pass


def tg_call(method: str, token: str, data: dict, retries: int = 2, files: dict | None = None) -> None:
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, data=data, files=files, timeout=60 if files else 20)
        except requests.RequestException as exc:
            # 'from None': la excepcion original contiene la URL con el token
            raise TelegramError(f"{method}: error de red ({type(exc).__name__})") from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 429 and attempt < retries:
            wait = (body.get("parameters") or {}).get("retry_after", 5)
            sleep(min(int(wait), 30))
            continue
        if not r.ok:
            raise TelegramError(f"{method}: HTTP {r.status_code} {scrub(str(body.get('description', '')))}")
        return


def send_deal(
    token: str, chat_id: str, caption: str, photo: bytes | None, photo_url: str | None = None
) -> None:
    """Prefiere la foto cuadrada subida como archivo; si falla, la URL original; si no, solo texto."""
    base = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if photo:
        try:
            tg_call("sendPhoto", token, base, files={"photo": ("oferta.jpg", photo, "image/jpeg")})
            return
        except TelegramError as exc:
            log.warning("sendPhoto con archivo fallo (%s)", exc)
    if photo_url:
        try:
            tg_call("sendPhoto", token, {**base, "photo": photo_url})
            return
        except TelegramError as exc:
            log.warning("sendPhoto con URL fallo (%s); envio solo texto", exc)
    tg_call("sendMessage", token, {"chat_id": chat_id, "text": caption, "parse_mode": "HTML"})


# --------------------------------------------------------------------------
# Ejecucion
# --------------------------------------------------------------------------


def send_alert(ch: Channel, text: str) -> bool:
    """Aviso privado por Telegram. Nunca lanza errores: un aviso que falla no debe parar el bot."""
    token, chat = get_secret(ch.token_env), get_secret(ch.alert_chat_env)
    if not token or not chat:
        log.info("%s: sin %s definido; no se envia el aviso", ch.name, ch.alert_chat_env)
        return False
    try:
        tg_call("sendMessage", token, {"chat_id": chat, "text": text}, retries=1)
    except TelegramError as exc:
        log.warning("%s: no se pudo enviar el aviso (%s)", ch.name, exc)
        return False
    return True


def record_problem(ch: Channel, state_path: Path, kind: str, detail: str, now: datetime) -> None:
    """Cuenta un fallo ('fail') o un intento sin candidatos ('empty') y avisa al llegar al umbral."""
    problem = load_problem(state_path)
    key = "fails" if kind == "fail" else "empties"
    problem[key] = int(problem.get(key, 0)) + 1
    problem["last"] = scrub(detail)[:200]
    threshold = ch.alert_after if kind == "fail" else ch.alert_after_empty
    alerted = None
    if isinstance(problem.get("alerted_at"), str):
        try:
            alerted = datetime.fromisoformat(problem["alerted_at"])
        except ValueError:
            alerted = None
    due = alerted is None or now - alerted >= timedelta(minutes=ch.alert_repeat_min)
    if problem[key] >= threshold and due:
        if kind == "fail":
            text = f"⚠️ {ch.name}: {problem[key]} fallos seguidos. Ultimo: {problem['last']}. Sigo reintentando."
        else:
            text = (
                f"⚠️ {ch.name}: {problem[key]} intentos seguidos sin ningun producto que publicar. "
                "Puede que los filtros sean demasiado estrictos o que Amazon no devuelva resultados."
            )
        if send_alert(ch, text):
            problem["alerted_at"] = now.isoformat()
    set_state_key(state_path, "problem", problem)


def record_success(ch: Channel, state_path: Path) -> None:
    """Tras publicar: pone los contadores a cero y avisa de que todo ha vuelto a la normalidad."""
    problem = load_problem(state_path)
    if not problem:
        return
    if problem.get("alerted_at"):
        send_alert(ch, f"✅ {ch.name}: vuelve a publicar con normalidad.")
    set_state_key(state_path, "problem", {})


def note(line: str) -> None:
    """Resumen de una linea: va al mensaje del commit de estado y al resumen de la ejecucion."""
    try:
        with (ROOT / "run_note.txt").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(line + "\n\n")
    except OSError:
        pass


def run_channel(
    ch: Channel,
    *,
    dry_run: bool = False,
    html_file: Path | None = None,
    state_dir: Path | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> int:
    """Devuelve cuantas ofertas se han publicado (o se habrian publicado).

    Con publicacion real y sin 'force', respeta el horario del canal y el intervalo
    entre ofertas; fuera de turno termina sin consultar Amazon.
    """
    state_dir = state_dir or ROOT / "state"
    now = now or datetime.now(timezone.utc)
    state_path = state_dir / f"{ch.name}.json"
    state = load_state(state_path, ch.repeat_days, now)
    publishing = not dry_run and html_file is None

    if publishing and not force:
        if not in_window(now, ch):
            log.info("%s: fuera de horario (%s-%s %s)", ch.name, ch.window_start, ch.window_end, ch.timezone)
            return 0
        next_at = load_next_at(state_path)
        if next_at and now < next_at:
            wait = int((next_at - now).total_seconds() // 60) + 1
            log.info("%s: aun no toca (faltan unos %d min)", ch.name, wait)
            return 0

    tag = get_secret("AMAZON_TAG")
    if not tag:
        log.warning("AMAZON_TAG no definido: los enlaces saldran sin tag de afiliado")

    token = chat_id = None
    if publishing:
        token, chat_id = get_secret(ch.token_env), get_secret(ch.chat_env)
        if not token or not chat_id:
            raise ConfigError(f"{ch.name}: faltan los secretos {ch.token_env} / {ch.chat_env}")

    retry_at = now + timedelta(minutes=ch.retry_minutes)
    rejected = load_rejected(state_path, now)
    cats = load_cats(state_path)
    session = make_session() if html_file is None else None
    if ch.categories:
        targets = category_order(ch, cats)[: 1 if html_file else ch.searches_per_run]
    else:
        targets = [None]

    chosen: list[Product] = []
    chosen_cat: dict[str, dict | None] = {}
    rows: list[tuple[Product, str, str]] = []
    for cat in targets:
        cch = category_channel(ch, cat) if cat else ch
        label = f"{ch.name}/{cat['nombre']}" if cat else ch.name
        try:
            products = collect(cch, html_file, session)
        except (Blocked, LocationError) as exc:
            if publishing and not force:
                save_state(state_path, state, next_at=retry_at, rejected=rejected, cats=cats)  # no insistir
                record_problem(ch, state_path, "fail", str(exc), now)
            raise
        if cat:
            cats[cat["nombre"]] = now.isoformat()
        check_pages = session is not None and (cch.min_listing_days > 0 or cch.min_history_drop > 0)
        candidates = pick(
            products, cch, state, limit=MAX_PAGE_CHECKS if check_pages else None, skip=set(rejected)
        )
        log.info("%s: %s", label, funnel(products, cch, state, skip=set(rejected)))

        for p in candidates:
            if len(chosen) >= ch.max_posts:
                break
            if check_pages:
                try:
                    ok, why = listing_ok(session, p, cch, now.date())
                except Exception as exc:  # una ficha que no carga no debe tumbar la ejecucion
                    log.info("%s: %s no se pudo comprobar la ficha (%s); se acepta", label, p.asin, type(exc).__name__)
                    ok, why = True, ""
                if not ok:
                    log.info("%s: descartado %s (%s)", label, p.asin, why)
                    rejected[p.asin] = {"t": now.isoformat(), "why": why}
                    continue
            chosen.append(p)
            chosen_cat[p.asin] = cat
        rows += [(p, why, cat["nombre"] if cat else "") for p, why in
                 explain(products, cch, state, skip=rejected, chosen=chosen)]
        if chosen:
            break
    try:
        write_report(ROOT / "reports" / f"{ch.name}.csv", rows)
    except OSError as exc:  # el informe es solo ayuda para revisar: nunca debe impedir publicar
        log.info("%s: no se pudo escribir el informe (%s)", ch.name, type(exc).__name__)
    if publishing and not chosen:
        note(f"{ch.name}: sin candidatos ({len(targets)} categorias probadas)" if ch.categories
             else f"{ch.name}: sin candidatos")
    if publishing and not force and not chosen:
        save_state(state_path, state, next_at=retry_at, rejected=rejected, cats=cats)
        record_problem(ch, state_path, "empty", "sin candidatos", now)
    elif publishing:
        save_state(state_path, state, next_at=load_next_at(state_path), rejected=rejected, cats=cats)

    next_post = now + timedelta(minutes=random.uniform(ch.interval_min, ch.interval_max))
    published = 0
    for p in chosen:
        caption = build_caption(p, affiliate_url(p.asin, tag), ch, chosen_cat.get(p.asin))
        if token is None:
            print(f"--- {ch.name} / {p.asin} ---\n{caption}\n")
            published += 1
            continue
        photo_url = full_size_image(p.image)
        try:
            send_deal(token, chat_id, caption, prepare_image(photo_url), photo_url)
        except TelegramError as exc:
            if not force:
                record_problem(ch, state_path, "fail", str(exc), now)
            raise
        state[p.asin] = {"t": datetime.now(timezone.utc).isoformat(), "title": p.title[:200]}
        save_state(state_path, state, next_at=next_post, rejected=rejected, cats=cats)
        published += 1
        where = f"{chosen_cat[p.asin]['nombre']}, " if chosen_cat.get(p.asin) else ""
        log.info("%s: publicada %s (%s-%d%%)", ch.name, p.asin, where, p.discount)
        note(f"{ch.name}: publicada {p.asin} ({where}-{p.discount}%)")
        record_success(ch, state_path)
        if published < len(chosen):
            sleep(2)
    return published


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="todos los canales activos")
    group.add_argument("--channel", help="nombre de un canal (aunque este desactivado)")
    ap.add_argument("--dry-run", action="store_true", help="no publica ni guarda estado")
    ap.add_argument("--html", type=Path, help="parsea este HTML en vez de consultar Amazon")
    ap.add_argument("--force", action="store_true", help="ignora horario e intervalo (pruebas manuales)")
    args = ap.parse_args(argv)
    force = args.force or os.environ.get("FORCE_RUN") == "1"

    setup_logging()
    load_dotenv()
    try:
        (ROOT / "run_note.txt").unlink()
    except OSError:
        pass

    if args.channel:
        paths = [ROOT / "channels" / f"{args.channel}.json"]
    else:
        paths = sorted((ROOT / "channels").glob("*.json"))

    failed = 0
    for path in paths:
        ch = None
        try:
            ch = load_channel(path)
            if args.all and not ch.enabled:
                continue
            run_channel(ch, dry_run=args.dry_run, html_file=args.html, force=force)
        except Blocked as exc:
            print(f"::warning::Amazon no ha respondido ({scrub(str(exc))})")
            log.warning("%s: bloqueado (%s)", path.stem, exc)
        except LocationError as exc:
            print(f"::warning::No se pudo fijar la entrega en España; no se publica ({scrub(str(exc))})")
            log.warning("%s: ubicacion no fijada (%s)", path.stem, exc)
        except (ConfigError, TelegramError) as exc:
            log.error("%s: %s", path.stem, exc)
            failed += 1
        except Exception as exc:  # sin traceback: podria llevar secretos
            log.error("%s: error inesperado %s: %s", path.stem, type(exc).__name__, scrub(str(exc)))
            failed += 1
            if ch is not None and not (args.dry_run or args.html or force):
                try:
                    record_problem(
                        ch, ROOT / "state" / f"{ch.name}.json", "fail",
                        f"error inesperado {type(exc).__name__}", datetime.now(timezone.utc),
                    )
                except Exception:
                    pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
