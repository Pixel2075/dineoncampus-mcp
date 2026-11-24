from datetime import date, datetime

import pytest

from conftest import LOCATION, TZ
from dineoncampus_mcp.menu import (
    SCHEMA_VERSION,
    fetch_day,
    hours_for_date,
    normalize_item,
    nutrient_key,
    resolve_date,
    validate_day,
)


def test_resolve_date():
    today = datetime.now(TZ).date()
    assert resolve_date(None, TZ) == today.isoformat()
    assert resolve_date("Today", TZ) == today.isoformat()
    assert resolve_date("tomorrow", TZ) == date.fromordinal(today.toordinal() + 1).isoformat()
    assert resolve_date("2026-10-20", TZ) == "2026-10-20"
    friday = date.fromisoformat(resolve_date("friday", TZ))
    assert friday.weekday() == 4 and 0 <= (friday - today).days < 7
    with pytest.raises(ValueError, match="Unrecognized date"):
        resolve_date("next blursday", TZ)


@pytest.mark.parametrize(
    ("name", "uom", "key"),
    [
        ("Calories", "kcal", "calories"),
        ("Protein (g)", "g", "protein_g"),  # October 2026 naming
        ("Protein", "g", "protein_g"),  # November 2026 naming
        ("Total Carbohydrates (g)", "g", "carbs_g"),
        ("Total Carbohydrates", "g", "carbs_g"),
        ("Sugar (g)", "g", "sugar_g"),
        ("Sugars", "g", "sugar_g"),
        ("Dietary Fiber", "g", "fiber_g"),
        ("Vitamin D (IU)", "IU", "vitamin_d_iu"),
        ("Vitamin D", "mcg", "vitamin_d_mcg"),
        ("Sodium (mg)", None, "sodium_mg"),
        ("Calories From Fat", "kcal", None),
        ("Fat Calories", "kcal", None),
        ("Saturated Fat + Trans Fat (g)", "g", None),
    ],
)
def test_nutrient_key(name, uom, key):
    assert nutrient_key(name, uom) == key


def test_normalize_item_handles_missing_values_and_garbage():
    item = normalize_item(
        {
            "name": " Club ",
            "portion": "�",
            "desc": "",
            "calories": 440.0,
            "nutrients": [
                {"name": "Total Fat", "uom": "g", "valueNumeric": 18.0},
                {"name": "Vitamin A (RE)", "uom": "RE", "valueNumeric": "-"},
            ],
            "filters": [{"name": "Vegan"}, {"name": ""}],
            "customAllergens": [{"name": "Peanut"}],
        },
        "Deli",
    )
    assert item == {
        "name": "Club",
        "station": "Deli",
        "portion": None,
        "description": None,
        "ingredients": None,
        "tags": ["Vegan"],
        "allergens": ["Peanut"],
        "nutrients": {"fat_g": 18, "vitamin_a_re": None, "calories": 440},
    }


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2026-11-24", {"open": True, "hours": ["7:00am-10:00pm"]}),  # regular Tuesday
        ("2026-11-25", {"open": True, "hours": ["7:00am-8:00pm"], "note": "Wednesday before Thanksgiving"}),
        ("2026-11-26", {"open": False, "note": "Thanksgiving Break"}),
        ("2026-11-28", {"open": False, "note": "Thanksgiving Break"}),
        ("2026-11-29", {"open": True, "hours": ["3:00pm-8:00pm"], "note": "Sunday after Thanksgiving"}),
        ("2026-12-04", {"open": True, "hours": ["7:00am-9:00pm"]}),  # regular Friday
        ("2026-12-05", {"open": True, "hours": ["9:00am-8:00pm"]}),  # regular Saturday
        ("2027-01-05", {"open": None, "note": "No hours published for this date"}),
    ],
)
def test_hours_for_date(hour_rules, day, expected):
    assert hours_for_date(hour_rules, date.fromisoformat(day), TZ) == expected


def test_fetch_day_reuses_default_menu(october_api):
    day = fetch_day(october_api, LOCATION, "2026-10-20", tz=TZ)
    assert [m["name"] for m in day["meals"]] == ["Breakfast", "Lunch", "Dinner"]
    assert [len(m["items"]) for m in day["meals"]] == [2, 4, 2]
    # Breakfast arrives inside the periods response, so only lunch and dinner are requested.
    requested = [params.get("period") for url, params in october_api.session.calls if url.endswith("/menu")]
    assert requested == ["p-lunch", "p-dinner"]
    eggs = day["meals"][0]["items"][0]
    assert eggs["station"] == "The Daily Plate"
    assert eggs["nutrients"]["protein_g"] == 14
    assert eggs["nutrients"]["vitamin_d_iu"] == 120
    assert "calories_from_fat" not in eggs["nutrients"]
    assert day["schema"] == SCHEMA_VERSION and day["hours"] is None


def test_fetch_day_november_format(november_api, hour_rules):
    day = fetch_day(november_api, LOCATION, "2026-11-24", tz=TZ, hours_rules=hour_rules, location_name="Highlander Commons")
    assert [m["name"] for m in day["meals"]] == ["Breakfast", "Lunch", "Dinner"]
    assert day["hours"] == {"open": True, "hours": ["7:00am-10:00pm"]}
    item = day["meals"][1]["items"][0]
    assert {"calories", "fat_g", "sodium_mg"} <= set(item["nutrients"])


def test_validate_day():
    good = {"schema": SCHEMA_VERSION, "date": "2026-10-20", "location_id": LOCATION,
            "meals": [{"name": "Lunch", "items": [{"name": "Soup", "nutrients": {"calories": 100}}]}]}
    out = validate_day(good, expected_location=LOCATION)
    assert out["meals"][0]["items"][0]["tags"] == [] and out["hours"] is None
    with pytest.raises(ValueError, match="schema"):
        validate_day({**good, "schema": 99})
    with pytest.raises(ValueError, match="location"):
        validate_day(good, expected_location="somewhere-else")
    with pytest.raises(ValueError):
        validate_day({**good, "date": "not-a-date"})
    with pytest.raises(ValueError, match="meals"):
        validate_day({**good, "meals": "lots"})
