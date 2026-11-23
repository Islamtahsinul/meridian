"""Listed companies at their headquarters, from Wikidata (CC0): items with a stock exchange (P414)
and a headquarters location (P159) with coordinates. No people are mapped."""

from __future__ import annotations

import re
import time

from . import countries
from .common import (Collection, batched, compact_num, fc, fnum, http_url, log_weight, point_feature, sparql_batches,
                     sparql_csv, text, values_clause, wd_id, wkt_point, year_of)

SOURCE_ID = "infra.wikidata_companies"
PAGE = "https://www.wikidata.org/wiki/Property:P414"
BATCH = 1000
MIN_LISTED = 5000
MIN_COMPANIES = 3000
LABEL_LANGS = ("en", "mul", "fr", "de", "es", "it", "pt", "nl", "sv", "ja", "zh", "ko", "ru", "pl", "tr", "id", "th",
               "ms", "vi", "he", "ar", "hi", "cs", "fi", "da", "no", "nb")

Q_LIST = "SELECT DISTINCT ?c WHERE { ?c wdt:P414 [] }"
Q_EXCHANGES = """SELECT ?ex ?l ?mic ?coord ?hqcoord ?cc WHERE {
  { SELECT DISTINCT ?ex WHERE { ?c wdt:P414 ?ex } }
  OPTIONAL { ?ex rdfs:label ?l FILTER(LANG(?l) = "en") }
  OPTIONAL { ?ex wdt:P7534 ?mic }
  OPTIONAL { ?ex wdt:P625 ?coord }
  OPTIONAL { ?ex wdt:P159/wdt:P625 ?hqcoord }
  OPTIONAL { ?ex wdt:P17/wdt:P297 ?cc }
}"""
Q_LISTINGS = """SELECT ?c ?ex ?ticker ?rank WHERE {
  ?c p:P414 ?st . ?st ps:P414 ?ex ; wikibase:rank ?rank . OPTIONAL { ?st pq:P249 ?ticker }
}"""
Q_HQ = """SELECT ?c ?hq ?coord ?qcoord ?rank WHERE {
  %s ?c p:P159 ?st . ?st ps:P159 ?hq ; wikibase:rank ?rank .
  OPTIONAL { ?st pq:P625 ?qcoord } OPTIONAL { ?hq wdt:P625 ?coord }
}"""
Q_FACTS = """SELECT ?c ?l ?cc ?web ?isin ?inc ?wp WHERE {
  %s
  OPTIONAL { ?c rdfs:label ?l FILTER(LANG(?l) = "en") }
  OPTIONAL { ?c wdt:P17/wdt:P297 ?cc }
  OPTIONAL { ?c wdt:P856 ?web }
  OPTIONAL { ?c wdt:P946 ?isin }
  OPTIONAL { ?c wdt:P571 ?inc }
  OPTIONAL { ?wp schema:about ?c ; schema:isPartOf <https://en.wikipedia.org/> }
}"""
Q_INDUSTRY = """SELECT ?c ?l WHERE { %s ?c wdt:P452 ?ind . ?ind rdfs:label ?l FILTER(LANG(?l) = "en") }"""
Q_QUANTITY = """SELECT ?c ?amt ?u ?t ?rank WHERE {
  %s ?c p:%s ?st . ?st psv:%s ?v ; wikibase:rank ?rank . ?v wikibase:quantityAmount ?amt .
  OPTIONAL { ?v wikibase:quantityUnit ?u } OPTIONAL { ?st pq:P585 ?t }
}"""
Q_UNITS = "SELECT ?u ?code WHERE { %s ?u wdt:P498 ?code }"
QUANTITIES = (("emp", "P1128"), ("rev", "P2139"), ("cap", "P2226"))
Q_ITEM_LABELS = """SELECT ?x ?l ?lang WHERE { %s ?x rdfs:label ?l . BIND(LANG(?l) AS ?lang) FILTER(?lang IN (%s)) }"""

