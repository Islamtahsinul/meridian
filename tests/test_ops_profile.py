"""Tests for tools/profile.py: exporting and importing the user's own things (favourites, case files,
alerts watchlists, rules and settings, saved workspaces and places) between databases. Temporary folders,
no network.

Run: python -m unittest tests.test_ops_profile -v
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from meridian.modules.alerts import areas, db as adb
from meridian.modules.workspace import db as wdb, model as wmodel
from meridian.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = time.time()


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"ops_tool_{name}", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                     # dataclasses look their module up while it loads
    spec.loader.exec_module(mod)
    return mod


profile = load_tool("profile")


def populate(db: Path) -> None:
    """A database with the user's things in it, made through the app's own functions where they exist."""
    store = Store(db)
    adb.setup(store)
    store.execute("INSERT INTO favourites(kind, ref, title, subtitle, lat, lon, layer, payload, created_at) "
                  "VALUES('aircraft', '4ca2b3', 'RYR8NE', 'Boeing 737', 51.47, -0.45, 'flights', '{\"reg\": \"EI-DCL\"}', ?)",
                  (NOW - 5000,))
    store.execute("INSERT INTO favourites(kind, ref, title, created_at) VALUES('camera', 'tfl:1', 'Tower Bridge', ?)",
                  (NOW - 4000,))
    cid = store.execute("INSERT INTO casefiles(title, notes, created_at, updated_at) VALUES(?,?,?,?)",
                        ("Baltic watch", "Tankers off Primorsk.\nCheck daily.", NOW - 3000.123456, NOW - 100)).lastrowid
    for i, (kind, ref) in enumerate((("ship", "273456780"), ("port", "wpi:123"))):
        store.execute("INSERT INTO casefile_items(casefile_id, kind, ref, title, lat, lon, layer, payload, note, added_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)", (cid, kind, ref, f"Item {i}", 59.9, 29.0, "ships", "{}", "note", NOW - 2000 + i))
    circle = areas.parse_area({"type": "circle", "center": [24.9, 60.2], "radius_km": 80})
    adb.create_watchlist(store, name="Helsinki", area=circle, color="#ffb547",
                         rules=adb.clean_rules([{"preset": "quakes", "params": {"min_magnitude": 4.5}}, "ships_dark"]),
                         now=NOW - 1000)
    world = areas.parse_area({"type": "world"})
    wid = adb.create_watchlist(store, name="Everywhere", area=world, color=None,
                               rules=adb.clean_rules(["any"]), now=NOW - 900)
    with store.tx() as c:
        adb.insert_alert(c, NOW - 50, watchlist_id=wid, title="Something happened", reason="new")
    adb.save_settings(store, {"rate_limit": 20, "quiet_hours": {"on": True}}, NOW - 800)
    store.close()


def rows(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return [tuple(r) for r in con.execute(sql)]
    finally:
        con.close()


def tables(db: Path) -> list[str]:
    return [r[0] for r in rows(db, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]


def digest(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


def module_says(check, value) -> str:
    """The workspace module's own message for a value it refuses: the import must report exactly that."""
    try:
        check(value)
    except wmodel.InputError as err:
        return str(err)
    raise AssertionError(f"the workspace module accepts {value!r}")


