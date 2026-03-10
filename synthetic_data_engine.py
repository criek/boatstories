#!/usr/bin/env python3
"""
Synthetic AIS data generator for watchfloor testing.

Generates vessel tracks that exhibit patterns from the watchfloor checklist:
- Slow loitering near cables
- Anchoring near infrastructure
- Repeated cable crossings
- Route deviations
- AIS gaps
- etc.

Outputs (per scenario in synthetic_output/<scenario>/):
  - track.geojson       Track + high-score point markers
  - history_parsed.json mst_output-compatible format
  - details_normalized.json Current position / vessel details
  - synthetic_summary.json Score breakdown

Usage:
  python synthetic_data_engine.py loitering   # Regenerate alert data + track (writes to ethera/)
  python synthetic_data_engine.py --all      # Run all scenarios (last one wins)
  python synthetic_data_engine.py --analyze  # Generate alerts from real ethera + OSM data (recommended)
"""

import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

try:
    from shapely.geometry import Point as ShapelyPoint, Polygon, LineString
    from shapely.ops import nearest_points
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False


def _point_in_polygon_raycast(x, y, ring):
    """Ray casting: point (x,y) inside polygon ring? Ring is list of [lon, lat]."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    p1x, p1y = ring[0][0], ring[0][1]
    for i in range(1, n + 1):
        p2x, p2y = ring[i % n][0], ring[i % n][1]
        if p1y == p2y:
            p1x, p1y = p2x, p2y
            continue
        if y > min(p1y, p2y) and y <= max(p1y, p2y) and x <= max(p1x, p2x):
            xints = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
            if p1x == p2x or x <= xints:
                inside = not inside
        p1x, p1y = p2x, p2y
    return inside

# ── SCORING RULES (from watchfloor checklist) ─────────────────────────────────
SCORE_RULES = {
    "inside_no_anchoring": 3,
    "inside_restricted_area": 3,
    "very_close_to_cable": 3,
    "anchoring_near_asset": 3,
    "sustained_low_speed_near_asset": 2,
    "repeated_cable_crossings": 2,
    "repeat_visits_to_asset": 2,
    "ais_gap_near_asset": 2,
    "large_cog_heading_mismatch": 1,
    "inside_legitimate_anchorage": -2,
    "clear_normal_transit": -1,
}

ALERT_LEVELS = {
    (0, 2): "Low concern / monitor",
    (3, 5): "Review",
    (6, 8): "Escalate",
    (9, 999): "High-priority alert",
}


def haversine_km(lon1, lat1, lon2, lat2):
    """Distance in km between two points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def point_to_line_dist_km(px, py, x1, y1, x2, y2):
    """Distance in km from point (px, py) to line segment (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return haversine_km(px, py, x1, y1)
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    x, y = x1 + t * dx, y1 + t * dy
    return haversine_km(px, py, x, y)


# ── SAMPLE INFRASTRUCTURE (Belgian/Dutch coast) ───────────────────────────────
SAMPLE_CABLES = [
    {"name": "Concerto 1", "coords": [[3.26, 51.97], [3.19, 51.82], [3.07, 51.65], [3.22, 51.48], [3.25, 51.42]]},
    {"name": "Cable near Terneuzen", "coords": [[3.20, 51.35], [3.25, 51.40], [3.30, 51.45]]},
]
SAMPLE_RESTRICTED = {"center": (3.25, 51.42), "radius_km": 0.8}
SAMPLE_ANCHORAGE = {"center": (3.15, 51.32), "radius_km": 0.4}
SAMPLE_NO_ANCHOR = {"center": (3.28, 51.44), "radius_km": 0.25}


def compute_derived_fields(point, prev_point, cable_coords, track_history):
    """Compute derived fields for a single position."""
    lon, lat = point.get("lon", point.get("lng")), point["lat"]
    fields = {}

    # Distance to nearest cable (km)
    min_cable_dist = 999
    for cable in cable_coords:
        coords = cable if isinstance(cable, list) else cable.get("coords", cable)
        for i in range(len(coords) - 1):
            x1, y1 = coords[i][0], coords[i][1]
            x2, y2 = coords[i + 1][0], coords[i + 1][1]
            d = point_to_line_dist_km(lon, lat, x1, y1, x2, y2)
            min_cable_dist = min(min_cable_dist, d)
    fields["distance_to_cable_km"] = round(min_cable_dist, 3)
    fields["very_close_to_cable"] = min_cable_dist < 0.05

    # Inside zones
    def in_circle(center, r_km):
        return haversine_km(lon, lat, center[0], center[1]) < r_km

    fields["inside_restricted_area"] = in_circle(SAMPLE_RESTRICTED["center"], SAMPLE_RESTRICTED["radius_km"])
    fields["inside_anchorage"] = in_circle(SAMPLE_ANCHORAGE["center"], SAMPLE_ANCHORAGE["radius_km"])
    fields["inside_no_anchoring"] = in_circle(SAMPLE_NO_ANCHOR["center"], SAMPLE_NO_ANCHOR["radius_km"])

    # Cable crossing (simplified: passed through cable corridor)
    fields["cable_crossing"] = prev_point and (
        (prev_point.get("distance_to_cable_km", 999) > 0.05 and min_cable_dist < 0.05)
        or (prev_point.get("distance_to_cable_km", 999) < 0.05 and min_cable_dist > 0.05)
    )

    # Speed / anchoring
    sog = point.get("speed_knots", point.get("sog", 0))
    fields["sustained_low_speed"] = sog < 2 and min_cable_dist < 0.1
    fields["anchoring_near_asset"] = sog < 0.5 and min_cable_dist < 0.1

    # COG vs heading (if available)
    cog = point.get("cog", point.get("course_deg", 0))
    heading = point.get("true_heading", cog)
    fields["heading_cog_diff"] = abs((cog - heading + 180) % 360 - 180) if heading is not None else 0
    fields["large_cog_heading_mismatch"] = fields["heading_cog_diff"] > 30 and min_cable_dist < 0.1

    # Time gap (AIS silence)
    ts = point.get("timestamp")
    prev_ts = prev_point.get("timestamp") if prev_point else None
    if prev_ts and ts:
        gap_mins = (ts - prev_ts).total_seconds() / 60 if hasattr(ts - prev_ts, "total_seconds") else 0
        fields["time_gap_minutes"] = gap_mins
        fields["ais_gap_near_asset"] = gap_mins > 30 and min_cable_dist < 0.15
    else:
        fields["time_gap_minutes"] = 0
        fields["ais_gap_near_asset"] = False

    return fields


def score_point(point, track_context):
    """Compute risk score for a single point."""
    s = 0
    reasons = []

    if point.get("inside_no_anchoring"):
        s += SCORE_RULES["inside_no_anchoring"]
        reasons.append("inside_no_anchoring")
    if point.get("inside_restricted_area"):
        s += SCORE_RULES["inside_restricted_area"]
        reasons.append("inside_restricted_area")
    if point.get("very_close_to_cable"):
        s += SCORE_RULES["very_close_to_cable"]
        reasons.append("very_close_to_cable")
    if point.get("anchoring_near_asset"):
        s += SCORE_RULES["anchoring_near_asset"]
        reasons.append("anchoring_near_asset")
    if point.get("sustained_low_speed"):
        s += SCORE_RULES["sustained_low_speed_near_asset"]
        reasons.append("sustained_low_speed")
    if point.get("cable_crossing"):
        s += SCORE_RULES["repeated_cable_crossings"]  # simplified
        reasons.append("cable_crossing")
    if point.get("ais_gap_near_asset"):
        s += SCORE_RULES["ais_gap_near_asset"]
        reasons.append("ais_gap_near_asset")
    if point.get("large_cog_heading_mismatch"):
        s += SCORE_RULES["large_cog_heading_mismatch"]
        reasons.append("large_cog_heading_mismatch")
    if point.get("inside_anchorage"):
        s += SCORE_RULES["inside_legitimate_anchorage"]
        reasons.append("inside_anchorage (mitigation)")

    return s, reasons


# ── TRACK GENERATORS ───────────────────────────────────────────────────────────

def generate_normal_transit(n_points=80, base_lat=51.33, base_lon=3.2):
    """Normal transit: straight line, steady speed."""
    points = []
    t = datetime.now() - timedelta(hours=2)
    for i in range(n_points):
        t += timedelta(minutes=2)
        lat = base_lat + (i / n_points) * 0.15 + random.gauss(0, 0.001)
        lon = base_lon + (i / n_points) * 0.1 + random.gauss(0, 0.001)
        points.append({
            "lat": round(lat, 5), "lon": round(lon, 5),
            "speed_knots": 8 + random.gauss(0, 1),
            "cog": 45 + random.gauss(0, 5),
            "timestamp": t,
            "navstatus": 0,  # under way
        })
    return points


def generate_slow_loitering_near_cable(n_points=60, cable_lat=51.43, cable_lon=3.26):
    """Slow loitering: low speed, small area, near cable."""
    points = []
    t = datetime.now() - timedelta(hours=1)
    center_lat, center_lon = cable_lat, cable_lon
    for i in range(n_points):
        t += timedelta(minutes=2)
        angle = (i / n_points) * 4 * math.pi + random.gauss(0, 0.3)
        r = 0.008 + random.gauss(0, 0.002)
        lat = center_lat + r * math.sin(angle)
        lon = center_lon + r * math.cos(angle)
        points.append({
            "lat": round(lat, 5), "lon": round(lon, 5),
            "speed_knots": 0.5 + random.gauss(0, 0.3),
            "cog": (angle * 180 / math.pi) % 360,
            "timestamp": t,
            "navstatus": 1,  # anchored
        })
    return points


def generate_repeated_cable_crossings(n_points=100, cable_coords=None):
    """Track that crosses a cable multiple times."""
    cable_coords = cable_coords or SAMPLE_CABLES[1]["coords"]  # Cable near Terneuzen
    cx, cy = (cable_coords[0][0] + cable_coords[-1][0]) / 2, (cable_coords[0][1] + cable_coords[-1][1]) / 2
    points = []
    t = datetime.now() - timedelta(hours=3)
    for leg in range(4):  # 4 crossings
        for i in range(n_points // 4):
            t += timedelta(minutes=1.5)
            # Zig-zag across cable
            side = 1 if leg % 2 == 0 else -1
            lat = cy + side * (0.02 * (i / (n_points // 4)) - 0.01) + random.gauss(0, 0.0005)
            lon = cx + (i / (n_points // 4)) * 0.05 + random.gauss(0, 0.0005)
            points.append({
                "lat": round(lat, 5), "lon": round(lon, 5),
                "speed_knots": 4 + random.gauss(0, 0.5),
                "cog": 90 if leg % 2 == 0 else 270,
                "timestamp": t,
                "navstatus": 0,
            })
    return points


def generate_anchoring_near_infra(n_points=40, cable_lat=51.45, cable_lon=3.22):
    """Anchored near cable: very low speed, drift pattern."""
    points = []
    t = datetime.now() - timedelta(minutes=90)
    for i in range(n_points):
        t += timedelta(minutes=2)
        lat = cable_lat + 0.015 + random.gauss(0, 0.002)
        lon = cable_lon + 0.01 + random.gauss(0, 0.002)
        points.append({
            "lat": round(lat, 5), "lon": round(lon, 5),
            "speed_knots": 0.1 + random.gauss(0, 0.05),
            "cog": random.uniform(0, 360),
            "timestamp": t,
            "navstatus": 1,  # anchored
        })
    return points


def generate_ais_gap_near_cable(n_points=50, gap_at=25):
    """Track with AIS silence (gap) near cable."""
    points = []
    t = datetime.now() - timedelta(hours=2)
    base_lat, base_lon = 51.4, 3.25  # Route passes near cable at 51.42
    for i in range(n_points):
        if i == gap_at:
            t += timedelta(minutes=45)  # 45 min gap
        else:
            t += timedelta(minutes=2)
        lat = base_lat + (i / n_points) * 0.2 + random.gauss(0, 0.001)
        lon = base_lon + (i / n_points) * 0.15 + random.gauss(0, 0.001)
        points.append({
            "lat": round(lat, 5), "lon": round(lon, 5),
            "speed_knots": 6 + random.gauss(0, 0.5),
            "cog": 50,
            "timestamp": t,
            "navstatus": 0,
        })
    return points


# ── OUTPUT FORMATS ────────────────────────────────────────────────────────────

def to_geojson_track(points, properties=None):
    """Convert points to GeoJSON LineString + Point features."""
    coords = [[p["lon"], p["lat"]] for p in points]
    props = dict(properties or {})
    if points and points[0].get("score") is not None:
        props["max_score"] = max(p.get("score", 0) for p in points)
    features = [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": props}
    ]
    for p in points:
        if p.get("score", 0) >= 3:  # Include high-scoring points as markers
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                "properties": {
                    "score": p.get("score", 0),
                    "alert_level": p.get("alert_level", ""),
                    "reasons": p.get("score_reasons", []),
                },
            })
    return {"type": "FeatureCollection", "features": features}


def _ts_str(t):
    return t.isoformat() + "Z" if hasattr(t, "isoformat") else str(t)


def to_mst_history(points):
    """Convert to mst_output/history_parsed.json format."""
    return {
        "raw_format_guess": "synthetic",
        "points": [
            {
                "line_index": i + 1,
                "lat": p["lat"],
                "lon": p["lon"],
                "timestamp": _ts_str(p["timestamp"]),
                "speed_knots_guess": p.get("speed_knots", 0),
                "course_deg_guess": p.get("cog", 0),
                "score": p.get("score", 0),
                "alert_level": p.get("alert_level", ""),
            }
            for i, p in enumerate(points)
        ],
    }


def to_mst_details(points, mmsi="999999001"):
    """Convert to mst_output/details_normalized.json format."""
    if not points:
        return {}
    last = points[-1]
    return {
        "requested_id": mmsi,
        "requested_id_field": "mmsi",
        "lat": last["lat"],
        "lon": last["lon"],
        "speed_knots": last.get("speed_knots", 0),
        "current_timestamp": _ts_str(last["timestamp"]),
        "details_flat": {"follow_info": {"dest_txt": "Synthetic track", "dest": "Test"}},
    }


# ── MAIN ─────────────────────────────────────────────────────────────────────

SCENARIOS = {
    "normal": ("Normal transit", generate_normal_transit),
    "loitering": ("Slow loitering near cable", generate_slow_loitering_near_cable),
    "crossings": ("Repeated cable crossings", generate_repeated_cable_crossings),
    "anchoring": ("Anchoring near infrastructure", generate_anchoring_near_infra),
    "ais_gap": ("AIS silence near cable", generate_ais_gap_near_cable),
}


def _unix_ts(t):
    """Unix timestamp from datetime."""
    return int(t.timestamp()) if hasattr(t, "timestamp") else 0


def to_ethera_formats(points, scenario_name, label):
    """Output in data/ethera/ format for frontend."""
    coords = [[p["lon"], p["lat"]] for p in points]
    first_ts = points[0]["timestamp"] if points else None
    last_ts = points[-1]["timestamp"] if points else None
    line_geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "scenario": scenario_name,
                "label": label,
                "point_count": len(points),
                "start": _ts_str(first_ts) if first_ts else None,
                "end": _ts_str(last_ts) if last_ts else None,
            }
        }]
    }
    points_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                "properties": {
                    "timestamp": _unix_ts(p["timestamp"]),
                    "datetime": _ts_str(p["timestamp"]),
                    "speed": p.get("speed_knots"),
                    "heading": p.get("cog"),
                    "score": p.get("score", 0),
                    "alert_level": p.get("alert_level", ""),
                    "reasons": p.get("score_reasons", []),
                }
            }
            for p in points
        ]
    }
    return line_geojson, points_geojson


def run_scenario(name, output_dir=None):
    """Generate data for a scenario and write outputs to data/ethera/ and output/data/ethera/."""
    if name not in SCENARIOS:
        print(f"Unknown scenario: {name}. Choose from: {list(SCENARIOS.keys())}")
        return
    label, gen = SCENARIOS[name]
    print(f"Generating: {label}")
    points = gen()

    # Compute derived fields and scores
    prev = None
    cable_cross_count = 0
    for p in points:
        p["_derived"] = compute_derived_fields(p, prev, SAMPLE_CABLES, [])
        p.update(p["_derived"])
        if p.get("cable_crossing"):
            cable_cross_count += 1
        score, reasons = score_point(p, {"cable_cross_count": cable_cross_count})
        p["score"] = score
        p["score_reasons"] = reasons
        for (lo, hi), level in ALERT_LEVELS.items():
            if lo <= score <= hi:
                p["alert_level"] = level
                break
        else:
            p["alert_level"] = "Unknown"
        prev = p

    total_score = sum(p["score"] for p in points)
    max_score = max(p["score"] for p in points)
    alert_points = [p for p in points if p["score"] >= 3]
    print(f"  Points: {len(points)}, Total score: {total_score}, Max point score: {max_score}, Alert points: {len(alert_points)}")

    # Alerts data for frontend tab
    alerts = {
        "scenario": name,
        "label": label,
        "summary": {"n_points": len(points), "total_score": total_score, "max_point_score": max_score, "alert_points": len(alert_points)},
        "score_breakdown": {r: sum(1 for p in points for r2 in p.get("score_reasons", []) if r2 == r) for r in set().union(*(set(p.get("score_reasons", [])) for p in points))},
        "alert_levels": {f"{k[0]}-{k[1]}": v for k, v in ALERT_LEVELS.items()},
        "high_score_points": [{"lat": p["lat"], "lon": p["lon"], "score": p["score"], "reasons": p.get("score_reasons", []), "alert_level": p.get("alert_level", "")} for p in points if p["score"] >= 3],
    }

    # Output to data/ethera/, output/data/ethera/, and ethera/ (for frontend)
    line_geo, points_geo = to_ethera_formats(points, name, label)
    for base in [Path("data/ethera"), Path("output/data/ethera")]:
        base.mkdir(parents=True, exist_ok=True)
        (base / "ethera.line.geojson").write_text(json.dumps(line_geo, indent=2))
        (base / "ethera.points.geojson").write_text(json.dumps(points_geo, indent=2))
        (base / "ethera-alerts.json").write_text(json.dumps(alerts, indent=2))

    # Write ethera-alerts.json to ethera/ (track/points come from pipeline, don't overwrite)
    Path("ethera").mkdir(parents=True, exist_ok=True)
    (Path("ethera") / "ethera-alerts.json").write_text(json.dumps(alerts, indent=2))

    # Also write mst-compatible and track.geojson to data/ethera
    data_ethera = Path("data/ethera")
    (data_ethera / "history_parsed.json").write_text(json.dumps(to_mst_history(points), indent=2))
    (data_ethera / "details_normalized.json").write_text(json.dumps(to_mst_details(points), indent=2))
    (data_ethera / "track.geojson").write_text(json.dumps(to_geojson_track(points, {"scenario": name, "label": label}), indent=2))

    print(f"  Wrote to data/ethera/, output/data/ethera/, and ethera/ethera-alerts.json")


# ── REAL-DATA ANALYSIS (ethera + OSM correlation) ───────────────────────────────

ETHERA_DIR = Path("ethera")
DATA_DIR = Path("data")
OSM_PATH = DATA_DIR / "openstreetmapdata-export.geojson"

# Distance thresholds (km)
VERY_CLOSE_KM = 0.05
NEAR_CABLE_KM = 0.15
CABLE_CROSSING_MATCH_KM = 0.5
TIMESTAMP_MATCH_SEC = 7200  # 2 hours - match track point to intersection by time
AIS_GAP_MINUTES = 30
LOW_SPEED_KNOTS = 2
ANCHORING_SPEED_KNOTS = 0.5


def _load_geojson(path):
    """Load GeoJSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _extract_intersection_centroids(geojson):
    """Extract intersection centroids from ethera.data-intersection.geojson."""
    centroids = []
    for f in geojson.get("features", []):
        if f.get("properties", {}).get("_role") != "intersection_centroid":
            continue
        geom = f.get("geometry")
        if not geom or geom.get("type") != "Point":
            continue
        coords = geom["coordinates"]
        props = f["properties"]
        centroids.append({
            "lon": coords[0],
            "lat": coords[1],
            "name": props.get("name", "unknown"),
            "_timestamp": props.get("_timestamp"),
            "_nearestDistKm": props.get("_nearestDistKm"),
        })
    return centroids


