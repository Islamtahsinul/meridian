"""Markets: prices from keyless public sources plus optional keyed equity quotes.

Keyless: gold-api.com (live precious metals, copper, bitcoin, ether), the ECB euro reference rates
(daily FX), the World Bank Pink Sheet (monthly commodity averages) and FRED daily series (when FRED
answers). Optional keys: FINNHUB_API_KEY and TWELVEDATA_API_KEY for delayed share prices.
Stooq, the usual keyless share-price CSV, removed its quote endpoint and now puts a browser check in
front of every page, so it is not used.
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .common import fnum, text

GOLDAPI = "https://api.gold-api.com/price/{sym}"
GOLDAPI_HOST = "api.gold-api.com"
ECB_90D = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_DAILY = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_PAGE = "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html"
WB_PAGE = "https://www.worldbank.org/en/research/commodity-markets"
WB_FALLBACK = ("https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/"
               "CMO-Historical-Data-Monthly.xlsx")
WB_LINK_RE = re.compile(r"https://thedocs\.worldbank\.org/en/doc/[0-9a-f]{16,64}-\d{6,12}/related/"
                        r"CMO-Historical-Data-Monthly\.xlsx")
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_PAGE = "https://fred.stlouisfed.org/"
FINNHUB = "https://finnhub.io/api/v1/quote"
TWELVEDATA = "https://api.twelvedata.com/quote"

SRC_GOLD = "infra.goldapi"
SRC_ECB = "infra.ecb"
SRC_WB = "infra.worldbank"
SRC_FRED = "infra.fred"
SRC_FINNHUB = "infra.finnhub"
SRC_TWELVE = "infra.twelvedata"

GOLD_SYMBOLS = {
    "XAU": ("Gold", "USD", "per troy ounce"), "XAG": ("Silver", "USD", "per troy ounce"),
    "XPT": ("Platinum", "USD", "per troy ounce"), "XPD": ("Palladium", "USD", "per troy ounce"),
    "HG": ("Copper", "USD", "per pound"), "BTC": ("Bitcoin", "USD", ""), "ETH": ("Ether", "USD", ""),
}
ECB_CURRENCIES = ("USD", "JPY", "BGN", "CZK", "DKK", "GBP", "HUF", "PLN", "RON", "SEK", "CHF", "ISK", "NOK", "TRY",
                  "AUD", "BRL", "CAD", "CNY", "HKD", "IDR", "ILS", "INR", "KRW", "MXN", "MYR", "NZD", "PHP", "SGD",
                  "THB", "ZAR", "EUR")

# World Bank Pink Sheet columns we publish: code -> (column title prefix, display name)
WB_SERIES = {
    "BRENT": ("Crude oil, Brent", "Brent crude"), "WTI": ("Crude oil, WTI", "WTI crude"),
    "DUBAI": ("Crude oil, Dubai", "Dubai crude"), "NGAS_US": ("Natural gas, US", "US natural gas (Henry Hub)"),
    "NGAS_EUR": ("Natural gas, Europe", "European natural gas (TTF)"),
    "LNG_JP": ("Liquefied natural gas, Japan", "Japan LNG"), "COAL_AUS": ("Coal, Australian", "Australian coal"),
    "WHEAT": ("Wheat, US HRW", "Wheat (US hard red winter)"), "MAIZE": ("Maize", "Maize"),
    "SOYBEANS": ("Soybeans", "Soybeans"), "RICE": ("Rice, Thai 5%", "Rice (Thai 5%)"),
    "SUGAR": ("Sugar, world", "Sugar"), "COFFEE": ("Coffee, Arabica", "Coffee (arabica)"), "COCOA": ("Cocoa", "Cocoa"),
    "ALUMINIUM": ("Aluminum", "Aluminium"), "COPPER": ("Copper", "Copper"), "IRON_ORE": ("Iron ore", "Iron ore"),
    "NICKEL": ("Nickel", "Nickel"), "ZINC": ("Zinc", "Zinc"), "UREA": ("Urea", "Urea"),
    "GOLD": ("Gold", "Gold"), "SILVER": ("Silver", "Silver"), "PLATINUM": ("Platinum", "Platinum"),
}
_WB_UNITS = {"$/bbl": "per barrel", "$/mmbtu": "per million BTU", "$/mt": "per tonne", "$/kg": "per kg",
             "$/dmtu": "per dry tonne unit", "$/troy oz": "per troy ounce"}

FRED_SERIES = {
    "DCOILBRENTEU": ("Brent crude", "USD", "per barrel"), "DCOILWTICO": ("WTI crude", "USD", "per barrel"),
    "DHHNGSP": ("US natural gas (Henry Hub)", "USD", "per million BTU"),
    "SP500": ("S&P 500", None, "index"), "DJIA": ("Dow Jones Industrial Average", None, "index"),
    "NASDAQCOM": ("Nasdaq Composite", None, "index"), "NIKKEI225": ("Nikkei 225", None, "index"),
}

# The drawer: groups of items; each item tries its sources in order.
_NY = {"lat": 40.7069, "lon": -74.0113, "zoom": 13, "label": "New York Stock Exchange"}
MARKET_GROUPS = [
    {"id": "indices", "title": "Stock markets", "items": [
        {"id": "spx", "name": "S&P 500", "sources": [("fred", "SP500"), ("equity", "SPY:ARCX")], "fly": _NY},
        {"id": "dji", "name": "Dow Jones", "sources": [("fred", "DJIA"), ("equity", "DIA:ARCX")], "fly": _NY},
        {"id": "ixic", "name": "Nasdaq Composite", "sources": [("fred", "NASDAQCOM"), ("equity", "QQQ:XNAS")],
         "fly": {"lat": 40.7568, "lon": -73.9862, "zoom": 14, "label": "Nasdaq MarketSite, Times Square"}},
        {"id": "n225", "name": "Nikkei 225", "sources": [("fred", "NIKKEI225"), ("equity", "EWJ:ARCX")],
         "fly": {"lat": 35.6826, "lon": 139.7797, "zoom": 13, "label": "Tokyo Stock Exchange"}},
        {"id": "ftse", "name": "UK shares", "sources": [("equity", "EWU:ARCX")],
         "fly": {"lat": 51.5152, "lon": -0.0990, "zoom": 13, "label": "London Stock Exchange"}},
        {"id": "dax", "name": "German shares", "sources": [("equity", "EWG:ARCX")],
         "fly": {"lat": 50.1153, "lon": 8.6775, "zoom": 13, "label": "Frankfurt Stock Exchange"}},
        {"id": "hsi", "name": "Hong Kong shares", "sources": [("equity", "EWH:ARCX")],
         "fly": {"lat": 22.2849, "lon": 114.1588, "zoom": 13, "label": "Hong Kong Exchanges"}},
        {"id": "china", "name": "China large caps", "sources": [("equity", "FXI:ARCX")],
         "fly": {"lat": 31.2390, "lon": 121.5026, "zoom": 13, "label": "Shanghai Stock Exchange"}},
        {"id": "india", "name": "Indian shares", "sources": [("equity", "INDA:BATS")],
         "fly": {"lat": 18.9298, "lon": 72.8337, "zoom": 13, "label": "Bombay Stock Exchange"}},
    ]},
    {"id": "metals", "title": "Precious metals and copper", "items": [
        {"id": "gold", "name": "Gold", "sources": [("goldapi", "XAU"), ("wb", "GOLD")]},
        {"id": "silver", "name": "Silver", "sources": [("goldapi", "XAG"), ("wb", "SILVER")]},
        {"id": "platinum", "name": "Platinum", "sources": [("goldapi", "XPT"), ("wb", "PLATINUM")]},
        {"id": "palladium", "name": "Palladium", "sources": [("goldapi", "XPD")]},
        {"id": "copper", "name": "Copper", "sources": [("goldapi", "HG"), ("wb", "COPPER")]},
    ]},
    {"id": "energy", "title": "Energy", "items": [
        {"id": "brent", "name": "Brent crude", "sources": [("fred", "DCOILBRENTEU"), ("wb", "BRENT")]},
        {"id": "wti", "name": "WTI crude", "sources": [("fred", "DCOILWTICO"), ("wb", "WTI")]},
        {"id": "henryhub", "name": "US natural gas", "sources": [("fred", "DHHNGSP"), ("wb", "NGAS_US")]},
        {"id": "ttf", "name": "European natural gas", "sources": [("wb", "NGAS_EUR")]},
        {"id": "lng", "name": "Japan LNG", "sources": [("wb", "LNG_JP")]},
        {"id": "coal", "name": "Australian coal", "sources": [("wb", "COAL_AUS")]},
    ]},
    {"id": "agri", "title": "Food and farm", "items": [
        {"id": "wheat", "name": "Wheat", "sources": [("wb", "WHEAT")]},
        {"id": "maize", "name": "Maize", "sources": [("wb", "MAIZE")]},
        {"id": "soybeans", "name": "Soybeans", "sources": [("wb", "SOYBEANS")]},
        {"id": "rice", "name": "Rice", "sources": [("wb", "RICE")]},
        {"id": "sugar", "name": "Sugar", "sources": [("wb", "SUGAR")]},
        {"id": "coffee", "name": "Coffee", "sources": [("wb", "COFFEE")]},
        {"id": "cocoa", "name": "Cocoa", "sources": [("wb", "COCOA")]},
        {"id": "urea", "name": "Urea fertiliser", "sources": [("wb", "UREA")]},
    ]},
    {"id": "base", "title": "Base metals and ore", "items": [
        {"id": "aluminium", "name": "Aluminium", "sources": [("wb", "ALUMINIUM")]},
        {"id": "ironore", "name": "Iron ore", "sources": [("wb", "IRON_ORE")]},
        {"id": "nickel", "name": "Nickel", "sources": [("wb", "NICKEL")]},
        {"id": "zinc", "name": "Zinc", "sources": [("wb", "ZINC")]},
    ]},
    {"id": "fx", "title": "Currencies", "items": [
        {"id": f"fx_{p.lower()}", "name": f"{p[:3]}/{p[3:]}", "sources": [("ecb", p)]}
        for p in ("EURUSD", "GBPUSD", "USDJPY", "USDCNY", "USDCHF", "AUDUSD", "USDCAD", "USDINR", "USDBRL",
                  "USDTRY", "USDKRW", "USDMXN", "USDZAR")
    ]},
    {"id": "crypto", "title": "Crypto", "items": [
        {"id": "btc", "name": "Bitcoin", "sources": [("goldapi", "BTC")]},
        {"id": "eth", "name": "Ether", "sources": [("goldapi", "ETH")]},
    ]},
]
ITEMS = {it["id"]: it for g in MARKET_GROUPS for it in g["items"]}
MIN_SERIES = 10
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.:^_=\-/&]{1,32}$")


# ---------------------------------------------------------------------------------------------
# Parsers (pure, unit-tested)
# ---------------------------------------------------------------------------------------------
def parse_goldapi(obj: dict, symbol: str) -> dict | None:
    if not isinstance(obj, dict):
        return None
    price = fnum(obj.get("price"))
    if price is None or price <= 0:
        return None
    name, cur, unit = GOLD_SYMBOLS.get(symbol, (obj.get("name") or symbol, "USD", ""))
    t = _iso_ts(obj.get("updatedAt"))
    return {"symbol": symbol, "name": name, "price": price, "currency": cur, "unit": unit, "time": t,
            "delay": "live", "source": "gold-api.com", "source_url": "https://gold-api.com/"}


def parse_ecb_xml(xml_text: str) -> list[tuple[str, dict[str, float]]]:
    """ECB eurofxref XML -> [(date, {currency: units per euro})], newest first."""
    root = ET.fromstring(xml_text)
    days = []
    for cube in root.iter():
        if cube.tag.endswith("Cube") and cube.get("time"):
            rates = {}
            for c in cube:
                cur, rate = c.get("currency"), fnum(c.get("rate"))
                if cur and rate:
                    rates[cur] = rate
            if rates:
                rates["EUR"] = 1.0
                days.append((cube.get("time"), rates))
    days.sort(key=lambda d: d[0], reverse=True)
    return days


def fx_from_ecb(days: list[tuple[str, dict[str, float]]], pair: str) -> dict | None:
    pair = pair.upper()
    if len(pair) != 6 or not days:
        return None
    base, quote = pair[:3], pair[3:]

    def rate(rates):
        if base in rates and quote in rates and rates[base]:
            return rates[quote] / rates[base]
        return None

    latest = rate(days[0][1])
    if latest is None:
        return None
    prev = rate(days[1][1]) if len(days) > 1 else None
    out = {"symbol": pair, "name": f"{base}/{quote}", "price": latest, "currency": quote, "unit": f"per {base}",
           "time": _date_ts(days[0][0]) + 14 * 3600 + 900, "as_of": days[0][0], "delay": "daily reference rate",
           "source": "European Central Bank", "source_url": ECB_PAGE}
    _add_change(out, prev)
    return out


def parse_fred_csv(csv_text: str) -> list[tuple[str, float]]:
    """fredgraph.csv -> [(date, value)] oldest first; "." (no data) rows skipped."""
    out = []
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row) < 2 or not re.match(r"^\d{4}-\d{2}-\d{2}$", row[0].strip()):
            continue
        v = fnum(row[1])
        if v is not None:
            out.append((row[0].strip(), v))
    return out


def fred_quote(series: str, rows: list[tuple[str, float]]) -> dict | None:
    if not rows:
        return None
    name, cur, unit = FRED_SERIES.get(series, (series, None, ""))
    date, value = rows[-1]
    out = {"symbol": f"fred:{series}", "name": name, "price": value, "currency": cur, "unit": unit,
           "time": _date_ts(date) + 21 * 3600, "as_of": date, "delay": "end of day", "source": "FRED, St. Louis Fed",
           "source_url": f"https://fred.stlouisfed.org/series/{series}"}
    _add_change(out, rows[-2][1] if len(rows) > 1 else None)
    return out


def parse_finnhub(obj: dict, symbol: str) -> dict | None:
    if not isinstance(obj, dict):
        return None
    c = fnum(obj.get("c"))
    t = obj.get("t")
    if not c or not t:
        return None
    out = {"symbol": symbol, "price": c, "change": fnum(obj.get("d")), "change_pct": fnum(obj.get("dp")),
           "open": fnum(obj.get("o")), "high": fnum(obj.get("h")), "low": fnum(obj.get("l")),
           "previous_close": fnum(obj.get("pc")), "time": int(t), "delay": "delayed", "source": "Finnhub",
           "source_url": "https://finnhub.io/", "currency": None}
    return out


def parse_twelvedata(obj: dict, symbol: str) -> dict | None:
    if not isinstance(obj, dict) or obj.get("status") == "error":
        return None
    close = fnum(obj.get("close"))
    if close is None:
        return None
    ts = obj.get("timestamp")
    return {"symbol": symbol, "name": text(obj.get("name"), 80), "price": close, "change": fnum(obj.get("change")),
            "change_pct": fnum(obj.get("percent_change")), "open": fnum(obj.get("open")), "high": fnum(obj.get("high")),
            "low": fnum(obj.get("low")), "previous_close": fnum(obj.get("previous_close")),
            "volume": fnum(obj.get("volume")), "currency": text(obj.get("currency"), 8),
            "exchange": text(obj.get("exchange"), 40), "time": int(ts) if isinstance(ts, (int, float)) else None,
            "market_open": obj.get("is_market_open") if isinstance(obj.get("is_market_open"), bool) else None,
            "delay": "delayed", "source": "Twelve Data", "source_url": "https://twelvedata.com/"}


def read_xlsx_sheet(path: Path, sheet_name: str, max_bytes: int = 40_000_000) -> list[list]:
    """Rows of one worksheet from an .xlsx file (zip of XML), using only the standard library."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.file_size > max_bytes:
                raise ValueError(f"{info.filename} is too large")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(ns + "si"):
                shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rid = None
        for s in wb.iter(ns + "sheet"):
            if s.get("name") == sheet_name:
                rid = s.get(rel_ns + "id")
        if rid is None:
            raise ValueError(f"sheet {sheet_name!r} not found")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = None
        for r in rels:
            if r.get("Id") == rid:
                target = r.get("Target")
        if not target:
            raise ValueError("sheet relationship not found")
        target = target.lstrip("/")
        member = target if target.startswith("xl/") else "xl/" + target
        rows: list[list] = []
        for row in ET.fromstring(z.read(member)).iter(ns + "row"):
            vals: dict[int, str] = {}
            for c in row.findall(ns + "c"):
                ref = c.get("r") or ""
                m = re.match(r"[A-Z]+", ref)
                if not m:
                    continue
                col = 0
                for ch in m.group(0):
                    col = col * 26 + ord(ch) - 64
                t = c.get("t")
                if t == "inlineStr":
                    is_ = c.find(ns + "is")
                    val = "".join(x.text or "" for x in is_.iter(ns + "t")) if is_ is not None else ""
                else:
                    v = c.find(ns + "v")
                    if v is None or v.text is None:
                        continue
                    val = shared[int(v.text)] if t == "s" and v.text.isdigit() and int(v.text) < len(shared) else v.text
                vals[col - 1] = val
            width = max(vals) + 1 if vals else 0
            rows.append([vals.get(i) for i in range(width)])
        return rows


