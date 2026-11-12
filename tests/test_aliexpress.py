import hashlib
import json
from datetime import date

import pytest
import requests

import aliexpress as ax
import feed

TODAY = date(2026, 11, 12)
KEY, SECRET, TRACK = "appkey123", "supersecret456", "mitracking"


class FakeResp:
    def __init__(self, data):
        self.data = data

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def product(pid, price, **kw):
    base = {"product_id": pid, "product_title": f"Producto {pid} auriculares", "target_sale_price": str(price),
            "target_original_price": str(price * 2), "promotion_link": f"https://s.click/{pid}",
            "product_main_image_url": "https://img/x.jpg", "evaluate_rate": "94.5%", "lastest_volume": "1200",
            "second_level_category_name": "Audio"}
    base.update(kw)
    return base


def ok(method, products):
    return {method.replace(".", "_") + "_response": {"resp_result": {"resp_code": 200, "resp_msg": "ok",
            "result": {"products": {"product": products}}}}}


def poster(products_by_call=None, error_for=None, calls=None):
    def post(url, params=None, data=None, timeout=None):
        merged = {**(params or {}), **(data or {})}
        if calls is not None:
            calls.append(merged)
        method = merged["method"]
        if error_for and error_for(merged):
            return FakeResp({"error_response": {"code": "IllegalParam", "msg": "bad"}})
        return FakeResp(ok(method, (products_by_call or [product("1", 10.0)])))
    return post


# ---------------------------------------------------------------- firma y llamada

def test_sign_is_md5_with_the_secret_around_the_sorted_pairs_uppercase():
    params = {"b": "2", "a": "1", "method": "m"}
    expected = hashlib.md5(b"seca1b2methodmsec").hexdigest().upper()
    assert ax.sign(params, "sec") == expected


def test_call_puts_system_params_in_the_url_and_query_params_in_the_body_and_signs_everything():
    seen = {}

    def post(url, params=None, data=None, timeout=None):
        seen.update(url=url, params=dict(params), data=dict(data))
        return FakeResp(ok(ax.HOT_METHOD, [product("1", 10.0)]))
    ax.call(ax.HOT_METHOD, {"keywords": "tv", "page_no": 1, "x": None, "y": ""}, KEY, SECRET,
            post=post, now=lambda: 1700000000.5)
    sysp, body = seen["params"], seen["data"]
    assert seen["url"] == ax.ENDPOINT and body == {"keywords": "tv", "page_no": "1"}
    assert sysp["app_key"] == KEY and sysp["timestamp"] == "1700000000500" and sysp["sign_method"] == "md5"
    assert sysp["v"] == "2.0" and sysp["format"] == "json" and sysp["method"] == ax.HOT_METHOD
    signed = {k: v for k, v in sysp.items() if k != "sign"} | body
    assert sysp["sign"] == ax.sign(signed, SECRET)
    assert SECRET not in json.dumps([sysp, body])


def test_call_raises_api_error_on_error_response_bad_code_and_network_problems():
    with pytest.raises(ax.ApiError, match="IllegalParam"):
        ax.call(ax.HOT_METHOD, {}, KEY, SECRET, post=poster(error_for=lambda d: True))
    bad = {"aliexpress_affiliate_hotproduct_query_response": {"resp_result": {"resp_code": 405, "resp_msg": "nope"}}}
    with pytest.raises(ax.ApiError, match="405"):
        ax.call(ax.HOT_METHOD, {}, KEY, SECRET, post=lambda *a, **k: FakeResp(bad))
    with pytest.raises(ax.ApiError):
        ax.call(ax.HOT_METHOD, {}, KEY, SECRET, post=lambda *a, **k: FakeResp(ValueError("no json")))

    def boom(*a, **k):
        raise requests.ConnectionError("sin red")
    with pytest.raises(ax.ApiError, match="ConnectionError"):
        ax.call(ax.HOT_METHOD, {}, KEY, SECRET, post=boom)


# ---------------------------------------------------------------- lectura de productos

def test_find_products_looks_through_nested_structures():
    p = [product("1", 10.0)]
    assert ax.find_products(ok(ax.HOT_METHOD, p)) == p
    assert ax.find_products({"a": {"b": {"products": p}}}) == p
    assert ax.find_products({"result": {"products": {"product": []}}}) == []
    assert ax.find_products("nada") == []


