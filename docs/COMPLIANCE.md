# Regulatory Compliance Mapping

> The software provides technical controls that *support* compliance. Compliance itself also
> depends on the customer's procedures, validation and use. Each customer must validate the
> system for its intended use (GAMP 5 category 4: IQ/OQ/PQ) before GxP use.

## 21 CFR Part 11 / EU GMP Annex 11
| Requirement | How the system supports it |
|---|---|
| §11.10(a) Validation | Automated test suite (`pytest`) usable as OQ evidence. The customer runs IQ/OQ/PQ |
| §11.10(b) Accurate copies | CSV and EPCIS exports of records and the audit trail |
| §11.10(c) Record protection | Records are never deleted. Users are deactivated, not deleted. Backup tool with integrity check |
| §11.10(d) Limited access | Named accounts, four roles, lockout after 5 failures, idle logoff, maximum session length |
| §11.10(e) Audit trail | Every create/modify action, login, failed login and admin change is recorded with user and UTC time. A SHA-256 hash chain makes edits, insertions and deletions detectable (`/audit/verify`) |
| §11.10(f) Operational checks | Enforced sequence: lots must exist before serialization, a pack can't ship while packed, recalled or expired packs can't be dispensed, and so on |
| §11.10(g) Authority checks | Role checks on every API endpoint |
| §11.10(h) Device checks | GS1 check-digit and format validation on every scanned code |
| §11.50 Signature manifestation | Signatures record signer username, full name, meaning and timestamp, and display in the audit trail |
| §11.70 Signature linking | The signature is part of the hashed event record and can't be separated from it |
| §11.100/§11.300 Signature controls | Unique usernames, password policy, re-authentication for each signature, failed signatures count toward lockout |

**Customer responsibilities:** SOPs, training, account management, periodic audit-trail
review, backups, change control, and the validation package.

## DSCSA (US)
| Requirement | Support |
|---|---|
| Product identifier (GTIN, serial, lot, expiry) | DataMatrix element strings, randomized serials |
| Aggregation | Unit / case / pallet hierarchy |
| Interoperable exchange (EPCIS) | EPCIS 2.0 shipment export with product master data, and EPCIS import |
| Verification and suspect product | `/verify` flags unknown serials, mismatches and illegitimate statuses |
| Recall and tracing | Lot whereabouts and full chain of custody per pack |
| Not included | Connections to a VRS network (verification router) and to partner EPCIS repositories. These need integration through the API |

## EU FMD / Delegated Regulation 2016/161
| Requirement | Support |
|---|---|
| Unique identifier and randomized serials | Yes |
| Decommissioning (dispensed, destroyed, stolen, sample, expired) | Yes |
| Verification at point of dispense | Yes, at the system level |
| Not included | Upload to the EU Hub / national NMVS. That needs an EMVO gateway integration |

## Security controls
- Passwords are hashed with scrypt. Session tokens are random, stored only as SHA-256 hashes,
  and revoked on logout, deactivation or role change.
- Security headers: CSP, X-Frame-Options DENY, nosniff, no-referrer. HSTS is set at the proxy.
- CSV exports neutralise spreadsheet formula injection.
- TLS is provided by the bundled Caddy reverse proxy.
