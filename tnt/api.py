"""REST API. Run: uvicorn tnt.api:app --workers 1

Configuration (environment variables):
  TNT_DB               database file                      (default tnt.db)
  TNT_IDLE_MINUTES     auto-logoff after inactivity       (default 30)
  TNT_SESSION_HOURS    maximum session length             (default 12)
  TNT_ADMIN_USER / TNT_ADMIN_PASSWORD   bootstrap the first admin on an empty database
  TNT_COMPANY_NAME     shown in the web UI header
"""
from __future__ import annotations

import csv
import io
import os
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import epcis, importers
from .auth import Auth
from .core import Forbidden, Invalid, TnTError, TrackTrace
from .gs1 import GS1Error

VERSION = "2.1.0"
STATIC = Path(__file__).parent / "static"

tt = TrackTrace(os.environ.get("TNT_DB", "tnt.db"))
auth = Auth(tt, int(os.environ.get("TNT_IDLE_MINUTES", 30)), int(os.environ.get("TNT_SESSION_HOURS", 12)))
if not auth.has_users() and os.environ.get("TNT_ADMIN_PASSWORD"):
    auth.create_user(os.environ.get("TNT_ADMIN_USER", "admin"), "Administrator",
                     os.environ["TNT_ADMIN_PASSWORD"], "admin", must_change=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    tt.close()


app = FastAPI(title="Pharma Track & Trace", version=VERSION, lifespan=lifespan)


@app.exception_handler(TnTError)
async def _tnt_error(_: Request, e: TnTError):
    return JSONResponse({"error": str(e)}, status_code=e.status)


@app.exception_handler(GS1Error)
async def _gs1_error(_: Request, e: GS1Error):
    return JSONResponse({"error": str(e)}, status_code=422)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers.setdefault("Cache-Control", "no-store")
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi")):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
    return resp


# ------------------------------------------------------------------ auth deps
_bearer = HTTPBearer(auto_error=False)


def role(min_role: str):
    def dep(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
        user = auth.authenticate(cred.credentials if cred else None, min_role)
        if user["must_change_pw"]:
            raise TnTError("password change required before continuing")
        return user
    return dep


Viewer = Annotated[dict, Depends(role("viewer"))]
Operator = Annotated[dict, Depends(role("operator"))]
QA = Annotated[dict, Depends(role("qa"))]
Admin = Annotated[dict, Depends(role("admin"))]


class Signature(BaseModel):
    """Electronic signature: the signer re-enters their password and states the meaning."""
    password: str
    meaning: str = Field(min_length=3, max_length=200)


def _sign(user: dict, sig: Signature | None) -> dict:
    if sig is None:
        raise Invalid("electronic signature required (password + meaning)")
    return auth.sign(user["username"], sig.password, sig.meaning)


# ------------------------------------------------------------------- models
Codes = Field(min_length=1, max_length=100_000)


class Login(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    username: str
    old_password: str
    new_password: str


class NewUser(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    full_name: str = Field(min_length=1, max_length=200)
    password: str
    role: Literal["viewer", "operator", "qa", "admin"]


class UserUpdate(BaseModel):
    role: Literal["viewer", "operator", "qa", "admin"] | None = None
    active: bool | None = None
    full_name: str | None = None
    unlock: bool = False
    temporary_password: str | None = None


class Product(BaseModel):
    gtin: str
    name: str
    strength: str | None = None
    dosage_form: str | None = None
    manufacturer: str | None = None
    ndc: str | None = None


class Location(BaseModel):
    gln: str
    name: str
    kind: Literal["manufacturer", "cmo", "warehouse", "distributor", "pharmacy", "hospital", "other"]
    address: str | None = None


class Lot(BaseModel):
    gtin: str
    lot: str = Field(min_length=1, max_length=20)
    expiry: date
    status: Literal["released", "quarantine"] = "quarantine"


class LotStatus(BaseModel):
    status: Literal["released", "quarantine", "recalled"]
    reason: str | None = None
    signature: Signature | None = None


class Commission(BaseModel):
    gtin: str
    lot: str
    location: str
    count: int = Field(gt=0, le=1_000_000)


class SerialImport(BaseModel):
    location: str
    gtin: str | None = None
    lot: str | None = None
    serials: list[str] | None = Field(default=None, max_length=1_000_000)
    codes: list[str] | None = Field(default=None, max_length=1_000_000)


class Container(BaseModel):
    kind: Literal["case", "pallet"] = "case"
    location: str
    company_prefix: str
    extension: int = Field(default=0, ge=0, le=9)
    count: int = Field(default=1, gt=0, le=100_000)


class Aggregate(BaseModel):
    parent: str
    children: list[str] = Codes


class AggregateBatch(BaseModel):
    packing: dict[str, list[str]] = Field(min_length=1, max_length=10_000)


class Disaggregate(BaseModel):
    parent: str
    children: list[str] | None = None


class Ship(BaseModel):
    codes: list[str] = Codes
    from_gln: str
    to_gln: str
    reference: str | None = None


class Receive(BaseModel):
    codes: list[str] = Codes
    at_gln: str
    reference: str | None = None


class Dispense(BaseModel):
    codes: list[str] = Codes
    at_gln: str


class Decommission(BaseModel):
    codes: list[str] = Codes
    reason: Literal["destroyed", "stolen", "sample", "expired"]
    signature: Signature | None = None


class Recall(BaseModel):
    gtin: str
    lot: str
    reason: str = Field(min_length=3)
    signature: Signature | None = None


# --------------------------------------------------------------------- UI
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui():
    html = (STATIC / "index.html").read_text()
    return html.replace("{{COMPANY}}", os.environ.get("TNT_COMPANY_NAME", "Pharma Track & Trace"))


@app.get("/static/{name}", include_in_schema=False)
def static(name: str):
    path = (STATIC / name).resolve()
    if path.parent != STATIC.resolve() or not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")


@app.get("/health", include_in_schema=False)
def health():
    tt.db.reader().execute("SELECT 1").fetchone()
    return {"status": "ok", "version": VERSION}


# ------------------------------------------------------------- first run
_setup_lock = threading.Lock()
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


class Setup(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    full_name: str = Field(min_length=1, max_length=200)
    password: str


@app.get("/setup/status", include_in_schema=False)
def setup_status(request: Request):
    local = bool(request.client and request.client.host in LOCAL_HOSTS)
    return {"needs_setup": not auth.has_users(), "allowed": local}


@app.post("/setup", status_code=201, include_in_schema=False)
def setup(b: Setup, request: Request):
    """Create the first administrator. Only on an empty database and only from this machine."""
    if not (request.client and request.client.host in LOCAL_HOSTS):
        raise Forbidden("first-time setup is only allowed on the server itself")
    with _setup_lock:
        if auth.has_users():
            raise TnTError("setup already completed")
        auth.create_user(b.username, b.full_name, b.password, "admin", actor="setup", must_change=False)
    return {"ok": True}


# ---------------------------------------------------------------- session
@app.post("/auth/login", tags=["auth"])
def login(b: Login, request: Request):
    return auth.login(b.username, b.password, request.client.host if request.client else None)


@app.post("/auth/logout", tags=["auth"])
def logout(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    user = auth.authenticate(cred.credentials if cred else None)
    auth.logout(cred.credentials, user["username"])
    return {"ok": True}


@app.post("/auth/change-password", tags=["auth"])
def change_password(b: PasswordChange):
    auth.change_own_password(b.username, b.old_password, b.new_password)
    return {"ok": True}


@app.get("/auth/me", tags=["auth"])
def me(user: Viewer):
    return {k: user[k] for k in ("username", "full_name", "role")}


# ------------------------------------------------------------------ users
@app.get("/users", tags=["admin"])
def list_users(_: Admin):
    return auth.list_users()


@app.post("/users", status_code=201, tags=["admin"])
def create_user(b: NewUser, user: Admin):
    return auth.create_user(b.username, b.full_name, b.password, b.role, actor=user["username"])


@app.patch("/users/{username}", tags=["admin"])
def update_user(username: str, b: UserUpdate, user: Admin):
    if username.lower() == user["username"].lower() and (b.active is False or (b.role and b.role != "admin")):
        raise Invalid("you cannot deactivate or demote your own account")
    if b.temporary_password:
        auth.set_password(username, b.temporary_password, actor=user["username"], temporary=True)
    return auth.update_user(username, user["username"], b.role, b.active, b.full_name, b.unlock)


# ------------------------------------------------------------ master data
@app.get("/products", tags=["master data"])
def list_products(_: Viewer):
    return tt.list_products()


@app.post("/products", status_code=201, tags=["master data"])
def add_product(p: Product, _: Admin):
    return tt.add_product(**p.model_dump())


@app.get("/locations", tags=["master data"])
def list_locations(_: Viewer):
    return tt.list_locations()


@app.post("/locations", status_code=201, tags=["master data"])
def add_location(loc: Location, _: Admin):
    return tt.add_location(**loc.model_dump())


@app.get("/lots", tags=["master data"])
def list_lots(_: Viewer, gtin: str | None = None):
    return tt.list_lots(gtin)


@app.post("/lots", status_code=201, tags=["master data"])
def add_lot(lot: Lot, _: QA):
    return tt.add_lot(lot.gtin, lot.lot, lot.expiry, lot.status)


@app.post("/lots/{gtin}/{lot}/status", tags=["master data"])
def set_lot_status(gtin: str, lot: str, b: LotStatus, user: QA):
    sig = _sign(user, b.signature)
    if b.status == "recalled":
        return tt.recall(gtin, lot, user["username"], b.reason, sig)
    return tt.set_lot_status(gtin, lot, b.status, user["username"], b.reason, sig)


# --------------------------------------------------------- serialization
@app.post("/commission", status_code=201, tags=["serialization"])
def commission(b: Commission, user: Operator):
    codes = tt.commission(b.gtin, b.lot, b.count, b.location, actor=user["username"])
    return {"count": len(codes), "codes": codes}


@app.post("/serials/import", status_code=201, tags=["serialization"])
def import_serials(b: SerialImport, user: Operator):
    """Register serials assigned elsewhere: `serials` (+ gtin & lot) or `codes` (barcode scans / EPC URIs)."""
    if b.codes:
        records = importers.records_from_codes(b.codes, b.lot)
    elif b.serials:
        if not (b.gtin and b.lot):
            raise Invalid("gtin and lot are required with a serial list")
        records = ((b.gtin, s, b.lot) for s in b.serials)
    else:
        raise Invalid("provide serials or codes")
    return tt.import_serials(records, b.location, actor=user["username"], source="api")


@app.post("/serials/import/csv", status_code=201, tags=["serialization"])
def import_serials_csv(user: Operator, location: str, gtin: str | None = None, lot: str | None = None,
                       body: str = Body(..., media_type="text/csv")):
    """CSV upload (request body). Columns: gtin, serial, lot - or a single code/barcode column."""
    records = importers.records_from_csv(body, default_lot=lot, default_gtin=gtin)
    return tt.import_serials(records, location, actor=user["username"], source="csv")


@app.post("/containers", status_code=201, tags=["serialization"])
def create_containers(b: Container, user: Operator):
    return {"ssccs": tt.create_containers(b.kind, b.location, b.company_prefix, b.count, b.extension,
                                          actor=user["username"])}


# ------------------------------------------------------------ aggregation
@app.post("/aggregate", tags=["packing"])
def aggregate(b: Aggregate, user: Operator):
    return tt.aggregate(b.parent, b.children, actor=user["username"])


@app.post("/aggregate/batch", tags=["packing"])
def aggregate_batch(b: AggregateBatch, user: Operator):
    return tt.aggregate_many(b.packing, actor=user["username"])


@app.post("/disaggregate", tags=["packing"])
def disaggregate(b: Disaggregate, user: Operator):
    return tt.disaggregate(b.parent, b.children, actor=user["username"])


# ---------------------------------------------------------------- custody
@app.post("/ship", tags=["custody"])
def ship(b: Ship, user: Operator):
    return tt.ship(b.codes, b.from_gln, b.to_gln, actor=user["username"], reference=b.reference)


@app.get("/shipments", tags=["custody"])
def shipments(_: Viewer, limit: int = Query(100, le=1000)):
    return tt.shipments(limit)


@app.post("/receive", tags=["custody"])
def receive(b: Receive, user: Operator):
    return tt.receive(b.codes, b.at_gln, actor=user["username"], reference=b.reference)


@app.post("/dispense", tags=["custody"])
def dispense(b: Dispense, user: Operator):
    return tt.dispense(b.codes, b.at_gln, actor=user["username"])


@app.post("/decommission", tags=["custody"])
def decommission(b: Decommission, user: QA):
    return tt.decommission(b.codes, b.reason, actor=user["username"], signature=_sign(user, b.signature))


# ---------------------------------------------------------------- queries
@app.get("/verify", tags=["query"])
def verify(code: str, _: Viewer):
    return tt.verify(code)


@app.get("/trace", tags=["query"])
def trace(code: str, _: Viewer):
    return tt.trace(code)


@app.post("/recall", tags=["quality"])
def recall(b: Recall, user: QA):
    return tt.recall(b.gtin, b.lot, user["username"], b.reason, _sign(user, b.signature))


@app.get("/lots/{gtin}/{lot}/whereabouts", tags=["quality"])
def whereabouts(gtin: str, lot: str, _: Viewer):
    return tt.whereabouts(gtin, lot)


@app.get("/stats", tags=["query"])
def stats(_: Viewer):
    return tt.stats()


@app.get("/events", tags=["audit"])
def events(_: Viewer, limit: int = Query(100, le=1000), before_id: int | None = None, biz_step: str | None = None):
    return tt.events(limit, before_id, biz_step)


@app.get("/audit/verify", tags=["audit"])
def audit(_: QA):
    return tt.verify_audit_chain()


# ------------------------------------------------------------------ EPCIS
@app.get("/epcis/shipments/{shipment_id}", tags=["epcis"])
def epcis_shipment(shipment_id: int, _: Operator):
    """EPCIS 2.0 document for one shipment - send to the receiving trading partner."""
    doc = epcis.export_shipment(tt, shipment_id)
    return JSONResponse(doc, media_type="application/ld+json", headers={
        "Content-Disposition": f'attachment; filename="shipment-{shipment_id}.epcis.json"'})


@app.get("/epcis/events", tags=["epcis"])
def epcis_events(_: QA, since: str | None = None, until: str | None = None, limit: int = Query(1000, le=10_000)):
    return JSONResponse(epcis.export_events(tt, since, until, limit), media_type="application/ld+json")


@app.post("/epcis/import", status_code=201, tags=["epcis"])
def epcis_import(user: QA, location: str, doc: dict = Body(...)):
    """Import commissioning + aggregation events (e.g. from a CMO) into `location`."""
    return epcis.import_document(tt, doc, location, actor=user["username"])


# ---------------------------------------------------------------- reports
def _csv(filename: str, header: list[str], rows) -> StreamingResponse:
    def safe(v):  # neutralise spreadsheet formula injection
        s = "" if v is None else str(v)
        return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        for i, row in enumerate(rows):
            w.writerow([safe(v) for v in row])
            if i % 5000 == 0:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/reports/lots/{gtin}/{lot}/serials.csv", tags=["reports"])
def report_lot_serials(gtin: str, lot: str, _: Viewer):
    return _csv(f"serials-{gtin}-{lot}.csv", ["serial", "code", "status", "location", "container", "updated_at"],
                (tuple(r) for r in tt.lot_serials(gtin, lot)))


@app.get("/reports/lots/{gtin}/{lot}/whereabouts.csv", tags=["reports"])
def report_whereabouts(gtin: str, lot: str, _: Viewer):
    w = tt.whereabouts(gtin, lot)
    return _csv(f"whereabouts-{gtin}-{lot}.csv", ["location", "location_name", "status", "units"],
                ((r["location"], r["location_name"], r["status"], r["units"]) for r in w["by_location"]))


@app.get("/reports/audit.csv", tags=["reports"])
def report_audit(_: QA, since: str | None = None, until: str | None = None):
    return _csv("audit-trail.csv", ["id", "timestamp_utc", "user", "event_type", "action", "step", "location",
                                    "data", "items", "prev_hash", "hash"],
                (tuple(r) for r in tt.audit_rows(since, until)))
