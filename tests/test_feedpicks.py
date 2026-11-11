import json
from datetime import date, datetime, timedelta, timezone

import pytest

import feed
import feedpicks as fp
import pricehist as ph

TODAY = date(2026, 11, 20)
T = ph.day_no(TODAY)


def entry(*pairs, last=T):
    """Historial en el formato de feed.py: [ultimo dia visto, dia, precio, dia, precio...]; dias = hoy - N."""
    flat = []
    for days_ago, price in pairs:
        flat += [T - days_ago, price]
    return [last, *flat]


def item(price, **kw):
    base = {"id": "1", "name": "Movil Xiaomi 12GB negro", "price": price, "stock": None, "valid_to": ""}
    base.update(kw)
    return base


def judge(price, *pairs, **kw):
    return fp.judge(item(price, **kw), entry(*pairs), TODAY)


# ---------------------------------------------------------------- filas del feed

def test_items_from_rows_reads_product_data_and_skips_unusable_rows():
    rows = [
        {"aw_product_id": "1", "search_price": "89.99", "condition": "new", "in_stock": "1", "product_name": "Uno",
         "brand_name": "Xiaomi", "aw_deep_link": "https://www.awin1.com/pclick.php?p=1", "rrp_price": "129.99",
         "large_image": "https://img/l.jpg", "merchant_image_url": "https://img/m.jpg", "ean": "123",
         "merchant_product_category_path": "Moviles > Xiaomi", "stock_quantity": "5", "average_rating": "4,5",
         "reviews": "12"},
        {"aw_product_id": "2", "search_price": "10", "condition": "used", "in_stock": "1"},
        {"aw_product_id": "3", "search_price": "10", "condition": "new", "in_stock": "0"},
        {"aw_product_id": "4", "search_price": "", "condition": "new", "in_stock": "1"},
        {"aw_product_id": "5", "search_price": "50", "rrp_price": "40", "merchant_image_url": "https://img/m.jpg"},
    ]
    items = fp.items_from_rows(rows)
    assert set(items) == {"1", "5"}
    one = items["1"]
    assert one["price"] == 89.99 and one["rrp"] == 129.99 and one["image"] == "https://img/l.jpg"
    assert one["stock"] == 5 and one["rating"] == 4.5 and one["reviews"] == 12 and one["brand"] == "Xiaomi"
    assert items["5"]["rrp"] is None and items["5"]["image"] == "https://img/m.jpg"  # un PVPR por debajo no cuenta


# ---------------------------------------------------------------- reglas de seleccion

def test_real_new_low_is_a_candidate():
    info, why = judge(80.0, (10, 100.0), (0, 80.0))
    assert why == "" and info["prior_low"] == 100.0 and info["median"] == 100.0 and info["drop"] == 20
    assert info["tracked_days"] == 11 and info["level_days"] == 1


def test_price_spike_and_return_is_not_a_discount():
    """Subir a 200 y volver a 100 no es una oferta: ese precio ya existia antes."""
    assert judge(100.0, (12, 100.0), (8, 200.0), (0, 100.0))[1] == "no es minimo nuevo"


def test_rejection_reasons():
    assert judge(80.0, (1, 100.0), (0, 80.0))[1] == "historial corto"               # solo 2 dias registrados
    assert judge(100.0, (10, 100.0))[1] == "precio sin cambios"
    assert judge(80.0, (20, 100.0), (15, 80.0))[1] == "bajada antigua"
    assert judge(95.0, (10, 100.0), (0, 95.0))[1] == "bajada pequena"               # -5 % sobre la mediana
    assert judge(100.0, (10, 1000.0), (0, 100.0))[1] == "bajada sospechosa"         # -90 %: error de datos
    assert judge(20.0, (10, 30.0), (0, 20.0))[1] == "precio bajo"
    assert judge(80.0, (10, 100.0), (0, 80.0), stock=0)[1] == "sin stock"
    assert judge(80.0, (10, 100.0), (0, 80.0), valid_to="2026-11-01 00:00:00")[1] == "oferta caducada"
    assert fp.judge(item(80.0), entry((10, 100.0), (0, 80.0), last=T - 1), TODAY)[1] == "sin historial de hoy"
    assert fp.judge(item(80.0), None, TODAY)[1] == "sin historial de hoy"


