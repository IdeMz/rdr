import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

import deals

TOKEN = "123456789:AAH_fake-token_abcdefghijklmnopqrstuvwxyz0123"


def card(asin, title, price, old=None, rating="4,5 de 5 estrellas", reviews="1.234", sponsored=False,
         image="https://m.media-amazon.com/images/I/71abcDEF._AC_UY218_.jpg"):
    old_html = f'<span class="a-price a-text-price"><span class="a-offscreen">{old}</span></span>' if old else ""
    spons = '<span class="puis-sponsored-label-text">Patrocinado</span>' if sponsored else ""
    return f"""
    <div data-component-type="s-search-result" data-asin="{asin}">
      {spons}
      <img class="s-image" src="{image}">
      <h2 aria-label="{title}"><a><span>{title}</span></a></h2>
      <span class="a-icon-alt">{rating}</span>
      <span class="s-underline-text">{reviews}</span>
      <span class="a-price"><span class="a-offscreen">{price}</span></span>
      {old_html}
    </div>"""


def page(*cards):
    return "<html><body>" + "".join(cards) + "</body></html>"


def channel(**kw):
    base = dict(name="t", token_env="TG_T", chat_env="TG_C", queries=["x"])
    base.update(kw)
    return deals.Channel(**base)


# ---------------------------------------------------------------- parseo

@pytest.mark.parametrize("text,expected", [
    ("1.299,99 €", 1299.99),
    ("29,99€", 29.99),
    ("29,99\xa0€", 29.99),
    ("1.299 €", 1299.0),
    ("", None),
    (None, None),
    ("gratis", None),
])
def test_parse_price(text, expected):
    assert deals.parse_price(text) == expected


def test_parse_rating():
    assert deals.parse_rating("4,5 de 5 estrellas") == 4.5
    assert deals.parse_rating("4 de 5") == 4.0
    assert deals.parse_rating("sin valoraciones") is None


@pytest.mark.parametrize("text,expected", [
    ("1.234", 1234), ("(2,5 mil)", 2500), ("12 mil", 12000), ("87", 87),
    ("1.234 mejores", 1234), ("", None), ("sin datos", None),
])
def test_parse_count(text, expected):
    assert deals.parse_count(text) == expected


def test_parse_results():
    html = page(
        card("B000000001", "Auriculares X", "39,99 €", "79,99 €"),
        card("B000000002", "Patrocinado Y", "10,00 €", "20,00 €", sponsored=True),
        card("INVALID", "No vale", "1,00 €"),
        card("B000000003", "Sin descuento", "10,00 €"),
    )
    items = deals.parse_results(html)
    assert [p.asin for p in items] == ["B000000001", "B000000002", "B000000003"]
    first = items[0]
    assert first.price == 39.99 and first.old_price == 79.99
    assert first.discount == 50
    assert first.rating == 4.5 and first.reviews == 1234
    assert items[1].sponsored and not first.sponsored
    assert items[2].discount == 0


def test_blocked_detection():
    assert deals.is_blocked("<html>Introduce los caracteres que ves</html>", 0)
    assert not deals.is_blocked("<html>Introduce los caracteres que ves</html>", 3)
    assert not deals.is_blocked("<html>nada</html>", 0)


# ------------------------------------------------------------- seleccion

def prod(asin, title, price, old, rating=4.5, reviews=500, sponsored=False):
    return deals.Product(asin, title, price, old, rating, reviews, None, sponsored)


def test_eligible_filters():
    ch = channel(min_discount=30, min_rating=4.0, min_reviews=100, max_price=100)
    assert deals.eligible(prod("A", "ok", 50, 100), ch)
    assert not deals.eligible(prod("A", "poco dto", 80, 100), ch)
    assert not deals.eligible(prod("A", "mala nota", 50, 100, rating=3.5), ch)
    assert not deals.eligible(prod("A", "pocas val", 50, 100, reviews=10), ch)
    assert not deals.eligible(prod("A", "sin nota", 50, 100, rating=None), ch)
    assert not deals.eligible(prod("A", "caro", 150, 300), ch)
    assert not deals.eligible(prod("A", "spons", 50, 100, sponsored=True), ch)


