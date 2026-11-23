"""Infra markets tests: quote parsers (gold-api.com, ECB, FRED, World Bank, Finnhub, Twelve Data),
the stdlib .xlsx reader, symbol routing and the drawer snapshot. No network.

Run: python -m unittest tests.test_infra_markets -v
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from meridian.config import Config
from meridian.modules.infra import markets
from meridian.store import Store

from tests.test_infra import FIX, FakeNet, fixture


def make_xlsx(path: Path, rows: list[list]) -> None:
    """A minimal .xlsx with one sheet called "Monthly Prices" (strings shared, numbers inline)."""
    strings: list[str] = []

    def sidx(s):
        if s not in strings:
            strings.append(s)
        return strings.index(s)

    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if v is None:
                continue
            if isinstance(v, str):
                cells.append(f'<c r="{ref}" t="s"><v>{sidx(v)}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><sheets><sheet name="Notes" sheetId="1" r:id="rId1"/>'
                                      f'<sheet name="Monthly Prices" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData/></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")


PINK_ROWS = [
    ["World Bank Commodity Price Data (The Pink Sheet)"],
    ["Updated on November 03, 2026"],
    [None, "Crude oil, Brent", "Natural gas, Europe", "Wheat, US HRW", "Gold"],
    [None, "($/bbl)", "($/mmbtu)", "($/mt)", "($/troy oz)"],
    ["2026M08", 90.9, 21.11, 240.5, 3300],
    ["2026M09", 116.8, 25.42, "…", 3400],
    ["2026M10", 125, 28.5, 250.0, 3500],
]


class ParserTests(unittest.TestCase):
    def test_goldapi(self):
        q = markets.parse_goldapi(json.loads(fixture("goldapi_xau.json")), "XAU")
        self.assertEqual((q["name"], q["price"], q["currency"], q["delay"]), ("Gold", 3539.0, "USD", "live"))
        self.assertIsInstance(q["time"], int)
        self.assertIsNone(markets.parse_goldapi({"price": 0}, "XAU"))
        self.assertIsNone(markets.parse_goldapi("nonsense", "XAU"))

    def test_ecb_cross_rates(self):
        days = markets.parse_ecb_xml(fixture("ecb_90d_sample.xml"))
        self.assertEqual([d for d, _ in days], ["2026-11-20", "2026-11-19"])
        eurusd = markets.fx_from_ecb(days, "EURUSD")
        self.assertAlmostEqual(eurusd["price"], 1.099)
        self.assertAlmostEqual(eurusd["change_pct"], (1.099 - 1.1) / 1.1 * 100)
        gbpusd = markets.fx_from_ecb(days, "GBPUSD")
        self.assertAlmostEqual(gbpusd["price"], 1.099 / 0.83735)
        usdjpy = markets.fx_from_ecb(days, "USDJPY")
        self.assertAlmostEqual(usdjpy["price"], 174.35 / 1.099)
        self.assertIsNone(markets.fx_from_ecb(days, "USDXXX"))

    def test_fred(self):
        rows = markets.parse_fred_csv(fixture("fred_brent_sample.csv"))
        self.assertEqual(len(rows), 3)          # the "." row is skipped
        q = markets.fred_quote("DCOILBRENTEU", rows)
        self.assertEqual((q["price"], q["as_of"], q["symbol"]), (89.53, "2026-11-17", "fred:DCOILBRENTEU"))
        self.assertAlmostEqual(q["change"], 89.53 - 89.24)

    def test_keyed_providers(self):
        f = markets.parse_finnhub(json.loads(fixture("finnhub_aapl.json")), "AAPL:XNAS")
        self.assertEqual((f["price"], f["change_pct"], f["source"]), (261.74, -0.5396, "Finnhub"))
        self.assertIsNone(markets.parse_finnhub({"c": 0, "t": 0}, "NOPE"))
        t = markets.parse_twelvedata(json.loads(fixture("twelvedata_aapl.json")), "AAPL:XNAS")
        self.assertEqual((t["price"], t["currency"], t["market_open"]), (261.74, "USD", False))
        self.assertIsNone(markets.parse_twelvedata({"code": 404, "status": "error", "message": "x"}, "X"))

    def test_xlsx_and_pinksheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pink.xlsx"
            make_xlsx(path, PINK_ROWS)
            rows = markets.read_xlsx_sheet(path, "Monthly Prices")
            self.assertEqual(rows[2][1], "Crude oil, Brent")
            data = markets.parse_pinksheet(rows)
        self.assertEqual(data["updated"], "November 03, 2026")
        self.assertEqual(set(data["series"]), {"BRENT", "NGAS_EUR", "WHEAT", "GOLD"})
        self.assertEqual(data["series"]["BRENT"]["unit"], "per barrel")
        self.assertEqual(len(data["series"]["WHEAT"]["values"]), 2)   # the "…" cell is skipped
        q = markets.wb_quote("BRENT", data)
        self.assertEqual((q["price"], q["as_of"], q["delay"]), (125, "October 2026", "monthly average"))
        self.assertAlmostEqual(q["change_pct"], (125 - 116.8) / 116.8 * 100)
        self.assertIsNone(markets.wb_quote("NOPE", data))

    def test_pinksheet_link(self):
        html = ('<a href="https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/'
                'CMO-Historical-Data-Monthly.xlsx">Monthly</a>')
        self.assertTrue(markets.find_pinksheet_link(html).endswith("CMO-Historical-Data-Monthly.xlsx"))
        self.assertIsNone(markets.find_pinksheet_link("<a href='https://evil.example/CMO-Historical-Data-Monthly.xlsx'>"))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.net = FakeNet({"price/XAU": "goldapi_xau.json", "eurofxref-hist-90d": "ecb_90d_sample.xml",
                            "finnhub.io": "finnhub_aapl.json"})
        self.statuses = {}
        self.config = Config(env_file=None)
        self.m = SimpleNamespace(name="infra", net=self.net, config=self.config, store=Store(root / "t.db"),
                                 log=logging.getLogger("test.markets"),
                                 status=lambda sid, **kw: self.statuses.__setitem__(sid, kw))
        path = root / "pink.xlsx"
        make_xlsx(path, PINK_ROWS)
        (root / "commodities.json").write_text(json.dumps(markets.parse_pinksheet(markets.read_xlsx_sheet(path, "Monthly Prices"))),
                                               encoding="utf-8")
        self.mk = markets.Markets(self.m, root / "commodities.json")

    def tearDown(self):
        try:
            self.m.store.conn().close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_routing(self):
        self.assertEqual(self.mk.quote("XAU")["price"], 3539.0)
        self.assertAlmostEqual(self.mk.quote("eurusd")["price"], 1.099)
        self.assertEqual(self.mk.quote("wb:BRENT")["price"], 125)
        self.assertEqual(self.mk.quote("gold")["id"], "gold")           # a drawer item by id
        bad = self.mk.quote("<script>")
        self.assertFalse(bad["available"])
        eq = self.mk.quote("AAPL:XNAS")
        self.assertFalse(eq["available"])
        self.assertTrue(eq["needs_key"])
        self.assertFalse(any("finnhub" in url for url, _ in self.net.calls))   # no key, no request

    def test_key_goes_in_a_header(self):
        cfg = Config(env_file=None, overrides={"FINNHUB_API_KEY": "secret-test-key-123"})
        self.m.config = cfg
        q = self.mk.quote("AAPL:XNAS")
        self.assertTrue(q["available"])
        url, kw = next((u, k) for u, k in self.net.calls if "finnhub.io" in u)
        self.assertNotIn("secret-test-key-123", url)
        self.assertEqual(kw["headers"]["X-Finnhub-Token"], "secret-test-key-123")
        self.assertNotIn("secret-test-key-123", json.dumps(q))

    def test_board(self):
        self.mk.refresh_board()
        doc = self.mk.board()
        groups = {g["id"]: g for g in doc["groups"]}
        self.assertEqual(set(groups), {"indices", "metals", "energy", "agri", "base", "fx", "crypto"})
        gold = next(i for i in groups["metals"]["items"] if i["id"] == "gold")
        self.assertTrue(gold["available"])
        brent = next(i for i in groups["energy"]["items"] if i["id"] == "brent")
        self.assertEqual(brent["source"], "World Bank Pink Sheet")      # FRED is not served by the fake net
        spx = next(i for i in groups["indices"]["items"] if i["id"] == "spx")
        self.assertFalse(spx["available"])
        self.assertIn("fly", spx)
        self.assertGreater(self.mk._fred_down_until, 0)                 # FRED failed once, then backs off


if __name__ == "__main__":
    unittest.main()