def test_item_from_converts_fields_and_rejects_unusable_products():
    item = ax.item_from(product("77", "12.50", ship_to_days="3", target_original_price="25.00"))
    assert item["id"] == "77" and item["price"] == 12.5 and item["rrp"] == 25.0 and item["ship_days"] == 3
    assert item["rating"] == 94.5 and item["reviews"] == 1200 and item["link"] == "https://s.click/77"
    assert item["foreign"] is False and item["stock"] is None
    assert ax.item_from(product("78", 9.0, ship_to_days=None))["ship_days"] is None
    assert ax.item_from(product("", 9.0)) is None
    assert ax.item_from(product("79", "abc")) is None
    assert ax.item_from(product("80", "1.299,99"))["price"] == 1299.99


def test_origin_is_read_from_any_shipping_origin_field():
    assert ax.item_from(product("1", 9.0, ship_from_country="ES"))["from_europe"] is True
    assert ax.item_from(product("2", 9.0, ship_from="China"))["from_europe"] is False
    assert ax.item_from(product("5", 9.0, ship_from_country="PL"))["from_europe"] is True
    assert ax.item_from(product("6", 9.0, ship_from_country="Germany"))["from_europe"] is True
    assert ax.item_from(product("3", 9.0, warehouse="Spain"))["from_europe"] is True
    unknown = ax.item_from(product("4", 9.0))
    assert unknown["from_europe"] is None and unknown["ship_from"] is None


def test_keywords_come_from_the_channel_search_urls(tmp_path):
    ch = tmp_path / "c.json"
    ch.write_text(json.dumps({"categories": [{"url": "/s?k=smartphone+libre"}, {"url": "/s?k=auriculares%20bluetooth"},
                                             {"url": "/s?k=smartphone+libre"}, {"url": "/sin-busqueda"}]}))
    assert ax.keywords_from_channel(ch) == ["smartphone libre", "auriculares bluetooth"]
    assert ax.keywords_from_channel(tmp_path / "no.json") == []


# ---------------------------------------------------------------- recogida

def test_gather_queries_each_keyword_with_the_delivery_filter_and_dedupes():
    calls = []
    items, stats = ax.gather(KEY, SECRET, TRACK, ["tv", "movil"], post=poster([product("1", 10.0)], calls=calls),
                             sleep=lambda s: None)
    assert list(items) == ["1"] and stats["llamadas"] == ax.GLOBAL_PAGES + 2 and stats["errores"] == 0
    assert calls[0]["ship_to_country"] == "ES" and calls[0]["target_currency"] == "EUR"
    assert calls[0]["tracking_id"] == TRACK and calls[0]["sign_method"] == "md5"
    assert calls[0]["method"] == ax.SEARCH_METHOD and calls[0]["delivery_days"] == ax.DELIVERY_DAYS == "5"
    assert [c["keywords"] for c in calls] == ["tv", "movil"]


def test_gather_survives_isolated_errors_but_gives_up_after_several_in_a_row():
    state = {"n": 0}

    def flaky(data):
        state["n"] += 1
        return state["n"] == 2
    items, stats = ax.gather(KEY, SECRET, TRACK, ["a", "b"], post=poster(error_for=flaky), sleep=lambda s: None)
    assert stats["errores"] == 1 and items

    items, stats = ax.gather(KEY, SECRET, TRACK, ["a"] * 10, post=poster(error_for=lambda d: True), sleep=lambda s: None)
    assert items == {} and stats["errores"] == ax.MAX_ERRORS and stats["llamadas"] == ax.MAX_ERRORS


