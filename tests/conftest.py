import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dineoncampus_mcp.api import DineOnCampus

FIXTURES = Path(__file__).parent / "fixtures"
LOCATION = "615f4f93a9f13a32678e5feb"
SITE = "5c86b28a8e45bf0ccb9ec92b"
TZ = ZoneInfo("America/New_York")


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Serves one fixture file the way apiv4.dineoncampus.com would, and records every request."""

    def __init__(self, fixture: str, status_code: int = 200):
        self.data = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
        self.status_code = status_code
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, dict(params)))
        if self.status_code != 200:
            return FakeResponse(self.status_code, "<html>blocked</html>")
        path = url.split("dineoncampus.com", 1)[1]
        if path.endswith("/periods/"):
            return FakeResponse(200, {**self.data["periods"], "date": params["date"]})
        if path.endswith("/menu"):
            return FakeResponse(200, self.data["menus"][params["period"]])
        if path.endswith("/hours"):
            return FakeResponse(200, self.data.get("hours", {"locations": []}))
        return FakeResponse(404, "not found")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def october_api():
    return DineOnCampus(FakeSession("october_format.json"), retries=0)


@pytest.fixture
def november_api():
    return DineOnCampus(FakeSession("njit_2026-11-24.json"), retries=0)


@pytest.fixture
def hour_rules():
    data = json.loads((FIXTURES / "njit_2026-11-24.json").read_text(encoding="utf-8"))
    return data["hours"]["locations"][0]["hours"]