def test_stock_and_expiry_accept_missing_or_future_values():
    assert judge(80.0, (10, 100.0), (0, 80.0), stock=3, valid_to="2026-12-31 00:00:00")[1] == ""
    assert judge(80.0, (10, 100.0), (0, 80.0), valid_to="no es fecha")[1] == ""


def test_median_needs_the_drop_to_be_recent_relative_to_the_window():
    # lleva 6 de 9 dias a 80: la mediana ya es 80, no hay bajada frente a lo habitual
    assert judge(80.0, (8, 100.0), (5, 80.0), (0, 80.0))[1] != ""


def test_select_orders_by_score_dedupes_variants_and_counts_reasons():
    items = {
        "1": item(80.0, id="1", name="Xiaomi Redmi Note 14 Pro 8GB negro"),
        "2": item(80.0, id="2", name="Xiaomi Redmi Note 14 Pro 8GB azul"),       # variante del 1
        "3": item(300.0, id="3", name="Aspirador robot Roborock Q Revo S"),      # mas caro y mayor caida
        "4": item(100.0, id="4", name="Teclado mecanico"),                       # sin cambios
    }
    hist = {"p": {
        "1": entry((10, 100.0), (0, 80.0)),
        "2": entry((10, 100.0), (0, 80.0)),
        "3": entry((10, 450.0), (0, 300.0)),
        "4": entry((10, 100.0)),
    }}
    picks, stats = fp.select(items, hist, TODAY)
    assert [c["id"] for c in picks] == ["3", "1"]
    assert stats == {"precio sin cambios": 1, "variante de otro candidato": 1, "candidatos": 2}
    assert picks[0]["drop"] == 33 and picks[0]["score"] > picks[1]["score"]


def test_select_respects_the_limit(monkeypatch):
    monkeypatch.setattr(fp, "LIMIT", 2)
    items = {str(i): item(80.0, id=str(i), name=f"Producto numero{i} unico{i}") for i in range(5)}
    hist = {"p": {str(i): entry((10, 100.0), (0, 80.0)) for i in range(5)}}
    assert len(fp.select(items, hist, TODAY)[0]) == 2


def test_select_reports_suspicious_drops_apart():
    items = {"1": item(100.0, id="1", name="Portatil gaming misterioso"), "2": item(80.0, id="2", name="Movil normal azul")}
    hist = {"p": {"1": entry((10, 1000.0), (0, 100.0)), "2": entry((10, 100.0), (0, 80.0))}}
    suspicious = []
    picks, stats = fp.select(items, hist, TODAY, suspicious)
    assert [c["id"] for c in picks] == ["2"] and stats["bajada sospechosa"] == 1
    assert [(c["id"], c["price"], c["prior_low"], c["drop"]) for c in suspicious] == [("1", 100.0, 1000.0, 90)]


def test_big_drop_goes_to_verify_not_rejected_and_absurd_drop_stays_suspicious():
    info, why = judge(40.0, (10, 100.0), (0, 40.0))                  # -60 %: se contrasta con otra tienda
    assert why == "" and info["verify"] is True and info["drop"] == 60
    info, why = judge(70.0, (10, 100.0), (0, 70.0))                  # -30 %: candidato normal
    assert why == "" and "verify" not in info
    assert judge(15.0 * 3, (10, 1000.0), (0, 45.0))[1] == "bajada sospechosa"   # -95 %


def test_select_separates_to_verify_from_candidates():
    items = {"1": item(40.0, id="1", name="Auriculares Epos Impact SC 230 MS II"),
             "2": item(80.0, id="2", name="Movil normal azul oscuro"),
             "3": item(100.0, id="3", name="Portatil gaming misterioso")}
    hist = {"p": {"1": entry((10, 100.0), (0, 40.0)), "2": entry((10, 100.0), (0, 80.0)),
                  "3": entry((10, 1000.0), (0, 100.0))}}
    suspicious, verify = [], []
    picks, stats = fp.select(items, hist, TODAY, suspicious, verify)
    assert [c["id"] for c in picks] == ["2"]
    assert [c["id"] for c in verify] == ["1"] and verify[0]["verify"] is True
    assert [c["id"] for c in suspicious] == ["3"]
    assert stats["a comprobar"] == 1 and stats["bajada sospechosa"] == 1 and stats["candidatos"] == 1


