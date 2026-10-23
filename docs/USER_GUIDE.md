# User Guide

## Signing in
Use your own account. You'll be asked to change a temporary password at first sign-in.
Passwords need at least 10 characters with letters and digits. After 5 wrong attempts the
account locks, and an administrator must unlock it. Sessions end after 30 minutes of
inactivity.

## Typical flow
```
Create lot (QA) → Release lot (QA, signed) → Serialize → Pack into cases → Pack cases on pallet
→ Ship → (partner) Receive → Unpack → Ship onward → Dispense
```

## Serialization
- **Generate**: choose product, lot, site and quantity. The system creates random serial
  numbers and registers them. Download the `.txt` file and send it to the line printer.
- **Import**: use this when serials come from your line system, serialization platform or a
  CMO. Paste scanned barcodes (one per line) or upload a CSV file:
  ```
  gtin,serial,lot
  00312345678906,A1B2C3D4E5,LOT001
  ```
  A single `code` column with barcode strings also works. If any line is invalid or the serial
  is already registered, nothing is imported and the error names the problem line.

## Packing
1. **Create SSCC labels** for cases or pallets, using your GS1 company prefix.
2. **Pack**: scan the case label into *Parent*, then scan each pack into *Children*.
   Pallets work the same way: scan the pallet label, then the case labels.
3. **Unpack** with the parent label. Leave *Children* empty to unpack everything.

## Shipping & receiving
- **Ship** the outermost container (pallet or loose case). Everything inside moves with it.
  After shipping, click **Download EPCIS for receiver** and send the file to your trading
  partner. This is the DSCSA transaction data.
- **Receive** at the destination by scanning the same containers.
- **Dispense** at the pharmacy or hospital. Recalled, quarantined and expired packs are
  refused.

## Verify & trace
Scan any pack, case or pallet:
- **VERIFIED** means it is genuine and saleable.
- **NOT VERIFIED** lists the reasons: unknown serial (possible counterfeit), recalled,
  expired, already dispensed, or a printed lot/expiry that doesn't match.

**Full trace** shows which case or pallet the item is in and its complete chain of custody.

## Quality & recall (QA)
- **Release / Quarantine / Recall** ask for an electronic signature. You re-enter your password
  and confirm the meaning, for example "Approved recall". The signature is stored permanently
  in the audit trail.
- **Recall** immediately blocks dispensing and shows every location and container holding the
  lot. Download the CSV to notify customers.
- **Import EPCIS** loads data received from a CMO. New lots arrive in *quarantine* until QA
  releases them.
- **Decommission** removes destroyed, stolen, sample or expired packs from circulation, under
  signature.

## Audit trail
Every action is recorded with user, time and details. **Verify hash chain** checks that no
record has been changed or deleted since it was written. You can export the trail as CSV or
EPCIS for inspectors.
