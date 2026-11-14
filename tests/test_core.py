"""Geometry, receiver parsing, tracker, history and settings (no plugin object)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

import util  # noqa: F401  (sets sys.path)
from fakes import HOME, aircraft_doc, receiver_doc, stats_doc

pkg = util.load_package()
geo, rx, history, settings = pkg.geo, pkg.receiver, pkg.history, pkg.settings


class GeoTest(unittest.TestCase):
    def test_haversine_known_distances(self):
        # One degree of latitude is ~111.2 km everywhere.
        self.assertAlmostEqual(geo.haversine_km(50, 8, 51, 8), 111.19, delta=0.05)
        # One degree of longitude at 50 N is ~71.5 km.
        self.assertAlmostEqual(geo.haversine_km(50, 8, 50, 9), 71.47, delta=0.1)
        self.assertEqual(geo.haversine_km(50, 8, 50, 8), 0.0)
        # Antipodes: half the circumference.
        self.assertAlmostEqual(geo.haversine_km(0, 0, 0, 180), 20015.1, delta=1)

    def test_bearing(self):
        self.assertAlmostEqual(geo.bearing_deg(50, 8, 51, 8), 0, delta=0.01)
        self.assertAlmostEqual(geo.bearing_deg(50, 8, 50, 9), 89.6, delta=0.2)
        self.assertAlmostEqual(geo.bearing_deg(50, 8, 49, 8), 180, delta=0.01)
        self.assertAlmostEqual(geo.bearing_deg(50, 8, 50, 7), 270.4, delta=0.2)

    def test_tiles(self):
        self.assertEqual(geo.lonlat_to_tile(0, 0, 1), (1.0, 1.0))
        x, y = geo.lonlat_to_tile(HOME[1], HOME[0], 8)
        self.assertEqual((int(x), int(y)), (133, 86))


class ParseTest(unittest.TestCase):
    def test_aircraft_fields(self):
        a = rx.parse_aircraft(aircraft_doc()["aircraft"][0])
        self.assertEqual(a["hex"], "aa0001")
        self.assertEqual(a["flight"], "TEST1")
        self.assertEqual((a["alt"], a["gs"], a["track"], a["vr"]), (36000, 450, 90, -64))
        self.assertEqual((a["squawk"], a["cat"], a["type"], a["reg"]), ("1000", "A3", "A320", "X-TEST"))
        self.assertEqual(a["src"], "adsb")

    def test_aircraft_variants(self):
        docs = aircraft_doc()["aircraft"]
        self.assertEqual(rx.parse_aircraft(docs[1])["src"], "mlat")
        ground = rx.parse_aircraft(docs[2])
        self.assertTrue(ground["ground"])
        self.assertNotIn("lat", ground)
        self.assertEqual(rx.parse_aircraft(docs[3])["hex"], "~bb0004")
        self.assertNotIn("lat", rx.parse_aircraft(docs[5]))       # stale position
        self.assertIsNone(rx.parse_aircraft(docs[6]))
        self.assertIsNone(rx.parse_aircraft(docs[7]))

    def test_hostile_values_are_dropped(self):
        a = rx.parse_aircraft({"hex": "AA0009", "flight": "<b>x</b>‮", "lat": "50", "lon": 8,
                               "alt_baro": float("nan"), "gs": 1e9, "squawk": "12345",
                               "t": {"x": 1}, "track": -5})
        self.assertEqual(a["hex"], "aa0009")
        self.assertEqual(a["flight"], "<b>x</b>"[:10])          # text only, frame uses textContent
        for key in ("lat", "alt", "gs", "squawk", "type", "track"):
            self.assertNotIn(key, a)

    def test_stats(self):
        s = rx.parse_stats(stats_doc())
        self.assertEqual(s["signal"], -12.3)
        self.assertEqual(s["noise"], -35.1)
        self.assertEqual(s["strong_signals"], 7)
        self.assertEqual(s["gain_db"], 43.9)
        self.assertEqual(s["estimated_ppm"], -1.2)
        self.assertEqual(s["msg_rate_1min"], 100.0)
        self.assertEqual(s["max_distance_km_15min"], 123.5)
        self.assertEqual(s["tracks_15min"], 42)
        self.assertEqual(rx.parse_stats({"last1min": "x", "gain_db": "y"}), {})

    def test_receiver(self):
        self.assertEqual(rx.parse_receiver(receiver_doc())["lat"], HOME[0])
        self.assertEqual(rx.parse_receiver({"lat": 0, "lon": 0}), {})


class TrackerTest(unittest.TestCase):
    def test_counts_distance_and_rate(self):
        t = rx.Tracker()
        snap = t.update(aircraft_doc(1000.0, 1000), 1000.0, HOME, 300)
        self.assertEqual(snap["total"], 6)                  # 8 entries, 1 invalid hex, 1 not a dict
        self.assertEqual(snap["with_pos"], 2)               # bogus + stale positions dropped
        self.assertEqual(snap["mlat"], 1)
        self.assertIsNone(snap["msg_rate"])
        by = {a["hex"]: a for a in snap["aircraft"]}
        self.assertAlmostEqual(by["aa0001"]["dist"], 55.6, delta=0.1)
        self.assertEqual(by["aa0001"]["brg"], 0)
        self.assertEqual(by["aa0002"]["brg"], 90)
        self.assertNotIn("lat", by["aa0005"])
        snap = t.update(aircraft_doc(1005.0, 1500), 1005.0, HOME, 300)
        self.assertEqual(snap["msg_rate"], 100.0)
        snap = t.update(aircraft_doc(1010.0, 10), 1010.0, HOME, 300)   # counter reset
        self.assertIsNone(snap["msg_rate"])

    def test_trails_are_bounded_and_pruned(self):
        t = rx.Tracker()
        doc = aircraft_doc()
        for i in range(500):
            doc["aircraft"][0]["lat"] = 50.0 + i * 0.001
            t.update(doc, 1000.0 + i, HOME, 10_000)
        self.assertEqual(len(t.trails["aa0001"]), rx.MAX_TRAIL_POINTS)
        # Unchanged positions add no points.
        n = len(t.trails["aa0002"])
        self.assertEqual(n, 1)
        payload = t.trails_payload({"aa0001", "aa0002"})
        self.assertIn("aa0001", payload)
        self.assertNotIn("aa0002", payload)                 # single point: no line
        # Short trail window: old points go, then the aircraft goes.
        t.update(doc, 20_000.0, HOME, 60)
        self.assertEqual(len(t.trails["aa0001"]), 1)
        t.update({"now": 1, "aircraft": []}, 20_100.0, HOME, 60)
        self.assertEqual(t.trails, {})
        t.update(doc, 20_200.0, HOME, 0)
        self.assertEqual(t.trails, {})

    def test_no_home_no_distance(self):
        snap = rx.Tracker().update(aircraft_doc(), 1.0, None, 0)
        self.assertTrue(all("dist" not in a for a in snap["aircraft"]))
        self.assertEqual(snap["with_pos"], 3)               # bogus one is kept without a home


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "history.json")

    def snap(self, now, aircraft=None, rate=50.0):
        t = rx.Tracker()
        doc = aircraft_doc(now)
        if aircraft is not None:
            doc["aircraft"] = aircraft
        s = t.update(doc, now, HOME, 0)
        s["msg_rate"] = rate
        return s

    def test_minute_rows_and_records(self):
        h = history.History(self.path)
        base = 1_700_000_040.0 - 1_700_000_040.0 % 60
        h.add_snapshot(self.snap(base + 5, rate=40), base + 5, 14)
        h.add_snapshot(self.snap(base + 35, rate=60), base + 35, 14)
        self.assertEqual(h.minutes, [])
        h.add_snapshot(self.snap(base + 65), base + 65, 14)       # next minute closes the first
        self.assertEqual(len(h.minutes), 1)
        row = h.minutes[0]
        self.assertEqual(row[history.T], base)
        self.assertEqual(row[history.AC], 6.0)
        self.assertEqual(row[history.POS], 2.0)
        self.assertEqual(row[history.MLAT], 1.0)
        self.assertEqual(row[history.MPS], 50.0)
        self.assertAlmostEqual(row[history.RANGE], 71.5, delta=0.1)
        self.assertEqual(row[history.UNIQ], 6)
        day = h.today_stats(base + 65)
        self.assertEqual(day["unique"], 6)
        self.assertEqual(day["furthest"]["hex"], "aa0002")
        self.assertEqual(day["highest"]["ft"], 36000)
        self.assertEqual(day["fastest"]["kt"], 450)
        self.assertEqual(day["fastest"]["flight"], "TEST1")
        # flush closes an expired minute without a new sample
        h.flush(base + 200, 14)
        self.assertEqual(len(h.minutes), 2)

    def test_atomic_save_and_load(self):
        h = history.History(self.path)
        now = time.time()
        h.add_snapshot(self.snap(now), now, 14)
        h.flush(now + 120, 14)
        h.save()
        self.assertFalse([f for f in os.listdir(self.dir) if ".tmp" in f])
        with open(self.path) as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["minutes"]), 1)
        h2 = history.History(self.path)
        h2.load()
        self.assertEqual(h2.minutes, h.minutes)
        self.assertEqual(h2.today_hex, h.today_hex)
        self.assertEqual(h2.days, h.days)

    def test_atomic_write_keeps_old_file_on_error(self):
        history.atomic_write_json(self.path, {"a": 1})
        with self.assertRaises(TypeError):
            history.atomic_write_json(self.path, {"a": object()})
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertEqual(os.listdir(self.dir), ["history.json"])

    def test_corrupt_file_is_moved_aside(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        h = history.History(self.path)
        h.load()
        self.assertEqual(h.minutes, [])
        self.assertTrue(os.path.exists(self.path + ".corrupt"))

    def test_invalid_rows_are_ignored(self):
        with open(self.path, "w") as f:
            json.dump({"minutes": [[1, 2, 3], [0, 1, 1, 0, 5.0, 10.0, 3], "x", [0, "a", 1, 1, 1, 1, 1]],
                       "hours": None, "days": {"bad": {}, "2020-01-01": {"unique": 3}}}, f)
        h = history.History(self.path)
        h.load()
        self.assertEqual(h.minutes, [[0, 1, 1, 0, 5.0, 10.0, 3]])
        self.assertEqual(list(h.days), ["2020-01-01"])

    def test_prune_rolls_minutes_into_hours_and_drops_old_days(self):
        h = history.History(self.path)
        now = 1_700_000_000.0 - 1_700_000_000.0 % 3600
        # 4 days of one row per minute
        for i in range(4 * 1440):
            t = now - 4 * 86400 + i * 60
            h.minutes.append([int(t), 10.0 + (i % 2), 5.0, 1.0, 100.0, float(i % 100), 3])
        for d in range(20):
            h.days[history.local_day(now - d * 86400)] = {"unique": d}
        h.prune(now, 3)
        self.assertTrue(all(r[history.T] >= now - history.MINUTE_KEEP_S for r in h.minutes))
        self.assertEqual(len(h.minutes), 2 * 1440)
        # 2 days rolled up, but only the last 3 days (+1 h) are kept
        self.assertTrue(h.hours)
        self.assertTrue(all(r[history.T] >= now - 3 * 86400 - 3600 for r in h.hours))
        full = [r for r in h.hours if r[history.N] == 60]
        self.assertTrue(full)
        self.assertEqual(full[0][history.AC], 10.5)
        self.assertEqual(full[0][history.RANGE], 99.0)
        self.assertLessEqual(len(h.days), 4)
        # Folding twice into the same hour stays a weighted average.
        h2 = history.History(self.path)
        h2._fold_into_hours([[3600, 10.0, 0, 0, 0, 1.0, 1]])
        h2._fold_into_hours([[3660, 20.0, 0, 0, 0, 5.0, 1], [3720, 30.0, 0, 0, 0, 2.0, 1]])
        self.assertEqual(h2.hours, [[3600, 20.0, 0, 0, 0, 5.0, 3]])

    def test_series_downsampling(self):
        h = history.History(self.path)
        start = 1_700_000_000 - 1_700_000_000 % 3600
        for i in range(120):
            h.minutes.append([start + i * 60, float(i), 1.0, 0.0, 10.0, float(i), 1])
        s = h.series(start, start + 7200, 300)
        self.assertEqual(len(s), 24)
        self.assertEqual(s[0], [start, 2.0, 1.0, 10.0, 4.0])
        self.assertEqual(s[-1][4], 119.0)
        self.assertEqual(h.series(start + 7200, start + 9000, 300), [])

    def test_best_records_and_day_list(self):
        h = history.History(self.path)
        h.days = {"2026-01-01": {"unique": 5, "furthest": {"km": 100.0, "hex": "aa0001", "t": 1}},
                  "2026-01-02": {"unique": 9, "furthest": {"km": 250.0, "hex": "aa0002", "t": 2},
                                 "highest": {"ft": "x"}}}
        best = h.best_records()
        self.assertEqual(best["furthest"]["day"], "2026-01-02")
        self.assertNotIn("highest", best)
        self.assertEqual([d["day"] for d in h.day_list()], ["2026-01-02", "2026-01-01"])


class SettingsTest(unittest.TestCase):
    def test_effective_defaults(self):
        s = settings.effective({})
        self.assertEqual(s["data_url"], "")
        self.assertEqual(s["poll_seconds"], 5)
        self.assertEqual(s["rings"], [50, 100, 150, 200])
        self.assertIsNone(s["lat"])
        self.assertTrue(s["show_pill"])
        self.assertEqual(s["links"], [])

    def test_effective_coerces_bad_values(self):
        s = settings.effective({
            "data_url": "file:///etc/passwd", "poll_seconds": 0.5, "lat": "50,5", "lon": "x",
            "range_rings_km": "10, 300, -1, abc, 10, 5000", "tile_provider": "Evil",
            "tile_url": "javascript:alert(1)", "start_zoom": 99, "trail_minutes": "7",
            "history_days": None, "show_pill": "false",
            "links": [{"label": "A", "url": "https://a.example/"}, {"url": "ftp://x"}, "x",
                      {"label": "", "url": "http://b.example:8080/x", "probe": 1}]})
        self.assertEqual(s["data_url"], "")
        self.assertEqual(s["poll_seconds"], 2)
        self.assertIsNone(s["lat"])                       # lon invalid -> both unset
        self.assertEqual(s["rings"], [10, 300])
        self.assertEqual(s["tile_provider"], "OpenStreetMap")
        self.assertEqual(s["tile_url"], "")
        self.assertEqual((s["start_zoom"], s["trail_minutes"], s["history_days"]), (12, 7, 14))
        self.assertFalse(s["show_pill"])
        self.assertEqual(s["links"], [
            {"label": "A", "url": "https://a.example/", "probe": False},
            {"label": "b.example", "url": "http://b.example:8080/x", "probe": True}])
        self.assertEqual(settings.effective({"lat": "50.5", "lon": "8"})["lat"], 50.5)

    def test_links_capped(self):
        links = [{"label": str(i), "url": f"https://x{i}.example/"} for i in range(50)]
        self.assertEqual(len(settings.effective({"links": links})["links"]), settings.MAX_LINKS)

    def test_migrate_fills_missing_keys(self):
        cfg = settings.migrate("0.9.0", {"data_url": "http://r.example/data", "links": "bad",
                                         "extra": 1})
        self.assertEqual(cfg["data_url"], "http://r.example/data")
        self.assertEqual(cfg["links"], [])
        self.assertEqual(cfg["poll_seconds"], 5)
        self.assertEqual(cfg["extra"], 1)                 # unknown keys are left alone
        self.assertEqual(settings.migrate("", None)["tile_provider"], "OpenStreetMap")
        self.assertEqual(settings.version_tuple("1.2.3-beta"), (1, 2, 3))

    def test_shipped_config_matches_defaults(self):
        with open(os.path.join(util.PLUGIN_SRC, "config.json")) as f:
            self.assertEqual(json.load(f), settings.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
