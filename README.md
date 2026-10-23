# Pharma Track & Trace

Serialization, aggregation and supply-chain traceability for pharmaceutical manufacturers,
CMOs, distributors and dispensers. It is built on GS1 standards (GTIN, SSCC, GLN, DataMatrix,
EPCIS 2.0) and designed to support DSCSA (US), EU FMD and 21 CFR Part 11.

## What customers get
| Module | Capabilities |
|---|---|
| **Serialization** | Generates random, non-sequential serial numbers, or imports serials from line systems, L4 platforms or CMOs (JSON, CSV, raw scanner data, EPC URIs). Files are all-or-nothing and duplicates are rejected |
| **Packing** | SSCC case and pallet labels, unit→case→pallet aggregation, unpacking, batch uploads from the packaging line |
| **Chain of custody** | Ship and receive whole hierarchies, dispense to patient, decommission (destroyed, stolen, sample, expired) |
| **Verification** | One scan shows whether a pack is genuine and saleable: unknown serial (suspect), recalled or quarantined lot, expired, already dispensed, or a printed lot/expiry that doesn't match the record |
| **Quality & recall** | Lot release, quarantine and recall under electronic signature. Shows where every unit of a lot is, and which cases and pallets hold it |
| **EPCIS 2.0** | Exports a shipment's history plus product master data for the receiving partner. Imports commissioning and aggregation events from CMOs; new lots arrive in quarantine |
| **Compliance** | Named users with four roles, password policy, lockout after 5 failed attempts, idle auto-logoff, electronic signatures (signer, meaning, time), and a SHA-256 hash-chained audit trail that detects tampering |
| **Reports** | CSV exports of a lot's serials, recall whereabouts and the audit trail |
| **Web app** | Login, dashboard, and a screen for every workflow. Works on phones and handheld scanners |
| **Desktop app** | Installers for Windows, macOS and Linux. Connects to the central server or runs standalone. Includes a Scan Station for USB barcode scanners ([docs/DESKTOP.md](docs/DESKTOP.md)) |

## Quick start (demo)
```bash
pip install -r requirements.txt
python -m tnt demo                 # seeds products, sites, lots, a shipped pallet and 4 users
python -m tnt serve --port 8000    # open http://localhost:8000  (API docs: /docs)
```
Demo logins: `admin`, `qa`, `operator`, `viewer`. All use the password `Demo-pass-2026`.

## Production
For a Docker + TLS install, backups and the first admin account, see
**[docs/INSTALL.md](docs/INSTALL.md)**. Day-to-day use is in **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)**.
The Part 11 / DSCSA / EU FMD mapping and what the customer must validate are in
**[docs/COMPLIANCE.md](docs/COMPLIANCE.md)**.

## Performance
Measured with `python bench.py` (200k packs, one process):

| Operation | Throughput |
|---|---|
| Commission serials | ~50,000 units/s |
| Aggregate (batch upload) | ~43,000 units/s |
| Ship / receive a pallet hierarchy | 100,000+ nested items/s |
| Verify a scanned pack | ~25,000 scans/s |
| Recall and locate a 200k-unit lot | ~0.5 s |
| Audit chain verification | ~1M item links/s |

The storage engine is SQLite in WAL mode with memory-mapped reads, set-based SQL and recursive
CTEs. All writes go through one connection, which keeps the hash chain strictly ordered, and
reads run concurrently on one connection per thread.

## Development
```bash
pip install -r requirements-dev.txt
pytest -q
```

## Layout
```
tnt/gs1.py         GS1 check digits, DataMatrix/HRI parsing, SSCC, EPC URN & Digital Link URIs
tnt/db.py          storage engine and schema
tnt/core.py        business rules (TrackTrace class) - usable without the API
tnt/auth.py        users, roles, sessions, electronic signatures
tnt/importers.py   CSV / scan parsing for serial import
tnt/epcis.py       EPCIS 2.0 export / import
tnt/api.py         REST API (FastAPI)
tnt/static/        web application
tnt/__main__.py    admin CLI: create-user, backup, verify-audit, demo, serve
tnt/desktop.py     desktop launcher (connect / standalone, native window)
packaging/         PyInstaller spec, Windows installer, Linux package
.github/workflows/desktop.yml   builds the Windows / macOS / Linux installers
```

## Desktop app (local build)
```bash
pip install -r requirements-desktop.txt
pyinstaller packaging/PharmaTnT.spec --noconfirm     # -> dist/PharmaTnT/
```
