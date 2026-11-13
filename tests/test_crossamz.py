import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

import crossamz

NOW = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)


@dataclass
class P:
    asin: str
    title: str
    price: float | None = None
    sponsored: bool = False


def item(pid="1", ean="0840006667278", name="Corsair K65 Plus Wireless 75% MLX Red RGB Negro Teclado gaming",
         brand="Corsair", price=163.96, median=319.95, score=50.0, **kw):
    d = {"id": pid, "ean": ean, "name": name, "brand": brand, "price": price, "median": median, "drop": 49,
         "score": score}
    d.update(kw)
    return d


def ficha(price="159,99 €", extra="", availability="En stock"):
    return (f'<html><div id="availability"><span>{availability}</span></div>'
            f'<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">{price}</span></span></div>'
            f"{extra}</html>")


def write_cands(tmp_path, candidates=(), to_verify=()):
    (tmp_path / crossamz.SOURCE).write_text(json.dumps({"candidates": list(candidates), "to_verify": list(to_verify)}),
                                           encoding="utf-8")


class Fake:
    def __init__(self, results=None, pages=None):
        self.results = results or {}
        self.pages = pages or {}
        self.searches, self.fichas = [], []

    def search(self, text):
        self.searches.append(text)
        return self.results.get(text, [])

    def page(self, asin):
        self.fichas.append(asin)
        return self.pages[asin]


def ref_none(html):
    return {}


# ---- titulos ----------------------------------------------------------------

def test_same_product_by_ean_needs_brand_and_model_code():
    name, brand = "Corsair K65 Plus Wireless 75% MLX Red RGB Negro Teclado gaming", "Corsair"
    assert crossamz.same_product(name, brand, "Corsair K65 Plus Wireless Teclado Mecánico para Juegos, Negro")
    assert not crossamz.same_product(name, brand, "Logitech G915 Teclado gaming inalámbrico")   # otra marca
    assert not crossamz.same_product(name, brand, "Corsair K70 Core RGB Teclado gaming")          # otro modelo


def test_accessories_are_not_the_product():
    assert not crossamz.same_product("Samsung Galaxy Watch7 L305 40 mm 4G Verde", "Samsung",
                                     "Correa compatible con Samsung Galaxy Watch7 L305 40 mm")


def test_capacity_must_match():
    name = "SanDisk Extreme Portable V2 2TB NVMe USB 3.2 SSD externo"
    assert crossamz.same_product(name, "Sandisk", "SanDisk Extreme Portable SSD V2 2TB, hasta 1050 MB/s")
    assert not crossamz.same_product(name, "Sandisk", "SanDisk Extreme Portable SSD V2 1TB, hasta 1050 MB/s")


def test_brand_uses_first_word_and_ignores_accents():
    assert crossamz.same_product("Kingston XS2000 500GB USB-C SSD externo", "Kingston Technology",
                                 "Kingston XS2000 SSD Externo Portátil 500GB USB 3.2")


def test_strict_name_match_is_harder():
    name = "Ariete Airy Fryer Metal 7L 1800W 12 programas Negro 4628/00"
    title = "Ariete 4628 Airy Fryer Metal, freidora de aire 7 L, 1800 W"
    assert crossamz.same_product(name, "Ariete", title)
    assert not crossamz.same_product(name, "Ariete", "Ariete hervidor eléctrico 1,7 L", strict=True)


# ---- ficha ------------------------------------------------------------------

def test_page_price_reads_core_price_and_stock():
    assert crossamz.page_price(ficha("1.299,00 €")) == (1299.0, True)
    assert crossamz.page_price(ficha(availability="Actualmente no disponible."))[1] is False
    assert crossamz.page_price("<html>nada</html>") == (None, False)


# ---- veredicto --------------------------------------------------------------

