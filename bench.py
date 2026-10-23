"""Throughput benchmark: python bench.py [units]"""
import os
import random
import sys
import tempfile
import time
from datetime import date, timedelta

from tnt import gs1
from tnt.core import TrackTrace


def key(body):
    return body + gs1.check_digit(body)


def timed(label, n, fn):
    t = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t
    print(f"{label:<44} {n:>9,} in {dt:7.3f}s  -> {n / dt:>12,.0f}/s")
    return out


def main(n_units=200_000):
    path = os.path.join(tempfile.mkdtemp(), "bench.db")
    tt = TrackTrace(path)
    gtin, mfg, dc = key("0031234567890"), key("061414100001"), key("061414100002")
    tt.add_product(gtin, "Paracetamol", "500 mg")
    tt.add_location(mfg, "Plant", "manufacturer")
    tt.add_location(dc, "DC", "distributor")
    tt.add_lot(gtin, "B1", date.today() + timedelta(days=700))

    units = timed("commission serialized units", n_units, lambda: tt.commission(gtin, "B1", n_units, mfg))

    per_case, cases_per_pallet = 50, 40
    cases = timed("create SSCC cases", n_units // per_case,
                  lambda: tt.create_containers("case", mfg, "0614141", n_units // per_case))
    timed("aggregate units -> cases (batch upload)", n_units, lambda: tt.aggregate_many(
        {c: units[i * per_case:(i + 1) * per_case] for i, c in enumerate(cases)}))
    timed("aggregate units -> cases (case-by-case)", 100 * per_case, lambda: [
        tt.disaggregate(c) and tt.aggregate(c, units[i * per_case:(i + 1) * per_case]) for i, c in enumerate(cases[:100])])
    pallets = tt.create_containers("pallet", mfg, "0614141", len(cases) // cases_per_pallet, extension=1)
    tt.aggregate_many({p: cases[i * cases_per_pallet:(i + 1) * cases_per_pallet] for i, p in enumerate(pallets)})

    timed("ship pallets (all nested items updated)", n_units, lambda: tt.ship(pallets, mfg, dc))
    timed("receive pallets", n_units, lambda: tt.receive(pallets, dc))

    sample = random.sample(units, 20_000)
    timed("verify (random pack scans)", len(sample), lambda: [tt.verify(c) for c in sample])
    timed("trace (full history + hierarchy)", 5_000, lambda: [tt.trace(c) for c in sample[:5_000]])
    timed("recall + locate whole lot", n_units, lambda: tt.recall(gtin, "B1"))
    timed("audit hash-chain verification (items)", n_units * 4, tt.verify_audit_chain)
    print(f"database size: {os.path.getsize(path) / 1e6:.1f} MB")
    tt.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200_000)
