"""GS1 helpers: check digits, DataMatrix / HRI parsing, SSCC building.

Supported Application Identifiers (the set used on pharma packs):
  (00) SSCC  - 18 digits, logistic unit (case / pallet)
  (01) GTIN  - 14 digits, product
  (17) Expiry YYMMDD
  (10) Batch / lot (variable, <= 20)
  (21) Serial number (variable, <= 20)
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

GS = "\x1d"  # FNC1 group separator in raw DataMatrix payloads
FIXED = {"00": 18, "01": 14, "17": 6}
VARIABLE = {"10": 20, "21": 20}
_HRI = re.compile(r"\((\d{2})\)([^()]+)")
SERIAL_RE = re.compile(r"^[!-~]{1,20}$")  # GS1 AI-82 printable subset (approx.)


class GS1Error(ValueError):
    pass


def check_digit(body: str) -> str:
    """GS1 mod-10 check digit for GTIN/SSCC/GLN bodies."""
    total = 3 * sum(map(int, body[::-2])) + sum(map(int, body[-2::-2]))
    return str(-total % 10)


def valid_key(key: str, length: int) -> bool:
    return len(key) == length and key.isdigit() and check_digit(key[:-1]) == key[-1]


def normalize_gtin(gtin: str) -> str:
    gtin = gtin.strip().zfill(14)
    if not valid_key(gtin, 14):
        raise GS1Error(f"invalid GTIN: {gtin}")
    return gtin


def build_sscc(extension: int, company_prefix: str, serial_ref: int) -> str:
    body_len = 16 - len(company_prefix)
    if not (0 <= extension <= 9) or not company_prefix.isdigit() or body_len < 1:
        raise GS1Error("bad SSCC components")
    if serial_ref >= 10**body_len:
        raise GS1Error("SSCC serial reference space exhausted for this prefix")
    body = f"{extension}{company_prefix}{serial_ref:0{body_len}d}"
    return body + check_digit(body)


def expiry_to_date(yymmdd: str) -> date:
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    year = 2000 + yy  # GS1 sliding century; fine for pharma shelf lives
    if dd == 0:  # "00" day means last day of month
        dd = calendar.monthrange(year, mm)[1]
    return date(year, mm, dd)


@dataclass(frozen=True, slots=True)
class Code:
    ais: dict[str, str]

    @property
    def key(self) -> str:
        """Canonical identity used as the database key."""
        if "00" in self.ais:
            return "00" + self.ais["00"]
        return "01" + self.ais["01"] + "21" + self.ais["21"]

    @property
    def is_sscc(self) -> bool:
        return "00" in self.ais

    gtin = property(lambda self: self.ais.get("01"))
    serial = property(lambda self: self.ais.get("21"))
    lot = property(lambda self: self.ais.get("10"))
    expiry = property(lambda self: expiry_to_date(self.ais["17"]) if "17" in self.ais else None)


def _parse_raw(s: str) -> dict[str, str]:
    ais: dict[str, str] = {}
    i = 0
    while i < len(s):
        if s[i] == GS:
            i += 1
            continue
        ai = s[i : i + 2]
        i += 2
        if ai in FIXED:
            n = FIXED[ai]
            ais[ai] = s[i : i + n]
            if len(ais[ai]) != n:
                raise GS1Error(f"truncated AI ({ai})")
            i += n
        elif ai in VARIABLE:
            end = s.find(GS, i)
            end = len(s) if end == -1 else end
            ais[ai] = s[i:end]
            i = end
        else:
            raise GS1Error(f"unsupported AI ({ai}) at position {i - 2}")
    return ais


def parse(raw: str, validate: bool = True) -> Code:
    """Parse HRI '(01)..(21)..', raw scanner payload (with ]d2 / GS) or canonical key.

    validate=False skips check-digit / format checks, for lookups of codes that
    must already exist in the database (an invalid code simply won't be found).
    """
    # keyboard-wedge scanners often transmit FNC1 as a printable placeholder
    s = raw.strip().replace("<GS>", GS).replace("{GS}", GS).replace("\\x1d", GS)
    if s.startswith("]d2") or s.startswith("]Q3"):
        s = s[3:]
    ais = dict(_HRI.findall(s)) if s.startswith("(") else _parse_raw(s)
    if not validate:
        if "00" not in ais and not ("01" in ais and "21" in ais):
            raise GS1Error("code must contain SSCC (00) or GTIN (01) + serial (21)")
        return Code(ais)

    if "00" in ais:
        if not valid_key(ais["00"], 18):
            raise GS1Error(f"invalid SSCC: {ais['00']}")
    elif "01" in ais and "21" in ais:
        if not valid_key(ais["01"], 14):
            raise GS1Error(f"invalid GTIN: {ais['01']}")
        if not SERIAL_RE.match(ais["21"]):
            raise GS1Error("invalid serial number")
    else:
        raise GS1Error("code must contain SSCC (00) or GTIN (01) + serial (21)")
    if "17" in ais:
        try:
            expiry_to_date(ais["17"])
        except ValueError as e:
            raise GS1Error(f"invalid expiry: {ais['17']}") from e
    return Code(ais)


def hri(gtin: str, serial: str, lot: str | None = None, expiry: date | None = None) -> str:
    """Human-readable string as printed under a pack's DataMatrix."""
    out = f"(01){gtin}"
    if expiry:
        out += f"(17){expiry:%y%m%d}"
    if lot:
        out += f"(10){lot}"
    return out + f"(21){serial}"


# ---------------------------------------------------------------- EPC / Digital Link URIs
def to_uri(key: str) -> str:
    """Canonical key -> GS1 Digital Link URI (accepted as EPC identifier in EPCIS 2.0)."""
    from urllib.parse import quote

    if key.startswith("00"):
        return f"https://id.gs1.org/00/{key[2:]}"
    code = parse(key, validate=False)
    return f"https://id.gs1.org/01/{code.gtin}/21/{quote(code.serial, safe='')}"


def gln_uri(gln: str) -> str:
    return f"https://id.gs1.org/414/{gln}"


def from_uri(uri: str) -> str:
    """EPC URN (sgtin / sscc) or GS1 Digital Link URI -> canonical key."""
    from urllib.parse import unquote, urlparse

    uri = uri.strip()
    if uri.startswith("urn:epc:id:sgtin:"):
        try:
            cp, ir, serial = uri[17:].split(".", 2)
        except ValueError as e:
            raise GS1Error(f"bad SGTIN URN: {uri}") from e
        body = ir[:1] + cp + ir[1:]
        if len(body) != 13 or not body.isdigit():
            raise GS1Error(f"bad SGTIN URN: {uri}")
        return parse(f"01{body}{check_digit(body)}21{unquote(serial)}").key
    if uri.startswith("urn:epc:id:sscc:"):
        try:
            cp, sr = uri[16:].split(".")
        except ValueError as e:
            raise GS1Error(f"bad SSCC URN: {uri}") from e
        body = sr[:1] + cp + sr[1:]
        if len(body) != 17 or not body.isdigit():
            raise GS1Error(f"bad SSCC URN: {uri}")
        return "00" + body + check_digit(body)
    if uri.startswith(("http://", "https://")):
        parts = [unquote(p) for p in urlparse(uri).path.strip("/").split("/")]
        for i, p in enumerate(parts):
            if p in ("00", "01"):
                ais = dict(zip(parts[i::2], parts[i + 1::2]))
                s = f"00{ais['00']}" if p == "00" else f"01{ais.get('01', '')}21{ais.get('21', '')}"
                return parse(s).key
    raise GS1Error(f"unsupported identifier URI: {uri}")


def gln_from_uri(uri: str) -> str | None:
    """sgln URN or Digital Link /414/ URI -> GLN (None if not recognised)."""
    if uri.startswith("urn:epc:id:sgln:"):
        cp, lr, *_ = uri[16:].split(".")
        body = cp + lr
        return body + check_digit(body) if len(body) == 12 and body.isdigit() else None
    if "/414/" in uri:
        gln = uri.split("/414/")[1].split("/")[0]
        return gln if valid_key(gln, 13) else None
    return gln if valid_key(gln := uri.strip(), 13) else None