DEPRECATED = "http://wikiba.se/ontology#DeprecatedRank"
PREFERRED = "http://wikiba.se/ontology#PreferredRank"

# MIC -> suffix used by quote providers that want Yahoo-style symbols (Finnhub)
FINNHUB_SUFFIX = {
    "XLON": ".L", "XTKS": ".T", "XHKG": ".HK", "XTSE": ".TO", "XETR": ".DE", "XFRA": ".F", "XPAR": ".PA",
    "XAMS": ".AS", "XBRU": ".BR", "XLIS": ".LS", "XMIL": ".MI", "XMAD": ".MC", "XSWX": ".SW", "XSTO": ".ST",
    "XHEL": ".HE", "XCSE": ".CO", "XOSL": ".OL", "XASX": ".AX", "XKRX": ".KS", "XKOS": ".KQ", "XSHG": ".SS",
    "XSHE": ".SZ", "XBOM": ".BO", "XNSE": ".NS", "BVMF": ".SA", "XMEX": ".MX", "XJSE": ".JO", "XTAI": ".TW",
    "XSES": ".SI", "XBKK": ".BK", "XIDX": ".JK", "XKLS": ".KL", "XTAE": ".TA", "XIST": ".IS", "XWBO": ".VI",
    "XWAR": ".WA", "XPRA": ".PR", "XNZE": ".NZ", "XSAU": ".SR", "XDUB": ".IR", "XATH": ".AT", "XBUD": ".BD",
}
US_MICS = {"XNYS", "XNAS", "XASE", "ARCX", "BATS", "OTCM", "XNGS", "XNMS", "XNCM", "XOTC", "IEXG", "PINX"}


def _rank_score(rank: str | None) -> int:
    if rank == DEPRECATED:
        return -1
    return 1 if rank == PREFERRED else 0