def parse_pinksheet(rows: list[list], keep_months: int = 13) -> dict:
    """World Bank "Monthly Prices" rows -> {updated, series: {code: {name, unit, values: [[month, v]]}}}."""
    updated = None
    header = units = None
    data_start = None
    for i, row in enumerate(rows):
        first = str(row[0] or "") if row else ""
        if first.lower().startswith("updated on"):
            updated = first[10:].strip()
        if re.fullmatch(r"\d{4}M\d{2}", first.strip()):
            data_start = i
            break
    if data_start is None or data_start < 2:
        raise ValueError("no monthly rows found")
    header, units = rows[data_start - 2], rows[data_start - 1]
    data = [r for r in rows[data_start:] if r and re.fullmatch(r"\d{4}M\d{2}", str(r[0] or "").strip())]
    series = {}
    for code, (prefix, display) in WB_SERIES.items():
        col = None
        for j, h in enumerate(header):
            if h and str(h).strip().lower().startswith(prefix.lower()):
                col = j
                break
        if col is None:
            continue
        vals = []
        for r in data:
            v = fnum(r[col]) if col < len(r) else None
            if v is not None:
                vals.append([str(r[0]).strip(), v])
        if not vals:
            continue
        unit_raw = str(units[col] or "").strip().strip("()") if col < len(units) else ""
        series[code] = {"name": display, "column": str(header[col]).strip(), "unit_raw": unit_raw,
                        "unit": _WB_UNITS.get(unit_raw, unit_raw.replace("$/", "per ")),
                        "currency": "USD" if unit_raw.startswith("$") else None, "values": vals[-keep_months:]}
    return {"updated": updated, "series": series}


