"""MCP tools over a menu source, plus the /health and /ingest HTTP routes."""

from __future__ import annotations

import hmac
import json
import zlib
from datetime import tzinfo
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .api import UpstreamError
from .menu import MACRO_KEYS, resolve_date, validate_day
from .sources import SnapshotSource

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
MAX_INGEST_BYTES = 16 * 1024 * 1024
MAX_INGEST_DECOMPRESSED = 64 * 1024 * 1024

INSTRUCTIONS = """\
Menus, opening hours and nutrition facts for a campus dining hall, from DineOnCampus.

Meals are usually Breakfast, Lunch and Dinner. Every item has its portion size, calories and \
macros, and where the dining hall publishes them: micronutrients, ingredients and diet/allergen \
tags. All values are per listed portion. Dates accept YYYY-MM-DD, 'today', 'tomorrow' or a \
weekday name, resolved in the dining hall's timezone.

Start with get_meal_periods (what is served and the opening hours), then get_menu for the full \
menu of a meal. search_menu filters by text, tags and macro limits; get_item_nutrition returns \
one item's full label; calculate_meal_totals adds up a plate.

The data comes straight from the dining hall's nutrition system and can be incomplete or wrong. \
Point it out when values look implausible, for example 0 g carbs on a sandwich."""


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def compact(item: dict) -> dict:
    """Small per-item view: name, portion, core macros, tags."""
    n = item["nutrients"]
    out = {"name": item["name"], "portion": item["portion"], **{k: n.get(k) for k in MACRO_KEYS}}
    if item["tags"]:
        out["tags"] = item["tags"]
    return out


def full_view(item: dict) -> dict:
    """Everything known about an item, minus empty fields and the meal/station it is grouped under."""
    out = {k: v for k, v in item.items() if k not in ("meal", "station", "nutrients") and v not in (None, [], "")}
    out["nutrients"] = {k: v for k, v in item["nutrients"].items() if v is not None}
    return out


def has_tags(item: dict, tags: list[str]) -> bool:
    have = [t.lower() for t in item["tags"]]
    return all(any(want.lower() in h for h in have) for want in tags)


def lacks_terms(item: dict, terms: list[str]) -> bool:
    """True if none of the terms appear in the item's name, tags, allergens, description or ingredients."""
    blob = " ".join([item["name"], *item["tags"], *item["allergens"], item["description"] or "", item["ingredients"] or ""]).lower()
    return not any(t.lower() in blob for t in terms)


def protein_density(item: dict):
    n = item["nutrients"]
    if n.get("calories") and n.get("protein_g") is not None:
        return round(n["protein_g"] / n["calories"] * 100, 1)
    return None


def closed_warning(day: dict) -> dict:
    """Flag days whose hours say closed even though upstream still lists a menu (it does on holidays)."""
    hours = day.get("hours") or {}
    if day["meals"] and (day.get("closed_on_date") or hours.get("open") is False):
        note = f" ({hours['note']})" if hours.get("note") else ""
        return {"warning": f"The dining hall is closed on this date{note}, although a menu is published."}
    return {}


def find_item(pool: list[dict], name: str) -> list[dict]:
    want = name.strip().lower()
    if not want:
        return []
    return [i for i in pool if i["name"].lower() == want] or [i for i in pool if want in i["name"].lower()]


def _gunzip(data: bytes) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, MAX_INGEST_DECOMPRESSED)
    if d.unconsumed_tail:
        raise ValueError("decompressed snapshot is too large")
    if not d.eof:
        raise ValueError("truncated gzip body")
    return out


