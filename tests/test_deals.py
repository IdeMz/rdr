import io
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests
from PIL import Image

import deals

TOKEN = "123456789:AAH_fake-token_abcdefghijklmnopqrstuvwxyz0123"


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
    monkeypatch.setattr(deals, "send_deal", lambda token, chat, caption, photo, url: sent.append((chat, photo, url)))
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
    assert "B000000021" in sent[0][2]
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


def test_category_order_visits_oldest_first():
    ch = channel(categories=CATS)
    assert [c["nombre"] for c in deals.category_order(ch, {})] == ["Auriculares", "Perfumes", "Lego"]
    visited = {"Auriculares": "2026-11-03T10:00:00+00:00", "Lego": "2026-11-03T09:00:00+00:00"}
    assert [c["nombre"] for c in deals.category_order(ch, visited)] == ["Perfumes", "Lego", "Auriculares"]


def test_run_channel_rotates_categories_and_skips_empty_ones(tmp_path, monkeypatch):
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