def wb_quote(code: str, data: dict) -> dict | None:
    s = (data or {}).get("series", {}).get(code)
    if not s or not s.get("values"):
        return None
    month, value = s["values"][-1]
    prev = s["values"][-2][1] if len(s["values"]) > 1 else None
    out = {"symbol": f"wb:{code}", "name": s["name"], "price": value, "currency": s.get("currency"),
           "unit": s.get("unit"), "as_of": _month_label(month), "time": _month_ts(month),
           "delay": "monthly average", "source": "World Bank Pink Sheet", "source_url": WB_PAGE,
           "history": [v for _, v in s["values"]]}
    _add_change(out, prev)
    return out


def find_pinksheet_link(html: str) -> str | None:
    m = WB_LINK_RE.search(html or "")
    return m.group(0) if m else None


def _add_change(out: dict, prev: float | None) -> None:
    if prev and out.get("price") is not None:
        out["change"] = out["price"] - prev
        out["change_pct"] = (out["price"] - prev) / prev * 100.0
        out["previous"] = prev


def _iso_ts(value) -> int | None:
    try:
        from datetime import datetime, timezone
        s = str(value or "").replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()) if s else None
    except ValueError:
        return None


def _date_ts(date: str) -> int:
    import calendar
    y, m, d = (int(x) for x in date[:10].split("-"))
    return calendar.timegm((y, m, d, 0, 0, 0))


