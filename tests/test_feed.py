import gzip
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import deals
import feed

D1 = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
HEADER = "aw_deep_link,product_name,aw_product_id,search_price,condition,in_stock"


def csv_bytes(rows, header=HEADER, delim=","):
    lines = [header.replace(",", delim)] + [delim.join(r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def many(n=600, price=10.0):
    return {str(1000 + i): price for i in range(n)}


# ---------------------------------------------------------------- lectura del feed

def test_parse_feed_reads_new_products_with_price():
    raw = csv_bytes([
        ["http://a", "Uno", "1", "89.99", "new", "1"],
        ["http://b", "Usado", "2", "50.00", "used", "1"],
        ["http://c", "Agotado", "3", "20.00", "new", "0"],
        ["http://d", "Sin precio", "4", "", "new", "1"],
        ["http://e", "Coma decimal", "5", '"12,50"', "new", "1"],
    ])
    assert feed.parse_feed(raw) == {"1": 89.99, "5": 12.5}


def test_parse_feed_accepts_gzip_and_other_delimiters():
    raw = csv_bytes([["http://a", "Uno", "1", "9.99", "new", "1"]], delim=";")
    assert feed.parse_feed(raw) == {"1": 9.99}
    assert feed.parse_feed(gzip.compress(raw)) == {"1": 9.99}


def test_parse_feed_accepts_quoted_header():
    raw = b'"aw_deep_link","product_name","aw_product_id","search_price"\n"u","n","7","5.50"\n'
    assert feed.parse_feed(raw) == {"7": 5.5}


def test_parse_feed_error_says_what_arrived():
    with pytest.raises(feed.FeedError, match=r"HTML.*Error 500"):
        feed.parse_feed(b"<html>Error 500</html>")
    with pytest.raises(feed.FeedError, match="XML"):
        feed.parse_feed(b'<?xml version="1.0"?><products></products>')
    with pytest.raises(feed.FeedError, match="bytes"):
        feed.parse_feed(b"nombre,precio\n1,2\n")


def zipped(data, name="feed.csv"):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return buf.getvalue()


def test_parse_feed_accepts_zip_like_awin_delivers():
    raw = csv_bytes([["http://a", "Uno", "1", "9.99", "new", "1"]])
    assert feed.parse_feed(zipped(raw)) == {"1": 9.99}
    assert feed.parse_feed(zipped(raw, name="PowerPlanet_ES.csv")) == {"1": 9.99}


def test_parse_feed_broken_zip_is_a_feed_error():
    with pytest.raises(feed.FeedError, match="zip"):
        feed.parse_feed(b"PK\x03\x04esto no es un zip")


def test_parse_feed_rejects_garbage():
    with pytest.raises(feed.FeedError):
        feed.parse_feed(b"<html>Error 500</html>")
    with pytest.raises(feed.FeedError):
        feed.parse_feed(b"aw_product_id,nombre\n1,x\n")  # sin search_price


# ---------------------------------------------------------------- historial

def test_update_history_records_only_changes():
    hist = {}
    stats = feed.update_history(hist, {"1": 10.0, "2": 20.0}, D1)
    assert stats["nuevos"] == 2 and hist["first"] == "2026-11-09"
    stats = feed.update_history(hist, {"1": 10.0, "2": 15.0}, D1 + timedelta(days=1))
    assert stats["cambiaron"] == 1 and stats["bajaron10"] == 1
    d0 = feed.day_no(D1)
    assert hist["p"]["1"] == [d0 + 1, d0, 10.0]                   # sin cambios: una sola pareja
    assert hist["p"]["2"] == [d0 + 1, d0, 20.0, d0 + 1, 15.0]


def test_update_history_trims_old_pairs_but_keeps_price_at_cutoff():
    hist = {}
    feed.update_history(hist, {"1": 10.0}, D1)
    feed.update_history(hist, {"1": 12.0}, D1 + timedelta(days=10))
    later = D1 + timedelta(days=60)
    feed.update_history(hist, {"1": 12.0}, later)
    d0 = feed.day_no(D1)
    # la pareja del dia 0 (10 EUR) ya es muy antigua; queda la del dia 10, vigente al empezar la ventana
    assert hist["p"]["1"] == [feed.day_no(later), d0 + 10, 12.0]


def test_update_history_prunes_products_that_left_the_feed():
    hist = {}
    feed.update_history(hist, {"1": 10.0, "2": 10.0}, D1)
    stats = feed.update_history(hist, {"1": 10.0}, D1 + timedelta(days=60))
    assert stats["podados"] == 1 and "2" not in hist["p"] and "1" in hist["p"]


def test_save_and_load_roundtrip_one_product_per_line(tmp_path):
    path = tmp_path / "state" / "feed.json"
    hist = {}
    feed.update_history(hist, {"2": 5.5, "1": 10.0}, D1)
    feed.save_history(path, hist)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and lines[1].startswith('"1":') and lines[2].startswith('"2":')
    assert feed.load_history(path) == hist
    assert feed.history_days(hist, D1 + timedelta(days=3)) == 4


def test_save_history_with_only_retry_marker_is_valid_json(tmp_path):
    path = tmp_path / "f.json"
    feed.save_history(path, {"p": {}})
    assert json.loads(path.read_text(encoding="utf-8")) == {"p": {}}
    feed.save_history(path, {"p": {}, "retry_at": "x"})
    assert json.loads(path.read_text(encoding="utf-8"))["retry_at"] == "x"


def test_load_history_survives_missing_or_broken_file(tmp_path):
    assert feed.load_history(tmp_path / "no.json") == {"p": {}}
    bad = tmp_path / "bad.json"
    bad.write_text("{{{")
    assert feed.load_history(bad) == {"p": {}}


# ---------------------------------------------------------------- collect

def fake_fetch(prices):
    rows = [["u", "n", pid, f"{p:.2f}", "new", "1"] for pid, p in prices.items()]
    return lambda url: csv_bytes(rows)


def test_collect_runs_once_a_day(tmp_path):
    path = tmp_path / "f.json"
    msg = feed.collect("http://x", path, D1, NOW, fetch=fake_fetch(many()))
    assert "600 productos" in msg and "1 dias de historial" in msg

    def boom(url):
        raise AssertionError("no debe descargar dos veces el mismo dia")
    assert feed.collect("http://x", path, D1, NOW, fetch=boom) == "feed ya recopilado hoy"
    msg = feed.collect("http://x", path, D1 + timedelta(days=1), NOW, fetch=fake_fetch(many(price=9.0)))
    assert "600 con otro precio" in msg and "600 bajaron" in msg and "2 dias" in msg


def test_collect_failure_waits_an_hour_and_never_raises(tmp_path):
    path = tmp_path / "f.json"
    calls = []

    def fail(url):
        calls.append(1)
        raise feed.FeedError("no se pudo descargar el feed (HTTP 503)")
    assert "HTTP 503" in feed.collect("http://x", path, D1, NOW, fetch=fail)
    assert feed.collect("http://x", path, D1, NOW + timedelta(minutes=30), fetch=fail) == "feed: reintento aplazado"
    assert len(calls) == 1
    feed.collect("http://x", path, D1, NOW + timedelta(minutes=61), fetch=fail)
    assert len(calls) == 2
    # y cuando por fin responde, se recopila y se borra el aplazamiento
    feed.collect("http://x", path, D1, NOW + timedelta(minutes=125), fetch=fake_fetch(many()))
    assert feed.load_history(path)["retry_at"] is None and feed.load_history(path)["n"] == 600


def test_collect_ignores_a_truncated_feed(tmp_path):
    path = tmp_path / "f.json"
    feed.collect("http://x", path, D1, NOW, fetch=fake_fetch(many(1000)))
    msg = feed.collect("http://x", path, D1 + timedelta(days=1), NOW, fetch=fake_fetch(many(480)))
    assert "se ignora" in msg
    assert feed.load_history(path)["updated"] == "2026-11-09"  # el historial no se toca
    msg = feed.collect("http://x", path, D1 + timedelta(days=1), NOW + timedelta(hours=2), fetch=fake_fetch(many(10)))
    assert "solo 10" in msg


def test_download_errors_never_contain_the_url(monkeypatch):
    secret_url = "https://productdata.awin.com/datafeed/download/apikey/abcdef0123456789abcdef0123456789/x.csv"
    monkeypatch.setattr(feed, "sleep", lambda s: None)

    def boom(url, timeout):
        raise feed.requests.ConnectionError(f"fallo con {url}")
    monkeypatch.setattr(feed.requests, "get", boom)
    with pytest.raises(feed.FeedError) as err:
        feed.download(secret_url)
    assert "abcdef0123456789" not in str(err.value) and "ConnectionError" in str(err.value)


def test_download_retries_then_returns_content(monkeypatch):
    monkeypatch.setattr(feed, "sleep", lambda s: None)
    answers = [503, 200]

    class R:
        def __init__(self, code):
            self.status_code, self.content = code, b"datos"
    monkeypatch.setattr(feed.requests, "get", lambda url, timeout: R(answers.pop(0)))
    assert feed.download("http://x") == b"datos"


# ---------------------------------------------------------------- integracion con el bot

def test_collect_feeds_skips_without_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("AWIN_FEED_URL", raising=False)
    monkeypatch.delenv("ALL_SECRETS", raising=False)
    monkeypatch.setattr(feed, "collect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debe llamarse")))
    deals.collect_feeds()


def test_collect_feeds_hides_the_key_and_never_raises(monkeypatch, caplog):
    key = "0123456789abcdef0123456789abcdef"
    monkeypatch.setenv("AWIN_FEED_URL", f"https://productdata.awin.com/datafeed/download/apikey/{key}/x.csv")

    def boom(*a, **k):
        raise RuntimeError(f"fallo {a[0]}")
    monkeypatch.setattr(feed, "collect", boom)
    deals._secrets.clear()
    with caplog.at_level("ERROR", logger="deals"):
        deals.collect_feeds()
    assert "feed: error inesperado RuntimeError" in caplog.text
    assert key in deals._secrets
    assert key not in deals.scrub(f"https://x/apikey/{key}/")
