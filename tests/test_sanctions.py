"""Sanctions module: the OFAC, UK, UN and EU parsers, the merge across lists, the backward-compatible
Python API, routes, search, the AI tool, and a failing list keeping its last good copy.

Run: python -m unittest tests.test_sanctions -v
No network: small fixtures in tests/fixtures/sanctions/ and a local HTTP server standing in for a list host.
The module keeps its index in module globals; every test restores what was there before.
"""

from __future__ import annotations

import gzip
import inspect
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from meridian.config import Config
from meridian.datasets import DatasetManager
from meridian.modules import sanctions as S
from meridian.modules.sanctions import merge as M
from meridian.modules.sanctions import parsers as P
from meridian.net import Net
from meridian.registry import App, HTTPError, Request
from meridian.scheduler import Scheduler
from meridian.store import Store

FIX = Path(__file__).resolve().parent / "fixtures" / "sanctions"
AI_SDN = Path(__file__).resolve().parent / "fixtures" / "ai" / "sdn.json"
BUILT = 1795380000.0
QUIET = logging.getLogger("meridian.test.sanctions")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False


def ofac_data() -> dict:
    vessels, aircraft = P.parse_sdn((FIX / "sdn_sample.csv").read_text(encoding="utf-8"))
    return {"built_at": BUILT, "vessels": vessels, "aircraft": aircraft}


def load_fixtures() -> None:
    S._reset()
    S._set_list("OFAC", ofac_data())
    S._set_list("UK", dict(P.parse_uk(FIX / "uk_sample.xml"), built_at=BUILT))
    S._set_list("UN", dict(P.parse_un(FIX / "un_sample.xml"), built_at=BUILT))
    S._set_list("EU", dict(P.parse_eu(FIX / "eu_sample.xml"), built_at=BUILT))


class Isolated(unittest.TestCase):
    """Saves and restores the sanctions module's global state around each test."""

    def setUp(self):
        with S._lock:
            self._saved = (dict(S._lists), S._index, dict(S.URLS))

    def tearDown(self):
        lists, index, urls = self._saved
        with S._lock:
            S._lists.clear()
            S._lists.update(lists)
            S._index = index
            S.URLS.clear()
            S.URLS.update(urls)


