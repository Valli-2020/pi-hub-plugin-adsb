"""ADS-B plugin (Plugin API v2): live map, statistics and links for a
readsb / tar1090 receiver.

The server side polls the receiver's ``data/`` directory (``aircraft.json``,
``stats.json``, ``receiver.json``), keeps the live picture and trails in
memory, writes per-minute aggregates and daily records to ``history.json``
and serves map tiles to the sandboxed frame.  The frame (``static/frame``)
draws everything; a declarative tab and a header pill work without it.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from typing import Any

from pi_hub.plugins.base import Contribution, FrameDef, Plugin, PluginContext, RouteDef, TaskDef

from . import history as history_mod
from . import receiver as rx
from . import settings as settings_mod
from .links import LinkProber
from .tiles import TileCache, TileError, parse_int, resolve_source

log = logging.getLogger(__name__)

VERSION = "1.1.0"
USER_AGENT = f"pi-hub-plugin-adsb/{VERSION} (+https://github.com/Valli-2020/pi-hub-plugin-adsb)"
HERE = os.path.dirname(os.path.abspath(__file__))

RECEIVER_EVERY_S = 600.0       # receiver.json rarely changes
STATS_EVERY_S = 30.0           # stats.json is rewritten once a minute
SAVE_EVERY_S = 300.0           # history.json write interval
STALE_AFTER_S = 60.0           # without a good poll for this long: "unreachable"
MAX_TRAIL_POINTS_TOTAL = 60000

PLANE_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M10.5 21l1.5-1 1.5 1v-2.5l-1-1V13l7 2v-2l-7-4.5V4a1 1 0 0 0-2 0v4.5L3.5 13v2l7-2v4.5l-1 1z"/></svg>'
)
PLANE_GLYPH = "✈︎"   # text presentation, not the emoji

# Scoped by the core to .plg-adsb.  Only the header pill: core pills are 26 px,
# plugin badges 20 px.
PILL_CSS = ('[data-slot="header.pill"] & .ph-badge { height:26px; padding:0 10px; '
            'font-size:12px; font-variant-numeric:tabular-nums; }')


class AdsbPlugin(Plugin):
    name = "adsb"
    version = VERSION
    description = "ADS-B receiver dashboard: live map, statistics, records and links for readsb/tar1090"
    min_core_version = "8.3.1"
    plugin_api_version = 2
    capabilities = ["ui.frame", "ui.header", "ui.style", "ui.tab"]

    def __init__(self) -> None:
        super().__init__()
        self.ctx: PluginContext | None = None
        self._lock = threading.RLock()
        self._settings = settings_mod.effective({})
        self._tracker = rx.Tracker()
        self._snap: dict | None = None
        self._stats: dict = {}
        self._receiver: dict = {}
        self._error = "starting"
        self._last_ok = 0.0
        self._next_poll = 0.0
        self._next_stats = 0.0
        self._next_receiver = 0.0
        self._next_save = 0.0
        self._history = history_mod.History(os.path.join(HERE, "history.json"))
        self._tiles = TileCache(os.path.join(HERE, "tile-cache"), USER_AGENT)
        self._prober = LinkProber(USER_AGENT)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        data = self._data_dir(ctx)
        self._history = history_mod.History(os.path.join(data, "history.json"))
        self._tiles.close()
        self._tiles = TileCache(os.path.join(data, "tile-cache"), USER_AGENT)
        self._apply_config(ctx.get_config())
        with self._lock:
            self._history.load()
            self._history.prune(time.time(), self._settings["history_days"])

    def unload(self) -> None:
        with self._lock:
            try:
                self._history.flush(time.time() + 60, self._settings["history_days"])
                if self._history.dirty:
                    self._history.save()
            except OSError as exc:
                log.warning("adsb: could not save history: %s", exc)
        self._tiles.close()

    @staticmethod
    def _data_dir(ctx: Any) -> str:
        """``ctx.data_dir()`` (kept across store updates since Pi Hub 8.3.1),
        else the plugin folder.  Moves files left by 1.0.0 there once."""
        fn = getattr(ctx, "data_dir", None)
        if not callable(fn):
            return HERE
        try:
            data = str(fn())
        except (OSError, PermissionError) as exc:
            log.warning("adsb: no data directory (%s); using the plugin folder", exc)
            return HERE
        move_legacy_data(HERE, data)
        return data

    def migrate_config(self, old_version: str, config: dict) -> dict:
        return settings_mod.migrate(old_version, config)

    def on_config_change(self, old: dict | None = None, new: dict | None = None) -> None:
        if new is None:                 # API v2 docs show a one-argument form
            new = old
        self._apply_config(new or (self.ctx.get_config() if self.ctx else {}))

    def _apply_config(self, raw: Any) -> None:
        s = settings_mod.effective(raw)
        with self._lock:
            if s["data_url"] != self._settings.get("data_url"):
                self._tracker = rx.Tracker()
                self._snap, self._stats, self._receiver = None, {}, {}
                self._error = "starting" if s["data_url"] else "not configured"
                self._last_ok = 0.0
                self._next_stats = self._next_receiver = 0.0
            self._settings = s
            self._next_poll = 0.0            # apply right away
        self._tiles.set_limit(s["tile_cache_mb"])

    # ── Descriptors ────────────────────────────────────────────────────────

    def get_config_schema(self) -> list[dict]:
        return settings_mod.config_schema()

    def get_routes(self) -> list[RouteDef]:
        return [
            RouteDef("GET", "/live", self.route_live),
            RouteDef("GET", "/stats", self.route_stats),
            RouteDef("GET", "/tiles", self.route_tiles),
            RouteDef("GET", "/links", self.route_links),
        ]

    def get_tasks(self) -> list[TaskDef]:
        return [TaskDef("poll", self.tick, interval=1),
                TaskDef("links", self.probe_links, interval=5)]

    def get_contributions(self) -> list[Contribution]:
        return [
            Contribution("header.pill", "count", self.provider_pill, poll=10, opens="flights"),
            Contribution("style", "pill", static={"css": PILL_CSS}),
            Contribution("tab", "receiver", self.provider_tab, poll=30,
                         label="ADS-B receiver", icon_svg=PLANE_SVG, order=110),
        ]

    def get_frames(self) -> list[FrameDef]:
        return [FrameDef(
            "flights", entry=["flights.js"], css=["flights.css"],
            surfaces=[{"type": "tab", "label": "Flights", "icon_svg": PLANE_SVG}],
            height=900,
        )]

    # ── Polling ────────────────────────────────────────────────────────────

    def _home(self) -> tuple[float, float] | None:
        s = self._settings
        if s["lat"] is not None:
            return s["lat"], s["lon"]
        if "lat" in self._receiver:
            return self._receiver["lat"], self._receiver["lon"]
        return None

    def tick(self) -> None:
        """Runs every second; polls when the configured interval is due."""
        now = time.time()
        with self._lock:
            s = dict(self._settings)
            due = now >= self._next_poll
            if due:
                self._next_poll = now + s["poll_seconds"]
            get_receiver = now >= self._next_receiver
            get_stats = now >= self._next_stats
        if s["data_url"] and due:
            self._poll(s, now, get_receiver, get_stats)
        elif not s["data_url"]:
            with self._lock:
                self._error = "not configured"
        with self._lock:
            self._history.flush(now, s["history_days"])
            if self._history.dirty and now >= self._next_save:
                self._next_save = now + SAVE_EVERY_S
                try:
                    self._history.save()
                except OSError as exc:
                    log.warning("adsb: could not save history: %s", exc)

    def _poll(self, s: dict, now: float, get_receiver: bool, get_stats: bool) -> None:
        url = s["data_url"]
        if get_receiver:
            try:
                info = rx.parse_receiver(rx.fetch_json(url, "receiver.json", USER_AGENT))
                with self._lock:
                    self._receiver = info
                    self._next_receiver = now + RECEIVER_EVERY_S
            except rx.ReceiverError as exc:
                log.info("adsb: %s", exc)
                with self._lock:
                    self._next_receiver = now + 30
        try:
            data = rx.fetch_json(url, "aircraft.json", USER_AGENT)
        except rx.ReceiverError as exc:
            with self._lock:
                self._error = f"Receiver unreachable ({exc})"
            return
        with self._lock:
            if s["data_url"] != self._settings["data_url"]:
                return                       # config changed while fetching
            snap = self._tracker.update(data, now, self._home(), s["trail_minutes"] * 60)
            self._snap = snap
            self._last_ok = now
            self._error = ""
            self._history.add_snapshot(snap, now, s["history_days"])
        if get_stats:
            try:
                stats = rx.parse_stats(rx.fetch_json(url, "stats.json", USER_AGENT))
                with self._lock:
                    self._stats = stats
                    self._next_stats = now + STATS_EVERY_S
            except rx.ReceiverError as exc:
                log.info("adsb: %s", exc)
                with self._lock:
                    self._next_stats = now + STATS_EVERY_S

    def probe_links(self) -> None:
        with self._lock:
            links = list(self._settings["links"])
        self._prober.run_due(links)

    # ── State helpers ──────────────────────────────────────────────────────

    def _state(self, now: float) -> tuple[str, str]:
        """(state, message): ok / unconfigured / unreachable / starting."""
        if not self._settings["data_url"]:
            return "unconfigured", "Set the receiver data URL in the plugin settings"
        if self._snap is None:
            if self._error and self._error != "starting":
                return "unreachable", self._error
            return "starting", "Waiting for the first answer from the receiver"
        if now - self._last_ok > max(STALE_AFTER_S, 3 * self._settings["poll_seconds"]):
            return "unreachable", self._error or "Receiver unreachable"
        return "ok", ""

    @staticmethod
    def _bad_query(query: Any, allowed: set[str]) -> str:
        if query is None:
            return ""
        if not isinstance(query, dict):
            return "invalid query"
        unknown = sorted(set(query) - allowed)
        return f"unknown parameter: {unknown[0]}" if unknown else ""

    # ── Routes ─────────────────────────────────────────────────────────────

    def route_live(self, query: dict | None = None):
        err = self._bad_query(query, {"trails"})
        if err:
            return {"error": err}, 400
        want_trails = True
        if query and "trails" in query:
            v = query["trails"][0] if isinstance(query["trails"], list) else query["trails"]
            if v not in ("0", "1"):
                return {"error": "trails must be 0 or 1"}, 400
            want_trails = v == "1"
        now = time.time()
        with self._lock:
            s = self._settings
            state, message = self._state(now)
            snap = self._snap or {"aircraft": [], "total": 0, "with_pos": 0, "mlat": 0,
                                  "msg_rate": None, "now": 0}
            home = self._home()
            aircraft = list(snap["aircraft"])
            trails = {}
            if want_trails and s["trail_minutes"] > 0:
                trails = self._tracker.trails_payload({a["hex"] for a in aircraft if "lat" in a})
            today = self._history.today_stats(now)
            far = today.get("furthest") if isinstance(today.get("furthest"), dict) else {}
            result = {
                "state": state, "message": message,
                "updated": int(self._last_ok) if self._last_ok else None,
                "now": int(now),
                "poll_seconds": s["poll_seconds"],
                "start_zoom": s["start_zoom"],
                "trail_minutes": s["trail_minutes"],
                "rings": s["rings"],
                "home": ({"lat": home[0], "lon": home[1],
                          "source": "settings" if s["lat"] is not None else "receiver"}
                         if home else None),
                "receiver_version": self._receiver.get("version", ""),
                "counts": {"total": snap["total"], "with_pos": snap["with_pos"],
                           "mlat": snap["mlat"], "msg_rate": snap["msg_rate"]},
                "today": {"unique": today.get("unique", 0), "furthest_km": far.get("km")},
                "aircraft": aircraft,
                "trails": _cap_trails(trails),
                "map": _map_info(s),
            }
        return result

    def route_stats(self, query: dict | None = None):
        err = self._bad_query(query, set())
        if err:
            return {"error": err}, 400
        now = time.time()
        with self._lock:
            h = self._history
            midnight = history_mod.local_midnight(now)
            state, message = self._state(now)
            stats = dict(self._stats)
            snap = self._snap or {}
            return {
                "state": state, "message": message, "now": int(now),
                "history_days": self._settings["history_days"],
                "series": {
                    "today": h.series(midnight, now + 60, 300),
                    "day": h.series(now - 86400, now + 60, 300),
                    "week": h.series(now - 7 * 86400, now + 60, 3600),
                },
                "columns": ["t", "aircraft", "with_pos", "msg_per_s", "max_range_km"],
                "today": h.today_stats(now),
                "best": h.best_records(),
                "days": h.day_list(),
                "current": {**stats, "msg_rate": snap.get("msg_rate"),
                            "total": snap.get("total"), "with_pos": snap.get("with_pos"),
                            "mlat": snap.get("mlat")},
            }

    def route_tiles(self, query: dict | None = None):
        err = self._bad_query(query, {"z", "x", "y", "w", "h", "theme"})
        if err:
            return {"error": err}, 400
        q = query or {}
        try:
            z = parse_int(q, "z", 0, 20)
            n = (1 << z) - 1
            x = parse_int(q, "x", 0, n)
            y = parse_int(q, "y", 0, n)
            w = parse_int(q, "w", 1, 8, 1)
            h = parse_int(q, "h", 1, 8, 1)
            if w * h > 30:
                raise TileError("at most 30 tiles per request")
            theme = q.get("theme", "dark")
            theme = theme[0] if isinstance(theme, list) and theme else theme
            if theme not in ("dark", "light"):
                raise TileError("theme must be dark or light")
            with self._lock:
                source = resolve_source(self._settings, theme)
            if z > source["max_zoom"]:
                raise TileError(f"z must be at most {source['max_zoom']}")
        except TileError as exc:
            return {"error": str(exc)}, 400
        return self._tiles.batch(source, z, x, y, w, h)

    def route_links(self, query: dict | None = None):
        err = self._bad_query(query, set())
        if err:
            return {"error": err}, 400
        with self._lock:
            links = list(self._settings["links"])
        return {"links": self._prober.status(links)}

    # ── Providers (never network I/O) ──────────────────────────────────────

    def provider_pill(self, session: Any = None) -> dict | None:
        now = time.time()
        with self._lock:
            s = self._settings
            if not s["show_pill"] or not s["data_url"]:
                return None                  # Pi Hub 8.3.1+ renders nothing
            state, message = self._state(now)
            snap = self._snap or {}
            total, with_pos = snap.get("total", 0), snap.get("with_pos", 0)
        if state in ("unreachable",):
            return {"type": "badge", "text": f"{PLANE_GLYPH} –", "tone": "bad",
                    "title": message[:200]}
        if state != "ok":
            return {"type": "badge", "text": f"{PLANE_GLYPH} …", "tone": "muted",
                    "title": message[:200]}
        count = with_pos if s["pill_count_positions_only"] else total
        return {"type": "badge", "text": f"{PLANE_GLYPH} {count}",
                "tone": "ok" if count > 0 else "muted",
                "title": f"{total} aircraft ({with_pos} with position)"}

    def provider_tab(self, session: Any = None) -> dict:
        """Declarative fallback: works with frames off, and its links open
        in a new tab (the sandboxed frame cannot open links)."""
        now = time.time()
        with self._lock:
            state, message = self._state(now)
            snap = self._snap or {}
            today = self._history.today_stats(now)
            stats = dict(self._stats)
            links = list(self._settings["links"])
        far = today.get("furthest") if isinstance(today.get("furthest"), dict) else {}
        children: list[dict] = []
        if state != "ok":
            children.append({"type": "status",
                             "state": "bad" if state == "unreachable" else "unknown",
                             "text": message[:80]})
        rate = snap.get("msg_rate")
        children.append({"type": "stack", "dir": "row", "children": [
            {"type": "stat", "label": "Aircraft", "value": str(snap.get("total", 0))},
            {"type": "stat", "label": "With position", "value": str(snap.get("with_pos", 0))},
            {"type": "stat", "label": "MLAT", "value": str(snap.get("mlat", 0))},
            {"type": "stat", "label": "Messages", "value": "–" if rate is None else f"{rate:.0f}",
             "unit": "/s"},
            {"type": "stat", "label": "Max range today",
             "value": "–" if far.get("km") is None else f"{far['km']:.0f}", "unit": "km"},
            {"type": "stat", "label": "Unique today", "value": str(today.get("unique", 0))},
        ]})
        rows = [[label, f"{stats[key]}{unit}"] for key, label, unit in (
            ("signal", "Signal", " dBFS"), ("noise", "Noise", " dBFS"),
            ("strong_signals", "Strong signals (1 min)", ""), ("gain_db", "Gain", " dB"),
            ("estimated_ppm", "Frequency error", " ppm")) if key in stats]
        if rows:
            children.append({"type": "section", "title": "Receiver", "children": [
                {"type": "kv", "rows": rows}]})
        if links:
            status = {x["url"]: x for x in self._prober.status(links)}
            dots = {"online": ("ok", "online"), "offline": ("bad", "offline"),
                    "unknown": ("unknown", "checking")}
            items = []
            for link in links:
                st = status.get(link["url"], {}).get("state", "off")
                row = [{"type": "link", "text": link["label"][:120], "href": link["url"]}]
                if st in dots:
                    row.insert(0, {"type": "status", "state": dots[st][0], "text": dots[st][1]})
                items.append({"type": "stack", "dir": "row", "children": row})
            children.append({"type": "section", "title": "Links", "children": items})
        else:
            children.append({"type": "text", "tone": "muted",
                             "text": "Add links (tar1090, graphs1090, feeder pages) in "
                                     "Settings → Plugins → adsb → Configure."})
        return {"type": "stack", "dir": "col", "children": children}


def move_legacy_data(old_dir: str, data_dir: str) -> list[str]:
    """Move ``history.json`` and ``tile-cache/`` from *old_dir* (1.0.0 kept them
    next to the code) into *data_dir*.  Never overwrites; returns what moved."""
    moved = []
    if os.path.realpath(old_dir) == os.path.realpath(data_dir):
        return moved
    for name in ("history.json", "history.json.corrupt", "tile-cache"):
        src, dst = os.path.join(old_dir, name), os.path.join(data_dir, name)
        if not os.path.lexists(src) or os.path.islink(src) or os.path.lexists(dst):
            continue
        try:
            os.makedirs(data_dir, exist_ok=True)
            shutil.move(src, dst)
            moved.append(name)
        except OSError as exc:
            log.warning("adsb: could not move %s to the data folder: %s", name, exc)
    if moved:
        log.info("adsb: moved %s to %s", ", ".join(moved), data_dir)
    return moved


def _map_info(s: dict) -> dict:
    provider = s["tile_provider"]
    ok = provider != "Custom" or bool(s["tile_url"])
    return {"provider": provider, "auto_theme": provider.startswith("CARTO auto"),
            "available": ok}


def _cap_trails(trails: dict[str, list]) -> dict[str, list]:
    """Keep the payload bounded: thin out every trail evenly if needed."""
    total = sum(len(t) for t in trails.values())
    if total <= MAX_TRAIL_POINTS_TOTAL:
        return trails
    step = total // MAX_TRAIL_POINTS_TOTAL + 1
    return {hx: (t[:-1:step] + [t[-1]]) for hx, t in trails.items()}


PLUGIN_CLASS = AdsbPlugin
