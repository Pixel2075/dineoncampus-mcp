"""Tests for the self-hosted single-machine source (no second PC / no cloud server)."""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from conftest import FakeSession, LOCATION, TZ
from dineoncampus_mcp.api import DineOnCampus
from dineoncampus_mcp.local import SelfHostedSource


def test_refresh_once_persists_days(tmp_path, november_api):
    source = SelfHostedSource(
        november_api, tmp_path, TZ, location_id=LOCATION, site_id="ignored",
        days=2, refresh_seconds=0,
    )
    result = source.refresh_once()
    assert result["saved"], result
    assert len(source.dates()) == len(result["saved"])
    for day in result["saved"]:
        assert (tmp_path / f"{day}.json").exists()


def test_stale_snapshot_served_when_upstream_blocked(tmp_path, november_api):
    """A blocked upstream must not empty already-synced menus."""
    source = SelfHostedSource(
        november_api, tmp_path, TZ, location_id=LOCATION, days=1, refresh_seconds=0,
    )
    source.refresh_once()
    day = datetime.now(TZ).date().isoformat()

    blocked = SelfHostedSource(
        DineOnCampus(FakeSession("njit_2026-11-24.json", status_code=403), retries=0),
        tmp_path, TZ, location_id=LOCATION, days=1, refresh_seconds=0,
    )
    result = blocked.refresh_once()
    assert result["saved"] == []
    assert day in blocked.dates()  # previously synced day still available


def test_get_day_reads_from_snapshot(tmp_path, november_api):
    source = SelfHostedSource(
        november_api, tmp_path, TZ, location_id=LOCATION, days=0, refresh_seconds=0,
    )
    source.refresh_once()
    day = source.dates()[0]
    data = asyncio.run(source.get_day(day))
    assert data is not None
    assert data["date"] == day
    assert data["location_id"] == LOCATION
    assert "meals" in data