def _get_polygon_rings(geom):
    """Extract exterior rings from Polygon or MultiPolygon. Returns list of rings (each is list of [lon,lat])."""
    if not geom:
        return []
    coords = geom.get("coordinates")
    if not coords:
        return []
    if geom["type"] == "Polygon":
        return [coords[0]] if coords else []
    if geom["type"] == "MultiPolygon":
        return [p[0] for p in coords if p]
    return []


def _extract_osm_zones(geojson):
    """Extract restricted areas, anchorages, wind farms from OSM GeoJSON."""
    restricted = []
    anchorages = []
    wind_farms = []
    for f in geojson.get("features", []):
        props = f.get("properties", {})
        geom = f.get("geometry")
        if not geom:
            continue
        stype = props.get("seamark:type", "")
        restriction = props.get("seamark:restricted_area:restriction", "")
        name = props.get("seamark:name", props.get("name", ""))
        rings = _get_polygon_rings(geom)
        if not rings:
            continue
        if stype == "restricted_area" or (stype == "production_area" and restriction == "no_entry"):
            for ring in rings:
                restricted.append({"ring": ring, "name": name})
        elif stype == "anchorage":
            for ring in rings:
                anchorages.append({"ring": ring, "name": name})
        elif stype == "production_area" and "wind" in str(props.get("seamark:production_area:category", "")).lower():
            for ring in rings:
                wind_farms.append({"ring": ring, "name": name})
    return restricted, anchorages, wind_farms