def test_pick_orders_and_dedupes():
    ch = channel(min_discount=20, max_posts=2)
    items = [
        prod("A1", "Zapatillas running hombre", 50, 100),
        prod("A2", "Robot aspirador inteligente", 100, 300),
        prod("A3", "Robot aspirador inteligente nuevo", 100, 300),  # parecido a A2
        prod("A4", "Lampara de mesa", 20, 30),
    ]
    chosen = deals.pick(items, ch, {})
    assert [p.asin for p in chosen] == ["A2", "A1"]


def test_pick_skips_recent_and_similar():
    ch = channel(max_posts=5)
    state = {"A1": {"t": "2026-01-01T00:00:00+00:00", "title": "Zapatillas running hombre"}}
    items = [
        prod("A1", "Zapatillas running hombre", 50, 100),
        prod("A9", "Zapatillas running hombre talla 42", 50, 100),
        prod("B1", "Cafetera de goteo", 30, 60),
    ]
    assert [p.asin for p in deals.pick(items, ch, state)] == ["B1"]


def test_state_prune_and_roundtrip(tmp_path):
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    path = tmp_path / "s" / "t.json"
    state = {
        "NEW": {"t": (now - timedelta(days=1)).isoformat(), "title": "a"},
        "OLD": {"t": (now - timedelta(days=30)).isoformat(), "title": "b"},
        "BAD": {"t": "no-es-fecha"},
    }
    deals.save_state(path, state, next_at=now)
    assert not path.with_suffix(".tmp").exists()
    assert set(deals.load_state(path, 14, now)) == {"NEW"}
    assert deals.load_next_at(path) == now
    assert deals.load_state(tmp_path / "no_existe.json", 14, now) == {}
    assert deals.load_next_at(tmp_path / "no_existe.json") is None


def test_state_legacy_flat_format(tmp_path):
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"B000000001": {"t": (now - timedelta(days=1)).isoformat(), "title": "a"}}))
    assert set(deals.load_state(path, 14, now)) == {"B000000001"}
    assert deals.load_next_at(path) is None


# --------------------------------------------------------------- mensaje

def test_fmt_eur():
    assert deals.fmt_eur(1299.99) == "1.299,99€"
    assert deals.fmt_eur(29.5) == "29,50€"
    assert deals.fmt_eur(20) == "20€"
    assert deals.fmt_eur(23.0) == "23€"


def test_affiliate_url_and_image():
    assert deals.affiliate_url("B000000001", "abc-21") == "https://www.amazon.es/dp/B000000001?tag=abc-21"
    assert deals.affiliate_url("B000000001", None) == "https://www.amazon.es/dp/B000000001"
    assert deals.full_size_image("https://m.media-amazon.com/images/I/71abc._AC_UY218_.jpg") == \
        "https://m.media-amazon.com/images/I/71abc.jpg"
    assert deals.full_size_image(None) is None


def test_caption_escapes_html():
    p = prod("A", "Cable <b>USB</b> & más " + "x" * 200, 10, 20)
    caption = deals.build_caption(p, "https://www.amazon.es/dp/A?tag=t-21", channel())
    assert "<b>USB</b>" not in caption
    assert "&lt;b&gt;USB" in caption
    assert "−50 %" in caption and "Ahora: 10€" in caption
    assert "afiliado" not in caption  # sin pie por defecto
    assert len(caption) < 1024


def test_caption_layout():
    p = deals.Product("B000000001", "Aspiradora X", 52.90, 81.61, 4.5, 1234, None)
    caption = deals.build_caption(p, "https://www.amazon.es/dp/B000000001?tag=t-21", channel())
    assert caption.split("\n") == [
        "🔥 <b>Aspiradora X</b>",
        "",
        "PVPR: <s>81,61€</s>",
        "Ahora: 52,90€",
        "−35 % · Diferencia 28,71€",
        "",
        "⭐ 4,5/5 · 1.234 valoraciones",
        "",
        '👉 <a href="https://www.amazon.es/dp/B000000001?tag=t-21">Ver en Amazon</a>',
    ]


def test_caption_footer_optional():
    p = deals.Product("B000000001", "X", 10.0, 20.0)
    assert "Aviso" in deals.build_caption(p, "https://www.amazon.es/dp/B000000001", channel(footer="Aviso"))


