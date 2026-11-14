"""Map tiles for the sandboxed frame.

The frame may not make network requests (``connect-src 'none'``) and only
shows ``data:`` images, so the server fetches tiles, keeps them in a small
LRU disk cache and hands them over as base64 in batches.

Safety: the tile URL is built only from the configured template (a built-in
provider or the admin's Custom URL) with integer z/x/y, and the resulting host
must equal the template's host.  Requests carry an identifying User-Agent as
the OpenStreetMap tile usage policy asks, never follow redirects, and only
image bodies up to 512 KB are accepted.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable

from .settings import PROVIDER_ATTRIBUTION, PROVIDER_MAX_ZOOM, PROVIDER_TEMPLATES

MAX_TILE_BYTES = 512 * 1024
MAX_BATCH = 30
MAX_RESPONSE_CHARS = 1_750_000       # the dashboard bridge refuses > 2 MB
FRESH_S = 14 * 86400                 # re-fetch older tiles (stale ones still served)
FAIL_TTL_S = 60.0
FETCH_TIMEOUT_S = 6.0
BATCH_DEADLINE_S = 10.0
FETCH_WORKERS = 2                    # OSM policy: few parallel connections

MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
         (b"RIFF", "image/webp"))


class TileError(ValueError):
    """Bad request parameters or configuration."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_kw):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def sniff(data: bytes) -> str | None:
    for magic, mime in MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                return None
            return mime
    return None


def resolve_source(settings: dict, theme: str) -> dict:
    """Template, attribution, max zoom and a cache key for the configured source."""
    provider = settings["tile_provider"]
    if provider == "CARTO auto (follows theme)":
        provider = "CARTO dark" if theme == "dark" else "CARTO light"
    if provider == "Custom":
        template = settings.get("tile_url") or ""
        if not template:
            raise TileError("Set a custom tile URL in the plugin settings")
        attribution = settings.get("tile_attribution") or ""
        max_zoom = 19
    else:
        template = PROVIDER_TEMPLATES[provider]
        attribution = PROVIDER_ATTRIBUTION[provider]
        max_zoom = PROVIDER_MAX_ZOOM[provider]
    check_template(template)
    key = hashlib.sha256(template.encode()).hexdigest()[:16]
    return {"template": template, "attribution": attribution, "max_zoom": max_zoom,
            "key": key, "provider": provider}


def check_template(template: str) -> str:
    """Raise unless *template* is an http(s) URL with {z} {x} {y} outside the host."""
    try:
        p = urllib.parse.urlsplit(template)
    except ValueError:
        raise TileError("Invalid tile URL") from None
    if p.scheme not in ("http", "https") or not p.hostname:
        raise TileError("The tile URL must start with http:// or https://")
    if p.username or p.password:
        raise TileError("The tile URL must not contain a user name or password")
    if "{" in p.netloc or "}" in p.netloc:
        raise TileError("Placeholders are not allowed in the tile host")
    rest = template[len(p.scheme) + 3 + len(p.netloc):]
    for ph in ("{z}", "{x}", "{y}"):
        if ph not in rest:
            raise TileError("The tile URL needs {z}, {x} and {y}")
    leftover = rest.replace("{z}", "").replace("{x}", "").replace("{y}", "")
    if "{" in leftover or "}" in leftover:
        raise TileError("Only {z}, {x} and {y} are supported in the tile URL")
    return p.hostname


def tile_url(template: str, z: int, x: int, y: int) -> str:
    for v in (z, x, y):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise TileError("tile coordinates must be non-negative integers")
    url = template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
    if urllib.parse.urlsplit(url).hostname != urllib.parse.urlsplit(template).hostname:
        raise TileError("tile host mismatch")
    return url


def parse_int(query: dict, name: str, lo: int, hi: int, default: int | None = None) -> int:
    raw = query.get(name)
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if raw is None or raw == "":
        if default is None:
            raise TileError(f"{name} is required")
        return default
    s = str(raw)
    if not (s.isdigit() or (s.startswith("-") and s[1:].isdigit())) or len(s) > 9:
        raise TileError(f"{name} must be an integer")
    v = int(s)
    if v < lo or v > hi:
        raise TileError(f"{name} must be between {lo} and {hi}")
    return v


