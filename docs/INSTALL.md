# Installation & Operations Guide

## 1. Requirements
- A Linux server with Docker and Docker Compose. Two vCPUs and 4 GB RAM handle tens of
  millions of serials. Use SSD storage.
- A DNS name pointing at the server, such as `tnt.customer.com`, with ports 80 and 443 open.
  Caddy obtains the TLS certificate automatically.

## 2. Install
```bash
git clone <your delivery repo> /opt/tnt && cd /opt/tnt
cp .env.example .env        # set TNT_ADMIN_PASSWORD and DOMAIN
docker compose up -d --build
```
Open `https://<DOMAIN>` and sign in as `admin` with the password from `.env`. You must set a
new password at first sign-in. After that, remove `TNT_ADMIN_PASSWORD` from `.env`. It is only
used when the database is empty.

Without Docker, use Python 3.11 or later:
```bash
pip install -r requirements.txt
TNT_DB=/var/lib/tnt/tnt.db python -m tnt create-user admin --role admin --name "Site Admin"
TNT_DB=/var/lib/tnt/tnt.db python -m tnt serve --port 8000   # put a TLS reverse proxy in front
```

## 3. Configuration (environment variables)
| Variable | Default | Purpose |
|---|---|---|
| `TNT_DB` | `tnt.db` (`/data/tnt.db` in Docker) | Database file |
| `TNT_COMPANY_NAME` | Pharma Track & Trace | Name shown in the web app |
| `TNT_IDLE_MINUTES` | 30 | Automatic logoff after inactivity |
| `TNT_SESSION_HOURS` | 12 | Maximum session length |
| `TNT_ADMIN_USER` / `TNT_ADMIN_PASSWORD` | – | Creates the first admin on an empty database |

## 4. First-day setup (in the web app)
1. **Users**: create accounts for each person. Never share accounts, because Part 11 requires
   named users.
2. **Products & Sites**: add each product (GTIN, name, strength, form, NDC) and each site or
   trading partner (GLN).
3. **Quality**: create lots with their expiry dates. QA releases each lot under signature.
4. Run a test pass on a non-production lot: generate or import serials, pack, ship, receive,
   verify.

## 5. Roles
| Role | Can do |
|---|---|
| viewer | Verify, trace, dashboards, reports |
| operator | All viewer actions, plus serialize, import serials, create SSCCs, pack, ship, receive, dispense, export shipment EPCIS |
| qa | All operator actions, plus create lots, release / quarantine / recall (signed), decommission (signed), EPCIS import and export, audit verification and export |
| admin | All qa actions, plus users, products, sites |

## 6. Backup & restore
- Hot backup, safe while running: `docker compose exec tnt python -m tnt backup /data/backup.db`
- `deploy/backup.sh` runs the backup, copies it off the container, checks the audit chain and
  keeps 30 days. Schedule it nightly with cron and copy the backups off-site.
- **Restore**: stop the stack, replace `tnt.db` in the `tnt-data` volume with the backup (and
  delete any `tnt.db-wal` / `tnt.db-shm` files), start the stack, then run
  `python -m tnt verify-audit`.

## 7. Monitoring
- `GET /health` returns `{"status":"ok"}`. The Docker healthcheck uses it.
- Check the audit chain weekly with `python -m tnt verify-audit`. It exits with code 2 if
  tampering is detected.

## 8. Integration API
Every screen uses the REST API, documented interactively at `https://<DOMAIN>/docs`.
To connect a system:
1. Create a dedicated user with the lowest role that works (for example `line1`, operator).
2. `POST /auth/login` returns a token. Send it as `Authorization: Bearer <token>`.
3. Main calls: `POST /serials/import`, `POST /serials/import/csv?location=<GLN>`,
   `POST /aggregate/batch`, `POST /ship`, `GET /epcis/shipments/{id}`, `GET /verify?code=`.

## 9. Scaling
One server handles the serialization volume of most mid-size manufacturers. For multi-site
active-active deployments, the business layer (`tnt/core.py`) can be moved onto PostgreSQL.
