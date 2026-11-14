"""Fake readsb/tar1090 receiver, tile server and link targets (all local).

All data is synthetic: positions around a made-up receiver at 50.0 N, 8.0 E,
made-up hex codes and callsigns.  Nothing here talks to the internet.
"""
from __future__ import annotations

import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = (50.0, 8.0)


def png(color: int = 0) -> bytes:
    """A valid 1x1 PNG; *color* varies the bytes so tiles differ."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"\x00" + bytes([color & 255, 0, 0])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def aircraft_doc(now: float = 1_700_000_000.0, messages: int = 1000) -> dict:
    return {"now": now, "messages": messages, "aircraft": [
        {"hex": "aa0001", "flight": "TEST1   ", "lat": 50.5, "lon": 8.0, "alt_baro": 36000,
         "alt_geom": 36500, "gs": 450.2, "track": 90.0, "baro_rate": -64, "squawk": "1000",
         "category": "A3", "t": "A320", "r": "X-TEST", "seen": 0.4, "seen_pos": 1.2,
         "rssi": -20.5, "mlat": [], "tisb": [], "type": "adsb_icao"},
        {"hex": "aa0002", "flight": "MLAT2", "lat": 50.0, "lon": 9.0, "alt_baro": 4000,
         "gs": 180, "track": 270, "seen": 1.0, "seen_pos": 2.0,
         "mlat": ["lat", "lon", "track", "gs"], "tisb": [], "type": "mlat"},
        {"hex": "aa0003", "alt_baro": "ground", "seen": 3.0, "mlat": [], "tisb": []},
        {"hex": "~bb0004", "flight": "NOPOS", "alt_baro": 12000, "seen": 5.0},
        {"hex": "aa0005", "flight": "BOGUS", "lat": 10.0, "lon": 60.0, "alt_baro": 30000,
         "seen_pos": 1.0},                                  # thousands of km away: dropped
        {"hex": "aa0006", "flight": "STALE", "lat": 50.1, "lon": 8.1, "alt_baro": 5000,
         "seen_pos": 600.0},                                # position too old
        {"hex": "not-hex", "lat": 50.0, "lon": 8.0},        # invalid
        "garbage",
    ]}


def stats_doc() -> dict:
    window = {"start": 1_699_999_940.0, "end": 1_700_000_000.0, "messages": 6000,
              "max_distance": 123456,
              "local": {"signal": -12.3, "noise": -35.1, "peak_signal": -2.0,
                        "strong_signals": 7},
              "tracks": {"all": 42, "single_message": 3}}
    return {"now": 1_700_000_000.0, "gain_db": 43.9, "estimated_ppm": -1.2,
            "aircraft_with_pos": 2, "aircraft_without_pos": 2,
            "last1min": window, "last5min": window, "last15min": window, "total": window}


def receiver_doc() -> dict:
    return {"refresh": 1000, "history": 120, "lat": HOME[0], "lon": HOME[1],
            "version": "readsb test build"}


class _Server:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.daemon_threads = True
        self.httpd.owner = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeReceiver(_Server):
    """Serves /data/{aircraft,stats,receiver}.json; ``fail`` makes it answer 503."""

    def __init__(self):
        self.docs = {"aircraft.json": aircraft_doc(), "stats.json": stats_doc(),
                     "receiver.json": receiver_doc()}
        self.fail = False
        self.hits: dict[str, int] = {}
        self.agents: set[str] = set()
        owner = self

        class H(_Quiet):
            def do_GET(self):
                name = self.path.rsplit("/", 1)[-1]
                owner.hits[name] = owner.hits.get(name, 0) + 1
                owner.agents.add(self.headers.get("User-Agent", ""))
                if owner.fail:
                    return self.send(503, b"{}")
                if not self.path.startswith("/data/") or name not in owner.docs:
                    return self.send(404, b"{}")
                doc = owner.docs[name]
                self.send(200, json.dumps(doc() if callable(doc) else doc).encode())

        super().__init__(H)

    @property
    def data_url(self) -> str:
        return self.base + "/data"


class FakeTiles(_Server):
    """Serves /{z}/{x}/{y}.png; ``mode`` = png | html | error | big."""

    def __init__(self):
        self.mode = "png"
        self.requests: list[str] = []
        self.agents: set[str] = set()
        owner = self

        class H(_Quiet):
            def do_GET(self):
                owner.requests.append(self.path)
                owner.agents.add(self.headers.get("User-Agent", ""))
                if owner.mode == "error":
                    return self.send(500, b"no")
                if owner.mode == "html":
                    return self.send(200, b"<html>nope</html>", "text/html")
                if owner.mode == "big":
                    return self.send(200, png() + b"\0" * (600 * 1024), "image/png")
                if owner.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://example.invalid/x.png")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                parts = self.path.strip("/").split("/")
                color = sum(int(p.split(".")[0]) for p in parts if p.split(".")[0].isdigit())
                self.send(200, png(color), "image/png")

        super().__init__(H)

    @property
    def template(self) -> str:
        return self.base + "/{z}/{x}/{y}.png"


class FakeSite(_Server):
    """Answers every path with a fixed status (for the link probe)."""

    def __init__(self, status: int = 200):
        self.status = status
        owner = self

        class H(_Quiet):
            def do_GET(self):
                self.send(owner.status, b"ok", "text/plain")

        super().__init__(H)


# ── Moving traffic and drawn tiles for the browser harness ─────────────────

def grid_png(z: int, x: int, y: int, dark: bool = False) -> bytes:
    """A 256x256 tile with a faint grid, so panning is visible."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    bg, line = ((38, 41, 46), (58, 62, 70)) if dark else ((232, 234, 228), (205, 208, 200))
    shade = ((x + y) % 2) * 6
    rows = []
    for py in range(256):
        row = bytearray(b"\x00")
        for px in range(256):
            c = line if px % 64 == 0 or py % 64 == 0 else tuple(v - shade for v in bg)
            row += bytes(c)
        rows.append(bytes(row))
    raw = b"".join(rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def fleet_doc(now: float, n: int = 28) -> dict:
    """Synthetic traffic around HOME that moves with time."""
    import math
    aircraft = []
    for i in range(n):
        track = (i * 47) % 360
        speed_kt = 140 + (i * 37) % 360
        dist0 = 15 + (i * 29) % 190
        brg0 = (i * 83) % 360
        km = dist0 + ((now / 3600.0) * speed_kt * 1.852) % 60
        lat = HOME[0] + km / 111.2 * math.cos(math.radians(brg0))
        lon = HOME[1] + km / (111.2 * math.cos(math.radians(HOME[0]))) * math.sin(math.radians(brg0))
        a = {"hex": "%06x" % (0xAB0000 + i), "flight": "TST%03d" % (i * 7), "lat": round(lat, 5),
             "lon": round(lon, 5), "alt_baro": (i * 2900) % 41000 if i % 9 else "ground",
             "gs": speed_kt, "track": track, "baro_rate": ((i % 5) - 2) * 640,
             "squawk": "%04d" % (1000 + i * 11 % 6000) if i % 4 else "7000",
             "category": "A3", "seen": 0.5, "seen_pos": 1.0, "rssi": -10 - i % 20,
             "type": "mlat" if i % 6 == 0 else "adsb_icao",
             "mlat": ["lat", "lon"] if i % 6 == 0 else [], "tisb": []}
        if i % 3 == 0:
            a["t"], a["r"] = "B738", "X-T%03d" % i
        aircraft.append(a)
    for i in range(5):
        aircraft.append({"hex": "%06x" % (0xAC0000 + i), "alt_baro": 20000 + i * 1000, "seen": 4.0})
    return {"now": now, "messages": int(now * 120) % 10_000_000, "aircraft": aircraft}


class LiveReceiver(FakeReceiver):
    """FakeReceiver whose aircraft move with the clock."""

    def __init__(self):
        super().__init__()
        import time as _time
        self.docs["aircraft.json"] = lambda: fleet_doc(_time.time())


class GridTiles(_Server):
    def __init__(self):
        self.requests = 0
        owner = self
        cache: dict = {}

        class H(_Quiet):
            def do_GET(self):
                owner.requests += 1
                parts = self.path.strip("/").split("/")
                try:
                    z, x, y = int(parts[0]), int(parts[1]), int(parts[2].split(".")[0])
                except (ValueError, IndexError):
                    return self.send(404, b"")
                key = (z, x, y)
                if key not in cache:
                    cache[key] = grid_png(z, x, y)
                self.send(200, cache[key], "image/png")

        super().__init__(H)

    @property
    def template(self) -> str:
        return self.base + "/{z}/{x}/{y}.png"
