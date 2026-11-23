"""Dams from Wikidata (CC0): notable dams with coordinates, i.e. those with a height, reservoir
volume, generating capacity, a reservoir item or an English Wikipedia article."""

from __future__ import annotations

import time

from . import countries
from .common import (Collection, compact_num, fc, fnum, log_weight, point_feature, sparql_batches, sparql_csv, text,
                     values_clause, wd_id, wkt_point, year_of)

SOURCE_ID = "infra.wikidata_dams"
PAGE = "https://www.wikidata.org/wiki/Q12323"
DAM_CLASS = "?d wdt:P31/wdt:P279* wd:Q12323 ; wdt:P625 [] ."
BATCH = 800
MIN_DAMS = 3000
LABEL_LANGS = ("en", "mul", "fr", "es", "pt", "de", "it", "nl", "sv", "cs", "pl", "tr", "ru", "uk", "fa", "ar",
               "ja", "zh", "ko", "vi", "id", "hi")

Q_HEIGHT = "SELECT ?d ?h WHERE { %s ?d p:P2048/psn:P2048/wikibase:quantityAmount ?h . }" % DAM_CLASS
Q_VOLUME = "SELECT ?d ?v WHERE { %s ?d p:P2234/psn:P2234/wikibase:quantityAmount ?v . }" % DAM_CLASS
Q_POWER = "SELECT ?d ?w WHERE { %s ?d p:P2109/psn:P2109/wikibase:quantityAmount ?w . }" % DAM_CLASS
Q_RESERVOIR = "SELECT DISTINCT ?d WHERE { %s ?d wdt:P4661 [] . }" % DAM_CLASS
Q_RES_VOLUME = ("SELECT ?d ?v WHERE { %s ?d wdt:P4661 ?r . ?r p:P2234/psn:P2234/wikibase:quantityAmount ?v . }"
                % DAM_CLASS)
Q_ENWIKI = ("SELECT ?d ?wp WHERE { %s ?wp schema:about ?d ; schema:isPartOf <https://en.wikipedia.org/> . }"
            % DAM_CLASS)

Q_DETAIL = """SELECT ?d ?l ?coord ?cc ?river ?water ?inc ?open ?res ?op ?len ?use WHERE {
  %s
  ?d wdt:P625 ?coord .
  OPTIONAL { ?d rdfs:label ?l FILTER(LANG(?l) = "en") }
  OPTIONAL { ?d wdt:P17/wdt:P297 ?cc }
  OPTIONAL { ?d wdt:P177 ?rv . ?rv rdfs:label ?river FILTER(LANG(?river) = "en") }
  OPTIONAL { ?d wdt:P206 ?wb . ?wb rdfs:label ?water FILTER(LANG(?water) = "en") }
  OPTIONAL { ?d wdt:P571 ?inc }
  OPTIONAL { ?d wdt:P1619 ?open }
  OPTIONAL { ?d wdt:P4661 ?rs . ?rs rdfs:label ?res FILTER(LANG(?res) = "en") }
  OPTIONAL { ?d wdt:P137 ?o . ?o rdfs:label ?op FILTER(LANG(?op) = "en") }
  OPTIONAL { ?d p:P2043/psn:P2043/wikibase:quantityAmount ?len }
  OPTIONAL { ?d wdt:P366 ?u . ?u rdfs:label ?use FILTER(LANG(?use) = "en") }
}"""

Q_LABELS = """SELECT ?d ?l ?lang WHERE { %s ?d rdfs:label ?l . BIND(LANG(?l) AS ?lang) FILTER(?lang IN (%s)) }"""


def _max_by(rows: list[dict], key: str, value: str, pick=max) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        q = wd_id(r.get(key))
        v = fnum(r.get(value))
        if q and v is not None and v > 0:
            out[q] = pick(out[q], v) if q in out else v
    return out


def assemble(details: list[dict], labels: dict[str, str], heights: dict[str, float], volumes: dict[str, float],
             powers: dict[str, float], enwiki: dict[str, str], reservoir_volumes: dict[str, float] | None = None) -> list[dict]:
    """Merge the per-batch detail rows (one row per value combination) into one record per dam.

    ``volumes`` is the dam item's own volume (often the dam body, sometimes the reservoir);
    ``reservoir_volumes`` is the capacity recorded on the reservoir item, shown as the reservoir."""
    reservoir_volumes = reservoir_volumes or {}
    by: dict[str, dict] = {}
    for r in details:
        q = wd_id(r.get("d"))
        if not q:
            continue
        rec = by.get(q)
        if rec is None:
            pos = wkt_point(r.get("coord"))
            if not pos:
                continue
            rec = by[q] = {"id": f"wikidata:{q}", "qid": q, "lat": pos[0], "lon": pos[1], "name": None,
                           "country": None, "river": None, "water": None, "year": None, "reservoir": None,
                           "operator": None,
                           "length_m": None, "uses": []}
        if not rec["name"] and r.get("l"):
            rec["name"] = text(r["l"], 120)
        if not rec["country"] and r.get("cc"):
            rec["country"] = text(r["cc"], 2)
        if not rec["river"] and r.get("river"):
            rec["river"] = text(r["river"], 80)
        if not rec["water"] and r.get("water"):
            rec["water"] = text(r["water"], 80)
        y = year_of(r.get("open")) or year_of(r.get("inc"))
        if y and (rec["year"] is None or y < rec["year"]):
            rec["year"] = y
        if not rec["reservoir"] and r.get("res"):
            rec["reservoir"] = text(r["res"], 100)
        if not rec["operator"] and r.get("op"):
            rec["operator"] = text(r["op"], 100)
        ln = fnum(r.get("len"))
        if ln and ln > 0 and not rec["length_m"]:
            rec["length_m"] = compact_num(ln, 0)
        use = text(r.get("use"), 40)
        if use and use not in rec["uses"] and len(rec["uses"]) < 5:
            rec["uses"].append(use)
    out = []
    for q, rec in by.items():
        rec["name"] = rec["name"] or labels.get(q) or f"Dam {q}"
        if rec["water"] in (rec["river"], rec["reservoir"]):
            rec["water"] = None
        rec["country_name"] = countries.name(rec["country"])
        h = heights.get(q)
        rec["height_m"] = compact_num(h, 1) if h and h < 400 else None
        v = volumes.get(q)
        rec["volume_mcm"] = compact_num(v / 1e6, 1) if v and v < 1e12 else None   # million m3, as entered
        rv = reservoir_volumes.get(q)
        rec["reservoir_mcm"] = compact_num(rv / 1e6, 1) if rv and rv < 1e12 else None
        w = powers.get(q)
        rec["power_mw"] = compact_num(w / 1e6, 1) if w and w < 5e10 else None
        rec["wikipedia"] = enwiki.get(q)
        rec["wikidata"] = f"https://www.wikidata.org/wiki/{q}"
        out.append(rec)
    return out


