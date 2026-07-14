#!/usr/bin/env python3
"""
Plane proximity notifier.

Polls the OpenSky Network API for aircraft near one or more locations and
posts a message to a Discord webhook when one comes within range.

Setup:
  1. pip install requests
  2. In Discord: Server Settings -> Integrations -> Webhooks -> New Webhook.
     Copy the webhook URL into the DISCORD_WEBHOOK_URL environment variable.
  3. Set LOCATION_1_LAT / LOCATION_1_LON (and LOCATION_2_LAT / LOCATION_2_LON,
     if watching a second spot) environment variables to your coordinates.
  4. Run: python3 plane_tracker.py
     (leave it running in a terminal, tmux session, or as a background
     service / cron-launched process)
"""

import os
import re
import time
import math
import requests

# ----------------------- CONFIG -----------------------

RADIUS_MI = 5.0         # default notify radius for locations that don't set their own
MAX_ALTITUDE_M = None   # e.g. 3000 to ignore high-altitude overflights; None = no limit
# How often to poll OpenSky. Each /states/all call costs "credits" (1 credit for a bounding
# box this small), and each location is a separate call, so N locations cost N credits per
# poll. Anonymous access gets only 400 credits/day -- at 2 locations that's exhausted in
# under 2 hours at 30s intervals, after which every poll 429s for the rest of the day.
# A free OpenSky account (see OPENSKY_CLIENT_ID/OPENSKY_CLIENT_SECRET below) raises that
# to 4000/day, comfortable at this default with up to ~2-3 locations.
POLL_SECONDS = 45
NOTIFY_COOLDOWN_MIN = 30  # don't re-notify about the same aircraft at the same location again within this many minutes
# Fallback wait after a 429 if OpenSky doesn't send a Retry-After header (it usually does).
OPENSKY_RATE_LIMIT_BACKOFF_SECONDS = 300


def location_from_env(prefix, default_name):
    """Build a location dict from LOCATION_<prefix>_{NAME,LAT,LON,RADIUS_MI} env vars.
    Real coordinates are kept out of this file on purpose -- set them in your shell
    environment (e.g. ~/.zshenv), not here, so they never end up committed to git."""
    return {
        "name": os.environ.get(f"{prefix}_NAME", default_name),
        "lat": float(os.environ.get(f"{prefix}_LAT", "0.0")),
        "lon": float(os.environ.get(f"{prefix}_LON", "0.0")),
        "radius_mi": float(os.environ.get(f"{prefix}_RADIUS_MI", str(RADIUS_MI))),
        "env_prefix": prefix,
    }


# Add more entries here (LOCATION_3, LOCATION_4, ...) to watch additional spots.
LOCATIONS = [
    location_from_env("LOCATION_1", "Home"),
    location_from_env("LOCATION_2", "Location 2"),
]

# Set via the DISCORD_WEBHOOK_URL environment variable rather than editing this file,
# so the real webhook URL never ends up committed to source control.
DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/XXXXXXXX/XXXXXXXX",  # <-- or paste your webhook URL here for local use
)

# Optional: a free OpenSky account's OAuth2 client credentials raise the daily rate
# limit from 400 to 4000 requests (create one at https://opensky-network.org/my-opensky/account).
# Leave both unset to use anonymous access.
OPENSKY_CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID")
OPENSKY_CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET")

# Optional: a FlightAware AeroAPI key (free "Starter" tier, no credit card required,
# 500 requests/month) gives real live route data instead of the static, sometimes-stale
# route tables the free fallback sources use. Sign up at https://www.flightaware.com/aeroapi/portal/
# Leave unset to skip this source and rely on adsbdb/hexdb only.
FLIGHTAWARE_API_KEY = os.environ.get("FLIGHTAWARE_API_KEY")

# --------------------------------------------------------

EARTH_RADIUS_KM = 6371.0
KM_PER_MILE = 1.60934

# Enrichment lookups (aircraft type, route) are cached since the same
# icao24/callsign is often seen across multiple polls.
AIRCRAFT_INFO_CACHE = {}
ROUTE_INFO_CACHE = {}