def build_server(source, *, tz: tzinfo, ingest_token: str | None = None) -> MCPServer:
    mcp = MCPServer(
        name="dineoncampus",
        title="Dining Hall Menus & Nutrition",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    async def load(date: str | None) -> tuple[str, dict | None]:
        try:
            iso = resolve_date(date, tz)
        except ValueError as e:
            raise ToolError(str(e)) from None
        try:
            return iso, await source.get_day(iso)
        except UpstreamError as e:
            raise ToolError(str(e)) from None

    def unavailable(iso: str, day: dict | None) -> str | None:
        """A ready-to-return answer when there is nothing to show for the date, else None."""
        if day is None:
            return dumps({
                "date": iso,
                "available": False,
                "message": "No menu has been synced for this date.",
                "synced_dates": source.dates(),
            })
        if not day["meals"]:
            hours = day.get("hours") or {}
            closed = day.get("closed_on_date") or hours.get("open") is False
            return dumps({
                "date": iso,
                "location": day.get("location_name"),
                "hours": day.get("hours"),
                "meals": [],
                "message": "The dining hall is closed on this date." if closed
                else "No meals are published for this date yet.",
            })
        return None

    def items_for(day: dict, meal: str | None) -> list[dict]:
        meals = day["meals"]
        if meal:
            want = meal.strip().lower()
            picked = [m for m in meals if m["name"].lower() == want] or [m for m in meals if want in m["name"].lower()]
            if not picked:
                names = ", ".join(m["name"] for m in meals) or "none"
                raise ToolError(f"No meal period {meal!r} on {day['date']}. Available: {names}.")
            meals = picked
        return [{**item, "meal": m["name"]} for m in meals for item in m["items"]]

    # ------------------------------------------------------------ HTTP routes

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        body: dict[str, Any] = {"ok": True, "version": __version__, "source": source.mode,
                                "location_id": source.location_id}
        if isinstance(source, SnapshotSource):
            dates = source.dates()
            body.update(synced_dates=len(dates), first=dates[0] if dates else None, latest=dates[-1] if dates else None)
        return JSONResponse(body)

    if isinstance(source, SnapshotSource):

        @mcp.custom_route("/ingest", methods=["POST"])
        async def ingest(request: Request) -> JSONResponse:
            """Receive one day's snapshot from `dineoncampus-mcp sync`."""
            auth = request.headers.get("authorization", "").encode()
            if not ingest_token or not hmac.compare_digest(auth, f"Bearer {ingest_token}".encode()):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > MAX_INGEST_BYTES:
                return JSONResponse({"error": "body too large"}, status_code=413)
            body = await request.body()
            if len(body) > MAX_INGEST_BYTES:
                return JSONResponse({"error": "body too large"}, status_code=413)
            try:
                if request.headers.get("content-encoding", "").lower() == "gzip":
                    body = _gunzip(body)
                data = validate_day(json.loads(body), expected_location=source.location_id)
            except (ValueError, zlib.error) as e:
                return JSONResponse({"error": f"bad snapshot: {e}"}, status_code=400)
            source.save(data)
            return JSONResponse({"ok": True, "date": data["date"], "meals": [m["name"] for m in data["meals"]]})

    # ------------------------------------------------------------ tools

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_meal_periods(date: str | None = None) -> str:
        """What is served on a date: each meal period with its item count and stations, plus the opening hours.

        Args:
            date: YYYY-MM-DD, 'today', 'tomorrow' or a weekday name. Defaults to today.
        """
        iso, day = await load(date)
        if msg := unavailable(iso, day):
            return msg
        return dumps({
            "date": iso,
            "location": day.get("location_name"),
            "hours": day.get("hours"),
            **closed_warning(day),
            "meals": [
                {"name": m["name"], "item_count": len(m["items"]),
                 "stations": list(dict.fromkeys(i["station"] for i in m["items"]))}
                for m in day["meals"]
            ],
            "fetched_at": day.get("fetched_at"),
        })

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_menu(meal: str | None = None, date: str | None = None, station: str | None = None,
                       detail: str = "full") -> str:
        """The menu for a meal, grouped by station.

        detail='full' (default) gives every item with portion, description, ingredients, diet/allergen
        tags and every published nutrient (calories, macros, saturated/trans fat, cholesterol, sodium,
        potassium, calcium, iron, vitamins). detail='summary' gives only calories and core macros,
        about half the size.

        Args:
            meal: 'breakfast', 'lunch', 'dinner' or any other period name. Omit for every meal that day.
            date: YYYY-MM-DD, 'today', 'tomorrow' or a weekday name. Defaults to today.
            station: keep only stations whose name contains this text, e.g. 'grill' or 'salad'.
            detail: 'full' or 'summary'.
        """
        if detail not in ("full", "summary"):
            raise ToolError("detail must be 'full' or 'summary'")
        iso, day = await load(date)
        if msg := unavailable(iso, day):
            return msg
        items = items_for(day, meal)
        if station:
            items = [i for i in items if station.lower() in i["station"].lower()]
        view = compact if detail == "summary" else full_view
        meals: dict[str, dict[str, list]] = {}
        for item in items:
            meals.setdefault(item["meal"], {}).setdefault(item["station"], []).append(view(item))
        return dumps({
            "date": iso,
            "location": day.get("location_name"),
            "hours": day.get("hours"),
            **closed_warning(day),
            "units": "calories in kcal; _g grams; _mg milligrams; _iu IU; all per listed portion",
            "meals": [
                {"meal": m, "stations": [{"station": s, "items": its} for s, its in stations.items()]}
                for m, stations in meals.items()
            ],
        })

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def search_menu(
        query: str | None = None,
        meal: str | None = None,
        date: str | None = None,
        tags: list[str] | None = None,
        exclude: list[str] | None = None,
        min_protein_g: float | None = None,
        max_calories: float | None = None,
        max_carbs_g: float | None = None,
        max_fat_g: float | None = None,
        sort_by: str | None = None,
        descending: bool = True,
        limit: int = 25,
    ) -> str:
        """Search and filter menu items by text, diet tags, excluded ingredients and macro limits, optionally sorted.

        Examples: high-protein lunch -> meal='lunch', min_protein_g=25, sort_by='protein_g';
        vegan dinner under 500 kcal -> meal='dinner', tags=['vegan'], max_calories=500;
        nothing with peanuts -> exclude=['peanut'].

        Args:
            query: text matched against item name, station, description and ingredients.
            meal: 'breakfast', 'lunch', 'dinner'. Omit to search the whole day.
            date: YYYY-MM-DD, 'today', 'tomorrow' or a weekday name. Defaults to today.
            tags: every tag must match (substring, case-insensitive), e.g. ['vegan']. See list_dietary_tags.
            exclude: drop items whose name, tags, allergens, description or ingredients mention any of these.
            min_protein_g: keep items with at least this much protein.
            max_calories: keep items at or under this many kcal.
            max_carbs_g: keep items at or under this many grams of carbohydrate.
            max_fat_g: keep items at or under this many grams of fat.
            sort_by: any nutrient key (calories, protein_g, carbs_g, fat_g, fiber_g, sodium_mg, ...) or protein_per_100kcal.
            descending: sort high-to-low (default) or low-to-high.
            limit: maximum number of items to return (default 25).
        """
        iso, day = await load(date)
        if msg := unavailable(iso, day):
            return msg
        items = items_for(day, meal)

        if query:
            q = query.lower()
            items = [i for i in items
                     if q in " ".join([i["name"], i["station"], i["description"] or "", i["ingredients"] or ""]).lower()]
        if tags:
            items = [i for i in items if has_tags(i, tags)]
        if exclude:
            items = [i for i in items if lacks_terms(i, exclude)]

        def within(item, key, lo=None, hi=None):
            v = item["nutrients"].get(key)
            return v is not None and (lo is None or v >= lo) and (hi is None or v <= hi)

        if min_protein_g is not None:
            items = [i for i in items if within(i, "protein_g", lo=min_protein_g)]
        if max_calories is not None:
            items = [i for i in items if within(i, "calories", hi=max_calories)]
        if max_carbs_g is not None:
            items = [i for i in items if within(i, "carbs_g", hi=max_carbs_g)]
        if max_fat_g is not None:
            items = [i for i in items if within(i, "fat_g", hi=max_fat_g)]

        if sort_by:
            known = {k for m in day["meals"] for i in m["items"] for k in i["nutrients"]} | {"protein_per_100kcal"}
            if sort_by not in known:
                raise ToolError(f"Unknown sort_by {sort_by!r}. Options: {', '.join(sorted(known))}")
            key = protein_density if sort_by == "protein_per_100kcal" else (lambda i: i["nutrients"].get(sort_by))
            present = [i for i in items if key(i) is not None]
            items = sorted(present, key=key, reverse=descending) + [i for i in items if key(i) is None]

        results = []
        for item in items[: max(1, limit)]:
            row = {"meal": item["meal"], "station": item["station"], **compact(item)}
            if sort_by == "protein_per_100kcal":
                row["protein_per_100kcal"] = protein_density(item)
            results.append(row)
        return dumps({"date": iso, "matches": len(items), "returned": len(results), "items": results})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_item_nutrition(item_name: str, meal: str | None = None, date: str | None = None) -> str:
        """Full nutrition label for a menu item: portion, every nutrient, ingredients, diet tags and allergens.

        Args:
            item_name: the item's name. Exact matches win; otherwise any item whose name contains this text.
            meal: optionally restrict to 'breakfast', 'lunch' or 'dinner'.
            date: YYYY-MM-DD, 'today', 'tomorrow' or a weekday name. Defaults to today.
        """
        iso, day = await load(date)
        if msg := unavailable(iso, day):
            return msg
        matches = find_item(items_for(day, meal), item_name)
        if not matches:
            return dumps({"date": iso, "found": False, "message": f"No item matching {item_name!r}. Try search_menu."})
        # The same dish often appears in several meals; show each distinct label once.
        seen, unique = set(), []
        for m in matches:
            key = (m["name"], m["portion"], json.dumps(m["nutrients"], sort_keys=True))
            if key not in seen:
                seen.add(key)
                unique.append({"meal": m["meal"], "station": m["station"], **full_view(m)})
        return dumps({"date": iso, "found": True, "items": unique[:10]})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def calculate_meal_totals(items: list[dict[str, Any] | str], meal: str | None = None,
                                    date: str | None = None) -> str:
        """Add up calories, macros and every other published nutrient for a plate of menu items.

        Args:
            items: list of {"name": str, "servings": number (default 1)}; plain names also work.
                Names match like get_item_nutrition.
            meal: optionally restrict lookups to 'breakfast', 'lunch' or 'dinner'.
            date: YYYY-MM-DD, 'today', 'tomorrow' or a weekday name. Defaults to today.
        """
        iso, day = await load(date)
        if msg := unavailable(iso, day):
            return msg
        pool = items_for(day, meal)
        plate, not_found = [], []
        for entry in items:
            entry = {"name": entry} if isinstance(entry, str) else dict(entry)
            name = str(entry.get("name", ""))
            try:
                servings = float(entry.get("servings", 1))
            except (TypeError, ValueError):
                raise ToolError(f"servings for {name!r} must be a number") from None
            if servings <= 0:
                raise ToolError(f"servings for {name!r} must be greater than 0")
            hits = find_item(pool, name)
            if hits:
                plate.append((hits[0], servings))
            else:
                not_found.append(name)

        keys = list(MACRO_KEYS) + sorted({k for item, _ in plate for k in item["nutrients"]} - set(MACRO_KEYS))
        totals, incomplete = {}, []
        for k in keys:
            values = [(item["nutrients"].get(k), s) for item, s in plate]
            if any(v is None for v, _ in values):
                incomplete.append(k)
            totals[k] = round(sum(v * s for v, s in values if v is not None), 1)

        result: dict[str, Any] = {
            "date": iso,
            "items": [
                {"name": item["name"], "meal": item["meal"], "station": item["station"], "portion": item["portion"],
                 "servings": s, **{k: round(item["nutrients"][k] * s, 1) for k in MACRO_KEYS
                                   if item["nutrients"].get(k) is not None}}
                for item, s in plate
            ],
            "totals": totals,
        }
        if totals.get("calories"):
            result["macro_split_pct"] = {
                "protein": round(totals["protein_g"] * 4 / totals["calories"] * 100),
                "carbs": round(totals["carbs_g"] * 4 / totals["calories"] * 100),
                "fat": round(totals["fat_g"] * 9 / totals["calories"] * 100),
            }
        if incomplete:
            result["incomplete"] = incomplete
            result["incomplete_note"] = "Some items have no value for these nutrients, so their totals are understated."
        if not_found:
            result["not_found"] = not_found
        return dumps(result)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def list_dietary_tags(meal: str | None = None, date: str | None = None) -> str:
        """The diet and allergen tags used on a day's menu (e.g. Vegan, Vegetarian, Milk, Pork) with item counts.

        Args:
            meal: optionally restrict to 'breakfast', 'lunch' or 'dinner'.
            date: YYYY-MM-DD, 'today', 'tomorrow' or a weekday name. Defaults to today.
        """
        iso, day = await load(date)
        if msg := unavailable(iso, day):
            return msg
        counts: dict[str, int] = {}
        for item in items_for(day, meal):
            for tag in set(item["tags"]) | set(item["allergens"]):
                counts[tag] = counts.get(tag, 0) + 1
        return dumps({"date": iso, "tags": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))})

    return mcp
