# dineoncampus-mcp

[![CI](https://github.com/Pixel2075/dineoncampus-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Pixel2075/dineoncampus-mcp/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server that gives Claude, or any other MCP client, your
campus dining hall's menus from [DineOnCampus](https://dineoncampus.com). Each item comes with its
portion size, calories, macros, micronutrients, ingredients and diet/allergen tags. The server also
returns the hall's opening hours.

It's built for asking an AI to plan your meals around what the dining hall is actually serving
today, without copying menus into the chat by hand:

> *"Plan lunch and dinner from today's menu: about 160 g of protein, nothing with pork, and keep sodium reasonable."*
>
> *"What's the highest-protein vegan option at dinner tomorrow?"*
>
> *"Add up 2 scrambled eggs, a cup of chicken tikka masala and basmati rice."*
>
> *"Is the dining hall open on Thursday?"*

Any school on DineOnCampus works. This is an unofficial project and is not affiliated with
DineOnCampus or any university.

## Tools

| Tool | What it returns |
|---|---|
| `get_meal_periods` | The meals served on a date (e.g. Breakfast, Lunch, Dinner), each with its item count and stations, plus the opening hours |
| `get_menu` | A meal's full menu grouped by station: portion, description, ingredients, tags and every published nutrient. `detail="summary"` returns calories and core macros only |
| `search_menu` | Items filtered by text, diet tags, excluded ingredients, minimum protein, or maximum calories, carbs or fat, optionally sorted by any nutrient |
| `get_item_nutrition` | One item's full nutrition label |
| `calculate_meal_totals` | Totals for a plate (with servings): every nutrient plus the protein/carb/fat calorie split |
| `list_dietary_tags` | The diet and allergen tags used on that day's menu, with counts |

Dates can be `YYYY-MM-DD`, `today`, `tomorrow` or a weekday name. They are resolved in the dining
hall's timezone. Nutrient keys include their unit (`protein_g`, `sodium_mg`, `vitamin_d_iu`), and all
values are per listed portion.

## Quick start: run it on your own computer

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs a suitable
Python (3.11 or newer) for you.

**1. Find your dining hall.**

```console
$ uvx --from git+https://github.com/Pixel2075/dineoncampus-mcp dineoncampus-mcp find "new jersey institute"

New Jersey Institute of Technology
  DINING_SITE_ID=5c86b28a8e45bf0ccb9ec92b
  Highlander Commons                 DINING_LOCATION_ID=615f4f93a9f13a32678e5feb  [webtrition]  Campus Center
  Faculty Dining                     DINING_LOCATION_ID=6399e7a5c625af06bc115d04  [webtrition]  Campus Center
  ...
```

Check that a location publishes daily menus:

```console
$ uvx --from git+https://github.com/Pixel2075/dineoncampus-mcp dineoncampus-mcp fetch \
    --location-id 615f4f93a9f13a32678e5feb --site-id 5c86b28a8e45bf0ccb9ec92b --summary
2026-11-24  Highlander Commons  hours: {'open': True, 'hours': ['7:00am-10:00pm']}
  Breakfast: 69 items across 13 stations
  Lunch: 188 items across 21 stations
  Dinner: 184 items across 21 stations
```

**2. Add it to your client.** The `DINING_SITE_ID` is optional and turns on opening hours. Set
`DINING_TZ` if your campus isn't in US Eastern time.

*Claude Code:*

```bash
claude mcp add dining \
  -e DINING_LOCATION_ID=615f4f93a9f13a32678e5feb \
  -e DINING_SITE_ID=5c86b28a8e45bf0ccb9ec92b \
  -e DINING_TZ=America/New_York \
  -- uvx --from git+https://github.com/Pixel2075/dineoncampus-mcp dineoncampus-mcp
```

*Claude Desktop:* add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), then restart the app:

```json
{
  "mcpServers": {
    "dining": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Pixel2075/dineoncampus-mcp", "dineoncampus-mcp"],
      "env": {
        "DINING_LOCATION_ID": "615f4f93a9f13a32678e5feb",
        "DINING_SITE_ID": "5c86b28a8e45bf0ccb9ec92b",
        "DINING_TZ": "America/New_York"
      }
    }
  }
}
```

If Claude Desktop can't find `uvx`, replace it with the full path (`which uvx` on macOS/Linux,
`where uvx` on Windows).

