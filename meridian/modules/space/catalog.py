"""CelesTrak GP (OMM) element sets: one cache per group in memory and on disk, compact records,
lookups by NORAD number and a small name index.

CelesTrak updates GP data about every two hours and asks clients not to download a group more
often than that. Each group is fetched only when the browser (or an AI tool) needs it, at most once
every two hours, and the raw JSON is kept in ``data/raw/space/celestrak/<group>.json`` so a restart
never refetches.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import sgp4 as S

GP_URL = "https://celestrak.org/NORAD/elements/gp.php"
MIN_REFRESH_S = 2 * 3600          # CelesTrak guidance: no more than once every two hours per group
FAIL_BACKOFF_S = 15 * 60          # after a failed fetch, wait before trying that group again
CATNR_TTL_S = 2 * 3600

# (id, title, family, default on, heavy). Families drive the map colour (see web/js/layers/space/common.js).
GROUPS: list[dict] = [
    {"id": "stations", "title": "Space stations", "family": "station", "default": True},
    {"id": "visual", "title": "Brightest satellites", "family": "general", "default": True},
    {"id": "weather", "title": "Weather satellites (includes NOAA)", "family": "earth", "default": True},
    {"id": "goes", "title": "GOES", "family": "earth", "default": False},
    {"id": "resource", "title": "Earth resources", "family": "earth", "default": False},
    {"id": "science", "title": "Science", "family": "earth", "default": True},
    {"id": "gps-ops", "title": "GPS", "family": "nav", "default": True},
    {"id": "galileo", "title": "Galileo", "family": "nav", "default": False},
    {"id": "glo-ops", "title": "GLONASS", "family": "nav", "default": False},
    {"id": "beidou", "title": "BeiDou", "family": "nav", "default": False},
    {"id": "amateur", "title": "Amateur radio", "family": "general", "default": False},
    {"id": "last-30-days", "title": "Launched in the last 30 days", "family": "general", "default": False},
    {"id": "geo", "title": "Geostationary belt", "family": "general", "default": False},
    {"id": "starlink", "title": "Starlink (about 10,000, heavy)", "family": "general", "default": False,
     "heavy": True},
]
GROUP_IDS = [g["id"] for g in GROUPS]
GROUP_BY_ID = {g["id"]: g for g in GROUPS}
FAMILY_RANK = {"station": 0, "nav": 1, "earth": 2, "general": 3}
FAMILY_TITLES = {"station": "Crewed and space stations", "nav": "Navigation", "earth": "Earth observation and science",
                 "general": "Other satellites"}

_NUM_FIELDS = (("mm", "MEAN_MOTION"), ("ecc", "ECCENTRICITY"), ("inc", "INCLINATION"), ("raan", "RA_OF_ASC_NODE"),
               ("argp", "ARG_OF_PERICENTER"), ("ma", "MEAN_ANOMALY"), ("bstar", "BSTAR"),
               ("mmdot", "MEAN_MOTION_DOT"), ("mmddot", "MEAN_MOTION_DDOT"))
ROW_FIELDS = ["id", "name", "cospar", "epoch", "mm", "ecc", "inc", "raan", "argp", "ma", "bstar", "mmdot", "mmddot"]
_CLEAN_NAME = re.compile(r"[\x00-\x1f\x7f]")
_WORDS = re.compile(r"[^A-Z0-9]+")
# Everyday names for famous objects (query in upper case -> NORAD number)
ALIASES = {"ISS": 25544, "INTERNATIONAL SPACE STATION": 25544, "SPACE STATION": 25544, "ZARYA": 25544,
           "TIANGONG": 48274, "CSS": 48274, "CHINESE SPACE STATION": 48274, "TIANHE": 48274,
           "HUBBLE": 20580, "HST": 20580, "HUBBLE SPACE TELESCOPE": 20580}
_FAMOUS = frozenset(ALIASES.values())


class GPError(Exception):
    pass


def compact(r: dict) -> dict | None:
    """One OMM JSON object -> the module's compact record, or None when it is unusable."""
    if not isinstance(r, dict):
        return None
    try:
        norad = int(r["NORAD_CAT_ID"])
        epoch = str(r["EPOCH"])[:40]
        epoch_unix = S.parse_epoch(epoch)
        vals = {k: float(r.get(src) if r.get(src) is not None else 0.0) for k, src in _NUM_FIELDS}
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < norad < 10_000_000) or not all(math.isfinite(v) for v in vals.values()):
        return None
    if not (0.05 < vals["mm"] < 20.0) or not (0.0 <= vals["ecc"] < 1.0):
        return None
    name = _CLEAN_NAME.sub("", str(r.get("OBJECT_NAME") or f"NORAD {norad}")).strip()[:60] or f"NORAD {norad}"
    cospar = _CLEAN_NAME.sub("", str(r.get("OBJECT_ID") or "")).strip()[:16]
    out = {"id": norad, "name": name, "cospar": cospar, "epoch": epoch, "epoch_unix": epoch_unix}
    out.update(vals)
    try:
        out["rev"] = int(r.get("REV_AT_EPOCH") or 0)
    except (TypeError, ValueError):
        out["rev"] = 0
    return out


