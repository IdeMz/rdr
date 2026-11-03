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
    deals.save_state(path, state)
    assert not path.with_suffix(".tmp").exists()
    assert set(deals.load_state(path, 14, now)) == {"NEW"}
    assert deals.load_state(tmp_path / "no_existe.json", 14, now) == {}


# --------------------------------------------------------------- mensaje

def test_fmt_eur():
    assert deals.fmt_eur(1299.99) == "1.299,99 €"
    assert deals.fmt_eur(29.5) == "29,50 €"
    assert deals.fmt_eur(20) == "20 €"


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
    assert "-50%" in caption and "10 €" in caption and "Enlace de afiliado" in caption
    assert len(caption) < 1024


# -------------------------------------------------------------- secretos

def test_scrub_hides_token_everywhere():
    assert TOKEN not in deals.scrub(f"POST /bot{TOKEN}/sendMessage failed")
    deals.register_secret("mi-secreto-largo")
    assert "mi-secreto-largo" not in deals.scrub("clave mi-secreto-largo en texto")


def test_log_filter_scrubs(capfd):
    deals.setup_logging()
    deals.log.error("fallo en https://api.telegram.org/bot%s/sendPhoto", TOKEN)
    out = capfd.readouterr().out
    assert TOKEN not in out and "***" in out


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

    assert deals.run_channel(channel(), state_dir=tmp_path) == 1
    assert sent == [("@canal", "https://m.media-amazon.com/images/I/71abcDEF.jpg")]
    saved = json.loads((tmp_path / "t.json").read_text())
    assert list(saved) == ["B000000001"]

    assert deals.run_channel(channel(), state_dir=tmp_path) == 0
    assert len(sent) == 1


def test_missing_secrets_raise(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_T", raising=False)
    monkeypatch.delenv("TG_C", raising=False)
    monkeypatch.delenv("ALL_SECRETS", raising=False)
    with pytest.raises(deals.ConfigError):
        deals.run_channel(channel(), state_dir=tmp_path)


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
