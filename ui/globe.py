"""The holographic target globe for the Network & Radar tab.

A Globe.gl scene mounted in its own ``components.html`` iframe: a dark cyan
wireframe sphere with the vault's cities as glowing points and animated arcs
sweeping from the busiest city (the hub) to the rest. Drag to rotate, scroll to
dive - all of it client side inside the iframe.

Why a plain iframe and not a bidirectional component: ``components.html`` has no
channel back to Python, so the globe can display and be flown around but cannot
report a click back to the server. Target selection therefore lives in a normal
Streamlit control beside the globe, and the chosen cities are passed back in as
data so the globe lights them. Building a return channel would mean a compiled
custom component (an npm build, no longer offline-first) or poking the parent
DOM through brittle testid selectors - the exact thing that broke this project
once when Streamlit renamed its internals.

The Globe.gl bundle is fetched from a CDN at render time and ships its own copy
of three.js, independent of the r128 the parent page loads for its background.
If that fetch fails the iframe says so in plain text rather than showing an
empty black box - the globe is the centerpiece, not an ornament, so its absence
is reported.
"""
from __future__ import annotations

import json

import streamlit.components.v1 as components

# The neon palette, matched to the rest of the HUD. Kept here as hex because
# that is what the WebGL material and the CSS both want.
GLOBE_CYAN = "#00f3ff"
GLOBE_POINT = "#4dffff"
ARC_HUB_HEX = "#50ffff"
ARC_EDGE_HEX = "#00c8ff"

CDN_GLOBE = "https://cdn.jsdelivr.net/npm/globe.gl"


def globe_payload(points: list[dict], selected_keys: set[str] | None = None,
                  normalise=None) -> dict:
    """The globe's data: points, arcs and the hub, as plain JSON-ready dicts.

    Pure and side-effect free so it can be tested without a browser. ``points``
    is the same city list every other surface reads (sorted by lead volume, so
    ``points[0]`` is the hub). ``selected_keys`` are normalised city keys the
    operator has acquired; the matching points come back flagged so the globe
    can light them.

    ``normalise`` is injected (``geo.normalise``) rather than imported so this
    module has no dependency on geo and the tests can pass a trivial one.
    """
    selected_keys = selected_keys or set()
    ident = normalise or (lambda s: (s or "").strip().lower())

    out_points = []
    for point in points:
        key = ident(point.get("city", ""))
        out_points.append({
            "city": point.get("city", ""),
            "lat": float(point.get("lat", 0.0)),
            "lng": float(point.get("lon", 0.0)),
            "leads": int(point.get("leads", 0)),
            "emails": int(point.get("emails", 0)),
            "types": point.get("types", ""),
            "selected": key in selected_keys,
        })

    arcs = []
    if len(out_points) >= 2:
        hub = out_points[0]
        for point in out_points[1:]:
            arcs.append({
                "startLat": hub["lat"], "startLng": hub["lng"],
                "endLat": point["lat"], "endLng": point["lng"],
                "hub": hub["city"], "city": point["city"],
                "selected": point["selected"],
            })

    return {
        "points": out_points,
        "arcs": arcs,
        "hub": out_points[0]["city"] if out_points else None,
    }


def payload_json(payload: dict) -> str:
    """Serialise a payload for embedding in a script tag, safely.

    A scraped city name can contain ``</script>`` (locations come from profile
    bios), which would terminate the tag even inside a JSON string. Escaping the
    ``</`` sequence is the standard defence for JSON embedded in HTML.
    """
    return json.dumps(payload).replace("</", "<\\/").replace(" ", "\\u2028") \
                              .replace(" ", "\\u2029")