def test_probe_compares_variants_by_product_ids_and_hides_secrets():
    calls = []

    def post(url, params=None, data=None, timeout=None):
        merged = {**(params or {}), **(data or {})}
        calls.append(merged)
        if merged.get("keywords") == "vacia" and merged.get("delivery_days"):
            return FakeResp({"x_response": {"resp_result": {"resp_code": 405, "resp_msg": "empty"}}})
        filtered = bool(merged.get("delivery_days")) and bool(merged.get("min_sale_price"))
        rows = [product("1", 12.0, ship_to_days="Ship to ES in 5 days")]
        if not filtered:
            rows.append(product("2", 5.0))
        return FakeResp(ok(merged["method"], rows))
    out = ax.probe(KEY, SECRET, TRACK, "tv", post=post, sleep=lambda s: None, empty_keyword="vacia")
    main = out["busqueda_actual"]
    assert main["ok"] and main["ids"] == ["1"] and "ship_to_days" in main["fields"] and main["precios"] == ["12.0"]
    assert "promotion_link" not in main["sample"][0]
    assert out["sin_ningun_filtro"]["ids"] == ["1", "2"]
    assert out["entrega_3_dias"]["params"]["delivery_days"] == "3"
    assert "delivery_days" not in out["sin_filtro_de_entrega"]["params"]
    assert out["palabra_vacia_con_filtros"]["count"] == 0 and out["palabra_vacia_sin_filtros"]["count"] == 2
    summary = out["resumen"]
    assert summary["el_filtro_de_entrega_cambia_los_resultados"] is True
    assert summary["productos_con_filtros"] == 1 and summary["productos_sin_filtros"] == 2
    text = json.dumps(out)
    assert SECRET not in text and KEY not in text and TRACK not in text


def test_probe_words_pick_the_richest_keyword_and_the_first_empty_one():
    assert ax.probe_words(["a", "b", "c", "d"], {"a": 0, "b": 5, "c": 50, "d": 0}) == ("c", "a")
    assert ax.probe_words(["a"], {"a": 3}) == ("a", "")
    assert ax.probe_words([], {}) == ("", "")


def test_report_filters_out_few_sales_and_low_rating(tmp_path):
    ch = channel_file(tmp_path)
    creds = (KEY, SECRET, TRACK)
    for n, price in enumerate([40.0, 40.0, 40.0, 28.0]):
        day = date(2026, 11, 9 + n)
        batch = [product("1", price, product_title="Auriculares Xiaomi Redmi Buds"),
                 product("2", price, product_title="Altavoz Anker Soundcore Mini", lastest_volume="3"),
                 product("3", price, product_title="Cargador Baseus GaN 65W rapido", evaluate_rate="80.0%"),
                 product("4", price, product_title="Teclado Logitech MX Keys Mini", evaluate_rate=None)]
        ax.run(creds, tmp_path, ch, day, 8, post=poster(batch), sleep=lambda s: None)
    data = json.loads((tmp_path / ax.PICKS_FILE).read_text())
    assert [c["id"] for c in data["candidates"]] == ["1"]
    assert data["stats"]["pocas ventas"] == 1 and data["stats"]["valoracion baja"] == 2


def test_collection_asks_for_the_minimum_price_in_cents():
    calls = []
    ax.gather(KEY, SECRET, TRACK, ["tv"], post=poster([product("1", 10.0)], calls=calls), sleep=lambda s: None)
    assert calls[0]["min_sale_price"] == ax.MIN_PRICE_CENTS == "1000"


# ---------------------------------------------------------------- recogida diaria completa

def channel_file(tmp_path):
    ch = tmp_path / "ch.json"
    ch.write_text(json.dumps({"categories": [{"url": "/s?k=auriculares"}]}))
    return ch


def test_run_waits_for_the_hour_and_collects_once_a_day(tmp_path):
    ch = channel_file(tmp_path)
    creds = (KEY, SECRET, TRACK)
    calls = []
    post = poster([product("1", 10.0)], calls=calls)
    assert "a partir de las" in ax.run(creds, tmp_path, ch, TODAY, 5, post=post, sleep=lambda s: None)
    assert calls == []
    text = ax.run(creds, tmp_path, ch, TODAY, 7, post=post, sleep=lambda s: None)
    assert text.startswith("aliexpress: 1 productos") and "1 nuevos" in text
    n = len(calls)
    assert "ya recogidos" in ax.run(creds, tmp_path, ch, TODAY, 9, post=post, sleep=lambda s: None)
    assert len(calls) == n                                      # sin llamadas nuevas el mismo dia
    hist = feed.load_history(tmp_path / ax.HIST_FILE)
    assert hist["p"]["1"][1:] == [feed.day_no(TODAY), 10.0] and hist["updated"] == "2026-11-12"
    assert json.loads((tmp_path / ax.PROBE_FILE).read_text())["day"] == "2026-11-12"
    assert json.loads((tmp_path / ax.PICKS_FILE).read_text())["mode"] == "informe"