def _month_ts(month: str) -> int:
    import calendar
    y, m = int(month[:4]), int(month[5:7])
    return calendar.timegm((y, m, 15, 0, 0, 0))


def _month_label(month: str) -> str:
    names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]
    return f"{names[int(month[5:7]) - 1]} {month[:4]}"


def split_equity(symbol: str) -> tuple[str, str | None]:
    """"AAPL:XNAS" -> ("AAPL", "XNAS"); "AAPL" -> ("AAPL", None)."""
    t, _, mic = symbol.partition(":")
    return t.strip().upper(), (mic.strip().upper() or None)


# ---------------------------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------------------------
class Markets:
    """Quotes with caching, a FRED circuit breaker and a small 24-hour history for live prices."""

    def __init__(self, m, commodities_path: Path):
        self.m = m
        self.net = m.net
        self.commodities_path = commodities_path
        self._wb: dict | None = None
        self._wb_mtime = 0.0
        self._fred_down_until = 0.0
        self._lock = threading.Lock()
        self._board: dict[str, dict] = {}
        self._board_at = 0.0
        self._refreshing = False
        self.last_demand = 0.0
        m.store.script("CREATE TABLE IF NOT EXISTS infra_quote_hist (symbol TEXT NOT NULL, ts REAL NOT NULL, "
                       "price REAL NOT NULL); CREATE INDEX IF NOT EXISTS infra_quote_hist_s ON infra_quote_hist(symbol, ts);")

    # -- keys ------------------------------------------------------------------------------------
    def keys(self) -> dict[str, bool]:
        return {"finnhub": self.m.config.has("FINNHUB_API_KEY"), "twelvedata": self.m.config.has("TWELVEDATA_API_KEY")}

    # -- providers -----------------------------------------------------------------------------
    def goldapi(self, sym: str) -> dict | None:
        try:
            obj = self.net.get_json(GOLDAPI.format(sym=sym), ttl=90, timeout=10, retries=1)
            q = parse_goldapi(obj, sym)
            self.m.status(SRC_GOLD, ok=bool(q), count=len(GOLD_SYMBOLS), error=None if q else "no price in answer")
        except Exception as err:
            self.m.status(SRC_GOLD, ok=False, error=str(err))
            return None
        if q:
            self._history(q)
        return q

    def _history(self, q: dict) -> None:
        now = time.time()
        sym = q["symbol"]
        try:
            last = self.m.store.scalar("SELECT MAX(ts) FROM infra_quote_hist WHERE symbol = ?", (sym,))
            if not last or now - last > 240:
                self.m.store.execute("INSERT INTO infra_quote_hist(symbol, ts, price) VALUES(?,?,?)", (sym, now, q["price"]))
                self.m.store.execute("DELETE FROM infra_quote_hist WHERE ts < ?", (now - 3 * 86400,))
            row = self.m.store.one("SELECT price, ts FROM infra_quote_hist WHERE symbol = ? AND ts BETWEEN ? AND ? "
                                   "ORDER BY ABS(ts - ?) LIMIT 1", (sym, now - 27 * 3600, now - 21 * 3600, now - 86400))
        except Exception:
            return
        if row and row["price"]:
            _add_change(q, row["price"])
            q["change_basis"] = "about 24 hours ago"

    def ecb_days(self) -> list:
        try:
            xml = self.net.get_text(ECB_90D, ttl=3600, timeout=15, retries=1)
            days = parse_ecb_xml(xml)
            self.m.status(SRC_ECB, ok=bool(days), count=len(days[0][1]) - 1 if days else 0,
                          note=f"Reference rates for {days[0][0]}" if days else None)
            return days
        except Exception as err:
            self.m.status(SRC_ECB, ok=False, error=str(err))
            return []

    def ecb(self, pair: str) -> dict | None:
        return fx_from_ecb(self.ecb_days(), pair)

    def eur_rates(self) -> dict[str, float]:
        days = self.ecb_days()
        return dict(days[0][1]) if days else {}

    def fred(self, series: str) -> dict | None:
        if series not in FRED_SERIES or time.time() < self._fred_down_until:
            return None
        start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 40 * 86400))
        try:
            body = self.net.get_text(FRED_CSV, params={"id": series, "cosd": start}, ttl=3 * 3600, timeout=12, retries=0)
            rows = parse_fred_csv(body)
            q = fred_quote(series, rows)
            if not q:
                raise ValueError("FRED answered without data rows")
            self.m.status(SRC_FRED, ok=True)
            return q
        except Exception as err:
            self._fred_down_until = time.time() + 6 * 3600
            self.m.status(SRC_FRED, ok=False, error=f"{err} (retrying in 6 hours)")
            return None

    def wb_data(self) -> dict:
        try:
            st = self.commodities_path.stat()
        except OSError:
            return {}
        if self._wb is None or st.st_mtime != self._wb_mtime:
            import json
            try:
                self._wb = json.loads(self.commodities_path.read_text(encoding="utf-8"))
                self._wb_mtime = st.st_mtime
            except ValueError:
                self._wb = {}
        return self._wb or {}

    def wb(self, code: str) -> dict | None:
        return wb_quote(code, self.wb_data())

    def equity(self, symbol: str) -> dict:
        """Delayed share price via a configured provider, or an explanation."""
        ticker, mic = split_equity(symbol)
        if not re.fullmatch(r"[A-Z0-9.\-^/&]{1,16}", ticker):
            return {"symbol": symbol, "available": False, "reason": "That is not a ticker symbol."}
        from .companies import FINNHUB_SUFFIX, US_MICS
        keys = self.keys()
        errors = []
        if keys["finnhub"] and (mic is None or mic in US_MICS or mic in FINNHUB_SUFFIX):
            fsym = ticker if (mic is None or mic in US_MICS) else ticker + FINNHUB_SUFFIX[mic]
            try:
                obj = self.net.get_json(FINNHUB, params={"symbol": fsym}, ttl=60, timeout=10, retries=1,
                                        headers={"X-Finnhub-Token": self.m.config.get("FINNHUB_API_KEY")})
                q = parse_finnhub(obj, symbol)
                self.m.status(SRC_FINNHUB, ok=True)
                if q:
                    q["available"] = True
                    return q
                errors.append("Finnhub has no price for it")
            except Exception as err:
                self.m.status(SRC_FINNHUB, ok=False, error=str(err))
                errors.append("Finnhub did not answer")
        if keys["twelvedata"]:
            params = {"symbol": ticker}
            if mic:
                params["mic_code"] = mic
            try:
                obj = self.net.get_json(TWELVEDATA, params=params, ttl=60, timeout=10, retries=1,
                                        headers={"Authorization": f"apikey {self.m.config.get('TWELVEDATA_API_KEY')}"})
                q = parse_twelvedata(obj, symbol)
                self.m.status(SRC_TWELVE, ok=True)
                if q:
                    q["available"] = True
                    return q
                errors.append("Twelve Data has no price for it on your plan")
            except Exception as err:
                self.m.status(SRC_TWELVE, ok=False, error=str(err))
                errors.append("Twelve Data did not answer")
        if not keys["finnhub"] and not keys["twelvedata"]:
            return {"symbol": symbol, "available": False, "needs_key": True,
                    "reason": "Share prices need a free FINNHUB_API_KEY or TWELVEDATA_API_KEY in .env. "
                              "No keyless share-price source can be used within its terms."}
        return {"symbol": symbol, "available": False, "reason": "; ".join(errors) or "No price available."}

    # -- dispatch ----------------------------------------------------------------------------------
    def quote(self, symbol: str) -> dict:
        s = (symbol or "").strip()
        if not SYMBOL_RE.fullmatch(s):
            return {"symbol": s[:32], "available": False, "reason": "Use a symbol such as XAU, EURUSD, wb:BRENT or AAPL:XNAS."}
        up = s.upper()
        item = ITEMS.get(s.lower())
        if item:
            return self.item_quote(item)
        q = None
        if up in GOLD_SYMBOLS:
            q = self.goldapi(up)
        elif up.startswith("FRED:"):
            q = self.fred(up[5:])
        elif up.startswith("WB:"):
            q = self.wb(up[3:])
        elif len(up) == 6 and up[:3] in ECB_CURRENCIES and up[3:] in ECB_CURRENCIES and ":" not in up:
            q = self.ecb(up)
        else:
            return self.equity(s)
        if q:
            q["available"] = True
            return q
        return {"symbol": s, "available": False, "reason": "That source is not answering right now."}

    def item_quote(self, item: dict) -> dict:
        tried = []
        for kind, sym in item["sources"]:
            q = None
            if kind == "goldapi":
                q = self.goldapi(sym)
            elif kind == "ecb":
                q = self.ecb(sym)
            elif kind == "fred":
                q = self.fred(sym)
            elif kind == "wb":
                q = self.wb(sym)
            elif kind == "equity":
                if not any(self.keys().values()):
                    tried.append("needs a share-price key")
                    continue
                q = self.equity(sym)
                if q and q.get("available"):
                    q["via"] = f"{split_equity(sym)[0]} fund"
                else:
                    q = None
            if q:
                q = dict(q)
                q.update({"id": item["id"], "name": item["name"], "available": True})
                if item.get("fly"):
                    q["fly"] = item["fly"]
                return q
            tried.append(kind)
        out = {"id": item["id"], "name": item["name"], "available": False, "symbol": item["id"],
               "reason": _reason(tried)}
        if item.get("fly"):
            out["fly"] = item["fly"]
        return out

    # -- the board (drawer) ----------------------------------------------------------------------
    def board(self) -> dict:
        """Cached snapshot for the drawer; a refresh runs in the background when it is stale."""
        self.last_demand = time.time()
        stale = time.time() - self._board_at > 120
        if stale:
            self._start_refresh()
        groups = []
        for g in MARKET_GROUPS:
            items = []
            for it in g["items"]:
                q = self._board.get(it["id"])
                items.append(q or {"id": it["id"], "name": it["name"], "pending": True, "fly": it.get("fly")})
            groups.append({"id": g["id"], "title": g["title"], "items": items})
        return {"groups": groups, "updated_at": self._board_at or None, "refreshing": self._refreshing,
                "keys": self.keys(), "fred_down": time.time() < self._fred_down_until,
                "commodities_updated": self.wb_data().get("updated")}

    def _start_refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self.refresh_board, name="infra-markets", daemon=True).start()

    def refresh_board(self) -> None:
        try:
            fresh = {}
            for g in MARKET_GROUPS:
                for it in g["items"]:
                    try:
                        fresh[it["id"]] = self.item_quote(it)
                    except Exception as err:   # one item never breaks the board
                        self.m.log.warning("market item %s failed: %s", it["id"], err)
                        fresh[it["id"]] = {"id": it["id"], "name": it["name"], "available": False,
                                           "reason": "Failed to load.", "fly": it.get("fly")}
            self._board = fresh
            self._board_at = time.time()
        finally:
            self._refreshing = False

    def wanted(self) -> bool:
        return time.time() - self.last_demand < 330


