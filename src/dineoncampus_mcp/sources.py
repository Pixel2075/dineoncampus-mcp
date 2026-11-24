"""Where the server gets its days from: live DineOnCampus requests, or snapshots pushed by `sync`."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from .api import UpstreamError
from .menu import fetch_day, location_hours


class DirectSource:
    """Fetch from DineOnCampus on demand and cache each day in memory.

    Works wherever DineOnCampus accepts requests, which in practice means a home or campus
    connection (it blocks most cloud IPs).
    """

    mode = "direct"

    def __init__(self, api, location_id: str, tz: tzinfo, *, site_id: str | None = None,
                 ttl: float = 1800, empty_ttl: float = 300, hours_ttl: float = 6 * 3600):
        self.api = api
        self.location_id = location_id
        self.site_id = site_id
        self.tz = tz
        self.ttl = ttl
        self.empty_ttl = empty_ttl
        self.hours_ttl = hours_ttl
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._hours: tuple[float, list | None, str | None] | None = None

    def dates(self) -> list[str]:
        return []

    async def get_day(self, day: str) -> dict:
        hit = self._cache.get(day)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        async with self._locks.setdefault(day, asyncio.Lock()):
            hit = self._cache.get(day)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            data = await asyncio.to_thread(self._fetch, day)
            now = time.monotonic()
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[day] = (now + (self.ttl if data["meals"] else self.empty_ttl), data)
            return data

    def _hour_rules(self) -> tuple[list | None, str | None]:
        if not self.site_id:
            return None, None
        if self._hours and self._hours[0] > time.monotonic():
            return self._hours[1], self._hours[2]
        try:
            rules, name = location_hours(self.api, self.site_id, self.location_id)
        except UpstreamError:
            rules, name = None, None  # hours are a nice-to-have; menus still work without them
        self._hours = (time.monotonic() + self.hours_ttl, rules, name)
        return rules, name

    def _fetch(self, day: str) -> dict:
        rules, name = self._hour_rules()
        return fetch_day(self.api, self.location_id, day, tz=self.tz, hours_rules=rules, location_name=name)


class SnapshotSource:
    """Serve days that a home machine pushed to POST /ingest, stored as one JSON file per date."""

    mode = "snapshots"

    def __init__(self, data_dir: str | Path, tz: tzinfo, *, location_id: str | None = None, keep_days: int = 30):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tz = tz
        self.location_id = location_id
        self.keep_days = keep_days
        self._cache: dict[str, tuple[float, dict]] = {}

    def dates(self) -> list[str]:
        return sorted(p.stem for p in self.data_dir.glob("*.json"))

    async def get_day(self, day: str) -> dict | None:
        path = self.data_dir / f"{day}.json"
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return None
        hit = self._cache.get(day)
        if hit and hit[0] == mtime:
            return hit[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        self._cache[day] = (mtime, data)
        return data

    def save(self, data: dict) -> None:
        path = self.data_dir / f"{data['date']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        cutoff = (datetime.now(self.tz).date() - timedelta(days=self.keep_days)).isoformat()
        for old in self.data_dir.glob("*.json"):
            if old.stem < cutoff:
                old.unlink(missing_ok=True)
                self._cache.pop(old.stem, None)
