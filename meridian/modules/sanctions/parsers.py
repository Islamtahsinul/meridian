"""Parsers for the four sanctions lists. Standard library only.

- OFAC SDN list: CSV (``parse_sdn``, unchanged from the original module).
- UK Sanctions List (FCDO): XML, schema 4.33 (``parse_uk``). Ships are a designation type of their own.
- UN Security Council consolidated list: XML (``parse_un``). No vessel type: a vessel is an entity whose
  comments give a valid ship IMO number and a flag or "the vessel", and whose names are not company names.
- EU consolidated financial sanctions file 1.1: XML (``parse_eu``). Same rule, with the IMO taken from the
  structured ``identification`` element. The file is usually stored gzip-compressed.

``parse_uk``, ``parse_un`` and ``parse_eu`` take a path, bytes or a binary file and return
``{"vessels": [...], "aircraft": [...], "generated": "YYYY-MM-DD" or None, "counts": {...}}``.
Records carry the keys of the OFAC records (see ``parse_sdn``) plus ``list``, ``list_id``,
``programmes``, ``listed_on`` and a few list-specific details.

People rule (CONTRACT 1.2): individuals are counted, never kept. Company IMO numbers (owners and
managers have 7-digit IMO numbers too) are told apart from ship numbers by the IMO check digit and the name.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
import xml.etree.ElementTree as ET
from xml.parsers import expat
from collections import Counter
from pathlib import Path

# -- shared helpers ------------------------------------------------------------------------------
_RE_IMO_TEXT = re.compile(r"\bIMO\b[^0-9]{0,24}?(\d{7})\b", re.I)
_RE_VESSEL_CUE = re.compile(r"\bflag(?:\s+state)?\s*:|\btype of (?:ship|vessel)\b|\bthe vessel\b", re.I)
_RE_FLAG = re.compile(r"\bFlag(?:\s+State)?\s*:\s*([A-Za-z][A-Za-z .,'()-]{1,60}?)\s*(?:[.;]|$)")
_RE_IMO_NAME = re.compile(r"""^\s*IMO\s*:?\s*\d{7}\s*\(\s*["'“‘]?(.+?)["'”’]?\s*\)\s*$""", re.I)
_RE_PROGRAM_SPLIT = re.compile(r"\]\s*\[")
# Company and organisation markers. Vessel names rarely contain them; owners and managers almost always do.
_RE_COMPANY = re.compile(
    r"\b(?:CO|COMPANY|CORP|CORPORATION|INC|LTD|LIMITED|LLC|L\.L\.C|LLP|PLC|JSC|PJSC|OJSC|CJSC|SPC|FZE|FZCO|FZ|"
    r"DMCC|GMBH|S\.A|B\.V|N\.V|PTE|PVT|PRIVATE|HOLDINGS?|GROUP|SHIPPING|SHPG|SHIPMANAGEMENT|MANAGEMENT|"
    r"MARITIME|SERVICES?|TRADING|LOGISTICS|ENTERPRISES?|JV|BANK|INDUSTRY|INDUSTRIES|ORGANI[SZ]ATION|DEPARTMENT|"
    r"NETWORK|AGENCY|MINISTRY|AUTHORITY|FOUNDATION|ASSOCIATION|OOO|OAO|ZAO|PAO|KFT|ZRT|SRL|SOCIETATE|SOCIEDAD|"
    r"ООО|АО|ОАО|ЗАО|ПАО)\b", re.I)
_UK_SHORT = {"Democratic People's Republic of Korea": "DPRK"}

MAX_ALIASES = 20
MAX_LINKED = 10


def norm_reg(text: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def norm_name(text: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def clean(text: str | None) -> str:
    """Collapse whitespace (including non-breaking and narrow spaces)."""
    return " ".join((text or "").split())


def split_programs(text: str | None) -> list[str]:
    """OFAC keeps several programmes in one field as 'SDGT] [NPWMD] [IFSR'."""
    return [p.strip(" []") for p in _RE_PROGRAM_SPLIT.split(text or "") if p.strip(" []")]


def imo_digits(value) -> str | None:
    """'IMO9562233', 'IMO 9562233' or '9562233' -> '9562233'; anything else -> None."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 7 else None


def imo_valid(digits: str | None) -> bool:
    """Ship IMO numbers end in a check digit: the sum of the first six digits weighted 7..2, mod 10.
    Company and registered-owner numbers usually fail it."""
    if not digits or len(digits) != 7 or not digits.isdigit():
        return False
    return sum(int(digits[i]) * (7 - i) for i in range(6)) % 10 == int(digits[6])


def company_like(name: str | None) -> bool:
    return bool(name and _RE_COMPANY.search(name))


def iso_date(text: str | None) -> str | None:
    """'31/07/2024', '2024-07-31' or '2026-11-22T00:00:00.942Z' -> '2024-07-31'."""
    s = (text or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def _unique(values, limit: int | None = None, key=lambda s: s.lower()) -> list[str]:
    out, seen = [], set()
    for v in values:
        v = clean(v)
        k = key(v) if v else ""
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(v)
        if limit and len(out) >= limit:
            break
    return out


def _vessel_name(text: str | None) -> str:
    """Names such as 'IMO 9123453 ("NORTHWIND")' become 'NORTHWIND'."""
    s = clean(text)
    m = _RE_IMO_NAME.match(s)
    return clean(m.group(1)) if m else s


def _aliases(primary: str, names) -> list[str]:
    seen = {norm_name(primary)}
    out = []
    for n in names:
        k = norm_name(n)
        if k and k not in seen:
            seen.add(k)
            out.append(clean(n))
            if len(out) >= MAX_ALIASES:
                break
    return out


def _vessel_record(list_id: str, native_id: str, **fields) -> dict:
    """A vessel record with every key an OFAC vessel record has (strings empty rather than missing)."""
    rec = {"uid": f"{list_id.lower()}-{native_id}", "list": list_id, "list_id": native_id, "kind": "sanctioned_vessel",
           "name": "", "program": "", "programmes": [], "linked_to": [], "aliases": [], "built": None, "remarks": "",
           "call_sign": "", "vessel_type": "", "tonnage": "", "grt": "", "flag": "", "owner": "", "imo": None,
           "mmsi": None, "listed_on": None}
    rec.update({k: v for k, v in fields.items() if v is not None})
    if not rec["program"] and rec["programmes"]:
        rec["program"] = "] [".join(rec["programmes"])
    return rec


class UnsafeXML(ValueError):
    pass


def _open_binary(source):
    """A binary stream for ``source`` (path, bytes or binary file), gunzipped when it starts with the gzip magic."""
    if isinstance(source, (bytes, bytearray)):
        fh = io.BytesIO(bytes(source))
    elif isinstance(source, (str, Path)):
        fh = open(source, "rb")
    else:
        fh = source
    head = fh.read(2)
    fh.seek(0)
    if head == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=fh, mode="rb")
    return fh


def _refuse_dtd(*_args):
    raise UnsafeXML("the file declares a DOCTYPE or entities, which a sanctions export never does")


def _xml_events(source, events=("start", "end")):
    """iterparse-like events over ``source``, refusing documents with a DOCTYPE (no entity expansion of any kind).

    The refusal lives in the expat parser itself (doctype, entity and DTD-only handlers raise ``UnsafeXML``), so it
    holds for any encoding (UTF-16 included) and any prolog length. The raw-byte scan of the head is only a fast
    first check. Elements and events match ``ET.iterparse``: tags are ``{namespace}local``, and an "end" element
    holds its full subtree.
    """
    fh = _open_binary(source)
    try:
        head = fh.read(65536)
        if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
            _refuse_dtd()
        fh.seek(0)
        want_start, want_end = "start" in events, "end" in events
        builder = ET.TreeBuilder()
        pending: list = []

        def qname(name: str) -> str:
            return "{" + name if "}" in name else name

        def on_start(name, attrs):
            el = builder.start(qname(name), {qname(k): v for k, v in attrs.items()})
            if want_start:
                pending.append(("start", el))

        def on_end(name):
            el = builder.end(qname(name))
            if want_end:
                pending.append(("end", el))

        parser = expat.ParserCreate(namespace_separator="}")
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.StartDoctypeDeclHandler = _refuse_dtd
        parser.EntityDeclHandler = _refuse_dtd
        parser.UnparsedEntityDeclHandler = _refuse_dtd
        parser.NotationDeclHandler = _refuse_dtd
        parser.ExternalEntityRefHandler = _refuse_dtd
        parser.StartElementHandler = on_start
        parser.EndElementHandler = on_end
        parser.CharacterDataHandler = builder.data
        parser.buffer_text = True
        while True:
            chunk = fh.read(65536)
            try:
                parser.Parse(chunk, not chunk)
            except expat.ExpatError as exc:
                err = ET.ParseError(str(exc))
                err.code, err.position = exc.code, (exc.lineno, exc.offset)
                raise err from None
            yield from pending
            pending.clear()
            if not chunk:
                break
    finally:
        fh.close()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# -- OFAC SDN (CSV) ------------------------------------------------------------------------------
_RE_IMO = re.compile(r"\bIMO\s+(\d{7})\b")
_RE_MMSI = re.compile(r"\bMMSI\s+(\d{9})\b")
_RE_MODE_S = re.compile(r"Mode S Transponder Code\s+([0-9A-Fa-f]{6})\b")
_RE_MSN = re.compile(r"Serial Number \(MSN\)\s+([A-Za-z0-9-]+)")
_RE_TAIL = re.compile(r"Tail Number\s+([A-Za-z0-9-]+)")
_RE_MODEL = re.compile(r"Aircraft Model\s+([^;]+)")
_RE_OPERATOR = re.compile(r"Aircraft Operator\s+([^;]+)")
_RE_BUILT = re.compile(r"(?:Manufacture Date|Year of Build)\s+(\d{4})")
_RE_LINKED = re.compile(r"Linked To:\s*([^;]+?)\.?(?:;|$)")
_RE_ALIAS = re.compile(r"(?:f\.k\.a\.|a\.k\.a\.)\s+'([^']+)'")


def _clean_sdn(value: str) -> str:
    value = (value or "").strip()
    return "" if value in ("-0-", "-0- ") else value


def parse_sdn(text: str) -> tuple[list[dict], list[dict]]:
    vessels: list[dict] = []
    aircraft: list[dict] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 12:
            continue
        kind = row[2].strip().lower()
        if kind not in ("vessel", "aircraft"):
            continue
        remarks = _clean_sdn(row[11])
        base = {"uid": _clean_sdn(row[0]), "name": _clean_sdn(row[1]), "program": _clean_sdn(row[3]),
                "linked_to": sorted(set(m.strip() for m in _RE_LINKED.findall(remarks)))[:10],
                "aliases": sorted(set(_RE_ALIAS.findall(remarks)))[:10],
                "built": (_RE_BUILT.search(remarks).group(1) if _RE_BUILT.search(remarks) else None),
                "remarks": remarks[:1500]}
        if kind == "vessel":
            imo = _RE_IMO.search(remarks)
            mmsi = _RE_MMSI.search(remarks)
            base.update({"kind": "sanctioned_vessel", "call_sign": _clean_sdn(row[5]), "vessel_type": _clean_sdn(row[6]),
                         "tonnage": _clean_sdn(row[7]), "grt": _clean_sdn(row[8]), "flag": _clean_sdn(row[9]),
                         "owner": _clean_sdn(row[10]), "imo": imo.group(1) if imo else None,
                         "mmsi": mmsi.group(1) if mmsi else None})
            vessels.append(base)
        else:
            mode_s = _RE_MODE_S.search(remarks)
            msn = _RE_MSN.search(remarks)
            tail = _RE_TAIL.search(remarks)
            model = _RE_MODEL.search(remarks)
            operator = _RE_OPERATOR.search(remarks)
            reg = tail.group(1) if tail else base["name"]
            base.update({"kind": "sanctioned_aircraft", "registration": reg,
                         "mode_s": mode_s.group(1).lower() if mode_s else None,
                         "msn": msn.group(1) if msn else None,
                         "model": model.group(1).strip() if model else None,
                         "operator": operator.group(1).strip() if operator else None})
            aircraft.append(base)
    return vessels, aircraft


# -- UK Sanctions List (XML) -----------------------------------------------------------------------
def uk_regime(name: str | None) -> str:
    """'The Russia (Sanctions) (EU Exit) Regulations 2019' -> 'Russia'."""
    s = clean(name)
    s = re.sub(r"^The\s+", "", s)
    s = re.sub(r"\s+Regulations(?:\s+\d{4})?$", "", s)
    s = s.replace("(EU Exit)", "").replace("(Sanctions)", "")
    s = clean(re.sub(r"\s+Sanctions$", "", clean(s)))
    return _UK_SHORT.get(s, s)


def _uk_measures(text: str | None) -> list[str]:
    out = []
    for part in (text or "").split("|"):
        part = re.sub(r"""\s*:?\s*\(?\s*see\s+["“]?other information["”]?\s*\)?\s*$""", "", clean(part),
                      flags=re.I)
        if part:
            out.append(part)
    return _unique(out)


def _uk_ship(el) -> dict | None:
    native = clean(el.findtext("UniqueID"))
    if not native:
        return None
    primary, others = None, []
    for n in el.iter("Name"):
        full = _vessel_name(" ".join(clean(n.findtext(f"Name{i}")) for i in range(1, 7)))
        if not full:
            continue
        if primary is None and clean(n.findtext("NameType")).lower().startswith("primary"):
            primary = full
        else:
            others.append(full)
    imos = _unique((imo_digits(i.text) or "" for i in el.iter("IMONumber")), key=lambda s: s)
    if primary is None:
        primary = others.pop(0) if others else (f"IMO {imos[0]}" if imos else native)

    def texts(tag):
        return _unique(x.text for x in el.iter(tag))

    owners, flags = texts("CurrentOwnerOperator"), texts("CurrentBelievedFlagOfShip")
    types, built = texts("TypeOfShip"), [b for b in texts("YearBuilt") if re.fullmatch(r"\d{4}", b)]
    tonnage, length = texts("TonnageOfShip"), texts("LengthOfShip")
    regime = uk_regime(el.findtext("RegimeName"))
    other = clean(el.findtext("OtherInformation"))
    un_ref = clean(el.findtext("UNReferenceNumber"))
    if not un_ref:
        m = re.search(r"UN Ref(?:erence Number)?\s*:?\s*(?:UN Ref\s*)?([A-Z]{2}[a-z]\.\d{3})", other)
        un_ref = m.group(1) if m else ""
    return _vessel_record(
        "UK", native, name=primary, aliases=_aliases(primary, others), programmes=[regime] if regime else [],
        listed_on=iso_date(el.findtext("DateDesignated")), updated_on=iso_date(el.findtext("LastUpdated")),
        measures=_uk_measures(el.findtext("SanctionsImposed")),
        designation_source=clean(el.findtext("DesignationSource")) or None, un_ref=un_ref or None,
        linked_to=owners[:MAX_LINKED], owner="; ".join(owners)[:300], previous_owners=texts("PreviousOwnerOperator")[:MAX_LINKED],
        imo=imos[0] if imos else None, imos=imos, flag=flags[0] if flags else "", previous_flags=texts("PreviousFlag")[:10],
        vessel_type=types[0] if types else "", tonnage=tonnage[0] if tonnage else "",
        length_m=length[0] if length else None, built=built[0] if built else None,
        hull_id=(texts("HullIdentificationNumber") or [None])[0], remarks=other[:600])


def parse_uk(source) -> dict:
    vessels: list[dict] = []
    counts: Counter = Counter()
    generated = None
    for event, el in _xml_events(source, events=("end",)):
        tag = el.tag
        if tag == "DateGenerated":
            generated = iso_date(el.text)
        elif tag == "Designation":
            counts["designations"] += 1
            kind = clean(el.findtext("IndividualEntityShip")).lower()
            if kind == "ship":
                counts["ships"] += 1
                rec = _uk_ship(el)
                if rec:
                    vessels.append(rec)
            elif kind == "individual":
                counts["individuals"] += 1
            else:
                counts["entities"] += 1
            el.clear()
    counts["vessels"] = len(vessels)
    counts["aircraft"] = 0
    return {"vessels": vessels, "aircraft": [], "generated": generated, "counts": dict(counts)}


# -- UN Security Council consolidated list (XML) -----------------------------------------------------
def _un_vessel(el) -> dict | None:
    name = _vessel_name(el.findtext("FIRST_NAME"))
    aliases = _unique(a.findtext("ALIAS_NAME") for a in el.iter("ENTITY_ALIAS"))
    comments = el.findtext("COMMENTS1") or ""
    imos = _unique((d for d in _RE_IMO_TEXT.findall(comments) if imo_valid(d)), key=lambda s: s)
    if not name or not imos or not _RE_VESSEL_CUE.search(comments):
        return None
    if company_like(name) or any(company_like(a) for a in aliases):
        return None
    flag = _RE_FLAG.search(clean(comments))
    native = clean(el.findtext("DATAID")) or clean(el.findtext("REFERENCE_NUMBER"))
    if not native:
        return None
    programme = clean(el.findtext("UN_LIST_TYPE"))
    return _vessel_record(
        "UN", native, name=name, aliases=_aliases(name, aliases), programmes=[programme] if programme else ["UN"],
        listed_on=iso_date(el.findtext("LISTED_ON")), reference=clean(el.findtext("REFERENCE_NUMBER")) or None,
        imo=imos[0], imos=imos, flag=clean(flag.group(1)) if flag else "", remarks=clean(comments)[:600])


def parse_un(source) -> dict:
    vessels: list[dict] = []
    counts: Counter = Counter()
    generated = None
    for event, el in _xml_events(source):
        tag = _local(el.tag)
        if event == "start":
            if tag == "CONSOLIDATED_LIST":
                generated = iso_date(el.get("dateGenerated"))
            continue
        if tag == "INDIVIDUAL":
            counts["designations"] += 1
            counts["individuals"] += 1
            el.clear()
        elif tag == "ENTITY":
            counts["designations"] += 1
            counts["entities"] += 1
            rec = _un_vessel(el)
            if rec:
                vessels.append(rec)
            el.clear()
    counts["vessels"] = len(vessels)
    counts["aircraft"] = 0
    return {"vessels": vessels, "aircraft": [], "generated": generated, "counts": dict(counts)}


# -- EU consolidated financial sanctions file 1.1 (XML) -----------------------------------------------
_EU_FIELDS = {"type of ship": "vessel_type", "type of vessel": "vessel_type", "owner": "owner", "flag": "flag",
              "flag state": "flag", "call sign": "call_sign", "mmsi": "mmsi"}


def _eu_vessel(el, ns: str) -> dict | None:
    names = _unique(n.get("wholeName") for n in el.iter(ns + "nameAlias"))
    imos = _unique((d for d in (imo_digits(i.get("number")) for i in el.iter(ns + "identification")
                                if (i.get("identificationTypeCode") or "").lower() == "imo") if imo_valid(d)),
                   key=lambda s: s)
    if not names or not imos or any(company_like(n) for n in names):
        return None
    native = clean(el.get("logicalId"))
    if not native:
        return None
    fields: dict[str, str] = {}
    remarks = []
    for r in el.iter(ns + "remark"):
        text = (r.text or "").strip()
        if not text:
            continue
        remarks.append(clean(text))
        for line in text.splitlines():
            m = re.match(r"\s*([A-Za-z ]{3,20}?)\s*:\s*(.+)", line)
            if m and m.group(1).strip().lower() in _EU_FIELDS:
                fields.setdefault(_EU_FIELDS[m.group(1).strip().lower()], clean(m.group(2)))
    regs = list(el.iter(ns + "regulation"))
    programmes = _unique((r.get("programme") for r in regs), key=lambda s: s.upper()) or ["EU"]
    dates = sorted(d for d in (iso_date(r.get("publicationDate")) for r in regs) if d)
    mmsi = re.sub(r"\D", "", fields.get("mmsi", ""))
    owner = fields.get("owner", "")
    return _vessel_record(
        "EU", native, name=names[0], aliases=_aliases(names[0], names[1:]), programmes=programmes,
        listed_on=iso_date(el.get("designationDate")) or (dates[0] if dates else None),
        reference=clean(el.get("euReferenceNumber")) or None, un_ref=clean(el.get("unitedNationId")) or None,
        imo=imos[0], imos=imos, mmsi=mmsi if len(mmsi) == 9 else None, flag=fields.get("flag", ""),
        call_sign=fields.get("call_sign", ""), vessel_type=fields.get("vessel_type", ""), owner=owner,
        linked_to=[owner] if owner else [], remarks=" ".join(remarks)[:600])


def parse_eu(source) -> dict:
    vessels: list[dict] = []
    counts: Counter = Counter()
    generated = None
    ns = ""
    for event, el in _xml_events(source):
        tag = _local(el.tag)
        if event == "start":
            if tag == "export":
                ns = el.tag[: el.tag.index("}") + 1] if el.tag.startswith("{") else ""
                generated = iso_date(el.get("generationDate"))
            continue
        if tag == "sanctionEntity":
            counts["designations"] += 1
            st = el.find(ns + "subjectType")
            code = (st.get("code") if st is not None else "") or ""
            if code.lower() == "person":
                counts["individuals"] += 1
            else:
                counts["entities"] += 1
                rec = _eu_vessel(el, ns)
                if rec:
                    vessels.append(rec)
            el.clear()
    counts["vessels"] = len(vessels)
    counts["aircraft"] = 0
    return {"vessels": vessels, "aircraft": [], "generated": generated, "counts": dict(counts)}
