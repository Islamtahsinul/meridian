"""Merge per-list sanctions records into one record per vessel or aircraft.

Rules (docs/modules/sanctions.md):
- Lists are taken in the order OFAC, UK, UN, EU. The first list that names a vessel gives the merged
  record its ``uid`` and its fields; later lists only fill fields that are still empty.
- Vessels merge on a shared IMO number, or failing that on a shared MMSI when exactly one record
  matches and the IMO numbers do not disagree. Aircraft merge on a shared Mode S code or registration,
  with the same ambiguity and conflict checks (serial numbers repeat across types, so they never merge).
- Records from the same list never merge with each other (a list's own duplicates stay apart).
- ``program`` stays one string in the OFAC style ("A] [B"). The OFAC part is unchanged; the other
  lists follow with their name in front ("RUSSIA-EO14024] [UK: Russia").
"""

from __future__ import annotations

from .parsers import MAX_ALIASES, norm_name, norm_reg, split_programs

LIST_ORDER = ("OFAC", "UK", "UN", "EU")

VESSEL_KEYS = ("uid", "name", "program", "linked_to", "aliases", "built", "remarks", "kind", "call_sign",
               "vessel_type", "tonnage", "grt", "flag", "owner", "imo", "mmsi")
AIRCRAFT_KEYS = ("uid", "name", "program", "linked_to", "aliases", "built", "remarks", "kind", "registration",
                 "mode_s", "msn", "model", "operator")
VESSEL_FILL = ("call_sign", "vessel_type", "tonnage", "grt", "flag", "owner", "imo", "mmsi", "built")
AIRCRAFT_FILL = ("registration", "mode_s", "msn", "model", "operator", "built")
LISTING_EXTRA = ("reference", "updated_on", "measures", "designation_source", "un_ref", "imo", "mmsi", "flag",
                 "previous_flags", "vessel_type", "owner", "previous_owners", "length_m", "registration", "mode_s",
                 "msn")
MAX_LINKED_MERGED = 15


def _blank(value) -> bool:
    return value is None or value == "" or value == []


def vessel_imos(rec: dict) -> list[str]:
    out = [i for i in (rec.get("imos") or []) if i]
    if rec.get("imo") and rec["imo"] not in out:
        out.insert(0, rec["imo"])
    return out


def _vessel_keys(rec: dict) -> tuple[list[str], list[str]]:
    return vessel_imos(rec), ([str(rec["mmsi"])] if rec.get("mmsi") else [])


def _aircraft_keys(rec: dict) -> tuple[list[str], list[str]]:
    hexes = [str(rec["mode_s"]).lower()] if rec.get("mode_s") else []
    reg = norm_reg(rec.get("registration"))
    return hexes, ([reg] if reg else [])


def merge_records(per_list: dict[str, list[dict]], kind: str) -> list[dict]:
    """Group ``{list_id: [records]}`` and return merged records, OFAC first, then in list and file order."""
    keys_of = _vessel_keys if kind == "vessel" else _aircraft_keys
    groups: list[list[dict]] = []
    strong: dict[str, list[int]] = {}     # IMO (vessels) or Mode S (aircraft) -> groups
    weak: dict[str, list[int]] = {}       # MMSI (vessels) or registration (aircraft) -> groups
    group_lists: list[set[str]] = []
    group_strong: list[set[str]] = []
    for lid in [x for x in LIST_ORDER if x in per_list] + [x for x in per_list if x not in LIST_ORDER]:
        for rec in per_list.get(lid) or []:
            s_keys, w_keys = keys_of(rec)
            target = None
            cands = {g for k in s_keys for g in strong.get(k, []) if lid not in group_lists[g]}
            if len(cands) == 1:
                target = cands.pop()
            elif not cands:
                cands = {g for k in w_keys for g in weak.get(k, []) if lid not in group_lists[g]
                         and not (s_keys and group_strong[g] and not (set(s_keys) & group_strong[g]))}
                if len(cands) == 1:
                    target = cands.pop()
            if target is None:
                groups.append([])
                group_lists.append(set())
                group_strong.append(set())
                target = len(groups) - 1
            groups[target].append(rec)
            group_lists[target].add(lid)
            group_strong[target].update(s_keys)
            for k in s_keys:
                if target not in strong.setdefault(k, []):
                    strong[k].append(target)
            for k in w_keys:
                if target not in weak.setdefault(k, []):
                    weak[k].append(target)
    return [combine(g, kind) for g in groups]


def compat_program(records: list[dict]) -> str:
    """One OFAC-style programme string for every list (the OFAC part kept exactly as published)."""
    parts = []
    for r in records:
        lid = r.get("list") or "OFAC"
        if lid == "OFAC":
            if r.get("program"):
                parts.append(r["program"])
            continue
        progs = r.get("programmes") or split_programs(r.get("program")) or ["listed"]
        parts.extend(f"{lid}: {p}" for p in progs)
    return "] [".join(parts)


def listing(rec: dict) -> dict:
    out = {"list": rec.get("list") or "OFAC", "id": rec.get("list_id") or rec.get("uid"), "uid": rec.get("uid"),
           "name": rec.get("name"),
           "programmes": list(rec.get("programmes") or split_programs(rec.get("program"))),
           "listed_on": rec.get("listed_on")}
    for k in LISTING_EXTRA:
        if not _blank(rec.get(k)):
            out[k] = rec[k]
    return out


def combine(records: list[dict], kind: str) -> dict:
    keys = VESSEL_KEYS if kind == "vessel" else AIRCRAFT_KEYS
    fill = VESSEL_FILL if kind == "vessel" else AIRCRAFT_FILL
    primary = records[0]
    out = {k: primary.get(k) for k in keys}
    out["kind"] = out.get("kind") or ("sanctioned_vessel" if kind == "vessel" else "sanctioned_aircraft")
    for k in ("linked_to", "aliases"):
        out[k] = list(out.get(k) or [])
    for rec in records[1:]:
        for k in fill:
            if _blank(out.get(k)) and not _blank(rec.get(k)):
                out[k] = rec[k]
    if len(records) > 1:
        seen = {norm_name(out.get("name"))} | {norm_name(a) for a in out["aliases"]}
        for rec in records[1:]:
            for n in [rec.get("name")] + list(rec.get("aliases") or []):
                k = norm_name(n)
                if k and k not in seen and len(out["aliases"]) < MAX_ALIASES:
                    seen.add(k)
                    out["aliases"].append(n)
        seen_l = {x.lower() for x in out["linked_to"]}
        for rec in records[1:]:
            for x in rec.get("linked_to") or []:
                if x and x.lower() not in seen_l and len(out["linked_to"]) < MAX_LINKED_MERGED:
                    seen_l.add(x.lower())
                    out["linked_to"].append(x)
        out["program"] = compat_program(records)
    elif (primary.get("list") or "OFAC") != "OFAC":
        out["program"] = compat_program(records)
    out["lists"] = [r.get("list") or "OFAC" for r in records]
    out["programmes"] = {r.get("list") or "OFAC": list(r.get("programmes") or split_programs(r.get("program")))
                         for r in records}
    out["listings"] = [listing(r) for r in records]
    dates = sorted(r["listed_on"] for r in records if r.get("listed_on"))
    out["listed_on"] = dates[0] if dates else None
    if kind == "vessel":
        imos = []
        for r in records:
            imos.extend(i for i in vessel_imos(r) if i not in imos)
        out["imos"] = imos
    return out
