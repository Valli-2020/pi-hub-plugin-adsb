"""Statistics history: per-minute aggregates, hourly roll-ups, daily records.

Layout of ``history.json`` (compact arrays keep the file small)::

    {"version": 1,
     "minutes": [[t, aircraft, with_pos, mlat, msg_per_s, max_range_km, unique], ...],
     "hours":   [[t, aircraft, with_pos, mlat, msg_per_s, max_range_km, n_minutes], ...],
     "days":    {"YYYY-MM-DD": {"unique": n, "max_aircraft": n, "furthest": {...},
                                "highest": {...}, "fastest": {...}}},
     "today":   {"day": "YYYY-MM-DD", "hex": [...]}}

Minute rows are kept for 48 hours and then folded into hourly rows; hourly
rows and days older than ``history_days`` are dropped.  ``t`` is the Unix
time at the start of the minute/hour.  Days are local calendar days.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Callable

MINUTE_KEEP_S = 48 * 3600
MAX_TODAY_HEX = 50000
MAX_ALT_FT = 60000
MAX_GS_KT = 1000

# Column indexes of a row.
T, AC, POS, MLAT, MPS, RANGE, UNIQ = range(7)
N = 6          # in hourly rows the last column is the number of minutes


def local_day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def local_midnight(t: float) -> float:
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def atomic_write_json(path: str, data: Any) -> None:
    """Write *data* to *path* so readers see either the old or the new file."""
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _valid_row(r: Any) -> bool:
    return isinstance(r, list) and len(r) == 7 and all(_finite(v) for v in r)


class _Minute:
    """Samples collected during one minute."""

    def __init__(self, start: float) -> None:
        self.start = start
        self.n = 0
        self.ac = self.pos = self.mlat = 0.0
        self.mps_sum, self.mps_n = 0.0, 0
        self.range = 0.0
        self.hexes: set[str] = set()

    def row(self) -> list:
        n = max(1, self.n)
        return [int(self.start), round(self.ac / n, 1), round(self.pos / n, 1),
                round(self.mlat / n, 1),
                round(self.mps_sum / self.mps_n, 1) if self.mps_n else 0.0,
                round(self.range, 1), len(self.hexes)]


class History:
    """Aggregation and persistence.  Not thread-safe: the caller locks."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.minutes: list[list] = []
        self.hours: list[list] = []
        self.days: dict[str, dict] = {}
        self.today = ""
        self.today_hex: set[str] = set()
        self.cur: _Minute | None = None
        self.dirty = False

    # ── Persistence ─────────────────────────────────────────────────────────

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # Keep the broken file for inspection instead of overwriting it.
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return
        if not isinstance(data, dict):
            return
        self.minutes = [r for r in data.get("minutes") or [] if _valid_row(r)]
        self.hours = [r for r in data.get("hours") or [] if _valid_row(r)]
        self.minutes.sort(key=lambda r: r[T])
        self.hours.sort(key=lambda r: r[T])
        days = data.get("days")
        if isinstance(days, dict):
            self.days = {k: v for k, v in days.items()
                         if isinstance(k, str) and len(k) == 10 and isinstance(v, dict)}
        today = data.get("today")
        if isinstance(today, dict) and today.get("day") == local_day(self.clock()):
            hexes = today.get("hex")
            if isinstance(hexes, list):
                self.today = today["day"]
                self.today_hex = {h for h in hexes[:MAX_TODAY_HEX] if isinstance(h, str) and len(h) <= 7}

    def save(self) -> None:
        atomic_write_json(self.path, {
            "version": 1, "minutes": self.minutes, "hours": self.hours, "days": self.days,
            "today": {"day": self.today, "hex": sorted(self.today_hex)},
        })
        self.dirty = False

    # ── Ingest ──────────────────────────────────────────────────────────────

    def add_snapshot(self, snap: dict, now: float, history_days: int) -> None:
        """Account one live snapshot (called after every successful poll)."""
        start = now - now % 60
        if self.cur is not None and self.cur.start != start:
            self._close_minute(history_days)
        if self.cur is None:
            self.cur = _Minute(start)
        m = self.cur
        m.n += 1
        m.ac += snap.get("total", 0)
        m.pos += snap.get("with_pos", 0)
        m.mlat += snap.get("mlat", 0)
        if snap.get("msg_rate") is not None:
            m.mps_sum += snap["msg_rate"]
            m.mps_n += 1

        day = local_day(now)
        if day != self.today:
            self.today, self.today_hex = day, set()
        rec = self.days.setdefault(day, {})
        for ac in snap.get("aircraft", []):
            hx = ac["hex"]
            m.hexes.add(hx)
            if len(self.today_hex) < MAX_TODAY_HEX:
                self.today_hex.add(hx)
            dist = ac.get("dist")
            if dist is not None:
                m.range = max(m.range, dist)
                self._record(rec, "furthest", "km", dist, ac, now)
            alt = ac.get("alt")
            if alt is not None and not ac.get("ground") and alt <= MAX_ALT_FT and "lat" in ac:
                self._record(rec, "highest", "ft", alt, ac, now)
            gs = ac.get("gs")
            if gs is not None and gs <= MAX_GS_KT and "lat" in ac and not ac.get("ground"):
                self._record(rec, "fastest", "kt", gs, ac, now)
        rec["unique"] = len(self.today_hex)
        rec["max_aircraft"] = max(rec.get("max_aircraft", 0), snap.get("total", 0))
        self.dirty = True

    @staticmethod
    def _record(rec: dict, kind: str, unit: str, value: float, ac: dict, now: float) -> None:
        cur = rec.get(kind)
        if isinstance(cur, dict) and _finite(cur.get(unit)) and cur[unit] >= value:
            return
        rec[kind] = {unit: value, "hex": ac["hex"], "flight": ac.get("flight", ""),
                     "type": ac.get("type", ""), "t": int(now)}

    def _close_minute(self, history_days: int) -> None:
        if self.cur is None:
            return
        row = self.cur.row()
        self.cur = None
        if self.minutes and self.minutes[-1][T] >= row[T]:
            return                      # clock went backwards: drop the minute
        self.minutes.append(row)
        self.prune(row[T] + 60, history_days)
        self.dirty = True

    def flush(self, now: float, history_days: int) -> None:
        """Close the running minute if it is over (called from the poll loop)."""
        if self.cur is not None and now - self.cur.start >= 60:
            self._close_minute(history_days)

    # ── Pruning and roll-up ────────────────────────────────────────────────

    def prune(self, now: float, history_days: int) -> None:
        cutoff = now - MINUTE_KEEP_S
        old = [r for r in self.minutes if r[T] < cutoff]
        if old:
            self.minutes = [r for r in self.minutes if r[T] >= cutoff]
            self._fold_into_hours(old)
        keep_from = now - history_days * 86400
        self.hours = [r for r in self.hours if r[T] >= keep_from - 3600]
        oldest_day = local_day(keep_from)
        self.days = {k: v for k, v in self.days.items() if k >= oldest_day}

    def _fold_into_hours(self, rows: list[list]) -> None:
        by_hour = {r[T]: r for r in self.hours}
        for r in rows:
            h = int(r[T] - r[T] % 3600)
            cur = by_hour.get(h)
            if cur is None:
                by_hour[h] = [h, r[AC], r[POS], r[MLAT], r[MPS], r[RANGE], 1]
                continue
            n = cur[N]
            for i in (AC, POS, MLAT, MPS):
                cur[i] = round((cur[i] * n + r[i]) / (n + 1), 1)
            cur[RANGE] = max(cur[RANGE], r[RANGE])
            cur[N] = n + 1
        self.hours = sorted(by_hour.values(), key=lambda r: r[T])

    # ── Queries ─────────────────────────────────────────────────────────────

    def series(self, start: float, end: float, bucket: int) -> list[list]:
        """``[[t, aircraft, with_pos, msg_per_s, max_range_km], ...]`` between
        *start* and *end*, averaged into *bucket*-second steps (range: max)."""
        buckets: dict[int, list] = {}

        def add(t: float, r: list, weight: float) -> None:
            if t < start or t >= end:
                return
            b = int(t - (t - start) % bucket)
            acc = buckets.setdefault(b, [0.0, 0.0, 0.0, 0.0, 0.0])
            acc[0] += r[AC] * weight
            acc[1] += r[POS] * weight
            acc[2] += r[MPS] * weight
            acc[3] = max(acc[3], r[RANGE])
            acc[4] += weight

        for r in self.hours:
            add(r[T], r, r[N])
        for r in self.minutes:
            add(r[T], r, 1)
        out = []
        for b in sorted(buckets):
            acc = buckets[b]
            w = acc[4] or 1
            out.append([b, round(acc[0] / w, 1), round(acc[1] / w, 1),
                        round(acc[2] / w, 1), round(acc[3], 1)])
        return out

    def today_stats(self, now: float) -> dict:
        day = local_day(now)
        rec = dict(self.days.get(day, {}))
        rec["day"] = day
        rec["unique"] = len(self.today_hex) if self.today == day else rec.get("unique", 0)
        return rec

    def best_records(self) -> dict:
        """Best furthest/highest/fastest over the kept days, with their day."""
        best: dict[str, dict] = {}
        for day, rec in self.days.items():
            for kind, unit in (("furthest", "km"), ("highest", "ft"), ("fastest", "kt")):
                r = rec.get(kind)
                if not isinstance(r, dict) or not _finite(r.get(unit)):
                    continue
                if kind not in best or r[unit] > best[kind][unit]:
                    best[kind] = dict(r, day=day)
        return best

    def day_list(self) -> list[dict]:
        out = []
        for day in sorted(self.days, reverse=True):
            rec = self.days[day]
            far = rec.get("furthest") if isinstance(rec.get("furthest"), dict) else {}
            out.append({"day": day, "unique": rec.get("unique", 0),
                        "max_aircraft": rec.get("max_aircraft", 0),
                        "furthest_km": far.get("km")})
        return out
