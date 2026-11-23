"""Official travel advice: UK FCDO (GOV.UK content API) and the US State Department travel advisories.

Pure parsing, level normalisation and change detection (no network, no database). The poller and its
storage live in ``advice_service.py``.

Common scale (1-4), with the original wording always kept next to it:

| Level | Meaning             | FCDO                                                        | US State Department |
|-------|---------------------|-------------------------------------------------------------|---------------------|
| 1     | Normal precautions  | no advice against travel                                    | Level 1             |
| 2     | Some areas          | against all (but essential) travel to parts of the country  | Level 2             |
| 3     | Reconsider          | against all but essential travel to the whole country       | Level 3             |
| 4     | Do not travel       | against all travel to the whole country                     | Level 4             |

The FCDO publishes four status codes (``alert_status``). They are coarse: a country where FCDO
"advises against all travel to Sudan" apart from two small border areas is coded
``avoid_all_travel_to_parts``, and Chad's code omits that the rest of the country is "all but
essential". So the level is the higher of the codes and of a conservative reading of the
"advises against" sentences on the "Warnings and insurance" page: a sentence that names the whole
country ("against all travel to Sudan", "to the whole of the Central African Republic") or its
remainder ("to the rest of Chad", "to all other regions of Ukraine", "to the remaining areas of
Venezuela"). Statuses for parts never become a whole-country level on their own.
"""

from __future__ import annotations

import calendar
import datetime as dt
import html
import re

FCDO_INDEX_URL = "https://www.gov.uk/api/content/foreign-travel-advice"
FCDO_API = "https://www.gov.uk/api/content/foreign-travel-advice/"
FCDO_SITE = "https://www.gov.uk/foreign-travel-advice"
FCDO_HOST = "www.gov.uk"
US_URL = "https://cadataapi.state.gov/api/TravelAdvisories"
US_SITE = "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories.html"
US_HOST = "cadataapi.state.gov"
# Stored FCDO entries parsed by an older version are read again once (normalisation changes reach every country).
PARSER_VERSION = 2

AAT_WHOLE = "avoid_all_travel_to_whole_country"
AABE_WHOLE = "avoid_all_but_essential_travel_to_whole_country"
AAT_PARTS = "avoid_all_travel_to_parts"
AABE_PARTS = "avoid_all_but_essential_travel_to_parts"
FCDO_CODES = (AAT_WHOLE, AABE_WHOLE, AAT_PARTS, AABE_PARTS)
FCDO_CODE_WORDS = {
    AAT_WHOLE: "against all travel to the whole country",
    AABE_WHOLE: "against all but essential travel to the whole country",
    AAT_PARTS: "against all travel to parts of the country",
    AABE_PARTS: "against all but essential travel to parts of the country",
}
_CODE_LEVEL = {AAT_WHOLE: 4, AABE_WHOLE: 3, AAT_PARTS: 2, AABE_PARTS: 2}

LEVEL_NAMES = {1: "Normal precautions", 2: "Some areas advised against", 3: "Reconsider travel", 4: "Do not travel"}
US_LEVEL_WORDS = {1: "Exercise normal precautions", 2: "Exercise increased caution", 3: "Reconsider travel",
                  4: "Do not travel"}