class TileCache:
    """Disk cache ``<root>/<source key>/<z>/<x>/<y>`` with LRU eviction."""

    def __init__(self, root: str, user_agent: str,
                 fetch: Callable[[str, str], tuple[bytes, str]] | None = None) -> None:
        self.root = root
        self.user_agent = user_agent
        self.max_bytes = 64 * 1024 * 1024
        self._fetch = fetch or self._http_fetch
        self._lock = threading.Lock()
        self._index: dict[str, list] | None = None   # path -> [size, last_used]
        self._total = 0
        self._failed: dict[str, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS,
                                        thread_name_prefix="adsb-tiles")

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def set_limit(self, mb: int) -> None:
        with self._lock:
            self.max_bytes = max(0, int(mb)) * 1024 * 1024
            self._ensure_index()
            self._evict()

    # ── Index ──────────────────────────────────────────────────────────────

    def _ensure_index(self) -> None:
        if self._index is not None:
            return
        self._index, self._total = {}, 0
        if not os.path.isdir(self.root):
            return
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                path = os.path.join(dirpath, fn)
                if ".tmp" in fn:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                self._index[path] = [st.st_size, st.st_mtime]
                self._total += st.st_size

    def _evict(self) -> None:
        if self._total <= self.max_bytes:
            return
        for path, _ in sorted(self._index.items(), key=lambda kv: kv[1][1]):
            if self._total <= self.max_bytes:
                break
            size = self._index.pop(path)[0]
            self._total -= size
            try:
                os.unlink(path)
            except OSError:
                pass

    def total_bytes(self) -> int:
        with self._lock:
            self._ensure_index()
            return self._total

    def _path(self, key: str, z: int, x: int, y: int) -> str:
        return os.path.join(self.root, key, str(z), str(x), str(y))

    # ── Reading and fetching ───────────────────────────────────────────────

    def _read_cached(self, path: str) -> tuple[bytes, float] | None:
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_TILE_BYTES + 1)
            mtime = os.stat(path).st_mtime
        except OSError:
            return None
        if len(data) > MAX_TILE_BYTES or not sniff(data):
            return None
        return data, mtime

    def _store(self, path: str, data: bytes) -> None:
        if self.max_bytes <= 0 or len(data) > self.max_bytes:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp-{threading.get_ident()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        now = time.time()
        with self._lock:
            self._ensure_index()
            old = self._index.get(path)
            if old:
                self._total -= old[0]
            self._index[path] = [len(data), now]
            self._total += len(data)
            self._evict()

    def _touch(self, path: str) -> None:
        with self._lock:
            entry = self._index.get(path) if self._index is not None else None
            if entry:
                entry[1] = time.time()

    def _http_fetch(self, url: str, _key: str) -> tuple[bytes, str]:
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                                                   "Accept": "image/png,image/*;q=0.8"})
        with _opener.open(req, timeout=FETCH_TIMEOUT_S) as resp:
            data = resp.read(MAX_TILE_BYTES + 1)
        return data, ""

    def get(self, source: dict, z: int, x: int, y: int) -> bytes | None:
        """One tile as bytes, from the cache or the provider (None if unavailable)."""
        path = self._path(source["key"], z, x, y)
        cached = self._read_cached(path)
        if cached and time.time() - cached[1] < FRESH_S:
            self._touch(path)
            return cached[0]
        url = tile_url(source["template"], z, x, y)
        failed_at = self._failed.get(url)
        if failed_at and time.time() - failed_at < FAIL_TTL_S:
            return cached[0] if cached else None
        try:
            data, _ = self._fetch(url, source["key"])
        except (urllib.error.URLError, OSError, ValueError):
            data = b""
        if not data or len(data) > MAX_TILE_BYTES or not sniff(data):
            if len(self._failed) > 2000:
                self._failed.clear()
            self._failed[url] = time.time()
            return cached[0] if cached else None
        self._failed.pop(url, None)
        try:
            self._store(path, data)
        except OSError:
            pass
        return data

    def batch(self, source: dict, z: int, x0: int, y0: int, w: int, h: int) -> dict:
        """Up to :data:`MAX_BATCH` tiles of the rectangle starting at (x0, y0).

        x wraps around the antimeridian; rows outside the world are skipped.
        Tiles that do not fit into the response budget or the time budget are
        listed in ``missing`` so the frame asks again.
        """
        n = 1 << z
        coords = []
        for dy in range(h):
            y = y0 + dy
            if y < 0 or y >= n:
                continue
            for dx in range(w):
                coords.append(((x0 + dx) % n, y))
        coords = list(dict.fromkeys(coords))[:MAX_BATCH]
        futures = {self._pool.submit(self.get, source, z, x, y): (x, y) for x, y in coords}
        done, _pending = wait(futures, timeout=BATCH_DEADLINE_S)
        tiles, missing, failed, size = [], [], [], 0
        for fut, (x, y) in futures.items():
            if fut not in done:
                missing.append([x, y])
                continue
            try:
                data = fut.result()
            except Exception:           # noqa: BLE001 - one bad tile must not fail the batch
                data = None
            if data is None:
                failed.append([x, y])
                continue
            url = "data:%s;base64,%s" % (sniff(data), base64.b64encode(data).decode("ascii"))
            if size + len(url) > MAX_RESPONSE_CHARS:
                missing.append([x, y])
                continue
            size += len(url)
            tiles.append({"x": x, "y": y, "src": url})
        return {"z": z, "tiles": tiles, "missing": missing, "failed": failed,
                "attribution": source["attribution"], "max_zoom": source["max_zoom"],
                "source": source["key"]}