def parse_exchanges(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        q = wd_id(r.get("ex"))
        if not q:
            continue
        ex = out.setdefault(q, {"qid": q, "name": None, "mic": None, "lat": None, "lon": None, "country": None})
        if not ex["name"] and r.get("l"):
            ex["name"] = text(r["l"], 80)
        mic = (r.get("mic") or "").strip().upper()
        if re.fullmatch(r"[A-Z0-9]{4}", mic) and (not ex["mic"] or (mic.startswith("X") and not ex["mic"].startswith("X"))):
            ex["mic"] = mic
        pos = wkt_point(r.get("coord")) or wkt_point(r.get("hqcoord"))
        if pos and ex["lat"] is None:
            ex["lat"], ex["lon"] = round(pos[0], 4), round(pos[1], 4)
        if not ex["country"] and r.get("cc"):
            ex["country"] = text(r["cc"], 2)
    return {q: e for q, e in out.items() if e["name"] and (e["mic"] or e["lat"] is not None)}


def parse_listings(rows: list[dict], exchanges: dict[str, dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        c, ex = wd_id(r.get("c")), wd_id(r.get("ex"))
        if not c or not ex or r.get("rank") == DEPRECATED:
            continue
        e = exchanges.get(ex)
        if e is None:
            continue
        ticker = (r.get("ticker") or "").strip().upper()[:16] or None
        if ticker and not re.fullmatch(r"[A-Z0-9.\-^/&]{1,16}", ticker):
            ticker = None
        lst = out.setdefault(c, [])
        key = (ex, ticker)
        if any((x["exchange_qid"], x["ticker"]) == key for x in lst):
            continue
        lst.append({"exchange_qid": ex, "exchange": e["name"], "mic": e.get("mic"), "ticker": ticker,
                    "preferred": r.get("rank") == PREFERRED})
    for lst in out.values():
        lst.sort(key=lambda x: (not x["preferred"], x["ticker"] is None, x["mic"] not in US_MICS))
        del lst[8:]
    return out


def pick_hq(rows: list[dict]) -> dict[str, dict]:
    best: dict[str, tuple[tuple, dict]] = {}
    for r in rows:
        c = wd_id(r.get("c"))
        if not c or r.get("rank") == DEPRECATED:
            continue
        q = wkt_point(r.get("qcoord"))
        h = wkt_point(r.get("coord"))
        pos = q or h
        if not pos:
            continue
        score = (_rank_score(r.get("rank")), 1 if q else 0)
        if c not in best or score > best[c][0]:
            best[c] = (score, {"lat": pos[0], "lon": pos[1], "hq_qid": wd_id(r.get("hq")), "precise": bool(q)})
    return {c: v[1] for c, v in best.items()}


def pick_money(rows: list[dict]) -> dict[str, dict]:
    """Latest employees / revenue / market cap per company (preferred rank, then newest date)."""
    best: dict[tuple[str, str], tuple[tuple, dict]] = {}
    for r in rows:
        c, k = wd_id(r.get("c")), r.get("k")
        amt = fnum(r.get("amt"))
        if not c or k not in ("emp", "rev", "cap") or amt is None or amt <= 0 or r.get("rank") == DEPRECATED:
            continue
        cur = (r.get("cur") or "").strip().upper() or None
        if k != "emp" and not (cur and re.fullmatch(r"[A-Z]{3}", cur)):
            continue
        y = year_of(r.get("t"))
        when = str(r.get("t") or "")
        score = (_rank_score(r.get("rank")), when, amt)
        if (c, k) not in best or score > best[(c, k)][0]:
            best[(c, k)] = (score, {"amount": amt, "currency": cur if k != "emp" else None, "year": y})
    out: dict[str, dict] = {}
    for (c, k), (_, v) in best.items():
        out.setdefault(c, {})[k] = v
    return out


def to_usd(amount: float | None, currency: str | None, eur_rates: dict[str, float]) -> float | None:
    """Convert with ECB reference rates (EUR base). None when the currency is unknown."""
    if amount is None or not currency:
        return None
    if currency == "USD":
        return amount
    usd = eur_rates.get("USD")
    if not usd:
        return None
    if currency == "EUR":
        return amount * usd
    r = eur_rates.get(currency)
    return amount / r * usd if r else None


def _url_rank(url: str) -> tuple[int, int]:
    """Shorter paths first: https://www.apple.com/ beats https://www.apple.com/ke/."""
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    return (len(parts.path.strip("/")) + len(parts.query), len(url))


def pick_labels(rows: list[dict], var: str = "x", langs: tuple[str, ...] = LABEL_LANGS) -> dict[str, str]:
    """One label per item, by language preference (rows carry ?lang)."""
    rank = {lang: i for i, lang in enumerate(langs)}
    best: dict[str, tuple[int, str]] = {}
    for r in rows:
        q, lab = wd_id(r.get(var)), text(r.get("l"), 120)
        if not q or not lab:
            continue
        score = rank.get(r.get("lang") or "", 999)
        if q not in best or score < best[q][0]:
            best[q] = (score, lab)
    return {q: v[1] for q, v in best.items()}


def _labels(ds, qids: list[str], var: str = "x") -> dict[str, str]:
    langs = ", ".join(f'"{x}"' for x in LABEL_LANGS)
    rows, _ = sparql_batches(ds.net, qids, lambda ch: Q_ITEM_LABELS % (values_clause(var, ch), langs), 1500,
                             log=ds.log, label="companies: labels")
    return pick_labels(rows, var)


def build(ds, eur_rates: dict[str, float] | None = None) -> None:
    eur_rates = eur_rates or {}
    all_ids = sorted({q for q in (wd_id(r.get("c")) for r in sparql_csv(ds.net, Q_LIST)) if q})
    if len(all_ids) < MIN_LISTED:
        raise RuntimeError(f"Wikidata returned too few listed companies ({len(all_ids)})")
    exchanges = parse_exchanges(sparql_csv(ds.net, Q_EXCHANGES))
    listings = parse_listings(sparql_csv(ds.net, Q_LISTINGS), exchanges)
    hq_rows, failed = sparql_batches(ds.net, all_ids, lambda ch: Q_HQ % values_clause("c", ch), BATCH,
                                     log=ds.log, label="companies: headquarters")
    if failed > len(all_ids) // 5:
        raise RuntimeError(f"Wikidata headquarters queries failed for {failed} companies")
    hqs = pick_hq(hq_rows)
    ids = sorted(hqs)
    facts: dict[str, dict] = {}
    industries: dict[str, list[str]] = {}
    rows, _ = sparql_batches(ds.net, ids, lambda ch: Q_FACTS % values_clause("c", ch), 800, log=ds.log,
                             label="companies: facts")
    for r in rows:
        c = wd_id(r.get("c"))
        if not c:
            continue
        f = facts.setdefault(c, {"isins": []})
        for k in ("l", "cc", "inc", "wp"):
            if r.get(k) and not f.get(k):
                f[k] = r[k]
        web = http_url(r.get("web"))
        if web and (not f.get("web") or _url_rank(web) < _url_rank(f["web"])):   # prefer the main site
            f["web"] = web
        isin = (r.get("isin") or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin) and isin not in f["isins"] and len(f["isins"]) < 4:
            f["isins"].append(isin)
    rows, _ = sparql_batches(ds.net, ids, lambda ch: Q_INDUSTRY % values_clause("c", ch), BATCH, log=ds.log,
                             label="companies: industries")
    for r in rows:
        c = wd_id(r.get("c"))
        lab = text(r.get("l"), 60)
        if c and lab and lab not in industries.setdefault(c, []) and len(industries[c]) < 4:
            industries[c].append(lab)
    money_rows: list[dict] = []
    units: set[str] = set()
    for key, prop in QUANTITIES:
        rows, _ = sparql_batches(ds.net, ids, lambda ch, p=prop: Q_QUANTITY % (values_clause("c", ch), p, p), 800,
                                 log=ds.log, label=f"companies: {prop}")
        for r in rows:
            r["k"] = key
            u = wd_id(r.get("u"))
            if u and u != "Q199":
                units.add(u)
        money_rows.extend(rows)
    codes: dict[str, str] = {}
    unit_rows, _ = sparql_batches(ds.net, sorted(units), lambda ch: Q_UNITS % values_clause("u", ch), 1500,
                                  log=ds.log, label="companies: currencies")
    for r in unit_rows:
        u, code = wd_id(r.get("u")), (r.get("code") or "").strip().upper()
        if u and re.fullmatch(r"[A-Z]{3}", code):
            codes.setdefault(u, code)
    for r in money_rows:
        r["cur"] = codes.get(wd_id(r.get("u")) or "", "")
    money = pick_money(money_rows)
    hq_labels = _labels(ds, sorted({h["hq_qid"] for h in hqs.values() if h.get("hq_qid")}))
    missing = [c for c in ids if not (facts.get(c) or {}).get("l")]
    fallback = _labels(ds, missing) if missing else {}
    companies = []
    for c in ids:
        h = hqs[c]
        f = facts.get(c) or {}
        mo = money.get(c) or {}
        name = text(f.get("l"), 120) or fallback.get(c)
        if not name:
            continue
        cc = text(f.get("cc"), 2)
        cap = mo.get("cap")
        rev = mo.get("rev")
        emp = mo.get("emp")
        cap_usd = to_usd(cap["amount"], cap["currency"], eur_rates) if cap else None
        lst = listings.get(c, [])
        companies.append({
            "id": f"wikidata:{c}", "qid": c, "name": name, "lat": h["lat"], "lon": h["lon"], "hq_precise": h["precise"],
            "hq": hq_labels.get(h.get("hq_qid") or ""), "country": cc, "country_name": countries.name(cc),
            "listings": lst, "industries": industries.get(c, []),
            "employees": int(emp["amount"]) if emp else None, "employees_year": emp["year"] if emp else None,
            "revenue": {"amount": rev["amount"], "currency": rev["currency"], "year": rev["year"]} if rev else None,
            "market_cap": {"amount": cap["amount"], "currency": cap["currency"], "year": cap["year"],
                           "usd": round(cap_usd) if cap_usd else None} if cap else None,
            "website": http_url(f.get("web")), "isins": f.get("isins") or [], "founded": year_of(f.get("inc")),
            "wikipedia": f.get("wp") if str(f.get("wp") or "").startswith("https://en.wikipedia.org/") else None,
            "wikidata": f"https://www.wikidata.org/wiki/{c}",
        })
    if len(companies) < MIN_COMPANIES:
        raise RuntimeError(f"Only {len(companies)} companies with headquarters coordinates")
    companies.sort(key=lambda x: (x.get("employees") or 0))
    ds.write_json("companies.geojson", geojson(companies, eur_rates))
    ds.write_json("companies-details.json", {"built_at": time.time(), "items": companies,
                                             "exchanges": exchanges})
    ds.set_rows(len(companies))
    ds.note(f"{len(companies):,} listed companies with headquarters coordinates "
            f"({sum(1 for x in companies if x.get('market_cap')):,} with a market cap)")


def importance(co: dict, eur_rates: dict[str, float] | None = None) -> float:
    """0..1 from employees, market value, revenue and an English article (most companies lack figures)."""
    cap_usd = (co.get("market_cap") or {}).get("usd")
    rev = co.get("revenue") or {}
    rev_usd = to_usd(rev.get("amount"), rev.get("currency"), eur_rates or {}) if rev else None
    has_ticker = any(x.get("ticker") for x in co.get("listings") or [])
    return round(max(log_weight(co.get("employees"), 100, 1_000_000), log_weight(cap_usd, 1e8, 3e12),
                     log_weight(rev_usd, 1e8, 6e11), 0.35 if co.get("wikipedia") else 0.0,
                     0.15 if has_ticker else 0.05), 2)


def geojson(companies: list[dict], eur_rates: dict[str, float] | None = None) -> dict:
    feats = []
    for co in companies:
        cap_usd = (co.get("market_cap") or {}).get("usd")
        primary = next((x for x in co.get("listings") or [] if x.get("ticker")), None)
        feats.append(point_feature(co["lon"], co["lat"], {
            "id": co["id"], "n": co["name"][:60], "e": co.get("employees"),
            "m": round(cap_usd / 1e9, 2) if cap_usd else None, "t": primary["ticker"] if primary else None,
            "w": importance(co, eur_rates)}))
    return fc(feats)


def subtitle(co: dict) -> str:
    lst = next((x for x in co.get("listings") or [] if x.get("ticker")), None)
    lead = f"{lst['ticker']} on {lst['exchange']}" if lst else "Listed company"
    place = ", ".join(x for x in (co.get("hq"), co.get("country_name")) if x)
    return ", ".join(x for x in (lead, place) if x)


def collection(items: list[dict], built_at: float | None, exchanges: dict | None = None) -> Collection:
    rows = []
    for co in items:
        codes = [co["id"], co["qid"]] + list(co.get("isins") or [])
        for x in co.get("listings") or []:
            if x.get("ticker"):
                codes.append(x["ticker"])
                if x.get("mic"):
                    codes.append(f"{x['ticker']}:{x['mic']}")
        w = importance(co)
        rows.append({"id": co["id"], "title": co["name"], "subtitle": subtitle(co), "lat": co["lat"], "lon": co["lon"],
                     "weight": w, "country": co.get("country"), "codes": codes,
                     "text": " ".join([*(co.get("industries") or []), co.get("hq") or "",
                                       *(x["exchange"] for x in co.get("listings") or [])]),
                     "detail": co})
    return Collection("company", "infra.companies", rows, cell_deg=1.0, zoom=12, built_at=built_at,
                      meta={"exchanges": exchanges or {}})


def quote_symbol(listing: dict) -> str | None:
    """Our provider-neutral quote symbol for a listing: "TICKER:MIC" (or just the ticker)."""
    t = listing.get("ticker")
    if not t:
        return None
    return f"{t}:{listing['mic']}" if listing.get("mic") else t
