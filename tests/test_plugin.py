"""The plugin object end to end: poller against a fake receiver, routes,
providers, tiles (fake tile server only), links, schema and frame files."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import unittest

import util
from fakes import HOME, FakeReceiver, FakeSite, FakeTiles, png

try:                                    # the real validators, when PIHUB_CORE is set
    from pi_hub.plugins import contrib as core_contrib
    if not hasattr(core_contrib, "validate_node"):
        core_contrib = None
except ImportError:
    core_contrib = None


def make_plugin(config: dict):
    mod, tmp = util.temp_plugin()
    p = mod.AdsbPlugin()
    cfg = mod.settings.migrate("", config)
    p.load(util.FakeCtx(cfg))
    return mod, p, tmp


def validate_node(node):
    if core_contrib is not None:
        return core_contrib.validate_node(node)
    json.dumps(node)
    return node


class PollerTest(unittest.TestCase):
    def setUp(self):
        self.rx = FakeReceiver()
        self.mod, self.p, self.tmp = make_plugin({"data_url": self.rx.data_url})

    def tearDown(self):
        self.p.unload()
        self.rx.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_poll_and_live_route(self):
        self.p.tick()
        live = self.p.route_live({})
        self.assertEqual(live["state"], "ok")
        self.assertEqual(live["counts"], {"total": 6, "with_pos": 2, "mlat": 1, "msg_rate": None})
        self.assertEqual(live["home"], {"lat": HOME[0], "lon": HOME[1], "source": "receiver"})
        self.assertEqual(live["rings"], [50, 100, 150, 200])
        self.assertEqual(live["receiver_version"], "readsb test build")
        by = {a["hex"]: a for a in live["aircraft"]}
        self.assertAlmostEqual(by["aa0001"]["dist"], 55.6, delta=0.1)
        self.assertEqual(self.rx.hits, {"receiver.json": 1, "aircraft.json": 1, "stats.json": 1})
        self.assertTrue(all(a.startswith("pi-hub-plugin-adsb/1.0.0 ") for a in self.rx.agents))

    def test_interval_from_config_and_live_reconfigure(self):
        self.p.tick()
        self.p.tick()                                    # not due yet
        self.assertEqual(self.rx.hits["aircraft.json"], 1)
        cfg = dict(self.p.ctx.get_config(), poll_seconds=2, lat="50.5", lon="8.5")
        self.p.on_config_change(self.p.ctx.get_config(), cfg)
        self.p.tick()                                    # config change polls at once
        self.assertEqual(self.rx.hits["aircraft.json"], 2)
        self.assertEqual(self.p.route_live({})["home"]["source"], "settings")
        self.p._next_poll = time.time() - 1
        self.rx.docs["aircraft.json"]["now"] += 5
        self.rx.docs["aircraft.json"]["messages"] += 500
        self.p.tick()
        self.assertEqual(self.p.route_live({})["counts"]["msg_rate"], 100.0)
        # one-argument form of on_config_change also works
        self.p.on_config_change(dict(cfg, data_url=""))
        self.assertEqual(self.p.route_live({})["state"], "unconfigured")

    def test_unreachable_receiver(self):
        self.rx.fail = True
        self.p.tick()
        live = self.p.route_live({})
        self.assertEqual(live["state"], "unreachable")
        self.assertIn("HTTP 503", live["message"])
        self.assertEqual(live["aircraft"], [])
        pill = self.p.provider_pill()
        self.assertEqual(pill["tone"], "bad")
        validate_node(pill)
        # Last good data stays, but turns "unreachable" when it is too old.
        self.rx.fail = False
        self.p._next_poll = 0
        self.p.tick()
        self.assertEqual(self.p.route_live({})["state"], "ok")
        self.p._last_ok -= 3600
        self.assertEqual(self.p.route_live({})["state"], "unreachable")

    def test_closed_port(self):
        self.rx.close()
        self.p.on_config_change({}, {"data_url": "http://127.0.0.1:9/data"})
        t0 = time.time()
        self.p.tick()
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(self.p.route_live({})["state"], "unreachable")
        self.rx = FakeReceiver()                         # for tearDown

    def test_query_validation(self):
        self.assertEqual(self.p.route_live({"trails": "2"})[1], 400)
        self.assertEqual(self.p.route_live({"evil": "1"})[1], 400)
        self.assertEqual(self.p.route_stats({"x": "1"})[1], 400)
        self.assertEqual(self.p.route_links({"x": "1"})[1], 400)
        self.assertIsInstance(self.p.route_live(None), dict)

    def test_trails_in_live(self):
        for i in range(3):
            self.rx.docs["aircraft.json"]["aircraft"][0]["lat"] = 50.5 + i * 0.01
            self.p._next_poll = 0
            self.p.tick()
        live = self.p.route_live({"trails": "1"})
        self.assertEqual(len(live["trails"]["aa0001"]), 3)
        self.assertEqual(self.p.route_live({"trails": "0"})["trails"], {})

    def test_stats_route_and_history_file(self):
        self.p.tick()
        stats = self.p.route_stats({})
        self.assertEqual(stats["current"]["signal"], -12.3)
        self.assertEqual(stats["current"]["total"], 6)
        self.assertEqual(stats["today"]["unique"], 6)
        self.assertEqual(stats["today"]["furthest"]["hex"], "aa0002")
        self.assertEqual(set(stats["series"]), {"today", "day", "week"})
        self.assertEqual(stats["columns"][0], "t")
        # unload writes history.json next to the code (atomically)
        self.p.unload()
        path = os.path.join(self.tmp, "adsb", "history.json")
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(len(data["minutes"]), 1)
        self.assertEqual(len(data["today"]["hex"]), 6)
        # and a fresh instance picks it up
        p2 = self.mod.AdsbPlugin()
        p2.load(util.FakeCtx(self.p.ctx.get_config()))
        self.assertEqual(p2.route_stats({})["today"]["unique"], 6)
        p2.unload()

    def test_pill(self):
        self.assertEqual(self.p.provider_pill()["text"], "✈︎ …")   # starting
        self.p.tick()
        pill = validate_node(self.p.provider_pill())
        self.assertEqual(pill["type"], "badge")
        self.assertEqual(pill["text"], "✈︎ 6")
        self.assertEqual(pill["tone"], "ok")
        self.assertEqual(pill["title"], "6 aircraft (2 with position)")
        cfg = dict(self.p.ctx.get_config(), pill_count_positions_only=True)
        self.p.on_config_change({}, cfg)
        self.assertEqual(self.p.provider_pill()["text"], "✈︎ 2")
        self.p.on_config_change({}, dict(cfg, show_pill=False))
        hidden = validate_node(self.p.provider_pill())
        self.assertEqual(hidden, {"type": "stack", "dir": "row", "children": []})
        self.rx.docs["aircraft.json"]["aircraft"] = []
        self.p.on_config_change({}, dict(cfg, show_pill=True))
        self.p.tick()
        self.assertEqual(self.p.provider_pill()["tone"], "muted")

    def test_pill_hidden_without_url(self):
        self.p.on_config_change({}, {"data_url": ""})
        self.assertEqual(self.p.provider_pill()["children"], [])

    def test_tab_provider(self):
        site = FakeSite(200)
        try:
            cfg = dict(self.p.ctx.get_config(), links=[
                {"label": "tar1090", "url": site.base + "/tar1090/", "probe": True},
                {"label": "graphs", "url": "https://graphs.example/", "probe": False}])
            self.p.on_config_change({}, cfg)
            self.p.tick()
            self.p.probe_links()
            node = validate_node(self.p.provider_tab())
            text = json.dumps(node)
            self.assertIn('"href": "%s/tar1090/"' % site.base, text)
            self.assertIn('"href": "https://graphs.example/"', text)
            self.assertIn('"text": "online"', text)
            self.assertIn("Signal", text)
        finally:
            site.close()
        self.p.on_config_change({}, {"data_url": ""})
        node = validate_node(self.p.provider_tab())
        self.assertIn("Configure", json.dumps(node))


class TilesTest(unittest.TestCase):
    def setUp(self):
        self.tiles = FakeTiles()
        self.mod, self.p, self.tmp = make_plugin({
            "tile_provider": "Custom", "tile_url": self.tiles.template,
            "tile_attribution": "Test tiles"})
        self.cache = os.path.join(self.tmp, "adsb", "tile-cache")

    def tearDown(self):
        self.p.unload()
        self.tiles.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, **kw):
        return {k: str(v) for k, v in kw.items()}

    def test_batch_and_cache(self):
        r = self.p.route_tiles(self.q(z=3, x=7, y=2, w=3, h=2))
        self.assertEqual(r["attribution"], "Test tiles")
        self.assertEqual(len(r["tiles"]), 6)
        self.assertEqual(r["missing"], [])
        xs = sorted({t["x"] for t in r["tiles"]})
        self.assertEqual(xs, [0, 1, 7])                     # wraps around the antimeridian
        t = r["tiles"][0]
        self.assertTrue(t["src"].startswith("data:image/png;base64,"))
        self.assertTrue(base64.b64decode(t["src"].split(",", 1)[1]).startswith(b"\x89PNG"))
        self.assertEqual(len(self.tiles.requests), 6)
        self.assertTrue(all(a.startswith("pi-hub-plugin-adsb/1.0.0 (+https://github.com/Valli-2020/")
                            for a in self.tiles.agents))
        # second call: from disk, no new upstream requests
        r2 = self.p.route_tiles(self.q(z=3, x=7, y=2, w=3, h=2))
        self.assertEqual(len(r2["tiles"]), 6)
        self.assertEqual(len(self.tiles.requests), 6)
        files = [f for _d, _s, fs in os.walk(self.cache) for f in fs]
        self.assertEqual(len(files), 6)

    def test_rows_outside_the_world_are_skipped(self):
        r = self.p.route_tiles(self.q(z=1, x=0, y=1, w=2, h=3))
        self.assertEqual(len(r["tiles"]), 2)

    def test_validation(self):
        bad = [
            {}, self.q(z=3), self.q(z=-1, x=0, y=0), self.q(z=21, x=0, y=0),
            self.q(z=3, x=8, y=0), self.q(z=3, x=0, y=8), self.q(z="3.5", x=0, y=0),
            self.q(z="0x3", x=0, y=0), self.q(z=3, x="1e2", y=0), self.q(z=3, x=0, y=0, w=9),
            self.q(z=3, x=0, y=0, w=6, h=6), self.q(z=3, x=0, y=0, theme="evil"),
            self.q(z=3, x=0, y=0, url="http://evil.example/"), self.q(z=3, x=" 1", y=0),
        ]
        for query in bad:
            res = self.p.route_tiles(query)
            self.assertIsInstance(res, tuple, query)
            self.assertEqual(res[1], 400, query)
        self.assertEqual(self.tiles.requests, [])

    def test_bad_upstream_answers(self):
        for mode in ("html", "error", "big", "redirect"):
            self.tiles.mode = mode
            self.p._tiles._failed.clear()
            r = self.p.route_tiles(self.q(z=2, x=1, y=1))
            self.assertEqual(r["tiles"], [], mode)
            self.assertEqual(r["failed"], [[1, 1]], mode)
        # failures are remembered for a minute: no hammering
        n = len(self.tiles.requests)
        self.p.route_tiles(self.q(z=2, x=1, y=1))
        self.assertEqual(len(self.tiles.requests), n)

    def test_template_rules(self):
        T = self.mod.tiles
        for template in ("ftp://x.example/{z}/{x}/{y}", "https://{z}.example/{x}/{y}",
                         "https://x.example/{z}/{x}", "https://u:p@x.example/{z}/{x}/{y}",
                         "https://x.example/{z}/{x}/{y}?k={key}", "/relative/{z}/{x}/{y}"):
            with self.assertRaises(T.TileError, msg=template):
                T.check_template(template)
        self.assertEqual(T.check_template("https://t.example:8443/a/{z}/{x}/{y}.png"), "t.example")
        with self.assertRaises(T.TileError):
            T.tile_url("https://t.example/{z}/{x}/{y}", 1, -1, 0)
        # Custom without URL: a clear error, nothing fetched
        self.p.on_config_change({}, {"tile_provider": "Custom", "tile_url": ""})
        res = self.p.route_tiles(self.q(z=1, x=0, y=0))
        self.assertEqual(res[1], 400)
        self.assertIn("custom tile URL", res[0]["error"])

    def test_builtin_providers_resolve(self):
        T = self.mod.tiles
        s = self.mod.settings.effective({"tile_provider": "CARTO auto (follows theme)"})
        dark, light = T.resolve_source(s, "dark"), T.resolve_source(s, "light")
        self.assertIn("dark_all", dark["template"])
        self.assertIn("light_all", light["template"])
        self.assertNotEqual(dark["key"], light["key"])
        osm = T.resolve_source(self.mod.settings.effective({}), "dark")
        self.assertEqual(osm["template"], "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
        self.assertIn("OpenStreetMap", osm["attribution"])

    def test_lru_eviction(self):
        T = self.mod.tiles
        cache = T.TileCache(os.path.join(self.tmp, "lru"), "test-agent",
                            fetch=lambda url, key: (png() + b"\0" * 4000, ""))
        try:
            cache.max_bytes = 3 * 4100
            src = {"key": "k", "template": "https://t.example/{z}/{x}/{y}.png"}
            for x in range(3):
                self.assertIsNotNone(cache.get(src, 4, x, 0))
                time.sleep(0.01)
            cache.get(src, 4, 0, 0)                         # touch x=0: now x=1 is the oldest
            time.sleep(0.01)
            cache.get(src, 4, 3, 0)                         # pushes one out
            names = sorted(int(os.path.basename(os.path.dirname(os.path.join(d, f))))
                           for d, _s, fs in os.walk(os.path.join(self.tmp, "lru")) for f in fs)
            self.assertEqual(names, [0, 2, 3])
            self.assertLessEqual(cache.total_bytes(), cache.max_bytes)
            cache.set_limit(0)                              # off: everything goes
            self.assertEqual(cache.total_bytes(), 0)
            self.assertIsNotNone(cache.get(src, 4, 5, 0))   # still served, just not stored
            self.assertEqual(cache.total_bytes(), 0)
        finally:
            cache.close()

    def test_index_rebuilt_from_disk(self):
        T = self.mod.tiles
        root = os.path.join(self.tmp, "idx")
        os.makedirs(os.path.join(root, "k", "1", "0"))
        with open(os.path.join(root, "k", "1", "0", "0"), "wb") as f:
            f.write(png())
        with open(os.path.join(root, "k", "1", "0", "1.tmp-5"), "wb") as f:
            f.write(b"partial")
        cache = T.TileCache(root, "ua", fetch=lambda u, k: (b"", ""))
        try:
            self.assertEqual(cache.total_bytes(), len(png()))
            self.assertFalse(os.path.exists(os.path.join(root, "k", "1", "0", "1.tmp-5")))
            self.assertEqual(cache.get({"key": "k", "template": "https://t.example/{z}/{x}/{y}"}, 1, 0, 0), png())
        finally:
            cache.close()

    def test_response_budget(self):
        T = self.mod.tiles
        big = png() + b"\0" * (300 * 1024)
        cache = T.TileCache(os.path.join(self.tmp, "budget"), "ua", fetch=lambda u, k: (big, ""))
        try:
            src = {"key": "k", "template": "https://t.example/{z}/{x}/{y}", "attribution": "",
                   "max_zoom": 19}
            r = cache.batch(src, 5, 0, 0, 6, 5)
            size = sum(len(t["src"]) for t in r["tiles"])
            self.assertLessEqual(size, T.MAX_RESPONSE_CHARS)
            self.assertEqual(len(r["tiles"]) + len(r["missing"]), 30)
            self.assertTrue(r["missing"])
            self.assertLess(len(json.dumps(r)), 1_800_000)
        finally:
            cache.close()


class LinksTest(unittest.TestCase):
    def test_probe_states_and_cache(self):
        up, err, auth = FakeSite(200), FakeSite(503), FakeSite(401)
        mod, _tmp = util.temp_plugin()
        try:
            links = mod.settings.clean_links([
                {"label": "up", "url": up.base + "/", "probe": True},
                {"label": "err", "url": err.base + "/", "probe": True},
                {"label": "auth", "url": auth.base + "/", "probe": True},
                {"label": "down", "url": "http://127.0.0.1:9/", "probe": True},
                {"label": "noprobe", "url": "https://x.example/", "probe": False}])
            clock = [1000.0]
            prober = mod.links.LinkProber("ua", clock=lambda: clock[0])
            self.assertEqual([x["state"] for x in prober.status(links)],
                             ["unknown", "unknown", "unknown", "unknown", "off"])
            self.assertEqual(prober.run_due(links), 4)
            self.assertEqual([x["state"] for x in prober.status(links)],
                             ["online", "offline", "online", "offline", "off"])
            self.assertEqual(prober.run_due(links), 0)              # cached
            clock[0] += 61
            self.assertEqual(prober.run_due(links[:1]), 1)          # stale again; others dropped
            self.assertEqual(len(prober._cache), 1)
        finally:
            for s in (up, err, auth):
                s.close()
            shutil.rmtree(_tmp, ignore_errors=True)

    def test_links_route(self):
        mod, p, tmp = make_plugin({"links": [{"label": "a", "url": "https://a.example/", "probe": True},
                                             {"label": "b", "url": "javascript:alert(1)"}]})
        try:
            p._prober._probe = lambda url, ua: True
            p.probe_links()
            self.assertEqual(p.route_links({}), {"links": [
                {"label": "a", "url": "https://a.example/", "probe": True, "state": "online",
                 "checked": p.route_links({})["links"][0]["checked"]}]})
        finally:
            p.unload()
            shutil.rmtree(tmp, ignore_errors=True)


# ── Config schema against the frozen 8.3 contract ──────────────────────────

FIELD_TYPES = ("text", "password", "number", "checkbox", "select", "url", "textarea", "list")
SUB_TYPES = ("text", "url", "number", "checkbox", "select")
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
KEYS = {"name", "type", "label", "help", "placeholder", "required", "secret", "default",
        "min", "max", "options", "section", "fields", "max_items"}


def check_field(f, sub=False):
    assert isinstance(f, dict), f
    assert set(f) <= KEYS, set(f) - KEYS
    assert NAME_RE.fullmatch(f["name"]), f["name"]
    types = SUB_TYPES if sub else FIELD_TYPES
    assert f["type"] in types, f
    assert len(f.get("label", "")) <= 120
    assert len(f.get("help", "")) <= 300, (f["name"], len(f["help"]))
    assert len(f.get("placeholder", "")) <= 120
    if "section" in f:
        assert not sub and isinstance(f["section"], str) and len(f["section"]) <= 60
    if "min" in f or "max" in f:
        assert f["type"] == "number"
    if f["type"] == "select":
        assert f.get("options") and all(isinstance(o, str) for o in f["options"])
        if "default" in f:
            assert f["default"] in f["options"]
    if f["type"] == "number" and "default" in f:
        assert f.get("min", -1e18) <= f["default"] <= f.get("max", 1e18)
    if sub:
        assert "fields" not in f and "max_items" not in f and not f.get("secret")
    if f["type"] == "list":
        assert 1 <= len(f["fields"]) <= 8
        names = [s["name"] for s in f["fields"]]
        assert len(names) == len(set(names))
        for s in f["fields"]:
            check_field(s, sub=True)
        assert 1 <= f.get("max_items", 50) <= 100
        assert isinstance(f.get("default", []), list)


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.mod, self.tmp = util.temp_plugin()
        self.schema = self.mod.AdsbPlugin().get_config_schema()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_contract(self):
        self.assertLessEqual(len(self.schema), 60)
        names = [f["name"] for f in self.schema]
        self.assertEqual(len(names), len(set(names)))
        for f in self.schema:
            check_field(f)

    def test_every_setting_is_in_the_schema(self):
        self.assertEqual({f["name"] for f in self.schema}, set(self.mod.settings.DEFAULTS))
        for f in self.schema:
            self.assertEqual(f.get("default"), self.mod.settings.DEFAULTS[f["name"]], f["name"])

    def test_sections_are_contiguous(self):
        seen, last = [], None
        for f in self.schema:
            if f.get("section") != last:
                self.assertNotIn(f.get("section"), seen, "section split: " + str(f.get("section")))
                seen.append(f.get("section"))
                last = f.get("section")
        self.assertEqual(seen, ["Receiver", "Location", "Map", "Header", "Links", "Statistics"])

    def test_number_limits_match(self):
        for f in self.schema:
            if f["type"] == "number":
                self.assertEqual((f["min"], f["max"]), self.mod.settings.NUMBER_LIMITS[f["name"]])

    @unittest.skipUnless(core_contrib is not None and "list" in (
        getattr(core_contrib, "CONFIG_FIELD_TYPES", ()) + getattr(core_contrib, "FIELD_TYPES", ())),
                         "core without the 8.3 field types (set PIHUB_CORE to an 8.3 checkout)")
    def test_core_validator(self):
        out = core_contrib.validate_config_schema(self.schema)
        self.assertEqual([f["name"] for f in out], [f["name"] for f in self.schema])
        for mine, core in zip(self.schema, out):
            self.assertEqual(core["type"], mine["type"], mine["name"])
            self.assertEqual(core.get("section"), mine.get("section"), mine["name"])
        links = [f for f in out if f["name"] == "links"][0]
        self.assertEqual([s["name"] for s in links["fields"]], ["label", "url", "probe"])
        self.assertEqual(links["max_items"], 30)
        bad, err = core_contrib.coerce_config_values(out, {"links": [{"label": "x", "url": "ftp://x"}]}, {})
        self.assertIn("row 1", err)
        values, err = core_contrib.coerce_config_values(out, {
            "data_url": "http://receiver.example.local/tar1090/data",
            "links": [{"label": "a", "url": "https://a.example/", "probe": True}]}, {})
        self.assertIsNone(err)
        self.assertEqual(values["links"][0]["url"], "https://a.example/")


# ── Descriptors and frame files ────────────────────────────────────────────

class DescriptorTest(unittest.TestCase):
    def setUp(self):
        self.mod, self.tmp = util.temp_plugin()
        self.p = self.mod.AdsbPlugin()

    def tearDown(self):
        self.p.unload()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_matches_class(self):
        for path in (os.path.join(util.PLUGIN_SRC, "pihub-plugin.json"),
                     os.path.join(util.REPO, "pihub-plugin.json")):
            with open(path) as f:
                m = json.load(f)
            self.assertEqual(m["name"], self.p.name)
            self.assertEqual(m["version"], self.p.version)
            self.assertEqual(m["min_core_version"], self.p.min_core_version)
            self.assertEqual(m["plugin_api_version"], self.p.plugin_api_version)
            self.assertEqual(sorted(m["capabilities"]), sorted(self.p.capabilities))
        self.assertIs(self.mod.PLUGIN_CLASS, self.mod.AdsbPlugin)

    def test_capabilities_cover_what_is_used(self):
        slots = {c.slot for c in self.p.get_contributions()}
        need = {"header.pill": "ui.header", "style": "ui.style", "tab": "ui.tab"}
        used = {need[s] for s in slots} | ({"ui.frame"} if self.p.get_frames() else set())
        self.assertEqual(used, set(self.p.capabilities))

    def test_style_is_accepted_by_the_sanitizer(self):
        css = [c for c in self.p.get_contributions() if c.slot == "style"][0].static["css"]
        if core_contrib is not None:
            self.assertEqual(core_contrib.sanitize_css(css), css)
        self.assertNotIn("url(", css)
        self.assertIn("26px", css)

    def test_routes(self):
        routes = {(r.method, r.path): r for r in self.p.get_routes()}
        self.assertEqual(set(routes), {("GET", "/live"), ("GET", "/stats"), ("GET", "/tiles"),
                                       ("GET", "/links")})
        self.assertTrue(all(r.caps == [] for r in routes.values()))

    def test_frame_files(self):
        frame_dir = os.path.join(util.PLUGIN_SRC, "static", "frame")
        (fd,) = self.p.get_frames()
        total = 0
        for name in fd.entry + fd.css:
            path = os.path.join(frame_dir, name)
            size = os.path.getsize(path)
            total += size
            self.assertLessEqual(size, 512 * 1024)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("</script", text.lower())
            self.assertNotIn("<!--", text)
        self.assertLessEqual(total, 2 * 1024 * 1024)
        self.assertEqual(sorted(os.listdir(frame_dir)), sorted(fd.entry + fd.css))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_syntax(self):
        path = os.path.join(util.PLUGIN_SRC, "static", "frame", "flights.js")
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
