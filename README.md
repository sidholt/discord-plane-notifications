# Plane Tracker

A small script that watches for aircraft flying near one or more locations
and posts an alert to a Discord channel when one comes within range. It
polls the free [OpenSky Network](https://opensky-network.org/) API for live
positions, and enriches each alert with route, aircraft type, tail number,
and — when available — a real photo of that specific tail number, from
[FlightAware AeroAPI](https://www.flightaware.com/aeroapi/portal/) (if
configured), [adsbdb](https://www.adsbdb.com/), [hexdb.io](https://hexdb.io/),
and [planespotters.net](https://www.planespotters.net/).

Example alert:

```
✈️  United 123  —  12.3 mi NE of Home

🔢 Flight: UAL123
🛫 Route: Anchorage, AK (ANC) → Chicago, IL (ORD)
🛩️ Aircraft: Boeing 737NG 8AS/W
🏷️ Tail: EI-EGA
📈 Altitude: 11484 ft
💨 Speed: 243 kt
🌎 Country: United States
```

## Setup

1. Install dependencies:
   ```
   pip install requests
   ```
2. Create a Discord webhook: in your server, go to **Server Settings →
   Integrations → Webhooks → New Webhook**, and copy the webhook URL.
3. Set your webhook URL and coordinates as environment variables (don't
   paste them directly into `plane_tracker.py` — that risks committing your
   webhook and locations to git):
   ```
   export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
   export LOCATION_1_NAME="Home"
   export LOCATION_1_LAT="40.7580"    # example: Times Square, NYC -- use your own
   export LOCATION_1_LON="-73.9855"
   ```
   To watch a second location, add `LOCATION_2_NAME` / `LOCATION_2_LAT` /
   `LOCATION_2_LON` (and `LOCATION_3_...`, etc. — see Configuration below).
4. (Optional, recommended) For accurate, live route data instead of the
   static "usual route for this flight number" tables adsbdb/hexdb use, sign
   up for a free FlightAware AeroAPI key at
   https://www.flightaware.com/aeroapi/portal/ — the "Starter" tier needs no
   credit card and includes 500 requests/month, which the built-in
   per-aircraft cooldown keeps well within reach for personal use:
   ```
   export FLIGHTAWARE_API_KEY="..."
   ```
5. Run it:
   ```
   python3 plane_tracker.py
   ```
   Leave it running in a terminal, a `tmux`/`screen` session, or as a
   background service.

## Configuration

Most config lives at the top of `plane_tracker.py`. Locations are built from
environment variables rather than hardcoded, so real coordinates never end
up committed to git.

| Variable | Description |
|---|---|
| `LOCATION_<n>_NAME` | Display name for this location, used in Discord alerts (e.g. "Home") |
| `LOCATION_<n>_LAT`, `LOCATION_<n>_LON` | Coordinates to watch around |
| `LOCATION_<n>_RADIUS_MI` | Optional per-location radius override; falls back to `RADIUS_MI` |
| `RADIUS_MI` | Default notify radius, in miles, for locations that don't set their own |
| `MAX_ALTITUDE_M` | Optional altitude ceiling (in meters) to ignore high-altitude overflights |
| `POLL_SECONDS` | How often to poll OpenSky (anonymous rate limits are tight, and each location is a separate API call — raise this if watching multiple locations) |
| `NOTIFY_COOLDOWN_MIN` | Minimum time before the same aircraft can trigger another alert at the same location |
| `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` | Optional OpenSky OAuth2 client credentials, raising the daily rate limit from 400 to 4000 requests; leave unset for anonymous access |
| `FLIGHTAWARE_API_KEY` | Optional AeroAPI key for real, live route data; leave unset to use adsbdb/hexdb only |

By default, `LOCATIONS` is set up for two slots (`LOCATION_1`, `LOCATION_2`).
To watch more, add another `location_from_env("LOCATION_3", "...")` entry to
the `LOCATIONS` list in `plane_tracker.py`.

## How it works

Each poll, the script queries OpenSky once per location for aircraft in a
bounding box around its coordinates, then filters by exact great-circle
distance (haversine). For any plane newly within range of a location, it
looks up flight route and aircraft metadata and posts a formatted message —
tagged with that location's name — to your Discord webhook. A per-aircraft,
per-location cooldown prevents duplicate alerts.

Route lookups try sources in order of trustworthiness: FlightAware AeroAPI
first (if configured) for the real live route, then adsbdb, then hexdb.io.
The latter two are static "usual route for this flight number" tables
sourced from crowdsourced/historical data, not the actual flight plan for
today — airlines reuse flight numbers across different routes on different
days, so their stored route is sometimes completely wrong for the plane
actually overhead. To catch that, each stored route's destination is
compared against the plane's live heading; when they clearly disagree, the
script scrapes FlightAware's public flight page for the real live route and
shows that instead. This scrape parses data embedded in a webpage rather
than a documented API, so it's fragile and likely against FlightAware's
terms of service for automated access — it's kept rare by only firing on
routes that already look wrong, and cached per callsign. Aircraft type/tail
number lookups try adsbdb first, then hexdb.io.

Each route airport is labeled with a US state or Canadian province
abbreviation, or an ISO country code for everywhere else (e.g. "Chicago,
IL" / "Toronto, ON" vs "London, GB"), reverse-geocoded from its coordinates
via the free Nominatim/OpenStreetMap API and cached per airport for the life
of the process.

If a photo of that specific tail number exists on planespotters.net, it's
attached to the Discord message as an embed, crediting the photographer and
linking back to the photo page. Falls back to adsbdb's bundled photo link
(sourced from airport-data.com) if planespotters has nothing. No photo line
appears at all if neither source has one.

## Notes

- OpenSky's anonymous API is rate-limited to 400 credits/day (roughly 200
  polls/day for a single location); each additional location you watch
  costs another credit per poll. A free OpenSky account (see
  `OPENSKY_CLIENT_ID`/`OPENSKY_CLIENT_SECRET` above) raises that to
  4000/day — sign up at https://opensky-network.org/ and generate an API
  client at https://opensky-network.org/my-opensky/account. On a 429, the
  script backs off using OpenSky's `Retry-After` header rather than
  hammering the API further.
- Aircraft/route lookups depend on the underlying source having data for
  that aircraft/callsign; general aviation flights will often show up
  without a route even with all three sources configured.
- Tail numbers are the exception: if adsbdb/hexdb don't have one, and the
  icao24 falls in the US allocation block, `icao24_to_n_number()` derives
  the real N-number directly via the FAA's documented assignment algorithm
  — no lookup needed, so it's only ever "unknown" for non-US aircraft or a
  genuine data gap elsewhere.
