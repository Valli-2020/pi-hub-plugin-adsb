"""Configuration: schema for the Configure dialog, defaults, coercion, migration.

Everything a user can tune lives in :func:`config_schema`.  The core
validates values it receives from the dialog, but ``config.json`` can also be
edited by hand, so :func:`effective` coerces every value again before the
plugin uses it.  Nothing here touches the network or the disk.
"""

from __future__ import annotations

import math
import re
import urllib.parse
from typing import Any

TILE_PROVIDERS = ("OpenStreetMap", "CARTO light", "CARTO dark",
                  "CARTO auto (follows theme)", "Custom")

# Built-in tile sources.  "{style}" is resolved to light_all / dark_all.
PROVIDER_TEMPLATES = {
    "OpenStreetMap": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "CARTO light": "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "CARTO dark": "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
}
PROVIDER_ATTRIBUTION = {
    "OpenStreetMap": "© OpenStreetMap contributors",
    "CARTO light": "© OpenStreetMap contributors © CARTO",
    "CARTO dark": "© OpenStreetMap contributors © CARTO",
}
PROVIDER_MAX_ZOOM = {"OpenStreetMap": 19, "CARTO light": 20, "CARTO dark": 20}

DEFAULTS: dict[str, Any] = {
    "data_url": "",
    "poll_seconds": 5,
    "lat": "",
    "lon": "",
    "range_rings_km": "50,100,150,200",
    "tile_provider": "OpenStreetMap",
    "tile_url": "",
    "tile_attribution": "",
    "tile_cache_mb": 64,
    "start_zoom": 8,
    "trail_minutes": 5,
    "show_pill": True,
    "pill_count_positions_only": False,
    "links": [],
    "history_days": 14,
}

# (min, max) for every number field.
NUMBER_LIMITS = {
    "poll_seconds": (2, 60),
    "tile_cache_mb": (0, 2048),
    "start_zoom": (4, 12),
    "trail_minutes": (0, 30),
    "history_days": (1, 90),
}

MAX_LINKS = 30
MAX_RINGS = 10


def config_schema() -> list[dict]:
    """Field list for the 8.3 Configure dialog (see CONFIG_SCHEMA contract)."""
    rx, loc, mp, hd, lk, st = ("Receiver", "Location", "Map", "Header",
                               "Links", "Statistics")
    return [
        {"name": "data_url", "label": "Data URL", "type": "url", "section": rx,
         "default": "", "placeholder": "http://receiver.example.local/tar1090/data",
         "help": "readsb/tar1090 data directory, e.g. "
                 "http://receiver.example.local/tar1090/data. It must serve "
                 "aircraft.json, stats.json and receiver.json."},
        {"name": "poll_seconds", "label": "Refresh every (seconds)", "type": "number",
         "section": rx, "default": 5, "min": 2, "max": 60,
         "help": "How often the receiver is asked for new positions."},

        {"name": "lat", "label": "Latitude", "type": "text", "section": loc,
         "default": "", "placeholder": "e.g. 50.0",
         "help": "Receiver position in decimal degrees. Empty takes it from "
                 "receiver.json."},
        {"name": "lon", "label": "Longitude", "type": "text", "section": loc,
         "default": "", "placeholder": "e.g. 8.0",
         "help": "Empty takes it from receiver.json."},
        {"name": "range_rings_km", "label": "Range rings (km)", "type": "text",
         "section": loc, "default": "50,100,150,200", "placeholder": "50,100,150,200",
         "help": "Comma separated distances, up to 10. Empty hides the rings."},

        {"name": "tile_provider", "label": "Map tiles", "type": "select",
         "section": mp, "default": "OpenStreetMap", "options": list(TILE_PROVIDERS),
         "help": "CARTO auto switches between light and dark with the dashboard."},
        {"name": "tile_url", "label": "Custom tile URL", "type": "url", "section": mp,
         "default": "", "placeholder": "https://tiles.example.org/{z}/{x}/{y}.png",
         "help": "Only for Custom: a template with {z}, {x} and {y}. Use a source "
                 "whose usage policy allows this, and set its attribution below."},
        {"name": "tile_attribution", "label": "Custom attribution", "type": "text",
         "section": mp, "default": "", "placeholder": "© Tile provider",
         "help": "Shown in the corner of the map for Custom tiles."},
        {"name": "tile_cache_mb", "label": "Tile cache (MB)", "type": "number",
         "section": mp, "default": 64, "min": 0, "max": 2048,
         "help": "Tiles are kept on disk in the plugin folder; the least recently "
                 "used are removed first. 0 turns the disk cache off."},
        {"name": "start_zoom", "label": "Start zoom", "type": "number", "section": mp,
         "default": 8, "min": 4, "max": 12},
        {"name": "trail_minutes", "label": "Trail length (minutes)", "type": "number",
         "section": mp, "default": 5, "min": 0, "max": 30,
         "help": "0 hides the trails."},

        {"name": "show_pill", "label": "Show the aircraft count in the header",
         "type": "checkbox", "section": hd, "default": True},
        {"name": "pill_count_positions_only", "label": "Count only aircraft with a position",
         "type": "checkbox", "section": hd, "default": False},

        {"name": "links", "label": "Links", "type": "list", "section": lk,
         "default": [], "max_items": MAX_LINKS,
         "help": "Shortcuts shown on the Flights and Receiver tabs, e.g. tar1090, "
                 "graphs1090 or a feeder status page.",
         "fields": [
             {"name": "label", "label": "Label", "type": "text", "required": True},
             {"name": "url", "label": "URL", "type": "url", "required": True},
             {"name": "probe", "label": "Show online status", "type": "checkbox",
              "default": False},
         ]},

        {"name": "history_days", "label": "Keep history (days)", "type": "number",
         "section": st, "default": 14, "min": 1, "max": 90,
         "help": "Per-minute statistics and daily records older than this are deleted."},
    ]