# ---------------------------------------------------------------------------------------------
# Parsers, one per list format
# ---------------------------------------------------------------------------------------------
class ParserTests(unittest.TestCase):
    def test_ofac_csv(self):
        vessels, aircraft = P.parse_sdn((FIX / "sdn_sample.csv").read_text(encoding="utf-8"))
        self.assertEqual([v["uid"] for v in vessels], ["10001", "10002", "10003"])   # people and entities skipped
        sea = vessels[1]
        self.assertEqual((sea["imo"], sea["mmsi"], sea["flag"], sea["call_sign"]), ("9187629", "352900000", "Panama", "9HA1234"))
        self.assertEqual(sea["aliases"], ["OCEAN BREEZE"])
        self.assertEqual(sea["linked_to"], ["EXAMPLE SHIPPING CO"])
        self.assertIsNone(vessels[2]["imo"])
        self.assertEqual(vessels[2]["mmsi"], "445123456")
        (plane,) = aircraft
        self.assertEqual((plane["registration"], plane["mode_s"], plane["msn"], plane["model"], plane["built"]),
                         ("EP-TST", "730abc", "123", "Airbus A340-300", "1999"))

    def test_uk_ships_only(self):
        out = P.parse_uk(FIX / "uk_sample.xml")
        self.assertEqual(out["generated"], "2026-11-23")
        self.assertEqual(out["counts"], {"designations": 7, "individuals": 1, "entities": 1, "ships": 5,
                                         "vessels": 5, "aircraft": 0})
        self.assertEqual(out["aircraft"], [])
        by = {v["list_id"]: v for v in out["vessels"]}
        self.assertEqual(sorted(by), ["DPR0075", "DPR0120", "LIB0079", "RUS2176", "RUS2321"])
        text = json.dumps(out)
        self.assertNotIn("PERSON", text)           # the individual is counted, never kept
        self.assertNotIn("1970", text)
        k = by["RUS2176"]
        self.assertEqual((k["uid"], k["list"], k["name"], k["imo"], k["flag"], k["vessel_type"], k["built"]),
                         ("uk-RUS2176", "UK", "KOROLEV PROSPECT", "9826902", "Gabon", "Oil Tanker", "2019"))
        self.assertEqual((k["listed_on"], k["updated_on"], k["programmes"], k["program"]),
                         ("2024-07-31", "2024-07-31", ["Russia"], "Russia"))
        self.assertEqual(k["measures"], ["Shipping sanctions"])
        self.assertEqual((k["owner"], k["linked_to"]), ("Stream Ship Management FZCO", ["Stream Ship Management FZCO"]))
        self.assertIsNone(k["mmsi"])
        self.assertEqual(k["call_sign"], "")
        mik = by["RUS2321"]
        self.assertEqual((mik["imos"], mik["aliases"], mik["tonnage"]), (["9250892"], ["MIKATI I"], "57301"))
        self.assertEqual(mik["linked_to"], ["Example Ship Holding Ltd"])       # same owner twice, kept once
        self.assertEqual(mik["measures"], ["Shipping & Trade Sanctions"])
        pet = by["DPR0075"]
        self.assertEqual((pet["imo"], pet["aliases"], pet["programmes"]), ("9562233", ["Jal Vahini"], ["DPRK"]))
        self.assertEqual((pet["designation_source"], pet["un_ref"], pet["previous_flags"], pet["length_m"]),
                         ("UN", "KPe.073", ["India"], "134.5"))
        self.assertEqual(by["LIB0079"]["name"], "NORTHWIND")                     # 'IMO 9123453 ("NORTHWIND")'
        self.assertEqual(by["LIB0079"]["programmes"], ["Libya"])
        self.assertIsNone(by["DPR0120"]["imo"])
        for v in out["vessels"]:                                            # every OFAC vessel key is present
            self.assertLessEqual(set(M.VESSEL_KEYS), set(v))

    def test_uk_regime_names(self):
        self.assertEqual(P.uk_regime("The Russia (Sanctions) (EU Exit) Regulations 2019"), "Russia")
        self.assertEqual(P.uk_regime("The Iran (Sanctions) (Nuclear) (EU Exit) Regulations 2019"), "Iran (Nuclear)")
        self.assertEqual(P.uk_regime("The Global Human Rights Sanctions Regulations 2020"), "Global Human Rights")
        self.assertEqual(P.uk_regime("The Democratic People's Republic of Korea (Sanctions) (EU Exit) "
                                     "Regulations 2019"), "DPRK")

    def test_un_vessels(self):
        out = P.parse_un(FIX / "un_sample.xml")
        self.assertEqual(out["generated"], "2026-11-22")
        self.assertEqual(out["counts"], {"designations": 4, "individuals": 1, "entities": 3, "vessels": 1, "aircraft": 0})
        (avax,) = out["vessels"]      # not the shipping company (company IMO number) nor the group (no vessel cue)
        self.assertEqual((avax["uid"], avax["name"], avax["imo"], avax["flag"], avax["programmes"], avax["listed_on"]),
                         ("un-9000003", "NORTHWIND", "9123453", "Togo", ["Libya"], "2026-06-15"))
        self.assertEqual(avax["reference"], "LYe.901")
        self.assertNotIn("EXAMPLE", json.dumps(out))

    def test_eu_vessels_plain_and_gzip(self):
        raw = (FIX / "eu_sample.xml").read_bytes()
        out = P.parse_eu(raw)
        self.assertEqual(P.parse_eu(gzip.compress(raw)), out)              # the download is stored gzip-compressed
        self.assertEqual(out["generated"], "2026-11-20")
        self.assertEqual(out["counts"], {"designations": 5, "individuals": 1, "entities": 4, "vessels": 2, "aircraft": 0})
        by = {v["list_id"]: v for v in out["vessels"]}
        self.assertEqual(sorted(by), ["147503", "199999"])      # not Al Houda (company), not FIRST OIL (company number)
        nk = by["147503"]
        self.assertEqual((nk["uid"], nk["name"], nk["imo"], nk["mmsi"], nk["vessel_type"], nk["programmes"]),
                         ("eu-147503", "New Konk", "9036387", "445123456", "delivery vessel, oil tanker", ["PRK"]))
        self.assertEqual((nk["owner"], nk["linked_to"], nk["listed_on"], nk["reference"]),
                         ("New Konk Ocean International Company Limited",
                          ["New Konk Ocean International Company Limited"], "2022-12-12", "EU.9443.81"))
        kp = by["199999"]
        self.assertEqual((kp["flag"], kp["programmes"], kp["listed_on"]), ("Gabon", ["RUS", "UKR"], "2024-12-16"))
        self.assertNotIn("Example Person", json.dumps(out))

    def test_helpers(self):
        self.assertTrue(P.imo_valid("9826902"))
        self.assertTrue(P.imo_valid("9123453"))
        self.assertFalse(P.imo_valid("5550001"))                  # a (fictional) shipping company's IMO number
        self.assertFalse(P.imo_valid("98269O2"))
        self.assertEqual(P.imo_digits("IMO9826902"), "9826902")
        self.assertEqual(P.imo_digits("IMO 9826902"), "9826902")
        self.assertIsNone(P.imo_digits("12345"))
        self.assertEqual(P.iso_date("31/07/2024"), "2024-07-31")
        self.assertEqual(P.iso_date("2026-11-22T00:00:00.942Z"), "2026-11-22")
        self.assertIsNone(P.iso_date("July 2024"))
        self.assertTrue(P.company_like("SAMPLELINE SHIPPING CO"))
        self.assertTrue(P.company_like("Акционерное Общество ООО “Пример”"))
        self.assertFalse(P.company_like("New Konk"))
        self.assertEqual(P.split_programs("SDGT] [NPWMD] [IFSR"), ["SDGT", "NPWMD", "IFSR"])

    def test_refuses_doctype(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]><Designations>&a;</Designations>'
        for parse in (P.parse_uk, P.parse_un, P.parse_eu):
            with self.assertRaises(P.UnsafeXML):
                parse(bomb)

    def test_refuses_doctype_in_any_encoding_or_after_long_prolog(self):
        dtd = '<!DOCTYPE d [<!ENTITY a "EXPANDED">]><Designations>&a;</Designations>'
        utf16 = ('<?xml version="1.0" encoding="UTF-16"?>' + dtd).encode("utf-16")
        long_prolog = ('<?xml version="1.0"?><!--' + "x" * 70000 + "-->" + dtd).encode()
        for doc in (utf16, long_prolog):
            with self.assertRaises(P.UnsafeXML):
                list(P._xml_events(doc, events=("end",)))
            for parse in (P.parse_uk, P.parse_un, P.parse_eu):
                with self.assertRaises(P.UnsafeXML):
                    parse(doc)

    def test_xml_events_match_iterparse(self):
        doc = (b'<?xml version="1.0"?><e:export xmlns:e="urn:x" when="1"><e:item xml:lang="en">a &amp; b</e:item>'
               b"<plain>t</plain></e:export>")
        import io
        import xml.etree.ElementTree as ET
        want = [(ev, el.tag, dict(el.attrib), el.text) for ev, el in ET.iterparse(io.BytesIO(doc), events=("start", "end"))]
        got = [(ev, el.tag, dict(el.attrib), el.text) for ev, el in P._xml_events(doc)]
        self.assertEqual(got, want)
        self.assertEqual(list(P._xml_events("<r>é</r>".encode("utf-16"), events=("end",)))[0][1].text, "é")

    def test_broken_xml_raises(self):
        with self.assertRaises(Exception):
            P.parse_uk(b"<Designations><Designation>")


