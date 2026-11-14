"""Configured links and their online status.

Probes run from the background task, at most once per :data:`PROBE_TTL_S`
per URL, so route handlers and providers only read the cache.  Any HTTP
answer below 500 (including 401/403 and redirects) counts as online: the
point is "does something answer there", not "may I see it".
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Callable

PROBE_TTL_S = 60.0
PROBE_TIMEOUT_S = 4.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_kw):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_probe(url: str, user_agent: str) -> bool:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": user_agent})
    try:
        with _opener.open(req, timeout=PROBE_TIMEOUT_S) as resp:
            resp.read(1024)
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


class LinkProber:
    """Cache of ``url -> (online, checked_at)``.  Callers hold no lock: the
    dict operations used here are atomic in CPython and probes are run by a
    single background task."""

    def __init__(self, user_agent: str,
                 probe: Callable[[str, str], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.user_agent = user_agent
        self._probe = probe or http_probe
        self._clock = clock
        self._cache: dict[str, tuple[bool, float]] = {}

    def due(self, links: list[dict]) -> list[str]:
        now = self._clock()
        urls = []
        for link in links:
            if not link.get("probe"):
                continue
            hit = self._cache.get(link["url"])
            if hit is None or now - hit[1] >= PROBE_TTL_S:
                urls.append(link["url"])
        return list(dict.fromkeys(urls))

    def run_due(self, links: list[dict]) -> int:
        """Probe every link whose result is older than the TTL."""
        urls = self.due(links)
        for url in urls:
            self._cache[url] = (self._probe(url, self.user_agent), self._clock())
        wanted = {link["url"] for link in links}
        for url in list(self._cache):
            if url not in wanted:
                self._cache.pop(url, None)
        return len(urls)

    def status(self, links: list[dict]) -> list[dict]:
        """Links with ``state``: ``online``/``offline``/``unknown``/``off`` (not probed)."""
        out = []
        for link in links:
            item = {"label": link["label"], "url": link["url"], "probe": link["probe"]}
            if not link["probe"]:
                item["state"] = "off"
            else:
                hit = self._cache.get(link["url"])
                item["state"] = "unknown" if hit is None else ("online" if hit[0] else "offline")
                if hit is not None:
                    item["checked"] = int(hit[1])
            out.append(item)
        return out
