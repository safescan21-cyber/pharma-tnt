"""Administration CLI.

  python -m tnt create-user USERNAME --role admin --name "Jane Doe"
  python -m tnt backup /backups/tnt-2026-10-23.db
  python -m tnt verify-audit
  python -m tnt demo            # seed demonstration data (empty database only)
  python -m tnt serve --port 8000
"""
from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from datetime import date, timedelta

from . import gs1


def _tt():
    from .core import TrackTrace
    return TrackTrace(os.environ.get("TNT_DB", "tnt.db"))


def create_user(a):
    from .auth import Auth
    pw = os.environ.get("TNT_NEW_PASSWORD") or getpass.getpass(f"Temporary password for {a.username}: ")
    tt = _tt()
    try:
        u = Auth(tt).create_user(a.username, a.name or a.username, pw, a.role, actor="cli")
        print(f"created {u['username']} ({u['role']}) - password change required at first login")
    finally:
        tt.close()


def backup(a):
    """Online, consistent backup via SQLite's backup API (safe while the server runs)."""
    src = sqlite3.connect(os.environ.get("TNT_DB", "tnt.db"))
    dst = sqlite3.connect(a.dest)
    with dst:
        src.backup(dst)
    ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    dst.close()
    src.close()
    print(f"backup written to {a.dest} (integrity: {ok})")


def verify_audit(_):
    tt = _tt()
    r = tt.verify_audit_chain()
    tt.close()
    print(r)
    sys.exit(0 if r["valid"] else 2)


def demo(_):
    from .auth import Auth
    tt = _tt()
    if tt.stats()["products"]:
        sys.exit("database is not empty - demo data is only seeded into a new database")
    key = lambda body: body + gs1.check_digit(body)  # noqa: E731
    plant, dc, pharmacy = key("061414100001"), key("061414100002"), key("061414100003")
    tt.add_location(plant, "Acme Pharma - Plant 1", "manufacturer", "1 Industrial Way")
    tt.add_location(dc, "MedLogix Distribution Center", "distributor", "200 Logistics Park")
    tt.add_location(pharmacy, "City Care Pharmacy", "pharmacy", "12 Main Street")
    products = [("0031234567890", "Amoxicillin", "500 mg", "capsule", "61234-001-30"),
                ("0031234567891", "Atorvastatin", "20 mg", "tablet", "61234-002-90"),
                ("0031234567892", "Insulin glargine", "100 U/mL", "solution for injection", "61234-003-05")]
    for body, name, strength, form, ndc in products:
        gtin = key(body)
        tt.add_product(gtin, name, strength, form, "Acme Pharma", ndc)
        for n in (1, 2):
            tt.add_lot(gtin, f"{name[:3].upper()}{n:03d}", date.today() + timedelta(days=365 * n))
    gtin = key(products[0][0])
    units = tt.commission(gtin, "AMO001", 400, plant, actor="demo")
    cases = tt.create_containers("case", plant, "0614141", 8, actor="demo")
    tt.aggregate_many({c: units[i * 50:(i + 1) * 50] for i, c in enumerate(cases)}, actor="demo")
    pallet = tt.create_container("pallet", plant, "0614141", extension=1, actor="demo")
    tt.aggregate(pallet, cases, actor="demo")
    tt.ship([pallet], plant, dc, actor="demo", reference="PO-10001")
    tt.receive([pallet], dc, actor="demo")
    tt.disaggregate(pallet, actor="demo")
    tt.ship(cases[:2], dc, pharmacy, actor="demo", reference="PO-20001")
    tt.receive(cases[:2], pharmacy, actor="demo")
    tt.dispense(units[:5], pharmacy, actor="demo")
    auth = Auth(tt)
    for user, name, role in [("admin", "Demo Administrator", "admin"), ("qa", "Quinn Quality", "qa"),
                             ("operator", "Olivia Operator", "operator"), ("viewer", "Victor Viewer", "viewer")]:
        auth.create_user(user, name, "Demo-pass-2026", role, actor="demo", must_change=False)
    tt.close()
    print("demo data created. Users: admin / qa / operator / viewer   password: Demo-pass-2026")
    print("sample pack to verify:", units[10])


def serve(a):
    import uvicorn
    uvicorn.run("tnt.api:app", host=a.host, port=a.port, workers=1, proxy_headers=True)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m tnt", description="Pharma Track & Trace administration")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user", help="create a user (password via prompt or TNT_NEW_PASSWORD)")
    c.add_argument("username")
    c.add_argument("--role", choices=["viewer", "operator", "qa", "admin"], default="admin")
    c.add_argument("--name")
    c.set_defaults(fn=create_user)
    b = sub.add_parser("backup", help="hot backup of the database")
    b.add_argument("dest")
    b.set_defaults(fn=backup)
    sub.add_parser("verify-audit", help="verify the audit hash chain").set_defaults(fn=verify_audit)
    sub.add_parser("demo", help="seed demo data into an empty database").set_defaults(fn=demo)
    s = sub.add_parser("serve", help="run the web server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=serve)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