def test_caption_without_old_price():
    caption = deals.build_caption(deals.Product("B000000001", "X", 23.0, None), "https://x/", channel())
    assert "Ahora: 23€" in caption and "PVPR" not in caption and "Diferencia" not in caption


# -------------------------------------------------------------- secretos

def test_scrub_hides_token_everywhere():
    assert TOKEN not in deals.scrub(f"POST /bot{TOKEN}/sendMessage failed")
    deals.register_secret("mi-secreto-largo")
    assert "mi-secreto-largo" not in deals.scrub("clave mi-secreto-largo en texto")


def test_log_scrubs_output(capfd):
    deals.setup_logging()
    deals.log.error("fallo en https://api.telegram.org/bot%s/sendPhoto", TOKEN)
    deals.log.info("%s: publicada %s (-%d%%)", "t", "B000000001", 50)
    out = capfd.readouterr().out
    assert TOKEN not in out and "***" in out
    assert "(-50%)" in out


def test_get_secret_from_all_secrets(monkeypatch):
    monkeypatch.delenv("TG_X", raising=False)
    monkeypatch.setenv("ALL_SECRETS", json.dumps({"TG_X": "valor-secreto-1"}))
    assert deals.get_secret("TG_X") == "valor-secreto-1"
    assert deals.get_secret("NO_EXISTE") is None
    monkeypatch.setenv("ALL_SECRETS", "null")
    assert deals.get_secret("TG_X") is None
    monkeypatch.setenv("ALL_SECRETS", "{roto")
    assert deals.get_secret("TG_X") is None


def test_telegram_error_never_contains_token(monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(deals.requests, "post", boom)
    with pytest.raises(deals.TelegramError) as info:
        deals.tg_call("sendMessage", TOKEN, {})
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}
        self.ok = status < 400

    def json(self):
        return self._body


def test_send_falls_back_to_text(monkeypatch):
    calls = []

    def fake_post(url, data=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        calls.append(method)
        return FakeResp(400, {"description": "wrong file identifier"}) if method == "sendPhoto" else FakeResp()

    monkeypatch.setattr(deals.requests, "post", fake_post)
    deals.send_deal(TOKEN, "1", "hola", "https://x/img.jpg")
    assert calls == ["sendPhoto", "sendMessage"]


def test_telegram_retries_on_429(monkeypatch):
    seq = [FakeResp(429, {"parameters": {"retry_after": 1}}), FakeResp()]
    monkeypatch.setattr(deals.requests, "post", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    deals.tg_call("sendMessage", TOKEN, {})
    assert not seq


# ------------------------------------------------------------ configuracion

def write_cfg(tmp_path, **kw):
    cfg = {"name": "c", "token_env": "A", "chat_env": "B", "queries": ["x"]}
    cfg.update(kw)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cfg))
    return path


def test_config_validation(tmp_path):
    assert deals.load_channel(write_cfg(tmp_path)).name == "c"
    with pytest.raises(deals.ConfigError, match="desconocidas"):
        deals.load_channel(write_cfg(tmp_path, min_descuento=3))
    with pytest.raises(deals.ConfigError, match="queries"):
        deals.load_channel(write_cfg(tmp_path, queries=[]))
    with pytest.raises(deals.ConfigError, match="urls deben"):
        deals.load_channel(write_cfg(tmp_path, urls=["https://evil.example/s?k=x"]))
    with pytest.raises(deals.ConfigError, match="name"):
        deals.load_channel(write_cfg(tmp_path, name="../x"))
    with pytest.raises(deals.ConfigError, match="&"):
        deals.load_channel(write_cfg(tmp_path, search_params="rh=1"))


def test_search_urls():
    ch = channel(queries=["robot aspirador"], pages=2, search_params="&rh=p_8%3A20-",
                 urls=[deals.BASE + "/s?k=a"])
    urls = deals.search_urls(ch)
    assert urls[0] == "https://www.amazon.es/s?k=robot+aspirador&page=1&rh=p_8%3A20-"
    assert urls[2] == "https://www.amazon.es/s?k=a"
    assert urls[3] == "https://www.amazon.es/s?k=a&page=2"


