"""Auth, roles, e-signatures, serial import, EPCIS exchange, reports, API."""
import importlib
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from tnt import epcis, gs1, importers
from tnt.auth import Auth
from tnt.core import Invalid, TnTError, TrackTrace, Unauthorized


def key(body):
    return body + gs1.check_digit(body)


GTIN = key("0031234567890")
MFG, DC = key("061414100001"), key("061414100002")
EXP = date.today() + timedelta(days=365)
PW = "Correct-horse-42"


def setup(tt):
    tt.add_product(GTIN, "Amoxicillin", "500 mg", "capsule", "Acme", "61234-001-30")
    tt.add_location(MFG, "Plant", "manufacturer")
    tt.add_location(DC, "DC", "distributor")
    tt.add_lot(GTIN, "L1", EXP)


@pytest.fixture
def tt(tmp_path):
    t = TrackTrace(str(tmp_path / "t.db"))
    setup(t)
    yield t
    t.close()


# ------------------------------------------------------------------ auth
def test_login_lockout_and_signature(tt):
    auth = Auth(tt)
    auth.create_user("jdoe", "Jane Doe", PW, "qa", must_change=False)
    assert auth.authenticate(auth.login("jdoe", PW)["token"], "qa")["username"] == "jdoe"
    for _ in range(4):
        with pytest.raises(Unauthorized, match="invalid"):
            auth.login("jdoe", "wrong-password-1")
    with pytest.raises(Unauthorized, match="locked"):
        auth.login("jdoe", "wrong-password-1")
    with pytest.raises(Unauthorized):
        auth.login("jdoe", PW)  # locked even with the right password
    auth.update_user("jdoe", "admin", unlock=True)
    sig = auth.sign("jdoe", PW, "Approved recall")
    assert sig["signer"] == "jdoe" and sig["meaning"] == "Approved recall"
    steps = [e["biz_step"] for e in tt.events(50)]
    assert "auth_failed" in steps and "user_created" in steps
    assert tt.verify_audit_chain()["valid"]


def test_password_policy_and_roles(tt):
    auth = Auth(tt)
    with pytest.raises(Invalid):
        auth.create_user("bob", "Bob", "short", "viewer")
    auth.create_user("bob", "Bob", PW, "viewer", must_change=False)
    tok = auth.login("bob", PW)["token"]
    with pytest.raises(TnTError, match="requires role operator"):
        auth.authenticate(tok, "operator")
    auth.update_user("bob", "admin", active=False)
    with pytest.raises(Unauthorized):
        auth.authenticate(tok)


# --------------------------------------------------------------- import
def test_import_serials_from_scans_and_csv(tt):
    scans = [f"]d201{GTIN}10L1{gs1.GS}21SCAN{i:04d}" for i in range(5)]
    r = tt.import_serials(importers.records_from_codes(scans), MFG)
    assert r["imported"] == 5
    csv_text = "GTIN;Serial;Batch\n" + "\n".join(f"{GTIN};CSV{i:04d};L1" for i in range(3))
    assert tt.import_serials(importers.records_from_csv(csv_text), MFG)["imported"] == 3
    assert tt.verify(f"(01){GTIN}(21)CSV0001")["verified"]

    # all-or-nothing: one duplicate rejects the whole file
    with pytest.raises(TnTError, match="already registered"):
        tt.import_serials([(GTIN, "NEW1", "L1"), (GTIN, "SCAN0001", "L1")], MFG)
    assert not tt.verify(f"(01){GTIN}(21)NEW1")["verified"]
    with pytest.raises(Invalid, match="lot is required"):
        tt.import_serials([(GTIN, "X1", "")], MFG)


# ---------------------------------------------------------------- EPCIS
def test_epcis_shipment_roundtrip_between_two_companies(tmp_path, tt):
    units = tt.commission(GTIN, "L1", 6, MFG)
    case = tt.create_container("case", MFG, "0614141")
    tt.aggregate(case, units[:4])
    shipment = tt.ship([case], MFG, DC, reference="PO-1")["shipment_id"]
    doc = epcis.export_shipment(tt, shipment)

    events = doc["epcisBody"]["eventList"]
    assert [e["bizStep"] for e in events] == ["commissioning", "commissioning", "packing", "shipping"]
    assert len(events[0]["epcList"]) == 4  # only the shipped units, not all 6
    assert events[0]["ilmd"]["cbvmda:lotNumber"] == "L1"
    assert doc["epcisHeader"]["epcisMasterData"]["vocabularyList"][0]["vocabularyElementList"][0]["id"].endswith(GTIN)

    # receiving company imports the document
    other = TrackTrace(str(tmp_path / "partner.db"))
    other.add_product(GTIN, "Amoxicillin")
    other.add_location(DC, "DC", "distributor")
    stats = epcis.import_document(other, json.loads(json.dumps(doc)), DC, actor="edi")
    assert stats == {"containers": 1, "lots_created": 1, "units": 4, "aggregations": 1, "skipped_events": 1}
    v = other.verify(units[0])
    assert v["lot_status"] == "quarantine" and "lot is quarantine" in v["reasons"]
    assert other.trace(units[0])["contained_in"][0]["code"] == gs1.parse(case).key
    other.close()