# ---------------------------------------------------------------------------------------------
# Merge rules
# ---------------------------------------------------------------------------------------------
def ofac_vessel(uid, name, imo=None, mmsi=None, program="IRAN", **kw):
    rec = {"uid": uid, "name": name, "program": program, "linked_to": [], "aliases": [], "built": None, "remarks": "",
           "kind": "sanctioned_vessel", "call_sign": "", "vessel_type": "", "tonnage": "", "grt": "", "flag": "",
           "owner": "", "imo": imo, "mmsi": mmsi}
    rec.update(kw)
    return S._normalise("OFAC", rec, "vessel")


def list_vessel(lid, native, name, **kw):
    return S._normalise(lid, P._vessel_record(lid, native, name=name, **kw), "vessel")


def ofac_aircraft(uid, reg, mode_s=None, msn=None):
    rec = {"uid": uid, "name": reg, "program": "SDGT", "linked_to": [], "aliases": [], "built": None, "remarks": "",
           "kind": "sanctioned_aircraft", "registration": reg, "mode_s": mode_s, "msn": msn, "model": None,
           "operator": None}
    return S._normalise("OFAC", rec, "aircraft")


class MergeTests(unittest.TestCase):
    def test_imo_merge_keeps_ofac_first(self):
        out = M.merge_records({
            "UK": [list_vessel("UK", "RUS1", "NEW NAME", imo="9826902", programmes=["Russia"], flag="Gabon",
                               listed_on="2024-07-31", linked_to=["OWNER LLC"])],
            "OFAC": [ofac_vessel("501", "OLD NAME", imo="9826902", program="RUSSIA-EO14024] [UKRAINE-EO13662",
                                 linked_to=["SOVCOMFLOT"], aliases=["OLDER NAME"])]}, "vessel")
        (v,) = out
        self.assertEqual((v["uid"], v["name"], v["lists"]), ("501", "OLD NAME", ["OFAC", "UK"]))
        self.assertEqual(v["program"], "RUSSIA-EO14024] [UKRAINE-EO13662] [UK: Russia")
        self.assertEqual(v["programmes"], {"OFAC": ["RUSSIA-EO14024", "UKRAINE-EO13662"], "UK": ["Russia"]})
        self.assertEqual(v["flag"], "Gabon")                          # blank on OFAC, filled from the UK
        self.assertEqual(v["aliases"], ["OLDER NAME", "NEW NAME"])
        self.assertEqual(v["linked_to"], ["SOVCOMFLOT", "OWNER LLC"])
        self.assertEqual(v["listed_on"], "2024-07-31")
        self.assertEqual([(x["list"], x["id"], x["uid"]) for x in v["listings"]],
                         [("OFAC", "501", "501"), ("UK", "RUS1", "uk-RUS1")])

    def test_primary_fields_are_never_overwritten(self):
        (v,) = M.merge_records({"OFAC": [ofac_vessel("1", "A", imo="9826902", flag="Panama", vessel_type="Tanker")],
                                "EU": [list_vessel("EU", "9", "A", imo="9826902", flag="Gabon", vessel_type="Bulk")]},
                               "vessel")
        self.assertEqual((v["flag"], v["vessel_type"]), ("Panama", "Tanker"))
        self.assertEqual(v["listings"][1]["flag"], "Gabon")           # each list's own view stays in its listing

    def test_single_list_records(self):
        (v,) = M.merge_records({"UK": [list_vessel("UK", "RUS2", "ONLY UK", imo="9250892", programmes=["Russia"])]},
                               "vessel")
        self.assertEqual((v["uid"], v["lists"], v["program"]), ("uk-RUS2", ["UK"], "UK: Russia"))
        (o,) = M.merge_records({"OFAC": [ofac_vessel("7", "ONLY OFAC", imo="9187629", program="IRAN] [SDGT")]}, "vessel")
        self.assertEqual((o["program"], o["programmes"], o["lists"]), ("IRAN] [SDGT", {"OFAC": ["IRAN", "SDGT"]}, ["OFAC"]))

    def test_records_from_one_list_never_merge(self):
        out = M.merge_records({"OFAC": [ofac_vessel("1", "A", imo="9826902"), ofac_vessel("2", "B", imo="9826902")],
                               "UK": [list_vessel("UK", "X", "A", imo="9826902"), list_vessel("UK", "Y", "A2", imo="9826902")]},
                              "vessel")
        self.assertEqual([v["lists"] for v in out], [["OFAC"], ["OFAC"], ["UK"], ["UK"]])   # two OFAC groups: ambiguous

    def test_mmsi_merge_rules(self):
        base = {"OFAC": [ofac_vessel("1", "A", mmsi="445123456"),
                         ofac_vessel("2", "B", imo="9826902", mmsi="273000000"),
                         ofac_vessel("3", "C", mmsi="352900000"), ofac_vessel("4", "D", mmsi="352900000")]}
        out = M.merge_records(dict(base, EU=[list_vessel("EU", "10", "E", imo="9036387", mmsi="445123456"),
                                             list_vessel("EU", "11", "F", imo="9187629", mmsi="273000000"),
                                             list_vessel("EU", "12", "G", mmsi="352900000")]), "vessel")
        by = {v["uid"]: v for v in out}
        self.assertEqual(by["1"]["lists"], ["OFAC", "EU"])                 # MMSI match, no IMO on OFAC: merged
        self.assertEqual(by["1"]["imo"], "9036387")
        self.assertEqual(by["2"]["lists"], ["OFAC"])                       # MMSI match but the IMOs disagree
        self.assertEqual(by["eu-11"]["lists"], ["EU"])
        self.assertEqual((by["3"]["lists"], by["4"]["lists"], by["eu-12"]["lists"]), (["OFAC"], ["OFAC"], ["EU"]))

    def test_aircraft_merge_rules(self):
        out = M.merge_records({
            "OFAC": [ofac_aircraft("1", "EP-AAA", mode_s="730aaa", msn="100"), ofac_aircraft("2", "EP-BBB", msn="200")],
            "UK": [S._normalise("UK", {"uid": "uk-A1", "list_id": "A1", "name": "EP AAA", "registration": "EP AAA",
                                       "programmes": ["Iran"]}, "aircraft"),
                   S._normalise("UK", {"uid": "uk-A2", "list_id": "A2", "name": "X", "registration": "EP-ZZZ",
                                       "msn": "200"}, "aircraft"),
                   S._normalise("UK", {"uid": "uk-A3", "list_id": "A3", "name": "EP-BBB", "registration": "EP-BBB",
                                       "mode_s": "730bbb"}, "aircraft")]}, "aircraft")
        by = {a["uid"]: a for a in out}
        self.assertEqual(by["1"]["lists"], ["OFAC", "UK"])                 # same registration
        self.assertEqual(by["1"]["program"], "SDGT] [UK: Iran")
        self.assertEqual(by["uk-A2"]["lists"], ["UK"])                     # a serial number alone never merges
        self.assertEqual(by["2"]["lists"], ["OFAC", "UK"])                 # registration; OFAC had no Mode S
        self.assertEqual(by["2"]["mode_s"], "730bbb")