def test_foreign_market_version_is_rejected_from_the_description():
    raw = ("aw_product_id,search_price,product_name,description\n"
           "1,109.95,Pixel Watch LTE,Aviso importante sobre la conectividad LTE: esta unidad corresponde a la version estadounidense\n"
           "2,109.95,Pixel Watch Wifi,Reloj con version USB 2.0 y sensor\n").encode()
    rows = feed.read_rows(raw)
    assert rows[0].get("foreign") == "1" and "foreign" not in rows[1]
    items = fp.items_from_rows(rows)
    assert items["1"]["foreign"] is True and items["2"]["foreign"] is False
    assert judge(80.0, (10, 100.0), (0, 80.0), foreign=True)[1] == "version de otro mercado"


def test_save_writes_valid_json_one_product_per_line(tmp_path):
    data = {"day": "2026-11-20", "suspicious": [{"id": "9", "name": "Raro"}],
            "candidates": [{"id": "1", "name": "Ñandú"}, {"id": "2", "name": "Dos"}]}
    path = tmp_path / "s" / "c.json"
    fp.save(path, data)
    assert json.loads(path.read_text(encoding="utf-8")) == data
    assert len(path.read_text(encoding="utf-8").splitlines()) == 6
    fp.save(path, {"day": "x", "candidates": []})
    assert json.loads(path.read_text(encoding="utf-8")) == {"day": "x", "candidates": []}


# ---------------------------------------------------------------- integracion con el colector del feed

FULL_HEADER = "aw_deep_link,product_name,aw_product_id,search_price,condition,in_stock,rrp_price,stock_quantity"


def feed_csv(prices):
    lines = [FULL_HEADER] + [
        f"https://awin/p/{pid},Producto {pid} modelo{pid},{pid},{price},new,1,{price * 2},4" for pid, price in prices.items()
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_feed_collect_writes_candidates_from_the_history(tmp_path):
    path = tmp_path / "feed.json"
    base = {str(1000 + i): 100.0 for i in range(600)}
    days = [date(2026, 11, 9) + timedelta(days=n) for n in range(3)]
    sentences = []
    for n, day in enumerate(days):
        prices = dict(base)
        if n == 2:
            prices["1000"] = 70.0                                  # baja el tercer dia
        sentences.append(feed.collect("http://x", path, day, now=datetime(2026, 11, 9, tzinfo=timezone.utc) + timedelta(days=n),
                                      fetch=lambda url, p=prices: feed_csv(p)))
    assert "candidatos" in sentences[-1] and "1 candidatos" in sentences[-1]
    data = json.loads((tmp_path / feed.PICKS_FILE).read_text(encoding="utf-8"))
    assert data["mode"] == "informe" and data["day"] == "2026-11-11" and data["products"] == 600
    cand = data["candidates"][0]
    assert cand["id"] == "1000" and cand["price"] == 70.0 and cand["drop"] == 30
    assert cand["link"] == "https://awin/p/1000" and cand["rrp"] == 140.0 and cand["stock"] == 4


def test_feed_collect_survives_a_failure_in_the_selector(tmp_path, monkeypatch, caplog):
    def boom(*a, **k):
        raise ValueError("fallo del selector")
    monkeypatch.setattr(fp, "build", boom)
    path = tmp_path / "feed.json"
    text = feed.collect("http://x", path, date(2026, 11, 9), now=datetime(2026, 11, 9, tzinfo=timezone.utc),
                        fetch=lambda url: feed_csv({str(1000 + i): 10.0 for i in range(600)}))
    assert text.startswith("feed: 600 productos") and "candidatos" not in text
    assert feed.load_history(path)["updated"] == "2026-11-09"       # el historial se guardo igualmente
    assert not (tmp_path / feed.PICKS_FILE).exists()
    assert "no se pudieron calcular los candidatos (ValueError)" in caplog.text
