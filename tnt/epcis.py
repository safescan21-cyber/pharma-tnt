"""EPCIS 2.0 (JSON-LD) export and import.

Export: the shipment's full history for the next trading partner (DSCSA
"transaction information"), restricted to the packs actually shipped, plus
product master data in the header.
Import: commissioning + aggregation events from a CMO / packaging line.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone

import orjson

from . import gs1
from .core import Invalid, NotFound, TrackTrace

CONTEXT = ["https://ref.gs1.org/standards/epcis/2.0.0/epcis-context.jsonld"]
DISPOSITION = {
    "commissioning": "active", "packing": "in_progress", "unpacking": "in_progress",
    "shipping": "in_transit", "receiving": "in_progress", "dispensing": "dispensed",
}
DECOMMISSION_DISPOSITION = {"destroyed": "destroyed", "stolen": "stolen", "expired": "expired", "sample": "inactive"}


def _document(events: list[dict], master_data: list[dict]) -> dict:
    doc = {
        "@context": CONTEXT,
        "type": "EPCISDocument",
        "schemaVersion": "2.0",
        "creationDate": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "epcisBody": {"eventList": events},
    }
    if master_data:
        doc["epcisHeader"] = {"epcisMasterData": {"vocabularyList": [
            {"type": "urn:epcglobal:epcis:vtype:EPCClass", "vocabularyElementList": master_data}]}}
    return doc


def _event(e, codes: list[str], lots: dict) -> dict | None:
    data = orjson.loads(e["data"])
    out = {
        "type": e["event_type"],
        "eventTime": e["ts"],
        "eventTimeZoneOffset": "+00:00",
        "eventID": f"ni:///sha-256;{e['hash']}?ver=CBV2.0",
        "action": e["action"],
        "bizStep": e["biz_step"],
    }
    disp = DISPOSITION.get(e["biz_step"]) or DECOMMISSION_DISPOSITION.get(data.get("reason"))
    if disp:
        out["disposition"] = disp
    if e["location"]:
        out["readPoint"] = {"id": gs1.gln_uri(e["location"])}
        out["bizLocation"] = {"id": gs1.gln_uri(e["location"])}
    if e["event_type"] == "AggregationEvent":
        parent = data["parent"]
        if parent not in codes:
            return None  # parent not part of this export scope
        out["parentID"] = gs1.to_uri(parent)
        out["childEPCs"] = [gs1.to_uri(c) for c in codes if c != parent]
    else:
        out["epcList"] = [gs1.to_uri(c) for c in codes]
    if e["biz_step"] == "commissioning" and "lot" in data:
        out["ilmd"] = {"cbvmda:lotNumber": data["lot"],
                       "cbvmda:itemExpirationDate": lots.get((data["gtin"], data["lot"]))}
    if e["biz_step"] == "shipping":
        out["destinationList"] = [{"type": "location", "destination": gs1.gln_uri(data["destination"])}]
        if data.get("reference"):
            out["bizTransactionList"] = [{"type": "desadv", "bizTransaction": data["reference"]}]
    return out


def _master_data(r, gtins) -> list[dict]:
    out = []
    for gtin in sorted(gtins):
        p = r.execute("SELECT * FROM products WHERE gtin=?", (gtin,)).fetchone()
        attrs = [("regulatedProductName", p["name"]), ("strengthDescription", p["strength"]),
                 ("dosageFormType", p["dosage_form"]), ("manufacturerOfTradeItemPartyName", p["manufacturer"]),
                 ("additionalTradeItemIdentification", p["ndc"])]
        out.append({"id": f"https://id.gs1.org/01/{gtin}",
                    "attributes": [{"id": f"urn:epcglobal:cbv:mda#{k}", "attribute": v} for k, v in attrs if v]})
    return out


def _build(tt: TrackTrace, event_ids, scope: set[int] | None) -> dict:
    r = tt.db.reader()
    lots = {(row["gtin"], row["lot"]): row["expiry"] for row in r.execute("SELECT * FROM lots")}
    events, gtins = [], set()
    for eid in event_ids:
        e = r.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if e["event_type"] == "SystemEvent":
            continue
        members = r.execute("SELECT i.id, i.code, i.gtin FROM event_items ei JOIN items i ON i.id=ei.item_id"
                            " WHERE ei.event_id=? ORDER BY ei.item_id", (eid,)).fetchall()
        if scope is not None:
            members = [m for m in members if m["id"] in scope]
        if not members:
            continue
        gtins.update(m["gtin"] for m in members if m["gtin"])
        if (ev := _event(e, [m["code"] for m in members], lots)) is not None:
            events.append(ev)
    return _document(events, _master_data(r, gtins))


def export_shipment(tt: TrackTrace, shipment_id: int) -> dict:
    r = tt.db.reader()
    e = r.execute("SELECT biz_step FROM events WHERE id=?", (shipment_id,)).fetchone()
    if not e or e["biz_step"] != "shipping":
        raise NotFound(f"no shipment with id {shipment_id}")
    scope = {row[0] for row in r.execute("SELECT item_id FROM event_items WHERE event_id=?", (shipment_id,))}
    ids = [row[0] for row in r.execute(
        "SELECT DISTINCT ei2.event_id FROM event_items ei JOIN event_items ei2 ON ei2.item_id=ei.item_id"
        " WHERE ei.event_id=? AND ei2.event_id<=? ORDER BY 1", (shipment_id, shipment_id))]
    return _build(tt, ids, scope)


def export_events(tt: TrackTrace, since: str | None = None, until: str | None = None, limit: int = 1000) -> dict:
    ids = [row[0] for row in tt.db.reader().execute(
        "SELECT id FROM events WHERE ts >= ? AND ts <= ? AND event_type <> 'SystemEvent' ORDER BY id LIMIT ?",
        (since or "", (until or "9999") + "~", min(limit, 10_000)))]
    return _build(tt, ids, None)


# ------------------------------------------------------------------ import
def _ilmd(ev: dict, name: str):
    for k, v in (ev.get("ilmd") or {}).items():
        if k.split(":")[-1].split("#")[-1] == name:
            return v
    return None


def _location(c, ev: dict, default: str) -> str:
    for field in ("bizLocation", "readPoint"):
        if (ref := (ev.get(field) or {}).get("id")) and (gln := gs1.gln_from_uri(ref)):
            if c.execute("SELECT 1 FROM locations WHERE gln=?", (gln,)).fetchone():
                return gln
    return default


def _ensure_lot(tt, c, gtin, lot, expiry, actor, stats) -> None:
    if not c.execute("SELECT 1 FROM products WHERE gtin=?", (gtin,)).fetchone():
        raise NotFound(f"unknown product {gtin}: add product master data before importing")
    row = c.execute("SELECT expiry FROM lots WHERE gtin=? AND lot=?", (gtin, lot)).fetchone()
    exp = str(expiry)[:10] if expiry else None
    if row:
        if exp and row["expiry"] != exp:
            raise Invalid(f"lot {lot}: expiry {exp} conflicts with recorded {row['expiry']}")
        return
    if not exp:
        raise Invalid(f"lot {lot} is new and the EPCIS event has no itemExpirationDate")
    date.fromisoformat(exp)
    # Incoming lots land in quarantine until QA releases them.
    c.execute("INSERT INTO lots VALUES(?,?,?,'quarantine')", (gtin, lot, exp))
    tt._record(c, "SystemEvent", "OBSERVE", "lot_quarantine", None, actor,
               {"gtin": gtin, "lot": lot, "status": "quarantine", "reason": "created by EPCIS import"}, [])
    stats["lots_created"] += 1


def import_document(tt: TrackTrace, doc: dict, default_location: str, actor: str = "system") -> dict:
    events = (doc.get("epcisBody") or {}).get("eventList") or doc.get("eventList")
    if not isinstance(events, list) or not events:
        raise Invalid("no EPCIS eventList found")
    stats: Counter = Counter()
    with tt._tx() as c:
        tt._require_location(c, default_location)
        for n, ev in enumerate(events, 1):
            try:
                _import_event(tt, c, ev, default_location, actor, stats)
            except gs1.GS1Error as e:
                raise Invalid(f"event {n}: {e}") from e
            except (KeyError, TypeError, ValueError) as e:
                raise Invalid(f"event {n}: malformed ({e})") from e
    return dict(stats)


def _import_event(tt, c, ev, default_location, actor, stats):
    kind, action = ev.get("type"), ev.get("action")
    step = str(ev.get("bizStep", "")).split(":")[-1].split("/")[-1]
    # Imported stock is registered where it physically is now (the importing site);
    # the sender's location is kept as provenance.
    loc = default_location
    meta = {"source": "epcis", "source_event_id": ev.get("eventID"), "source_event_time": ev.get("eventTime"),
            "source_location": _location(c, ev, None)}
    if kind == "ObjectEvent" and action == "ADD" and step == "commissioning":
        keys = [gs1.from_uri(u) for u in ev.get("epcList", [])]
        if ssccs := [k for k in keys if k.startswith("00")]:
            tt._insert_containers(c, "case", loc, ssccs, actor)
            stats["containers"] += len(ssccs)
        groups: dict[str, list[str]] = {}
        for k in keys:
            if k.startswith("01"):
                code = gs1.parse(k)
                groups.setdefault(code.gtin, []).append(code.serial)
        if groups:
            lot = _ilmd(ev, "lotNumber")
            if not lot:
                raise Invalid("commissioning event without ilmd lotNumber")
            for gtin, serials in groups.items():
                _ensure_lot(tt, c, gtin, lot, _ilmd(ev, "itemExpirationDate"), actor, stats)
                tt._commission(c, gtin, lot, serials, loc, actor, meta)
                stats["units"] += len(serials)
    elif kind == "AggregationEvent" and action == "ADD":
        parent = gs1.from_uri(ev["parentID"])
        children = [gs1.from_uri(u) for u in ev.get("childEPCs", [])]
        nested = any(k.startswith("00") for k in children)
        row = c.execute("SELECT id, kind FROM items WHERE code=?", (parent,)).fetchone()
        if not row:
            tt._insert_containers(c, "pallet" if nested else "case", loc, [parent], actor)
            stats["containers"] += 1
        elif nested and row["kind"] == "case":
            c.execute("UPDATE items SET kind='pallet' WHERE id=?", (row["id"],))
        tt._aggregate(c, parent, children, actor)
        stats["aggregations"] += 1
    else:
        stats["skipped_events"] += 1