# ---------------------------------------------------------------------------------------------
# The Python API other modules use
# ---------------------------------------------------------------------------------------------
class ApiTests(Isolated):
    def setUp(self):
        super().setUp()
        load_fixtures()

    def test_signatures_unchanged(self):
        sig = inspect.signature(S.lookup_vessel)
        self.assertEqual([(p.name, p.default) for p in sig.parameters.values()],
                         [("imo", None), ("mmsi", None), ("name", None)])
        sig = inspect.signature(S.lookup_aircraft)
        self.assertEqual([(p.name, p.default) for p in sig.parameters.values()],
                         [("hex", None), ("reg", None), ("msn", None)])
        for fn in (S.vessels, S.aircraft, S.ready):
            self.assertEqual(len(inspect.signature(fn).parameters), 0)

    def test_ofac_only_records_keep_the_old_shape(self):
        S._reset()
        S._load(AI_SDN)                                  # the OFAC build file format, as the AI tests load it
        old = json.loads(AI_SDN.read_text(encoding="utf-8"))
        new_keys = {"match", "lists", "programmes", "listings", "listed_on"}
        hit = S.lookup_vessel(imo="9187629")
        for k, v in old["vessels"][0].items():
            self.assertEqual(hit[k], v, k)
        self.assertEqual(set(hit) - set(old["vessels"][0]), new_keys | {"imos"})
        self.assertEqual((hit["match"], hit["lists"], hit["programmes"]), ("imo", ["OFAC"], {"OFAC": ["IRAN"]}))
        self.assertEqual(S.lookup_vessel(mmsi="572469210")["match"], "mmsi")
        plane = S.lookup_aircraft(hex="730ABC")
        for k, v in old["aircraft"][0].items():
            self.assertEqual(plane[k], v, k)
        self.assertEqual(set(plane) - set(old["aircraft"][0]), new_keys)
        self.assertEqual(S.lookup_aircraft(reg="ep tst")["match"], "registration")
        self.assertEqual(S.lookup_aircraft(msn="123")["match"], "msn")
        self.assertEqual(S.vessels()[0]["uid"], "90001")
        self.assertIsNone(S.lookup_vessel(imo="1234567"))
        self.assertIsNone(S.lookup_vessel())
        self.assertIsNone(S.lookup_aircraft())

    def test_merged_vessel(self):
        k = S.lookup_vessel(imo="9826902")
        self.assertEqual((k["uid"], k["match"], k["lists"]), ("10001", "imo", ["OFAC", "UK", "EU"]))
        self.assertEqual(k["program"], "UKRAINE-EO13662] [RUSSIA-EO14024] [UK: Russia] [EU: RUS] [EU: UKR")
        self.assertEqual(k["programmes"], {"OFAC": ["UKRAINE-EO13662", "RUSSIA-EO14024"], "UK": ["Russia"],
                                           "EU": ["RUS", "UKR"]})
        self.assertEqual((k["flag"], k["built"], k["owner"], k["vessel_type"]),
                         ("Gabon", "2019", "Stream Ship Management FZCO", "Crude Oil Tanker"))
        self.assertEqual(k["linked_to"], ["EXAMPLE TANKER FLEET JSC", "Stream Ship Management FZCO"])
        self.assertEqual(k["listed_on"], "2024-07-31")
        self.assertEqual([(x["list"], x["id"], x.get("listed_on")) for x in k["listings"]],
                         [("OFAC", "10001", None), ("UK", "RUS2176", "2024-07-31"), ("EU", "199999", "2024-12-16")])
        star = S.lookup_vessel(mmsi="445123456")                          # OFAC (MMSI only) + EU (IMO and MMSI)
        self.assertEqual((star["uid"], star["lists"], star["imo"]), ("10003", ["OFAC", "EU"], "9036387"))
        self.assertEqual(S.lookup_vessel(imo="9036387")["uid"], "10003")
        avax = S.lookup_vessel(imo="9123453")
        self.assertEqual((avax["uid"], avax["lists"], avax["program"]), ("uk-LIB0079", ["UK", "UN"], "UK: Libya] [UN: Libya"))

    def test_uk_only_vessel(self):
        v = S.lookup_vessel(imo="9250892")
        self.assertEqual((v["uid"], v["match"], v["lists"], v["program"], v["name"]),
                         ("uk-RUS2321", "imo", ["UK"], "UK: Russia", "MIKATI"))
        self.assertEqual(P.split_programs(v["program"]), ["UK: Russia"])   # what ships and flights split it into
        for key in M.VESSEL_KEYS:
            self.assertIn(key, v)
        self.assertEqual(S.record("uk-RUS2321")["uid"], "uk-RUS2321")
        self.assertEqual(S.record("uk-RUS2176")["uid"], "10001")            # a list's own id finds the merged record
        self.assertIsNone(S.record("uk-NOPE"))

    def test_name_matches_stay_weak(self):
        self.assertEqual(S.lookup_vessel(name="Jal Vahini")["match"], "name")
        self.assertEqual(S.lookup_vessel(name="jal-vahini")["uid"], "uk-DPR0075")
        no_imo = S.lookup_vessel(name="MIN NING DE YOU 078")
        self.assertEqual((no_imo["match"], no_imo["uid"], no_imo["imo"]), ("name", "uk-DPR0120", None))
        self.assertEqual(S.lookup_vessel(imo="9250892", name="Petrel 8")["match"], "imo")   # identifiers first

    def test_identifiers_are_normalised(self):
        for imo in ("9826902", 9826902, " IMO 9826902 ", "IMO9826902"):
            self.assertEqual(S.lookup_vessel(imo=imo)["uid"], "10001", imo)
        self.assertEqual(S.lookup_vessel(mmsi=352900000)["uid"], "10002")
        self.assertIsNone(S.lookup_vessel(imo="x" * 5000))

    def test_ready_version_and_lists(self):
        v1 = S.version()
        S._drop_list("UK")
        self.assertGreater(S.version(), v1)
        self.assertIsNone(S.lookup_vessel(imo="9250892"))
        self.assertEqual(S.lookup_vessel(imo="9826902")["lists"], ["OFAC", "EU"])
        self.assertEqual(S.lookup_vessel(imo="9123453")["uid"], "un-9000003")
        S._reset()
        self.assertFalse(S.ready())
        self.assertEqual(S.vessels(), [])
        S._set_list("UN", dict(P.parse_un(FIX / "un_sample.xml"), built_at=BUILT))
        self.assertTrue(S.ready())                       # any list makes the index ready
        self.assertEqual(len(S.vessels()), 1)

    def test_summary(self):
        s = S.summary()
        self.assertTrue(s["ready"])
        rows = {r["id"]: r for r in s["lists"]}
        self.assertEqual(list(rows), ["OFAC", "UK", "UN", "EU"])
        self.assertEqual((rows["OFAC"]["vessels"], rows["OFAC"]["aircraft"], rows["OFAC"]["vessels_not_on_ofac"]),
                         (3, 1, None))
        self.assertEqual((rows["UK"]["vessels"], rows["UK"]["vessels_with_imo"], rows["UK"]["vessels_not_on_ofac"],
                          rows["UK"]["vessels_only_here"], rows["UK"]["designations"], rows["UK"]["generated"]),
                         (5, 4, 4, 3, 7, "2026-11-23"))
        self.assertEqual((rows["UN"]["vessels"], rows["UN"]["vessels_not_on_ofac"]), (1, 1))
        self.assertEqual((rows["EU"]["vessels"], rows["EU"]["vessels_not_on_ofac"]), (2, 0))
        self.assertEqual(s["merged"], {"vessels": 7, "aircraft": 1, "vessels_on_several_lists": 3,
                                       "aircraft_on_several_lists": 0})
        for r in rows.values():
            self.assertEqual((r["state"], r["loaded"], r["built_at"]), ("ok", True, BUILT))
            self.assertTrue(r["licence"] and r["url"].startswith("https://"))
        S._set_list("OFAC", dict(ofac_data(), built_at=time.time() - 3 * 86400))
        S._set_list("UN", dict(P.parse_un(FIX / "un_sample.xml"), built_at=time.time() - 3600))
        rows = {r["id"]: r for r in S.summary()["lists"]}
        self.assertEqual((rows["OFAC"]["stale"], rows["UN"]["stale"]), (True, False))   # over two days old

    def test_python_search(self):
        hits = S._search("mikati", 10)
        self.assertEqual([(h["kind"], h["id"]) for h in hits], [("sanctioned_vessel", "uk-RUS2321")])
        self.assertEqual(hits[0]["subtitle"], "Sanctioned vessel (UK), type unknown, IMO 9250892")
        self.assertEqual(S._search("Jal Vahini", 5)[0]["id"], "uk-DPR0075")             # aliases are searched
        self.assertEqual(S._search("9123453", 5)[0]["subtitle"], "Sanctioned vessel (UK, UN), type unknown, IMO 9123453")
        self.assertEqual(S._search("korolev", 5)[0]["subtitle"],
                         "Sanctioned vessel (OFAC, UK, EU), Crude Oil Tanker, IMO 9826902")
        self.assertEqual(S._search("ep-tst", 5)[0]["kind"], "sanctioned_aircraft")
        self.assertEqual(S._search("person", 10), [])      # individuals are never searchable
        # Owners and linked companies are not searched; an aircraft's operator is (as before).
        self.assertEqual([h["kind"] for h in S._search("example", 10)], ["sanctioned_aircraft"])
        self.assertEqual(S._search("ab", 10), [])


