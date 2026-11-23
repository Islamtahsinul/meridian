"""NOAA tsunami warning centres (tsunami.gov): the latest message from the National Tsunami Warning
Center (Palmer, Alaska) and the Pacific Tsunami Warning Center (Honolulu). US public domain.

Each Atom feed only holds the newest bulletin, so the module keeps a short history in SQLite.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .common import clean, num, parse_time, r5, valid_lonlat

FEEDS = {
    "PAAQ": ("National Tsunami Warning Center", "https://www.tsunami.gov/events/xml/PAAQAtom.xml"),
    "PHEB": ("Pacific Tsunami Warning Center", "https://www.tsunami.gov/events/xml/PHEBAtom.xml"),
}
NS = "{http://www.w3.org/2005/Atom}"
GEO = "{http://www.w3.org/2003/01/geo/wgs84_pos#}"
FIELDS = ("Category", "Bulletin Issue Time", "Preliminary Magnitude", "Lat/Lon", "Affected Region", "Note",
          "Definition")
CATEGORIES = ("Warning", "Threat", "Watch", "Advisory", "Information", "Cancellation")


def _xhtml_text(el) -> str:
    """Flatten an XHTML summary, turning <br/> into line breaks."""
    parts: list[str] = []

    def walk(node):
        if node.text:
            parts.append(node.text)
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1].lower()
            if tag == "br":
                parts.append("\n")
            elif tag not in ("img",):
                walk(child)
            if child.tail:
                parts.append(child.tail)

    if el is not None:
        walk(el)
    return "".join(parts)


def parse_fields(text: str) -> dict:
    out = {}
    pattern = re.compile(r"(%s):\s*" % "|".join(re.escape(f) for f in FIELDS))
    matches = list(pattern.finditer(text or ""))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group(1)] = " ".join(text[m.end():end].split())
    return out


def _sentence(text: str | None) -> str:
    """The Pacific centre writes in capitals: "ABOUT 65 MILES NW OF ISLA MONA" -> "About 65 miles nw of isla mona"."""
    s = clean(text, 200)
    if s and s.upper() == s and re.search(r"[A-Z]{3}", s):
        s = s[:1] + s[1:].lower()
    return s


def parse_atom(text: str, center: str) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    center_name = FEEDS.get(center, (center, ""))[0]
    feed_title = _sentence(root.findtext(NS + "title"))
    out = []
    for entry in root.findall(NS + "entry"):
        uid = (entry.findtext(NS + "id") or "").strip()
        lat = num(entry.findtext(GEO + "lat"))
        lon = num(entry.findtext(GEO + "long"))
        if not uid or lat is None or lon is None or not valid_lonlat(lon, lat):
            continue
        fields = parse_fields(_xhtml_text(entry.find(NS + "summary")))
        category = (fields.get("Category") or "").strip().capitalize()
        if category not in CATEGORIES:
            low = (feed_title or "").lower()
            category = next((c for c in CATEGORIES if c.lower() in low), "Information")
        mag_text = fields.get("Preliminary Magnitude") or ""
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(?:\((\w+)\))?", mag_text)
        bulletin = cap = None
        for link in entry.findall(NS + "link"):
            href = (link.get("href") or "").strip()
            if not href.startswith("https://"):
                continue
            if link.get("rel") == "alternate":
                bulletin = href
            elif "cap" in (link.get("type") or "").lower():
                cap = href
        number = re.search(r"number\s+(\d+)", feed_title or "", re.I)
        out.append({
            "id": f"tsunami:{center}:{uid.split(':')[-1]}",
            "center": center,
            "center_name": center_name,
            "title": feed_title or f"Tsunami {category.lower()} statement",
            "number": int(number.group(1)) if number else None,
            "category": category,
            "region": _sentence(entry.findtext(NS + "title") or fields.get("Affected Region")),
            "lat": r5(lat), "lon": r5(lon),
            "magnitude": float(m.group(1)) if m else None,
            "magnitude_type": m.group(2) if m and m.group(2) else None,
            "issued": parse_time(fields.get("Bulletin Issue Time")) or parse_time(entry.findtext(NS + "updated")),
            "updated": parse_time(entry.findtext(NS + "updated")),
            "note": clean(fields.get("Note"), 600) or None,
            "definition": clean(re.sub(r"\s*View bulletin\s*$", "", fields.get("Definition") or ""), 600) or None,
            "bulletin_url": bulletin,
            "cap_url": cap,
        })
    return out
