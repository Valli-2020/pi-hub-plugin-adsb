"""Talking to a readsb / tar1090 receiver and keeping the live picture.

:func:`fetch_json` reads one file from the receiver's ``data/`` directory.
:class:`Tracker` turns successive ``aircraft.json`` documents into a snapshot
(aircraft with distance and bearing, counts, message rate) plus bounded
per-aircraft trails.  Everything the receiver sends is untrusted: every field
is type-checked and clamped before it is kept.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from collections import deque
from typing import Any

from . import geo

FETCH_TIMEOUT_S = 4.0
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_AIRCRAFT = 2000
MAX_TRAIL_POINTS = 360
POSITION_MAX_AGE_S = 60.0      # older positions are not drawn
PLAUSIBLE_RANGE_KM = 1000.0    # beyond this a position is treated as bogus

HEX_RE = re.compile(r"~?[0-9a-f]{6}")
_TEXT_RE = re.compile(r"[^\x20-\x7e]")


class ReceiverError(Exception):
    """The receiver could not be reached or sent something unusable."""


def fetch_json(base_url: str, name: str, user_agent: str,
               timeout: float = FETCH_TIMEOUT_S) -> dict:
    """GET ``<base_url>/<name>`` and parse it as a JSON object."""
    if not base_url:
        raise ReceiverError("no data URL configured")
    req = urllib.request.Request(base_url.rstrip("/") + "/" + name,
                                 headers={"User-Agent": user_agent,
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ReceiverError(f"{name}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ReceiverError(f"{name}: {reason}") from None
    if len(raw) > MAX_JSON_BYTES:
        raise ReceiverError(f"{name}: response too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ReceiverError(f"{name}: not JSON") from None
    if not isinstance(data, dict):
        raise ReceiverError(f"{name}: unexpected content")
    return data


# ── Field parsing ───────────────────────────────────────────────────────────

def num(v: Any, lo: float, hi: float) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f) or f < lo or f > hi:
        return None
    return f


def text(v: Any, limit: int = 16) -> str:
    if not isinstance(v, str):
        return ""
    return _TEXT_RE.sub("", v).strip()[:limit]


def _source(a: dict) -> str:
    t = a.get("type")
    if isinstance(t, str):
        if t == "mlat":
            return "mlat"
        if t.startswith("tisb"):
            return "tisb"
        if t.startswith("adsr"):
            return "adsr"
        if t.startswith("adsb"):
            return "adsb"
        if t in ("mode_s", "mode_ac"):
            return "modes"
    for key, src in (("mlat", "mlat"), ("tisb", "tisb")):
        lst = a.get(key)
        if isinstance(lst, list) and "lat" in lst:
            return src
    return "adsb" if "lat" in a else "modes"


def parse_aircraft(a: Any) -> dict | None:
    """One entry of ``aircraft.json`` -> a small, validated dict (or None)."""
    if not isinstance(a, dict):
        return None
    hx = a.get("hex")
    if not isinstance(hx, str):
        return None
    hx = hx.strip().lower()
    if not HEX_RE.fullmatch(hx):
        return None
    out: dict[str, Any] = {"hex": hx, "flight": text(a.get("flight"), 10)}
    lat, lon = num(a.get("lat"), -90, 90), num(a.get("lon"), -180, 180)
    seen_pos = num(a.get("seen_pos"), 0, 1e7)
    if lat is not None and lon is not None and (seen_pos is None or seen_pos <= POSITION_MAX_AGE_S):
        out["lat"], out["lon"] = round(lat, 5), round(lon, 5)
        out["seen_pos"] = round(seen_pos or 0.0, 1)
    alt = a.get("alt_baro")
    if alt == "ground":
        out["ground"] = True
        out["alt"] = 0
    else:
        v = num(alt, -2000, 100000)
        if v is None:
            v = num(a.get("alt_geom"), -2000, 100000)
        if v is not None:
            out["alt"] = int(v)
    for key, src, lo, hi, nd in (("gs", "gs", 0, 2000, 0),
                                 ("track", "track", 0, 360, 0),
                                 ("rssi", "rssi", -60, 10, 1)):
        v = num(a.get(src), lo, hi)
        if v is not None:
            out[key] = round(v, nd) if nd else int(round(v))
    if "track" not in out:
        v = num(a.get("true_heading"), 0, 360)
        if v is None:
            v = num(a.get("mag_heading"), 0, 360)
        if v is not None:
            out["track"] = int(round(v))
    vr = num(a.get("baro_rate"), -30000, 30000)
    if vr is None:
        vr = num(a.get("geom_rate"), -30000, 30000)
    if vr is not None:
        out["vr"] = int(vr)
    sq = a.get("squawk")
    if isinstance(sq, str) and re.fullmatch(r"[0-7]{4}", sq):
        out["squawk"] = sq
    cat = a.get("category")
    if isinstance(cat, str) and re.fullmatch(r"[A-D][0-7]", cat):
        out["cat"] = cat
    for key in ("t", "r"):
        v = text(a.get(key), 12)
        if v:
            out["type" if key == "t" else "reg"] = v
    seen = num(a.get("seen"), 0, 1e7)
    out["seen"] = round(seen, 1) if seen is not None else 0.0
    out["src"] = _source(a)
    return out


# ── Live picture ────────────────────────────────────────────────────────────

class Tracker:
    """Live aircraft picture with trails.  Not thread-safe: the caller locks."""

    def __init__(self) -> None:
        self.trails: dict[str, deque] = {}
        self.trail_seen: dict[str, float] = {}
        self.last_messages: tuple[float, float] | None = None   # (now, counter)
        self.msg_rate: float | None = None

    def update(self, data: dict, now: float, home: tuple[float, float] | None,
               trail_seconds: float) -> dict:
        """Ingest one ``aircraft.json`` document and return the snapshot."""
        src_now = num(data.get("now"), 0, 1e11) or now
        raw = data.get("aircraft")
        aircraft: list[dict] = []
        if isinstance(raw, list):
            for entry in raw[:MAX_AIRCRAFT * 2]:
                ac = parse_aircraft(entry)
                if ac is not None:
                    aircraft.append(ac)
                if len(aircraft) >= MAX_AIRCRAFT:
                    break

        # Message rate from the receiver's running counter.
        counter = num(data.get("messages"), 0, 1e15)
        if counter is not None:
            prev = self.last_messages
            if prev and src_now > prev[0] and counter >= prev[1]:
                self.msg_rate = round((counter - prev[1]) / (src_now - prev[0]), 1)
            elif prev and counter < prev[1]:
                self.msg_rate = None          # receiver restarted
            self.last_messages = (src_now, counter)

        with_pos = mlat = 0
        for ac in aircraft:
            if "lat" not in ac:
                continue
            if home:
                d = geo.haversine_km(home[0], home[1], ac["lat"], ac["lon"])
                if d > PLAUSIBLE_RANGE_KM:          # decoding error, not a plane
                    for key in ("lat", "lon", "seen_pos"):
                        ac.pop(key, None)
                    continue
                ac["dist"] = round(d, 1)
                ac["brg"] = int(round(geo.bearing_deg(home[0], home[1], ac["lat"], ac["lon"]))) % 360
            with_pos += 1
            if ac["src"] == "mlat":
                mlat += 1
            if trail_seconds > 0:
                self._add_point(ac, now)
        self._prune_trails(now, trail_seconds)

        return {"now": now, "aircraft": aircraft, "total": len(aircraft),
                "with_pos": with_pos, "mlat": mlat, "msg_rate": self.msg_rate}

    def _add_point(self, ac: dict, now: float) -> None:
        hx = ac["hex"]
        trail = self.trails.get(hx)
        if trail is None:
            if len(self.trails) >= MAX_AIRCRAFT:
                return
            trail = self.trails[hx] = deque(maxlen=MAX_TRAIL_POINTS)
        point = (round(now, 1), ac["lat"], ac["lon"], ac.get("alt"))
        if trail and trail[-1][1] == point[1] and trail[-1][2] == point[2]:
            trail[-1] = point                 # same spot: keep one fresh point
            self.trail_seen[hx] = now
            return
        trail.append(point)
        self.trail_seen[hx] = now

    def _prune_trails(self, now: float, trail_seconds: float) -> None:
        if trail_seconds <= 0:
            self.trails.clear()
            self.trail_seen.clear()
            return
        cutoff = now - trail_seconds
        for hx in list(self.trails):
            trail = self.trails[hx]
            while trail and trail[0][0] < cutoff:
                trail.popleft()
            if not trail or self.trail_seen.get(hx, 0) < cutoff:
                del self.trails[hx]
                self.trail_seen.pop(hx, None)

    def trails_payload(self, hexes: set[str]) -> dict[str, list]:
        """``{hex: [[lat, lon, alt], ...]}`` for the given aircraft."""
        out = {}
        for hx in hexes:
            trail = self.trails.get(hx)
            if trail and len(trail) > 1:
                out[hx] = [[p[1], p[2], p[3]] for p in trail]
        return out


def parse_stats(data: dict) -> dict:
    """The interesting parts of ``stats.json``, all optional."""
    out: dict[str, Any] = {}
    for key, lo, hi in (("gain_db", -10, 100), ("estimated_ppm", -500, 500)):
        v = num(data.get(key), lo, hi)
        if v is not None:
            out[key] = round(v, 1)
    last1 = data.get("last1min") if isinstance(data.get("last1min"), dict) else {}
    local = last1.get("local") if isinstance(last1.get("local"), dict) else {}
    for key, lo, hi in (("signal", -100, 10), ("noise", -100, 10), ("peak_signal", -100, 10)):
        v = num(local.get(key), lo, hi)
        if v is not None:
            out[key] = round(v, 1)
    v = num(local.get("strong_signals"), 0, 1e12)
    if v is not None:
        out["strong_signals"] = int(v)
    start, end = num(last1.get("start"), 0, 1e11), num(last1.get("end"), 0, 1e11)
    msgs = num(last1.get("messages"), 0, 1e15)
    if msgs is not None:
        span = (end - start) if start and end and end > start else 60.0
        out["msg_rate_1min"] = round(msgs / span, 1)
    for window in ("last15min", "total"):
        w = data.get(window) if isinstance(data.get(window), dict) else {}
        md = num(w.get("max_distance"), 0, 1e8)
        if md is not None:
            out["max_distance_km_" + ("15min" if window == "last15min" else "total")] = round(md / 1000.0, 1)
    tracks = (data.get("last15min") or {}).get("tracks") if isinstance(data.get("last15min"), dict) else None
    if isinstance(tracks, dict):
        v = num(tracks.get("all"), 0, 1e9)
        if v is not None:
            out["tracks_15min"] = int(v)
    return out


def parse_receiver(data: dict) -> dict:
    out: dict[str, Any] = {}
    lat, lon = num(data.get("lat"), -90, 90), num(data.get("lon"), -180, 180)
    if lat is not None and lon is not None and not (lat == 0 and lon == 0):
        out["lat"], out["lon"] = lat, lon
    v = num(data.get("refresh"), 0, 600000)
    if v is not None:
        out["refresh_ms"] = int(v)
    ver = text(data.get("version"), 60)
    if ver:
        out["version"] = ver
    return out