# ---------------------------------------------------------------------------------------------
# Registered module: routes, search, AI tool, status
# ---------------------------------------------------------------------------------------------
class _NoNet:
    def set_host_interval(self, host, seconds):
        pass

    def download(self, *a, **kw):
        raise AssertionError("no downloads in this test")


def make_app(tmp: str, net=None) -> tuple[App, object]:
    cfg = Config(env_file=None, overrides={"MERIDIAN_DATA_DIR": tmp})
    app = App(cfg, Store(Path(tmp) / "t.db"), net or _NoNet(), QUIET, Scheduler(QUIET), DatasetManager(QUIET))
    m = app.module("sanctions")
    S.register(m)
    return app, m


class RegisteredTests(Isolated):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="meridian-sanctions-")
        self.app, self.m = make_app(self.tmp)
        load_fixtures()

    def tearDown(self):
        self.app.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def get(self, path, **query):
        route, params, _ = self.app.match("GET", path)
        self.assertIsNotNone(route, path)
        return route.handler(Request("GET", path, params, {k: [str(v)] for k, v in query.items()}, {}))

    def test_declarations(self):
        for lid, sid, licence in (("OFAC", "sanctions.ofac_sdn", "US Government public domain"),
                                  ("UK", "sanctions.uk", "Open Government Licence v3.0"),
                                  ("UN", "sanctions.un", "UN terms of use"), ("EU", "sanctions.eu", "European Commission")):
            self.assertIn(sid, self.app.sources)
            self.assertIn(licence, self.app.sources[sid]["licence"])
            self.assertTrue(self.app.sources[sid]["refresh"].startswith("daily"))
        self.assertEqual(sorted(self.app.datasets.items),
                         ["sanctions.eu", "sanctions.sdn", "sanctions.uk", "sanctions.un"])
        self.assertTrue(all(d.max_age_hours == 24 for d in self.app.datasets.items.values()))

    def test_check(self):
        r = self.get("/api/sanctions/check", imo="9826902", hex="730abc")
        self.assertEqual(set(r), {"ready", "vessel", "aircraft", "lists"})
        self.assertTrue(r["ready"])
        self.assertEqual((r["vessel"]["uid"], r["aircraft"]["uid"]), ("10001", "10004"))
        self.assertEqual(r["lists"], ["OFAC", "UK", "EU"])
        r = self.get("/api/sanctions/check", imo="9123453")
        self.assertEqual((r["vessel"]["uid"], r["aircraft"], r["lists"]), ("uk-LIB0079", None, ["UK", "UN"]))
        r = self.get("/api/sanctions/check", mmsi="123456789")
        self.assertEqual((r["vessel"], r["aircraft"], r["lists"]), (None, None, []))
        r = self.get("/api/sanctions/check", name="mikati i")
        self.assertEqual((r["vessel"]["match"], r["vessel"]["uid"]), ("name", "uk-RUS2321"))
        self.assertIsNone(self.get("/api/sanctions/check", imo="9" * 500)["vessel"])

    def test_vessels_and_aircraft(self):
        r = self.get("/api/sanctions/vessels")
        self.assertTrue(r["ready"])
        self.assertEqual(len(r["vessels"]), 7)
        self.assertEqual(r["lists"], ["OFAC", "UK", "UN", "EU"])
        self.assertEqual(r["counts"], {"OFAC": 3, "UK": 5, "UN": 1, "EU": 2})
        uk = self.get("/api/sanctions/vessels", list="uk")
        self.assertEqual(sorted(v["uid"] for v in uk["vessels"]),
                         ["10001", "uk-DPR0075", "uk-DPR0120", "uk-LIB0079", "uk-RUS2321"])
        a = self.get("/api/sanctions/aircraft")
        self.assertEqual([x["uid"] for x in a["aircraft"]], ["10004"])
        self.assertEqual(self.get("/api/sanctions/aircraft", list="UN")["aircraft"], [])
        with self.assertRaises(HTTPError) as cm:
            self.get("/api/sanctions/vessels", list="mars")
        self.assertEqual((cm.exception.status, cm.exception.code), (400, "BAD_ARG"))
        json.dumps(r)                                                       # JSON-serialisable

    def test_lists_route(self):
        r = self.get("/api/sanctions/lists")
        self.assertEqual([x["id"] for x in r["lists"]], ["OFAC", "UK", "UN", "EU"])
        self.assertEqual(r["merged"]["vessels"], 7)
        self.assertTrue(all(x["last_error"] is None and x["last_attempt"] is None for x in r["lists"]))
        json.dumps(r)

    def test_search_through_the_app(self):
        hits = self.app.run_search("mikati", 10)
        self.assertEqual([(h["kind"], h["id"], h["layer"]) for h in hits], [("sanctioned_vessel", "uk-RUS2321", "ships")])

    def test_ai_tool(self):
        out = self.app.call_ai_tool("sanctions_lookup", {"imo": "9826902"})
        self.assertEqual(out["lists"], ["OFAC", "UK", "EU"])
        self.assertEqual(out["vessel"]["programmes"]["UK"], ["Russia"])
        self.assertEqual([c["list"] for c in out["checked"]], ["OFAC", "UK", "UN", "EU"])
        self.assertEqual(out["checked"][1]["published"], "2026-11-23")
        self.assertNotIn("note", out)
        weak = self.app.call_ai_tool("sanctions_lookup", {"vessel_name": "Petrel 8"})
        self.assertEqual(weak["vessel"]["match"], "name")
        self.assertIn("weak", weak["note"])
        none = self.app.call_ai_tool("sanctions_lookup", {"registration": "G-ABCD"})
        self.assertEqual((none["vessel"], none["aircraft"], none["lists"]), (None, None, []))
        json.dumps(out)

    def test_loading_a_build_file_sets_status(self):
        S._reset()
        self.m.data_dir.mkdir(parents=True, exist_ok=True)
        (self.m.data_dir / "uk.json").write_text(json.dumps(dict(P.parse_uk(FIX / "uk_sample.xml"), built_at=BUILT)),
                                                 encoding="utf-8")
        for fn in self.app.datasets.items["sanctions.uk"].listeners:
            fn()
        src = self.app.sources["sanctions.uk"]
        self.assertTrue(src["ok"])
        self.assertEqual(src["count"], 5)
        self.assertEqual(src["note"], "5 vessels, 5 not on the OFAC list; list of 2026-11-23")
        self.assertEqual(S.lookup_vessel(imo="9250892")["uid"], "uk-RUS2321")
        (self.m.data_dir / "un.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            for fn in self.app.datasets.items["sanctions.un"].listeners:
                fn()
        self.assertFalse(self.app.sources["sanctions.un"]["ok"])
        self.assertTrue(S.ready())                                          # the UK list is still in service


# ---------------------------------------------------------------------------------------------
# A failing list keeps its last good copy (real Net and DatasetManager, local HTTP server)
# ---------------------------------------------------------------------------------------------
def big_un_xml(n_people: int = 420) -> bytes:
    """The UN fixture padded past the build's size and completeness checks with fictional individuals."""
    text = (FIX / "un_sample.xml").read_text(encoding="utf-8")
    person = ("    <INDIVIDUAL><DATAID>{i}</DATAID><FIRST_NAME>TEST</FIRST_NAME><SECOND_NAME>PERSON {i}</SECOND_NAME>"
              "<UN_LIST_TYPE>DRC</UN_LIST_TYPE><LISTED_ON>2012-12-31</LISTED_ON><COMMENTS1>{pad}</COMMENTS1>"
              "</INDIVIDUAL>\n")
    people = "".join(person.format(i=8000000 + i, pad="Fictional padding. " * 40) for i in range(n_people))
    return text.replace("  </INDIVIDUALS>", people + "  </INDIVIDUALS>").encode("utf-8")


class _ListHost(BaseHTTPRequestHandler):
    def do_GET(self):
        spec = self.server.spec
        self.server.hits.append(dict(self.headers))
        etag = spec.get("etag")
        if etag and self.headers.get("If-None-Match") == etag and spec["status"] == 200:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        body = spec["body"]
        self.send_response(spec["status"])
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FailingListTests(Isolated):
    def setUp(self):
        super().setUp()
        S._reset()
        S._set_list("OFAC", ofac_data())
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _ListHost)
        self.srv.spec = {"status": 200, "body": big_un_xml(), "etag": '"v1"'}
        self.srv.hits = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        S.URLS["UN"] = f"http://127.0.0.1:{self.srv.server_address[1]}/consolidated.xml"
        self.tmp = tempfile.mkdtemp(prefix="meridian-sanctions-")
        cfg = Config(env_file=None, overrides={"MERIDIAN_DATA_DIR": self.tmp})
        self.app, self.m = make_app(self.tmp, Net(cfg, logging.getLogger("meridian.net")))
        self.raw = self.m.raw_dir / "un-consolidated.xml"
        self.out = self.m.data_dir / "un.json"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.app.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def build(self) -> bool:
        return self.app.datasets.build("sanctions.un")

    def age_raw_copy(self):
        old = time.time() - 2 * 86400
        os.utime(self.raw, (old, old))

    def test_first_build_conditional_refresh_and_failures(self):
        self.assertTrue(self.build())
        self.assertEqual(S.lookup_vessel(imo="9123453")["uid"], "un-9000003")
        self.assertTrue(self.app.sources["sanctions.un"]["ok"])
        # A day later the host answers 304 Not Modified: a cheap, successful refresh.
        self.age_raw_copy()
        self.assertTrue(self.build())
        self.assertEqual(self.srv.hits[-1].get("If-None-Match"), '"v1"')
        self.assertGreater(self.raw.stat().st_mtime, time.time() - 60)
        good = self.out.read_bytes()
        # Then the host fails: the build fails, the last good copy stays in service and on disk.
        self.age_raw_copy()
        self.srv.spec = {"status": 503, "body": b"down for maintenance"}
        with self.assertLogs("meridian.net", level="WARNING"):
            self.assertFalse(self.build())
        item = self.app.datasets.items["sanctions.un"]
        self.assertIn("HTTP 503", item.last_error)
        self.assertIn("keeping the UN Security Council list", item.last_error)
        self.assertEqual(self.out.read_bytes(), good)
        self.assertEqual(S.lookup_vessel(imo="9123453")["uid"], "un-9000003")
        self.assertEqual(S.lookup_vessel(imo="9826902")["uid"], "10001")      # other lists are untouched
        src = self.app.sources["sanctions.un"]
        self.assertFalse(src["ok"])
        self.assertIn("HTTP 503", src["error"])
        self.assertTrue(src["note"].startswith("Keeping the copy from"))
        row = next(r for r in S.summary(self.app.datasets)["lists"] if r["id"] == "UN")
        self.assertEqual((row["state"], row["loaded"], row["vessels"]), ("error", True, 1))
        # A broken file (too few designations) is refused the same way.
        self.srv.spec = {"status": 200, "body": (FIX / "un_sample.xml").read_bytes() + b" " * 400_000, "etag": '"v2"'}
        self.assertFalse(self.build())
        self.assertIn("looks incomplete", item.last_error)
        self.assertEqual(self.out.read_bytes(), good)
        self.assertTrue(S.lookup_vessel(imo="9123453"))
        # Once the host is back, the next build clears the error.
        self.age_raw_copy()
        self.srv.spec = {"status": 200, "body": big_un_xml(), "etag": '"v3"'}
        self.assertTrue(self.build())
        self.assertIsNone(item.last_error)
        self.assertTrue(self.app.sources["sanctions.un"]["ok"])

    def test_unreachable_before_any_copy(self):
        self.srv.spec = {"status": 503, "body": b"down"}
        self.assertFalse(self.build())
        self.assertFalse(self.out.exists())
        self.assertNotIn("UN", S._lists)
        src = self.app.sources["sanctions.un"]
        self.assertFalse(src["ok"])
        self.assertEqual(src["note"], "Not loaded yet.")
        self.assertTrue(S.ready())                                         # OFAC still serves lookups