@pytest.mark.parametrize("pp,before,amz,ref,expected", [
    (164.0, 320.0, 160.0, {}, "amazon_mas_barato"),
    (164.0, 320.0, 168.0, {}, "amazon_mas_barato"),           # menos de un 3 % mas caro: mismo precio
    (164.0, 320.0, 299.0, {"median": 310.0}, "oferta_real"),
    (200.0, 834.0, 215.0, {"median": 210.0}, "pvpr_inflado"),  # antes 834, pero en Amazon ronda los 210
    (200.0, 215.0, 215.0, {"median": 210.0}, "dudoso"),
    (200.0, 300.0, None, {}, "sin_precio"),
])
def test_verdict(pp, before, amz, ref, expected):
    assert crossamz.verdict(pp, before, amz, ref)[0] == expected


def test_verdict_uses_low30_when_there_is_no_median():
    # Amazon hoy en 170, pero su minimo de 30 dias es 250: el habitual es 250 y PowerPlanet a 164 es oferta
    v, _, saving = crossamz.verdict(164.0, 320.0, 250.0, {"low30": 240.0})
    assert v == "oferta_real" and saving == 34


# ---- recorrido completo -----------------------------------------------------

def test_run_finds_by_ean_and_saves_verdict(tmp_path):
    write_cands(tmp_path, [item()])
    fake = Fake({"0840006667278": [P("B0SPONSOR1", "Corsair K65 Plus Wireless", sponsored=True),
                                   P("B0CORSAIR1", "Corsair K65 Plus Wireless Teclado mecánico", 299.0)]},
                {"B0CORSAIR1": ficha("299,00 €")})
    line = crossamz.run(tmp_path, fake.search, fake.page, lambda h: {"median": 305.0}, NOW, sleep=lambda s: None)
    assert "1 cruzados" in line and "oferta_real 1" in line
    data = json.loads((tmp_path / crossamz.FILE).read_text(encoding="utf-8"))
    e = data["items"]["1"]
    assert e["asin"] == "B0CORSAIR1" and e["veredicto"] == "oferta_real" and e["encontrado_por"] == "ean"
    assert data["ean_asin"]["0840006667278"]["a"] == "B0CORSAIR1"
    assert fake.fichas == ["B0CORSAIR1"]


def test_falls_back_to_name_search(tmp_path):
    it = item()
    write_cands(tmp_path, [it])
    fake = Fake({crossamz.name_query(it): [P("B0CORSAIR1", "Corsair K65 Plus Wireless 75% Teclado gaming RGB", 160.0)]},
                {"B0CORSAIR1": ficha("160,00 €")})
    crossamz.run(tmp_path, fake.search, fake.page, ref_none, NOW, sleep=lambda s: None)
    e = crossamz.load(tmp_path / crossamz.FILE)["items"]["1"]
    assert e["encontrado_por"] == "nombre" and e["veredicto"] == "amazon_mas_barato"
    assert fake.searches == ["0840006667278", crossamz.name_query(it)]


def test_not_found_is_remembered_and_not_searched_again(tmp_path):
    write_cands(tmp_path, [item()])
    fake = Fake()
    crossamz.run(tmp_path, fake.search, fake.page, ref_none, NOW, sleep=lambda s: None)
    data = crossamz.load(tmp_path / crossamz.FILE)
    assert data["items"]["1"]["veredicto"] == "no_esta"
    # el precio de PowerPlanet cambia: se cruza otra vez, pero el EAN no se vuelve a buscar
    write_cands(tmp_path, [item(price=150.0)])
    crossamz.run(tmp_path, fake.search, fake.page, ref_none, NOW + timedelta(hours=1), sleep=lambda s: None)
    assert len(fake.searches) == 2


def test_known_asin_skips_the_search(tmp_path):
    write_cands(tmp_path, [item()])
    data = crossamz.empty()
    data["ean_asin"]["0840006667278"] = {"a": "B0CORSAIR1", "t": NOW.isoformat(), "via": "ean", "title": "Corsair K65"}
    crossamz.save(tmp_path / crossamz.FILE, data)
    fake = Fake(pages={"B0CORSAIR1": ficha("320,00 €")})
    crossamz.run(tmp_path, fake.search, fake.page, ref_none, NOW, sleep=lambda s: None)
    assert fake.searches == [] and fake.fichas == ["B0CORSAIR1"]


