import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import deals
import review
import storeposts as sp

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)  # 13:00 en Madrid
MADRID = ZoneInfo("Europe/Madrid")


def cand(pid="1000", price=70.0, **kw):
    base = {"id": pid, "name": f"Auriculares Modelo{pid} inalambricos", "price": price, "rrp": 100.0, "link": f"https://awin/p/{pid}",
            "image": "https://img/x.jpg", "stock": 4, "foreign": False, "score": 5, "page": f"https://pp/{pid}"}
    base.update(kw)
    return base


def cross(pid="1000", price=70.0, verdict="oferta_real", t=NOW):
    return {"items": {pid: {"t": t.isoformat(), "pp_price": price, "motivo": "-12 % bajo Amazon", "veredicto": verdict}}}


def picks(*cands, day="2026-11-20"):
    return {"day": day, "candidates": list(cands)}


# ------------------------------------------------------------ cupo y suerte

def test_counts_today_only_counts_the_stores_posts_of_that_local_day():
    state = {
        "pp:1": {"t": NOW.isoformat(), "st": "POWERPLANET"},
        "pp:2": {"t": (NOW - timedelta(days=1)).isoformat(), "st": "POWERPLANET"},
        "ae:3": {"t": NOW.isoformat(), "st": "ALIEXPRESS"},
        "B0AAAAAAAA": {"t": NOW.isoformat()},
    }
    assert sp.counts_today(state, date(2026, 11, 20), MADRID) == {"powerplanet": 1, "aliexpress": 1}


def test_remaining_only_lists_stores_with_quota_left():
    assert sp.remaining({"powerplanet": 2, "aliexpress": 2}, {"powerplanet": 2, "aliexpress": 1}) == {"aliexpress": 1}
    assert sp.remaining({"powerplanet": 2}, {}) == {"powerplanet": 2}


def test_probability_grows_as_the_window_closes_and_is_certain_at_the_end():
    early = sp.probability(4, 900, 27.5)
    late = sp.probability(4, 60, 27.5)
    assert 0 < early < late <= 1
    assert sp.probability(1, 0, 27.5) == 1.0
    assert sp.probability(0, 500, 27.5) == 0.0


def test_draw_uses_the_given_random_source():
    assert sp.draw(2, 100, 25, rng=lambda: 0.0) is True
    assert sp.draw(2, 100, 25, rng=lambda: 0.999) is False


def test_store_order_puts_the_most_behind_store_first():
    daily = {"powerplanet": 2, "aliexpress": 2}
    assert sp.store_order({"powerplanet": 1, "aliexpress": 2}, daily, {"powerplanet": 1, "aliexpress": 0}) == ["aliexpress", "powerplanet"]
    assert sp.store_order({"powerplanet": 2, "aliexpress": 2}, daily, {}) == ["powerplanet", "aliexpress"]


# ------------------------------------------------------------ ficha

@pytest.mark.parametrize("text,expected", [
    ("Recíbelo el martes 24 nov", 4),
    ("Entrega estimada: 27 de noviembre", 7),
    ("Entrega en 24/48 h", None),
    ("", None),
])
def test_delivery_days(text, expected):
    assert sp.delivery_days(text, date(2026, 11, 20)) == expected


def test_delivery_days_rolls_into_next_year():
    assert sp.delivery_days("2 ene", date(2026, 12, 30)) == 3


def test_page_sold_out_looks_at_structured_data_and_explicit_phrases_only():
    assert sp.page_sold_out('<link itemprop="availability" href="https://schema.org/OutOfStock">')
    assert sp.page_sold_out("<p>Producto agotado</p>")
    assert not sp.page_sold_out("<nav>Ofertas agotadas no, lo mejor de la semana</nav><p>En stock</p>")


# ------------------------------------------------------------ candidatos de PowerPlanet

def test_only_real_offers_confirmed_recently_with_the_same_price_are_candidates():
    pool = picks(cand("1"), cand("2"), cand("3"), cand("4"), cand("5"), cand("6", stock=0), cand("7", foreign=True))
    x = {"items": {
        "1": cross("1")["items"]["1"],
        "2": cross("2", verdict="dudoso")["items"]["2"],
        "3": cross("3", t=NOW - timedelta(days=5))["items"]["3"],
        "4": cross("4", price=80.0)["items"]["4"],                      # el precio cambio despues del cruce
        "6": cross("6")["items"]["6"], "7": cross("7")["items"]["7"],
    }}
    out = sp.pp_candidates(pool, x, {}, NOW)
    assert [c["id"] for c in out] == ["1"]
    c = out[0]
    assert c["key"] == "pp:1" and c["store"] == "powerplanet" and c["old"] == 100.0 and c["page"] == "https://pp/1"
    assert c["url"] == "https://awin/p/1"                                # enlace de afiliado sin tocar


def test_pp_candidates_skip_published_ones_and_stale_lists():
    pool = picks(cand("1"), cand("2"))
    x = {"items": {**cross("1")["items"], **cross("2")["items"]}}
    assert [c["id"] for c in sp.pp_candidates(pool, x, {"pp:1": {}}, NOW)] == ["2"]
    assert sp.pp_candidates(picks(cand("1"), day="2026-11-10"), x, {}, NOW) == []


def test_pp_candidates_best_score_first():
    pool = picks(cand("1", score=3), cand("2", score=9))
    x = {"items": {**cross("1")["items"], **cross("2")["items"]}}
    assert [c["id"] for c in sp.pp_candidates(pool, x, {}, NOW)] == ["2", "1"]


# ------------------------------------------------------------ candidatos de AliExpress

def rev_item(pid="55", st="ok", age_days=1, **kw):
    base = {"src": "a", "id": pid, "st": st, "t": (NOW - timedelta(days=age_days)).isoformat(), "title": f"Altavoz {pid}",
            "price": 20.0, "drop": 20, "rating": 95.0, "image": "https://img/a.jpg", "link": "https://s.click.aliexpress.com/e/x",
            "score": 4, "shop_id": "9"}
    base.update(kw)
    return base


def test_only_approved_recent_unpublished_aliexpress_items_are_candidates():
    rev = {"items": {"a1": rev_item("1"), "a2": rev_item("2", st="new"), "a3": rev_item("3", st="no"),
                     "a4": rev_item("4", age_days=6), "a5": rev_item("5"), "a6": rev_item("6", st="pub")}}
    out = sp.ae_candidates(rev, {"ae:5": {}}, NOW)
    assert [c["id"] for c in out] == ["1"]
    c = out[0]
    assert c["key"] == "ae:1" and c["rid"] == "a1" and c["rating"] == 4.8 and c["old"] == 25.0
    assert c["url"] == "https://s.click.aliexpress.com/e/x"
