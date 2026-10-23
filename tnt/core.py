"""Track & trace business logic: serialization, aggregation, custody, verification, recall."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import string
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone

import orjson

from . import gs1
from .db import Database

GENESIS = "0" * 64
SERIAL_ALPHABET = string.ascii_uppercase + string.digits
DECOMMISSION_REASONS = {"destroyed", "stolen", "sample", "expired"}
LOT_STATUSES = {"released", "quarantine", "recalled"}
MAX_BATCH = 1_000_000


class TnTError(Exception):
    status = 409


class NotFound(TnTError):
    status = 404


class Invalid(TnTError):
    status = 422


class Unauthorized(TnTError):
    status = 401


class Forbidden(TnTError):
    status = 403


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canon(code: str) -> str:
    try:
        return gs1.parse(code, validate=False).key
    except gs1.GS1Error as e:
        raise Invalid(str(e)) from e


def _json(ids: Iterable[int]) -> str:
    return orjson.dumps(list(ids)).decode()


def random_serials(n: int, length: int = 12) -> list[str]:
    """Non-sequential, randomized serials (EU FMD / DSCSA expectation)."""
    out: set[str] = set()
    limit = 256 - 256 % len(SERIAL_ALPHABET)  # rejection sampling keeps the distribution uniform
    while len(out) < n:
        need = (n - len(out)) * length
        chars = [SERIAL_ALPHABET[b % len(SERIAL_ALPHABET)] for b in secrets.token_bytes(need * 9 // 8 + 16) if b < limit]
        out.update("".join(chars[i:i + length]) for i in range(0, len(chars) - length + 1, length))
    return list(out)[:n]


class TrackTrace:
    def __init__(self, path: str = "tnt.db"):
        self.db = Database(path)
        row = self.db.writer.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
        self._last_hash = row[0] if row else GENESIS

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ infra
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self.db.write_lock:
            conn = self.db.writer
            conn.execute("BEGIN IMMEDIATE")
            saved_hash = self._last_hash
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                self._last_hash = saved_hash
                raise

    def _record(self, conn, event_type, action, biz_step, location, actor, data, items) -> int:
        """Append a hash-chained event and link every affected item (sorted by item id)."""
        items = sorted(items, key=lambda r: r[0])
        ts = _now()
        data_s = orjson.dumps(data, option=orjson.OPT_SORT_KEYS).decode()
        digest = self._event_hash(self._last_hash, event_type, action, biz_step, location, ts,
                                  actor, data_s, (r[1] for r in items))
        cur = conn.execute(
            "INSERT INTO events(event_type,action,biz_step,location,ts,actor,data,prev_hash,hash)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (event_type, action, biz_step, location, ts, actor, data_s, self._last_hash, digest),
        )
        eid = cur.lastrowid
        conn.executemany("INSERT INTO event_items(item_id,event_id) VALUES(?,?)",
                         ((r[0], eid) for r in items))
        self._last_hash = digest
        return eid

    @staticmethod
    def _event_hash(prev, event_type, action, biz_step, location, ts, actor, data_s, codes) -> str:
        h = hashlib.sha256()
        for part in (prev, event_type, action, biz_step, location or "", ts, actor, data_s):
            h.update(part.encode())
            h.update(b"\x1f")
        for c in codes:
            h.update(c.encode())
            h.update(b"\x1e")
        return h.hexdigest()

    def _resolve(self, conn, codes: Iterable[str]) -> list[sqlite3.Row]:
        keys = list(dict.fromkeys(_canon(c) for c in codes))
        if not keys:
            raise Invalid("no codes given")
        rows = conn.execute(
            "SELECT * FROM items WHERE code IN (SELECT value FROM json_each(?))",
            (orjson.dumps(keys).decode(),),
        ).fetchall()
        if len(rows) != len(keys):
            missing = set(keys) - {r["code"] for r in rows}
            raise NotFound(f"unknown codes: {sorted(missing)[:10]}")
        return rows

    @staticmethod
    def _descendants(conn, ids: Iterable[int]) -> list[tuple[int, str]]:
        """Roots plus everything packed inside them (any depth)."""
        return conn.execute(
            """WITH RECURSIVE tree(id) AS (
                 SELECT value FROM json_each(?)
                 UNION ALL SELECT i.id FROM items i JOIN tree t ON i.parent_id = t.id)
               SELECT items.id, items.code FROM tree JOIN items USING(id)""",
            (_json(ids),),
        ).fetchall()

    @staticmethod
    def _require_location(conn, gln: str) -> None:
        if not conn.execute("SELECT 1 FROM locations WHERE gln=?", (gln,)).fetchone():
            raise NotFound(f"unknown location {gln}")

    # --------------------------------------------------------- master data
    def add_product(self, gtin, name, strength=None, dosage_form=None, manufacturer=None, ndc=None):
        gtin = gs1.normalize_gtin(gtin)
        with self._tx() as c:
            c.execute("INSERT INTO products VALUES(?,?,?,?,?,?) ON CONFLICT(gtin) DO UPDATE SET"
                      " name=excluded.name, strength=excluded.strength, dosage_form=excluded.dosage_form,"
                      " manufacturer=excluded.manufacturer, ndc=excluded.ndc",
                      (gtin, name, strength, dosage_form, manufacturer, ndc))
        return {"gtin": gtin, "name": name}

    def add_location(self, gln, name, kind, address=None):
        if not gs1.valid_key(gln, 13):
            raise Invalid(f"invalid GLN: {gln}")
        with self._tx() as c:
            c.execute("INSERT INTO locations VALUES(?,?,?,?) ON CONFLICT(gln) DO UPDATE SET"
                      " name=excluded.name, kind=excluded.kind, address=excluded.address",
                      (gln, name, kind, address))
        return {"gln": gln, "name": name, "kind": kind}

    def add_lot(self, gtin, lot, expiry: date, status="released"):
        gtin = gs1.normalize_gtin(gtin)
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM products WHERE gtin=?", (gtin,)).fetchone():
                raise NotFound(f"unknown product {gtin}")
            c.execute("INSERT INTO lots VALUES(?,?,?,?)", (gtin, lot, expiry.isoformat(), status))
        return {"gtin": gtin, "lot": lot, "expiry": expiry.isoformat(), "status": status}

    def set_lot_status(self, gtin, lot, status, actor="system", reason=None, signature=None):
        if status not in LOT_STATUSES:
            raise Invalid(f"status must be one of {sorted(LOT_STATUSES)}")
        gtin = gs1.normalize_gtin(gtin)
        with self._tx() as c:
            if not c.execute("UPDATE lots SET status=? WHERE gtin=? AND lot=?", (status, gtin, lot)).rowcount:
                raise NotFound(f"unknown lot {gtin}/{lot}")
            self._record(c, "SystemEvent", "OBSERVE", f"lot_{status}", None, actor,
                         {"gtin": gtin, "lot": lot, "status": status, "reason": reason, "signature": signature}, [])
        return {"gtin": gtin, "lot": lot, "status": status}

    # ------------------------------------------------------- serialization
    def commission(self, gtin, lot, count, location, actor="system", serials=None):
        """Generate and register `count` serialized units of one lot at a site."""
        gtin = gs1.normalize_gtin(gtin)
        if serials is None:
            if not 1 <= count <= 1_000_000:
                raise Invalid("count must be 1..1,000,000")
            serials = random_serials(count)
        elif bad := [s for s in serials if not gs1.SERIAL_RE.match(s)]:
            raise Invalid(f"invalid serials: {bad[:5]}")
        with self._tx() as c:
            self._require_location(c, location)
            expiry = self._commission(c, gtin, lot, serials, location, actor, {"source": "generated"})
        prefix = gs1.hri(gtin, "", lot, date.fromisoformat(expiry))  # ends with "(21)"
        return [prefix + s for s in serials]

    def import_serials(self, records, location, actor="system", source="import"):
        """Register serials assigned elsewhere (line system, L4 platform, CMO).

        `records` is an iterable of (gtin, serial, lot). All-or-nothing: one bad
        or duplicate serial rejects the whole file.
        """
        groups: dict[tuple[str, str], list[str]] = {}
        n = 0
        for i, (gtin, serial, lot) in enumerate(records, 1):
            try:
                gtin = gs1.normalize_gtin(str(gtin))
            except gs1.GS1Error as e:
                raise Invalid(f"record {i}: {e}") from e
            serial, lot = str(serial).strip(), str(lot or "").strip()
            if not gs1.SERIAL_RE.match(serial):
                raise Invalid(f"record {i}: invalid serial {serial!r}")
            if not lot:
                raise Invalid(f"record {i}: lot is required")
            groups.setdefault((gtin, lot), []).append(serial)
            n += 1
        if not n:
            raise Invalid("no serials in import")
        if n > MAX_BATCH:
            raise Invalid(f"import limited to {MAX_BATCH:,} serials per file")
        with self._tx() as c:
            self._require_location(c, location)
            for (gtin, lot), serials in groups.items():
                self._commission(c, gtin, lot, serials, location, actor, {"source": source})
        return {"imported": n, "lots": [{"gtin": g, "lot": l, "count": len(v)} for (g, l), v in groups.items()]}

    def _commission(self, c, gtin, lot, serials, location, actor, extra) -> str:
        """Insert unit rows + commissioning event inside an open transaction. Returns lot expiry."""
        lot_row = c.execute("SELECT expiry FROM lots WHERE gtin=? AND lot=?", (gtin, lot)).fetchone()
        if not lot_row:
            raise NotFound(f"unknown lot {gtin}/{lot}")
        if len(set(serials)) != len(serials):
            raise Invalid(f"duplicate serials within the batch for {gtin}/{lot}")
        ts = _now()
        start = c.execute("SELECT COALESCE(MAX(id),0) FROM items").fetchone()[0]
        try:
            c.executemany(
                "INSERT INTO items(code,kind,gtin,serial,lot,status,location,updated_at)"
                " VALUES(?,'unit',?,?,?,'active',?,?)",
                ((f"01{gtin}21{s}", gtin, s, lot, location, ts) for s in serials),
            )
        except sqlite3.IntegrityError as e:
            dup = c.execute("SELECT code FROM items WHERE code IN (SELECT value FROM json_each(?)) LIMIT 10",
                            (orjson.dumps([f"01{gtin}21{s}" for s in serials]).decode(),)).fetchall()
            raise TnTError(f"serials already registered: {[d[0] for d in dup]}") from e
        rows = c.execute("SELECT id, code FROM items WHERE id > ? ORDER BY id", (start,)).fetchall()
        self._record(c, "ObjectEvent", "ADD", "commissioning", location, actor,
                     {"gtin": gtin, "lot": lot, "count": len(rows), **extra}, rows)
        return lot_row["expiry"]

    def create_container(self, kind, location, company_prefix, extension=0, actor="system"):
        return self.create_containers(kind, location, company_prefix, 1, extension, actor)[0]

    def create_containers(self, kind, location, company_prefix, count, extension=0, actor="system"):
        """Allocate `count` SSCC-labelled cases or pallets from the prefix's sequence."""
        if kind not in ("case", "pallet"):
            raise Invalid("kind must be case or pallet")
        if not 1 <= count <= 100_000:
            raise Invalid("count must be 1..100,000")
        with self._tx() as c:
            self._require_location(c, location)
            last = c.execute(
                "INSERT INTO sequences VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value"
                " RETURNING value", (f"sscc:{extension}:{company_prefix}", count)).fetchone()[0]
            try:
                ssccs = [gs1.build_sscc(extension, company_prefix, ref) for ref in range(last - count + 1, last + 1)]
            except gs1.GS1Error as e:
                raise Invalid(str(e)) from e
            self._insert_containers(c, kind, location, ["00" + s for s in ssccs], actor)
        return [f"(00){s}" for s in ssccs]

    def _insert_containers(self, c, kind, location, codes, actor):
        start = c.execute("SELECT COALESCE(MAX(id),0) FROM items").fetchone()[0]
        try:
            c.executemany("INSERT INTO items(code,kind,status,location,updated_at) VALUES(?,?,'active',?,?)",
                          ((code, kind, location, _now()) for code in codes))
        except sqlite3.IntegrityError as e:
            raise TnTError("SSCC already registered") from e
        rows = c.execute("SELECT id, code FROM items WHERE id > ? ORDER BY id", (start,)).fetchall()
        self._record(c, "ObjectEvent", "ADD", "commissioning", location, actor,
                     {"kind": kind, "count": len(rows)}, rows)

    # ---------------------------------------------------------- aggregation
    def aggregate(self, parent, children, actor="system"):
        return self.aggregate_many({parent: children}, actor)[0]

    def aggregate_many(self, packing: dict[str, list[str]], actor="system"):
        """Pack several parents in one transaction (e.g. a packaging line's batch upload)."""
        with self._tx() as c:
            return [self._aggregate(c, parent, children, actor) for parent, children in packing.items()]

    def _aggregate(self, c, parent, children, actor):
        p = self._resolve(c, [parent])[0]
        kids = self._resolve(c, children)
        if p["kind"] == "unit":
            raise Invalid("a unit cannot contain other items")
        if p["status"] != "active":
            raise TnTError(f"parent is {p['status']}")
        for k in kids:
            if k["id"] == p["id"]:
                raise Invalid("item cannot contain itself")
            if k["status"] != "active" or k["location"] != p["location"]:
                raise TnTError(f"{k['code']} is not active at the parent's location")
            if k["parent_id"] is not None:
                raise TnTError(f"{k['code']} is already packed; disaggregate first")
            if p["kind"] == "case" and k["kind"] != "unit":
                raise Invalid("cases may only contain units")
            if k["kind"] == "pallet":
                raise Invalid("pallets cannot be nested")
        ids = [k["id"] for k in kids]
        c.execute("UPDATE items SET parent_id=?, updated_at=? WHERE id IN (SELECT value FROM json_each(?))",
                  (p["id"], _now(), _json(ids)))
        self._record(c, "AggregationEvent", "ADD", "packing", p["location"], actor,
                     {"parent": p["code"], "children": len(ids)},
                     [(p["id"], p["code"])] + [(k["id"], k["code"]) for k in kids])
        return {"parent": p["code"], "packed": len(ids)}

    def disaggregate(self, parent, children=None, actor="system"):
        """Unpack specific children, or everything when `children` is omitted."""
        with self._tx() as c:
            p = self._resolve(c, [parent])[0]
            if children:
                kids = self._resolve(c, children)
                if any(k["parent_id"] != p["id"] for k in kids):
                    raise TnTError("some children are not in this parent")
            else:
                kids = c.execute("SELECT * FROM items WHERE parent_id=?", (p["id"],)).fetchall()
            c.execute("UPDATE items SET parent_id=NULL, updated_at=? WHERE id IN (SELECT value FROM json_each(?))",
                      (_now(), _json(k["id"] for k in kids)))
            self._record(c, "AggregationEvent", "DELETE", "unpacking", p["location"], actor,
                         {"parent": p["code"], "children": len(kids)},
                         [(p["id"], p["code"])] + [(k["id"], k["code"]) for k in kids])
        return {"parent": p["code"], "unpacked": len(kids)}

    # -------------------------------------------------------- chain of custody
    def ship(self, codes, from_gln, to_gln, actor="system", reference=None):
        with self._tx() as c:
            self._require_location(c, to_gln)
            roots = self._resolve(c, codes)
            for r in roots:
                if r["status"] != "active" or r["location"] != from_gln:
                    raise TnTError(f"{r['code']} is not active at {from_gln}")
                if r["parent_id"] is not None:
                    raise TnTError(f"{r['code']} is packed; ship its outermost container")
            tree = self._descendants(c, (r["id"] for r in roots))
            c.execute("UPDATE items SET status='in_transit', location=?, updated_at=?"
                      " WHERE id IN (SELECT value FROM json_each(?))",
                      (to_gln, _now(), _json(t[0] for t in tree)))
            eid = self._record(c, "ObjectEvent", "OBSERVE", "shipping", from_gln, actor,
                               {"destination": to_gln, "reference": reference}, tree)
        return {"shipment_id": eid, "shipped": len(roots), "items": len(tree)}

    def receive(self, codes, at_gln, actor="system", reference=None):
        with self._tx() as c:
            roots = self._resolve(c, codes)
            for r in roots:
                if r["status"] != "in_transit" or r["location"] != at_gln:
                    raise TnTError(f"{r['code']} is not in transit to {at_gln}")
            tree = self._descendants(c, (r["id"] for r in roots))
            c.execute("UPDATE items SET status='active', updated_at=? WHERE id IN (SELECT value FROM json_each(?))",
                      (_now(), _json(t[0] for t in tree)))
            self._record(c, "ObjectEvent", "OBSERVE", "receiving", at_gln, actor,
                         {"reference": reference}, tree)
        return {"received": len(roots), "items": len(tree)}

    def dispense(self, codes, at_gln, actor="system"):
        """Supply to patient. Blocks recalled / quarantined / expired packs."""
        today = date.today().isoformat()
        with self._tx() as c:
            units = self._resolve(c, codes)
            for u in units:
                if u["kind"] != "unit":
                    raise Invalid(f"{u['code']} is a container")
                if u["status"] != "active" or u["location"] != at_gln:
                    raise TnTError(f"{u['code']} is not active at {at_gln} (status {u['status']})")
                lot = c.execute("SELECT status, expiry FROM lots WHERE gtin=? AND lot=?",
                                (u["gtin"], u["lot"])).fetchone()
                if lot["status"] != "released":
                    raise TnTError(f"{u['code']}: lot is {lot['status']}")
                if lot["expiry"] < today:
                    raise TnTError(f"{u['code']}: expired {lot['expiry']}")
            c.execute("UPDATE items SET status='dispensed', parent_id=NULL, updated_at=?"
                      " WHERE id IN (SELECT value FROM json_each(?))",
                      (_now(), _json(u["id"] for u in units)))
            self._record(c, "ObjectEvent", "DELETE", "dispensing", at_gln, actor, {},
                         [(u["id"], u["code"]) for u in units])
        return {"dispensed": len(units)}

    def decommission(self, codes, reason, actor="system", signature=None):
        if reason not in DECOMMISSION_REASONS:
            raise Invalid(f"reason must be one of {sorted(DECOMMISSION_REASONS)}")
        with self._tx() as c:
            roots = self._resolve(c, codes)
            if any(r["status"] in ("dispensed", *DECOMMISSION_REASONS) for r in roots):
                raise TnTError("item already out of the supply chain")
            tree = self._descendants(c, (r["id"] for r in roots))
            c.execute("UPDATE items SET status=?, updated_at=? WHERE id IN (SELECT value FROM json_each(?))",
                      (reason, _now(), _json(t[0] for t in tree)))
            self._record(c, "ObjectEvent", "DELETE", "decommissioning", roots[0]["location"], actor,
                         {"reason": reason, "signature": signature}, tree)
        return {"decommissioned": len(tree)}

    # ------------------------------------------------------------- queries
    def verify(self, code: str) -> dict:
        """Authenticity / saleability check (DSCSA VRS / EU FMD style)."""
        try:
            parsed = gs1.parse(code)
        except gs1.GS1Error as e:
            return {"code": code, "verified": False, "reasons": [str(e)]}
        r = self.db.reader()
        item = r.execute("SELECT * FROM items WHERE code=?", (parsed.key,)).fetchone()
        if not item:
            return {"code": parsed.key, "verified": False, "reasons": ["serial not found - suspect product"]}
        reasons = []
        out = {"code": parsed.key, "kind": item["kind"], "status": item["status"], "location": item["location"]}
        if item["status"] not in ("active", "in_transit"):
            reasons.append(f"status is {item['status']}")
        if item["kind"] == "unit":
            lot = r.execute("SELECT * FROM lots WHERE gtin=? AND lot=?", (item["gtin"], item["lot"])).fetchone()
            prod = r.execute("SELECT name, strength FROM products WHERE gtin=?", (item["gtin"],)).fetchone()
            out |= {"product": dict(prod), "lot": lot["lot"], "expiry": lot["expiry"], "lot_status": lot["status"]}
            if lot["status"] != "released":
                reasons.append(f"lot is {lot['status']}")
            if lot["expiry"] < date.today().isoformat():
                reasons.append("expired")
            if parsed.lot is not None and parsed.lot != lot["lot"]:
                reasons.append("lot on pack does not match record")
            if parsed.expiry is not None and parsed.expiry.isoformat() != lot["expiry"]:
                reasons.append("expiry on pack does not match record")
        return out | {"verified": not reasons, "reasons": reasons}

    def trace(self, code: str, max_children: int = 100) -> dict:
        r = self.db.reader()
        key = _canon(code)
        item = r.execute("SELECT * FROM items WHERE code=?", (key,)).fetchone()
        if not item:
            raise NotFound(f"unknown code {key}")
        ancestors = r.execute(
            """WITH RECURSIVE up(id, depth) AS (
                 SELECT parent_id, 1 FROM items WHERE id=? AND parent_id IS NOT NULL
                 UNION ALL SELECT i.parent_id, depth+1 FROM items i JOIN up ON i.id=up.id
                 WHERE i.parent_id IS NOT NULL)
               SELECT items.code, items.kind FROM up JOIN items USING(id) ORDER BY depth""",
            (item["id"],)).fetchall()
        children = r.execute("SELECT code, kind, status FROM items WHERE parent_id=? LIMIT ?",
                             (item["id"], max_children)).fetchall()
        n_children = r.execute("SELECT COUNT(*) FROM items WHERE parent_id=?", (item["id"],)).fetchone()[0]
        events = r.execute(
            "SELECT e.id, e.event_type, e.action, e.biz_step, e.location, e.ts, e.actor, e.data, e.hash"
            " FROM event_items ei JOIN events e ON e.id=ei.event_id WHERE ei.item_id=? ORDER BY e.id",
            (item["id"],)).fetchall()
        return {
            "item": {k: item[k] for k in ("code", "kind", "gtin", "serial", "lot", "status", "location", "updated_at")},
            "contained_in": [dict(a) for a in ancestors],
            "contains": {"count": n_children, "items": [dict(ch) for ch in children]},
            "history": [dict(e) | {"data": orjson.loads(e["data"])} for e in events],
        }

    def recall(self, gtin, lot, actor="system", reason=None, signature=None) -> dict:
        """Mark a lot recalled and return where every affected pack is right now."""
        self.set_lot_status(gtin, lot, "recalled", actor, reason, signature)
        return self.whereabouts(gtin, lot)

    def whereabouts(self, gtin, lot) -> dict:
        gtin = gs1.normalize_gtin(gtin)
        r = self.db.reader()
        by_loc = r.execute(
            "SELECT i.location, l.name AS location_name, i.status, COUNT(*) AS units FROM items i"
            " LEFT JOIN locations l ON l.gln=i.location WHERE i.gtin=? AND i.lot=?"
            " GROUP BY i.location, i.status ORDER BY units DESC", (gtin, lot)).fetchall()
        containers = r.execute(
            """WITH RECURSIVE up(id, parent_id) AS (
                 SELECT id, parent_id FROM items WHERE gtin=? AND lot=?
                 UNION SELECT i.id, i.parent_id FROM items i JOIN up ON i.id=up.parent_id)
               SELECT code, kind, status, location FROM items
               WHERE id IN (SELECT id FROM up WHERE parent_id IS NULL) AND kind<>'unit'""",
            (gtin, lot)).fetchall()
        return {"gtin": gtin, "lot": lot, "by_location": [dict(x) for x in by_loc],
                "top_level_containers": [dict(x) for x in containers]}

    def verify_audit_chain(self) -> dict:
        """Recompute every event hash; detects any edited, inserted or deleted event."""
        r = self.db.reader()
        prev, n = GENESIS, 0
        for e in r.execute("SELECT * FROM events ORDER BY id"):
            codes = (row[0] for row in r.execute(
                "SELECT i.code FROM event_items ei JOIN items i ON i.id=ei.item_id"
                " WHERE ei.event_id=? ORDER BY ei.item_id", (e["id"],)))
            expected = self._event_hash(prev, e["event_type"], e["action"], e["biz_step"], e["location"],
                                        e["ts"], e["actor"], e["data"], codes)
            if e["prev_hash"] != prev or e["hash"] != expected:
                return {"valid": False, "events_checked": n, "broken_at_event": e["id"]}
            prev, n = e["hash"], n + 1
        return {"valid": True, "events_checked": n, "head": prev}

    def stats(self) -> dict:
        r = self.db.reader()
        return {
            "items_by_status": {row[0]: row[1] for row in r.execute("SELECT status, COUNT(*) FROM items GROUP BY 1")},
            "events": r.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "products": r.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "lots": r.execute("SELECT COUNT(*) FROM lots").fetchone()[0],
        }

    # ------------------------------------------------------------- listings
    def list_products(self):
        return [dict(r) for r in self.db.reader().execute("SELECT * FROM products ORDER BY name")]

    def list_locations(self):
        return [dict(r) for r in self.db.reader().execute("SELECT * FROM locations ORDER BY name")]

    def list_lots(self, gtin=None):
        q = ("SELECT l.*, p.name AS product,"
             " (SELECT COUNT(*) FROM items i WHERE i.gtin=l.gtin AND i.lot=l.lot) AS units"
             " FROM lots l JOIN products p USING(gtin)")
        args: tuple = ()
        if gtin:
            q, args = q + " WHERE l.gtin=?", (gs1.normalize_gtin(gtin),)
        return [dict(r) for r in self.db.reader().execute(q + " ORDER BY l.expiry DESC", args)]

    def events(self, limit=100, before_id=None, biz_step=None):
        q, args = "SELECT e.*, (SELECT COUNT(*) FROM event_items WHERE event_id=e.id) AS items FROM events e WHERE 1=1", []
        if before_id:
            q += " AND e.id < ?"
            args.append(before_id)
        if biz_step:
            q += " AND e.biz_step = ?"
            args.append(biz_step)
        rows = self.db.reader().execute(q + " ORDER BY e.id DESC LIMIT ?", (*args, min(limit, 1000)))
        return [dict(r) | {"data": orjson.loads(r["data"])} for r in rows]

    def shipments(self, limit=100):
        return self.events(limit, biz_step="shipping")

    # -------------------------------------------------------------- reports
    def lot_serials(self, gtin, lot) -> Iterator[sqlite3.Row]:
        gtin = gs1.normalize_gtin(gtin)
        return self.db.reader().execute(
            "SELECT i.serial, i.code, i.status, i.location, p.code AS container, i.updated_at"
            " FROM items i LEFT JOIN items p ON p.id=i.parent_id WHERE i.gtin=? AND i.lot=? ORDER BY i.id",
            (gtin, lot))

    def audit_rows(self, since=None, until=None) -> Iterator[sqlite3.Row]:
        return self.db.reader().execute(
            "SELECT e.id, e.ts, e.actor, e.event_type, e.action, e.biz_step, e.location, e.data,"
            " (SELECT COUNT(*) FROM event_items WHERE event_id=e.id) AS items, e.prev_hash, e.hash"
            " FROM events e WHERE e.ts >= ? AND e.ts <= ? ORDER BY e.id",
            (since or "", (until or "9999") + "~"))  # "~" sorts after any ISO suffix -> inclusive until