def test_epcis_import_accepts_urns(tt):
    doc = {"epcisBody": {"eventList": [
        {"type": "ObjectEvent", "action": "ADD", "bizStep": "urn:epcglobal:cbv:bizstep:commissioning",
         "epcList": ["urn:epc:id:sgtin:0312345.067890.ABC1"], "ilmd": {"cbvmda:lotNumber": "L1"}}]}}
    assert epcis.import_document(tt, doc, MFG)["units"] == 1
    assert tt.verify(f"(01){GTIN}(21)ABC1")["verified"]


# ------------------------------------------------------------------ API
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TNT_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("TNT_ADMIN_PASSWORD", "Initial-admin-1")
    import tnt.api
    api = importlib.reload(tnt.api)
    setup(api.tt)
    api.auth.create_user("qa1", "Quinn QA", PW, "qa", must_change=False)
    api.auth.create_user("op1", "Olivia Op", PW, "operator", must_change=False)
    with TestClient(api.app) as c:
        yield c


def login(c, user, pw=PW):
    r = c.post("/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_api_requires_auth_and_forced_password_change(client):
    assert client.get("/stats").status_code == 401
    admin = login(client, "admin", "Initial-admin-1")
    assert client.get("/stats", headers=admin).status_code == 409  # must change password first
    assert client.post("/auth/change-password", json={"username": "admin", "old_password": "Initial-admin-1",
                                                      "new_password": "Brand-new-pass-7"}).status_code == 200
    admin = login(client, "admin", "Brand-new-pass-7")
    assert client.get("/users", headers=admin).status_code == 200
    assert client.get("/users", headers=login(client, "op1")).status_code == 403


def test_api_workflow_with_signatures_and_reports(client):
    op, qa = login(client, "op1"), login(client, "qa1")
    r = client.post("/serials/import/csv", params={"location": MFG}, headers={**op, "Content-Type": "text/csv"},
                    content="gtin,serial,lot\n" + "\n".join(f"{GTIN},S{i},L1" for i in range(3)))
    assert r.status_code == 201, r.text
    codes = client.post("/commission", headers=op, json={"gtin": GTIN, "lot": "L1", "location": MFG, "count": 2}).json()["codes"]
    sscc = client.post("/containers", headers=op, json={"location": MFG, "company_prefix": "0614141"}).json()["ssccs"][0]
    assert client.post("/aggregate", headers=op, json={"parent": sscc, "children": codes}).json()["packed"] == 2
    sid = client.post("/ship", headers=op, json={"codes": [sscc], "from_gln": MFG, "to_gln": DC}).json()["shipment_id"]
    assert client.get(f"/epcis/shipments/{sid}", headers=op).json()["type"] == "EPCISDocument"

    # operators cannot recall; QA must sign
    assert client.post("/recall", headers=op, json={"gtin": GTIN, "lot": "L1", "reason": "OOS"}).status_code == 403
    assert client.post("/recall", headers=qa, json={"gtin": GTIN, "lot": "L1", "reason": "OOS"}).status_code == 422
    bad = {"password": "wrong-pass-123", "meaning": "Approved recall"}
    assert client.post("/recall", headers=qa, json={"gtin": GTIN, "lot": "L1", "reason": "OOS", "signature": bad}).status_code == 401
    good = {"password": PW, "meaning": "Approved recall"}
    r = client.post("/recall", headers=qa, json={"gtin": GTIN, "lot": "L1", "reason": "OOS", "signature": good})
    assert r.status_code == 200 and {x["location"] for x in r.json()["by_location"]} == {MFG, DC}

    csv_body = client.get(f"/reports/lots/{GTIN}/L1/serials.csv", headers=qa).text
    assert csv_body.startswith("serial,code,status") and csv_body.count("\n") == 6
    audit = client.get("/reports/audit.csv", headers=qa).text
    assert "Approved recall" in audit and "qa1" in audit
    assert client.get("/audit/verify", headers=qa).json()["valid"]
    page = client.get("/")
    assert "Content-Security-Policy" in page.headers and "app.js" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/..%2Fapi.py").status_code == 404


def test_first_run_setup_only_from_local_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("TNT_DB", str(tmp_path / "fresh.db"))
    monkeypatch.delenv("TNT_ADMIN_PASSWORD", raising=False)
    import tnt.api
    api = importlib.reload(tnt.api)
    body = {"username": "boss", "full_name": "Boss", "password": PW}
    with TestClient(api.app) as c:
        assert c.get("/setup/status").json() == {"needs_setup": True, "allowed": False}
        assert c.post("/setup", json=body).status_code == 403  # remote client
        monkeypatch.setattr(api, "LOCAL_HOSTS", {"testclient"})
        assert c.post("/setup", json=body).status_code == 201
        assert c.post("/setup", json=body | {"username": "evil"}).status_code == 409
        assert c.get("/setup/status").json()["needs_setup"] is False
        login(c, "boss")


def test_scanner_gs_placeholder():
    a = gs1.parse(f"01{GTIN}10LOT7<GS>21XYZ")
    assert a.lot == "LOT7" and a.serial == "XYZ"


def test_desktop_standalone_starts_local_server(tmp_path):
    import subprocess
    import sys
    code = ("from tnt import desktop; import urllib.request, json;"
            "u = desktop.start_local_server();"
            "print(json.load(urllib.request.urlopen(u + '/setup/status')))")
    env = {**__import__("os").environ, "XDG_DATA_HOME": str(tmp_path), "TNT_DB": str(tmp_path / "d.db")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert "'needs_setup': True, 'allowed': True" in out.stdout, out.stderr
