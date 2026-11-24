"""Command line: `serve` (default), `find`, `fetch` and `sync`."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from . import __version__
from .api import DineOnCampus, UpstreamBlocked, UpstreamError
from .menu import fetch_day, location_hours, resolve_date

DEFAULT_TZ = "America/New_York"
DEFAULT_DATA_DIR = Path.home() / ".dineoncampus-mcp" / "snapshots"
FIND_HINT = "find it with: dineoncampus-mcp find \"<school name>\""


def env(name: str, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def split_list(values: list[str]) -> list[str]:
    """Flatten repeatable, comma-separated option values."""
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def load_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"error: unknown timezone {name!r} (use an IANA name such as America/New_York)") from None


# ---------------------------------------------------------------- serve

def cmd_serve(args) -> int:
    from mcp.server.transport_security import TransportSecuritySettings

    from .server import build_server
    from .local import SelfHostedSource
    from .sources import DirectSource, SnapshotSource

    tz = load_tz(args.tz)
    token = env("DINING_INGEST_TOKEN")
    local_source = None
    if args.source == "direct":
        if not args.location_id:
            return fail(f"set DINING_LOCATION_ID or --location-id ({FIND_HINT})")
        source = DirectSource(DineOnCampus(), args.location_id, tz, site_id=args.site_id)
    elif args.source == "local":
        if not args.location_id:
            return fail(f"set DINING_LOCATION_ID or --location-id ({FIND_HINT})")
        refresh = int(args.refresh_seconds or env("DINING_REFRESH_SECONDS") or 21600)
        local_source = SelfHostedSource(
            DineOnCampus(), args.data_dir, tz, location_id=args.location_id,
            site_id=args.site_id, days=int(args.sync_days or env("DINING_SYNC_DAYS") or 7),
            refresh_seconds=refresh,
            log=lambda m: print(f"{datetime.now().isoformat(timespec='seconds')} {m}", file=sys.stderr),
        )
        local_source.start()
        source = local_source
    else:
        if args.transport == "http" and not token:
            return fail("snapshot mode needs DINING_INGEST_TOKEN so `sync` can push menus to /ingest")
        source = SnapshotSource(args.data_dir, tz, location_id=args.location_id)

    mcp = build_server(source, tz=tz, ingest_token=token)
    if args.transport == "stdio":
        mcp.run("stdio")
        return 0

    hosts = split_list(args.allowed_host)
    origins = split_list(args.allowed_origin)
    if "*" in hosts:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        allowed = ["127.0.0.1:*", "localhost:*", "[::1]:*"] + [x for h in hosts for x in (h, f"{h}:*")]
        allowed_origins = [f"{scheme}://{h}" for h in hosts for scheme in ("https", "http")] + origins
        security = TransportSecuritySettings(allowed_hosts=allowed, allowed_origins=allowed_origins)
    mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True, json_response=True,
            transport_security=security)
    return 0


# ---------------------------------------------------------------- find

def cmd_find(args) -> int:
    api = DineOnCampus()
    q = args.query.strip().lower()
    sites = [s for s in api.sites() if q in (s.get("name") or "").lower() or q == (s.get("slug") or "").lower()]
    if not sites:
        print(f"No school on DineOnCampus matches {args.query!r}.")
        return 1
    if len(sites) > 5:
        print(f"{len(sites)} schools match {args.query!r}; be more specific:")
        for s in sites:
            print(f"  {s['name']}  (slug {s.get('slug')})")
        return 0
    for s in sites:
        print(f"\n{s['name']}\n  DINING_SITE_ID={s['id']}")
        for loc in api.site_locations(s["id"]):
            building = f"  {loc['buildingName']}" if loc.get("buildingName") else ""
            print(f"  {loc.get('name', '?'):<34} DINING_LOCATION_ID={loc['id']}  [{loc.get('type') or '?'}]{building}")
    print("\nCheck what a location publishes with: dineoncampus-mcp fetch --location-id <id> --summary")
    return 0


# ---------------------------------------------------------------- fetch

def cmd_fetch(args) -> int:
    if not args.location_id:
        return fail(f"set DINING_LOCATION_ID or --location-id ({FIND_HINT})")
    tz = load_tz(args.tz)
    day = resolve_date(args.date, tz)
    api = DineOnCampus()
    if args.raw:
        periods = api.periods(args.location_id, day)
        menus = {p["id"]: api.menu(args.location_id, day, p["id"]) for p in periods.get("periods") or []}
        print(json.dumps({"periods": periods, "menus": menus}, indent=2, ensure_ascii=False))
        return 0
    rules = name = None
    if args.site_id:
        rules, name = location_hours(api, args.site_id, args.location_id)
    data = fetch_day(api, args.location_id, day, tz=tz, hours_rules=rules, location_name=name)
    if args.summary:
        print(f"{data['date']}  {data['location_name'] or data['location_id']}  hours: {data['hours']}")
        for meal in data["meals"]:
            stations = list(dict.fromkeys(i["station"] for i in meal["items"]))
            print(f"  {meal['name']}: {len(meal['items'])} items across {len(stations)} stations")
        if not data["meals"]:
            print("  no meals published")
        return 0
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- sync

def push(server: str, token: str, day: dict) -> dict:
    body = gzip.compress(json.dumps(day, separators=(",", ":"), ensure_ascii=False).encode())
    resp = requests.post(
        f"{server.rstrip('/')}/ingest",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Content-Encoding": "gzip"},
        timeout=60,
    )
    if resp.status_code == 401:
        raise RuntimeError("the server rejected the ingest token")
    if resp.status_code >= 400:
        raise RuntimeError(f"the server answered HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def cmd_sync(args) -> int:
    config = {}
    if args.config:
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))

    def pick(flag, key, env_name, default=None):
        for value in (flag, config.get(key), env(env_name)):
            if value not in (None, ""):
                return value
        return default

    server = pick(args.server, "server", "DINING_SYNC_SERVER")
    token = pick(None, "token", "DINING_INGEST_TOKEN")
    location_id = pick(args.location_id, "location_id", "DINING_LOCATION_ID")
    site_id = pick(args.site_id, "site_id", "DINING_SITE_ID")
    tz = load_tz(pick(args.tz, "tz", "DINING_TZ", DEFAULT_TZ))
    days = int(pick(args.days, "days", "DINING_SYNC_DAYS", 7))
    if not server:
        return fail("set the server URL with --server, the config file or DINING_SYNC_SERVER")
    if not token:
        return fail("set the ingest token in the config file or DINING_INGEST_TOKEN")
    if not location_id:
        return fail(f"set DINING_LOCATION_ID, --location-id or location_id in the config ({FIND_HINT})")

    log_path = Path(args.log) if args.log else None

    def log(message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        print(line)
        if log_path:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    api = DineOnCampus()
    rules = name = None
    if site_id:
        try:
            rules, name = location_hours(api, site_id, location_id)
        except UpstreamError as e:
            log(f"hours unavailable: {e}")

    failures = 0
    today = datetime.now(tz).date()
    for offset in range(days + 1):
        day = (today + timedelta(days=offset)).isoformat()
        try:
            data = fetch_day(api, location_id, day, tz=tz, hours_rules=rules, location_name=name, delay=1.0)
            closed = data["closed_on_date"] or (data["hours"] or {}).get("open") is False
            if not data["meals"] and not closed:
                # Upstream sometimes briefly returns no periods for a date it does have;
                # skipping keeps the server's previous copy instead of overwriting it with nothing.
                log(f"{day}: no meals published upstream, skipped")
                continue
            push(server, token, data)
            log(f"{day}: pushed {', '.join(m['name'] for m in data['meals']) or 'closed day'}")
        except UpstreamBlocked as e:
            log(f"{day}: FAILED {e}")
            return 1
        except Exception as e:  # keep going so one bad day doesn't block the rest
            failures += 1
            log(f"{day}: FAILED {e}")
    return 1 if failures else 0


def cmd_sync_local(args) -> int:
    """Fetch upcoming days into the local snapshot store on this machine; no server needed."""
    location_id = args.location_id or env("DINING_LOCATION_ID")
    site_id = args.site_id or env("DINING_SITE_ID")
    if not location_id:
        return fail(f"set DINING_LOCATION_ID or --location-id ({FIND_HINT})")
    tz_name = str(args.tz or env("DINING_TZ") or DEFAULT_TZ)
    tz = load_tz(tz_name)
    days = int(args.days or env("DINING_SYNC_DAYS") or 7)
    data_dir = str(args.data_dir or env("DINING_DATA_DIR") or DEFAULT_DATA_DIR)
    log_path = Path(args.log) if args.log else None

    def log(message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        print(line)
        if log_path:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    from .local import SelfHostedSource
    source = SelfHostedSource(
        DineOnCampus(), data_dir, tz, location_id=location_id, site_id=site_id,
        days=days, refresh_seconds=0, log=log,
    )
    result = source.refresh_once()
    saved = result.get("saved", [])
    log(f"refreshed {len(saved)} day(s); available: {', '.join(result.get('available', [])) or 'none'}")
    return 1 if result.get("failed") and not saved else 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dineoncampus-mcp",
        description="MCP server for DineOnCampus dining hall menus, hours and nutrition facts.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def location_args(p):
        p.add_argument("--location-id", default=env("DINING_LOCATION_ID"), help="DineOnCampus location id [DINING_LOCATION_ID]")
        p.add_argument("--site-id", default=env("DINING_SITE_ID"), help="school site id, enables opening hours [DINING_SITE_ID]")
        p.add_argument("--tz", default=env("DINING_TZ", DEFAULT_TZ),
                       help=f"dining hall timezone [DINING_TZ, default {DEFAULT_TZ}]")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    location_args(serve)
    serve.add_argument("--source", choices=["direct", "local", "snapshots"], default=env("DINING_SOURCE", "direct"),
                       help="direct: fetch from DineOnCampus; local: self-hosted fetch+snapshot on one machine; snapshots: serve menus pushed by `sync` [DINING_SOURCE]")
    serve.add_argument("--sync-days", type=int, default=None,
                       help="local mode: days after today to cache [DINING_SYNC_DAYS, default 7]")
    serve.add_argument("--refresh-seconds", type=int, default=None,
                       help="local mode: seconds between background refreshes [DINING_REFRESH_SECONDS, default 21600]")
    serve.add_argument("--transport", choices=["stdio", "http"], default=env("DINING_TRANSPORT", "stdio"),
                       help="stdio for local clients, http for a remote server [DINING_TRANSPORT]")
    serve.add_argument("--host", default=env("DINING_HOST", "127.0.0.1"), help="HTTP bind address [DINING_HOST]")
    serve.add_argument("--port", type=int, default=int(env("DINING_PORT", 8000)), help="HTTP port [DINING_PORT]")
    serve.add_argument("--allowed-host", action="append", default=[env("DINING_ALLOWED_HOSTS", "")],
                       help="public hostname clients connect with; repeatable, '*' disables Host checks [DINING_ALLOWED_HOSTS]")
    serve.add_argument("--allowed-origin", action="append", default=[env("DINING_ALLOWED_ORIGINS", "")],
                       help="extra browser Origin to accept; repeatable [DINING_ALLOWED_ORIGINS]")
    serve.add_argument("--data-dir", default=env("DINING_DATA_DIR", str(DEFAULT_DATA_DIR)),
                       help="snapshot directory [DINING_DATA_DIR]")
    serve.set_defaults(func=cmd_serve)

    find = sub.add_parser("find", help="look up a school's site id and dining location ids")
    find.add_argument("query", help="part of the school's name, e.g. \"New Jersey Institute\"")
    find.set_defaults(func=cmd_find)

    fetch = sub.add_parser("fetch", help="print one day's menu as JSON")
    location_args(fetch)
    fetch.add_argument("--date", default=None, help="YYYY-MM-DD, today, tomorrow or a weekday (default today)")
    fetch.add_argument("--summary", action="store_true", help="print meal and item counts instead of JSON")
    fetch.add_argument("--raw", action="store_true", help="print the raw API responses")
    fetch.set_defaults(func=cmd_fetch)

    sync = sub.add_parser("sync", help="fetch upcoming days and push them to a snapshot-mode server")
    sync.add_argument("--server", help="server base URL, e.g. https://1-2-3-4.sslip.io [DINING_SYNC_SERVER]")
    sync.add_argument("--location-id", help="[DINING_LOCATION_ID]")
    sync.add_argument("--site-id", help="[DINING_SITE_ID]")
    sync.add_argument("--tz", help=f"[DINING_TZ, default {DEFAULT_TZ}]")
    sync.add_argument("--days", type=int, help="days after today to sync [DINING_SYNC_DAYS, default 7]")
    sync.add_argument("--config", help="JSON file with server, token, location_id, site_id, tz, days")
    sync.add_argument("--log", help="also append log lines to this file")
    sync.set_defaults(func=cmd_sync)

    sync_local = sub.add_parser("sync-local", help="fetch upcoming days into this machine's snapshot store (no second PC)")
    sync_local.add_argument("--location-id", help="[DINING_LOCATION_ID]")
    sync_local.add_argument("--site-id", help="[DINING_SITE_ID]")
    sync_local.add_argument("--tz", help=f"[DINING_TZ, default {DEFAULT_TZ}]")
    sync_local.add_argument("--days", type=int, help="days after today to cache [DINING_SYNC_DAYS, default 7]")
    sync_local.add_argument("--data-dir", help="local snapshot directory [DINING_DATA_DIR]")
    sync_local.add_argument("--log", help="also append log lines to this file")
    sync_local.set_defaults(func=cmd_sync_local)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "serve")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UpstreamError as e:
        return fail(str(e))
