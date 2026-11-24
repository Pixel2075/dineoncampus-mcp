"""Turn DineOnCampus API responses into the normalized "day" format the server works with.

A day looks like:

    {
      "schema": 1,
      "date": "2026-11-24",
      "location_id": "615f4f93a9f13a32678e5feb",
      "location_name": "Highlander Commons",          # null without a site id
      "hours": {"open": true, "hours": ["7:00am-10:00pm"]},  # null without a site id
      "closed_on_date": false,
      "meals": [{"name": "Lunch", "items": [<item>, ...]}, ...],
      "fetched_at": "2026-11-24T11:00:03+00:00"
    }

and an item:

    {"name", "station", "portion", "description", "ingredients",
     "tags": [...], "allergens": [...], "nutrients": {"calories": 240, "protein_g": 23, ...}}
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone, tzinfo

SCHEMA_VERSION = 1

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Upstream nutrient names differ between nutrition systems ("Protein (g)" vs "Protein",
# "Sugar (g)" vs "Sugars"), so names are matched with any "(unit)" suffix stripped. The unit
# in the output key comes from the nutrient's own `uom`: "Vitamin D (IU)" -> vitamin_d_iu.
NUTRIENT_BASES = {
    "calories": "calories",
    "protein": "protein",
    "total carbohydrates": "carbs",
    "total carbohydrate": "carbs",
    "carbohydrates": "carbs",
    "total fat": "fat",
    "fat": "fat",
    "saturated fat": "saturated_fat",
    "trans fat": "trans_fat",
    "cholesterol": "cholesterol",
    "sodium": "sodium",
    "potassium": "potassium",
    "calcium": "calcium",
    "iron": "iron",
    "dietary fiber": "fiber",
    "fiber": "fiber",
    "sugar": "sugar",
    "sugars": "sugar",
    "total sugars": "sugar",
    "added sugar": "added_sugar",
    "added sugars": "added_sugar",
    "vitamin a": "vitamin_a",
    "vitamin c": "vitamin_c",
    "vitamin d": "vitamin_d",
}
# Derived values that add nothing a model can't compute from the rest.
SKIPPED_NUTRIENTS = {"calories from fat", "fat calories", "saturated fat + trans fat"}
UNIT_ALIASES = {"kcal": "", "cal": "", "µg": "mcg", "ug": "mcg"}

MACRO_KEYS = ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")


# ---------------------------------------------------------------- dates

def resolve_date(value: str | None, tz: tzinfo) -> str:
    """Accept None, 'today', 'tomorrow', 'yesterday', a weekday name or YYYY-MM-DD."""
    today = datetime.now(tz).date()
    v = (value or "").strip().lower()
    if v in ("", "today"):
        return today.isoformat()
    if v == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if v == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    if v in WEEKDAYS:
        return (today + timedelta(days=(WEEKDAYS.index(v) - today.weekday()) % 7)).isoformat()
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise ValueError(
            f"Unrecognized date {value!r}. Use YYYY-MM-DD, 'today', 'tomorrow' or a weekday name."
        ) from None


# ---------------------------------------------------------------- items

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else round(f, 2)


def _text(value):
    if not isinstance(value, str):
        return None
    value = value.replace("�", "").strip()
    return value or None


def nutrient_key(name: str | None, uom: str | None) -> str | None:
    raw = (name or "").strip()
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", raw)
    label, unit_in_name = (m.group(1), m.group(2)) if m else (raw, "")
    if not label or raw.lower() in SKIPPED_NUTRIENTS or label.lower() in SKIPPED_NUTRIENTS:
        return None
    base = NUTRIENT_BASES.get(label.lower()) or _slug(label)
    if base == "calories":
        return "calories"
    unit = (uom or unit_in_name or "").strip().lower()
    unit = UNIT_ALIASES.get(unit, _slug(unit))
    return f"{base}_{unit}" if unit else base


def normalize_item(raw: dict, station: str) -> dict:
    nutrients: dict = {}
    for n in raw.get("nutrients") or []:
        key = nutrient_key(n.get("name"), n.get("uom"))
        if key and nutrients.get(key) is None:
            nutrients[key] = _num(n.get("valueNumeric", n.get("value")))
    if nutrients.get("calories") is None and _num(raw.get("calories")) is not None:
        nutrients["calories"] = _num(raw.get("calories"))
    allergens = []
    for a in raw.get("customAllergens") or []:
        name = a.get("name") if isinstance(a, dict) else a
        if _text(name):
            allergens.append(_text(name))
    return {
        "name": _text(raw.get("name")) or "",
        "station": station,
        "portion": _text(raw.get("portion")),
        "description": _text(raw.get("desc")),
        "ingredients": _text(raw.get("ingredients")),
        "tags": [_text(f["name"]) for f in raw.get("filters") or [] if isinstance(f, dict) and _text(f.get("name"))],
        "allergens": allergens,
        "nutrients": nutrients,
    }


# ---------------------------------------------------------------- hours

def _fmt_time(hour, minute) -> str:
    hour, minute = int(hour or 0), int(minute or 0)
    return f"{hour % 12 or 12}:{minute:02d}{'am' if hour < 12 else 'pm'}"


def _local_date(stamp: str, tz: tzinfo) -> date:
    return datetime.fromisoformat(stamp).astimezone(tz).date()


def hours_for_date(rules: list[dict], day: date, tz: tzinfo) -> dict:
    """Resolve a location's hour rules (from /sites/{id}/hours) for one date.

    Rule types seen upstream: "location" (regular hours), "special_hours" (replaces the
    regular hours on matching days) and "closed" (overrides everything). `days` counts
    0 = Sunday; an empty list means every day in the rule's date range.
    """
    weekday = (day.weekday() + 1) % 7
    active = []
    for rule in rules:
        try:
            start, end = _local_date(rule["start"], tz), _local_date(rule["end"], tz)
        except (KeyError, TypeError, ValueError):
            continue
        days = rule.get("days") or []
        if start <= day <= end and (not days or weekday in days):
            active.append(rule)

    closed = [r for r in active if r.get("type") == "closed"]
    if closed:
        return {"open": False, "note": closed[0].get("name") or "Closed"}
    special = [r for r in active if r.get("type") == "special_hours"]
    chosen = special or [r for r in active if r.get("type") == "location"]
    if not chosen:
        return {"open": None, "note": "No hours published for this date"}

    spans = []
    for r in sorted(chosen, key=lambda r: (int(r.get("startHour") or 0), int(r.get("startMinutes") or 0))):
        if r.get("alwaysOpen"):
            span = "open 24 hours"
        else:
            span = f"{_fmt_time(r.get('startHour'), r.get('startMinutes'))}-{_fmt_time(r.get('endHour'), r.get('endMinutes'))}"
        if span not in spans:
            spans.append(span)
    out: dict = {"open": True, "hours": spans}
    notes = [r["name"] for r in special if r.get("name")] + [r["notes"] for r in chosen if r.get("notes")]
    if notes:
        out["note"] = "; ".join(dict.fromkeys(notes))
    return out


def location_hours(api, site_id: str, location_id: str) -> tuple[list[dict] | None, str | None]:
    """Return (hour rules, location name) for a location, or (None, None) if the site doesn't list it."""
    for loc in api.site_hours(site_id):
        if loc.get("id") == location_id:
            return loc.get("hours") or [], loc.get("name")
    return None, None


