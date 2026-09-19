"""Offline geocoding for the radar map.

Deliberately local. Resolving a city name against a web geocoder would send
scraped business locations to a third party on every render, so this ships a
bundled table of common cities and caches anything resolved into SQLite. A city
that is not in the table is reported to the user rather than silently dropped -
adding it is one line in ``CITIES``.
"""

from __future__ import annotations

import re
from pathlib import Path

from core import vault

# lat, lon. Capitals, large metros, and the places a video or content agency
# is actually likely to sit. Add to this freely.
CITIES: dict[str, tuple[float, float]] = {
    # Italy
    "rome": (41.9028, 12.4964), "milan": (45.4642, 9.1900),
    "milano": (45.4642, 9.1900), "roma": (41.9028, 12.4964),
    "turin": (45.0703, 7.6869), "naples": (40.8518, 14.2681),
    "florence": (43.7696, 11.2558), "bologna": (44.4949, 11.3426),
    "venice": (45.4408, 12.3155), "palermo": (38.1157, 13.3615),
    "genoa": (44.4056, 8.9463), "bari": (41.1171, 16.8719),
    "catania": (37.5079, 15.0830), "verona": (45.4384, 10.9916),
    # United States
    "new york": (40.7128, -74.0060), "nyc": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437), "la": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298), "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740), "philadelphia": (39.9526, -75.1652),
    "san antonio": (29.4241, -98.4936), "san diego": (32.7157, -117.1611),
    "dallas": (32.7767, -96.7970), "austin": (30.2672, -97.7431),
    "san francisco": (37.7749, -122.4194), "seattle": (47.6062, -122.3321),
    "denver": (39.7392, -104.9903), "boston": (42.3601, -71.0589),
    "miami": (25.7617, -80.1918), "atlanta": (33.7490, -84.3880),
    "las vegas": (36.1699, -115.1398), "nashville": (36.1627, -86.7816),
    "portland": (45.5152, -122.6784), "detroit": (42.3314, -83.0458),
    "minneapolis": (44.9778, -93.2650), "washington": (38.9072, -77.0369),
    # Europe
    "london": (51.5074, -0.1278), "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050), "madrid": (40.4168, -3.7038),
    "barcelona": (41.3874, 2.1686), "amsterdam": (52.3676, 4.9041),
    "lisbon": (38.7223, -9.1393), "dublin": (53.3498, -6.2603),
    "brussels": (50.8503, 4.3517), "vienna": (48.2082, 16.3738),
    "zurich": (47.3769, 8.5417), "munich": (48.1351, 11.5820),
    "hamburg": (53.5511, 9.9937), "prague": (50.0755, 14.4378),
    "warsaw": (52.2297, 21.0122), "stockholm": (59.3293, 18.0686),
    "copenhagen": (55.6761, 12.5683), "oslo": (59.9139, 10.7522),
    "helsinki": (60.1699, 24.9384), "athens": (37.9838, 23.7275),
    "budapest": (47.4979, 19.0402), "bucharest": (44.4268, 26.1025),
    "zagreb": (45.8150, 15.9819), "manchester": (53.4808, -2.2426),
    # Rest of world
    "toronto": (43.6532, -79.3832), "vancouver": (49.2827, -123.1207),
    "montreal": (45.5017, -73.5673), "mexico city": (19.4326, -99.1332),
    "sao paulo": (-23.5505, -46.6333), "buenos aires": (-34.6037, -58.3816),
    "dubai": (25.2048, 55.2708), "tel aviv": (32.0853, 34.7818),
    "istanbul": (41.0082, 28.9784), "cairo": (30.0444, 31.2357),
    "lagos": (6.5244, 3.3792), "johannesburg": (-26.2041, 28.0473),
    "nairobi": (-1.2921, 36.8219), "casablanca": (33.5731, -7.5898),
    "tunis": (36.8065, 10.1815), "mumbai": (19.0760, 72.8777),
    "delhi": (28.7041, 77.1025), "bangalore": (12.9716, 77.5946),
    "singapore": (1.3521, 103.8198), "hong kong": (22.3193, 114.1694),
    "tokyo": (35.6762, 139.6503), "seoul": (37.5665, 126.9780),
    "shanghai": (31.2304, 121.4737), "beijing": (39.9042, 116.4074),
    "sydney": (-33.8688, 151.2093), "melbourne": (-37.8136, 144.9631),
    "auckland": (-36.8485, 174.7633),
}

# US state codes, so "Austin, TX" resolves to Austin.
_STATE_SUFFIX_RE = re.compile(r",\s*[A-Za-z]{2,3}\.?$")

_GEO_SCHEMA = """
CREATE TABLE IF NOT EXISTS geocode (
    city TEXT PRIMARY KEY,
    lat  REAL NOT NULL,
    lon  REAL NOT NULL
);
"""


