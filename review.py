"""Cola de revision manual de productos (AliExpress, y mas adelante los `to_verify` de PowerPlanet).

La API de AliExpress no dice desde donde se envia un producto. Para no publicar nada que salga de China, cada
candidato te llega por privado con tres botones y no se publica sin tu OK:

  ✅ rv:ok    -> sale de Europa: aprobado, y la tienda queda "de confianza" (sus siguientes productos pasan solos)
  ❌ rv:no    -> descarta solo este producto
  🚫 rv:shop  -> descarta el producto y la tienda (no se vuelve a pedir nada de ella)

Este modulo no habla con Telegram ni con ninguna API: solo guarda y cambia el estado (state/revision.json).
deals.py hace el envio y lee los botones. El modulo de publicacion leera `approved()`.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta
from pathlib import Path

FILE = "revision.json"
EXPIRE_DAYS = 2          # un candidato sin respuesta caduca (el precio ya habra cambiado)
KEEP_DAYS = 14           # cuanto se recuerda un producto ya resuelto (para no volver a pedirlo)
DAILY_CAP = 8            # mensajes nuevos al dia
TICK_CAP = 3             # mensajes nuevos por ejecucion (no inundar el chat)
ACTIONS = ("ok", "no", "shop")

NEW, SENT, OK, NO, EXPIRED, DONE = "new", "sent", "ok", "no", "exp", "pub"


def empty() -> dict:
    return {"items": {}, "trusted": {}, "blocked": {}}


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict):
        return empty()
    for key in ("items", "trusted", "blocked"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def rid_of(source: str, pid: str) -> str:
    """Identificador corto (cabe de sobra en los 64 bytes de un boton de Telegram)."""
    return f"{source}{pid}"


def _entry(cand: dict, rid: str, now: datetime, status: str) -> dict:
    return {
        "src": "a", "id": str(cand.get("id", "")), "st": status, "t": now.isoformat(),
        "title": str(cand.get("name", ""))[:160], "price": cand.get("price"), "drop": cand.get("drop"),
        "sales": cand.get("reviews"), "rating": cand.get("rating"), "image": cand.get("image", ""),
        "shop_id": str(cand.get("shop_id") or ""), "shop": str(cand.get("shop") or ""),
        "link": cand.get("link", ""), "score": cand.get("score", 0),
    }


def queue(data: dict, candidates: list[dict], now: datetime, source: str = "a") -> list[str]:
    """Anota los candidatos nuevos. Los de tiendas de confianza pasan aprobados; los de tiendas vetadas se ignoran.
    Devuelve los identificadores anotados."""
    added: list[str] = []
    for cand in candidates:
        pid = str(cand.get("id", "")).strip()
        if not pid:
            continue
        rid = rid_of(source, pid)
        if rid in data["items"]:
            continue
        shop_id = str(cand.get("shop_id") or "")
        if shop_id and shop_id in data["blocked"]:
            continue
        trusted = bool(shop_id) and shop_id in data["trusted"]
        entry = _entry(cand, rid, now, OK if trusted else NEW)
        if trusted:
            entry["auto"] = True
        data["items"][rid] = entry
        added.append(rid)
    return added


def expire(data: dict, now: datetime) -> int:
    """Caduca lo que lleva mas de EXPIRE_DAYS sin respuesta y olvida lo resuelto hace mas de KEEP_DAYS."""
    changed = 0
    for rid, item in list(data["items"].items()):
        age = now - _ts(item["t"])
        if item["st"] in (NEW, SENT) and age > timedelta(days=EXPIRE_DAYS):
            item["st"], changed = EXPIRED, changed + 1
        elif age > timedelta(days=KEEP_DAYS):
            del data["items"][rid]
    return changed


def to_send(data: dict, now: datetime) -> list[tuple[str, dict]]:
    """Candidatos por enviar ahora: los mejores primero, respetando el tope diario y el de cada ejecucion."""
    today = now.date().isoformat()
    sent_today = sum(1 for i in data["items"].values() if str(i.get("sent_t", ""))[:10] == today)
    room = max(0, min(TICK_CAP, DAILY_CAP - sent_today))
    waiting = sorted(((rid, i) for rid, i in data["items"].items() if i["st"] == NEW),
                     key=lambda pair: pair[1].get("score") or 0, reverse=True)
    return waiting[:room]


def mark_sent(data: dict, rid: str, msg_id: int | None, now: datetime) -> None:
    item = data["items"].get(rid)
    if item and item["st"] == NEW:
        item["st"], item["m"], item["sent_t"] = SENT, msg_id, now.isoformat()


def has_open(data: dict) -> bool:
    return any(i["st"] == SENT for i in data["items"].values())


def apply(data: dict, action: str, rid: str, now: datetime) -> tuple[str, list[str]]:
    """Aplica un boton. Devuelve (texto de respuesta, ids de otros productos de la misma tienda que cambian)."""
    item = data["items"].get(rid)
    if item is None or action not in ACTIONS:
        return "Ya no está en la lista", []
    if item["st"] not in (NEW, SENT):
        return {OK: "Ya aprobado", NO: "Ya descartado", EXPIRED: "Caducado", DONE: "Ya publicado"}.get(
            item["st"], "Ya respondido"), []
    shop_id, shop = item.get("shop_id", ""), item.get("shop", "")
    also: list[str] = []
    if action == "ok":
        item["st"], item["answered"] = OK, now.isoformat()
        if shop_id:
            data["trusted"][shop_id] = shop or shop_id
            data["blocked"].pop(shop_id, None)
            also = _same_shop(data, shop_id, rid, OK, now)
        return ("Aprobado ✅ (tienda de confianza)" if shop_id else "Aprobado ✅"), also
    item["st"], item["answered"] = NO, now.isoformat()
    if action == "shop" and shop_id:
        data["blocked"][shop_id] = shop or shop_id
        data["trusted"].pop(shop_id, None)
        also = _same_shop(data, shop_id, rid, NO, now)
        return "Tienda descartada 🚫", also
    return "Descartado ❌", []


def _same_shop(data: dict, shop_id: str, skip: str, status: str, now: datetime) -> list[str]:
    changed: list[str] = []
    for rid, item in data["items"].items():
        if rid != skip and item.get("shop_id") == shop_id and item["st"] in (NEW, SENT):
            item["st"], item["answered"], item["auto"] = status, now.isoformat(), True
            changed.append(rid)
    return changed


def approved(data: dict) -> list[tuple[str, dict]]:
    """Productos aprobados y aun sin publicar (los consumira el modulo de publicacion)."""
    return sorted(((rid, i) for rid, i in data["items"].items() if i["st"] == OK),
                  key=lambda pair: pair[1].get("score") or 0, reverse=True)


def mark_published(data: dict, rid: str) -> None:
    if rid in data["items"]:
        data["items"][rid]["st"] = DONE


def page_url(item: dict) -> str:
    """Ficha del producto sin el enlace de afiliado, para que mires el envio sin tocar las comisiones."""
    return f"https://es.aliexpress.com/item/{item['id']}.html"


def _eur(value) -> str:
    try:
        return f"{float(value):.2f}".replace(".", ",") + " €"
    except (TypeError, ValueError):
        return "?"


def caption(item: dict) -> str:
    drop = item.get("drop")
    drop_text = f" (−{round(drop)} % frente a su precio de los últimos días)" if isinstance(drop, (int, float)) else ""
    extras = []
    if item.get("sales") is not None:
        extras.append(f"{int(item['sales'])} ventas")
    if item.get("rating"):
        extras.append(f"⭐ {item['rating']:g} %")
    lines = [
        "🛒 <b>AliExpress · ¿sale de Europa?</b>",
        html.escape(item.get("title", "")),
        f"💶 {_eur(item.get('price'))}{drop_text}",
    ]
    if extras:
        lines.append(" · ".join(extras))
    if item.get("shop"):
        lines.append(f"🏪 {html.escape(item['shop'])}")
    lines.append(f'<a href="{page_url(item)}">Abrir la ficha</a> y mira «Envío desde» antes de pulsar.')
    return "\n".join(lines)


def keyboard(rid: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Sale de Europa (confiar en la tienda)", "callback_data": f"rv:ok:{rid}"}],
        [{"text": "❌ Descartar este producto", "callback_data": f"rv:no:{rid}"}],
        [{"text": "🚫 Descartar la tienda", "callback_data": f"rv:shop:{rid}"}],
    ]}


def parse_callback(cb: str) -> tuple[str, str] | None:
    """'rv:ok:a1005...' -> ('ok', 'a1005...'); None si no es de esta lista."""
    parts = cb.split(":", 2)
    if len(parts) == 3 and parts[0] == "rv" and parts[1] in ACTIONS and parts[2]:
        return parts[1], parts[2]
    return None