# ---------------------------------------------------------------- days

def fetch_day(
    api,
    location_id: str,
    day: str,
    *,
    tz: tzinfo,
    hours_rules: list[dict] | None = None,
    location_name: str | None = None,
    delay: float = 0.0,
) -> dict:
    """Fetch every meal period for a date and return it in the normalized day format."""
    data = api.periods(location_id, day)
    periods = [p for p in data.get("periods") or [] if isinstance(p, dict) and p.get("id")]
    menus: dict[str, dict] = {}
    default = data.get("menu") or {}
    default_id = (default.get("period") or {}).get("id")
    if default_id:
        menus[default_id] = default
    for p in periods:
        if p["id"] not in menus:
            if delay:
                time.sleep(delay)
            menus[p["id"]] = api.menu(location_id, day, p["id"])

    meals = []
    for p in periods:
        categories = ((menus.get(p["id"]) or {}).get("period") or {}).get("categories") or []
        items = [
            normalize_item(it, (cat.get("name") or "").strip())
            for cat in categories
            for it in cat.get("items") or []
        ]
        meals.append({"name": (p.get("name") or "").strip(), "items": items})

    return {
        "schema": SCHEMA_VERSION,
        "date": day,
        "location_id": location_id,
        "location_name": location_name,
        "hours": hours_for_date(hours_rules, date.fromisoformat(day), tz) if hours_rules is not None else None,
        "closed_on_date": any(bool(m.get("closedOnDate")) for m in menus.values()),
        "meals": meals,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def validate_day(payload, expected_location: str | None = None) -> dict:
    """Check a pushed snapshot and fill in missing optional item fields."""
    if not isinstance(payload, dict):
        raise ValueError("snapshot must be a JSON object")
    if payload.get("schema") != SCHEMA_VERSION:
        raise ValueError(
            f"snapshot schema {payload.get('schema')!r} does not match this server's schema "
            f"{SCHEMA_VERSION}; run the same dineoncampus-mcp version for sync and server"
        )
    iso = date.fromisoformat(str(payload.get("date"))).isoformat()
    if expected_location and payload.get("location_id") != expected_location:
        raise ValueError(
            f"snapshot is for location {payload.get('location_id')!r} but this server serves {expected_location!r}"
        )
    meals = payload.get("meals")
    if not isinstance(meals, list):
        raise ValueError("meals must be a list")
    for meal in meals:
        if not isinstance(meal, dict) or not isinstance(meal.get("name"), str) or not isinstance(meal.get("items"), list):
            raise ValueError("each meal must be an object with a name and a list of items")
        for item in meal["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ValueError("each item must be an object with a name")
            if not isinstance(item.get("nutrients", {}), dict):
                raise ValueError("item nutrients must be an object")
            for key, default in (("station", ""), ("portion", None), ("description", None), ("ingredients", None)):
                item.setdefault(key, default)
            for key in ("tags", "allergens"):
                if not isinstance(item.get(key), list):
                    item[key] = []
            item.setdefault("nutrients", {})
    hours = payload.get("hours")
    return {**payload, "date": iso, "hours": hours if isinstance(hours, dict) else None}
