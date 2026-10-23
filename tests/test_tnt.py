from datetime import date, timedelta

import pytest

from tnt import gs1
from tnt.core import NotFound, TnTError, TrackTrace


def key(body):
    return body + gs1.check_digit(body)


GTIN = key("0031234567890")
MFG, DC, PHARM = key("061414100001"), key("061414100002"), key("061414100003")
PREFIX = "0614141"
EXP = date.today() + timedelta(days=365)


@pytest.fixture
def tt(tmp_path):
    t = TrackTrace(str(tmp_path / "t.db"))
    t.add_product(GTIN, "Amoxicillin", "500 mg", "capsule", "Acme Pharma")
    t.add_location(MFG, "Acme Plant", "manufacturer")
    t.add_location(DC, "Central DC", "distributor")
    t.add_location(PHARM, "City Pharmacy", "pharmacy")
    t.add_lot(GTIN, "L001", EXP)
    yield t
    t.close()


def test_gs1_parsing_formats():
    hri = f"(01){GTIN}(17){EXP:%y%m%d}(10)L001(21)ABC123"
    raw = f"]d201{GTIN}17{EXP:%y%m%d}10L001{gs1.GS}21ABC123"
    a, b = gs1.parse(hri), gs1.parse(raw)
    assert a.key == b.key == f"01{GTIN}21ABC123"
    assert a.lot == b.lot == "L001" and a.expiry == EXP
    with pytest.raises(gs1.GS1Error):
        gs1.parse("(01)00312345678900(21)X")  # bad check digit
    sscc = gs1.build_sscc(0, PREFIX, 1)
    assert gs1.valid_key(sscc, 18)


def test_full_supply_chain(tt):
    units = tt.commission(GTIN, "L001", 10, MFG)
    assert len(set(units)) == 10
    case = tt.create_container("case", MFG, PREFIX)
    pallet = tt.create_container("pallet", MFG, PREFIX, extension=1)
    tt.aggregate(case, units[:8])
    tt.aggregate(pallet, [case])

    with pytest.raises(TnTError):
        tt.ship([units[0]], MFG, DC)  # packed unit can't ship alone

    assert tt.ship([pallet], MFG, DC)["items"] == 10  # pallet + case + 8 units
    assert tt.verify(units[0])["status"] == "in_transit"
    tt.receive([pallet], DC)
    tt.disaggregate(pallet)
    tt.ship([case], DC, PHARM)
    tt.receive([case], PHARM)

    assert tt.dispense([units[0]], PHARM) == {"dispensed": 1}
    v = tt.verify(units[0])
    assert not v["verified"] and "status is dispensed" in v["reasons"]

    trace = tt.trace(units[1])
    steps = [h["biz_step"] for h in trace["history"]]
    assert steps == ["commissioning", "packing", "shipping", "receiving", "shipping", "receiving"]
    assert trace["contained_in"][0]["code"] == gs1.parse(case).key
    assert tt.verify_audit_chain()["valid"]


def test_verify_detects_counterfeit_and_label_mismatch(tt):
    unit = tt.commission(GTIN, "L001", 1, MFG)[0]
    assert tt.verify(unit)["verified"]
    assert not tt.verify(f"(01){GTIN}(21)FAKE999")["verified"]
    serial = gs1.parse(unit).serial
    tampered = tt.verify(f"(01){GTIN}(10)L999(21){serial}")
    assert "lot on pack does not match record" in tampered["reasons"]


def test_recall_blocks_dispense_and_locates_stock(tt):
    units = tt.commission(GTIN, "L001", 5, MFG)
    case = tt.create_container("case", MFG, PREFIX)
    tt.aggregate(case, units[:3])
    tt.ship([case], MFG, PHARM)
    tt.receive([case], PHARM)

    report = tt.recall(GTIN, "L001", reason="contamination")
    locs = {(r["location"], r["status"]): r["units"] for r in report["by_location"]}
    assert locs == {(PHARM, "active"): 3, (MFG, "active"): 2}
    assert report["top_level_containers"][0]["code"] == gs1.parse(case).key
    assert "lot is recalled" in tt.verify(units[0])["reasons"]
    with pytest.raises(TnTError, match="recalled"):
        tt.dispense([units[0]], PHARM)


def test_aggregation_rules(tt):
    units = tt.commission(GTIN, "L001", 3, MFG)
    case = tt.create_container("case", MFG, PREFIX)
    case2 = tt.create_container("case", MFG, PREFIX)
    tt.aggregate(case, units[:2])
    with pytest.raises(TnTError, match="already packed"):
        tt.aggregate(case2, [units[0]])
    with pytest.raises(TnTError, match="only contain units"):
        tt.aggregate(case, [case2])
    with pytest.raises(NotFound):
        tt.aggregate(case, [f"(01){GTIN}(21)NOPE"])


def test_failed_transaction_rolls_back(tt):
    units = tt.commission(GTIN, "L001", 2, MFG)
    with pytest.raises(TnTError):
        tt.ship(units, MFG, "9999999999999")  # unknown destination
    assert tt.verify(units[0])["status"] == "active"
    assert tt.verify_audit_chain()["valid"]


def test_audit_chain_detects_tampering(tt):
    tt.commission(GTIN, "L001", 3, MFG)
    tt.db.writer.execute("UPDATE events SET actor='mallory' WHERE id=1")
    assert tt.verify_audit_chain() == {"valid": False, "events_checked": 0, "broken_at_event": 1}