# ---------------------------------------------------------------------------------------------
# The callers: ships matching and the flights card view, unchanged code on merged records
# ---------------------------------------------------------------------------------------------
class CallerTests(Isolated):
    def setUp(self):
        super().setUp()
        load_fixtures()

    def test_ships_flags_uk_listed_vessels(self):
        from meridian.modules.ships.service import ShipsService
        t0 = 1795380000.0
        svc = ShipsService(now=lambda: t0, lookup=S.lookup_vessel, log=QUIET)
        st = svc.store
        st.position(273000001, t0 - 30, 59.7, 24.2, sog=12.0, cog=90.0)
        st.static(273000001, t0 - 20, imo=9250892, name="MIKATI")           # UK only
        st.position(273000002, t0 - 30, 59.8, 24.3, sog=11.0, cog=90.0)
        st.static(273000002, t0 - 20, imo=9826902, name="KOROLEV PROSPECT")  # OFAC, UK and EU
        st.position(273000003, t0 - 30, 59.9, 24.4, sog=10.0, cog=90.0)
        st.static(273000003, t0 - 20, name="PETREL 8")                      # name only
        v1, v2, v3 = (st.vessels[n] for n in (273000001, 273000002, 273000003))
        self.assertTrue(v1.sanctioned)
        self.assertEqual((v1.sanc["programs"], v1.sanc["program"], v1.sanc["uid"]), (["UK: Russia"], "UK: Russia", "uk-RUS2321"))
        self.assertTrue(v2.sanctioned)
        self.assertEqual(v2.sanc["programs"], ["UKRAINE-EO13662", "RUSSIA-EO14024", "UK: Russia", "EU: RUS", "EU: UKR"])
        self.assertTrue(v3.possible_sanction and not v3.sanctioned)
        d = svc.vessel(273000001)
        self.assertEqual(d["sanctions_strength"], "confirmed")
        self.assertEqual(d["sanctions"]["programs"], ["UK: Russia"])
        listed = {x["mmsi"]: x for x in svc.sanctioned(include_possible=True)}
        self.assertEqual((listed["273000001"]["strength"], listed["273000003"]["strength"]), ("confirmed", "possible"))

    def test_flights_view_of_a_merged_aircraft(self):
        from meridian.modules.flights.service import FlightsService
        rec = S.lookup_aircraft(hex="730abc")
        view = FlightsService.sanctions_view(rec)
        self.assertEqual((view["uid"], view["programs"], view["registration"], view["match"]),
                         ("10004", ["IRAN", "SDGT"], "EP-TST", "Mode S transponder code"))


if __name__ == "__main__":
    unittest.main()
