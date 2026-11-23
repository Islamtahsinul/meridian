"""WHO Disease Outbreak News (the public OData API behind who.int/emergencies/disease-outbreak-news).

WHO terms of use: non-commercial reuse with attribution. Reports are placed at the label points
of the countries named in their titles (or, failing that, in their summaries).
"""

from __future__ import annotations

import re

from . import countries
from .common import clean, parse_time, strip_html

API = "https://www.who.int/api/news/diseaseoutbreaknews"
ITEM_PAGE = "https://www.who.int/emergencies/disease-outbreak-news/item/{slug}"
FIELDS = ("Id,Title,OverrideTitle,UseOverrideTitle,PublicationDate,PublicationDateAndTime,LastModified,UrlName,"
          "DonId,Summary,Overview,Assessment,Advice,Epidemiology,Response")
ACTIVE_DAYS = 60

HIGH_CONSEQUENCE = re.compile(r"ebola|marburg|bundibugyo|sudan virus|lassa|nipah|mers|middle east respiratory|"
                              r"avian influenza|h5n1|h7n9|h5n6|plague|smallpox|mpox|monkeypox|anthrax|"
                              r"crimean-congo|rift valley|yellow fever|cholera|diphtheria|polio|novel|"
                              r"undetermined|unknown", re.I)
_SPLIT = re.compile(r"\s*(?:\s[-–—]\s|[-–—]\s|\s[-–—]|�|:)\s*")


def query_params(top: int = 50) -> dict:
    return {"$orderby": "PublicationDate desc", "$top": str(top), "$select": FIELDS}


def split_title(title: str) -> tuple[str, str]:
    """("Ebola disease caused by Bundibugyo virus", "Democratic Republic of the Congo, Kenya and Nigeria")."""
    t = clean(title, 300)
    m = _SPLIT.search(t)
    if m and m.start() > 3:
        return t[: m.start()].strip(" ,"), t[m.end():].strip(" ,")
    i = countries.first_mention(t)
    if i is not None and i > 3:
        head = re.sub(r"(?:\s+(?:in|the))+\s*$", "", t[:i].rstrip(" ,-"), flags=re.I)
        return head.strip(" ,-"), t[i:].strip()
    if "," in t:
        head, _, tail = t.partition(",")
        return head.strip(), tail.strip()
    return t, ""


def locate(title: str, summary: str) -> tuple[list[dict], str | None]:
    """Countries for a report (title first, then summary) and a region label if none are named."""
    _, place = split_title(title)
    found = countries.find_in_text(place or title)
    if not found:
        found = countries.find_in_text(title)
    region = None
    if not found:
        r = countries.find_region(place or title)
        if r:
            region = r
        found = countries.find_in_text(summary, limit=6)
    out = [{"iso2": c["iso2"], "name": c["name"], "lat": c["lat"], "lon": c["lon"]} for c in found]
    if not out and region:
        label, lat, lon = region
        out = [{"iso2": None, "name": label, "lat": lat, "lon": lon}]
    return out, (region[0] if region else None)


def parse_items(data: dict) -> list[dict]:
    out = []
    for it in (data or {}).get("value") or []:
        item = parse_item(it)
        if item:
            out.append(item)
    return out


def parse_item(it: dict) -> dict | None:
    if not isinstance(it, dict):
        return None
    title = it.get("OverrideTitle") if it.get("UseOverrideTitle") and it.get("OverrideTitle") else it.get("Title")
    title = clean((title or "").replace("�", "–"), 300)
    slug = it.get("UrlName") or ""
    don = it.get("DonId") or slug
    published = parse_time(it.get("PublicationDateAndTime")) or parse_time(it.get("PublicationDate"))
    if not title or not don or published is None:
        return None
    summary = strip_html(it.get("Summary"), 1800)
    places, region = locate(title, summary)
    disease, place_text = split_title(title)
    sections = {}
    for key, label in (("Overview", "Overview"), ("Assessment", "WHO risk assessment"), ("Advice", "WHO advice"),
                       ("Epidemiology", "About the disease"), ("Response", "Public health response")):
        txt = strip_html(it.get(key), 1500)
        if txt:
            sections[label] = txt
    return {
        "id": f"who:{don}",
        "don": don,
        "title": title,
        "disease": disease or title,
        "place_text": place_text or ", ".join(p["name"] for p in places),
        "published": published,
        "modified": parse_time(it.get("LastModified")),
        "summary": summary or None,
        "sections": sections,
        "places": places,
        "region": region,
        "url": ITEM_PAGE.format(slug=slug) if slug else "https://www.who.int/emergencies/disease-outbreak-news",
        "high_consequence": bool(HIGH_CONSEQUENCE.search(title)),
    }


def disease_key(disease: str) -> str:
    """Group follow-up reports of one outbreak ("Ebola disease caused by Bundibugyo virus")."""
    s = re.sub(r"\(.*?\)", "", disease or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    words = [w for w in s.split() if w not in ("disease", "caused", "by", "virus", "outbreak", "the", "of", "in",
                                               "human", "cases", "case", "infection", "update")]
    return " ".join(words[:4])


def severity(item: dict, now: float) -> int:
    age_days = (now - item["published"]) / 86400.0
    sev = 6 if item.get("high_consequence") else 4
    if len(item.get("places") or []) > 1:
        sev += 1
    if age_days > 30:
        sev -= 1
    return max(3, min(8, sev))
