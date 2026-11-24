import gzip
import json

import pytest
from mcp import Client
from starlette.testclient import TestClient

from conftest import LOCATION, SITE, TZ, FakeSession
from dineoncampus_mcp.api import DineOnCampus
from dineoncampus_mcp.menu import fetch_day
from dineoncampus_mcp.server import build_server
from dineoncampus_mcp.sources import DirectSource, SnapshotSource

pytestmark = pytest.mark.anyio
DAY = "2026-10-20"


@pytest.fixture
def direct_server(october_api):
    return build_server(DirectSource(october_api, LOCATION, TZ), tz=TZ)


async def call(server, tool, **args):
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    text = result.content[0].text
    return result.is_error, (text if result.is_error else json.loads(text))


async def test_lists_tools(direct_server):
    async with Client(direct_server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"get_meal_periods", "get_menu", "search_menu", "get_item_nutrition",
                     "calculate_meal_totals", "list_dietary_tags"}


async def test_get_meal_periods(direct_server):
    _, out = await call(direct_server, "get_meal_periods", date=DAY)
    assert [(m["name"], m["item_count"]) for m in out["meals"]] == [("Breakfast", 2), ("Lunch", 4), ("Dinner", 2)]
    assert out["meals"][1]["stations"] == ["The Halal Kitchen", "Flame", "Fajita Station"]


async def test_get_menu_full_and_summary(direct_server):
    _, full = await call(direct_server, "get_menu", meal="lunch", date=DAY)
    tikka = full["meals"][0]["stations"][0]["items"][0]
    assert tikka["name"] == "Chicken Tikka Masala"
    assert tikka["nutrients"]["potassium_mg"] == 720 and tikka["nutrients"]["iron_mg"] == 3.3
    assert "Poultry" in tikka["tags"] and tikka["ingredients"].startswith("Chicken Thigh")

    _, summary = await call(direct_server, "get_menu", meal="lunch", date=DAY, detail="summary")
    row = summary["meals"][0]["stations"][0]["items"][0]
    assert row["protein_g"] == 23 and "potassium_mg" not in row and "nutrients" not in row


async def test_unknown_meal_is_a_readable_error(direct_server):
    is_error, text = await call(direct_server, "get_menu", meal="brunch", date=DAY)
    assert is_error and "Available: Breakfast, Lunch, Dinner" in text


async def test_bad_date_is_a_readable_error(direct_server):
    is_error, text = await call(direct_server, "get_menu", date="someday")
    assert is_error and "Unrecognized date" in text


async def test_search_menu(direct_server):
    _, out = await call(direct_server, "search_menu", date=DAY, min_protein_g=20, sort_by="protein_g")
    assert [i["name"] for i in out["items"]] == ["Cheeseburger on Bun", "Grilled Chili Lime Chicken", "Chicken Tikka Masala"]

    _, vegan = await call(direct_server, "search_menu", date=DAY, tags=["vegan"])
    assert {i["name"] for i in vegan["items"]} == {"Basmati Rice", "Zucchini Lyonnaise"}

    _, no_pork = await call(direct_server, "search_menu", date=DAY, exclude=["pork", "vodka"])
    names = {i["name"] for i in no_pork["items"]}
    assert not names & {"Bacon", "Cheeseburger on Bun", "Vodka Sauce"} and "Scrambled Eggs" in names

    is_error, text = await call(direct_server, "search_menu", date=DAY, sort_by="tastiness")
    assert is_error and "protein_g" in text


async def test_get_item_nutrition(direct_server):
    _, out = await call(direct_server, "get_item_nutrition", item_name="scrambled", date=DAY)
    assert out["found"] and out["items"][0]["nutrients"]["cholesterol_mg"] == 420
    _, missing = await call(direct_server, "get_item_nutrition", item_name="lobster", date=DAY)
    assert missing["found"] is False


