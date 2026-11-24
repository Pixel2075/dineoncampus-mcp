"""Self-contained single-machine source: fetch direct and persist to local snapshots.

This replaces the two-machine setup (home PC syncing to a cloud server). One box
fetches from DineOnCampus on a schedule, writes dated snapshots to a local directory,
and serves those files. If an upstream fetch fails, the last good snapshot is still
served instead of losing the day.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from .api import UpstreamError
from .menu import fetch_day, location_hours
from .sources import SnapshotSource


class SelfHostedSource:
    """Single-box source: scheduled direct fetch + local snapshot persistence.

    A background thread refreshes today plus the next `days` days every
    `refresh_seconds`, writing each successful day through SnapshotSource.save so
    the files are atomically replaced and old ones pruned. Reads always come from
    the local snapshot files, so an upstream outage or block never empties the
    menus. `start()` must be called once before serving; `stop()` is best-effort.
    """

    mode = "local"

    def __init__(self, api, data_dir: str | Path, tz: tzinfo, *, location_id: str,
                 site_id: str | None = None, days: int = 7,
                 refresh_seconds: float = 6 * 3600, log=None):
        self.store = SnapshotSource(data_dir, tz, location_id=location_id)
        self.api = api
        self.location_id = location_id
        self.site_id = site_id
        self.tz = tz
        self.days = days
        self.refresh_seconds = refresh_seconds
        self.log = log or (lambda message: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hours: tuple[float, list | None, str | None] | None = None
        self._lock = threading.Lock()

    @property
    def location_name(self):
        return self.store.location_id

    def dates(self) -> list[str]:
        return self.store.dates()

    async def get_day(self, day: str) -> dict | None:
        return await self.store.get_day(day)

    def _hour_rules(self):
        if not self.site_id:
            return None, None
        if self._hours and self._hours[0] > time.monotonic():
            return self._hours[1], self._hours[2]
        try:
            rules, name = location_hours(self.api, self.site_id, self.location_id)
        except UpstreamError:
            rules, name = None, None  # hours optional; keep serving menus
        self._hours = (time.monotonic() + 6 * 3600, rules, name)
        return rules, name

    def refresh_once(self) -> dict:
        """Fetch today..today+days and persist each successful day. Returns a summary."""
        if not self._lock.acquire(blocking=False):
            return {"skipped": "a refresh is already running"}
        try:
            rules, name = self._hour_rules()
            today = datetime.now(self.tz).date()
            saved, failed, skipped = [], [], []
            for offset in range(self.days + 1):
                day = (today + timedelta(days=offset)).isoformat()
                try:
                    data = fetch_day(self.api, self.location_id, day, tz=self.tz,
                                     hours_rules=rules, location_name=name, delay=1.0)
                except UpstreamError as e:
                    failed.append(day)
                    self.log(f"{day}: fetch failed: {e}")
                    continue
                closed = data["closed_on_date"] or (data["hours"] or {}).get("open") is False
                if not data["meals"] and not closed:
                    skipped.append(day)  # keep any previous snapshot for this date
                    continue
                self.store.save(data)
                saved.append(day)
            return {"saved": saved, "failed": failed, "skipped": skipped,
                    "available": self.store.dates()}
        finally:
            self._lock.release()

    def _loop(self) -> None:
        # Refresh immediately on start, then on a fixed interval.
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as e:  # keep the worker thread alive
                self.log(f"refresh error: {e}")
            self._stop.wait(self.refresh_seconds)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dineoncampus-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
