from datetime import date

import feed
import manual

PP_URL = "https://www.powerplanetonline.com/es/ssd-sandisk-extreme-portable-v2-2tb-azul"

ROWS = [
    {"aw_product_id": "45948306775", "product_name": "SanDisk Extreme Portable V2 2TB NVMe USB 3.2 Gen 2 1050MB/s IP65 SSD externo",
     "search_price": "266.95", "rrp_price": "791.64", "aw_deep_link": "https://www.awin1.com/pclick.php?p=45948306775&a=1&m=2",
     "merchant_image_url": "https://www.powerplanetonline.com/cdnassets/products/109442/sandisk.webp"},
    {"aw_product_id": "45948306776", "product_name": "SanDisk Extreme Portable V2 4TB IP55 USB 3.2 Gen 2 1050MB/s SSD externo",
     "search_price": "399.95", "rrp_price": "999.00", "aw_deep_link": "https://www.awin1.com/pclick.php?p=45948306776&a=1&m=2"},
    {"aw_product_id": "1", "product_name": "Corsair K65 Plus Wireless Teclado", "search_price": "163.96",
     "aw_deep_link": "https://www.awin1.com/pclick.php?p=1"},
]

PAGE = """<html><head><meta property="og:title" content="SanDisk Extreme Portable V2 2TB Azul - SSD IP65 1050 MB/s | Powerplanet">
<meta property="og:image" content="https://www.powerplanetonline.com/x.webp">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","offers":{"@type":"Offer","price":"259.95"}}</script>
</head><body><p>Recíbelo a partir del martes, 24 Nov.</p></body></html>"""


def test_detect():
    assert manual.detect(f"mira esto {PP_URL} !") == ("powerplanet", PP_URL)
    assert manual.detect("https://es.aliexpress.com/item/1005006097742074.html?spm=x")[0] == "aliexpress"
    assert manual.detect("https://s.click.aliexpress.com/e/_oABC")[0] == "aliexpress"
    assert manual.detect("https://www.amazon.es/dp/B0CT8W3Y29") == ("", "")


def test_slug_words():
    assert manual.slug_words(PP_URL) == "ssd sandisk extreme portable v2 2tb azul"


def test_parse_pp_page():
    page = manual.parse_pp_page(PAGE)
    assert page["title"] == "SanDisk Extreme Portable V2 2TB Azul - SSD IP65 1050 MB/s"
    assert page["price"] == 259.95 and page["image"].endswith("x.webp")
    assert page["delivery"] == "Recíbelo a partir del martes, 24 Nov."
    assert manual.parse_pp_page('<meta property="product:price:amount" content="19.90">')["price"] == 19.9


def test_feed_match_picks_the_right_capacity():
    row, score = manual.best_row(ROWS, "ssd sandisk extreme portable v2 2tb azul")
    assert row["aw_product_id"] == "45948306775" and score >= manual.MIN_MATCH


def test_pp_item_uses_page_price_feed_link_and_warns():
    item, info = manual.pp_item(ROWS, PP_URL, manual.parse_pp_page(PAGE), None, date(2026, 11, 13))
    assert item["key"] == "pp:45948306775" and item["price"] == 259.95 and item["old"] == 791.64
    assert item["url"].startswith("https://www.awin1.com/pclick.php?p=45948306775")
    assert item["store"] == "POWERPLANET"
    text = "\n".join(info)
    assert "Precio de la web 259,95" in text and "24 Nov" in text


def test_pp_item_without_page_uses_the_slug_and_feed_price():
    item, info = manual.pp_item(ROWS, PP_URL, {}, None, date(2026, 11, 13))
    assert item["price"] == 266.95 and any("No se pudo abrir" in line for line in info)


def test_pp_item_not_in_feed():
    item, info = manual.pp_item(ROWS, "https://www.powerplanetonline.com/es/cafetera-delonghi-magnifica", {}, None,
                                date(2026, 11, 13))
    assert item is None and "No encuentro" in info[0]


def test_pp_history_line():
    d = feed.day_no(date(2026, 11, 13))
    assert manual.pp_history_line([d, d - 11, 299.0, d - 3, 259.95], date(2026, 11, 13)) == \
        "historial propio: mínimo 259,95 € en 12 días"


def test_ae_id():
    assert manual.ae_id("https://es.aliexpress.com/item/1005006097742074.html?spm=a2g0o") == "1005006097742074"
    assert manual.ae_id("https://m.aliexpress.com/p/x?productIds=1005006097742074") == "1005006097742074"
    assert manual.ae_id("https://s.click.aliexpress.com/e/_oABC") is None


def test_ae_item_marks_shop_and_europe_check():
    api = {"id": "33039312461", "name": "Luz LED nocturna", "price": 14.19, "rrp": 14.81, "image": "https://ae-pic-a1.aliexpress-media.com/a.jpg",
           "link": "https://s.click.aliexpress.com/e/_x", "rating": 99.3, "reviews": 1234, "shop_id": "s1", "shop": "STARNIGHT"}
    item, info = manual.ae_item(api, {"s1": "STARNIGHT"}, {})
    assert item["key"] == "ae:33039312461" and item["store"] == "ALIEXPRESS" and item["rating"] == 5.0
    text = "\n".join(info)
    assert "de confianza" in text and "Europa" in text and "1.234 vendidos" in text
    assert "bloqueada" in "\n".join(manual.ae_item(api, {}, {"s1": "x"})[1])