def ensure_table(path: Path | None = None) -> None:
    with vault.connect(path) as conn:
        conn.executescript(_GEO_SCHEMA)


def normalise(city: str) -> str:
    """Lowercased city key: drops a trailing state or country code."""
    value = " ".join((city or "").split()).strip(" .,")
    if not value:
        return ""
    value = _STATE_SUFFIX_RE.sub("", value)
    return value.strip().lower()


def remember(city: str, lat: float, lon: float, path: Path | None = None) -> None:
    """Cache a coordinate so it never has to be resolved again."""
    key = normalise(city)
    if not key:
        return
    ensure_table(path)
    with vault.connect(path) as conn:
        conn.execute(
            "INSERT INTO geocode(city, lat, lon) VALUES(?, ?, ?) "
            "ON CONFLICT(city) DO UPDATE SET lat = excluded.lat, lon = excluded.lon",
            (key, float(lat), float(lon)),
        )


def cached(path: Path | None = None) -> dict[str, tuple[float, float]]:
    ensure_table(path)
    with vault.connect(path) as conn:
        rows = conn.execute("SELECT city, lat, lon FROM geocode").fetchall()
    return {row["city"]: (row["lat"], row["lon"]) for row in rows}


def resolve(city: str, cache: dict[str, tuple[float, float]] | None = None
            ) -> tuple[float, float] | None:
    """Coordinates for a city name, from the SQLite cache or the bundled table.

    Never touches the network. Tries the full name, then the part before the
    first comma, then the part after the last one, so both "Brooklyn, New York"
    and "New York, Brooklyn" land on New York.
    """
    key = normalise(city)
    if not key:
        return None
    store = cache if cache is not None else cached()
    candidates = [key]
    if "," in key:
        candidates.append(key.split(",")[0].strip())
        candidates.append(key.rsplit(",", 1)[-1].strip())
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in store:
            return store[candidate]
        if candidate in CITIES:
            return CITIES[candidate]
    return None


def city_points(rows: list[dict], path: Path | None = None
                ) -> tuple[list[dict], list[str]]:
    """Group leads by city into map points, plus the cities we could not place.

    Returns ``(points, unplaced)`` so the UI can show the gap honestly instead
    of quietly dropping a third of the list.
    """
    store = cached(path)
    grouped: dict[str, dict] = {}
    unplaced: dict[str, int] = {}

    for row in rows:
        city = (row.get("location") or "").strip()
        if not city:
            continue
        point = resolve(city, store)
        if point is None:
            unplaced[city] = unplaced.get(city, 0) + 1
            continue
        key = normalise(city)
        entry = grouped.setdefault(key, {
            "city": city, "lat": point[0], "lon": point[1],
            "leads": 0, "emails": 0, "types": set(),
        })
        entry["leads"] += 1
        if row.get("email"):
            entry["emails"] += 1
        if row.get("lead_type"):
            entry["types"].add(row["lead_type"])

    points = []
    for entry in grouped.values():
        entry["types"] = ", ".join(sorted(entry["types"])) or "Unclassified"
        points.append(entry)
    points.sort(key=lambda p: -p["leads"])
    return points, [f"{city} ({count})" for city, count in
                    sorted(unplaced.items(), key=lambda kv: -kv[1])]


# ---------------------------------------------------------------------------
# Radar map construction
# ---------------------------------------------------------------------------
# The deck.gl layers live here rather than in app.py so the map is defined
# beside the coordinates that feed it, and so it can be built and inspected
# without a Streamlit script context.
#
# pydeck is imported inside each function on purpose: this module is the
# geocoder, tools/selftest.py imports it directly, and a top-level UI-library
# import would make the geocoding tests depend on deck.gl being installed.
# core/diagnostics.py defers its core.cannon import for the same reason.

# Carto Dark Matter, named explicitly rather than through pydeck's "dark"
# alias, so the basemap cannot change underneath us on a pydeck upgrade.
CARTO_DARK_MATTER = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"

# Aggressive perspective: the map reads as a tracking table, not a chart.
MAP_PITCH = 60
MAP_BEARING = 30

CYAN = [0, 255, 255, 200]      # target columns
CYAN_SOFT = [0, 243, 255, 55]  # ground glow under each column
AMBER = [255, 170, 0, 160]     # routing arcs, at the hub end