def test_run_with_no_products_writes_nothing_and_retries(tmp_path):
    text = ax.run((KEY, SECRET, TRACK), tmp_path, channel_file(tmp_path), TODAY, 8,
                  post=poster(error_for=lambda d: True), sleep=lambda s: None)
    assert "sin productos" in text and "se reintenta" in text
    assert not (tmp_path / ax.HIST_FILE).exists()
    status = json.loads((tmp_path / ax.STATUS_FILE).read_text())
    assert status["errores"] == 1 and status["muestra_de_errores"] == ["IllegalParam: bad"]
    assert SECRET not in json.dumps(status) and KEY not in json.dumps(status)
    before = (tmp_path / ax.STATUS_FILE).stat().st_mtime_ns
    ax.run((KEY, SECRET, TRACK), tmp_path, channel_file(tmp_path), TODAY, 8, post=poster(error_for=lambda d: True),
           sleep=lambda s: None)
    assert (tmp_path / ax.STATUS_FILE).stat().st_mtime_ns == before      # igual que antes: no se reescribe


def test_report_finds_a_real_drop_with_the_lower_price_floor(tmp_path):
    ch = channel_file(tmp_path)
    creds = (KEY, SECRET, TRACK)
    for n, price in enumerate([20.0, 20.0, 20.0, 14.0]):
        day = date(2026, 11, 9 + n)
        ax.run(creds, tmp_path, ch, day, 8, post=poster([product("1", price), product("2", 8.0)]), sleep=lambda s: None)
    data = json.loads((tmp_path / ax.PICKS_FILE).read_text())
    assert [c["id"] for c in data["candidates"]] == ["1"] and data["candidates"][0]["drop"] == 30
    assert data["stats"]["precio sin cambios"] == 1                # el de 8 EUR no ha bajado nunca


def test_report_drops_products_shipped_from_abroad_and_keeps_unknown_origin(tmp_path):
    ch = channel_file(tmp_path)
    creds = (KEY, SECRET, TRACK)
    for n, price in enumerate([20.0, 20.0, 20.0, 14.0]):
        day = date(2026, 11, 9 + n)
        batch = [product("1", price, ship_from_country="ES", product_title="Auriculares Xiaomi Redmi Buds"),
                 product("2", price, ship_from_country="CN", product_title="Altavoz Anker Soundcore Mini"),
                 product("3", price, product_title="Cargador Baseus GaN 65W rapido")]
        ax.run(creds, tmp_path, ch, day, 8, post=poster(batch), sleep=lambda s: None)
    data = json.loads((tmp_path / ax.PICKS_FILE).read_text())
    assert sorted(c["id"] for c in data["candidates"]) == ["1", "3"]
    assert {c["id"]: c["from_europe"] for c in data["candidates"]} == {"1": True, "3": None}
    assert data["stats"]["no envia desde Europa"] == 1
    assert data["origin_known"] == 2 and data["from_europe"] == 1


def test_run_repeats_an_outdated_probe_once_even_if_the_day_is_already_collected(tmp_path):
    ch = channel_file(tmp_path)
    creds = (KEY, SECRET, TRACK)
    calls = []
    post = poster([product("1", 10.0)], calls=calls)
    ax.run(creds, tmp_path, ch, TODAY, 7, post=post, sleep=lambda s: None)
    probe_path = tmp_path / ax.PROBE_FILE
    assert json.loads(probe_path.read_text())["version"] == ax.PROBE_VERSION
    old = json.loads(probe_path.read_text())
    old["version"] = 1
    probe_path.write_text(json.dumps(old))
    n = len(calls)
    before = (tmp_path / ax.HIST_FILE).read_text()
    assert "sondeo actualizado" in ax.run(creds, tmp_path, ch, TODAY, 8, post=post, sleep=lambda s: None)
    assert len(calls) > n and json.loads(probe_path.read_text())["version"] == ax.PROBE_VERSION
    assert (tmp_path / ax.HIST_FILE).read_text() == before            # el historial no se toca
    assert json.loads((tmp_path / ax.STATUS_FILE).read_text())["consultas"]
    n = len(calls)
    assert "ya recogidos" in ax.run(creds, tmp_path, ch, TODAY, 9, post=post, sleep=lambda s: None)
    assert len(calls) == n