def place_error(**change) -> str:
    return module_says(wmodel.clean_place, {"name": "Home", "type": "home", "lat": 51.5, "lon": -0.12, **change})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.src = self.base / "home" / "data"
        self.dst = self.base / "second" / "data"
        self.src.mkdir(parents=True)
        populate(self.src / "meridian.db")
        # the other computer: a Meridian that has started once there (the core's tables only, so the import
        # has to make the alerts and workspace tables itself)
        Store(self.dst / "meridian.db").close()
        self.file = self.base / "profile.json"
        self.env = mock.patch.dict(os.environ, {"MERIDIAN_ENV_FILE": "none", "MERIDIAN_DATA_DIR": str(self.base / "unused")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_tool(self, *argv, answer: str = ""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("builtins.input", side_effect=lambda prompt="": answer):
            code = profile.main([str(a) for a in argv])
        return code, out.getvalue(), err.getvalue()

    def export(self, data: Path | None = None) -> dict:
        code, out, err = self.run_tool("export", self.file, "--data-dir", data or self.src)
        self.assertEqual(code, 0, out + err)
        return json.loads(self.file.read_text(encoding="utf-8"))

    def imp(self, *extra, data: Path | None = None, answer: str = ""):
        return self.run_tool("import", self.file, "--data-dir", data or self.dst, *extra, answer=answer)

    def things(self, data: Path) -> dict:
        db = data / "meridian.db"
        return {
            "favourites": rows(db, "SELECT kind, ref, title, subtitle, lat, lon, layer, payload, created_at FROM favourites ORDER BY kind, ref"),
            "casefiles": rows(db, "SELECT title, notes, created_at FROM casefiles ORDER BY title"),
            "items": rows(db, "SELECT c.title, i.kind, i.ref, i.title, i.note, i.added_at FROM casefile_items i "
                              "JOIN casefiles c ON c.id = i.casefile_id ORDER BY i.kind, i.ref"),
            "watchlists": rows(db, "SELECT name, area, color, enabled, show_on_map, created_at FROM alerts_watchlists ORDER BY name"),
            "rules": rows(db, "SELECT w.name, r.preset, r.params, r.enabled FROM alerts_rules r "
                              "JOIN alerts_watchlists w ON w.id = r.watchlist_id ORDER BY w.name, r.preset"),
            # compared as values: the alerts module's own cleaning may order keys differently
            "settings": [(k, json.loads(v)) for k, v in rows(db, "SELECT k, v FROM alerts_settings ORDER BY k")],
        }


class ExportTests(Base):
    def test_export_shape_and_counts(self):
        doc = self.export()
        self.assertEqual((doc["format"], doc["schema"]), ("meridian-profile", 1))
        self.assertEqual(doc["counts"], {"favourites": 2, "casefiles": 1, "casefile_items": 2, "watchlists": 2,
                                         "rules": 3, "alert_settings": 2, "workspace_rows": 0})
        fav = [f for f in doc["favourites"] if f["kind"] == "aircraft"][0]
        self.assertEqual(fav["payload"], {"reg": "EI-DCL"})
        self.assertEqual(doc["casefiles"][0]["notes"], "Tankers off Primorsk.\nCheck daily.")
        wl = {w["name"]: w for w in doc["alerts"]["watchlists"]}
        self.assertEqual(wl["Helsinki"]["area"]["type"], "circle")
        self.assertEqual({r["preset"] for r in wl["Helsinki"]["rules"]}, {"quakes", "ships_dark"})
        self.assertEqual(doc["alerts"]["settings"]["rate_limit"], 20)
        self.assertNotIn("workspaces", doc)
        # nothing downloaded or derived goes into a profile
        text = self.file.read_text(encoding="utf-8")
        self.assertNotIn("Something happened", text)                  # fired alerts are not the user's things

    def test_default_file_goes_to_backups(self):
        code, out, _ = self.run_tool("export", "--data-dir", self.src)
        self.assertEqual(code, 0)
        made = list((self.src / "backups").glob("meridian-profile-*.json"))
        self.assertEqual(len(made), 1)
        self.assertIn("keep it private", out)

    def test_export_needs_a_database(self):
        code, _out, err = self.run_tool("export", self.file, "--data-dir", self.base / "nothing")
        self.assertEqual(code, 1)
        self.assertIn("no database", err)


class ImportTests(Base):
    def test_merge_into_a_new_computer_reproduces_everything(self):
        self.export()
        code, out, err = self.imp()
        self.assertEqual(code, 0, out + err)
        self.assertIn("Favourites: 2 added", out)
        mine, theirs = self.things(self.src), self.things(self.dst)
        for key in ("favourites", "casefiles", "items", "rules", "settings"):
            self.assertEqual(theirs[key], mine[key], key)
        self.assertEqual([w[:5] for w in theirs["watchlists"]], [w[:5] for w in mine["watchlists"]])
        # imported rules start from now and baseline first, so old events do not flood the inbox
        r = rows(self.dst / "meridian.db", "SELECT watch_from, baseline_at, checked_at FROM alerts_rules")
        self.assertTrue(all(w >= NOW and b is None and c is None for w, b, c in r))
        self.assertEqual(rows(self.dst / "meridian.db", "SELECT COUNT(*) FROM alerts_alerts"), [(0,)])

    def test_merging_twice_adds_nothing(self):
        self.export()
        self.imp()
        before = self.things(self.dst)
        code, out, _ = self.imp()
        self.assertEqual(code, 0)
        self.assertIn("Favourites: 0 added, 2 already here", out)
        self.assertIn("Case files: 0 added, 1 already here; 0 items added", out)
        self.assertIn("Watchlists: 0 added, 2 already here; 0 rules added", out)
        self.assertEqual(self.things(self.dst), before)

    def test_merge_adds_missing_items_and_rules_but_never_overwrites(self):
        self.export()
        self.imp()
        db = self.dst / "meridian.db"
        con = sqlite3.connect(str(db))
        con.execute("UPDATE favourites SET title = 'My own title' WHERE ref = 'tfl:1'")
        con.execute("DELETE FROM casefile_items WHERE kind = 'port'")
        con.execute("DELETE FROM alerts_rules WHERE preset = 'ships_dark'")
        con.execute("UPDATE alerts_settings SET v = '50' WHERE k = 'rate_limit'")
        con.execute("INSERT INTO favourites(kind, ref, title, created_at) VALUES('ship', 'only-here', 'Local', 1.8e9)")
        con.commit()
        con.close()
        code, out, _ = self.imp()
        self.assertEqual(code, 0)
        self.assertIn("1 item added", out)
        self.assertIn("1 rule added", out)
        t = self.things(self.dst)
        self.assertIn("My own title", [f[2] for f in t["favourites"]])
        self.assertIn("only-here", [f[1] for f in t["favourites"]])
        self.assertIn(("rate_limit", 50), t["settings"])
        self.assertEqual(len(t["items"]), 2)
        self.assertEqual(len(t["rules"]), 3)

    def test_replace_asks_saves_the_current_things_and_matches_the_profile(self):
        self.export()
        populate_other = Store(self.dst / "meridian.db")
        adb.setup(populate_other)
        populate_other.execute("INSERT INTO favourites(kind, ref, title, created_at) VALUES('ship', 'x', 'Old', 1.7e9)")
        wid = adb.create_watchlist(populate_other, name="Old area", area=areas.parse_area({"type": "world"}), color=None,
                                   rules=adb.clean_rules(["any"]), now=NOW - 10)
        with populate_other.tx() as c:
            adb.insert_alert(c, NOW, watchlist_id=wid, title="Old alert")
        populate_other.close()
        code, out, _ = self.imp("--mode", "replace", answer="no")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", out)
        self.assertIn(("ship", "x"), [f[:2] for f in self.things(self.dst)["favourites"]])
        code, out, err = self.imp("--mode", "replace", "--yes")
        self.assertEqual(code, 0, out + err)
        t, mine = self.things(self.dst), self.things(self.src)
        for key in ("favourites", "casefiles", "items", "rules", "settings"):
            self.assertEqual(t[key], mine[key], key)
        self.assertNotIn("Old area", [w[0] for w in t["watchlists"]])
        self.assertEqual(rows(self.dst / "meridian.db", "SELECT COUNT(*) FROM alerts_alerts"), [(0,)])   # its alerts went too
        saved = list((self.dst / "backups").glob("meridian-profile-before-import-*.json"))
        self.assertEqual(len(saved), 1)
        old = json.loads(saved[0].read_text(encoding="utf-8"))
        self.assertEqual([f["ref"] for f in old["favourites"]], ["x"])

    def test_dry_run_changes_nothing(self):
        self.export()
        db = self.dst / "meridian.db"
        before, schema = digest(db), tables(db)
        temp = self.base / "temp"
        temp.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(temp)):
            for mode in ("merge", "replace"):
                code, out, err = self.imp("--mode", mode, "--dry-run")
                self.assertEqual(code, 0, out + err)
                self.assertIn("Nothing was changed", out)
                self.assertIn("Favourites: 2 added", out)
                self.assertIn("Watchlists: 2 added", out)          # reported as if the alerts tables were there
        # not a byte of the database changed: no rows and no tables (this one has no alerts tables yet)
        self.assertEqual((digest(db), tables(db)), (before, schema))
        self.assertNotIn("alerts_watchlists", schema)
        self.assertFalse((self.dst / "backups").exists())             # no safety copy for a dry run
        self.assertEqual(list(temp.iterdir()), [])                    # the temporary copy is gone

    def test_a_data_folder_without_a_database_is_refused_unless_create_is_given(self):
        self.export()
        typo = self.base / "second" / "dta"                            # a mistyped --data-dir
        for extra in ((), ("--dry-run",), ("--mode", "replace", "--yes"), ("--mode", "replace", "--dry-run")):
            code, out, err = self.imp(*extra, data=typo)
            self.assertEqual(code, 1, extra)
            self.assertIn("There is no database at", err)
            self.assertIn("--create", err)
            self.assertNotIn("Nothing was changed.", out)
            self.assertFalse(typo.exists(), extra)                    # no folder, no database, nothing at all
        # --create with --dry-run reports the import into a new database, and still makes nothing
        code, out, err = self.imp("--create", "--dry-run", data=typo)
        self.assertEqual(code, 0, out + err)
        self.assertIn("into a new database", out)
        self.assertIn("Favourites: 2 added", out)
        self.assertFalse(typo.exists())
        # --create makes it, with the core, alerts and workspace tables the app would make
        code, out, err = self.imp("--create", data=typo)
        self.assertEqual(code, 0, out + err)
        self.assertIn("into a new database", out)
        mine, theirs = self.things(self.src), self.things(typo)
        for key in ("favourites", "casefiles", "items", "rules", "settings"):
            self.assertEqual(theirs[key], mine[key], key)
        self.assertTrue({"favourites", "alerts_watchlists", "workspace_saved", "workspace_places"}
                        <= set(tables(typo / "meridian.db")))

    def test_watchlist_limit_is_respected(self):
        doc = self.export()
        with mock.patch.object(adb, "MAX_WATCHLISTS", 3):
            self.imp()                                                  # 2 watchlists: fine
            doc["alerts"]["watchlists"] = [dict(w, created_at=w["created_at"] + 1, name=w["name"] + " 2")
                                           for w in doc["alerts"]["watchlists"]]
            self.file.write_text(json.dumps(doc), encoding="utf-8")
            code, _out, err = self.imp()
        self.assertEqual(code, 1)
        self.assertIn("at most 3", err)
        self.assertEqual(len(self.things(self.dst)["watchlists"]), 2)    # the whole import was rolled back


class ValidationTests(Base):
    def broken(self, change) -> tuple[int, str]:
        doc = self.export()
        bad = copy.deepcopy(doc)
        change(bad)
        self.file.write_text(json.dumps(bad), encoding="utf-8")
        db = self.dst / "meridian.db"
        before = digest(db)
        code, out, err = self.imp()
        self.assertEqual(digest(db), before, "a bad profile changed the database")
        return code, out + err

    def test_problems_are_listed_and_nothing_is_imported(self):
        cases = [
            (lambda d: d.update(format="something-else"), "not a Meridian profile"),
            (lambda d: d.update(schema=2), "newer Meridian"),
            (lambda d: d.update(schema="1"), "schema version"),
            (lambda d: d["favourites"][0].update(lat=123.0), "favourites[0]: lat and lon"),
            (lambda d: d["favourites"][0].update(kind=""), "favourites[0].kind: is required"),
            (lambda d: d["favourites"][0].update(title="x" * 301), "longer than 300"),
            (lambda d: d["favourites"][0].update(payload=[1, 2]), "payload: must be an object"),
            (lambda d: d["favourites"][0].update(created_at="yesterday"), "created_at: must be a time"),
            (lambda d: d["casefiles"][0]["items"][0].update(ref=None), "items[0].ref: is required"),
            (lambda d: d["casefiles"][0].update(notes=5), "notes: must be text"),
            (lambda d: d["alerts"]["watchlists"][0].update(area={"type": "circle", "center": [0, 0], "radius_km": -5}),
             "radius"),
            (lambda d: d["alerts"]["watchlists"][0].update(color="red"), "hex colour"),
            (lambda d: d["alerts"]["watchlists"][0]["rules"][0].update(params={"min_magnitude": "big"}), "rules[0]"),
            (lambda d: d["alerts"]["watchlists"][0].update(enabled="yes"), "must be true or false"),
            (lambda d: d["alerts"]["settings"].update(rate_limit=7), "rate_limit"),
            (lambda d: d.update(favourites={"not": "a list"}), "favourites: must be a list"),
        ]
        for change, words in cases:
            code, text = self.broken(change)
            self.assertEqual(code, 1, words)
            self.assertIn(words, text)
        self.file.write_text("{not json", encoding="utf-8")
        code, _out, err = self.imp()
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)
        code, _out, err = self.run_tool("import", self.base / "missing.json", "--data-dir", self.dst)
        self.assertIn("no file", err)

    def test_rules_and_settings_from_a_newer_meridian_are_skipped_with_a_note(self):
        doc = self.export()
        doc["alerts"]["watchlists"][0]["rules"].append({"preset": "future_rule", "params": {}, "enabled": True})
        doc["alerts"]["settings"]["future_setting"] = {"x": 1}
        self.file.write_text(json.dumps(doc), encoding="utf-8")
        code, out, err = self.imp()
        self.assertEqual(code, 0, out + err)
        self.assertIn("Rules this Meridian does not know were skipped: future_rule", out)
        self.assertIn("Alert settings this Meridian does not know were skipped: future_setting", out)
        self.assertEqual(len(self.things(self.dst)["rules"]), 3)

    def test_validate_cleans_areas_through_the_alerts_module(self):
        doc = self.export()
        prof = profile.validate(doc)
        wl = {w["name"]: w for w in prof["watchlists"]}
        stored = json.loads(wl["Helsinki"]["area"])
        self.assertEqual(stored, areas.parse_area(stored).to_json())
        self.assertEqual(json.loads(wl["Helsinki"]["rules"][0]["params"]), {"min_magnitude": 4.5})


class WorkspaceTests(Base):
    """Saved workspaces and My places, made and read back through the workspace module's own functions."""

    def add_workspaces(self, db: Path) -> None:
        store = Store(db)
        wdb.setup(store)
        wdb.create_workspace(store, "Baltic", {"v": 1, "view": {"center": [20, 58], "zoom": 5}, "basemap": "dark",
                                               "layers": {"ships": {"on": True}}}, NOW - 600)
        wdb.create_workspace(store, "Gulf", {"v": 1, "projection": "globe", "notes": "Hormuz"}, NOW - 500)
        home = wdb.create_place(store, {"name": "Home", "type": "home", "lat": 51.5, "lon": -0.12, "radius_km": 30},
                                NOW - 400)
        wdb.create_place(store, {"name": "Office", "type": "work", "lat": 53.48, "lon": -2.24, "notes": "Desk 4"},
                         NOW - 300)
        wdb.set_place_watchlist(store, home, store.scalar("SELECT id FROM alerts_watchlists WHERE name = 'Everywhere'"),
                                NOW - 200)
        store.close()

    def app_view(self, data: Path) -> dict:
        """What the workspace module serves, with each place's watchlist link shown as the watchlist's name."""
        store = Store(data / "meridian.db")
        try:
            names = {r["id"]: r["name"] for r in store.query("SELECT id, name FROM alerts_watchlists")}
            places = [dict(p, alerts_watchlist_id=names.get(p["alerts_watchlist_id"])) for p in wdb.list_places(store)]
            return {"saved": wdb.list_workspaces(store), "places": places}
        finally:
            store.close()

    def test_workspaces_and_places_travel_into_a_meridian_that_has_none(self):
        self.add_workspaces(self.src / "meridian.db")
        doc = self.export()
        self.assertEqual(doc["counts"]["workspace_rows"], 4)
        # the other computer has no workspace tables yet: the import makes them with the module's own setup
        self.assertNotIn("workspace_places", tables(self.dst / "meridian.db"))
        code, out, err = self.imp()
        self.assertEqual(code, 0, out + err)
        self.assertIn("Workspaces: 4 rows added", out)
        self.assertNotIn("no workspace", out)
        mine = self.app_view(self.src)
        self.assertEqual(self.app_view(self.dst), mine)
        self.assertEqual(mine["places"][0]["alerts_watchlist_id"], "Everywhere")
        # merging into workspaces that already exist is left alone; replace takes the profile's
        code, out, _ = self.imp()
        self.assertIn("already has workspaces", out)
        con = sqlite3.connect(str(self.dst / "meridian.db"))
        con.execute("UPDATE workspace_places SET name = 'Changed here'")
        con.commit()
        con.close()
        code, out, _ = self.imp("--mode", "replace", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("alert settings, saved workspaces and places, and puts", out)   # replace says what it removes
        self.assertEqual(self.app_view(self.dst), mine)

    def test_a_dry_run_reports_workspaces_for_a_database_without_their_tables(self):
        self.add_workspaces(self.src / "meridian.db")
        self.export()
        db = self.dst / "meridian.db"
        before = digest(db)
        code, out, err = self.imp("--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Workspaces: 4 rows added", out)
        self.assertNotIn("no workspace", out)
        self.assertEqual(digest(db), before)
        self.assertNotIn("workspace_places", tables(db))

    def test_links_to_watchlists_follow_their_new_ids(self):
        store = Store(self.src / "meridian.db")
        wdb.setup(store)
        old_id = store.scalar("SELECT id FROM alerts_watchlists WHERE name = 'Everywhere'")
        for name, lat, link in (("Home", 51.5, old_id), ("Orphan", 1, 999), ("Unlinked", 2, None)):
            pid = wdb.create_place(store, {"name": name, "lat": lat, "lon": 0}, NOW)
            if link:
                wdb.set_place_watchlist(store, pid, link, NOW)
        store.close()
        self.export()
        store = Store(self.dst / "meridian.db")                          # this PC already has a watchlist,
        adb.setup(store)                                                 # so the imported ones get other ids
        adb.create_watchlist(store, name="Local", area=areas.parse_area({"type": "world"}), color=None, rules=[], now=NOW)
        store.close()
        code, out, err = self.imp()
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 place lost its link to a watchlist", out)
        dst = self.dst / "meridian.db"
        new_id = rows(dst, "SELECT id FROM alerts_watchlists WHERE name = 'Everywhere'")[0][0]
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(rows(dst, "SELECT name, alerts_watchlist_id FROM workspace_places ORDER BY id"),
                         [("Home", new_id), ("Orphan", None), ("Unlinked", None)])

    def test_the_review_profile_is_refused_and_nothing_is_imported(self):
        # 300 places, one with a 5,000-character name, lat 999, lon -500, an unknown type and a negative radius,
        # and a saved workspace whose state is not JSON: before the fix all of it went in raw
        self.add_workspaces(self.src / "meridian.db")
        doc = self.export()
        spec = doc["workspaces"]["tables"]["workspace_places"]
        first = dict(zip(spec["columns"], spec["rows"][0]))
        places = []
        for i in range(300):
            place = dict(first, id=i + 1, name=f"Place {i}", alerts_watchlist_id=None)
            if i == 0:
                place.update(name="x" * 5000, lat=999, lon=-500, type="not-a-type", radius_km=-5)
            places.append([place[c] for c in spec["columns"]])
        spec["rows"] = places
        saved = doc["workspaces"]["tables"]["workspace_saved"]
        broken = dict(zip(saved["columns"], saved["rows"][0]), id=99, name="Broken", state="{not json")
        saved["rows"].append([broken[c] for c in saved["columns"]])
        self.file.write_text(json.dumps(doc), encoding="utf-8")
        db = self.dst / "meridian.db"
        before = digest(db)
        code, out, err = self.imp("--mode", "replace", "--yes")
        self.assertEqual(code, 1, out + err)
        self.assertIn("nothing was imported", err)
        self.assertIn(f"workspaces.tables.workspace_places: Meridian keeps at most {wdb.MAX_PLACES} places", err)
        self.assertIn("workspaces.tables.workspace_saved.rows[2]: The state is not valid JSON.", err)
        self.assertEqual(digest(db), before)
        # within the limit, the bad place itself is refused for its type, position and radius in turn
        spec["rows"] = places[:wdb.MAX_PLACES]
        cols = spec["columns"]
        for fix, words in ((("type",), place_error(type="not-a-type")), (("lat", "lon"), place_error(lat=999, lon=-500)),
                           (("radius_km",), place_error(radius_km=-5))):
            with self.assertRaises(profile.ProfileError) as cm:
                profile.validate(doc)
            self.assertIn("workspaces.tables.workspace_places.rows[0]: " + words, cm.exception.problems)
            for column in fix:                                           # put it right, then find the next one
                spec["rows"][0][cols.index(column)] = first[column]
        saved["rows"].pop()                                              # and the state that is not JSON
        # only the long name is left, which the app cuts to its limit rather than refusing
        self.assertEqual(len(profile.validate(doc)["workspaces"]["workspace_places"]["rows"][0][1]), wmodel.NAME_MAX)

    def test_rows_are_checked_with_the_workspace_modules_own_rules(self):
        self.add_workspaces(self.src / "meridian.db")
        doc = self.export()

        def cell(table, column, value, row=0):
            def change(d):
                spec = d["workspaces"]["tables"][table]
                spec["rows"][row][spec["columns"].index(column)] = value
            return change

        def table(name, **changes):
            return lambda d: d["workspaces"]["tables"][name].update(changes)

        many = doc["workspaces"]["tables"]["workspace_saved"]["rows"] * (wdb.MAX_WORKSPACES // 2 + 1)
        for change, words in [
            (cell("workspace_places", "lon", -500), "rows[0]: " + place_error(lon=-500)),
            (cell("workspace_places", "name", ""), "rows[0]: " + place_error(name="")),
            (cell("workspace_places", "name", {"$base64": "AP8="}), "rows[0]: " + place_error(name={"$base64": "AP8="})),
            (cell("workspace_places", "type", None), "rows[0]: " + place_error(type=None)),
            (cell("workspace_places", "alerts_watchlist_id", "abc"), "alerts_watchlist_id: must be a watchlist id or null"),
            (cell("workspace_places", "id", 2), "rows[1].id: repeats the id 2"),
            (cell("workspace_places", "id", "seven"), "rows[0].id: must be a whole number"),
            (cell("workspace_places", "created_at", "yesterday"), "rows[0].created_at: must be a time"),
            (cell("workspace_saved", "state", "{not json"), "rows[0]: The state is not valid JSON"),
            (cell("workspace_saved", "state", json.dumps({"v": 2})), "rows[0]: " + module_says(wmodel.clean_state, {"v": 2})),
            (cell("workspace_saved", "state", "[1, 2]"), "rows[0]: " + module_says(wmodel.clean_state, [1, 2])),
            (cell("workspace_saved", "name", "GULF"), 'rows[1].name: repeats the workspace name "Gulf"'),
            (table("workspace_saved", rows=many), f"Meridian keeps at most {wdb.MAX_WORKSPACES} saved workspaces"),
            (lambda d: d["workspaces"]["tables"]["workspace_saved"]["rows"].append([1]), "rows[2]: must have 5 values"),
            (table("workspace_saved", columns=["id; DROP"]), "workspace_saved: needs a list of column names"),
            (lambda d: d["workspaces"]["tables"].update({"sqlite_master": {"columns": ["x"], "rows": []}}),
             "not a workspace table"),
        ]:
            bad = copy.deepcopy(doc)
            change(bad)
            with self.assertRaises(profile.ProfileError) as cm:
                profile.validate(bad)
            self.assertTrue(any(words in p for p in cm.exception.problems), (words, cm.exception.problems))
        # what the app cleans rather than refuses is cleaned the same way: control characters go, runs of
        # spaces close up and a long name is cut to the app's limit
        ok = copy.deepcopy(doc)
        long_name = "  Home \t base\x1b " + "x" * 5000
        cell("workspace_places", "name", long_name)(ok)
        name = profile.validate(ok)["workspaces"]["workspace_places"]["rows"][0][1]
        self.assertEqual(name, wmodel.clean_name(long_name))
        self.assertTrue(name.startswith("Home base x"))
        self.assertEqual(len(name), wmodel.NAME_MAX)

    def test_tables_and_columns_this_meridian_does_not_know_are_skipped(self):
        self.add_workspaces(self.src / "meridian.db")
        made_up = "CREATE TABLE workspace_items (id INTEGER PRIMARY KEY, name TEXT, icon BLOB)"
        con = sqlite3.connect(str(self.src / "meridian.db"))
        con.execute(made_up)
        con.execute("INSERT INTO workspace_items(name, icon) VALUES('Baltic', ?)", (bytes([0, 255]),))
        con.commit()
        con.close()
        doc = self.export()
        self.assertEqual(doc["workspaces"]["tables"]["workspace_items"]["rows"][0][2], {"$base64": "AP8="})
        spec = doc["workspaces"]["tables"]["workspace_places"]
        spec["columns"].append("colour")                                 # as if from a newer Meridian
        for r in spec["rows"]:
            r.append("#ff0000")
        self.file.write_text(json.dumps(doc), encoding="utf-8")
        con = sqlite3.connect(str(self.dst / "meridian.db"))             # a table of that name here is not written
        con.execute(made_up)
        con.commit()
        con.close()
        code, out, err = self.imp()
        self.assertEqual(code, 0, out + err)
        self.assertIn("Workspace tables this Meridian does not know were skipped: workspace_items.", out)
        self.assertIn("Workspace columns this Meridian does not know were skipped: workspace_places.colour.", out)
        self.assertIn("Workspaces: 4 rows added", out)
        self.assertEqual(rows(self.dst / "meridian.db", "SELECT COUNT(*) FROM workspace_items"), [(0,)])
        self.assertEqual(self.app_view(self.dst), self.app_view(self.src))

    def test_without_the_workspace_module_the_rest_is_still_imported(self):
        self.add_workspaces(self.src / "meridian.db")
        self.export()
        with mock.patch.object(profile, "_workspace_code", side_effect=ImportError("no workspace module")):
            code, out, err = self.imp()
        self.assertEqual(code, 0, out + err)
        self.assertIn("Workspaces were skipped: the workspace module cannot be loaded here", out)
        self.assertIn("Favourites: 2 added", out)
        self.assertEqual(rows(self.dst / "meridian.db", "SELECT COUNT(*) FROM workspace_places"), [(0,)])


class HelpTests(unittest.TestCase):
    def test_help(self):
        for argv in (["--help"], ["export", "--help"], ["import", "--help"]):
            with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
                profile.main(argv)
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("usage", out.getvalue())


if __name__ == "__main__":
    unittest.main()
