"""Minimal client for the public DineOnCampus API (https://apiv4.dineoncampus.com).

Uses requests rather than httpx on purpose: the Cloudflare layer in front of the API
rejects httpx's client fingerprint even from home connections, while a plain requests
session with an identifying User-Agent is accepted.
"""

from __future__ import annotations

import time

import requests

from . import __version__

BASE_URL = "https://apiv4.dineoncampus.com"
USER_AGENT = f"dineoncampus-mcp/{__version__} (+https://github.com/Pixel2075/dineoncampus-mcp)"

BLOCKED_MESSAGE = (
    "DineOnCampus returned 403 Forbidden. Its Cloudflare protection blocks most cloud and "
    "datacenter IP addresses. Run the server on a home connection, or run it in snapshot mode "
    "and feed it with `dineoncampus-mcp sync` from a home machine."
)


class UpstreamError(Exception):
    """DineOnCampus could not be reached or returned an error."""


class UpstreamBlocked(UpstreamError):
    """DineOnCampus refused the request, usually because it came from a cloud IP."""


class DineOnCampus:
    def __init__(self, session=None, *, base_url: str = BASE_URL, timeout: float = 30, retries: int = 2):
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            session.headers["Accept"] = "application/json"
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def get(self, path: str, params: dict | None = None):
        """GET a JSON endpoint, retrying connection errors, 429s and 5xx responses."""
        error: UpstreamError | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
            except requests.RequestException as e:
                error = UpstreamError(f"Could not reach DineOnCampus: {e}")
            else:
                if resp.status_code == 403:
                    raise UpstreamBlocked(BLOCKED_MESSAGE)
                if resp.status_code == 429 or resp.status_code >= 500:
                    error = UpstreamError(f"DineOnCampus returned HTTP {resp.status_code} for {path}")
                elif resp.status_code >= 400:
                    raise UpstreamError(f"DineOnCampus returned HTTP {resp.status_code} for {path}")
                else:
                    try:
                        return resp.json()
                    except ValueError:
                        raise UpstreamError(f"DineOnCampus returned a non-JSON response for {path}") from None
            if attempt < self.retries:
                time.sleep(2**attempt)
        raise error

    def sites(self) -> list[dict]:
        """Every school ("site") on DineOnCampus: id, name, slug."""
        return self.get("/sites/public") or []

    def site_locations(self, site_id: str) -> list[dict]:
        """A school's dining locations, flattened out of their buildings."""
        data = self.get(f"/sites/{site_id}/locations-public", {"for_map": "true", "locale": "en"}) or {}
        out = []
        for building in data.get("buildings") or []:
            for loc in building.get("locations") or []:
                out.append({**loc, "buildingName": loc.get("buildingName") or building.get("buildingName")})
        out.extend(data.get("standaloneLocations") or [])
        return out

    def site_hours(self, site_id: str) -> list[dict]:
        """Opening-hour rules for every location of a school."""
        return (self.get(f"/sites/{site_id}/hours") or {}).get("locations") or []

    def periods(self, location_id: str, day: str) -> dict:
        """Meal periods served on a date. Usually includes the default period's menu too."""
        return self.get(f"/locations/{location_id}/periods/", {"date": day}) or {}

    def menu(self, location_id: str, day: str, period_id: str) -> dict:
        return self.get(f"/locations/{location_id}/menu", {"date": day, "period": period_id}) or {}