def test_empty_results_are_not_errors_and_per_query_counts_are_recorded():
    def post(url, params=None, data=None, timeout=None):
        merged = {**(params or {}), **(data or {})}
        if merged.get("keywords") == "sin resultados":
            return FakeResp({"aliexpress_affiliate_hotproduct_query_response": {"resp_result": {
                "resp_code": 405, "resp_msg": "The result is empty"}}})
        return FakeResp(ok(merged["method"], [product("1", 10.0, ship_to_days="3", ship_from_country="ES")]))
    items, stats = ax.gather(KEY, SECRET, TRACK, ["sin resultados", "con resultados"], post=post, sleep=lambda s: None)
    assert stats["errores"] == 0 and stats["vacias"] == 1 and stats["llamadas"] == ax.GLOBAL_PAGES + 2
    assert stats["consultas"]["sin resultados"] == 0 and stats["consultas"]["con resultados"] == 1
    assert "ship_from_country" in stats["campos"] and "ship_to_days" in stats["campos"]
    assert stats["muestra_productos"][0]["product_id"] == "1" and "promotion_link" not in stats["muestra_productos"][0]
    assert items["1"]["from_europe"] is True


def test_many_empty_queries_in_a_row_do_not_abort_the_day():
    def post(url, params=None, data=None, timeout=None):
        merged = {**(params or {}), **(data or {})}
        if merged.get("keywords") != "buena":
            return FakeResp({"x_response": {"resp_result": {"resp_code": 405, "resp_msg": "empty"}}})
        return FakeResp(ok(merged["method"], [product("1", 10.0)]))
    items, stats = ax.gather(KEY, SECRET, TRACK, ["a", "b", "c", "d", "e", "buena"], post=post, sleep=lambda s: None)
    assert items and stats["errores"] == 0 and stats["vacias"] == 5 and stats["llamadas"] == ax.GLOBAL_PAGES + 6


def test_run_records_the_status_file_and_probes_with_a_working_keyword(tmp_path):
    ch = tmp_path / "ch.json"
    ch.write_text(json.dumps({"categories": [{"url": "/s?k=vacia"}, {"url": "/s?k=buena"}]}))
    seen = []

    def post(url, params=None, data=None, timeout=None):
        merged = {**(params or {}), **(data or {})}
        seen.append(merged.get("keywords"))
        if merged.get("keywords") == "vacia":
            return FakeResp({"x_response": {"resp_result": {"resp_code": 405, "resp_msg": "empty"}}})
        return FakeResp(ok(merged["method"], [product("1", 10.0)]))
    ax.run((KEY, SECRET, TRACK), tmp_path, ch, TODAY, 8, post=post, sleep=lambda s: None)
    status = json.loads((tmp_path / ax.STATUS_FILE).read_text())
    assert status["consultas"]["vacia"] == 0 and status["consultas"]["buena"] == 1 and status["campos"]
    probe_params = json.loads((tmp_path / ax.PROBE_FILE).read_text())["variants"]["busqueda_actual"]["params"]
    assert probe_params["keywords"] == "buena"


def test_repeated_query_adds_only_products_not_yet_in_todays_history(tmp_path):
    ch = channel_file(tmp_path)
    creds = (KEY, SECRET, TRACK)
    ax.run(creds, tmp_path, ch, TODAY, 7, post=poster([product("1", 10.0)]), sleep=lambda s: None)
    probe_path = tmp_path / ax.PROBE_FILE
    old = json.loads(probe_path.read_text())
    old["version"] = 1
    probe_path.write_text(json.dumps(old))
    batch = [product("1", 99.0), product("2", 20.0, product_title="Otro producto distinto")]
    ax.run(creds, tmp_path, ch, TODAY, 8, post=poster(batch), sleep=lambda s: None)
    hist = feed.load_history(tmp_path / ax.HIST_FILE)
    assert hist["p"]["1"][1:] == [feed.day_no(TODAY), 10.0]          # el ya anotado no se toca
    assert hist["p"]["2"][1:] == [feed.day_no(TODAY), 20.0] and hist["n"] == 2