# ---------------------------------------------------------------- ejecucion

HTML = page(
    card("B000000001", "Auriculares inalambricos", "39,99 €", "79,99 €"),
    card("B000000002", "Cafetera de goteo", "20,00 €", "22,00 €"),
)
NOW_IN = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)  # 13:00 en Madrid


def test_dry_run_prints_and_keeps_state_clean(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AMAZON_TAG", "demo-21")
    f = tmp_path / "p.html"
    f.write_text(HTML, encoding="utf-8")
    n = deals.run_channel(channel(), html_file=f, state_dir=tmp_path / "state")
    out = capsys.readouterr().out
    assert n == 1
    assert "Auriculares inalambricos" in out and "tag=demo-21" in out
    assert not (tmp_path / "state").exists()


def test_publish_saves_state_and_does_not_repeat(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv("TG_T", TOKEN)
    monkeypatch.setenv("TG_C", "@canal")
    monkeypatch.setattr(deals, "collect", lambda ch, html_file=None: deals.parse_results(HTML))
    monkeypatch.setattr(deals, "send_deal", lambda token, chat, caption, image: sent.append((chat, image)))
    monkeypatch.setattr(deals, "sleep", lambda s: None)

    assert deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN) == 1
    assert sent == [("@canal", "https://m.media-amazon.com/images/I/71abcDEF.jpg")]
    saved = json.loads((tmp_path / "t.json").read_text())
    assert list(saved["posted"]) == ["B000000001"]
    gap = datetime.fromisoformat(saved["next_at"]) - NOW_IN
    assert timedelta(minutes=30) <= gap <= timedelta(minutes=35)

    # a los 10 min aun no toca; a los 36 toca, pero ya no hay nada nuevo que cumpla
    assert deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(minutes=10)) == 0
    assert deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(minutes=36)) == 0
    assert len(sent) == 1


def test_missing_secrets_raise(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_T", raising=False)
    monkeypatch.delenv("TG_C", raising=False)
    monkeypatch.delenv("ALL_SECRETS", raising=False)
    with pytest.raises(deals.ConfigError):
        deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN)


def test_main_dry_run_with_html(tmp_path, monkeypatch, capsys):
    f = tmp_path / "p.html"
    f.write_text(HTML, encoding="utf-8")
    monkeypatch.setattr(deals, "ROOT", tmp_path)
    (tmp_path / "channels").mkdir()
    (tmp_path / "channels" / "t.json").write_text(
        json.dumps({"name": "t", "token_env": "A", "chat_env": "B", "queries": ["x"]})
    )
    assert deals.main(["--channel", "t", "--dry-run", "--html", str(f)]) == 0
    assert "Auriculares inalambricos" in capsys.readouterr().out


# ------------------------------------------------------- horario e intervalo

def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_hhmm():
    assert deals.hhmm("08:00") == 480 and deals.hhmm("23:00") == 1380 and deals.hhmm("8:05") == 485
    for bad in ("25:00", "08:60", "8", "ocho", ""):
        with pytest.raises(ValueError):
            deals.hhmm(bad)


def test_in_window_follows_madrid_time_incl_dst():
    ch = channel()
    # invierno (UTC+1): 08:00 Madrid = 07:00 UTC
    assert not deals.in_window(utc(2026, 11, 3, 6, 59), ch)
    assert deals.in_window(utc(2026, 11, 3, 7, 0), ch)
    assert deals.in_window(utc(2026, 11, 3, 22, 0), ch)       # 23:00 en punto entra
    assert not deals.in_window(utc(2026, 11, 3, 22, 1), ch)
    # verano (UTC+2): 08:00 Madrid = 06:00 UTC
    assert not deals.in_window(utc(2026, 7, 1, 5, 59), ch)
    assert deals.in_window(utc(2026, 7, 1, 6, 0), ch)
    assert not deals.in_window(utc(2026, 7, 1, 21, 1), ch)


def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no deberia consultar Amazon")
    monkeypatch.setattr(deals, "collect", boom)