def _html(payload: dict, height: int) -> str:
    data = payload_json(payload)
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>
  html, body {{ margin: 0; height: 100%; background: transparent; overflow: hidden; }}
  #globe {{ width: 100%; height: {height}px; }}
  #globe canvas {{ outline: none; }}
  #msg {{
      position: absolute; inset: 0; display: none; align-items: center;
      justify-content: center; color: #7fd6ff; font: 14px/1.5 monospace;
      text-align: center; padding: 0 24px;
  }}
  #legend {{
      position: absolute; left: 14px; bottom: 12px; color: #7fd6ff;
      font: 11px/1.6 monospace; letter-spacing: .08em; opacity: .85;
      text-shadow: 0 0 8px rgba(0,243,255,.5); pointer-events: none;
  }}
</style></head>
<body>
  <div id="globe"></div>
  <div id="msg">GLOBE OFFLINE - the 3D component could not load.<br>
       The rest of the console is unaffected.</div>
  <div id="legend"></div>
  <script>
    var DATA = {data};
  </script>
  <script src="{CDN_GLOBE}"
          onerror="document.getElementById('msg').style.display='flex';"></script>
  <script>
    (function () {{
      if (typeof Globe === 'undefined') {{
        document.getElementById('msg').style.display = 'flex';
        return;
      }}
      var el = document.getElementById('globe');
      var world = Globe()(el)
        .backgroundColor('rgba(0,0,0,0)')
        .showGlobe(true)
        .showGraticules(true)          // the lat/long grid = wireframe hologram
        .showAtmosphere(true)
        .atmosphereColor('{GLOBE_CYAN}')
        .atmosphereAltitude(0.20)
        .pointsData(DATA.points)
        .pointLat('lat').pointLng('lng')
        .pointColor(function (d) {{ return d.selected ? '#ffffff' : '{GLOBE_POINT}'; }})
        .pointAltitude(function (d) {{
            return 0.01 + Math.min(0.5, Math.log10(1 + d.leads) * 0.12); }})
        .pointRadius(function (d) {{ return d.selected ? 0.55 : 0.3; }})
        .pointLabel(function (d) {{
            return '<div style="font:12px monospace;color:#00f3ff">'
                 + d.city + '<br>' + d.leads + ' leads, ' + d.emails
                 + ' with an email<br>' + d.types + '</div>'; }})
        .arcsData(DATA.arcs)
        .arcColor(function (d) {{
            return d.selected ? ['#ffffff', '{ARC_EDGE_HEX}']
                              : ['{ARC_HUB_HEX}', '{ARC_EDGE_HEX}']; }})
        .arcStroke(0.5)
        .arcDashLength(0.4).arcDashGap(0.2).arcDashAnimateTime(2200)
        .arcAltitudeAutoScale(0.4);

      // The hologram look: a dark navy sphere with a cyan wireframe skin, no
      // satellite texture at all.
      var mat = world.globeMaterial();
      mat.color.set('#04121f');
      mat.emissive.set('#00243a');
      mat.wireframe = true;
      mat.transparent = true;
      mat.opacity = 0.92;

      // Free flight: drag to rotate, scroll to dive, and a slow idle spin so
      // it reads as live even when untouched. The spin stops the moment the
      // operator grabs it.
      var controls = world.controls();
      controls.autoRotate = true;
      controls.autoRotateSpeed = 0.35;
      controls.enableZoom = true;
      controls.addEventListener('start', function () {{ controls.autoRotate = false; }});

      if (DATA.hub) {{
        var hub = DATA.points[0];
        world.pointOfView({{ lat: hub.lat, lng: hub.lng, altitude: 2.4 }}, 0);
      }}
      document.getElementById('legend').textContent =
        DATA.points.length + ' CITIES  //  ' + DATA.arcs.length + ' DATA STREAMS'
        + (DATA.hub ? '  //  HUB: ' + DATA.hub.toUpperCase() : '');

      function size() {{
        world.width(el.clientWidth).height({height});
      }}
      size();
      window.addEventListener('resize', size);
    }})();
  </script>
</body>
</html>"""


def render_globe(points: list[dict], selected_keys: set[str] | None = None,
                 normalise=None, height: int = 720) -> None:
    """Mount the globe as the centerpiece of the tab."""
    payload = globe_payload(points, selected_keys, normalise)
    components.html(_html(payload, height), height=height + 6, scrolling=False)
