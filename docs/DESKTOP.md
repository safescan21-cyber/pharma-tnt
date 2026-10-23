# Desktop Application

Customers download one installer per operating system:

| OS | File | Install |
|---|---|---|
| Windows 10/11 | `PharmaTnT-Setup-<version>.exe` | Double-click and follow the wizard. Adds Start-menu and optional desktop shortcuts |
| macOS 12+ | `PharmaTnT-macOS.dmg` | Open the DMG and drag the app into Applications |
| Linux x64 | `PharmaTnT-linux-x64.tar.gz` | Extract, then run `sudo ./install-linux.sh` |

The installers are built automatically by `.github/workflows/desktop.yml`. Push a tag such as
`git tag v2.1.0 && git push --tags` and the Windows, macOS and Linux installers appear on the
GitHub Release page. Link that page from your website or customer portal.

## How it works
At first start the app asks how this computer should work:

```
                ┌────────────────────────────────────────┐
                │   Central server (cloud or customer    │
                │   data centre) - ONE shared database   │
                └──────────────▲─────────────────────────┘
             HTTPS             │              HTTPS
   ┌───────────────┬───────────┴───────┬────────────────────┐
   │ Plant PC      │ Warehouse PC      │ Pharmacy PC        │  ← desktop app, "Connect" mode
   │ + scanner     │ + scanner         │ + scanner          │
   └───────────────┴───────────────────┴────────────────────┘

   Standalone mode: app + database on one PC (single site, offline, demos)
```

- **Connect (recommended):** the app is a secure window onto the company's central server,
  where all sites share one database. This is what makes end-to-end tracking possible: a pack
  commissioned at the plant can be seen at the warehouse and verified at the pharmacy.
- **Standalone:** the full system, database included, runs inside the app on this PC. Data is
  stored in:
  - Windows: `%APPDATA%\PharmaTnT`
  - macOS: `~/Library/Application Support/PharmaTnT`
  - Linux: `~/.local/share/pharma-tnt`

  On first launch the app asks you to create the administrator account.

To change the connection later, use **Settings → Change connection**, the
"Change connection" Start-menu shortcut on Windows, or run `PharmaTnT --reset`.

Command-line options for IT departments:
`PharmaTnT --mode connect --server https://tnt.company.com`, `--mode standalone`, `--reset`.

## Barcode scanners
Any USB or Bluetooth 2D scanner in **keyboard (HID) mode** works, for example Zebra DS2208,
Honeywell Xenon or Datalogic QuickScan:
1. Configure the scanner to send **Enter** after each scan.
2. Make sure it transmits the GS1 separator (FNC1). Configure it to send ASCII 29, or the
   placeholder `<GS>`. Both are understood.
3. Open **Scan Station**, choose a mode (Verify, Receive, Dispense, Pack) and scan
   continuously. Each scan gets a large green or red result and a beep. You can export the
   session log as CSV.

## How a product is tracked, end to end
| Step | Where | What happens | Recorded |
|---|---|---|---|
| 1. Serialize | Plant | A unique serial is created or imported for each pack. The DataMatrix printed on the pack holds GTIN, expiry, lot and serial | `commissioning` |
| 2. Pack | Packaging line | Packs are scanned into a case (SSCC label), and cases onto a pallet | `packing` |
| 3. Ship | Plant | The pallet is scanned out. Every case and pack inside moves with it. The EPCIS file is sent to the receiver | `shipping` |
| 4. Receive | Distributor | The pallet is scanned in | `receiving` |
| 5. Unpack & re-ship | Distributor | The pallet is broken into cases and cases ship to pharmacies | `unpacking`, `shipping` |
| 6. Verify & dispense | Pharmacy | The pack is scanned. VERIFIED means genuine, in date and not recalled; then it is dispensed | `dispensing` |

At any point, scanning a pack in **Verify & Trace** shows its full history: who handled it,
where and when. A **recall** instantly lists every location and container holding the lot and
blocks dispensing. A counterfeit pack fails at the first scan because its serial was never
commissioned.

## Distribution notes
- **Code signing:** unsigned apps show warnings (Windows SmartScreen, macOS Gatekeeper). For
  commercial distribution, buy a Windows code-signing certificate and an Apple Developer ID,
  then enable the signing steps marked in the workflow.
- **Updates:** publish a new tag. Customers install the new version over the old one, and
  settings and local data are kept.
- **Offline use:** Connect mode needs the network. Sites with unreliable connectivity can use
  Standalone mode and exchange data with the central system as EPCIS files. Automatic
  offline sync is not included.