def _extract_osm_cables(geojson):
    """Extract submarine cable LineStrings from OSM."""
    cables = []
    for f in geojson.get("features", []):
        props = f.get("properties", {})
        if props.get("seamark:type") != "cable_submarine":
            continue
        geom = f.get("geometry")
        if not geom or geom.get("type") != "LineString":
            continue
        coords = geom["coordinates"]
        cables.append({"coords": coords, "name": props.get("name", "cable")})
    return cables


def _point_in_any(lon, lat, zones):
    """Check if point (lon, lat) is inside any zone. Zones have 'ring' (list of [lon,lat]) and 'name'."""
    for z in zones:
        if _point_in_polygon_raycast(lon, lat, z["ring"]):
            return z.get("name", "")
    return None


def _estimate_speed_knots(lat1, lon1, ts1, lat2, lon2, ts2):
    """Estimate speed in knots between two points (nautical miles per hour)."""
    if not ts1 or not ts2 or ts2 == ts1:
        return None
    dist_km = haversine_km(lon1, lat1, lon2, lat2)
    dist_nm = dist_km / 1.852
    dt_hours = abs(ts2 - ts1) / 3600.0
    if dt_hours <= 0:
        return None
    return dist_nm / dt_hours


def run_real_data_analysis():
    """Analyze real ethera track + intersections + OSM, output true alerts to ethera/ethera-alerts.json."""
    points_path = ETHERA_DIR / "ethera.data.points.geojson"
    intersection_path = ETHERA_DIR / "ethera.data-intersection.geojson"

    if not points_path.exists():
        print(f"Missing {points_path}")
        return
    if not intersection_path.exists():
        print(f"Missing {intersection_path}")
        return

    points_geo = _load_geojson(points_path)
    intersection_geo = _load_geojson(intersection_path)
    centroids = _extract_intersection_centroids(intersection_geo)
    print(f"Loaded {len(centroids)} intersection centroids from ethera.data-intersection.geojson")

    restricted, anchorages, wind_farms = [], [], []
    cables_osm = []
    if OSM_PATH.exists():
        osm_geo = _load_geojson(OSM_PATH)
        restricted, anchorages, wind_farms = _extract_osm_zones(osm_geo)
        cables_osm = _extract_osm_cables(osm_geo)
        print(f"Loaded OSM: {len(restricted)} restricted, {len(anchorages)} anchorages, {len(wind_farms)} wind farms, {len(cables_osm)} cables")
    else:
        print(f"OSM file not found: {OSM_PATH}")

    # Build track points (sorted by timestamp ascending for speed/gap computation)
    track = []
    for f in points_geo.get("features", []):
        geom = f.get("geometry")
        if not geom or geom.get("type") != "Point":
            continue
        coords = geom["coordinates"]
        props = f.get("properties", {})
        ts = props.get("timestamp")
        track.append({
            "lat": coords[1],
            "lon": coords[0],
            "timestamp": ts,
            "datetime": props.get("datetime"),
        })
    track.sort(key=lambda p: p["timestamp"] or 0)
    print(f"Loaded {len(track)} track points")

    # Compute derived fields and scores for each point
    prev = None
    cable_cross_count = 0
    for i, p in enumerate(track):
        lon, lat = p["lon"], p["lat"]
        ts = p["timestamp"]

        # Distance to nearest intersection centroid (real cable crossing from ethera)
        min_dist_centroid = 999.0
        nearest_centroid = None
        for c in centroids:
            d = haversine_km(lon, lat, c["lon"], c["lat"])
            if d < min_dist_centroid:
                min_dist_centroid = d
                nearest_centroid = c

        # Also check distance to OSM cables (line segments)
        min_dist_cable = min_dist_centroid
        for cab in cables_osm:
            for j in range(len(cab["coords"]) - 1):
                x1, y1 = cab["coords"][j][0], cab["coords"][j][1]
                x2, y2 = cab["coords"][j + 1][0], cab["coords"][j + 1][1]
                d = point_to_line_dist_km(lon, lat, x1, y1, x2, y2)
                min_dist_cable = min(min_dist_cable, d)

        p["distance_to_cable_km"] = round(min(min_dist_centroid, min_dist_cable), 4)
        p["very_close_to_cable"] = p["distance_to_cable_km"] < VERY_CLOSE_KM
        p["near_cable"] = p["distance_to_cable_km"] < NEAR_CABLE_KM

        # Match to intersection centroid by timestamp (crossing event)
        p["cable_crossing"] = False
        p["matched_centroid_name"] = None
        if nearest_centroid and nearest_centroid.get("_timestamp") and ts:
            ts_diff = abs(ts - nearest_centroid["_timestamp"])
            if ts_diff < TIMESTAMP_MATCH_SEC and min_dist_centroid < CABLE_CROSSING_MATCH_KM:
                p["cable_crossing"] = True
                p["matched_centroid_name"] = nearest_centroid.get("name")

        if p["cable_crossing"]:
            cable_cross_count += 1

        # OSM zones
        p["inside_restricted_area"] = _point_in_any(lon, lat, restricted) is not None
        p["inside_anchorage"] = _point_in_any(lon, lat, anchorages) is not None
        p["inside_wind_farm"] = _point_in_any(lon, lat, wind_farms) is not None
        p["inside_no_anchoring"] = p["inside_restricted_area"]

        # Speed from consecutive points
        speed = None
        if prev and prev.get("timestamp") and ts:
            speed = _estimate_speed_knots(prev["lat"], prev["lon"], prev["timestamp"], lat, lon, ts)
        p["speed_knots"] = speed
        p["sustained_low_speed"] = speed is not None and speed < LOW_SPEED_KNOTS and p["near_cable"]
        p["anchoring_near_asset"] = speed is not None and speed < ANCHORING_SPEED_KNOTS and p["near_cable"]

        # AIS gap
        gap_mins = 0
        if prev and prev.get("timestamp") and ts:
            gap_mins = abs(ts - prev["timestamp"]) / 60.0
        p["time_gap_minutes"] = gap_mins
        p["ais_gap_near_asset"] = gap_mins > AIS_GAP_MINUTES and p["near_cable"]

        p["large_cog_heading_mismatch"] = False

        # Score
        score, reasons = score_point(p, {"cable_cross_count": cable_cross_count})
        p["score"] = score
        p["score_reasons"] = reasons
        for (lo, hi), level in ALERT_LEVELS.items():
            if lo <= score <= hi:
                p["alert_level"] = level
                break
        else:
            p["alert_level"] = "Unknown"
        prev = p

    alert_points = [p for p in track if p["score"] >= 3]
    total_score = sum(p["score"] for p in track)
    max_score = max(p["score"] for p in track) if track else 0

    print(f"Analysis: {len(track)} points, {len(alert_points)} alert points, max score {max_score}")

    alerts = {
        "scenario": "real_data",
        "label": "Real-data analysis (ethera + OSM)",
        "summary": {
            "n_points": len(track),
            "total_score": total_score,
            "max_point_score": max_score,
            "alert_points": len(alert_points),
            "intersection_centroids": len(centroids),
        },
        "score_breakdown": {
            r: sum(1 for p in track for r2 in p.get("score_reasons", []) if r2 == r)
            for r in set().union(*(set(p.get("score_reasons", [])) for p in track))
        },
        "alert_levels": {f"{k[0]}-{k[1]}": v for k, v in ALERT_LEVELS.items()},
        "high_score_points": [
            {
                "lat": p["lat"],
                "lon": p["lon"],
                "score": p["score"],
                "reasons": p.get("score_reasons", []),
                "alert_level": p.get("alert_level", ""),
                "timestamp": p.get("timestamp"),
                "datetime": p.get("datetime"),
                "matched_cable": p.get("matched_centroid_name"),
                "distance_to_cable_km": p.get("distance_to_cable_km"),
            }
            for p in alert_points
        ],
    }

    Path("ethera").mkdir(parents=True, exist_ok=True)
    (Path("ethera") / "ethera-alerts.json").write_text(json.dumps(alerts, indent=2))
    print(f"  Wrote ethera/ethera-alerts.json ({len(alert_points)} high-score points)")


def main():
    import argparse
    p = argparse.ArgumentParser(description="Generate synthetic AIS data for watchfloor testing. Outputs to data/ethera/ and output/data/ethera/.")
    p.add_argument("scenario", nargs="?", help="Scenario: " + ", ".join(SCENARIOS.keys()))
    p.add_argument("--all", action="store_true", help="Run all scenarios (last one wins)")
    p.add_argument("--analyze", action="store_true", help="Generate alerts from real ethera + OSM data")
    args = p.parse_args()

    if args.analyze:
        run_real_data_analysis()
        return
    if args.all:
        for name in SCENARIOS:
            run_scenario(name, None)
    elif args.scenario:
        run_scenario(args.scenario, None)
    else:
        p.print_help()
        print("\nScenarios:", list(SCENARIOS.keys()))


if __name__ == "__main__":
    main()
