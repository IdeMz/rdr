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
import io
import json
import logging
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
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
    if not ch.queries and not ch.urls:
        raise ConfigError(f"{path.name}: hace falta 'queries' o 'urls'")
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
    return ch


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
        data = r2.json()
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


def collect(ch: Channel, html_file: Path | None = None) -> list[Product]:
    if html_file is not None:
        return parse_results(html_file.read_text(encoding="utf-8"))
    session = make_session()
    found: dict[str, Product] = {}
    for i, url in enumerate(search_urls(ch)):
        if i:
            sleep(random.uniform(2, 5))
        html = fetch(session, url)
        results = parse_results(html)
        if is_blocked(html, len(results)):
            raise Blocked("captcha o bloqueo")
        if ch.require_spain and not location_ok(html, ch.postal_code):
            log.info("%s: la entrega no esta en España; fijando CP %s", ch.name, ch.postal_code)
            set_location(session, url, html, ch.postal_code)
            sleep(random.uniform(2, 4))
            html = fetch(session, url)
            results = parse_results(html)
            if is_blocked(html, len(results)):
                raise Blocked("captcha o bloqueo")
            if not location_ok(html, ch.postal_code):
                raise LocationError("tras fijar el codigo postal la pagina sigue sin mostrar España")
        log.info("%s: %d productos en la busqueda %d de %d", ch.name, len(results), i + 1, len(search_urls(ch)))
        for p in results:
            found.setdefault(p.asin, p)
    return list(found.values())


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
    steps += [
        (f"con >={ch.min_discount}% dto", lambda p: p.discount >= ch.min_discount),
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


def funnel(products: list[Product], ch: Channel, state: dict) -> str:
    """Resume cuantos productos sobreviven a cada filtro: 'N leidos -> N con precio -> ...'."""
    parts = [f"{len(products)} leidos"]
    current = products
    for label, keep in filter_steps(ch):
        current = [p for p in current if keep(p)]
        parts.append(f"{len(current)} {label}")
    parts.append(f"{len(pick(current, ch, state, limit=len(current) or 1))} nuevos")
    return " -> ".join(parts)


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


def pick(products: list[Product], ch: Channel, state: dict, limit: int | None = None) -> list[Product]:
    """Mejores ofertas que cumplen los filtros y no se han publicado (ni una variante parecida)."""
    limit = ch.max_posts if limit is None else limit
    recent_titles = [v.get("title", "") for v in state.values()]
    chosen: list[Product] = []
    for p in sorted((p for p in products if eligible(p, ch)), key=score, reverse=True):
        if p.asin in state:
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


def save_state(path: Path, posted: dict, next_at: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"next_at": next_at.isoformat() if next_at else None, "posted": posted}
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


def build_caption(p: Product, url: str, ch: Channel) -> str:
    title = p.title if len(p.title) <= 110 else p.title[:107].rstrip() + "…"
    lines = [f"🔥 <b>{escape(title)}</b>", ""]
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
    try:
        products = collect(ch, html_file)
    except (Blocked, LocationError):
        if publishing and not force:
            save_state(state_path, state, next_at=retry_at)  # no insistir en cada ejecucion
        raise
    chosen = pick(products, ch, state)
    log.info("%s: %s", ch.name, funnel(products, ch, state))
    if publishing and not chosen and not force:
        save_state(state_path, state, next_at=retry_at)

    next_post = now + timedelta(minutes=random.uniform(ch.interval_min, ch.interval_max))
    published = 0
    for p in chosen:
        caption = build_caption(p, affiliate_url(p.asin, tag), ch)
        if token is None:
            print(f"--- {ch.name} / {p.asin} ---\n{caption}\n")
            published += 1
            continue
        photo_url = full_size_image(p.image)
        send_deal(token, chat_id, caption, prepare_image(photo_url), photo_url)
        state[p.asin] = {"t": datetime.now(timezone.utc).isoformat(), "title": p.title[:200]}
        save_state(state_path, state, next_at=next_post)
        published += 1
        log.info("%s: publicada %s (-%d%%)", ch.name, p.asin, p.discount)
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

    if args.channel:
        paths = [ROOT / "channels" / f"{args.channel}.json"]
    else:
        paths = sorted((ROOT / "channels").glob("*.json"))

    failed = 0
    for path in paths:
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
