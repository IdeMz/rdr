from datetime import date, timedelta

import pytest

import pricehist as ph

D0 = date(2026, 11, 1)


def day(n):
    return D0 + timedelta(days=n)


def feed(hist, series, asin="B000000001"):
    """series: lista de (dia, precio) vistos."""
    for n, price in series:
        ph.record(hist, {asin: price}, day(n))


def test_record_new_product_and_same_day_keeps_lowest():
    hist = {"p": {}}
    stats = ph.record(hist, {"A": 25.99, "B": 0, "C": None}, day(0))
    assert stats == {"vistos": 1, "nuevos": 1, "podados": 0}
    ph.record(hist, {"A": 21.7}, day(0))   # mas bajo el mismo dia: se queda
    ph.record(hist, {"A": 30.0}, day(0))   # mas alto el mismo dia: se ignora
    assert hist["p"]["A"] == [ph.day_no(day(0)), 1, ph.day_no(day(0)), 21.7]


def test_record_adds_pair_only_when_price_changes():
    hist = {"p": {}}
    feed(hist, [(0, 20.0), (1, 20.0), (2, 20.0), (3, 25.0), (4, 25.0)], "A")
    entry = hist["p"]["A"]
    assert entry[0] == ph.day_no(day(4)) and entry[1] == 5          # ultimo dia visto y dias vistos
    assert entry[2:] == [ph.day_no(day(0)), 20.0, ph.day_no(day(3)), 25.0]


def test_reference_needs_enough_evidence():
    hist = {"p": {}}
    feed(hist, [(0, 20.0), (1, 20.0)], "A")
    assert ph.reference(hist["p"]["A"], day(1)) is None             # pocos dias vistos
    feed(hist, [(2, 20.0)], "A")
    assert ph.reference(hist["p"]["A"], day(2)) is None             # 3 dias vistos pero solo 2 de margen
    feed(hist, [(6, 20.0)], "A")
    assert ph.reference(hist["p"]["A"], day(6))["low30"] == 20.0
    assert ph.reference(None, day(6)) is None
    assert ph.reference([1, 2, 3], day(6)) is None


def test_reference_lg_case_price_at_top_of_its_range():
    """LG: casi todo el mes a 379, con una bajada a 347,78: hoy 379 esta un 9 % por encima del minimo."""
    hist = {"p": {}}
    series = [(n, 379.0) for n in range(0, 14)] + [(n, 347.78) for n in range(14, 17)] + [(n, 379.0) for n in range(17, 21)]
    feed(hist, series, "LG")
    ref = ph.reference(hist["p"]["LG"], day(20))
    assert ref["low30"] == 347.78 and ref["median"] == 379.0
    assert ref["low_ago"] == 4  # visto por ultima vez hace 4 dias (dia 16 -> hoy dia 20)


def test_reference_median_is_weighted_by_days():
    hist = {"p": {}}
    feed(hist, [(0, 100.0), (1, 100.0), (2, 100.0), (3, 100.0), (4, 100.0), (5, 60.0), (6, 100.0)], "A")
    ref = ph.reference(hist["p"]["A"], day(6))
    assert ref["low30"] == 60.0 and ref["median"] == 100.0


def test_reference_ignores_prices_older_than_the_window():
    hist = {"p": {}}
    # precio bajo hace 40 dias; despues siempre 50
    feed(hist, [(0, 10.0), (1, 10.0), (2, 10.0), (3, 50.0)] + [(n, 50.0) for n in range(4, 43)], "A")
    ref = ph.reference(hist["p"]["A"], day(42))
    assert ref["low30"] == 50.0


def test_record_prunes_forgotten_products_and_old_pairs():
    hist = {"p": {}}
    feed(hist, [(0, 10.0)], "VIEJO")
    feed(hist, [(0, 10.0), (1, 12.0), (2, 12.0), (3, 12.0), (4, 15.0)] + [(n, 15.0) for n in range(5, 39)], "A")
    assert "VIEJO" not in hist["p"]                                  # llevaba mas de 35 dias sin verse
    entry = hist["p"]["A"]
    pairs = [(entry[i], entry[i + 1]) for i in range(2, len(entry), 2)]
    # la pareja del dia 1 (12) era la vigente cuando empezaba el periodo guardado, se conserva; la del 0 no
    assert pairs[0][1] == 12.0 and pairs[-1][1] == 15.0
    assert all(d >= ph.day_no(day(1)) for d, _ in pairs)


def test_save_load_roundtrip_one_product_per_line(tmp_path):
    path = tmp_path / "precios.json"
    hist = {"p": {}}
    feed(hist, [(0, 20.0), (1, 21.5)], "A")
    feed(hist, [(0, 5.0)], "B")
    ph.save(path, hist)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and lines[1].startswith('"A":') and lines[2].startswith('"B":')
    assert ph.load(path)["p"] == hist["p"]


def test_load_survives_missing_or_corrupt_file(tmp_path):
    assert ph.load(tmp_path / "no.json") == {"p": {}}
    bad = tmp_path / "bad.json"
    bad.write_text("{no es json", encoding="utf-8")
    assert ph.load(bad) == {"p": {}}
    bad.write_text('{"p":{"A":[1,2,3],"B":"x","C":[9,1,9,5.5]}}', encoding="utf-8")
    assert set(ph.load(bad)["p"]) == {"C"}                           # descarta lo mal formado, conserva lo bueno


@pytest.mark.parametrize("seen,span,ok", [(3, 5, True), (2, 9, False), (9, 4, False)])
def test_reference_thresholds(seen, span, ok):
    entry = [ph.day_no(day(span)), seen, ph.day_no(day(0)), 10.0]
    assert (ph.reference(entry, day(span)) is not None) is ok