def _safe(ds, label: str, query: str, required: bool = False) -> list[dict]:
    try:
        return sparql_csv(ds.net, query)
    except Exception as err:
        if required:
            raise
        ds.note(f"Wikidata {label} query failed ({type(err).__name__})")
        ds.log.warning("dams: %s query failed: %s", label, err)
        return []


def build(ds) -> None:
    # Several heights usually mean structural and hydraulic height, or a stray crest elevation: keep the lowest.
    heights = _max_by(_safe(ds, "height", Q_HEIGHT, required=True), "d", "h", pick=min)
    volumes = _max_by(_safe(ds, "volume", Q_VOLUME), "d", "v")
    res_volumes = _max_by(_safe(ds, "reservoir volume", Q_RES_VOLUME), "d", "v")
    powers = _max_by(_safe(ds, "power", Q_POWER), "d", "w")
    reservoirs = {wd_id(r.get("d")) for r in _safe(ds, "reservoir", Q_RESERVOIR)} - {None}
    enwiki = {}
    for r in _safe(ds, "English Wikipedia", Q_ENWIKI):
        q = wd_id(r.get("d"))
        if q and r.get("wp", "").startswith("https://en.wikipedia.org/"):
            enwiki[q] = r["wp"]
    candidates = sorted(set(heights) | set(volumes) | set(res_volumes) | set(powers) | reservoirs | set(enwiki))
    if len(candidates) < MIN_DAMS:
        raise RuntimeError(f"Wikidata returned too few dams ({len(candidates)})")
    details, failed = sparql_batches(ds.net, candidates, lambda ch: Q_DETAIL % values_clause("d", ch), BATCH,
                                     log=ds.log, label="dams: details")
    if failed > len(candidates) // 5:
        raise RuntimeError(f"Wikidata detail queries failed for {failed} dams")
    named = {wd_id(r.get("d")) for r in details if r.get("l")}
    missing = [q for q in candidates if q not in named]
    langs = ", ".join(f'"{x}"' for x in LABEL_LANGS)
    rows, _ = sparql_batches(ds.net, missing, lambda ch: Q_LABELS % (values_clause("d", ch), langs), BATCH * 2,
                             log=ds.log, label="dams: labels")
    from .companies import pick_labels
    labels = pick_labels(rows, "d", LABEL_LANGS)
    dams = assemble(details, labels, heights, volumes, powers, enwiki, res_volumes)
    dams.sort(key=lambda d: (d.get("height_m") or 0))
    ds.write_json("dams.geojson", fc([point_feature(d["lon"], d["lat"], {
        "id": d["id"], "n": d["name"][:60], "h": int(d["height_m"]) if d.get("height_m") else None,
        "v": int(d["reservoir_mcm"]) if d.get("reservoir_mcm") else None,
        "p": 1 if d.get("power_mw") else None}) for d in dams]))
    ds.write_json("dams-details.json", {"built_at": time.time(), "items": dams})
    ds.set_rows(len(dams))
    ds.note(f"{len(dams):,} dams ({sum(1 for d in dams if d.get('height_m')):,} with a height)")


def subtitle(d: dict) -> str:
    parts = ["Dam"]
    if d.get("height_m"):
        parts[0] = f"Dam, {d['height_m']:g} m high"
    if d.get("river"):
        parts.append(d["river"])
    if d.get("country_name"):
        parts.append(d["country_name"])
    return ", ".join(parts)


def collection(items: list[dict], built_at: float | None) -> Collection:
    rows = []
    for d in items:
        w = max(log_weight(d.get("height_m"), 5, 300), log_weight(d.get("reservoir_mcm"), 1, 100000))
        rows.append({"id": d["id"], "title": d["name"], "subtitle": subtitle(d), "lat": d["lat"], "lon": d["lon"],
                     "weight": w, "country": d.get("country"), "codes": [d["id"], d["qid"]],
                     "text": " ".join(x for x in (d.get("river"), d.get("water"), d.get("reservoir"), d.get("operator")) if x),
                     "detail": d})
    return Collection("dam", "infra.dams", rows, cell_deg=1.0, zoom=12, built_at=built_at)