def haversine_km(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bounding_box(lat, lon, radius_km):
    # rough degree padding for the query box; final filtering uses haversine
    lat_pad = radius_km / 111.0
    lon_pad = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat - lat_pad, lat + lat_pad, lon - lon_pad, lon + lon_pad


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial compass bearing (0-360, 0=N) from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


COMPASS_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                   "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass_direction(bearing):
    return COMPASS_POINTS[round(bearing / 22.5) % 16]


def fetch_states(lat, lon, radius_mi):
    lamin, lamax, lomin, lomax = bounding_box(lat, lon, radius_mi * KM_PER_MILE)
    params = {"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax}
    if OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET:
        # OAuth2 client-credentials flow (newer OpenSky API accounts)
        token = get_opensky_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get("https://opensky-network.org/api/states/all", params=params, headers=headers, timeout=15)
    else:
        resp = requests.get("https://opensky-network.org/api/states/all", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("states") or []


def get_opensky_token():
    resp = requests.post(
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": OPENSKY_CLIENT_ID,
            "client_secret": OPENSKY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def send_discord_message(content):
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    resp.raise_for_status()


def lookup_aircraft_adsbdb(icao24):
    """Look up aircraft metadata for an icao24 via the free adsbdb API.
    Returns a dict with manufacturer/type/icao_type/registration keys, or None on any failure."""
    try:
        resp = requests.get(f"https://api.adsbdb.com/v0/aircraft/{icao24}", timeout=8)
        resp.raise_for_status()
        return (resp.json().get("response") or {}).get("aircraft") or None
    except (requests.RequestException, ValueError, AttributeError):
        return None


def lookup_aircraft_hexdb(icao24):
    """Look up aircraft metadata for an icao24 via the free hexdb.io API (fallback source
    for when adsbdb doesn't have this aircraft). Normalized to the same key names adsbdb
    uses, or None on any failure."""
    try:
        resp = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao24}", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        return {
            "manufacturer": data.get("Manufacturer"),
            "type": data.get("Type"),
            "icao_type": data.get("ICAOTypeCode"),
            "registration": data.get("Registration"),
            "operator": data.get("RegisteredOwners"),
        }
    except (requests.RequestException, ValueError, AttributeError):
        return None


def lookup_aircraft(icao24):
    """Look up aircraft metadata for an icao24, trying adsbdb first and falling back to
    hexdb.io if adsbdb has no record. Returns None if neither source has it."""
    if icao24 in AIRCRAFT_INFO_CACHE:
        return AIRCRAFT_INFO_CACHE[icao24]

    result = lookup_aircraft_adsbdb(icao24) or lookup_aircraft_hexdb(icao24)

    AIRCRAFT_INFO_CACHE[icao24] = result
    return result


# U.S.-registered aircraft are the exception to needing a lookup at all: the FAA assigns
# icao24 (Mode-S) addresses to N-numbers via a documented sequential algorithm (14 CFR
# Sec 47.15), so the tail number can be derived directly from the icao24 with no API call
# and no chance of being missing from a database. Only covers the US allocation block
# (A00001-ADF7C7); every other country's icao24 blocks use non-algorithmic assignment, so
# this simply doesn't apply there.
N_NUMBER_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # 24 letters, skipping I and O
N_NUMBER_SUFFIX_COUNTS = {1: 601, 2: 601, 3: 601, 4: 25, 5: 1}
N_NUMBER_SUBTREE_SIZES = {5: 1}
for _root_len in range(4, 0, -1):
    N_NUMBER_SUBTREE_SIZES[_root_len] = N_NUMBER_SUFFIX_COUNTS[_root_len] + 10 * N_NUMBER_SUBTREE_SIZES[_root_len + 1]
del _root_len


def _n_number_suffix_from_offset(root_len, offset):
    if offset == 0:
        return ""
    offset -= 1
    if root_len == 4:
        return N_NUMBER_LETTERS[offset]
    first, rest = divmod(offset, 25)
    if rest == 0:
        return N_NUMBER_LETTERS[first]
    return N_NUMBER_LETTERS[first] + N_NUMBER_LETTERS[rest - 1]


def icao24_to_n_number(icao24):
    """Derive a US N-number from an icao24 hex address. Returns None if icao24 falls
    outside the US allocation block (A00001-ADF7C7) or isn't valid hex."""
    try:
        value = int(icao24, 16)
    except (TypeError, ValueError):
        return None
    if not (0xA00001 <= value <= 0xADF7C7):
        return None

    offset = value - 0xA00000 - 1
    first_digit, offset = divmod(offset, N_NUMBER_SUBTREE_SIZES[1])
    digits = str(first_digit + 1)
    prefix_len = 1
    while True:
        if offset < N_NUMBER_SUFFIX_COUNTS[prefix_len]:
            return "N" + digits + _n_number_suffix_from_offset(prefix_len, offset)
        offset -= N_NUMBER_SUFFIX_COUNTS[prefix_len]
        child_size = N_NUMBER_SUBTREE_SIZES[prefix_len + 1]
        digit, offset = divmod(offset, child_size)
        digits += str(digit)
        prefix_len += 1


def format_aircraft_type(aircraft):
    """Format manufacturer + type, e.g. 'Boeing 737NG 8AS/W'. Returns None if unavailable."""
    if not aircraft:
        return None
    manufacturer = aircraft.get("manufacturer")
    ac_type = aircraft.get("type") or aircraft.get("icao_type")
    if manufacturer and ac_type:
        if ac_type.lower().startswith(manufacturer.lower()):
            return ac_type
        return f"{manufacturer} {ac_type}"
    return ac_type or manufacturer


def format_operator(aircraft):
    """Registered owner/operator, e.g. 'Ryanair'. adsbdb and hexdb use different key
    names for this field, so check both. Returns None if unavailable."""
    if not aircraft:
        return None
    return aircraft.get("registered_owner") or aircraft.get("operator")


def lookup_flightroute_adsbdb(callsign):
    """Look up flightroute data (origin/destination/airline) for a callsign via adsbdb.
    Returns the raw 'flightroute' dict, or None on any failure."""
    try:
        resp = requests.get(f"https://api.adsbdb.com/v0/callsign/{callsign}", timeout=8)
        resp.raise_for_status()
        return (resp.json().get("response") or {}).get("flightroute") or None
    except (requests.RequestException, ValueError, AttributeError):
        return None


def lookup_flightroute_hexdb(callsign):
    """Look up origin/destination for a callsign via the free hexdb.io API (fallback source
    for when adsbdb doesn't recognize this callsign). hexdb only gives ICAO airport codes,
    not airline/city names, so the result here is shaped like adsbdb's but sparser -- no
    'airline' key, so spell_out_callsign() will fall back to the raw callsign. None on failure."""
    try:
        resp = requests.get(f"https://hexdb.io/api/v1/route/icao/{callsign}", timeout=8)
        resp.raise_for_status()
        route = resp.json().get("route")  # e.g. "EGLL-KIAD"
        if not route or "-" not in route:
            return None
        origin_code, destination_code = route.split("-", 1)
        return {
            "origin": {"icao_code": origin_code},
            "destination": {"icao_code": destination_code},
        }
    except (requests.RequestException, ValueError, AttributeError):
        return None


def lookup_flightroute_flightaware(callsign):
    """Look up the real, live route for a callsign via FlightAware AeroAPI (requires
    FLIGHTAWARE_API_KEY). Unlike adsbdb/hexdb, this reflects today's actual filed flight
    plan rather than a static "this flight number usually goes here" table. Returns a dict
    with 'origin'/'destination' keys, or None if no key is set, on any failure, or if no
    matching flight is found."""
    if not FLIGHTAWARE_API_KEY:
        return None
    try:
        resp = requests.get(
            f"https://aeroapi.flightaware.com/aeroapi/flights/{callsign}",
            headers={"x-apikey": FLIGHTAWARE_API_KEY},
            timeout=8,
        )
        resp.raise_for_status()
        flights = resp.json().get("flights") or []
        if not flights:
            return None

        # Prefer a flight that's currently airborne (wheels-off recorded, wheels-on not
        # yet) over past/future flights sharing the same callsign; fall back to the most
        # recent entry if none match, since AeroAPI orders results by recency.
        flight = next(
            (f for f in flights if f.get("actual_off") and not f.get("actual_on")),
            flights[0],
        )

        def to_airport(ap):
            ap = ap or {}
            return {
                "municipality": ap.get("city"),
                "name": ap.get("name"),
                "iata_code": ap.get("code_iata"),
                "icao_code": ap.get("code_icao"),
                "latitude": ap.get("latitude"),
                "longitude": ap.get("longitude"),
            }

        origin = to_airport(flight.get("origin"))
        destination = to_airport(flight.get("destination"))
        if not (origin.get("icao_code") or destination.get("icao_code")):
            return None
        return {"origin": origin, "destination": destination}
    except (requests.RequestException, ValueError, AttributeError, KeyError):
        return None


def lookup_flightroute(callsign):
    """Look up flightroute data for a callsign. Tries FlightAware first (real live route,
    if FLIGHTAWARE_API_KEY is set), then adsbdb, then hexdb.io as last-resort fallbacks --
    both of the latter are static "usual route for this flight number" tables that can be
    stale or wrong. The airline name used to spell out the callsign only comes from adsbdb,
    so that's looked up regardless of which source supplies the route itself."""
    callsign = callsign.strip()
    if not callsign:
        return None
    if callsign in ROUTE_INFO_CACHE:
        return ROUTE_INFO_CACHE[callsign]

    live_route = lookup_flightroute_flightaware(callsign)
    adsbdb_route = lookup_flightroute_adsbdb(callsign)

    origin = None
    destination = None
    for candidate in (live_route, adsbdb_route):
        if candidate and (candidate.get("origin") or candidate.get("destination")):
            origin = candidate.get("origin")
            destination = candidate.get("destination")
            break
    else:
        hexdb_route = lookup_flightroute_hexdb(callsign)
        if hexdb_route:
            origin = hexdb_route.get("origin")
            destination = hexdb_route.get("destination")

    result = None
    if origin or destination or adsbdb_route:
        result = {
            "origin": origin,
            "destination": destination,
            "airline": (adsbdb_route or {}).get("airline"),
            "callsign_icao": (adsbdb_route or {}).get("callsign_icao"),
        }

    ROUTE_INFO_CACHE[callsign] = result
    return result


def describe_airport(airport):
    name = airport.get("municipality") or airport.get("name")
    code = airport.get("iata_code") or airport.get("icao_code")
    if name and code:
        return f"{name} ({code})"
    return name or code or "unknown"


def format_route(route):
    if not route:
        return None
    origin = route.get("origin") or {}
    destination = route.get("destination") or {}
    if not (origin or destination):
        return None
    return f"{describe_airport(origin)} → {describe_airport(destination)}"


def spell_out_callsign(route, raw_callsign):
    """Turn a raw callsign like 'UAL123' into its spoken ATC form, e.g. 'United 123'."""
    airline = (route or {}).get("airline") or {}
    airline_word = airline.get("callsign")
    if not airline_word:
        return raw_callsign

    icao_callsign = (route or {}).get("callsign_icao") or raw_callsign
    flight_number = re.sub(r"^[A-Za-z]+", "", icao_callsign)
    if not flight_number:
        return raw_callsign

    return f"{airline_word.title()} {flight_number}"


def format_alert(state, distance_mi, location):
    # OpenSky state vector field order:
    # icao24, callsign, origin_country, time_position, last_contact,
    # longitude, latitude, baro_altitude, on_ground, velocity,
    # true_track, vertical_rate, sensors, geo_altitude, squawk, spi, position_source
    icao24 = state[0]
    callsign = (state[1] or "").strip() or "unknown"
    country = state[2] or "unknown"
    plane_lon, plane_lat = state[5], state[6]
    altitude_m = state[7] or state[13]
    velocity_ms = state[9]

    direction = compass_direction(bearing_deg(location["lat"], location["lon"], plane_lat, plane_lon))

    alt_str = f"{altitude_m * 3.281:.0f} ft" if altitude_m else "unknown altitude"
    speed_str = f"{velocity_ms * 1.94384:.0f} kt" if velocity_ms else "unknown speed"

    aircraft = lookup_aircraft(icao24)
    aircraft_type = format_aircraft_type(aircraft) or "unknown"
    registration = (aircraft or {}).get("registration") or icao24_to_n_number(icao24) or "unknown"

    route = lookup_flightroute(callsign) if callsign != "unknown" else None
    route_str = format_route(route) or "not available"

    display_callsign = spell_out_callsign(route, callsign) if callsign != "unknown" else callsign

    lines = [
        f"✈️  **{display_callsign}**  —  {distance_mi:.1f} mi {direction} of {location['name']}",
        "",
        f"\U0001F6EB Route: {route_str}",
        f"\U0001F6E9️ Aircraft: {aircraft_type}",
        f"\U0001F3F7️ Tail: {registration}",
        f"\U0001F4C8 Altitude: {alt_str}",
        f"\U0001F4A8 Speed: {speed_str}",
        f"\U0001F30E Country: {country}",
        "",
        "​",  # zero-width space: Discord trims plain trailing blank lines, this survives to add spacing
    ]

    return "\n".join(line for line in lines if line is not None)


def main():
    for loc in LOCATIONS:
        if loc["lat"] == 0.0 and loc["lon"] == 0.0:
            raise SystemExit(f"Set {loc['env_prefix']}_LAT / {loc['env_prefix']}_LON before running.")
    if "XXXXXXXX" in DISCORD_WEBHOOK_URL:
        raise SystemExit("Set DISCORD_WEBHOOK_URL before running.")

    for loc in LOCATIONS:
        print(f"Watching for planes within {loc['radius_mi']} mi of {loc['name']} ({loc['lat']}, {loc['lon']})...")
    cooldown_sec = NOTIFY_COOLDOWN_MIN * 60
    last_notified = {}  # (icao24, location name) -> unix timestamp of last notification

    while True:
        wait_seconds = POLL_SECONDS
        try:
            now = time.time()

            for loc in LOCATIONS:
                states = fetch_states(loc["lat"], loc["lon"], loc["radius_mi"])

                for state in states:
                    lat, lon = state[6], state[5]
                    if lat is None or lon is None:
                        continue

                    altitude_m = state[7] or state[13]
                    if MAX_ALTITUDE_M is not None and altitude_m and altitude_m > MAX_ALTITUDE_M:
                        continue

                    distance_mi = haversine_km(loc["lat"], loc["lon"], lat, lon) / KM_PER_MILE
                    if distance_mi <= loc["radius_mi"]:
                        icao24 = state[0]
                        key = (icao24, loc["name"])
                        last_sent = last_notified.get(key)
                        if last_sent is None or (now - last_sent) >= cooldown_sec:
                            send_discord_message(format_alert(state, distance_mi, loc))
                            last_notified[key] = now
                            print(f"Notified: {icao24} at {distance_mi:.1f} mi from {loc['name']}")

            # drop stale entries so this doesn't grow unbounded over a long-running process
            last_notified = {k: v for k, v in last_notified.items() if now - v < cooldown_sec}

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                retry_after = e.response.headers.get("Retry-After")
                try:
                    wait_seconds = max(int(retry_after), POLL_SECONDS)
                except (TypeError, ValueError):
                    wait_seconds = OPENSKY_RATE_LIMIT_BACKOFF_SECONDS
                print(
                    f"OpenSky rate limit hit (daily credit quota likely exhausted) -- "
                    f"waiting {wait_seconds}s before retrying"
                )
            else:
                print(f"Request failed: {e}")
        except requests.RequestException as e:
            print(f"Request failed: {e}")

        time.sleep(wait_seconds)


if __name__ == "__main__":
    main()
