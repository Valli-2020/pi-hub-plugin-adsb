"""Serve the plugin in the real Pi Hub dashboard on temp state.

Usage: PIHUB_CORE=<pi-hub checkout> python3 tests/harness.py [node-script] [port]

Starts a fake readsb receiver with moving synthetic traffic, a fake tile
server and a fake link target, seeds 26 hours of history, then runs the node
script with HUB, ADMIN, VIEWER (and SHOTS if set) in its environment, or
serves for 10 minutes without one.  Nothing touches a real receiver, tile
provider or Pi Hub install.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

import util
from fakes import FakeSite, GridTiles, LiveReceiver

if not util.CORE:
    sys.exit("Set PIHUB_CORE to a Pi Hub 8.3+ checkout")

from pi_hub import auth, config, server  # noqa: E402
from pi_hub.plugins import manager as mgr_mod  # noqa: E402


def seed_history(path: str, now: float) -> None:
    rows = []
    start = int(now - now % 60) - 26 * 3600
    for i in range(26 * 60):
        t = start + i * 60
        hour = time.localtime(t).tm_hour + time.localtime(t).tm_min / 60
        busy = max(0.0, math.sin((hour - 5) / 24 * 2 * math.pi)) * 0.8 + 0.2
        rows.append([t, round(30 * busy, 1), round(26 * busy, 1), round(3 * busy, 1),
                     round(400 * busy + (i % 7) * 5, 1), round(120 + 110 * busy + (i % 13), 1),
                     int(20 * busy)])
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with open(path, "w") as f:
        json.dump({"version": 1, "minutes": rows, "hours": [], "days": {day: {
            "unique": 0, "max_aircraft": 31,
            "furthest": {"km": 248.3, "hex": "ab0004", "flight": "TST028", "type": "", "t": int(now) - 7200},
            "highest": {"ft": 40600, "hex": "ab0014", "flight": "TST098", "type": "", "t": int(now) - 3600},
            "fastest": {"kt": 497, "hex": "ab0010", "flight": "TST070", "type": "B738", "t": int(now) - 600}}},
            "today": {"day": day, "hex": []}}, f)


def main() -> None:
    script = sys.argv[1] if len(sys.argv) > 1 else None
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    rx, tiles, site = LiveReceiver(), GridTiles(), FakeSite(200)

    tmp = tempfile.mkdtemp(prefix="adsb-harness-")
    auth.USERS_PATH = os.path.join(tmp, "users.json")
    auth.SESSIONS_PATH = os.path.join(tmp, "sessions.json")
    config.CONFIG_PATH = os.path.join(tmp, "config.json")
    with open(os.path.join(util.CORE, "config.example.json")) as f:
        cfg = json.load(f)
    cfg.update({"hosts": [], "services": [], "proxmox": [], "dockge": []})
    cfg.setdefault("auth", {})["enabled"] = True
    with open(config.CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    auth.add_user("admin1", "correct-horse-battery", role="admin")
    auth.add_user("viewer1", "correct-horse-battery", role="viewer")
    admin = auth.issue_session("admin1", "admin", web=True)
    viewer = auth.issue_session("viewer1", "viewer", web=True)

    plugins = os.path.join(tmp, "plugins")
    dst = os.path.join(plugins, "adsb")
    shutil.copytree(util.PLUGIN_SRC, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "history.json*", "tile-cache"))
    with open(os.path.join(dst, "config.json")) as f:
        pcfg = json.load(f)
    pcfg.update({"data_url": rx.data_url, "poll_seconds": 2, "tile_provider": "Custom",
                 "tile_url": tiles.template, "tile_attribution": "Test tiles",
                 "links": [{"label": "tar1090", "url": site.base + "/tar1090/", "probe": True},
                           {"label": "graphs1090", "url": "http://127.0.0.1:9/graphs1090/", "probe": True},
                           {"label": "Feeder status", "url": "https://feeder.example/status", "probe": False}]})
    with open(os.path.join(dst, "config.json"), "w") as f:
        json.dump(pcfg, f)
    seed_history(os.path.join(dst, "history.json"), time.time())

    m = mgr_mod.PluginManager()
    m._root = plugins
    with open(os.path.join(dst, "pihub-plugin.json")) as f:
        declared = json.load(f)["capabilities"]
    ok, why = m.approve("adsb", declared)
    assert ok, why
    m.load_plugin("adsb")
    mgr_mod._instance = m

    httpd = ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    hub = f"http://127.0.0.1:{httpd.server_address[1]}"
    rc = 0
    try:
        time.sleep(1.5)                     # first poll
        if script:
            env = dict(os.environ, HUB=hub, ADMIN=admin, VIEWER=viewer)
            rc = subprocess.run(["node", script], env=env).returncode
        else:
            print(hub, admin, flush=True)
            time.sleep(600)
    finally:
        httpd.shutdown()
        m.unload_plugin("adsb")
        for s in (rx, tiles, site):
            s.close()
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
