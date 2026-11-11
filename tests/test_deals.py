import io
import json
from pathlib import Path
import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests
from PIL import Image

import deals
import pricehist

TOKEN ="123456789:AAH_fake-token_abcdefghijklmnopqrstuvwxyz0123"


@pytest.fixture(autouse=True)
def restore_logging():
    """main() y setup_logging() cambian el logging global: lo dejamos como estaba."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def png_bytes(size=(300, 600), color=(200, 30, 30), mode="RGB"):
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, "PNG")
    return out.getvalue()


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
    base = dict(name="t", token_env="TG_T", chat_env="TG_C", queries=["x"], min_listing_days=0, min_history_drop=0)
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


FUNNEL_ITEMS = [
    prod("A1", "Zapatillas running hombre", 50, 100),                     # pasa todo
    prod("A2", "Robot aspirador inteligente", 100, 300, reviews=10),      # pocas valoraciones
    prod("A3", "Cafetera de goteo", 20, 22),                              # solo 9 % de descuento
    prod("A4", "Lampara de mesa", 20, None),                              # sin precio anterior
    prod("A5", "Anuncio", 10, 50, sponsored=True),                        # patrocinado
    prod("A6", "Sin precio", None, None),
    prod("A7", "Altavoz portatil", 30, 60, rating=3.5),                   # mala nota
    prod("A8", "Bicicleta estatica", 400, 800),                           # por encima del precio maximo
]


def test_funnel_counts_each_filter():
    ch = channel(min_discount=10, min_rating=4.0, min_reviews=100, max_price=200)
    assert deals.funnel(FUNNEL_ITEMS, ch, {}) == (
        "8 leidos -> 7 con precio -> 6 no patrocinados -> 5 con precio anterior"
        " -> 4 con >=10% dto -> 3 en rango de precio -> 2 con nota >=4.0"
        " -> 1 con >=100 valoraciones -> 1 nuevos"
    )


def test_funnel_counts_already_posted_as_not_new():
    ch = channel(min_discount=10, min_rating=4.0, min_reviews=100, max_price=200)
    state = {"A1": {"t": "2026-11-03T12:00:00+00:00", "title": "Zapatillas running hombre"}}
    assert deals.funnel(FUNNEL_ITEMS, ch, state).endswith("-> 1 con >=100 valoraciones -> 0 nuevos")


def test_funnel_and_eligible_agree():
    ch = channel(min_discount=10, min_rating=4.0, min_reviews=100, max_price=200)
    survivors = [p for p in FUNNEL_ITEMS if deals.eligible(p, ch)]
    assert [p.asin for p in survivors] == ["A1"]
    assert deals.funnel(FUNNEL_ITEMS, ch, {}).split(" -> ")[-2].startswith(f"{len(survivors)} ")


def test_funnel_with_zero_min_discount_skips_old_price_step():
    text = deals.funnel(FUNNEL_ITEMS, channel(min_discount=0), {})
    assert "precio anterior" not in text


def test_run_channel_logs_funnel(tmp_path, monkeypatch, caplog):
    setup_publish(monkeypatch)
    with caplog.at_level(logging.INFO):
        deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN)
    assert any("leidos ->" in r.getMessage() and "nuevos" in r.getMessage() for r in caplog.records)


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


@pytest.mark.parametrize("a,b,expected", [
    ("Philips Avent Biberon Natural 260ml Azul", "Philips Avent Biberon Natural 260ml Rosa", True),
    ("Smileat Tarrito Ecologico Guisito de Alubias 220 g", "Smileat Tarrito Ecologico Guisito de Alubias 220 g Pack 2", True),
    ("Auriculares Bluetooth Sony WH-1000XM5 Negro", "Auriculares Bluetooth Sony WH-1000XM5 Plata", True),
    ("Lego Technic Ferrari 42143", "Lego Technic Porsche 42176", False),
    ("Robot aspirador Roborock Q Revo", "Cafetera de goteo Philips 1000W", False),
    ("Camiseta", "Pantalon", False),
])
def test_similar_detects_color_variants(a, b, expected):
    assert deals.similar(a, b, 0.5) is expected


def test_pick_skips_color_variant_of_recent_post():
    ch = channel()
    state = {"B0BTJ8ZXG5": {"t": "2026-11-03T12:00:00+00:00",
                             "title": "Philips Avent Biberon Natural 260ml Azul"}}
    items = [prod("B0BTJ9WHL9", "Philips Avent Biberon Natural 260ml Rosa", 10, 30),
             prod("B0OTRO0001", "Cafetera de goteo Philips 1000W", 20, 60)]
    assert [p.asin for p in deals.pick(items, ch, state)] == ["B0OTRO0001"]


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


def square_of(data):
    with Image.open(io.BytesIO(data)) as im:
        return im.format, im.size, im.convert("RGB")


@pytest.mark.parametrize("size", [(300, 600), (900, 300), (50, 50), (4000, 2500)])
def test_to_square_jpeg_always_1200(size):
    fmt, dims, im = square_of(deals.to_square_jpeg(png_bytes(size)))
    assert fmt == "JPEG" and dims == (1200, 1200)
    assert im.getpixel((0, 0)) == (255, 255, 255) and im.getpixel((1199, 1199)) == (255, 255, 255)
    r, g, b = im.getpixel((600, 600))
    assert r > 150 and g < 90 and b < 90  # el producto queda centrado


def test_to_square_jpeg_keeps_aspect_ratio_and_margin():
    _, _, im = square_of(deals.to_square_jpeg(png_bytes((300, 600))))
    # alto util 1200-2*40 = 1120 -> ancho 560; el producto no toca los bordes
    assert im.getpixel((600, 30)) == (255, 255, 255)
    assert im.getpixel((600, 60))[1] < 90
    assert im.getpixel((600 - 290, 600)) == (255, 255, 255)
    assert im.getpixel((600 - 270, 600))[1] < 90


def test_to_square_jpeg_transparent_becomes_white():
    _, _, im = square_of(deals.to_square_jpeg(png_bytes((200, 200), (0, 0, 0, 0), "RGBA")))
    assert im.getpixel((600, 600)) == (255, 255, 255)


def test_to_square_jpeg_rejects_garbage():
    with pytest.raises(OSError):
        deals.to_square_jpeg(b"no soy una imagen")


class ImgResp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("x")


def test_prepare_image(monkeypatch):
    url = "https://m.media-amazon.com/images/I/71abc.jpg"
    monkeypatch.setattr(deals.requests, "get", lambda u, **k: ImgResp(png_bytes((640, 480))))
    out = deals.prepare_image(url)
    assert square_of(out)[1] == (1200, 1200)


@pytest.mark.parametrize("url", [
    None, "", "http://m.media-amazon.com/x.jpg", "https://evil.example/x.jpg",
    "https://media-amazon.com.evil.example/x.jpg", "file:///etc/passwd",
])
def test_prepare_image_rejects_foreign_urls(monkeypatch, url):
    def boom(*a, **k):
        raise AssertionError("no deberia descargar")
    monkeypatch.setattr(deals.requests, "get", boom)
    assert deals.prepare_image(url) is None


def test_prepare_image_failures_return_none(monkeypatch):
    url = "https://m.media-amazon.com/images/I/71abc.jpg"
    monkeypatch.setattr(deals.requests, "get", lambda u, **k: ImgResp(b"", 404))
    assert deals.prepare_image(url) is None
    monkeypatch.setattr(deals.requests, "get", lambda u, **k: ImgResp(b"basura"))
    assert deals.prepare_image(url) is None
    monkeypatch.setattr(deals.requests, "get", lambda u, **k: ImgResp(b"x" * (deals.MAX_IMAGE_BYTES + 1)))
    assert deals.prepare_image(url) is None

    def net(u, **k):
        raise requests.ConnectionError("caido")

    monkeypatch.setattr(deals.requests, "get", net)
    assert deals.prepare_image(url) is None


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
        "",
        "🌐 AMAZON",
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


def test_send_falls_back_from_file_to_url_to_text(monkeypatch):
    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        calls.append((method, bool(files), (data or {}).get("photo")))
        return FakeResp(400, {"description": "wrong file identifier"}) if method == "sendPhoto" else FakeResp()

    monkeypatch.setattr(deals.requests, "post", fake_post)
    deals.send_deal(TOKEN, "1", "hola", b"JPEG", "https://m.media-amazon.com/images/I/x.jpg")
    assert calls == [
        ("sendPhoto", True, None),
        ("sendPhoto", False, "https://m.media-amazon.com/images/I/x.jpg"),
        ("sendMessage", False, None),
    ]


def test_send_uploads_square_photo_as_file(monkeypatch):
    seen = {}

    def fake_post(url, data=None, files=None, timeout=None):
        seen.update(url=url, data=data, files=files, timeout=timeout)
        return FakeResp()

    monkeypatch.setattr(deals.requests, "post", fake_post)
    deals.send_deal(TOKEN, "@c", "pie", b"JPEGDATA", "https://m.media-amazon.com/x.jpg")
    assert seen["url"].endswith("/sendPhoto")
    assert seen["files"]["photo"] == ("oferta.jpg", b"JPEGDATA", "image/jpeg")
    assert seen["data"]["caption"] == "pie" and "photo" not in seen["data"]


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
    monkeypatch.setattr(deals, "collect", lambda ch, html_file=None, session=None: deals.parse_results(HTML))
    monkeypatch.setattr(deals, "prepare_image", lambda url: b"JPEG:" + url.encode())
    monkeypatch.setattr(deals, "send_deal", lambda token, chat, caption, photo, url, markup=None: sent.append((chat, photo, url)))
    monkeypatch.setattr(deals, "sleep", lambda s: None)

    assert deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN) == 1
    url = "https://m.media-amazon.com/images/I/71abcDEF.jpg"
    assert sent == [("@canal", b"JPEG:" + url.encode(), url)]
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
    monkeypatch.setattr(deals, "collect", lambda ch, html_file=None, session=None: deals.parse_results(html))
    monkeypatch.setattr(deals, "prepare_image", lambda url: b"JPEG")
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

    def fail(ch, html_file=None, session=None):
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
        self.headers = {"content-type": "application/json" if body is not None else "text/html"}

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


# ---------------------------------------------------------------- antiguedad del producto

def product_page(extra):
    return f"<html><body><div id='detail'>{extra}</div></body></html>"


TODAY = datetime(2026, 11, 3, tzinfo=timezone.utc).date()


@pytest.mark.parametrize("text,expected", [
    ("12 de octubre de 2026", (2026, 10, 12)),
    ("3 noviembre 2025", (2025, 11, 3)),
    ("1 de Setiembre de 2024", (2024, 9, 1)),
    ("31 de febrero de 2025", None),
    ("sin fecha", None),
    ("12 de foo de 2026", None),
])
def test_parse_spanish_date(text, expected):
    got = deals.parse_spanish_date(text)
    assert (got and (got.year, got.month, got.day)) == (expected or None)


def test_listing_age_from_label():
    html = product_page("<span>Producto en Amazon.es desde</span><span>25 de octubre de 2026</span>")
    assert deals.listing_age_days(html, TODAY) == 9


def test_listing_age_from_new_badge():
    html = product_page("<span>Nuevo en Amazon en el último mes</span>")
    assert deals.listing_age_days(html, TODAY) == deals.NEW_BADGE_AGE_DAYS


def test_listing_age_unknown():
    assert deals.listing_age_days(product_page("nada"), TODAY) is None


def test_listing_ok_rejects_recent_and_accepts_old_or_unknown(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    ch = channel(min_listing_days=60)
    pages = {
        "B1": product_page("Nuevo en Amazon en el último mes"),
        "B2": product_page("Producto en Amazon.es desde 1 de enero de 2024"),
        "B3": product_page("sin datos"),
    }
    monkeypatch.setattr(deals, "fetch", lambda session, url: pages[url.rsplit("/", 1)[1]])

    class P:
        def __init__(self, asin):
            self.asin = asin

    assert deals.listing_ok(None, P("B1"), ch, TODAY)[0] is False
    assert deals.listing_ok(None, P("B2"), ch, TODAY)[0] is True
    assert deals.listing_ok(None, P("B3"), ch, TODAY)[0] is True


def test_max_discount_filter():
    ch = channel(min_discount=10, max_discount=70, min_rating=0, min_reviews=0)
    ok = deals.parse_results(page(card("B000000010", "Algo bueno", "50,00 €", "100,00 €")))[0]
    fake = deals.parse_results(page(card("B000000011", "Algo raro", "20,00 €", "100,00 €")))[0]
    assert deals.eligible(ok, ch)
    assert not deals.eligible(fake, ch)


def test_run_channel_skips_recent_listing_and_remembers_it(tmp_path, monkeypatch):
    html = page(
        card("B000000020", "Auriculares nuevos falsos", "24,99 €", "99,99 €", reviews="5.000"),
        card("B000000021", "Cafetera antigua buena", "40,00 €", "60,00 €", reviews="900"),
    )
    sent = setup_publish(monkeypatch, html=html)
    # fuerza la comprobacion de fichas aunque collect este simulado
    monkeypatch.setattr(deals, "make_session", lambda: object())
    pages = {
        "B000000020": product_page("Nuevo en Amazon en el último mes"),
        "B000000021": product_page("Producto en Amazon.es desde 5 de mayo de 2022"),
    }
    monkeypatch.setattr(deals, "fetch", lambda session, url: pages[url.rsplit("/", 1)[1]])
    ch = channel(min_listing_days=60, min_discount=10, min_rating=0, min_reviews=0)
    assert deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN) == 1
    assert "Cafetera antigua buena" in sent[0][2]
    assert "B000000021" in sent[0][5]["inline_keyboard"][-1][0]["url"]
    saved = json.loads((tmp_path / "t.json").read_text())
    assert "B000000020" in saved["rejected"]
    assert "B000000021" in saved["posted"]


def test_rejected_expire(tmp_path):
    path = tmp_path / "t.json"
    old = (NOW_IN - timedelta(days=deals.REJECT_DAYS + 1)).isoformat()
    new = (NOW_IN - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({"next_at": None, "posted": {},
                                "rejected": {"A": {"t": old}, "B": {"t": new}}}))
    assert set(deals.load_rejected(path, NOW_IN)) == {"B"}


def test_collect_retries_location_when_first_post_is_not_json(monkeypatch):
    class Flaky(FakeAmazon):
        def post(self, url, json=None, headers=None, timeout=None):
            if len([c for c in self.calls if c[0] == "POST"]) == 0:
                self.calls.append(("POST", url.split("?")[0], json, headers))
                return Resp(text="<html><body>Robot Check</body></html>")
            return super().post(url, json=json, headers=headers, timeout=timeout)

    fake = Flaky()
    monkeypatch.setattr(deals, "make_session", lambda: fake)
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    assert [p.asin for p in deals.collect(channel())] == ["B000000001"]
    assert len([c for c in fake.calls if c[0] == "POST"]) == 2


def test_set_location_non_json_error_is_descriptive(monkeypatch):
    class Html(FakeAmazon):
        def post(self, url, json=None, headers=None, timeout=None):
            return Resp(text="<html><body>Robot Check</body></html>")

    monkeypatch.setattr(deals, "sleep", lambda s: None)
    fake = Html()
    with pytest.raises(deals.LocationError, match="no JSON.*Robot Check"):
        deals.set_location(fake, "https://www.amazon.es/s?k=x", fake.get("https://www.amazon.es/s?k=x").text, "28001")


def test_collect_gives_up_after_three_location_attempts(monkeypatch):
    fake = FakeAmazon(accepts=False)
    monkeypatch.setattr(deals, "make_session", lambda: fake)
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    with pytest.raises(deals.LocationError):
        deals.collect(channel())
    assert len([c for c in fake.calls if c[0] == "POST"]) == deals.LOCATION_ATTEMPTS


def test_date_hints_show_label_context():
    html = product_page("<span>Fecha de lanzamiento</span> <span>5 de mayo de 2022</span> y 3 de foo de 2020")
    hints = deals.date_hints(html)
    assert len(hints) == 1 and "Fecha de lanzamiento" in hints[0]
    assert deals.date_hints(product_page("nada")) == []


def test_explain_and_report(tmp_path):
    html = page(
        card("B000000030", "Barato sin dto", "10,00 €"),
        card("B000000031", "Cafetera Delonghi negra", "50,00 €", "100,00 €"),
        card("B000000032", "Cafetera Delonghi blanca", "50,00 €", "100,00 €"),
        card("B000000033", "Taladro percutor", "40,00 €", "80,00 €"),
        card("B000000034", "Aspirador viejo", "30,00 €", "60,00 €"),
    )
    ch = channel(min_discount=10, min_rating=0, min_reviews=0)
    products = deals.parse_results(html)
    state = {"B000000032": {"t": NOW_IN.isoformat(), "title": "Cafetera Delonghi blanca"}}
    skip = {"B000000034": {"t": NOW_IN.isoformat(), "why": "alta hace 3 dias"}}
    chosen = deals.pick(products, ch, state, skip=set(skip))
    why = {p.asin: r for p, r in deals.explain(products, ch, state, skip=skip, chosen=chosen)}
    assert why["B000000030"].startswith("no cumple")
    assert why["B000000032"] == "ya publicado"
    assert why["B000000031"] == "parecido a otro ya publicado"
    assert why["B000000034"] == "descartado por la ficha: alta hace 3 dias"
    assert why["B000000033"] == "elegido"
    out = tmp_path / "reports" / "t.csv"
    deals.write_report(out, deals.explain(products, ch, state, skip=skip, chosen=chosen))
    text = out.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("asin;categoria;motivo") and "B000000033" in text


# ---------------------------------------------------------------- historial de precios de la ficha

HISTORIAL = (
    "<div>En los últimos 30 días, el precio de los <a>Auriculares Inalámbricos Drsaec J55</a> "
    "ha variado entre 20,57 € y 23,99 €. El precio actual es 23,99 €, el más alto en los últimos 30 días.</div>"
)


def test_price_history_parsing():
    assert deals.price_history(product_page(HISTORIAL)) == (20.57, 23.99, 30)
    assert deals.price_history(product_page("nada")) is None


def fake_product(asin="B000000040", price=23.99):
    return deals.Product(asin=asin, title="Auriculares", price=price, old_price=49.99, rating=4.3, reviews=25920)


def test_listing_ok_rejects_discount_contradicted_by_history(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page(HISTORIAL))
    ch = channel(min_listing_days=0, min_history_drop=10)
    ok, why = deals.listing_ok(None, fake_product(), ch, TODAY)
    assert not ok and "20,57€" in why and "23,99€" in why and "0%" in why


def test_listing_ok_accepts_real_drop_and_missing_history(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    ch = channel(min_listing_days=0, min_history_drop=10)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page(HISTORIAL))
    assert deals.listing_ok(None, fake_product(price=15.0), ch, TODAY)[0] is True  # -37 % sobre 23,99
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page("sin historial"))
    assert deals.listing_ok(None, fake_product(), ch, TODAY)[0] is True


def test_min_history_drop_zero_skips_check(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page(HISTORIAL))
    ch = channel(min_listing_days=0, min_history_drop=0)
    assert deals.listing_ok(None, fake_product(), ch, TODAY)[0] is True


def test_load_channel_validates_min_history_drop(tmp_path):
    with pytest.raises(deals.ConfigError, match="min_history_drop"):
        deals.load_channel(write_cfg(tmp_path, min_history_drop=95))


# ---------------------------------------------------------------- precio de referencia de la ficha

# textos tal como salen en las fichas guardadas de Amazon.es
REF_FICHA = (
    "Historial de precios -5 % El precio más bajo de los últimos 30 días: 39,99€ 39,99€ "
    "Es el precio más bajo del producto en Amazon.es ... Precio mediano: 39,99 € Precio mediano: 39,99€ 39,99€"
)
REF_AURICULARES = "El precio más bajo de los últimos 30 días: 19,49€ 19,49€"


def test_price_reference_parses_low30_and_median():
    assert deals.price_reference(product_page(REF_FICHA)) == {"low30": 39.99, "median": 39.99}
    assert deals.price_reference(product_page(REF_AURICULARES)) == {"low30": 19.49}
    assert deals.price_reference(product_page("100+ comprados el mes pasado 19,29 € Historial de precios")) == {}


def test_price_check_rejects_price_at_top_of_its_range(monkeypatch):
    """El caso de los auriculares: el precio acaba de subir a 26 EUR y el minimo de 30 dias es 19,49."""
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page(REF_AURICULARES))
    ch = channel(price_check="soft")
    ok, why = deals.listing_ok(None, fake_product(price=25.99), ch, TODAY)
    assert not ok and why.startswith(deals.REF_WHY) and "19,49€" in why and "33%" in why


def test_price_check_accepts_price_near_or_below_reference(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page(REF_AURICULARES))
    ch = channel(price_check="soft", price_tolerance=3)
    assert deals.listing_ok(None, fake_product(price=19.49), ch, TODAY)[0] is True
    assert deals.listing_ok(None, fake_product(price=19.99), ch, TODAY)[0] is True   # +2,6 %: dentro
    assert deals.listing_ok(None, fake_product(price=20.29), ch, TODAY)[0] is False  # +4,1 %: fuera
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page(REF_FICHA))
    assert deals.listing_ok(None, fake_product(price=37.99), channel(price_check="soft"), TODAY)[0] is True
    assert deals.listing_ok(None, fake_product(price=45.0), channel(price_check="soft"), TODAY)[0] is False


def test_price_check_modes_without_reference(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page("sin datos de precio"))
    assert deals.listing_ok(None, fake_product(), channel(price_check="soft"), TODAY)[0] is True
    ok, why = deals.listing_ok(None, fake_product(), channel(price_check="strict"), TODAY)
    assert not ok and why.startswith(deals.REF_WHY)
    assert deals.listing_ok(None, fake_product(price=99), channel(price_check="off"), TODAY)[0] is True


def test_price_check_off_does_not_even_download_the_page():
    ch = channel(price_check="off")
    assert not (ch.min_listing_days > 0 or ch.min_history_drop > 0 or ch.price_check != "off")


def test_price_rejections_are_forgotten_sooner(tmp_path):
    path = tmp_path / "s.json"
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    why = f"{deals.REF_WHY}: minimo de 30 dias 19,49€"
    deals.save_state(path, {}, next_at=None, rejected={
        "PRECIO_VIEJO": {"t": (now - timedelta(hours=25)).isoformat(), "why": why},
        "PRECIO_RECIENTE": {"t": (now - timedelta(hours=3)).isoformat(), "why": why},
        "OTRO": {"t": (now - timedelta(hours=25)).isoformat(), "why": "alta hace 5 dias"},
    })
    assert set(deals.load_rejected(path, now)) == {"PRECIO_RECIENTE", "OTRO"}


def test_load_channel_validates_price_check(tmp_path):
    assert deals.load_channel(write_cfg(tmp_path, price_check="strict")).price_check == "strict"
    with pytest.raises(deals.ConfigError, match="price_check"):
        deals.load_channel(write_cfg(tmp_path, price_check="mucho"))
    with pytest.raises(deals.ConfigError, match="price_tolerance"):
        deals.load_channel(write_cfg(tmp_path, price_tolerance=80))


# ---------------------------------------------------------------- historial propio de precios

def own_ref(low=347.78, median=379.0, ago=4):
    return {"low30": low, "median": median, "seen": 9, "span": 20, "low_ago": ago}


def test_own_reference_why():
    assert deals.own_reference_why(None, 379.0, 3) == ""
    assert deals.own_reference_why(own_ref(), 347.78, 3) == ""
    assert deals.own_reference_why(own_ref(low=370.0), 379.0, 3) == ""            # +2,4 %: dentro de la tolerancia
    why = deals.own_reference_why(own_ref(), 379.0, 3)
    assert why.startswith(deals.REF_WHY) and "historial propio" in why
    assert "347,78€" in why and "379€" in why and "9%" in why and "hace 4 dias" in why


def test_listing_ok_rejects_with_own_history_without_downloading_the_page(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no deberia descargar la ficha")
    monkeypatch.setattr(deals, "fetch", boom)
    ok, why = deals.listing_ok(None, fake_product(price=379.0), channel(price_check="strict"), TODAY, own=own_ref())
    assert not ok and "historial propio" in why


def test_strict_accepts_without_page_reference_when_own_history_backs_it(monkeypatch, caplog):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page("sin datos de precio"))
    strict = channel(price_check="strict")
    with caplog.at_level(logging.INFO, logger="deals"):
        assert deals.listing_ok(None, fake_product(price=347.78), strict, TODAY, own=own_ref())[0] is True
    assert "lo respalda el historial propio" in caplog.text
    ok, why = deals.listing_ok(None, fake_product(price=347.78), strict, TODAY)  # sin historial propio: como antes
    assert not ok and why.startswith(deals.REF_WHY)


def test_page_reference_still_applies_when_own_history_exists(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page(REF_AURICULARES))
    ok, why = deals.listing_ok(None, fake_product(price=25.99), channel(price_check="strict"), TODAY,
                               own=own_ref(low=25.99, median=25.99))
    assert not ok and "19,49€" in why and "historial propio" not in why


def test_own_history_is_ignored_when_price_check_is_off(monkeypatch):
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page("nada"))
    assert deals.listing_ok(None, fake_product(price=379.0), channel(price_check="off"), TODAY, own=own_ref())[0] is True


def test_load_channel_validates_own_history(tmp_path):
    assert deals.load_channel(write_cfg(tmp_path)).own_history is True
    assert deals.load_channel(write_cfg(tmp_path, own_history=False)).own_history is False
    with pytest.raises(deals.ConfigError, match="own_history"):
        deals.load_channel(write_cfg(tmp_path, own_history="si"))


# ---------------------------------------------------------------- busqueda en el departamento Ofertas

def test_deals_urls():
    assert deals.deals_urls(channel(queries=["a b"], deals_search=False)) == []
    ch = channel(queries=["robot aspirador"], pages=2, search_params="&x=1", deals_search=True,
                 urls=[deals.BASE + "/s?k=tv", deals.BASE + "/s?k=tv&i=electronics",
                       deals.BASE + "/s?k=tv&rh=p_8%3A20-", deals.BASE + "/b?node=123"])
    assert deals.deals_urls(ch) == [
        "https://www.amazon.es/s?k=robot+aspirador&page=1&x=1&i=todays-deals",
        "https://www.amazon.es/s?k=robot+aspirador&page=2&x=1&i=todays-deals",
        "https://www.amazon.es/s?k=tv&i=todays-deals",
        "https://www.amazon.es/s?k=tv&i=todays-deals&page=2",
    ]  # sin tocar las direcciones que ya llevan departamento, filtros o no son una busqueda


def fake_amazon(monkeypatch, normal, extra, calls):
    def fetch(session, url, retries=3):
        calls.append((url, retries))
        out = extra if "todays-deals" in url else normal
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(deals, "fetch", fetch)
    monkeypatch.setattr(deals, "sleep", lambda s: None)


def test_collect_adds_products_from_the_deals_department(monkeypatch, caplog):
    calls = []
    normal = page(card("B000000060", "Movil normal", "100,00 €", "150,00 €"),
                  card("B000000061", "Movil repetido", "90,00 €", "120,00 €"))
    extra = page(card("B000000061", "Movil repetido", "90,00 €", "120,00 €"),
                 card("B000000062", "Movil en oferta del dia", "80,00 €", "160,00 €"))
    fake_amazon(monkeypatch, normal, extra, calls)
    ch = channel(queries=["movil"], deals_search=True, require_spain=False)
    with caplog.at_level(logging.INFO, logger="deals"):
        found = deals.collect(ch, session=object())
    assert [p.asin for p in found] == ["B000000060", "B000000061", "B000000062"]   # sin duplicados
    assert calls[0][1] == 3 and calls[1][1] == 1                                   # el extra, un solo intento
    assert "busqueda 2 de 2 (ofertas)" in caplog.text


def test_collect_keeps_going_when_the_deals_page_fails(monkeypatch, caplog):
    calls = []
    normal = page(card("B000000060", "Movil normal", "100,00 €", "150,00 €"))
    fake_amazon(monkeypatch, normal, deals.Blocked("HTTP 503"), calls)
    with caplog.at_level(logging.INFO, logger="deals"):
        found = deals.collect(channel(queries=["movil"], deals_search=True, require_spain=False), session=object())
    assert [p.asin for p in found] == ["B000000060"]
    assert "la busqueda de ofertas no respondio" in caplog.text


def test_collect_empty_deals_page_is_not_a_block(monkeypatch):
    calls = []
    normal = page(card("B000000060", "Movil normal", "100,00 €", "150,00 €"))
    fake_amazon(monkeypatch, normal, page(), calls)
    found = deals.collect(channel(queries=["movil"], deals_search=True, require_spain=False), session=object())
    assert [p.asin for p in found] == ["B000000060"]


def test_collect_still_stops_on_a_captcha_in_the_deals_page(monkeypatch):
    calls = []
    normal = page(card("B000000060", "Movil normal", "100,00 €", "150,00 €"))
    fake_amazon(monkeypatch, normal, "<html>Introduce los caracteres que ves</html>", calls)
    with pytest.raises(deals.Blocked):
        deals.collect(channel(queries=["movil"], deals_search=True, require_spain=False), session=object())


def test_collect_without_deals_search_makes_no_extra_request(monkeypatch):
    calls = []
    fake_amazon(monkeypatch, page(card("B000000060", "Movil normal", "100,00 €", "150,00 €")), page(), calls)
    deals.collect(channel(queries=["movil"], require_spain=False), session=object())
    assert len(calls) == 1 and "todays-deals" not in calls[0][0]


def test_deals_search_per_category_and_validation(tmp_path):
    ch = channel(deals_search=True, categories=[{"nombre": "A", "query": "a"}, {"nombre": "B", "query": "b", "deals_search": False}])
    assert deals.category_channel(ch, ch.categories[0]).deals_search is True
    assert deals.category_channel(ch, ch.categories[1]).deals_search is False
    assert deals.load_channel(write_cfg(tmp_path, deals_search=True)).deals_search is True
    with pytest.raises(deals.ConfigError, match="deals_search"):
        deals.load_channel(write_cfg(tmp_path, deals_search="si"))


def seed_history(path, series_by_asin, today):
    """Escribe un historial como si el bot hubiera visto cada producto a lo largo de los dias anteriores."""
    hist = {"p": {}}
    for asin, prices in series_by_asin.items():
        for back, price in prices:  # (dias atras, precio)
            pricehist.record(hist, {asin: price}, today - timedelta(days=back))
    pricehist.save(path, hist)


OWN_PAGE = page(
    card("B000000050", "Smart TV LG 43 pulgadas QNED", "379,00 €", "600,00 €"),     # -37 %, pero en su maximo
    card("B000000051", "Aspirador Dyson sin cable potente", "347,78 €", "449,00 €"),  # -22 %, en su minimo
)


def test_run_channel_uses_own_history_to_skip_overpriced_and_verify_the_rest(tmp_path, monkeypatch, caplog):
    sent = setup_publish(monkeypatch, html=OWN_PAGE)
    pages = []
    monkeypatch.setattr(deals, "fetch", lambda session, url: pages.append(url) or product_page("sin datos de precio"))
    today = NOW_IN.date()
    hist_path = tmp_path / "precios_amazon.json"
    seed_history(hist_path, {
        "B000000050": [(12, 379.0), (9, 379.0), (6, 347.78), (3, 379.0), (1, 379.0)],
        "B000000051": [(12, 347.78), (9, 347.78), (6, 347.78), (3, 347.78), (1, 347.78)],
    }, today)
    ch = channel(price_check="strict", min_discount=10, min_rating=0, min_reviews=0)
    with caplog.at_level(logging.INFO, logger="deals"):
        assert deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN) == 1
    assert list(json.loads((tmp_path / "t.json").read_text())["posted"]) == ["B000000051"]
    assert len(pages) == 1 and "B000000051" in pages[0]          # la ficha del LG ni se descarga
    assert "1 candidatos descartados por precio alto" in caplog.text
    assert "lo respalda el historial propio" in caplog.text
    # y los precios de hoy quedan anotados
    saved = pricehist.load(hist_path)["p"]
    assert saved["B000000050"][0] == pricehist.day_no(today) and saved["B000000050"][1] == 6


def test_run_channel_without_history_falls_back_to_page_check(tmp_path, monkeypatch):
    sent = setup_publish(monkeypatch, html=OWN_PAGE)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page("sin datos de precio"))
    ch = channel(price_check="strict", min_discount=10, min_rating=0, min_reviews=0)
    assert deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN) == 0   # nada verificable: strict no publica
    assert sent == []
    assert set(json.loads((tmp_path / "t.json").read_text())["rejected"]) == {"B000000050", "B000000051"}
    assert pricehist.load(tmp_path / "precios_amazon.json")["p"].keys() == {"B000000050", "B000000051"}


def test_own_history_failure_never_blocks_publishing(tmp_path, monkeypatch, caplog):
    sent = setup_publish(monkeypatch, html=OWN_PAGE)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page("sin datos de precio"))

    def boom(*a, **k):
        raise OSError("disco lleno")
    monkeypatch.setattr(pricehist, "save", boom)
    ch = channel(price_check="soft", min_discount=10, min_rating=0, min_reviews=0)
    with caplog.at_level(logging.INFO, logger="deals"):
        assert deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN) == 1
    assert "historial propio: OSError" in caplog.text


def test_dry_run_reads_but_never_writes_the_history(tmp_path, monkeypatch):
    setup_publish(monkeypatch, html=OWN_PAGE)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page("sin datos de precio"))
    ch = channel(price_check="soft", min_discount=10, min_rating=0, min_reviews=0)
    deals.run_channel(ch, dry_run=True, state_dir=tmp_path, now=NOW_IN)
    assert not (tmp_path / "precios_amazon.json").exists()


def test_own_history_off_keeps_old_behaviour(tmp_path, monkeypatch):
    setup_publish(monkeypatch, html=OWN_PAGE)
    monkeypatch.setattr(deals, "fetch", lambda session, url: product_page("sin datos de precio"))
    ch = channel(price_check="soft", own_history=False, min_discount=10, min_rating=0, min_reviews=0)
    assert deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN) == 1
    assert not (tmp_path / "precios_amazon.json").exists()


def test_explain_labels_own_history_rejections():
    products = deals.parse_results(OWN_PAGE)
    ch = channel(min_discount=10, min_rating=0, min_reviews=0)
    skip = {"B000000050": {"t": NOW_IN.isoformat(), "why": "referencia: historial propio, minimo 347,78€"}}
    why = {p.asin: r for p, r in deals.explain(products, ch, {}, skip=skip)}
    assert why["B000000050"].startswith("descartado por el historial propio: ")


# ---------------------------------------------------------------- categorias en rotacion

CATS = [
    {"nombre": "Auriculares", "emoji": "🎧", "url": "/s?k=auriculares"},
    {"nombre": "Perfumes", "query": "perfume", "max_discount": 40},
    {"nombre": "Lego", "query": "lego"},
]


def test_load_channel_with_categories_only(tmp_path):
    ch = deals.load_channel(write_cfg(tmp_path, queries=[], categories=CATS))
    assert [c["nombre"] for c in ch.categories] == ["Auriculares", "Perfumes", "Lego"]


@pytest.mark.parametrize("cats,msg", [
    ([{"url": "/s?k=x"}], "nombre"),
    ([{"nombre": "A", "query": "x"}, {"nombre": "A", "query": "y"}], "repetida"),
    ([{"nombre": "A"}], "'url' o 'query'"),
    ([{"nombre": "A", "url": "/s?k=x", "query": "x"}], "'url' o 'query'"),
    ([{"nombre": "A", "query": "x", "color": "rojo"}], "claves desconocidas"),
    ([{"nombre": "A", "url": "https://otra.com/s?k=x"}], "url debe empezar"),
    ([{"nombre": "A", "query": "x", "min_discount": 50, "max_discount": 40}], "max_discount"),
])
def test_load_channel_validates_categories(tmp_path, cats, msg):
    with pytest.raises(deals.ConfigError, match=msg):
        deals.load_channel(write_cfg(tmp_path, queries=[], categories=cats))


def test_category_channel_applies_overrides_and_target():
    ch = channel(min_discount=10, max_discount=55)
    perf = deals.category_channel(ch, CATS[1])
    assert perf.max_discount == 40 and perf.min_discount == 10
    assert perf.queries == ["perfume"] and perf.urls == [] and perf.categories == []
    aur = deals.category_channel(ch, CATS[0])
    assert aur.urls == ["https://www.amazon.es/s?k=auriculares"] and aur.queries == []


@pytest.fixture
def no_shuffle(monkeypatch):
    monkeypatch.setattr(deals.random, "shuffle", lambda x: None)


def test_category_order_is_random_and_covers_all():
    ch = channel(categories=[{"nombre": f"C{i}", "query": f"q{i}"} for i in range(40)])
    orders = {tuple(c["nombre"] for c in deals.category_order(ch, {})) for _ in range(5)}
    assert len(orders) > 1  # no sale siempre en el orden del archivo
    assert all(len(o) == 40 and len(set(o)) == 40 for o in orders)


def test_category_order_never_visited_come_first():
    ch = channel(categories=[{"nombre": f"C{i}", "query": f"q{i}"} for i in range(10)])
    visited = {"C0": "2026-11-03T10:00:00+00:00", "C1": "2026-11-03T11:00:00+00:00"}
    for _ in range(20):
        order = [c["nombre"] for c in deals.category_order(ch, visited)]
        assert set(order[:8]) == {f"C{i}" for i in range(2, 10)}
        assert set(order[8:]) == {"C0", "C1"}


def test_category_order_puts_recently_visited_last():
    ch = channel(categories=[{"nombre": f"C{i}", "query": f"q{i}"} for i in range(10)])
    # C0..C4 visitadas, de la mas antigua (C0) a la mas reciente (C4)
    visited = {f"C{i}": f"2026-11-03T1{i}:00:00+00:00" for i in range(5)}
    for _ in range(20):
        order = [c["nombre"] for c in deals.category_order(ch, visited)]
        assert len(order) == 10 and set(order) == {f"C{i}" for i in range(10)}
        # la mitad mas reciente de las visitadas (todas, aqui: 5 visitadas, tope 10//2=5) va al final
        assert set(order[-5:]) == {f"C{i}" for i in range(5)}
        assert set(order[:5]) == {f"C{i}" for i in range(5, 10)}


def test_category_order_old_visits_return_to_pool():
    ch = channel(categories=[{"nombre": f"C{i}", "query": f"q{i}"} for i in range(10)])
    visited = {f"C{i}": f"2026-11-03T{10 + i}:00:00+00:00" for i in range(10)}  # todas visitadas
    for _ in range(20):
        order = [c["nombre"] for c in deals.category_order(ch, visited)]
        assert set(order[-5:]) == {f"C{i}" for i in range(5, 10)}  # las 5 mas recientes, al final
        assert set(order[:5]) == {f"C{i}" for i in range(5)}


def test_run_channel_rotates_categories_and_skips_empty_ones(tmp_path, monkeypatch, no_shuffle):
    pages = {
        "Auriculares": page(card("B000000050", "Auriculares malos", "20,00 €", "21,00 €")),  # sin descuento
        "Perfumes": page(card("B000000051", "Perfume Eau", "30,00 €", "40,00 €")),
        "Lego": page(card("B000000052", "Lego Ciudad", "30,00 €", "60,00 €")),
    }
    seen = []

    def fake_collect(ch, html_file=None, session=None):
        key = "Auriculares" if ch.urls else {"perfume": "Perfumes", "lego": "Lego"}[ch.queries[0]]
        seen.append(key)
        return deals.parse_results(pages[key])

    sent = setup_publish(monkeypatch)
    monkeypatch.setattr(deals, "collect", fake_collect)
    ch = channel(categories=CATS, min_discount=10, min_rating=0, min_reviews=0)

    # 1.a ejecucion: Auriculares no tiene candidatos, publica el de Perfumes y se detiene
    assert deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN) == 1
    assert seen == ["Auriculares", "Perfumes"]
    saved = json.loads((tmp_path / "t.json").read_text())
    assert set(saved["cats"]) == {"Auriculares", "Perfumes"} and "B000000051" in saved["posted"]

    # 2.a ejecucion (cuando toca): empieza por la que nunca se visito
    seen.clear()
    later = NOW_IN + timedelta(hours=1)
    assert deals.run_channel(ch, state_dir=tmp_path, now=later) == 1
    assert seen == ["Lego"]


def test_category_max_discount_override_blocks_inflated_pvpr(tmp_path, monkeypatch):
    html = page(card("B000000060", "Perfume Eau", "20,00 €", "100,00 €"))  # -80 %
    sent = setup_publish(monkeypatch, html=html)
    ch = channel(categories=[CATS[1]], min_discount=10, min_rating=0, min_reviews=0)
    assert deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN) == 0
    assert sent == []


def test_save_state_keeps_categories_when_not_passed(tmp_path):
    path = tmp_path / "t.json"
    deals.save_state(path, {}, cats={"Lego": "2026-11-03T10:00:00+00:00"})
    deals.save_state(path, {}, next_at=NOW_IN)
    assert deals.load_cats(path) == {"Lego": "2026-11-03T10:00:00+00:00"}


def test_run_note_names_category_and_asin(tmp_path, monkeypatch):
    monkeypatch.setattr(deals, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    html = page(card("B000000070", "Taladro percutor", "40,00 €", "80,00 €"))
    setup_publish(monkeypatch, html=html)
    ch = channel(categories=[{"nombre": "Herramientas", "query": "taladro"}],
                 min_discount=10, min_rating=0, min_reviews=0)
    assert deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN) == 1
    assert (tmp_path / "run_note.txt").read_text().strip() == "t: publicada B000000070 (Herramientas, -50%)"
    assert "B000000070" in (tmp_path / "summary.md").read_text()


def test_run_note_when_no_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(deals, "ROOT", tmp_path)
    setup_publish(monkeypatch, html=page(card("B000000071", "Cafetera", "20,00 €", "22,00 €")))
    assert deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN) == 0
    assert (tmp_path / "run_note.txt").read_text().strip() == "t: sin candidatos"


# ---------------------------------------------------------------- avisos privados

@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setenv("TG_T", TOKEN)
    monkeypatch.setenv("TG_ALERT_CHAT", "777")
    monkeypatch.setattr(deals, "tg_call", lambda method, token, data, **kw: sent.append((method, data)))
    return sent


def test_alert_after_three_failures_then_throttled_then_recovery(tmp_path, alerts):
    ch = channel()
    path = tmp_path / "t.json"
    for i in range(2):
        deals.record_problem(ch, path, "fail", "Amazon no responde", NOW_IN + timedelta(minutes=10 * i))
    assert alerts == []
    deals.record_problem(ch, path, "fail", "Amazon no responde", NOW_IN + timedelta(minutes=20))
    assert len(alerts) == 1 and alerts[0][1]["chat_id"] == "777" and "3 fallos seguidos" in alerts[0][1]["text"]
    deals.record_problem(ch, path, "fail", "otra vez", NOW_IN + timedelta(minutes=30))  # dentro de 3 h: no repite
    assert len(alerts) == 1
    deals.record_problem(ch, path, "fail", "otra vez", NOW_IN + timedelta(hours=4))  # pasado el plazo: repite
    assert len(alerts) == 2
    deals.record_success(ch, path)
    assert len(alerts) == 3 and "Vuelve a publicar" in alerts[2][1]["text"]
    assert deals.load_problem(path) == {}


def test_no_recovery_message_if_never_alerted(tmp_path, alerts):
    ch = channel()
    path = tmp_path / "t.json"
    deals.record_problem(ch, path, "fail", "x", NOW_IN)
    deals.record_success(ch, path)
    assert alerts == []


def test_empty_attempts_alert_later_than_failures(tmp_path, alerts):
    ch = channel()
    path = tmp_path / "t.json"
    for i in range(5):
        deals.record_problem(ch, path, "empty", "sin candidatos", NOW_IN + timedelta(minutes=10 * i))
    assert alerts == []
    deals.record_problem(ch, path, "empty", "sin candidatos", NOW_IN + timedelta(minutes=50))
    assert len(alerts) == 1 and "sin ningun producto" in alerts[0][1]["text"]


def test_no_alert_without_alert_chat_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_T", TOKEN)
    monkeypatch.delenv("TG_ALERT_CHAT", raising=False)
    calls = []
    monkeypatch.setattr(deals, "tg_call", lambda *a, **k: calls.append(a))
    for i in range(4):
        deals.record_problem(channel(), tmp_path / "t.json", "fail", "x", NOW_IN + timedelta(minutes=i))
    assert calls == []


def test_alert_failure_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_T", TOKEN)
    monkeypatch.setenv("TG_ALERT_CHAT", "777")

    def boom(*a, **k):
        raise deals.TelegramError("sendMessage: HTTP 400")

    monkeypatch.setattr(deals, "tg_call", boom)
    assert deals.send_alert(channel(), "hola") is False


def test_run_channel_counts_block_and_publish_resets(tmp_path, monkeypatch, alerts):
    sent = setup_publish(monkeypatch)
    monkeypatch.setenv("TG_T", TOKEN)

    def blocked(ch, html_file=None, session=None):
        raise deals.Blocked("captcha")

    good = deals.collect
    monkeypatch.setattr(deals, "collect", blocked)
    for i in range(3):
        with pytest.raises(deals.Blocked):
            deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(minutes=10 * i))
    assert len(alerts) == 1 and "captcha" in alerts[0][1]["text"]
    monkeypatch.setattr(deals, "collect", lambda ch, html_file=None, session=None: deals.parse_results(HTML))
    assert deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(hours=1)) == 1
    assert len(alerts) == 2 and "Vuelve a publicar" in alerts[1][1]["text"]


def test_force_runs_do_not_count_as_failures(tmp_path, monkeypatch, alerts):
    setup_publish(monkeypatch)

    def blocked(ch, html_file=None, session=None):
        raise deals.Blocked("captcha")

    monkeypatch.setattr(deals, "collect", blocked)
    for _ in range(4):
        with pytest.raises(deals.Blocked):
            deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN, force=True)
    assert alerts == [] and deals.load_problem(tmp_path / "t.json") == {}


def test_caption_header_with_category_emoji():
    p = deals.Product("B000000001", "Auriculares X", 23.0, 40.0)
    ch = channel()
    cap = deals.build_caption(p, "https://x/", ch, {"nombre": "Auriculares", "emoji": "🎧"})
    lines = cap.splitlines()
    assert lines[0] == "<b>🎧 Auriculares</b>" and lines[1] == "" and lines[2].startswith("🔥 <b>Auriculares X")
    assert deals.build_caption(p, "https://x/", ch, {"nombre": "Lego"}).splitlines()[0] == "<b>Lego</b>"
    assert deals.build_caption(p, "https://x/", ch).startswith("🔥")


def test_run_channel_uses_category_header(tmp_path, monkeypatch):
    html = page(card("B000000080", "Taladro percutor", "40,00 €", "80,00 €"))
    sent = setup_publish(monkeypatch, html=html)
    ch = channel(categories=[{"nombre": "Herramientas", "emoji": "🔧", "query": "taladro"}],
                 min_discount=10, min_rating=0, min_reviews=0)
    deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN)
    assert sent[0][2].startswith("<b>🔧 Herramientas</b>\n\n🔥")


# ---------------------------------------------------------------- resumen diario

def posted(asin, hour, title, price=20.0, disc=30, cat="Lego", emoji="🧱", day=(2026, 11, 3)):
    t = datetime(*day, hour, 0, tzinfo=timezone.utc).isoformat()
    return asin, {"t": t, "title": title, "p": price, "d": disc, "c": cat, "e": emoji}


def summary_setup(monkeypatch, tmp_path, entries, ch=None):
    sent = []
    monkeypatch.setenv("TG_T", TOKEN)
    monkeypatch.setenv("TG_C", "@canal")
    monkeypatch.setenv("AMAZON_TAG", "tag-21")
    monkeypatch.setattr(deals, "tg_call", lambda method, token, data, **kw: sent.append((method, data)))
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    ch = ch or channel(summary_time="22:50")
    path = tmp_path / "t.json"
    deals.save_state(path, dict(entries))
    return ch, path, sent


def test_summary_sent_once_after_summary_time(tmp_path, monkeypatch):
    entries = [posted("B000000001", 8, "Lego Ciudad"), posted("B000000002", 11, "Auriculares & más", 23.99, 52, "Auriculares", "🎧")]
    ch, path, sent = summary_setup(monkeypatch, tmp_path, entries)
    state = deals.load_state(path, 14, utc(2026, 11, 3, 21, 50))
    assert deals.maybe_send_summary(ch, path, state, utc(2026, 11, 3, 21, 49)) is False  # 22:49 en Madrid
    assert sent == []
    assert deals.maybe_send_summary(ch, path, state, utc(2026, 11, 3, 21, 50)) is True  # 22:50
    assert len(sent) == 1 and sent[0][0] == "sendMessage"
    text = sent[0][1]["text"]
    assert "Resumen del día · martes 3 nov" in text and "2 ofertas publicadas" in text
    assert "1. 🧱" in text and "2. 🎧" in text and "23,99€ (−52 %)" in text
    assert "https://www.amazon.es/dp/B000000002?tag=tag-21" in text and "Auriculares &amp; más" in text
    assert sent[0][1]["disable_web_page_preview"] == "true" and sent[0][1]["chat_id"] == "@canal"
    # una sola vez al dia
    assert deals.maybe_send_summary(ch, path, state, utc(2026, 11, 3, 21, 55)) is False
    assert len(sent) == 1 and json.loads(path.read_text())["summary_day"] == "2026-11-03"


def test_summary_only_counts_todays_posts(tmp_path, monkeypatch):
    entries = [posted("B000000001", 9, "De ayer", day=(2026, 11, 2)), posted("B000000002", 9, "De hoy")]
    ch, path, sent = summary_setup(monkeypatch, tmp_path, entries)
    state = deals.load_state(path, 14, utc(2026, 11, 3, 22, 0))
    deals.maybe_send_summary(ch, path, state, utc(2026, 11, 3, 22, 0))
    assert "De hoy" in sent[0][1]["text"] and "De ayer" not in sent[0][1]["text"]
    assert "1 oferta publicada" in sent[0][1]["text"]


def test_summary_skipped_when_no_posts_but_day_is_marked(tmp_path, monkeypatch):
    ch, path, sent = summary_setup(monkeypatch, tmp_path, [])
    assert deals.maybe_send_summary(ch, path, {}, utc(2026, 11, 3, 22, 0)) is False
    assert sent == [] and json.loads(path.read_text())["summary_day"] == "2026-11-03"


def test_summary_retries_if_telegram_fails(tmp_path, monkeypatch):
    ch, path, sent = summary_setup(monkeypatch, tmp_path, [posted("B000000001", 9, "Lego")])

    def boom(*a, **k):
        raise deals.TelegramError("sendMessage: HTTP 500")

    monkeypatch.setattr(deals, "tg_call", boom)
    state = deals.load_state(path, 14, utc(2026, 11, 3, 22, 0))
    assert deals.maybe_send_summary(ch, path, state, utc(2026, 11, 3, 22, 0)) is False
    assert "summary_day" not in json.loads(path.read_text()) or json.loads(path.read_text())["summary_day"] is None


def test_summary_splits_long_lists():
    entries = [posted(f"B{i:09d}", 8 + i % 14, "Producto con un titulo bastante largo para ocupar sitio " * 2, 19.99, 30)
               for i in range(60)]
    texts = deals.summary_messages(entries, datetime(2026, 11, 3).date(), "t-21")
    assert len(texts) > 1 and all(len(t) <= 4096 for t in texts)
    assert "(1/" in texts[0] and f"({len(texts)}/{len(texts)})" in texts[-1]
    assert sum(t.count("\n") > 0 for t in texts) == len(texts)
    assert "60 ofertas publicadas" in texts[0]


def test_summary_runs_before_window_gate_and_state_survives(tmp_path, monkeypatch):
    entries = [posted("B000000001", 9, "Lego")]
    ch, path, sent = summary_setup(monkeypatch, tmp_path, entries)
    monkeypatch.setattr(deals, "ROOT", tmp_path)
    late = utc(2026, 11, 3, 21, 52)  # 22:52 Madrid: dentro de horario, pero aun no toca otra oferta
    deals.save_state(path, dict(entries), next_at=late + timedelta(minutes=20))
    assert deals.run_channel(ch, state_dir=tmp_path, now=late) == 0
    assert len(sent) == 1 and "Resumen del día" in sent[0][1]["text"]
    data = json.loads(path.read_text())
    assert data["summary_day"] == "2026-11-03" and "B000000001" in data["posted"]
    deals.save_state(path, deals.load_state(path, 14, late), next_at=late)  # un save_state posterior lo conserva
    assert json.loads(path.read_text())["summary_day"] == "2026-11-03"


def test_published_entry_stores_price_discount_and_category(tmp_path, monkeypatch):
    html = page(card("B000000090", "Taladro percutor", "40,00 €", "80,00 €"))
    setup_publish(monkeypatch, html=html)
    ch = channel(categories=[{"nombre": "Herramientas", "emoji": "🔧", "query": "taladro"}],
                 min_discount=10, min_rating=0, min_reviews=0)
    deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN)
    e = json.loads((tmp_path / "t.json").read_text())["posted"]["B000000090"]
    assert (e["p"], e["d"], e["c"], e["e"]) == (40.0, 50, "Herramientas", "🔧")


def test_load_channel_validates_summary_time(tmp_path):
    with pytest.raises(deals.ConfigError, match="summary_time"):
        deals.load_channel(write_cfg(tmp_path, summary_time="tarde"))
    assert deals.load_channel(write_cfg(tmp_path, summary_time="22:50")).summary_time == "22:50"


# ---------------------------------------------------------------- tienda, botones y votos

def test_caption_keeps_text_link_and_adds_store_line_at_the_end():
    p = deals.Product("B000000001", "X", 10.0, 20.0)
    cap = deals.build_caption(p, "https://www.amazon.es/dp/B000000001", channel())
    assert cap.splitlines()[-2:] == ["", "🌐 AMAZON"] and ">Ver en Amazon</a>" in cap
    assert "🌐 ALIEXPRESS" in deals.build_caption(p, "u", channel(store="ALIEXPRESS"))
    assert "🌐" not in deals.build_caption(p, "u", channel(store=""))


def test_build_keyboard_layout():
    ch = channel(vote_button=True, website_url="https://www.concriterio.eu/", website_label="")
    kb = deals.build_keyboard(ch, "B000000001", "tag-21", likes=3)["inline_keyboard"]
    assert kb[0] == [
        {"text": "👍 3", "callback_data": "like"},
        {"text": "www.concriterio.eu", "url": "https://www.concriterio.eu/"},
    ]
    assert kb[1] == [{"text": "VER EN AMAZON", "url": "https://www.amazon.es/dp/B000000001?tag=tag-21"}]
    only = deals.build_keyboard(channel(), "B000000001", None)["inline_keyboard"]
    assert len(only) == 1 and only[0][0]["text"] == "VER EN AMAZON"
    assert deals.build_keyboard(channel(website_url="https://x.es", website_label="Mi web"), "A", None)[
        "inline_keyboard"][0] == [{"text": "Mi web", "url": "https://x.es"}]


def test_send_deal_attaches_markup_and_returns_message_id(monkeypatch):
    calls = []

    def fake(method, token, data, **kw):
        calls.append((method, data))
        return {"ok": True, "result": {"message_id": 42}}

    monkeypatch.setattr(deals, "tg_call", fake)
    markup = {"inline_keyboard": [[{"text": "x", "url": "https://x.es"}]]}
    assert deals.send_deal(TOKEN, "@c", "pie", b"JPEG", None, markup) == 42
    assert json.loads(calls[0][1]["reply_markup"]) == markup
    # sin foto: sendMessage con los mismos botones
    calls.clear()
    assert deals.send_deal(TOKEN, "@c", "pie", None, None, markup) == 42
    assert calls[0][0] == "sendMessage" and "reply_markup" in calls[0][1]


def test_website_url_must_be_https(tmp_path):
    with pytest.raises(deals.ConfigError, match="website_url"):
        deals.load_channel(write_cfg(tmp_path, website_url="http://x.es"))


def update(uid, user, data="like", msg_id=42, chat_id=-100123, username="canal", cq_id="cq1"):
    return {"update_id": uid, "callback_query": {
        "id": cq_id, "data": data, "from": {"id": user},
        "message": {"message_id": msg_id, "chat": {"id": chat_id, "username": username}},
    }}


def votes_setup(monkeypatch, tmp_path, updates):
    calls = []
    monkeypatch.setenv("TG_T", TOKEN)
    monkeypatch.setenv("TG_C", "@canal")

    def fake(method, token, data, **kw):
        calls.append((method, data))
        if method == "getUpdates":
            return {"ok": True, "result": updates if not any(c[0] == "getUpdates" for c in calls[:-1]) else []}
        return {"ok": True}

    monkeypatch.setattr(deals, "tg_call", fake)
    path = tmp_path / "t.json"
    state = {"B000000001": {"t": NOW_IN.isoformat(), "title": "X", "m": 42, "v": []}}
    deals.save_state(path, state)
    ch = channel(vote_button=True, website_url="https://www.concriterio.eu")
    return ch, path, state, calls


def test_votes_count_toggle_and_edit_buttons(tmp_path, monkeypatch):
    ch, path, state, calls = votes_setup(monkeypatch, tmp_path, [update(10, 1), update(11, 2, cq_id="cq2"), update(12, 1, cq_id="cq3")])
    assert deals.process_votes(ch, path, state, "tag-21") == 1
    # usuario 1 vota, usuario 2 vota, usuario 1 retira -> queda 1 voto
    assert len(state["B000000001"]["v"]) == 1
    edit = [c for c in calls if c[0] == "editMessageReplyMarkup"]
    assert len(edit) == 1 and edit[0][1]["message_id"] == 42
    kb = json.loads(edit[0][1]["reply_markup"])["inline_keyboard"]
    assert kb[0][0]["text"] == "👍 1" and kb[1][0]["url"].endswith("?tag=tag-21")
    answers = [c[1]["text"] for c in calls if c[0] == "answerCallbackQuery"]
    assert answers == ["¡Gracias por tu voto! 👍", "¡Gracias por tu voto! 👍", "Voto retirado"]
    saved = json.loads(path.read_text())
    assert saved["tg_offset"] == 13 and len(saved["posted"]["B000000001"]["v"]) == 1
    # el ID real de Telegram no se guarda en el repositorio
    assert "\"1\"" not in json.dumps(saved["posted"]) and deals.voter_hash(TOKEN, 2) in saved["posted"]["B000000001"]["v"]


def test_votes_ignore_other_chats_unknown_messages_and_other_data(tmp_path, monkeypatch):
    ups = [
        update(1, 1, chat_id=-999, username="otro"),   # otro chat
        update(2, 1, data="otra"),                       # otro boton
        update(3, 1, msg_id=999),                        # mensaje desconocido
    ]
    ch, path, state, calls = votes_setup(monkeypatch, tmp_path, ups)
    assert deals.process_votes(ch, path, state, None) == 0
    assert state["B000000001"]["v"] == []
    assert not [c for c in calls if c[0] == "editMessageReplyMarkup"]
    assert [c[1]["text"] for c in calls if c[0] == "answerCallbackQuery"] == ["Votación cerrada"]
    assert json.loads(path.read_text())["tg_offset"] == 4  # se avanza para no releerlos


def test_votes_survive_telegram_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_T", TOKEN)
    monkeypatch.setenv("TG_C", "@canal")

    def boom(*a, **k):
        raise deals.TelegramError("getUpdates: HTTP 409 conflict")

    monkeypatch.setattr(deals, "tg_call", boom)
    ch = channel(vote_button=True)
    assert deals.process_votes(ch, tmp_path / "t.json", {}, None) == 0


def test_run_channel_processes_votes_and_stores_message_id(tmp_path, monkeypatch):
    sent = setup_publish(monkeypatch)
    monkeypatch.setattr(deals, "send_deal", lambda *a: sent.append(a) or 77)
    got = []
    monkeypatch.setattr(deals, "process_votes", lambda *a: got.append(a) or 0)
    ch = channel(vote_button=True, website_url="https://www.concriterio.eu")
    assert deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN) == 1
    assert len(got) == 1
    entry = json.loads((tmp_path / "t.json").read_text())["posted"]["B000000001"]
    assert entry["m"] == 77 and entry["v"] == []
    assert sent[0][5]["inline_keyboard"][0][0] == {"text": "👍 0", "callback_data": "like"}


# ------------------------------------------------- cabeceras rotativas y sin duplicados

def test_header_profiles_are_complete_and_consistent():
    uas = [p["User-Agent"] for p in deals.HEADER_PROFILES]
    assert len(set(uas)) == len(uas) >= 5
    for p in deals.HEADER_PROFILES:
        assert p["Accept-Language"].startswith("es") and "br" not in p["Accept-Encoding"]
        assert p["Sec-Fetch-Mode"] == "navigate"
        # solo los navegadores Chromium envian sec-ch-ua
        assert ("sec-ch-ua" in p) == ("Chrome/" in p["User-Agent"])


def test_make_session_uses_a_full_profile():
    s = deals.make_session()
    assert s.headers["User-Agent"] in [p["User-Agent"] for p in deals.HEADER_PROFILES]
    assert s.headers["Referer"].startswith("https://www.amazon.es")
    assert "python-requests" not in s.headers["User-Agent"]


def test_rotate_profile_changes_user_agent_and_keeps_cookies():
    s = deals.make_session()
    s.cookies.set("session-id", "abc")
    before = s.headers["User-Agent"]
    for _ in range(10):
        deals.rotate_profile(s)
        assert s.headers["User-Agent"] != before
        before = s.headers["User-Agent"]
    assert s.cookies.get("session-id") == "abc"


def test_fetch_rotates_headers_and_backs_off_after_a_503(monkeypatch):
    s = deals.make_session()
    seen, waits = [], []

    def fake_get(url, headers=None, timeout=None):
        seen.append(s.headers["User-Agent"])
        return Resp(text="ok") if len(seen) == 3 else Resp(status=503)

    monkeypatch.setattr(s, "get", fake_get)
    monkeypatch.setattr(deals, "sleep", waits.append)
    assert deals.fetch(s, "https://www.amazon.es/s?k=x") == "ok"
    assert len(set(seen)) == 3 or seen[0] != seen[1] != seen[2]
    assert len(waits) == 2 and all(w >= 5 for w in waits)


def test_fetch_gives_up_with_blocked_after_all_retries(monkeypatch):
    s = deals.make_session()
    monkeypatch.setattr(s, "get", lambda *a, **k: Resp(status=503))
    monkeypatch.setattr(deals, "sleep", lambda s_: None)
    with pytest.raises(deals.Blocked, match="503"):
        deals.fetch(s, "https://www.amazon.es/s?k=x")


def test_timeout_is_ambiguous_but_connection_refused_is_not(monkeypatch):
    def timeout(url, **kw):
        raise requests.ReadTimeout("timed out")

    monkeypatch.setattr(deals.requests, "post", timeout)
    with pytest.raises(deals.TelegramError) as info:
        deals.tg_call("sendPhoto", TOKEN, {})
    assert info.value.ambiguous

    def refused(url, **kw):
        raise requests.ConnectTimeout("no connect")

    monkeypatch.setattr(deals.requests, "post", refused)
    with pytest.raises(deals.TelegramError) as info:
        deals.tg_call("sendPhoto", TOKEN, {})
    assert not info.value.ambiguous


def test_send_deal_does_not_resend_after_a_timeout(monkeypatch):
    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(url.rsplit("/", 1)[1])
        raise requests.ReadTimeout("timed out")

    monkeypatch.setattr(deals.requests, "post", fake_post)
    with pytest.raises(deals.TelegramError) as info:
        deals.send_deal(TOKEN, "1", "hola", b"JPEG", "https://m.media-amazon.com/x.jpg")
    assert info.value.ambiguous and calls == ["sendPhoto"]  # una sola peticion, ningun reenvio


def test_send_deal_does_not_resend_after_a_5xx(monkeypatch):
    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(url.rsplit("/", 1)[1])
        return FakeResp(502, {"description": "Bad Gateway"})

    monkeypatch.setattr(deals.requests, "post", fake_post)
    with pytest.raises(deals.TelegramError):
        deals.send_deal(TOKEN, "1", "hola", b"JPEG", "https://m.media-amazon.com/x.jpg")
    assert calls == ["sendPhoto"]


def test_unconfirmed_send_counts_as_published_so_it_is_not_repeated(tmp_path, monkeypatch):
    setup_publish(monkeypatch)

    def timeout(*a):
        raise deals.TelegramError("sendPhoto: error de red (ReadTimeout)", ambiguous=True)

    monkeypatch.setattr(deals, "send_deal", timeout)
    assert deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN) == 1
    saved = json.loads((tmp_path / "t.json").read_text())
    assert len(saved["posted"]) == 1 and next(iter(saved["posted"].values()))["m"] is None
    assert datetime.fromisoformat(saved["next_at"]) > NOW_IN  # y no vuelve a intentarlo en el siguiente turno


def test_definitive_telegram_error_is_not_saved_as_published(tmp_path, monkeypatch):
    setup_publish(monkeypatch)

    def rejected(*a):
        raise deals.TelegramError("sendMessage: HTTP 400 chat not found")

    monkeypatch.setattr(deals, "send_deal", rejected)
    with pytest.raises(deals.TelegramError):
        deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN)
    assert json.loads((tmp_path / "t.json").read_text())["posted"] == {}


def test_alert_texts_do_not_include_channel_name(tmp_path, alerts):
    ch = channel(name="ejemplo")
    path = tmp_path / "t.json"
    for i in range(3):
        deals.record_problem(ch, path, "fail", "HTTP 503", NOW_IN + timedelta(minutes=i))
    deals.record_success(ch, path)
    assert alerts and all("ejemplo" not in a[1]["text"] for a in alerts)
    assert "HTTP 503" in alerts[0][1]["text"]


# ------------------------------------------------- oferta de reserva con boton

MANY = page(
    card("B00000000A", "Auriculares inalambricos Alfa", "30,00 €", "100,00 €"),
    card("B00000000B", "Cafetera superautomatica Beta", "100,00 €", "200,00 €"),
    card("B00000000C", "Teclado mecanico gamer Gamma", "40,00 €", "80,00 €"),
    card("B00000000D", "Aspirador de mano Delta", "25,00 €", "50,00 €"),
    card("B00000000E", "Zapatillas de running Epsilon", "45,00 €", "75,00 €"),
)


class Tg:
    """Telegram simulado: guarda las llamadas y devuelve ids de mensaje; 'updates' alimenta getUpdates."""

    def __init__(self):
        self.calls, self.updates, self.n = [], [], 100

    def __call__(self, method, token, data, **kw):
        self.calls.append((method, data))
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return {"result": ups}
        self.n += 1
        return {"ok": True, "result": {"message_id": self.n}}

    def of(self, method):
        return [d for m, d in self.calls if m == method]


def offer_env(monkeypatch, html=MANY):
    monkeypatch.setenv("TG_T", TOKEN)
    monkeypatch.setenv("TG_C", "@canal")
    monkeypatch.setenv("TG_ALERT_CHAT", "777")
    monkeypatch.setenv("AMAZON_TAG", "tag-21")
    monkeypatch.setattr(deals, "collect", lambda ch, html_file=None, session=None: deals.parse_results(html))
    monkeypatch.setattr(deals, "prepare_image", lambda url: b"JPEG")
    monkeypatch.setattr(deals, "sleep", lambda s: None)
    tg = Tg()
    monkeypatch.setattr(deals, "tg_call", tg)
    return tg


def test_publish_stores_the_next_best_offers_as_reserve(tmp_path, monkeypatch):
    tg = offer_env(monkeypatch)
    assert deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN) == 1
    saved = json.loads((tmp_path / "t.json").read_text())
    published = next(iter(saved["posted"]))
    reserve = saved["reserve"]
    assert len(reserve) == 1 and published not in [d["a"] for d in reserve]  # una por categoria
    assert all(d["title"] and d["p"] and d["img"] for d in reserve)
    assert "tg_offset" not in saved or isinstance(saved["tg_offset"], int)


def test_reserve_is_topped_up_not_duplicated(tmp_path, monkeypatch):
    offer_env(monkeypatch)
    deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN)
    later = NOW_IN + timedelta(hours=1)
    deals.run_channel(channel(), state_dir=tmp_path, now=later)
    saved = json.loads((tmp_path / "t.json").read_text())
    ids = [d["a"] for d in saved["reserve"]]
    assert len(ids) == len(set(ids)) <= 3 and not set(ids) & set(saved["posted"])


def test_reserve_disabled_when_manual_offer_is_off(tmp_path, monkeypatch):
    offer_env(monkeypatch)
    deals.run_channel(channel(manual_offer=False), state_dir=tmp_path, now=NOW_IN)
    assert "reserve" not in json.loads((tmp_path / "t.json").read_text())


def fail_after_publishing(tmp_path, monkeypatch, fails):
    """Publica una vez (con reserva) y luego Amazon empieza a dar 503."""
    tg = offer_env(monkeypatch)
    deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN)

    def blocked(ch, html_file=None, session=None):
        raise deals.Blocked("HTTP 503")

    monkeypatch.setattr(deals, "collect", blocked)
    t = NOW_IN + timedelta(minutes=40)
    for i in range(fails):
        with pytest.raises(deals.Blocked):
            deals.run_channel(channel(), state_dir=tmp_path, now=t + timedelta(minutes=10 * i))
    return tg


def test_no_offer_after_a_single_failure(tmp_path, monkeypatch):
    tg = fail_after_publishing(tmp_path, monkeypatch, 1)
    assert not [d for d in tg.of("sendPhoto") if d["chat_id"] == "777"]


def test_offer_is_sent_privately_with_buttons_after_two_failures_and_only_once(tmp_path, monkeypatch):
    tg = fail_after_publishing(tmp_path, monkeypatch, 3)
    offers = [d for d in tg.of("sendPhoto") if d["chat_id"] == "777"]
    assert len(offers) == 1  # un solo aviso mientras esperas
    o = offers[0]
    assert "Amazon no responde" in o["caption"] and "Ver en Amazon" in o["caption"] and "tag=tag-21" in o["caption"]
    kb = json.loads(o["reply_markup"])["inline_keyboard"]
    asin = json.loads((tmp_path / "t.json").read_text())["offer"]["a"]
    assert kb[0][0]["callback_data"] == f"pub:{asin}" and kb[1][0]["callback_data"] == f"skip:{asin}"
    assert o["photo"].startswith("https://m.media-amazon.com/")


def test_offer_needs_the_private_chat(tmp_path, monkeypatch):
    tg = offer_env(monkeypatch)
    deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN)
    monkeypatch.delenv("TG_ALERT_CHAT")
    assert deals.maybe_offer(channel(), tmp_path / "t.json", NOW_IN, None) is False


def press(tg, asin, action, chat="777", uid=1):
    tg.updates.append({
        "update_id": 900 + len(tg.calls),
        "callback_query": {
            "id": "cb1", "data": f"{action}:{asin}", "from": {"id": uid},
            "message": {"message_id": 5, "chat": {"id": int(chat)}},
        },
    })


def test_pressing_publish_posts_to_channel_and_records_it(tmp_path, monkeypatch):
    tg = fail_after_publishing(tmp_path, monkeypatch, 2)
    path = tmp_path / "t.json"
    asin = json.loads(path.read_text())["offer"]["a"]
    sent = []
    monkeypatch.setattr(deals, "send_deal", lambda *a: sent.append(a) or 555)
    press(tg, asin, "pub")
    t = NOW_IN + timedelta(minutes=70)
    assert deals.run_channel(channel(), state_dir=tmp_path, now=t) == 0  # atiende el boton y no toca consultar Amazon
    saved = json.loads(path.read_text())
    assert len(sent) == 1 and sent[0][1] == "@canal" and "Ver en Amazon" in sent[0][2]
    assert saved["posted"][asin]["manual"] is True and saved["posted"][asin]["m"] == 555
    assert asin not in [d["a"] for d in saved["reserve"]] and saved["offer"] is None
    assert datetime.fromisoformat(saved["next_at"]) > t  # el bot espera su turno despues de publicar a mano
    assert any("Publicada en el canal" in d.get("text", "") for d in tg.of("sendMessage"))
    assert any(d.get("text") == "Publicada ✅" for d in tg.of("answerCallbackQuery"))


def test_published_by_hand_is_never_repeated_by_the_bot(tmp_path, monkeypatch):
    tg = fail_after_publishing(tmp_path, monkeypatch, 2)
    path = tmp_path / "t.json"
    asin = json.loads(path.read_text())["offer"]["a"]
    monkeypatch.setattr(deals, "send_deal", lambda *a: 555)
    press(tg, asin, "pub")
    deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(minutes=70))
    monkeypatch.setattr(deals, "collect", lambda ch, html_file=None, session=None: deals.parse_results(MANY))
    posted = []
    monkeypatch.setattr(deals, "send_deal", lambda *a: posted.append(a) or 1)
    deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(hours=3))
    saved = json.loads(path.read_text())
    assert len(posted) == 1 and len(saved["posted"]) == 3  # la primera, la manual y la nueva: ninguna repetida
    assert saved["posted"][asin]["manual"] is True


def test_pressing_skip_discards_and_never_picks_it_again(tmp_path, monkeypatch):
    tg = fail_after_publishing(tmp_path, monkeypatch, 2)
    path = tmp_path / "t.json"
    asin = json.loads(path.read_text())["offer"]["a"]
    press(tg, asin, "skip")
    with pytest.raises(deals.Blocked):
        deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(minutes=70))
    saved = json.loads(path.read_text())
    assert asin in saved["rejected"] and asin not in saved["posted"]
    assert asin not in [d["a"] for d in saved["reserve"]]
    assert saved["offer"] is None or saved["offer"]["a"] != asin  # si Amazon sigue fallando, te ofrece la siguiente
    assert any(d.get("text") == "Descartada" for d in tg.of("answerCallbackQuery"))


def test_expired_offer_cannot_be_published(tmp_path, monkeypatch):
    tg = fail_after_publishing(tmp_path, monkeypatch, 2)
    path = tmp_path / "t.json"
    asin = json.loads(path.read_text())["offer"]["a"]
    sent = []
    monkeypatch.setattr(deals, "send_deal", lambda *a: sent.append(a))
    press(tg, asin, "pub")
    with pytest.raises(deals.Blocked):
        deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(hours=5))
    assert sent == []
    assert any(d.get("text") == "Oferta caducada" for d in tg.of("answerCallbackQuery"))


def test_buttons_from_other_chats_are_ignored(tmp_path, monkeypatch):
    tg = fail_after_publishing(tmp_path, monkeypatch, 2)
    path = tmp_path / "t.json"
    asin = json.loads(path.read_text())["offer"]["a"]
    sent = []
    monkeypatch.setattr(deals, "send_deal", lambda *a: sent.append(a))
    press(tg, asin, "pub", chat="999")  # otro chat: nadie mas puede publicar
    with pytest.raises(deals.Blocked):
        deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(minutes=70))
    assert sent == [] and asin not in json.loads(path.read_text())["posted"]


def test_failed_publish_keeps_the_offer_so_you_can_retry(tmp_path, monkeypatch):
    tg = fail_after_publishing(tmp_path, monkeypatch, 2)
    path = tmp_path / "t.json"
    asin = json.loads(path.read_text())["offer"]["a"]

    def rejected(*a):
        raise deals.TelegramError("sendPhoto: HTTP 400 bad request")

    monkeypatch.setattr(deals, "send_deal", rejected)
    press(tg, asin, "pub")
    with pytest.raises(deals.Blocked):
        deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN + timedelta(minutes=70))
    saved = json.loads(path.read_text())
    assert asin not in saved["posted"] and asin in [d["a"] for d in saved["reserve"]]


def test_save_state_keeps_unknown_keys(tmp_path):
    path = tmp_path / "s.json"
    deals.set_state_key(path, "reserve", [{"a": "X"}])
    deals.set_state_key(path, "tg_offset", 7)
    deals.save_state(path, {}, next_at=NOW_IN)
    saved = json.loads(path.read_text())
    assert saved["reserve"] == [{"a": "X"}] and saved["tg_offset"] == 7


def test_diversify_keeps_one_per_category_and_drops_lookalikes_of_posted():
    ch = channel()
    posted = {"P1": {"title": "DDPAI Mini Pro 4K Dash CAM Coche Camara 2160P WiFi GPS"}}
    items = [
        {"a": "A1", "c": "Accesorios de coche", "title": "DDPAI Mini Pro 4K Dash CAM Coche Camara 2160P WiFi"},
        {"a": "A2", "c": "Accesorios de coche", "title": "Aspirador de coche portatil potente"},
        {"a": "A3", "c": "Accesorios de coche", "title": "Cargador de coche USB rapido doble"},
        {"a": "A4", "c": "Hogar", "title": "Lampara de mesa LED regulable"},
        {"a": "A5", "c": "Hogar", "title": "Organizador de cocina apilable"},
    ]
    got = deals.diversify(items, posted, ch)
    assert [d["a"] for d in got] == ["A2", "A4"]  # A1 parecida a lo publicado; A3/A5 repiten categoria


def test_reserve_loaded_for_offers_skips_lookalikes_of_what_was_just_posted(tmp_path):
    ch = channel()
    path = tmp_path / "t.json"
    deals.set_state_key(path, "reserve", [
        {"a": "A1", "c": "Coche", "title": "DDPAI Mini Pro 4K Dash CAM Coche Camara 2160P", "p": 50.0, "t": NOW_IN.isoformat()},
        {"a": "A2", "c": "Hogar", "title": "Lampara de mesa LED regulable", "p": 20.0, "t": NOW_IN.isoformat()},
    ])
    posted = {"P1": {"title": "DDPAI Mini Pro 4K Dash CAM Coche Camara 2160P WiFi", "t": NOW_IN.isoformat()}}
    got = deals.load_reserve(path, NOW_IN, ch, exclude=posted)
    assert [d["a"] for d in got] == ["A2"]


def test_every_channel_file_loads_and_its_secrets_are_in_the_workflow():
    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github/workflows/run.yml").read_text(encoding="utf-8")
    files = sorted((root / "channels").glob("*.json"))
    assert files
    names = []
    for f in files:
        ch = deals.load_channel(f)
        names.append(ch.name)
        if ch.enabled:  # un canal activo sin sus secretos en el workflow nunca publicaria
            assert f"{ch.token_env}: ${{{{ secrets.{ch.token_env} }}}}" in workflow
            assert f"{ch.chat_env}: ${{{{ secrets.{ch.chat_env} }}}}" in workflow
    assert len(set(names)) == len(names)  # nombres distintos: cada canal tiene su propio estado


# ------------------------------------------------- texto para X

def xprod(title="Auriculares inalambricos", price=39.99, old=79.99, asin="B000000001"):
    return deals.Product(asin=asin, title=title, price=price, old_price=old, rating=4.5, reviews=1000, image=None)


def test_x_weight_counts_emojis_double_and_latin_single():
    assert deals.x_weight("abc áéíóú ñ") == 11
    assert deals.x_weight("🔥") == 2 and deals.x_weight("💶 👉") == 5
    assert deals.x_weight("€") == 2  # el simbolo del euro tambien cuenta doble para X


def test_x_text_matches_the_format_used_by_hand():
    ch = channel(x_copy=True)
    url = "https://www.amazon.es/dp/B0FG3ZYOQ?tag=mitag-21"
    p = xprod(title="Mellerware - Freidora sin aceite Crunchy Plus 5.5L 1500W | 11 Menus Predefinidos",
              price=39.99, old=69.99)
    assert deals.build_x_text(p, url, ch, {"emoji": "💡"}) == (
        "Mellerware - Freidora sin aceite Crunchy Plus 5.5L 1500W | 11 Menus Predefinidos\n\n"
        "❌69.99€\n🛒39.99€👉-43%\n\n" + url
    )


def test_x_text_fits_280_even_with_a_huge_title():
    ch = channel(x_copy=True)
    url = "https://www.amazon.es/dp/B000000001?tag=mitag-21"
    text = deals.build_x_text(xprod(title="Super " * 80 + "final"), url, ch)
    assert deals.x_weight(text.replace(url, "")) + deals.X_URL_WEIGHT <= deals.X_LIMIT
    assert "…" in text.split("\n")[0] and url in text and "🛒39.99€👉-50%" in text


def test_x_text_whole_prices_without_old_price_and_optional_tags():
    ch = channel(x_copy=True, x_hashtags="#chollo", x_disclosure="Enlace afiliado")
    text = deals.build_x_text(xprod(price=23.0, old=None), "https://www.amazon.es/dp/B000000001", ch)
    assert "❌" not in text and "🛒23€" in text and "👉" not in text.split("🛒")[1].split("\n")[0]
    assert text.endswith("#chollo (Enlace afiliado)")


def x_env(monkeypatch):
    tg = offer_env(monkeypatch)
    return tg


def x_sent(tg):
    return [d for m, d in tg.calls if m == "sendPhoto" and d.get("chat_id") == "777"]


def test_x_copy_is_sent_privately_with_the_text_in_a_code_block(tmp_path, monkeypatch):
    tg = x_env(monkeypatch)
    deals.run_channel(channel(x_copy=True, x_min_discount=10), state_dir=tmp_path, now=NOW_IN)
    sent = x_sent(tg)
    assert len(sent) == 1
    assert "<code>" in sent[0]["caption"] and "tag=tag-21" in sent[0]["caption"] and "1/6" in sent[0]["caption"]
    assert "🛒" in sent[0]["caption"] and "❌" in sent[0]["caption"]
    assert [d for m, d in tg.calls if m == "sendPhoto" and d.get("chat_id") == "@canal"]  # y el canal, como siempre


def test_x_copy_is_off_by_default_and_respects_min_discount(tmp_path, monkeypatch):
    tg = x_env(monkeypatch)
    deals.run_channel(channel(), state_dir=tmp_path, now=NOW_IN)
    assert x_sent(tg) == []
    tg2 = x_env(monkeypatch)
    deals.run_channel(channel(x_copy=True, x_min_discount=99), state_dir=tmp_path / "b", now=NOW_IN)
    assert x_sent(tg2) == []


def test_x_copy_daily_cap_and_new_day_resets(tmp_path, monkeypatch):
    tg = x_env(monkeypatch)
    ch = channel(x_copy=True, x_min_discount=10, x_max_per_day=1)
    deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN)
    deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN + timedelta(hours=1))
    assert len(x_sent(tg)) == 1  # el segundo dia-mismo ya no entra
    deals.run_channel(ch, state_dir=tmp_path, now=NOW_IN + timedelta(days=1))
    assert len(x_sent(tg)) == 2


def test_x_copy_needs_the_private_chat(tmp_path, monkeypatch):
    tg = x_env(monkeypatch)
    monkeypatch.delenv("TG_ALERT_CHAT")
    assert deals.run_channel(channel(x_copy=True, x_min_discount=10), state_dir=tmp_path, now=NOW_IN) == 1
    assert x_sent(tg) == []


def test_x_copy_failure_never_breaks_the_publication(tmp_path, monkeypatch):
    tg = x_env(monkeypatch)
    real = deals.tg_call

    def flaky(method, token, data, **kw):
        if data.get("chat_id") == "777":
            raise deals.TelegramError(f"{method}: HTTP 403 blocked")
        return real(method, token, data, **kw)

    monkeypatch.setattr(deals, "tg_call", flaky)
    assert deals.run_channel(channel(x_copy=True, x_min_discount=10), state_dir=tmp_path, now=NOW_IN) == 1
    saved = json.loads((tmp_path / "t.json").read_text())
    assert len(saved["posted"]) == 1 and "x_copy" not in saved


def test_x_intent_url_opens_the_composer_with_the_exact_text():
    from urllib.parse import parse_qs, urlparse
    text = "Freidora 5,5L ñ\n\n❌69.99€\n🛒39.99€👉-43%\n\nhttps://www.amazon.es/dp/B000000001?tag=tag-21"
    url = deals.x_intent_url(text)
    assert url.startswith("https://x.com/intent/post?text=") and "\n" not in url and " " not in url
    assert parse_qs(urlparse(url).query)["text"] == [text]
    assert deals.x_intent_url("a" * 3000) is None


def test_x_copy_has_a_button_that_opens_x_with_the_text(tmp_path, monkeypatch):
    from html import unescape
    from urllib.parse import parse_qs, urlparse
    tg = x_env(monkeypatch)
    deals.run_channel(channel(x_copy=True, x_min_discount=10), state_dir=tmp_path, now=NOW_IN)
    sent = x_sent(tg)[0]
    button = json.loads(sent["reply_markup"])["inline_keyboard"][0][0]
    text = parse_qs(urlparse(button["url"]).query)["text"][0]
    assert "tag=tag-21" in text and "🛒" in text and button["url"].startswith("https://x.com/intent/post?text=")
    assert f"<code>{deals.escape(text)}</code>" in sent["caption"] and "Pulsa el botón" in sent["caption"]


def test_x_button_can_be_turned_off(tmp_path, monkeypatch):
    tg = x_env(monkeypatch)
    deals.run_channel(channel(x_copy=True, x_min_discount=10, x_button=False), state_dir=tmp_path, now=NOW_IN)
    sent = x_sent(tg)[0]
    assert "reply_markup" not in sent and "Toca el texto" in sent["caption"]


def test_x_copy_is_resent_without_the_button_if_telegram_refuses_it(tmp_path, monkeypatch, caplog):
    tg = x_env(monkeypatch)
    real = deals.tg_call

    def picky(method, token, data, **kw):
        if data.get("chat_id") == "777" and "reply_markup" in data:
            raise deals.TelegramError(f"{method}: HTTP 400 BUTTON_URL_INVALID")
        return real(method, token, data, **kw)

    monkeypatch.setattr(deals, "tg_call", picky)
    with caplog.at_level(logging.WARNING, logger="deals"):
        assert deals.run_channel(channel(x_copy=True, x_min_discount=10), state_dir=tmp_path, now=NOW_IN) == 1
    sent = x_sent(tg)
    assert len(sent) == 1 and "reply_markup" not in sent[0] and "<code>" in sent[0]["caption"]
    assert "no acepto el boton de X" in caplog.text
    assert json.loads((tmp_path / "t.json").read_text())["x_copy"]["n"] == 1


def test_load_channel_validates_x_button(tmp_path):
    assert deals.load_channel(write_cfg(tmp_path)).x_button is True
    with pytest.raises(deals.ConfigError, match="x_button"):
        deals.load_channel(write_cfg(tmp_path, x_button="si"))


# ------------------------------------------------- precio por kilo no es el precio anterior

def unit_card(price, unit_html, strike_html=""):
    return f"""
    <div data-component-type="s-search-result" data-asin="B00CHJ3DW4">
      <img class="s-image" src="https://m.media-amazon.com/images/I/x.jpg">
      <h2 aria-label="Creatina 500g"><a><span>Creatina 500g</span></a></h2>
      <span class="a-icon-alt">4,5 de 5 estrellas</span><span class="s-underline-text">2.000</span>
      <span class="a-price" data-a-size="xl"><span class="a-offscreen">{price}</span></span>
      {strike_html}
      <span class="a-size-base a-color-secondary">{unit_html}</span>
    </div>"""


KG = '(<span class="a-price a-text-price" data-a-size="b" data-a-color="secondary"><span class="a-offscreen">33,98 €</span></span>/kg)'
STRIKE = ('<span class="a-size-base a-color-secondary">PVPR: <span class="a-price a-text-price" data-a-size="b" '
          'data-a-strike="true" data-a-color="secondary"><span class="a-offscreen">24,99 €</span></span></span>')


def parse_one(html):
    (p,) = deals.parse_results("<html><body>" + html + "</body></html>")
    return p


def test_price_per_kilo_is_not_taken_as_the_old_price():
    p = parse_one(unit_card("16,99 €", KG))
    assert p.price == 16.99 and p.old_price is None and p.discount == 0


def test_real_strikethrough_price_wins_over_the_unit_price_in_any_order():
    assert parse_one(unit_card("16,99 €", KG, STRIKE)).old_price == 24.99
    assert parse_one(unit_card("16,99 €", STRIKE + KG)).old_price == 24.99


@pytest.mark.parametrize("suffix", ["/kg", "/100 g", "/l", "/100 ml", "/ud.", "/unidad", "/ 100g"])
def test_unit_price_suffixes_are_all_ignored(suffix):
    html = (
        '(<span class="a-price a-text-price" data-a-size="b"><span class="a-offscreen">1,99 €</span></span>'
        + suffix + ")"
    )
    assert parse_one(unit_card("0,99 €", html)).old_price is None


def test_unmarked_strikethrough_price_still_works_without_the_strike_attribute():
    html = unit_card("16,99 €", "", '<span class="a-price a-text-price"><span class="a-offscreen">24,99 €</span></span>')
    assert parse_one(html).old_price == 24.99


def test_title_skips_brand_only_h2():
    """En tarjetas de moda el primer <h2> es solo la marca; el nombre va en el segundo."""
    html = """<div data-component-type="s-search-result" data-asin="B0CKXT51LV">
      <img class="s-image" src="https://m.media-amazon.com/images/I/x.jpg" alt="adidas Hombre Runfalcon 5 Zapatillas de Correr">
      <h2 class="a-size-mini"><span>adidas</span></h2>
      <h2 aria-label="adidas Hombre Runfalcon 5 Zapatillas de Correr"><a><span>adidas Hombre Runfalcon 5 Zapatillas de Correr</span></a></h2>
      <span class="a-icon-alt">4,4 de 5 estrellas</span><span class="s-underline-text">13.005</span>
      <span class="a-price"><span class="a-offscreen">37,99 €</span></span></div>"""
    assert deals.parse_results("<html><body>" + html + "</body></html>")[0].title == "adidas Hombre Runfalcon 5 Zapatillas de Correr"


def test_title_falls_back_to_image_alt_when_only_brand():
    html = """<div data-component-type="s-search-result" data-asin="B0H3KV5W3B">
      <img class="s-image" src="https://m.media-amazon.com/images/I/y.jpg" alt="Yankony Mochila Impermeable 20L">
      <h2><span>Yankony</span></h2>
      <span class="a-price"><span class="a-offscreen">21,98 €</span></span></div>"""
    assert deals.parse_results("<html><body>" + html + "</body></html>")[0].title == "Yankony Mochila Impermeable 20L"


def test_price_reference_uses_history_sentence_as_low30():
    html = product_page(
        "<div>En los últimos 30 días, el precio de las Zapatillas Adidas Runfalcon 5 ha variado entre 31,99 € y 39,49 €. "
        "El precio actual es 39,49 €, el más alto en los últimos 30 días.</div>")
    assert deals.price_reference(html) == {"low30": 31.99}


def test_price_reference_prefers_explicit_low30_over_sentence():
    html = product_page(HISTORIAL + "<div>El precio más bajo de los últimos 30 días: 19,49€</div>")
    assert deals.price_reference(html)["low30"] == 19.49