def _reason(tried: list[str]) -> str:
    if "needs a share-price key" in tried and len(tried) == 1:
        return "Needs a free FINNHUB_API_KEY or TWELVEDATA_API_KEY"
    if "fred" in tried and "needs a share-price key" in tried:
        return "FRED is not answering; a share-price key would show the fund instead"
    if "fred" in tried:
        return "FRED is not answering"
    return "Source not answering"


def build_commodities(ds) -> None:
    """Dataset: the World Bank Pink Sheet monthly prices (link found on the commodity-markets page)."""
    url = None
    try:
        html = ds.net.get_text(WB_PAGE, timeout=30, retries=1)
        url = find_pinksheet_link(html)
    except Exception as err:
        ds.note(f"World Bank page unavailable ({err}); using the last known link")
    url = url or WB_FALLBACK
    path = ds.download(url, "CMO-Historical-Data-Monthly.xlsx", min_bytes=100_000, timeout=120)
    data = parse_pinksheet(read_xlsx_sheet(path, "Monthly Prices"))
    if len(data["series"]) < MIN_SERIES:
        raise RuntimeError(f"Pink Sheet parse found only {len(data['series'])} series")
    data["built_at"] = time.time()
    data["url"] = url
    ds.write_json("commodities.json", data)
    last = next(iter(data["series"].values()))["values"][-1][0]
    ds.set_rows(len(data["series"]))
    ds.note(f"{len(data['series'])} monthly series to {_month_label(last)} (updated {data.get('updated') or 'unknown'})")