def test_to_verify_go_first_and_only_a_few_per_tick(tmp_path):
    cands = [item(str(i), ean=f"84{i:011d}", score=i) for i in range(5)]
    write_cands(tmp_path, cands, to_verify=[item("v", ean="8400000000999")])
    fake = Fake()
    line = crossamz.run(tmp_path, fake.search, fake.page, ref_none, NOW, sleep=lambda s: None)
    data = crossamz.load(tmp_path / crossamz.FILE)
    assert set(data["items"]) == {"v", "4"}  # el to_verify y el de mas nota
    assert data["items"]["v"]["lista"] == "to_verify"
    assert "4 pendientes" in line


def test_request_budget_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(crossamz, "PER_TICK", 10)
    write_cands(tmp_path, [item(str(i), ean=f"84{i:011d}") for i in range(10)])
    fake = Fake()
    crossamz.run(tmp_path, fake.search, fake.page, ref_none, NOW, sleep=lambda s: None)
    assert len(fake.searches) + len(fake.fichas) == crossamz.MAX_REQUESTS


def test_recent_cross_is_not_repeated(tmp_path):
    write_cands(tmp_path, [item()])
    fake = Fake()
    crossamz.run(tmp_path, fake.search, fake.page, ref_none, NOW, sleep=lambda s: None)
    line = crossamz.run(tmp_path, fake.search, fake.page, ref_none, NOW + timedelta(hours=5), sleep=lambda s: None)
    assert "0 cruzados, 0 pendientes" in line


def test_network_error_stops_without_losing_state(tmp_path):
    write_cands(tmp_path, [item("1", score=9), item("2", ean="8400000000002", score=1)])

    def boom(text):
        raise RuntimeError("503")
    line = crossamz.run(tmp_path, boom, lambda a: "", ref_none, NOW, sleep=lambda s: None)
    assert "parado por RuntimeError" in line
    assert crossamz.load(tmp_path / crossamz.FILE)["items"] == {}


def test_old_entries_of_products_that_left_the_list_are_pruned(tmp_path):
    data = crossamz.empty()
    data["items"]["viejo"] = {"t": (NOW - timedelta(days=20)).isoformat(), "veredicto": "no_esta"}
    data["items"]["reciente"] = {"t": (NOW - timedelta(days=2)).isoformat(), "veredicto": "no_esta"}
    crossamz.save(tmp_path / crossamz.FILE, data)
    write_cands(tmp_path, [])
    crossamz.run(tmp_path, lambda t: [], lambda a: "", ref_none, NOW, sleep=lambda s: None)
    assert set(crossamz.load(tmp_path / crossamz.FILE)["items"]) == {"reciente"}


def test_missing_candidates_file_is_harmless(tmp_path):
    line = crossamz.run(tmp_path, lambda t: [], lambda a: "", ref_none, NOW, sleep=lambda s: None)
    assert line.startswith("cruce amazon: 0 cruzados")


def test_night_crosses_more_but_at_most_every_15_minutes(tmp_path):
    write_cands(tmp_path, [item(str(i), ean=f"84{i:011d}") for i in range(10)])
    assert crossamz.night_due(tmp_path, NOW)
    fake = Fake()
    line = crossamz.run(tmp_path, fake.search, fake.page, ref_none, NOW, sleep=lambda s: None, night=True)
    assert line.startswith("cruce amazon (noche): 3 cruzados")
    assert not crossamz.night_due(tmp_path, NOW + timedelta(minutes=10))
    assert crossamz.night_due(tmp_path, NOW + timedelta(minutes=15))


def test_night_is_not_due_when_nothing_is_pending(tmp_path):
    write_cands(tmp_path, [item()])
    crossamz.run(tmp_path, lambda t: [], lambda a: "", ref_none, NOW, sleep=lambda s: None)
    assert not crossamz.night_due(tmp_path, NOW + timedelta(hours=1))
