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
  python synthetic_data_engine.py loitering -o synthetic_output
  python synthetic_data_engine.py --all -o synthetic_output
"""

import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

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


def to_ethera_formats(points, scenario_name, label):
    """Output in data/ethera/ format for frontend."""
    coords = [[p["lon"], p["lat"]] for p in points]
    line_geojson = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": {"scenario": scenario_name, "label": label}}]
    }
    points_geojson = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]}, "properties": {
                "speed": p.get("speed_knots"), "heading": p.get("cog"), "timestamp": _ts_str(p["timestamp"]),
                "score": p.get("score", 0), "alert_level": p.get("alert_level", ""), "reasons": p.get("score_reasons", []),
            }}
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

    # Output to data/ethera/ and output/data/ethera/
    for base in [Path("data/ethera"), Path("output/data/ethera")]:
        base.mkdir(parents=True, exist_ok=True)
        line_geo, points_geo = to_ethera_formats(points, name, label)
        (base / "ethera.line.geojson").write_text(json.dumps(line_geo, indent=2))
        (base / "ethera.points.geojson").write_text(json.dumps(points_geo, indent=2))
        (base / "ethera-alerts.json").write_text(json.dumps(alerts, indent=2))

    # Also write mst-compatible and track.geojson to data/ethera
    data_ethera = Path("data/ethera")
    (data_ethera / "history_parsed.json").write_text(json.dumps(to_mst_history(points), indent=2))
    (data_ethera / "details_normalized.json").write_text(json.dumps(to_mst_details(points), indent=2))
    (data_ethera / "track.geojson").write_text(json.dumps(to_geojson_track(points, {"scenario": name, "label": label}), indent=2))

    print(f"  Wrote to data/ethera/ and output/data/ethera/")


def main():
    import argparse
    p = argparse.ArgumentParser(description="Generate synthetic AIS data for watchfloor testing. Outputs to data/ethera/ and output/data/ethera/.")
    p.add_argument("scenario", nargs="?", help="Scenario: " + ", ".join(SCENARIOS.keys()))
    p.add_argument("--all", action="store_true", help="Run all scenarios (last one wins)")
    args = p.parse_args()

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
