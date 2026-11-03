#!/usr/bin/env python3
"""Publica ofertas de Amazon.es en canales de Telegram.

Cada canal es un JSON en channels/. El estado anti-repeticion vive en state/.

    python deals.py --all
    python deals.py --channel NOMBRE [--dry-run]
    python deals.py --channel NOMBRE --html pagina.html     # prueba sin red
"""
from __future__ import annotations

import argparse
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
from html import escape
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
BASE = "https://www.amazon.es"
TELEGRAM_API = "https://api.telegram.org"

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


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = ()
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_ScrubFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
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
    similarity: float = 0.7
    skip_sponsored: bool = True
    footer: str = "Enlace de afiliado"
    enabled: bool = True


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
    return ch


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
                return r.text
            last = f"HTTP {r.status_code}"
        log.warning("Amazon: %s, intento %d/%d", last, attempt, retries)
        if attempt < retries:
            sleep(random.uniform(3, 7) * attempt)
    raise Blocked(last)


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
        log.info("%s: %d productos en la pagina %d", ch.name, len(results), i + 1)
        for p in results:
            found.setdefault(p.asin, p)
    return list(found.values())


# --------------------------------------------------------------------------
# Seleccion y anti-duplicados
# --------------------------------------------------------------------------


def eligible(p: Product, ch: Channel) -> bool:
    if not p.title or not p.price:
        return False
    if ch.skip_sponsored and p.sponsored:
        return False
    if p.discount < ch.min_discount:
        return False
    if p.price < ch.min_price or (ch.max_price and p.price > ch.max_price):
        return False
    if ch.min_rating and (p.rating is None or p.rating < ch.min_rating):
        return False
    if ch.min_reviews and (p.reviews is None or p.reviews < ch.min_reviews):
        return False
    return True


def score(p: Product) -> float:
    rating = (p.rating or 4.0) / 5
    return p.discount * rating * (1 + math.log10(1 + (p.reviews or 0)))


def _tokens(title: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9áéíóúüñ]+", title.lower()) if len(t) > 2}


def similar(a: str, b: str, threshold: float) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def pick(products: list[Product], ch: Channel, state: dict) -> list[Product]:
    recent_titles = [v.get("title", "") for v in state.values()]
    chosen: list[Product] = []
    for p in sorted((p for p in products if eligible(p, ch)), key=score, reverse=True):
        if p.asin in state:
            continue
        if any(similar(p.title, t, ch.similarity) for t in recent_titles):
            continue
        chosen.append(p)
        recent_titles.append(p.title)
        if len(chosen) >= ch.max_posts:
            break
    return chosen


def _ts(entry: dict) -> datetime:
    try:
        dt = datetime.fromisoformat(entry["t"])
    except (KeyError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_state(path: Path, repeat_days: int, now: datetime) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = now - timedelta(days=repeat_days)
    return {a: v for a, v in data.items() if isinstance(v, dict) and _ts(v) > cutoff}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Mensaje y Telegram
# --------------------------------------------------------------------------


def fmt_eur(value: float) -> str:
    s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if s.endswith(",00"):
        s = s[:-3]
    return f"{s} €"


def affiliate_url(asin: str, tag: str | None) -> str:
    url = f"{BASE}/dp/{asin}"
    return f"{url}?tag={tag}" if tag else url


def full_size_image(url: str | None) -> str | None:
    if not url:
        return None
    return re.sub(r"\._[A-Za-z0-9_,%-]+_\.", ".", url)


def build_caption(p: Product, url: str, ch: Channel) -> str:
    title = p.title if len(p.title) <= 110 else p.title[:107].rstrip() + "…"
    lines = [f"🔥 <b>{escape(title)}</b>", ""]
    price_line = f"💶 <b>{fmt_eur(p.price)}</b>"
    if p.old_price and p.discount:
        price_line += f"  <s>{fmt_eur(p.old_price)}</s>  (-{p.discount}%)"
    lines.append(price_line)
    if p.rating:
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


def tg_call(method: str, token: str, data: dict, retries: int = 2) -> None:
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, data=data, timeout=20)
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


def send_deal(token: str, chat_id: str, caption: str, image: str | None) -> None:
    if image:
        try:
            tg_call(
                "sendPhoto",
                token,
                {"chat_id": chat_id, "photo": image, "caption": caption, "parse_mode": "HTML"},
            )
            return
        except TelegramError as exc:
            log.warning("sendPhoto fallo (%s); envio solo texto", exc)
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
) -> int:
    """Devuelve cuantas ofertas se han publicado (o se habrian publicado)."""
    state_dir = state_dir or ROOT / "state"
    now = datetime.now(timezone.utc)
    state_path = state_dir / f"{ch.name}.json"
    state = load_state(state_path, ch.repeat_days, now)

    tag = get_secret("AMAZON_TAG")
    if not tag:
        log.warning("AMAZON_TAG no definido: los enlaces saldran sin tag de afiliado")

    token = chat_id = None
    if not dry_run and html_file is None:
        token, chat_id = get_secret(ch.token_env), get_secret(ch.chat_env)
        if not token or not chat_id:
            raise ConfigError(f"{ch.name}: faltan los secretos {ch.token_env} / {ch.chat_env}")

    products = collect(ch, html_file)
    chosen = pick(products, ch, state)
    log.info("%s: %d productos, %d candidatos nuevos", ch.name, len(products), len(chosen))

    published = 0
    for p in chosen:
        caption = build_caption(p, affiliate_url(p.asin, tag), ch)
        if token is None:
            print(f"--- {ch.name} / {p.asin} ---\n{caption}\n")
            published += 1
            continue
        send_deal(token, chat_id, caption, full_size_image(p.image))
        state[p.asin] = {"t": datetime.now(timezone.utc).isoformat(), "title": p.title[:120]}
        save_state(state_path, state)
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
    args = ap.parse_args(argv)

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
            run_channel(ch, dry_run=args.dry_run, html_file=args.html)
        except Blocked as exc:
            print(f"::warning::Amazon no ha respondido ({scrub(str(exc))})")
            log.warning("%s: bloqueado (%s)", path.stem, exc)
        except (ConfigError, TelegramError) as exc:
            log.error("%s: %s", path.stem, exc)
            failed += 1
        except Exception as exc:  # sin traceback: podria llevar secretos
            log.error("%s: error inesperado %s: %s", path.stem, type(exc).__name__, scrub(str(exc)))
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