# Acquired targets. Amber reads as "locked" against the cyan field, and the
# halo gives the column a visible ring without a second draw call per frame.
LOCKED = [255, 170, 0, 255]
LOCKED_HALO = [57, 255, 136, 110]
# Quarantined: everything outside the zone drops to a tenth of its alpha and
# falls back into the void rather than disappearing, so the operator keeps the
# shape of the full map while working inside the zone.
DIMMED = [0, 255, 255, 20]
DIMMED_SOFT = [0, 243, 255, 8]
# The drawn zone itself.
ZONE_FILL = [0, 243, 255, 40]
ZONE_EDGE = [0, 243, 255, 190]

# Stable layer ids. st.pydeck_chart refuses to keep a chart stateful across
# reruns unless every layer declares one, and the selection state comes back
# keyed by exactly these strings.
LAYER_GLOW = "nx-glow"
LAYER_ARCS = "nx-arcs"
LAYER_COLUMNS = "nx-columns"    # the pickable layer: selection reads this key
LAYER_ZONE = "nx-zone"
LAYER_HALO = "nx-halo"


def view_state(points: list[dict], zoom: float = 2.0):
    """Camera locked on the busiest city, tilted into a 3D perspective."""
    import pydeck as pdk

    if points:
        lat, lon = float(points[0]["lat"]), float(points[0]["lon"])
    else:
        lat, lon = 20.0, 0.0
    return pdk.ViewState(latitude=lat, longitude=lon, zoom=zoom,
                         pitch=MAP_PITCH, bearing=MAP_BEARING)


def arc_rows(points: list[dict]) -> list[dict]:
    """Routing arcs from the busiest city to every other city.

    ``city_points`` returns its points sorted by lead volume, so ``points[0]``
    is the natural hub. A single city would arc to itself - a zero-length arc
    that deck.gl draws as a dot - so that case returns nothing.
    """
    if len(points) < 2:
        return []
    hub = points[0]
    return [
        {
            "from_lon": float(hub["lon"]), "from_lat": float(hub["lat"]),
            "to_lon": float(point["lon"]), "to_lat": float(point["lat"]),
            "hub": hub["city"], "city": point["city"], "leads": int(point["leads"]),
        }
        for point in points[1:]
    ]


EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres.

    Plain haversine rather than a projected approximation: the map spans whole
    continents, and a flat-earth delta is wrong by hundreds of kilometres at
    the latitudes this list actually covers.
    """
    from math import asin, cos, radians, sin, sqrt

    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = phi2 - phi1
    dlambda = radians(lon2 - lon1)
    h = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    # min() guards the sqrt against a float overshoot past 1.0,
    # which asin would reject for a near-antipodal pair.
    return 2 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(h)))


def capture_zone(points: list[dict], seeds: list[int],
                 radius_km: float = 0.0) -> list[int]:
    """Indices captured by a zone seeded on ``seeds``.

    The seeds are what the operator picked on the map. With a radius the zone
    widens to every other point within ``radius_km`` of any seed, which is what
    turns a handful of clicks into a territory. Returns positional indices into
    ``points``, sorted, including the seeds themselves.
    """
    valid = [i for i in seeds if 0 <= i < len(points)]
    if not valid or radius_km <= 0:
        return sorted(set(valid))

    captured = set(valid)
    for index, point in enumerate(points):
        if index in captured:
            continue
        for seed in valid:
            origin = points[seed]
            if haversine_km(float(origin["lat"]), float(origin["lon"]),
                            float(point["lat"]), float(point["lon"])) <= radius_km:
                captured.add(index)
                break
    return sorted(captured)


def zone_polygon(points: list[dict], seeds: list[int],
                 radius_km: float, segments: int = 48) -> list[dict]:
    """One translucent disc per seed, as polygon rings deck.gl can fill.

    Drawn in degrees rather than metres because PolygonLayer takes lon/lat
    pairs. Longitude is divided by cos(latitude) so the disc stays circular on
    the ground instead of squashing towards the poles.
    """
    from math import cos, pi, radians, sin

    if radius_km <= 0:
        return []
    rings = []
    for seed in seeds:
        if not 0 <= seed < len(points):
            continue
        origin = points[seed]
        lat, lon = float(origin["lat"]), float(origin["lon"])
        dlat = radius_km / 111.32
        dlon = dlat / max(0.2, cos(radians(lat)))
        ring = []
        for step in range(segments + 1):
            angle = 2 * pi * step / segments
            ring.append([lon + dlon * cos(angle), lat + dlat * sin(angle)])
        rings.append({"polygon": ring, "city": origin.get("city", "")})
    return rings


def map_layers(points: list[dict], selected: list[int] | None = None,
               radius_km: float = 0.0) -> list:
    """Ground glow, zone, arcs, halo and columns, in draw order.

    With a selection the map goes into quarantine: captured targets carry the
    locked colour, everything else drops to a tenth of its alpha. The colours
    are baked into the frame as a column rather than set per layer, so the two
    states are one draw call instead of two overlapping layers fighting over
    the same depth.
    """
    import pandas as pd
    import pydeck as pdk

    # view_state and arc_rows both handle an empty list; this one has to as
    # well, or an empty DataFrame raises KeyError on ["leads"].
    if not points:
        return []

    captured = set(capture_zone(points, selected or [], radius_km))
    quarantined = bool(captured)

    frame = pd.DataFrame(points)
    tallest = max(1, int(frame["leads"].max()))
    frame["_locked"] = [i in captured for i in range(len(points))]
    if quarantined:
        frame["_fill"] = [LOCKED if hit else DIMMED for hit in frame["_locked"]]
        frame["_glow"] = [LOCKED_HALO if hit else DIMMED_SOFT
                          for hit in frame["_locked"]]
    else:
        frame["_fill"] = [CYAN] * len(frame)
        frame["_glow"] = [CYAN_SOFT] * len(frame)

    layers = [
        pdk.Layer(
            "ScatterplotLayer",
            id=LAYER_GLOW,
            data=frame,
            get_position=["lon", "lat"],
            get_radius="leads",
            radius_scale=26000,
            radius_min_pixels=6,
            get_fill_color="_glow",
            pickable=False,
        )
    ]

    # The zone sits under the columns so the discs read as ground, not as fog
    # over the targets.
    rings = zone_polygon(points, selected or [], radius_km)
    if rings:
        layers.append(
            pdk.Layer(
                "PolygonLayer",
                id=LAYER_ZONE,
                data=pd.DataFrame(rings),
                get_polygon="polygon",
                get_fill_color=ZONE_FILL,
                get_line_color=ZONE_EDGE,
                line_width_min_pixels=2,
                stroked=True,
                filled=True,
                pickable=False,
            )
        )

    arcs = arc_rows(points)
    if arcs:
        layers.append(
            pdk.Layer(
                "ArcLayer",
                id=LAYER_ARCS,
                data=pd.DataFrame(arcs),
                get_source_position=["from_lon", "from_lat"],
                get_target_position=["to_lon", "to_lat"],
                get_source_color=AMBER if not quarantined else DIMMED,
                get_target_color=CYAN if not quarantined else DIMMED,
                get_width=1.6,
                get_height=0.45,
                pickable=False,
            )
        )

    # A ring around every captured column. Static, not animated: a pulse would
    # need a redraw loop, and a loop around this chart means a rerun loop.
    if quarantined:
        locked = frame[frame["_locked"]]
        if not locked.empty:
            layers.append(
                pdk.Layer(
                    "ScatterplotLayer",
                    id=LAYER_HALO,
                    data=locked,
                    get_position=["lon", "lat"],
                    get_radius="leads",
                    radius_scale=44000,
                    radius_min_pixels=14,
                    get_fill_color=LOCKED_HALO,
                    stroked=True,
                    get_line_color=[57, 255, 136, 220],
                    line_width_min_pixels=2,
                    pickable=False,
                )
            )

    layers.append(
        pdk.Layer(
            "ColumnLayer",
            id=LAYER_COLUMNS,
            data=frame,
            get_position=["lon", "lat"],
            get_elevation="leads",
            elevation_scale=90000 / tallest,
            radius=70000,
            get_fill_color="_fill",
            pickable=True,
            auto_highlight=True,
            extruded=True,
        )
    )
    return layers


# Free roam. pdk.Deck has no controller argument - the setting lives on the
# View - and the default view deck.gl builds sets `controller: true`, which
# sounds like enough and is not: with the bare default, rotation sits behind a
# modifier key and the wheel is the only thing that feels connected. Every
# gesture is therefore named, so a plain drag orbits the globe. That is the
# whole point of holding the camera at sixty degrees.
FREE_ROAM = {
    "dragPan": True,
    "dragRotate": True,      # plain drag orbits, no ctrl or right button
    "scrollZoom": True,
    "doubleClickZoom": True,
    "touchZoom": True,
    "touchRotate": True,
    "keyboard": True,
    "inertia": 300,          # the globe keeps turning after the drag ends
}


def map_view(controller=None):
    """The MapView the deck renders through, with its gestures spelled out."""
    import pydeck as pdk

    return pdk.View(type="MapView",
                    controller=FREE_ROAM if controller is None else controller)


def deck(points: list[dict], selected: list[int] | None = None,
         radius_km: float = 0.0):
    """The whole map, ready for ``st.pydeck_chart``."""
    import pydeck as pdk

    return pdk.Deck(
        layers=map_layers(points, selected, radius_km),
        initial_view_state=view_state(points),
        views=[map_view()],
        map_style=CARTO_DARK_MATTER,
        tooltip={"text": "{city}\n{leads} leads, {emails} with an email\n{types}"},
    )