async def test_calculate_meal_totals(direct_server):
    _, out = await call(direct_server, "calculate_meal_totals", date=DAY,
                        items=[{"name": "Scrambled Eggs", "servings": 2}, "Chicken Tikka Masala", "lobster"])
    t = out["totals"]
    assert (t["calories"], t["protein_g"], t["carbs_g"], t["fat_g"]) == (660, 51, 16, 42)
    assert t["potassium_mg"] == 1020
    assert out["not_found"] == ["lobster"]
    assert "sugar_g" in out["incomplete"]  # the tikka masala has no sugar value
    assert out["macro_split_pct"] == {"protein": 31, "carbs": 10, "fat": 57}

    is_error, text = await call(direct_server, "calculate_meal_totals", date=DAY, items=[{"name": "Bacon", "servings": 0}])
    assert is_error and "greater than 0" in text


async def test_list_dietary_tags(direct_server):
    _, out = await call(direct_server, "list_dietary_tags", meal="dinner", date=DAY)
    assert list(out["tags"].items()) == [("Alcohol", 1), ("Milk", 1), ("Onion", 1), ("Vegan", 1), ("Vegetarian", 1)]


async def test_blocked_upstream_explains_itself():
    api = DineOnCampus(FakeSession("october_format.json", status_code=403), retries=0)
    server = build_server(DirectSource(api, LOCATION, TZ), tz=TZ)
    is_error, text = await call(server, "get_menu", date=DAY)
    assert is_error and "403" in text and "sync" in text


async def test_hours_come_through_with_a_site_id(november_api):
    server = build_server(DirectSource(november_api, LOCATION, TZ, site_id=SITE), tz=TZ)
    _, out = await call(server, "get_meal_periods", date="2026-11-26")
    assert out["hours"] == {"open": False, "note": "Thanksgiving Break"}
    assert out["location"] == "Highlander Commons"
    assert "closed on this date (Thanksgiving Break)" in out["warning"]
    _, regular = await call(server, "get_meal_periods", date="2026-11-24")
    assert "warning" not in regular and regular["hours"]["hours"] == ["7:00am-10:00pm"]


# ---------------------------------------------------------------- snapshot mode

def snapshot_app(tmp_path, token="secret"):
    source = SnapshotSource(tmp_path, TZ, location_id=LOCATION, keep_days=100_000)
    server = build_server(source, tz=TZ, ingest_token=token)
    return server, server.streamable_http_app(stateless_http=True, json_response=True)


def test_ingest_requires_token_and_stores_snapshot(tmp_path, october_api):
    server, app = snapshot_app(tmp_path)
    day = fetch_day(october_api, LOCATION, DAY, tz=TZ)
    body = gzip.compress(json.dumps(day).encode())
    headers = {"Content-Encoding": "gzip", "Content-Type": "application/json"}
    with TestClient(app) as http:
        assert http.post("/ingest", content=body, headers=headers).status_code == 401
        bad = http.post("/ingest", content=body, headers={**headers, "Authorization": "Bearer wrong"})
        assert bad.status_code == 401
        ok = http.post("/ingest", content=body, headers={**headers, "Authorization": "Bearer secret"})
        assert ok.status_code == 200 and ok.json()["meals"] == ["Breakfast", "Lunch", "Dinner"]
        other = json.dumps({**day, "location_id": "elsewhere"}).encode()
        rejected = http.post("/ingest", content=other, headers={"Authorization": "Bearer secret"})
        assert rejected.status_code == 400 and "location" in rejected.json()["error"]
        assert http.get("/health").json()["latest"] == DAY
    assert (tmp_path / f"{DAY}.json").exists()


async def test_snapshot_server_serves_pushed_days_and_lists_missing_ones(tmp_path, october_api):
    source = SnapshotSource(tmp_path, TZ, location_id=LOCATION, keep_days=100_000)
    source.save(fetch_day(october_api, LOCATION, DAY, tz=TZ))
    server = build_server(source, tz=TZ, ingest_token="secret")
    _, out = await call(server, "search_menu", date=DAY, tags=["vegan"])
    assert out["matches"] == 2
    _, missing = await call(server, "get_menu", date="2026-10-21")
    assert missing["available"] is False and missing["synced_dates"] == [DAY]


def test_ingest_route_only_exists_in_snapshot_mode(october_api):
    server = build_server(DirectSource(october_api, LOCATION, TZ), tz=TZ)
    with TestClient(server.streamable_http_app(stateless_http=True, json_response=True)) as http:
        assert http.post("/ingest", content=b"{}").status_code in (404, 405)
        assert http.get("/health").json()["source"] == "direct"