def setup_publish(monkeypatch, html=HTML):
    sent = []
    monkeypatch.setenv("TG_T", TOKEN)
    monkeypatch.setenv("TG_C", "@canal")
    monkeypatch.setattr(deals, "collect", lambda ch, html_file=None: deals.parse_results(html))
    monkeypatch.setattr(deals, "send_deal", lambda *a: sent.append(a))
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    return sent


def test_outside_window_does_nothing(tmp_path, monkeypatch):
    no_network(monkeypatch)
    night = utc(2026, 11, 3, 3, 0)  # 04:00 en Madrid
    assert deals.run_channel(channel(), state_dir=tmp_path, now=night) == 0
    assert not (tmp_path / "t.json").exists()


def test_force_ignores_window_and_interval(tmp_path, monkeypatch):
    sent = setup_publish(monkeypatch)
    night = utc(2026, 11, 3, 3, 0)
    assert deals.run_channel(channel(), state_dir=tmp_path, now=night, force=True) == 1
    assert len(sent) == 1


def test_no_candidates_waits_before_retrying(tmp_path, monkeypatch):
    setup_publish(monkeypatch, html=page(card("B000000002", "Cafetera", "20,00 €", "22,00 €")))
    assert deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN) == 0
    saved = json.loads((tmp_path / "t.json").read_text())
    assert datetime.fromisoformat(saved["next_at"]) == NOW_IN + timedelta(minutes=10)


@pytest.mark.parametrize("exc", [deals.Blocked("captcha"), deals.LocationError("sin ES")])
def test_block_or_location_failure_publishes_nothing_and_backs_off(tmp_path, monkeypatch, exc):
    sent = setup_publish(monkeypatch)

    def fail(ch, html_file=None):
        raise exc

    monkeypatch.setattr(deals, "collect", fail)
    with pytest.raises(type(exc)):
        deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN)
    assert sent == []
    saved = json.loads((tmp_path / "t.json").read_text())
    assert datetime.fromisoformat(saved["next_at"]) == NOW_IN + timedelta(minutes=10)


def test_main_reports_location_error_without_failing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deals, "ROOT", tmp_path)
    (tmp_path / "channels").mkdir()
    (tmp_path / "channels" / "t.json").write_text(
        json.dumps({"name": "t", "token_env": "A", "chat_env": "B", "queries": ["x"]})
    )

    def fail(ch, **kw):
        raise deals.LocationError("no se pudo")

    monkeypatch.setattr(deals, "run_channel", fail)
    assert deals.main(["--all"]) == 0
    assert "::warning::" in capsys.readouterr().out


def test_force_run_env_var(tmp_path, monkeypatch):
    monkeypatch.setattr(deals, "ROOT", tmp_path)
    (tmp_path / "channels").mkdir()
    (tmp_path / "channels" / "t.json").write_text(
        json.dumps({"name": "t", "token_env": "A", "chat_env": "B", "queries": ["x"]})
    )
    seen = {}
    monkeypatch.setattr(deals, "run_channel", lambda ch, **kw: seen.update(kw))
    monkeypatch.setenv("FORCE_RUN", "1")
    deals.main(["--all"])
    assert seen["force"] is True
    monkeypatch.setenv("FORCE_RUN", "0")
    deals.main(["--all"])
    assert seen["force"] is False


def test_config_validation_new_fields(tmp_path):
    for bad, msg in [
        ({"postal_code": "2800"}, "postal_code"),
        ({"timezone": "Marte/Olimpo"}, "zona horaria"),
        ({"window_start": "9am"}, "HH:MM"),
        ({"window_start": "23:00", "window_end": "08:00"}, "anterior"),
        ({"interval_min": 40, "interval_max": 30}, "intervalo"),
        ({"retry_minutes": 0}, "retry_minutes"),
    ]:
        with pytest.raises(deals.ConfigError, match=msg):
            deals.load_channel(write_cfg(tmp_path, **bad))
    ch = deals.load_channel(write_cfg(tmp_path))
    assert (ch.postal_code, ch.window_start, ch.window_end, ch.interval_min, ch.interval_max) == \
        ("28001", "08:00", "23:00", 30, 35)
    assert ch.footer == ""


# ------------------------------------------------- ubicacion (precios con IVA)

class Resp:
    def __init__(self, text="", body=None, status=200):
        self.text, self._body, self.status_code = text, body, status
        self.ok = status < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._body is None:
            raise ValueError("sin json")
        return self._body


