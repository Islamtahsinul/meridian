"""UCDP Georeferenced Event Dataset (GED): historical organised violence, 1989 to last year, plus
the monthly candidate events of the current year. Aggregated into 0.5-degree cells per year.

The public downloads (https://ucdp.uu.se/downloads/) need no key. The newest GED CSV zip and the
current candidate CSVs are found by link pattern on that page. Events geocoded only to a country
or to international waters (where_prec 6 and 7) are counted in notes but not mapped.
"""

from __future__ import annotations

import contextlib
import csv
import io
import math
import re
import zipfile
from collections import Counter, defaultdict

DOWNLOADS = "https://ucdp.uu.se/downloads/"
SITE = "https://ucdp.uu.se/"
CELL = 0.5
VIOLENCE = {"1": "state-based", "2": "non-state", "3": "one-sided"}
MAX_UNZIPPED = 1_200_000_000

_RE_GED = re.compile(r'https://ucdp\.uu\.se/downloads/ged/ged(\d{2,3})-csv\.zip')
_RE_CAND = re.compile(r'https://ucdp\.uu\.se/downloads/candidateged/GEDEvent_v(\d{2})_[0-9_]+\.csv')


def find_links(html: str) -> dict:
    """{'ged': (version, url) | None, 'candidates': [url, ...]} from the downloads page."""
    ged = None
    for m in _RE_GED.finditer(html or ""):
        v = int(m.group(1))
        if ged is None or v > ged[0]:
            ged = (v, m.group(0))
    cands = []
    years = [int(m.group(1)) for m in _RE_CAND.finditer(html or "")]
    newest = max(years) if years else None
    for m in _RE_CAND.finditer(html or ""):
        if int(m.group(1)) == newest and m.group(0) not in cands:
            cands.append(m.group(0))
    return {"ged": ged, "candidates": cands[:4]}


class Aggregator:
    def __init__(self, cell: float = CELL):
        self.cell = cell
        self.cells: dict[tuple[int, int], dict] = {}
        self.seen: set[str] = set()
        self.unmapped = 0
        self.rows = 0
        self.years = Counter()
        self.deaths_by_year = Counter()

    def add(self, row: dict) -> None:
        eid = (row.get("id") or "").strip()
        if eid:
            if eid in self.seen:
                return
            self.seen.add(eid)
        try:
            year = int(float(row.get("year") or 0))
            lat = float(row.get("latitude"))
            lon = float(row.get("longitude"))
        except (TypeError, ValueError):
            return
        if not (1946 <= year <= 2100 and math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            return
        try:
            best = max(0, int(float(row.get("best") or 0)))
        except (TypeError, ValueError):
            best = 0
        self.rows += 1
        self.years[year] += 1
        self.deaths_by_year[year] += best
        if (row.get("where_prec") or "").strip() in ("6", "7"):
            self.unmapped += 1
            return
        key = (int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell)))
        c = self.cells.get(key)
        if c is None:
            c = self.cells[key] = {"years": defaultdict(lambda: [0, 0, 0, 0, 0]), "conflicts": Counter(),
                                   "countries": Counter()}
        y = c["years"][year]
        y[0] += 1
        y[1] += best
        tv = (row.get("type_of_violence") or "").strip()
        if tv in ("1", "2", "3"):
            y[1 + int(tv)] += best
        name = (row.get("conflict_name") or row.get("dyad_name") or "").strip()
        if name:
            c["conflicts"][name[:120]] += best + 1
        country = (row.get("country") or "").strip()
        if country:
            c["countries"][country[:80]] += 1

    def add_csv(self, text_stream) -> int:
        n = 0
        for row in csv.DictReader(text_stream):
            self.add(row)
            n += 1
        return n

    def result(self, version: str, candidates: list[str]) -> dict:
        cells = []
        for (r, c), d in self.cells.items():
            lat = round((r + 0.5) * self.cell, 3)
            lon = round((c + 0.5) * self.cell, 3)
            years = {str(y): v for y, v in sorted(d["years"].items())}
            cells.append([lat, lon, years, [n for n, _ in d["conflicts"].most_common(3)],
                          [n for n, _ in d["countries"].most_common(2)]])
        return {"version": version, "candidates": candidates, "cell": self.cell, "cells": cells,
                "events": self.rows, "unmapped": self.unmapped,
                "years": {str(y): [n, self.deaths_by_year[y]] for y, n in sorted(self.years.items())}}


@contextlib.contextmanager
def open_zip_csv(path):
    """The single CSV inside a GED zip as a text stream (read, never extracted to disk)."""
    with zipfile.ZipFile(path) as zf:
        members = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError("unexpected UCDP archive layout")
        if members[0].file_size > MAX_UNZIPPED:
            raise ValueError("UCDP archive member too large")
        with zf.open(members[0]) as raw:
            yield io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")


def cell_id(lat: float, lon: float) -> str:
    return f"ucdp:{lat:.2f}:{lon:.2f}"


def cells_geojson(data: dict, y0: int, y1: int) -> dict:
    feats = []
    for lat, lon, years, conflicts, countries in data.get("cells") or []:
        ev = deaths = d1 = d2 = d3 = 0
        for y, v in years.items():
            yi = int(y)
            if y0 <= yi <= y1:
                ev += v[0]
                deaths += v[1]
                d1 += v[2]
                d2 += v[3]
                d3 += v[4]
        if not ev:
            continue
        kind = max((("state", d1), ("nonstate", d2), ("onesided", d3)), key=lambda t: t[1])[0] if (d1 or d2 or d3) else "state"
        feats.append({"type": "Feature", "properties": {"id": cell_id(lat, lon), "events": ev, "deaths": deaths, "type": kind,
                                                        "conflict": conflicts[0] if conflicts else "",
                                                        "country": countries[0] if countries else ""},
                      "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    return {"type": "FeatureCollection", "features": feats}