def parse_gp(body: bytes | str) -> list[dict]:
    """CelesTrak GP JSON -> compact records. CelesTrak answers bad queries with a text message."""
    text = body.decode("utf-8-sig", errors="replace") if isinstance(body, bytes) else body
    stripped = text.strip()
    if not stripped.startswith("["):
        msg = re.sub(r"\s+", " ", stripped)[:160]
        raise GPError(f"CelesTrak answered with a message instead of data: {msg or 'empty response'}")
    try:
        data = json.loads(stripped)
    except ValueError as err:
        raise GPError(f"CelesTrak sent JSON that could not be read ({err})") from None
    out = []
    for item in data if isinstance(data, list) else []:
        rec = compact(item)
        if rec is not None:
            out.append(rec)
    return out


def row(rec: dict) -> list:
    return [rec[k] for k in ROW_FIELDS]


class Catalog:
    """Thread-safe group cache. ``status(ok, count, error, note)`` reports health to the core."""

    def __init__(self, net, raw_dir: Path, log, status=None):
        self.net = net
        self.dir = Path(raw_dir) / "celestrak"
        self.log = log
        self._status = status or (lambda **kw: None)
        self._lock = threading.RLock()
        self._groups: dict[str, dict] = {}
        self._fetch_locks: dict[str, threading.Lock] = {g: threading.Lock() for g in GROUP_IDS}
        self._refreshing: set[str] = set()
        self._by_id: dict[int, dict] = {}
        self._groups_of: dict[int, tuple[str, ...]] = {}
        self._extra: dict[int, tuple[float, dict | None]] = {}
        self._satrecs: OrderedDict = OrderedDict()
        self.wanted: dict[str, float] = {}

    # -- disk ------------------------------------------------------------------------------------
    def path(self, group: str) -> Path:
        return self.dir / f"{group}.json"

    def load_disk(self) -> int:
        """Load every cached group file (any age). Returns the number of groups loaded."""
        n = 0
        for g in GROUP_IDS:
            p = self.path(g)
            try:
                st = p.stat()
                recs = parse_gp(p.read_bytes())
            except FileNotFoundError:
                continue
            except (OSError, GPError) as err:
                self.log.warning("ignoring the cached CelesTrak group %s: %s", g, err)
                continue
            with self._lock:
                self._groups[g] = {"records": recs, "fetched_at": st.st_mtime, "error": None, "failed_at": 0.0}
            n += 1
        self._reindex()
        return n

    def _write(self, group: str, body: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(group)
        tmp = p.with_name(p.name + f".part{os.getpid()}")
        tmp.write_bytes(body)
        os.replace(tmp, p)

    # -- state -----------------------------------------------------------------------------------
    def info(self, group: str) -> dict:
        with self._lock:
            g = self._groups.get(group)
            meta = GROUP_BY_ID.get(group, {})
            if not g:
                return {"id": group, "title": meta.get("title", group), "family": meta.get("family", "general"),
                        "count": 0, "fetched_at": None, "age_s": None, "stale": True, "error": None}
            age = time.time() - g["fetched_at"] if g["fetched_at"] else None
            return {"id": group, "title": meta.get("title", group), "family": meta.get("family", "general"),
                    "count": len(g["records"]), "fetched_at": g["fetched_at"] or None,
                    "age_s": round(age) if age is not None else None,
                    "stale": age is None or age > MIN_REFRESH_S, "error": g.get("error")}

    def has(self, group: str) -> bool:
        with self._lock:
            return bool(self._groups.get(group, {}).get("records"))

    def loaded_groups(self) -> list[str]:
        with self._lock:
            return [g for g in GROUP_IDS if self._groups.get(g, {}).get("records")]

    def age(self, group: str) -> float | None:
        with self._lock:
            g = self._groups.get(group)
            return time.time() - g["fetched_at"] if g and g["fetched_at"] else None

    # -- fetching --------------------------------------------------------------------------------
    def refresh(self, group: str, *, force: bool = False) -> bool:
        """Fetch one group from CelesTrak (respecting the two-hour rule and the failure backoff)."""
        if group not in GROUP_BY_ID:
            raise KeyError(group)
        lock = self._fetch_locks[group]
        with lock:
            with self._lock:
                g = self._groups.get(group)
                now = time.time()
                if g and not force:
                    if g["fetched_at"] and now - g["fetched_at"] < MIN_REFRESH_S:
                        return True
                    if g.get("failed_at") and now - g["failed_at"] < FAIL_BACKOFF_S:
                        return False
            try:
                resp = self.net.request(GP_URL, params={"GROUP": group, "FORMAT": "json"}, timeout=30,
                                        retries=1, accept=(200, 403))
                if resp.status == 403:
                    raise GPError(_short(resp.body) or "CelesTrak refused the request (HTTP 403)")
                body = resp.body
                recs = parse_gp(body)
                if not recs:
                    raise GPError("CelesTrak returned no usable element sets")
            except Exception as err:   # NetError, GPError
                message = str(err)
                with self._lock:
                    g = self._groups.setdefault(group, {"records": [], "fetched_at": 0.0, "error": None,
                                                        "failed_at": 0.0})
                    g["error"] = message[:300]
                    g["failed_at"] = time.time()
                    have_any = bool(g["records"])
                if "not updated since" in message.lower() and have_any:
                    # CelesTrak says our copy is current: keep it and treat it as fresh
                    with self._lock:
                        g["fetched_at"] = time.time()
                        g["error"] = None
                    return True
                self.log.warning("CelesTrak group %s failed: %s", group, message)
                self._report()
                return False
            try:
                self._write(group, body)
            except OSError as err:
                self.log.warning("could not cache CelesTrak group %s: %s", group, err)
            with self._lock:
                self._groups[group] = {"records": recs, "fetched_at": time.time(), "error": None, "failed_at": 0.0}
            self._reindex()
            self.log.info("CelesTrak group %s: %d objects", group, len(recs))
            self._report()
            return True

    def refresh_async(self, group: str) -> None:
        with self._lock:
            if group in self._refreshing:
                return
            self._refreshing.add(group)

        def run():
            try:
                self.refresh(group)
            except Exception as err:  # never let a background refresh die loudly
                self.log.warning("background refresh of %s failed: %s", group, err)
            finally:
                with self._lock:
                    self._refreshing.discard(group)

        threading.Thread(target=run, daemon=True, name=f"space-gp-{group}").start()

    def ensure(self, group: str, *, max_age: float = MIN_REFRESH_S, block: bool = True) -> dict:
        """Make a group available: fetch now when missing (``block``), in the background when stale."""
        if group not in GROUP_BY_ID:
            raise KeyError(group)
        self.wanted[group] = time.time()
        age = self.age(group)
        if not self.has(group):
            if block:
                self.refresh(group)
            else:
                self.refresh_async(group)
        elif age is None or age > max_age:
            self.refresh_async(group)
        return self.info(group)

    def refresh_wanted(self, within_s: float = 3 * 3600) -> int:
        """Refresh stale groups the browser asked for recently (the periodic task)."""
        n = 0
        now = time.time()
        for group, t in list(self.wanted.items()):
            if now - t > within_s:
                continue
            age = self.age(group)
            if age is None or age > MIN_REFRESH_S:
                if self.refresh(group):
                    n += 1
        return n

    # -- lookups ---------------------------------------------------------------------------------
    def _reindex(self) -> None:
        by_id: dict[int, dict] = {}
        groups_of: dict[int, list[str]] = {}
        with self._lock:
            for g in GROUP_IDS:
                for rec in self._groups.get(g, {}).get("records", ()):
                    cur = by_id.get(rec["id"])
                    if cur is None or rec["epoch_unix"] > cur["epoch_unix"]:
                        by_id[rec["id"]] = rec
                    groups_of.setdefault(rec["id"], []).append(g)
            self._by_id = by_id
            self._groups_of = {k: tuple(v) for k, v in groups_of.items()}

    def _report(self) -> None:
        with self._lock:
            loaded = [g for g in GROUP_IDS if self._groups.get(g, {}).get("records")]
            errors = [f"{g}: {self._groups[g]['error']}" for g in GROUP_IDS
                      if self._groups.get(g, {}).get("error")]
            ages = [time.time() - self._groups[g]["fetched_at"] for g in loaded if self._groups[g]["fetched_at"]]
            count = len(self._by_id)
        note = f"{len(loaded)} groups, {count:,} objects"
        if ages:
            note += f", newest {min(ages) / 3600:.1f} h old"
        if errors and not loaded:
            self._status(ok=False, count=count, error="; ".join(errors)[:400], note=note)
        else:
            self._status(ok=True, count=count, note=note + (f" ({len(errors)} group errors)" if errors else ""))

    def report(self) -> None:
        self._report()

    def records(self, groups: list[str]) -> list[dict]:
        out = []
        seen = set()
        with self._lock:
            for g in groups:
                for rec in self._groups.get(g, {}).get("records", ()):
                    if rec["id"] in seen:
                        continue
                    seen.add(rec["id"])
                    out.append(self._by_id.get(rec["id"], rec))
        return out

    def group_ids(self, group: str) -> list[int]:
        with self._lock:
            return [r["id"] for r in self._groups.get(group, {}).get("records", ())]

    def groups_of(self, norad: int) -> tuple[str, ...]:
        with self._lock:
            return self._groups_of.get(int(norad), ())

    def family_of(self, norad: int) -> str:
        groups = self.groups_of(norad)
        fams = [GROUP_BY_ID[g]["family"] for g in groups if g in GROUP_BY_ID]
        return min(fams, key=lambda f: FAMILY_RANK[f]) if fams else "general"

    def lookup(self, norad: int) -> dict | None:
        norad = int(norad)
        with self._lock:
            rec = self._by_id.get(norad)
            if rec is not None:
                return rec
            hit = self._extra.get(norad)
            return hit[1] if hit else None

    def fetch_catnr(self, norad: int) -> dict | None:
        """One object by NORAD number (for favourites outside the loaded groups). Cached two hours."""
        norad = int(norad)
        rec = self.lookup(norad)
        if rec is not None:
            return rec
        with self._lock:
            hit = self._extra.get(norad)
            if hit and time.time() - hit[0] < CATNR_TTL_S:
                return hit[1]
        try:
            body = self.net.get_bytes(GP_URL, params={"CATNR": str(norad), "FORMAT": "json"}, timeout=20, retries=1)
            recs = parse_gp(body)
        except Exception as err:
            self.log.info("CelesTrak lookup of %s failed: %s", norad, err)
            recs = []
        rec = next((r for r in recs if r["id"] == norad), None)
        with self._lock:
            self._extra[norad] = (time.time(), rec)
            while len(self._extra) > 500:
                self._extra.pop(next(iter(self._extra)))
        return rec

    def all_records(self) -> list[dict]:
        with self._lock:
            return list(self._by_id.values())

    def count(self) -> int:
        with self._lock:
            return len(self._by_id)

    def satrec(self, rec: dict) -> S.Satrec:
        key = (rec["id"], rec["epoch_unix"])
        with self._lock:
            s = self._satrecs.get(key)
            if s is not None:
                self._satrecs.move_to_end(key)
                return s
        s = S.satrec_from_omm(rec)
        with self._lock:
            self._satrecs[key] = s
            while len(self._satrecs) > 4000:
                self._satrecs.popitem(last=False)
        return s

    def search(self, q: str, limit: int = 20) -> list[tuple[float, dict]]:
        """Name, NORAD number or international designator matches, best first.

        Short queries match the start of a word only ("iss" finds ISS, not SWISSCUBE); four or more
        characters also match inside names."""
        qn = q.strip().upper()
        if len(qn) < 2:
            return []
        digits = qn.isascii() and qn.isdigit() and len(qn) <= 9
        hits: dict[int, float] = {}
        with self._lock:
            alias = ALIASES.get(qn)
            if alias and alias in self._by_id:
                hits[alias] = 0.97
            if digits:
                if int(qn) in self._by_id:
                    hits[int(qn)] = max(hits.get(int(qn), 0), 0.95)
                if len(qn) < 4:
                    return [(s, self._by_id[i]) for i, s in hits.items()]
            for rec in self._by_id.values():
                name = rec["name"].upper()
                if name == qn or rec["cospar"].upper() == qn:
                    score = 0.9
                elif name.startswith(qn):
                    score = 0.74
                elif any(w.startswith(qn) for w in _WORDS.split(name) if w):
                    score = 0.62
                elif len(qn) >= 4 and qn in name:
                    score = 0.5
                else:
                    continue
                score -= min(0.15, len(name) / 600.0)
                if rec["id"] in self._groups_of and "stations" in self._groups_of[rec["id"]]:
                    score += 0.03
                if rec["id"] in _FAMOUS:          # the station itself ranks above its modules
                    score += 0.05
                hits[rec["id"]] = max(hits.get(rec["id"], 0.0), score)
        ranked = sorted(((s, self._by_id[i]) for i, s in hits.items()), key=lambda h: (-h[0], h[1]["name"]))
        return ranked[:limit]


def _short(body: bytes | None) -> str:
    if not body:
        return ""
    return re.sub(r"\s+", " ", body.decode("utf-8", errors="replace"))[:160]