MODAL = json.dumps({
    "ajaxHeaders": {"anti-csrftoken-a2z": "tok-pagina"},
    "url": "/portal-migration/hz/glow/get-location-label?storeContext=generic&pageType=Search",
})


def amazon_page(location, *cards):
    return (
        "<html><body>"
        f"<a id='nav-global-location-data-modal-action' data-a-modal='{MODAL}'></a>"
        f"<span id='glow-ingress-line2'>{location}</span>"
        + "".join(cards) + "</body></html>"
    )


class FakeAmazon:
    """Simula una sesion de Amazon: empieza fuera de España y cambia al recibir el codigo postal."""

    def __init__(self, accepts=True, sticks=True):
        self.accepts, self.sticks, self.located = accepts, sticks, False
        self.calls, self.headers = [], {}

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url.split("?")[0]))
        if "get-location-label" in url:
            return Resp(text='{"CSRF_TOKEN":"tok-ventana"}')
        where = "Madrid 28001" if self.located else "Estados Unidos"
        return Resp(text=amazon_page(where, card("B000000001", "Auriculares", "39,99 €", "79,99 €")))

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url.split("?")[0], json, headers))
        if self.accepts and self.sticks:
            self.located = True
        return Resp(body={"isAddressUpdated": 1 if self.accepts else 0, "address": {"countryCode": "ES"}})


def test_location_ok():
    assert deals.location_ok(amazon_page("Madrid 28001"), "28001")
    assert not deals.location_ok(amazon_page("Estados Unidos"), "28001")
    assert not deals.location_ok("<html></html>", "28001")


def test_collect_sets_spanish_location_before_reading_prices(monkeypatch):
    fake = FakeAmazon()
    monkeypatch.setattr(deals, "make_session", lambda: fake)
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    products = deals.collect(channel())
    assert [p.asin for p in products] == ["B000000001"]
    assert [c[0] for c in fake.calls] == ["GET", "GET", "POST", "GET"]
    _, url, payload, headers = fake.calls[2]
    assert url.endswith("/portal-migration/hz/glow/address-change")
    assert payload["zipCode"] == "28001" and payload["locationType"] == "LOCATION_INPUT"
    assert payload["pageType"] == "Search"
    assert headers["anti-csrftoken-a2z"] == "tok-ventana"


def test_collect_skips_location_when_already_spanish(monkeypatch):
    fake = FakeAmazon()
    fake.located = True
    monkeypatch.setattr(deals, "make_session", lambda: fake)
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    deals.collect(channel())
    assert [c[0] for c in fake.calls] == ["GET"]


def test_collect_respects_require_spain_false(monkeypatch):
    fake = FakeAmazon()
    monkeypatch.setattr(deals, "make_session", lambda: fake)
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    deals.collect(channel(require_spain=False))
    assert [c[0] for c in fake.calls] == ["GET"]


def test_collect_refuses_if_amazon_rejects_postal_code(monkeypatch):
    monkeypatch.setattr(deals, "make_session", lambda: FakeAmazon(accepts=False))
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    with pytest.raises(deals.LocationError):
        deals.collect(channel())


def test_collect_refuses_if_page_still_not_spanish(monkeypatch):
    monkeypatch.setattr(deals, "make_session", lambda: FakeAmazon(sticks=False))
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    with pytest.raises(deals.LocationError, match="sigue sin mostrar"):
        deals.collect(channel())


def test_set_location_needs_modal(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    with pytest.raises(deals.LocationError, match="selector"):
        deals.set_location(FakeAmazon(), "https://www.amazon.es/s?k=x", "<html></html>", "28001")


def test_follow_refresh(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)

    class S:
        def get(self, url, headers=None, timeout=None):
            return Resp(text="<html>" + "x" * 30000 + "</html>")

    short = '<html><meta http-equiv="refresh" content="5; URL=/s?k=a&amp;page=1"></html>'
    assert len(deals.follow_refresh(S(), short, "https://www.amazon.es/s?k=a")) > 30000
    assert deals.follow_refresh(S(), "<html>normal</html>", "x") == "<html>normal</html>"