# FCDO page slug -> ISO 3166-1 alpha-2 (all 226 pages on 23 November 2026; one page can cover several
# territories). Pages added later fall back to a country-name match.
_FCDO_SLUGS = """
afghanistan=AF albania=AL algeria=DZ andorra=AD angola=AO anguilla=AI
antarctica-british-antarctic-territory=AQ antigua-and-barbuda=AG argentina=AR armenia=AM aruba=AW
australia=AU austria=AT azerbaijan=AZ bahamas=BS bahrain=BH bangladesh=BD barbados=BB belarus=BY belgium=BE
belize=BZ benin=BJ bermuda=BM bhutan=BT bolivia=BO bonaire-st-eustatius-saba=BQ bosnia-and-herzegovina=BA
botswana=BW brazil=BR british-indian-ocean-territory=IO british-virgin-islands=VG brunei=BN bulgaria=BG
burkina-faso=BF burundi=BI cambodia=KH cameroon=CM canada=CA cape-verde=CV cayman-islands=KY
central-african-republic=CF chad=TD chile=CL china=CN colombia=CO comoros=KM congo=CG
cook-islands-tokelau-and-niue=CK,TK,NU costa-rica=CR cote-d-ivoire=CI croatia=HR cuba=CU curacao=CW
cyprus=CY czechia=CZ democratic-republic-of-the-congo=CD denmark=DK djibouti=DJ dominica=DM
dominican-republic=DO ecuador=EC egypt=EG el-salvador=SV equatorial-guinea=GQ eritrea=ER estonia=EE
eswatini=SZ ethiopia=ET falkland-islands=FK federated-states-of-micronesia=FM fiji=FJ finland=FI france=FR
french-guiana=GF french-polynesia=PF gabon=GA georgia=GE germany=DE ghana=GH gibraltar=GI greece=GR
grenada=GD guadeloupe=GP guatemala=GT guinea=GN guinea-bissau=GW guyana=GY haiti=HT honduras=HN hong-kong=HK
hungary=HU iceland=IS india=IN indonesia=ID iran=IR iraq=IQ ireland=IE israel=IL italy=IT jamaica=JM
japan=JP jordan=JO kazakhstan=KZ kenya=KE kiribati=KI kosovo=XK kuwait=KW kyrgyzstan=KG laos=LA latvia=LV
lebanon=LB lesotho=LS liberia=LR libya=LY liechtenstein=LI lithuania=LT luxembourg=LU macao=MO madagascar=MG
malawi=MW malaysia=MY maldives=MV mali=ML malta=MT marshall-islands=MH martinique=MQ mauritania=MR
mauritius=MU mayotte=YT mexico=MX moldova=MD monaco=MC mongolia=MN montenegro=ME montserrat=MS morocco=MA
mozambique=MZ myanmar=MM namibia=NA nauru=NR nepal=NP netherlands=NL new-caledonia=NC new-zealand=NZ
nicaragua=NI niger=NE nigeria=NG north-korea=KP north-macedonia=MK norway=NO oman=OM pakistan=PK palau=PW
palestine=PS panama=PA papua-new-guinea=PG paraguay=PY peru=PE philippines=PH pitcairn-island=PN poland=PL
portugal=PT qatar=QA reunion=RE romania=RO russia=RU rwanda=RW samoa=WS san-marino=SM
sao-tome-and-principe=ST saudi-arabia=SA senegal=SN serbia=RS seychelles=SC sierra-leone=SL singapore=SG
slovakia=SK slovenia=SI solomon-islands=SB somalia=SO south-africa=ZA
south-georgia-and-south-sandwich-islands=GS south-korea=KR south-sudan=SS spain=ES sri-lanka=LK
st-helena-ascension-and-tristan-da-cunha=SH st-kitts-and-nevis=KN st-lucia=LC st-maarten=SX
st-martin-and-st-barthelemy=MF,BL st-pierre-and-miquelon=PM st-vincent-and-the-grenadines=VC sudan=SD
suriname=SR sweden=SE switzerland=CH syria=SY taiwan=TW tajikistan=TJ tanzania=TZ thailand=TH the-gambia=GM
timor-leste=TL togo=TG tonga=TO trinidad-and-tobago=TT tunisia=TN turkey=TR turkmenistan=TM
turks-and-caicos-islands=TC tuvalu=TV uganda=UG ukraine=UA united-arab-emirates=AE uruguay=UY usa=US
uzbekistan=UZ vanuatu=VU venezuela=VE vietnam=VN wallis-and-futuna=WF western-sahara=EH yemen=YE zambia=ZM
zimbabwe=ZW
"""
FCDO_SLUG_ISO2: dict[str, tuple[str, ...]] = {
    k: tuple(v.split(",")) for k, _, v in (w.partition("=") for w in _FCDO_SLUGS.split())}


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def parse_time(value) -> float | None:
    """ISO 8601 with Z, an offset or none (UTC) -> epoch seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", s)
        if not m:
            return None
        try:
            t = dt.datetime.fromisoformat(m.group(1))
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return float(calendar.timegm(t.utctimetuple()))


def html_text(fragment: str | None) -> str:
    """Plain text from a small HTML fragment (tags dropped, entities decoded, spaces collapsed)."""
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", ". ", fragment or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"<[^>]*$", " ", s)                       # a tag cut off at the end of a clipped fragment
    s = html.unescape(s).replace("\u00a0", " ").replace("\u202f", " ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"([.!?])(\s*\.)+", r"\1", s)
    return s.strip(" .")


def clip(text: str | None, n: int) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip(" ,;:") + "…"


# ---------------------------------------------------------------------------------------------
# FCDO
# ---------------------------------------------------------------------------------------------
def parse_fcdo_index(payload, min_countries: int = 100) -> list[dict]:
    """One entry per FCDO country page: slug, name, public_updated_at (epoch), change text, web URL."""
    children = ((payload or {}).get("links") or {}).get("children") if isinstance(payload, dict) else None
    if not isinstance(children, list):
        raise ValueError("FCDO index has no country list")
    out = []
    for ch in children:
        if not isinstance(ch, dict):
            continue
        det = ch.get("details") or {}
        country = det.get("country") or {}
        slug = str(country.get("slug") or (ch.get("base_path") or "").rsplit("/", 1)[-1]).strip()
        if not re.fullmatch(r"[a-z0-9-]{2,80}", slug):
            continue
        out.append({"slug": slug, "name": clip(country.get("name") or ch.get("title") or slug, 80),
                    "updated": parse_time(ch.get("public_updated_at")),
                    "change": clip(html_text(det.get("change_description")), 400),
                    "url": f"{FCDO_SITE}/{slug}"})
    if len(out) < min_countries:
        raise ValueError(f"FCDO index looks incomplete ({len(out)} countries)")
    return out


def _names(name: str) -> list[str]:
    """Ways the advice text names the country: 'Myanmar (Burma)' -> Myanmar (Burma), Myanmar, Burma."""
    base = (name or "").strip()
    out = {base}
    m = re.match(r"^(.*?)\s*\((.*?)\)\s*$", base)
    if m:
        out.update({m.group(1).strip(), m.group(2).strip()})
    out.update({n[4:] for n in list(out) if n.lower().startswith("the ")})
    return sorted((n for n in out if len(n) >= 3), key=len, reverse=True)


_LEAD = r"advises against all (but essential )?travel (?:to|in|within) "
_REMAINDER = (r"(?:the rest of|the remainder of|the remaining (?:areas|parts|regions) of|all other (?:areas|parts|regions|"
              r"provinces|states) of|all remaining (?:areas|parts|regions) of)")
_WHOLE = r"(?:the whole of |all of |the entire )?"
# The country name must end there: not "Chad's borders", "the Djibouti-Eritrea border" or "Kenya border areas".
_NAME_END = (r"(?![\w'’‘\-–])(?!\s+(?:border|borders|frontier|region|regions|province|provinces|state|states|coast|"
             r"side|city|cities|area|areas)\b)")


def advice_sentences(text: str) -> list[str]:
    """Sentences that state where FCDO advises against travel (in page order, deduplicated)."""
    out, seen = [], set()
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\s+(?=Areas where FCDO advises)", text or "") if p.strip()]
    for i, s in enumerate(parts):
        if not re.search(r"advises against all (but essential )?travel", s, re.I):
            continue
        if re.search(r":\.?$", s):                    # "... to the following regions:" then a list of places
            items = []
            for nxt in parts[i + 1:i + 7]:
                if len(nxt) > 80 or re.search(r"advises", nxt, re.I):
                    break
                items.append(nxt.rstrip(". "))
            s = re.sub(r":\.?$", ":", s) + (" " + ", ".join(items) + "." if items else "")
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def text_scope(text: str, name: str) -> dict:
    """What the advice sentences say about the whole country.

    Returns {"aat_whole": bool, "aabe_whole": bool, "except": bool, "remainder_aat": bool, "remainder_aabe": bool}."""
    res = {"aat_whole": False, "aabe_whole": False, "except": False, "remainder_aat": False, "remainder_aabe": False}
    for s in advice_sentences(text):
        low = s.lower()
        for n in _names(name):
            nl = re.escape(n.lower())
            for the in ("", "the "):
                whole = re.search(_LEAD + _WHOLE + the + nl + _NAME_END, low)
                if whole:
                    key = "aabe_whole" if whole.group(1) else "aat_whole"
                    res[key] = True
                    if re.search(r"\bexcept|excluding|with the exception|other than|apart from", low[whole.end():]):
                        res["except"] = True
                rest = re.search(_LEAD + _REMAINDER + r" " + the + nl + _NAME_END, low)
                if rest:
                    res["remainder_aabe" if rest.group(1) else "remainder_aat"] = True
    return res


def fcdo_level(statuses: list[str], scope: dict | None = None) -> tuple[int, str]:
    """(level 1-4, basis) from FCDO status codes and the text scope. Basis: 'codes' or 'text'."""
    codes = [s for s in statuses or [] if s in FCDO_CODES]
    level = max((_CODE_LEVEL[c] for c in codes), default=1)
    basis = "codes"
    sc = scope or {}
    text_level = 1
    if sc.get("aat_whole") or sc.get("remainder_aat"):
        text_level = 4
    elif sc.get("aabe_whole") or sc.get("remainder_aabe"):
        text_level = 3
    if codes and text_level > level:          # the text only refines countries that already have a status
        level, basis = text_level, "text"
    return level, basis


def fcdo_label(level: int, statuses: list[str], scope: dict | None = None) -> str:
    """Plain wording of the FCDO position, close to FCDO's own phrases."""
    st = set(statuses or [])
    sc = scope or {}
    if level >= 4:
        if AAT_WHOLE in st and not (AABE_PARTS in st or sc.get("except")):
            return "Against all travel"
        return "Against all travel to most of the country"
    if level == 3:
        if AAT_PARTS in st or AAT_WHOLE in st:
            return "Against all but essential travel, and all travel to parts"
        return "Against all but essential travel"
    if level == 2:
        if AAT_PARTS in st and AABE_PARTS in st:
            return "Against all travel and all but essential travel to parts"
        if AAT_PARTS in st:
            return "Against all travel to parts"
        return "Against all but essential travel to parts"
    return "No advice against travel"