In this mode the server fetches menus directly from DineOnCampus whenever a tool is called and caches
them for 30 minutes. That works from a home or campus connection. It does not work from a cloud
server; see below.

## Hosting it for claude.ai and your phone

A local server only exists while your computer runs the client. To use the menus from claude.ai or
the mobile apps, you need a public HTTPS server. That raises one problem:

**DineOnCampus blocks cloud IPs.** Cloudflare in front of its API returns `403 Forbidden` to
requests from AWS and other datacenters, even when the same request works from home. Don't try to
get around the block. This project avoids it instead: **your home machine fetches the menus and
pushes them to the server**, and the server only serves what it was given.

```
 home PC ── dineoncampus-mcp sync (4x a day) ──► POST /ingest ┐
                                                              │  server (snapshot mode)
 Claude ─────────────── MCP over HTTPS ──────────► /mcp ──────┘
```

Each sync fetches today plus the next 7 days (about 3 requests per day, spaced 1 second apart) and
pushes one snapshot per date. If your PC is off for a while, the server keeps serving the days it
already has. When asked for a date it has no snapshot for, it says so and lists the dates it does
have.

### 1. Server (Ubuntu 24.04+, e.g. an EC2 instance)

Open inbound TCP **80** and **443** (on AWS: the instance's security group), then run:

```bash
curl -fsSL https://raw.githubusercontent.com/Pixel2075/dineoncampus-mcp/main/deploy/install-server.sh \
  | sudo bash -s -- --location-id 615f4f93a9f13a32678e5feb --tz America/New_York
```

The script installs the server as a systemd service and puts [Caddy](https://caddyserver.com) in
front of it with a Let's Encrypt certificate. If you don't pass `--domain`, it uses
`<public-ip>.sslip.io`, a free hostname that resolves to your IP with no DNS setup. At the end it
prints:

```
  MCP URL (add this to Claude):  https://3-145-27-189.sslip.io/mcp
  Ingest token:                  6f1c...
```

Re-run the script any time to upgrade; it keeps the same token. On AWS, attach an **Elastic IP**
before you share the URL. Otherwise a stop and start gives the instance a new IP and the
`sslip.io` URL changes.

### 2. Home machine: schedule the sync

*Windows* (from a clone of this repo, in PowerShell):

```powershell
.\deploy\windows-sync-task.ps1 -Server https://3-145-27-189.sslip.io -Token <ingest token> `
    -LocationId 615f4f93a9f13a32678e5feb -SiteId 5c86b28a8e45bf0ccb9ec92b -Tz America/New_York
```

This installs the tool with uv, saves the settings to `%APPDATA%\dineoncampus-mcp\sync.json` (only
your account can read it), and registers a Task Scheduler task called "DineOnCampus MCP Sync". The
task runs at 6:00, 11:00, 16:00 and 21:00, and catches up after the PC was off. Its log is
`%APPDATA%\dineoncampus-mcp\sync.log`.

*macOS / Linux:*

```bash
uv tool install git+https://github.com/Pixel2075/dineoncampus-mcp
mkdir -p ~/.config/dineoncampus-mcp && chmod 700 ~/.config/dineoncampus-mcp
cat > ~/.config/dineoncampus-mcp/sync.json <<'EOF'
{"server": "https://3-145-27-189.sslip.io", "token": "<ingest token>",
 "location_id": "615f4f93a9f13a32678e5feb", "site_id": "5c86b28a8e45bf0ccb9ec92b",
 "tz": "America/New_York", "days": 7}
EOF
chmod 600 ~/.config/dineoncampus-mcp/sync.json
dineoncampus-mcp sync --config ~/.config/dineoncampus-mcp/sync.json    # try it once
( crontab -l 2>/dev/null; echo "0 6,11,16,21 * * * $HOME/.local/bin/dineoncampus-mcp sync --config $HOME/.config/dineoncampus-mcp/sync.json --log $HOME/.config/dineoncampus-mcp/sync.log" ) | crontab -
```

### 3. Add the connector

* **claude.ai / Claude Desktop / mobile:** Settings → Connectors → Add custom connector, then paste the MCP URL.
* **Claude Code:** `claude mcp add --transport http dining https://3-145-27-189.sslip.io/mcp`

`https://<host>/health` shows how many dates the server holds and the latest one.

## Configuration

Every option can also be set as a command-line flag (`dineoncampus-mcp serve --help`).

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `DINING_LOCATION_ID` | (required) | serve, fetch, sync | DineOnCampus location id, from `find` |
| `DINING_SITE_ID` | (none) | serve, fetch, sync | School site id, from `find`; enables opening hours |
| `DINING_TZ` | `America/New_York` | all | Dining hall timezone (IANA name) |
| `DINING_SOURCE` | `direct` | serve | `direct` fetches from DineOnCampus; `snapshots` serves what `sync` pushed |
| `DINING_TRANSPORT` | `stdio` | serve | `stdio` for local clients, `http` for a server |
| `DINING_HOST` / `DINING_PORT` | `127.0.0.1` / `8000` | serve | HTTP bind address |
| `DINING_ALLOWED_HOSTS` | (none) | serve | Public hostname(s), comma-separated. Requests with any other `Host` header are rejected (DNS-rebinding protection); `*` turns the check off |
| `DINING_ALLOWED_ORIGINS` | (none) | serve | Extra browser `Origin`s to accept |
| `DINING_DATA_DIR` | `~/.dineoncampus-mcp/snapshots` | serve | Where snapshot mode stores pushed days (kept for 30 days) |
| `DINING_INGEST_TOKEN` | (none) | serve, sync | Shared secret for `POST /ingest`; required for an HTTP snapshot server |
| `DINING_SYNC_SERVER` | (none) | sync | Base URL of the snapshot server |
| `DINING_SYNC_DAYS` | `7` | sync | Days after today to sync |

## Command line

```
dineoncampus-mcp [serve]   run the MCP server (the default command)
dineoncampus-mcp find      look up a school's site id and dining location ids
dineoncampus-mcp fetch     print one day's normalized menu as JSON (--summary, --raw)
dineoncampus-mcp sync      fetch upcoming days and push them to a snapshot server
```

## About the data

* Everything comes from the dining hall's own nutrition system, and some entries are incomplete or
  wrong. Sometimes a menu briefly shows another outlet's items, or values like 0 g of carbs on a
  sandwich. The server tells the model to point out values that look implausible.
* Tags such as `Vegan`, `Milk` or `Pork*` are passed through exactly as the school publishes them.
  The school decides what a tag (and a `*`) means.
* Opening hours are for the whole location, not for each meal. On holidays DineOnCampus sometimes
  still lists a menu. When the hours say the hall is closed, the tools add a `warning` saying so.
* One server instance covers one dining location. Run another instance for a second hall.
* Please keep request volumes modest. The defaults (a 30-minute cache in direct mode, and four syncs
  a day with 1 second between requests) are plenty for personal use.

## Development

```bash
git clone https://github.com/Pixel2075/dineoncampus-mcp && cd dineoncampus-mcp
uv sync
uv run pytest
uv run dineoncampus-mcp fetch --location-id <id> --summary
```

The tests run offline against recorded API responses in `tests/fixtures`.

## License

[MIT](LICENSE)
