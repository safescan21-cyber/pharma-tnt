"""Turn customer serial files into (gtin, serial, lot) records for TrackTrace.import_serials."""
from __future__ import annotations

import csv
import io

from . import gs1
from .core import Invalid

_ALIASES = {
    "gtin": {"gtin", "gtin14", "product_gtin", "01"},
    "serial": {"serial", "serial_number", "serialnumber", "sn", "21"},
    "lot": {"lot", "batch", "lot_number", "batch_number", "10"},
    "code": {"code", "barcode", "datamatrix", "scan", "epc", "uri"},
}


def records_from_codes(codes, default_lot: str | None = None):
    """Scanner payloads / HRI strings / EPC URIs, one per item. Lot comes from (10) or default_lot."""
    for i, raw in enumerate(codes, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            if raw.startswith(("urn:", "http://", "https://")):
                code = gs1.parse(gs1.from_uri(raw))
            else:
                code = gs1.parse(raw)
        except gs1.GS1Error as e:
            raise Invalid(f"line {i}: {e}") from e
        if code.is_sscc:
            raise Invalid(f"line {i}: SSCC codes are containers, not serialized units")
        yield code.gtin, code.serial, code.lot or default_lot


def records_from_csv(text: str, default_lot: str | None = None, default_gtin: str | None = None):
    """CSV with a header row. Accepts either gtin/serial/lot columns or a single code column."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), dialect)
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration:
        raise Invalid("empty CSV") from None
    col = {}
    for key, names in _ALIASES.items():
        for idx, h in enumerate(header):
            if h in names:
                col[key] = idx
                break
    if "code" in col and "serial" not in col:
        yield from records_from_codes((row[col["code"]] for row in reader if row), default_lot)
        return
    if "serial" not in col:
        raise Invalid("CSV needs a 'serial' column (plus gtin, lot) or a 'code' column with barcodes")
    for line, row in enumerate(reader, 2):
        if not row or not any(cell.strip() for cell in row):
            continue
        try:
            gtin = row[col["gtin"]] if "gtin" in col else default_gtin
            lot = row[col["lot"]] if "lot" in col else default_lot
            serial = row[col["serial"]]
        except IndexError:
            raise Invalid(f"CSV line {line}: missing columns") from None
        if not gtin:
            raise Invalid(f"CSV line {line}: gtin missing (add a gtin column or choose a product)")
        yield gtin, serial, lot
