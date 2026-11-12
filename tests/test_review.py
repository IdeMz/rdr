import json
from datetime import datetime, timedelta, timezone

import pytest

import review

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def cand(pid, shop_id="s1", shop="Tienda Uno", **kw):
    base = {"id": pid, "name": f"Producto {pid}", "price": 19.9, "drop": 32.4, "reviews": 800, "rating": 96.5,
            "image": "https://img/x.jpg", "link": f"https://s.click/{pid}", "shop_id": shop_id, "shop": shop,
            "score": 40.0}
    base.update(kw)
    return base


def test_queue_adds_new_candidates_once():
    data = review.empty()
    assert review.queue(data, [cand("1"), cand("2", "s2")], NOW) == ["a1", "a2"]
    assert review.queue(data, [cand("1")], NOW) == []
    assert data["items"]["a1"]["st"] == review.NEW


def test_candidates_without_id_are_ignored():
    data = review.empty()
    assert review.queue(data, [{"name": "sin id"}], NOW) == []


def test_ok_trusts_the_shop_and_approves_its_other_pending_products():
    data = review.empty()
    review.queue(data, [cand("1"), cand("2"), cand("3", "s2", "Otra")], NOW)
    reply, also = review.apply(data, "ok", "a1", NOW)
    assert "confianza" in reply and also == ["a2"]
    assert data["trusted"] == {"s1": "Tienda Uno"}
    assert [data["items"][r]["st"] for r in ("a1", "a2", "a3")] == [review.OK, review.OK, review.NEW]
    # un producto nuevo de esa tienda pasa aprobado sin preguntar
    review.queue(data, [cand("4")], NOW)
    assert data["items"]["a4"]["st"] == review.OK and data["items"]["a4"]["auto"] is True
    assert [r for r, _ in review.approved(data)] == ["a1", "a2", "a4"]


def test_no_discards_only_this_product():
    data = review.empty()
    review.queue(data, [cand("1"), cand("2")], NOW)
    reply, also = review.apply(data, "no", "a1", NOW)
    assert reply.startswith("Descartado") and also == []
    assert data["items"]["a2"]["st"] == review.NEW and data["blocked"] == {}


def test_shop_button_blocks_the_shop_and_its_pending_products():
    data = review.empty()
    review.queue(data, [cand("1"), cand("2")], NOW)
    reply, also = review.apply(data, "shop", "a1", NOW)
    assert "Tienda" in reply and also == ["a2"]
    assert data["blocked"] == {"s1": "Tienda Uno"}
    assert review.queue(data, [cand("3")], NOW) == []  # ya no se pide nada de esa tienda


def test_answering_twice_or_unknown_product():
    data = review.empty()
    review.queue(data, [cand("1")], NOW)
    review.apply(data, "no", "a1", NOW)
    assert review.apply(data, "ok", "a1", NOW) == ("Ya descartado", [])
    assert review.apply(data, "ok", "zzz", NOW) == ("Ya no está en la lista", [])
    assert data["trusted"] == {}


def test_product_without_shop_id_is_approved_but_no_shop_is_remembered():
    data = review.empty()
    review.queue(data, [cand("1", shop_id="", shop="")], NOW)
    reply, _ = review.apply(data, "ok", "a1", NOW)
    assert reply == "Aprobado ✅" and data["trusted"] == {}


def test_to_send_respects_caps_and_order():
    data = review.empty()
    review.queue(data, [cand(str(n), f"s{n}", score=n) for n in range(1, 11)], NOW)
    first = review.to_send(data, NOW)
    assert [r for r, _ in first] == ["a10", "a9", "a8"]  # tope por ejecucion, mejores primero
    for rid, _ in first:
        review.mark_sent(data, rid, 100, NOW)
    for rid, _ in review.to_send(data, NOW):
        review.mark_sent(data, rid, 101, NOW)
    for rid, _ in review.to_send(data, NOW):
        review.mark_sent(data, rid, 102, NOW)
    assert sum(1 for i in data["items"].values() if i["st"] == review.SENT) == review.DAILY_CAP
    assert review.to_send(data, NOW) == []  # tope diario
    assert len(review.to_send(data, NOW + timedelta(days=1))) == 2  # al dia siguiente, lo que queda


def test_expire_old_pending_and_forget_old_resolved():
    data = review.empty()
    review.queue(data, [cand("1"), cand("2", "s2")], NOW)
    review.apply(data, "no", "a2", NOW)
    assert review.expire(data, NOW + timedelta(days=3)) == 1
    assert data["items"]["a1"]["st"] == review.EXPIRED
    review.expire(data, NOW + timedelta(days=review.KEEP_DAYS + 1))
    assert data["items"] == {}


def test_caption_is_escaped_and_links_to_the_plain_product_page():
    item = review.empty()
    review.queue(item, [cand("1", name="<b>Cable</b> & más", shop="Tienda <X>")], NOW)
    text = review.caption(item["items"]["a1"])
    assert "&lt;b&gt;Cable&lt;/b&gt; &amp; más" in text and "Tienda &lt;X&gt;" in text
    assert "19,90 €" in text and "800 ventas" in text and "−32 %" in text
    assert 'href="https://es.aliexpress.com/item/1.html"' in text
    assert "s.click" not in text  # no se usa el enlace de afiliado para mirar el envio


def test_keyboard_and_callback_roundtrip():
    rows = review.keyboard("a1005007345624146")["inline_keyboard"]
    data = [r[0]["callback_data"] for r in rows]
    assert all(len(d.encode()) <= 64 for d in data)
    assert [review.parse_callback(d) for d in data] == [("ok", "a1005007345624146"),
                                                        ("no", "a1005007345624146"),
                                                        ("shop", "a1005007345624146")]
    assert review.parse_callback("like") is None and review.parse_callback("rv:xx:a1") is None


def test_load_save_roundtrip_and_corrupt_file(tmp_path):
    path = tmp_path / "estado" / review.FILE
    assert review.load(path) == review.empty()
    data = review.empty()
    review.queue(data, [cand("1")], NOW)
    review.save(path, data)
    assert review.load(path) == data
    path.write_text("{no es json", encoding="utf-8")
    assert review.load(path) == review.empty()
    path.write_text("[1]", encoding="utf-8")
    assert review.load(path) == review.empty()


def test_mark_published():
    data = review.empty()
    review.queue(data, [cand("1")], NOW)
    review.apply(data, "ok", "a1", NOW)
    review.mark_published(data, "a1")
    assert review.approved(data) == [] and data["items"]["a1"]["st"] == review.DONE
