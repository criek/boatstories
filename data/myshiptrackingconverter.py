#!/usr/bin/env python3
"""
Convert GPS track files to GeoJSON.

Expected line format (skips anything that doesn't match):
  lat,lon,timestamp,,speed,heading,,hdop,satellites,altitude
  e.g. 51.40178,3.26645,1773135655,,3.1,91,,8.4,8,216

Outputs:
  <input>.points.geojson  — one Point feature per fix, with all fields as properties
  <input>.line.geojson    — a single LineString feature connecting all fixes
"""

import sys
import re
import json
from pathlib import Path
from datetime import datetime, timezone

# Matches a line that starts with two floats (lat, lon) followed by an integer timestamp
ROW_RE = re.compile(
    r"^\s*"
    r"(?P<lat>-?\d+\.\d+)"      # latitude
    r","
    r"(?P<lon>-?\d+\.\d+)"      # longitude
    r","
    r"(?P<ts>\d+)"               # unix timestamp
    r","
    r"(?P<rest>.*)"              # remaining fields
    r"\s*$"
)

FIELD_NAMES = ["speed", "heading", "hdop", "satellites", "altitude"]


def parse_value(s):
    """Return int, float, or string (or None for empty)."""
    s = s.strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def parse_file(path):
    features_points = []
    coordinates = []

    with open(path, "r", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            m = ROW_RE.match(raw)
            if not m:
                continue  # skip header / unrecognised lines

            lat = float(m.group("lat"))
            lon = float(m.group("lon"))
            ts  = int(m.group("ts"))

            # Parse remaining optional columns
            extra = [parse_value(v) for v in m.group("rest").split(",")]
            # Pad / trim to exactly len(FIELD_NAMES) entries
            extra += [None] * max(0, len(FIELD_NAMES) - len(extra))
            props = {name: extra[i] for i, name in enumerate(FIELD_NAMES)}

            # Add timestamp in both raw and ISO-8601 form
            props["timestamp"] = ts
            props["datetime"]  = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            props["_line"]     = lineno

            coordinates.append([lon, lat])

            features_points.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            })

    return features_points, coordinates


def write_geojson(obj, dest):
    with open(dest, "w") as fh:
        json.dump(obj, fh, indent=2)
    print(f"  wrote {dest}  ({len(json.dumps(obj))//1024 + 1} KB)")


def main():
    if len(sys.argv) < 2:
        print("Usage: python gpx_to_geojson.py <input_file> [output_stem]")
        sys.exit(1)

    src  = Path(sys.argv[1])
    stem = Path(sys.argv[2]) if len(sys.argv) > 2 else src

    print(f"Parsing {src} …")
    features_points, coordinates = parse_file(src)

    if not features_points:
        print("No matching lines found. Check that your file contains lines like:\n"
              "  51.40178,3.26645,1773135655,,3.1,91,,8.4,8,216")
        sys.exit(1)

    print(f"Found {len(features_points)} fixes.")

    # ── Points GeoJSON ──────────────────────────────────────────────────────
    points_fc = {
        "type": "FeatureCollection",
        "features": features_points,
    }
    write_geojson(points_fc, f"{stem}.points.geojson")

    # ── Line GeoJSON ────────────────────────────────────────────────────────
    first_ts = features_points[0]["properties"]["datetime"]
    last_ts  = features_points[-1]["properties"]["datetime"]

    line_fc = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coordinates},
            "properties": {
                "point_count": len(coordinates),
                "start":       first_ts,
                "end":         last_ts,
            },
        }],
    }
    write_geojson(line_fc, f"{stem}.line.geojson")

    print("Done.")


if __name__ == "__main__":
    main()