def parse_fcdo_country(payload, slug: str) -> dict:
    """The parts of an FCDO country page we keep (statuses, level, key sentences, latest change)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("details"), dict):
        raise ValueError("FCDO country page has no details")
    det = payload["details"]
    statuses = [s for s in det.get("alert_status") or [] if isinstance(s, str) and s in FCDO_CODES]
    name = clip((det.get("country") or {}).get("name") or payload.get("title") or slug, 80)
    warn = ""
    for p in det.get("parts") or []:
        if isinstance(p, dict) and p.get("slug") == "warnings-and-insurance":
            body = p.get("body")
            if isinstance(body, list):
                body = " ".join(b.get("content", "") for b in body if isinstance(b, dict))
            warn = html_text(body if isinstance(body, str) else "")
            break
    scope = text_scope(warn, name)
    level, basis = fcdo_level(statuses, scope)
    sentences = [clip(s, 300) for s in advice_sentences(warn)][:6]
    history = []
    for h in det.get("change_history") or []:
        if isinstance(h, dict):
            t = parse_time(h.get("public_timestamp"))
            note = clip(html_text(h.get("note")), 400)
            if t and note:
                history.append({"t": t, "note": note})
    return {"pv": PARSER_VERSION, "slug": slug, "name": name, "statuses": statuses, "level": level, "basis": basis, "scope": scope,
            "label": fcdo_label(level, statuses, scope), "sentences": sentences,
            "change": clip(html_text(det.get("change_description")), 400),
            "updated": parse_time(payload.get("public_updated_at")) or parse_time(det.get("updated_at")),
            "reviewed": parse_time(det.get("reviewed_at")), "history": history[:20],
            "url": f"{FCDO_SITE}/{slug}"}


def is_advice_note(note: str | None) -> bool:
    """FCDO change notes that change where it advises against travel ("FCDO now advises against ...")."""
    return bool(re.search(r"\b(now|no longer) advises? against\b|\bno longer advises\b|"
                          r"\bremoved (our|the|its) advice against\b", note or "", re.I))


# ---------------------------------------------------------------------------------------------
# US State Department
# ---------------------------------------------------------------------------------------------
def parse_us(payload, min_items: int = 100) -> list[dict]:
    """One entry per advisory: level, wording, ISO3 from the link, FIPS codes, times, summary text."""
    if not isinstance(payload, list):
        raise ValueError("US travel advisories response is not a list")
    out = []
    for it in payload:
        if not isinstance(it, dict):
            continue
        title = clip(it.get("Title"), 160)
        m = re.search(r"Level\s*([1-4])\s*:\s*([^|]+)$", title)
        if not m:
            continue
        level = int(m.group(1))
        link = str(it.get("Link") or "")
        if not link.startswith("https://"):
            link = ""
        iso3 = None
        lm = re.search(r"destination\.([a-z]{3})\.html", link)
        if lm:
            iso3 = lm.group(1).upper()
        cats = it.get("Category")
        if isinstance(cats, str):
            cats = re.findall(r"[A-Z]{2}", cats)
        fips = [c for c in cats or [] if isinstance(c, str) and re.fullmatch(r"[A-Z0-9]{2}", c)]
        place = re.sub(r"\s*(Travel Advisory)?\s*-\s*Level.*$", "", title).strip()
        out.append({"level": level, "words": US_LEVEL_WORDS[level], "title": title, "place": clip(place, 80),
                    "iso3": iso3, "fips": fips[:4], "url": link,
                    "updated": parse_time(it.get("Updated")) or parse_time(it.get("Published")),
                    "summary": clip(html_text(it.get("Summary")), 600)})
    if len(out) < min_items:
        raise ValueError(f"US advisories list looks incomplete ({len(out)} items)")
    return out


# ---------------------------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------------------------
def change_severity(new_level: int | None, old_level: int | None, kind: str, note: str | None = None) -> float:
    """Level changes by the new level: 2 -> 4, 3 -> 5, 4 -> 6 (+0.5 for a jump of 2+), 3 for a lowering.
    Areas updates (same level): 4 when FCDO "now advises against" more, 3 when it relaxes, +0.5 at level 3 or 4."""
    if kind == "areas" or new_level is None:
        tighter = bool(re.search(r"\bnow advises? against\b", note or "", re.I)) and "no longer" not in (note or "").lower()
        return (4.0 if tighter else 3.0) + (0.5 if (new_level or 0) >= 3 else 0.0)
    if old_level is not None and new_level < old_level:
        return 3.0
    sev = {1: 3.0, 2: 4.0, 3: 5.0, 4: 6.0}.get(new_level, 3.0)
    if old_level is not None and new_level - old_level >= 2:
        sev += 0.5
    return min(7.0, sev)


_PHRASE = {   # fcdo_label() -> phrase with the country name
    "Against all travel": "against all travel to {n}",
    "Against all travel to most of the country": "against all travel to most of {n}",
    "Against all but essential travel, and all travel to parts": "against all but essential travel to {n}, and all travel to parts of it",
    "Against all but essential travel": "against all but essential travel to {n}",
    "Against all travel and all but essential travel to parts": "against all travel and all but essential travel to parts of {n}",
    "Against all travel to parts": "against all travel to parts of {n}",
    "Against all but essential travel to parts": "against all but essential travel to parts of {n}",
}


def fcdo_change_text(name: str, old: dict | None, new: dict) -> str:
    """'FCDO now advises against all travel to most of Sudan (previously against all travel to parts)'."""
    lvl = new.get("level") or 1
    if lvl <= 1:
        head = f"FCDO no longer advises against travel to {name}"
    else:
        lab = fcdo_label(lvl, new.get("statuses") or [], new.get("scope"))
        head = "FCDO now advises " + _PHRASE.get(lab, lab[0].lower() + lab[1:] + " ({n})").format(n=name)
    if old is not None:
        old_lab = fcdo_label(old.get("level") or 1, old.get("statuses") or [], old.get("scope"))
        head += f" (previously {old_lab[0].lower() + old_lab[1:]})"
    return head


def first_sentence(text: str | None, n: int = 180) -> str:
    """The first sentence of a note ("FCDO now advises against ... Region."), clipped to ``n`` characters."""
    t = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    return clip(m.group(1) if m else t, n)


def areas_change_text(name: str, note: str | None) -> str:
    """'Ethiopia: FCDO now advises against all but essential travel to East Borena and Arsi in Oromia Region.'"""
    return f"{name}: {first_sentence(note) or 'FCDO changed the areas it advises against'}"


def us_change_text(name: str, old_level: int | None, new_level: int) -> str:
    verb = "raises" if old_level is not None and new_level > old_level else "lowers"
    if old_level is None:
        return f"US State Department: {name} at Level {new_level}, {US_LEVEL_WORDS[new_level].lower()}"
    return (f"US State Department {verb} {name} to Level {new_level}: {US_LEVEL_WORDS[new_level].lower()} "
            f"(previously Level {old_level})")


def detect_fcdo_change(old: dict | None, new: dict) -> str | None:
    """'level' when the level or the status codes changed, 'areas' when FCDO's own change note says it
    changed where it advises against travel, None otherwise (and always None on first sight)."""
    if old is None:
        return None
    if (old.get("level"), sorted(old.get("statuses") or [])) != (new.get("level"), sorted(new.get("statuses") or [])):
        return "level"
    if new.get("updated") and new.get("updated") != old.get("updated") and is_advice_note(new.get("change")):
        return "areas"
    return None