# ── Coercion ────────────────────────────────────────────────────────────────

_CTRL_RE = re.compile(r"[\x00-\x20\x7f]")


def clean_http_url(value: Any, limit: int = 2000) -> str:
    """Return *value* if it is an http(s) URL with a host, else ''."""
    if not isinstance(value, str):
        return ""
    s = value.strip()
    if not s or len(s) > limit or _CTRL_RE.search(s):
        return ""
    try:
        p = urllib.parse.urlsplit(s)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    return s


def parse_coord(value: Any, limit: float) -> float | None:
    """Decimal degrees within +-limit, or None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(f) or abs(f) > limit:
        return None
    return f


def parse_rings(value: Any) -> list[float]:
    """"50,100" -> [50.0, 100.0]; invalid entries dropped, sorted, max 10."""
    out: list[float] = []
    for part in str(value or "").replace(";", ",").split(","):
        try:
            f = float(part.strip())
        except ValueError:
            continue
        if math.isfinite(f) and 0 < f <= 2000 and f not in out:
            out.append(round(f, 3))
    return sorted(out)[:MAX_RINGS]


def _number(value: Any, name: str) -> int:
    lo, hi = NUMBER_LIMITS[name]
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = float(DEFAULTS[name])
    if not math.isfinite(f):
        f = float(DEFAULTS[name])
    return int(round(min(hi, max(lo, f))))


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def clean_links(value: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for row in value:
        if not isinstance(row, dict):
            continue
        url = clean_http_url(row.get("url"))
        label = str(row.get("label") or "").strip()[:80]
        if not url:
            continue
        out.append({"label": label or urllib.parse.urlsplit(url).hostname or url,
                    "url": url, "probe": _bool(row.get("probe"), False)})
        if len(out) >= MAX_LINKS:
            break
    return out


def effective(raw: Any) -> dict[str, Any]:
    """Config as the plugin uses it: every key present, every value valid."""
    cfg = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    out["data_url"] = clean_http_url(cfg.get("data_url")).rstrip("/")
    for name in NUMBER_LIMITS:
        out[name] = _number(cfg.get(name, DEFAULTS[name]), name)
    out["lat"] = parse_coord(cfg.get("lat"), 90)
    out["lon"] = parse_coord(cfg.get("lon"), 180)
    if out["lat"] is None or out["lon"] is None:
        out["lat"] = out["lon"] = None
    out["rings"] = parse_rings(cfg.get("range_rings_km", DEFAULTS["range_rings_km"]))
    provider = cfg.get("tile_provider")
    out["tile_provider"] = provider if provider in TILE_PROVIDERS else DEFAULTS["tile_provider"]
    out["tile_url"] = clean_http_url(cfg.get("tile_url"))
    out["tile_attribution"] = str(cfg.get("tile_attribution") or "").strip()[:200]
    out["show_pill"] = _bool(cfg.get("show_pill", True), True)
    out["pill_count_positions_only"] = _bool(cfg.get("pill_count_positions_only"), False)
    out["links"] = clean_links(cfg.get("links"))
    return out


def migrate(old_version: str, config: Any) -> dict:
    """Bring a config file from an older plugin version up to date.

    1.0.0 is the first release, so this only fills in missing keys and drops
    values of the wrong type.  Later versions add their steps below, keyed on
    *old_version*.
    """
    cfg = dict(config) if isinstance(config, dict) else {}
    for key, default in DEFAULTS.items():
        if key not in cfg or (isinstance(default, list) and not isinstance(cfg[key], list)):
            cfg[key] = list(default) if isinstance(default, list) else default
    # Example for a future version:
    # if _ver(old_version) < (1, 1, 0): cfg["new_key"] = ...
    return cfg


def version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v or "0").split(".")[:3]:
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)
