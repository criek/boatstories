#!/usr/bin/env python3
"""
Best-effort MyShipTracking fetcher using only internal site endpoints.

What it does
------------
- Fetches current vessel details from:
  /requests/vesseldetailsTEST.php
- Fetches raw vessel history from:
  /requests/vessel_history2.php
- Optionally fetches nearby map data from:
  /requests/vesselsonmaptempTTT.php
- Optionally picks the nearest historical point to a requested timestamp

Notes
-----
- No API key required.
- The internal endpoints are undocumented and may change.
- Historical parsing is best-effort because the history endpoint format is not stable.
- Works with MMSI or IMO for the details call.
- If you pass IMO and history fails, the script tries to discover the MMSI from details and retries.

Examples
--------
python myshiptracking_fetch.py 255916493
python myshiptracking_fetch.py 255916493 --timestamp 2026-03-10T12:30:00Z
python myshiptracking_fetch.py 9215660 --id-field imo
python myshiptracking_fetch.py 255916493 --include-map
python myshiptracking_fetch.py 255916493 --stdout-only
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


BASE_URL = "https://www.myshiptracking.com/requests"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://www.myshiptracking.com/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def save_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def try_json_loads(text: str) -> Optional[Any]:
    text = text.strip("\ufeff \n\r\t")
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def flatten_value_field(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Converts keys like:
      "cval-sog": {"V": "8.6 Knots"}
    into:
      "cval_sog": "8.6 Knots"
    """
    out: Dict[str, Any] = {}

    for key, value in data.items():
        if isinstance(value, dict) and "V" in value and len(value) <= 3:
            out[key.replace("-", "_")] = value.get("V")
        else:
            out[key.replace("-", "_")] = value

    return out


def maybe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def maybe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None


def parse_datetime_any(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        # Heuristic: milliseconds vs seconds
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except Exception:
            return None

    s = str(value).strip()
    if not s:
        return None

    # Pure epoch string
    if re.fullmatch(r"\d{10,13}", s):
        try:
            v = float(s)
            if len(s) == 13:
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except Exception:
            pass

    # Normalize Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    # ISO-ish
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue

    return None


def extract_known_identifiers(obj: Any) -> Dict[str, Optional[str]]:
    """
    Recursively searches for MMSI/IMO-like identifiers in the details response.
    """
    found = {"mmsi": None, "imo": None}

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower()
                if lk == "mmsi" and found["mmsi"] is None:
                    iv = maybe_int(v)
                    if iv:
                        found["mmsi"] = str(iv)
                elif lk == "imo" and found["imo"] is None:
                    iv = maybe_int(v)
                    if iv:
                        found["imo"] = str(iv)
                else:
                    walk(v)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)
    return found


def fetch_details(
    session: requests.Session,
    identifier: str,
    id_field: str = "mmsi",
) -> Tuple[Dict[str, Any], str]:
    url = f"{BASE_URL}/vesseldetailsTEST.php"
    params = {
        "type": "json",
        "return": "json",
        "lang": "",
        id_field: identifier,
    }
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()

    data = try_json_loads(r.text)
    if not isinstance(data, dict):
        raise RuntimeError("Details endpoint did not return parseable JSON.")
    return data, r.text


def normalize_details(raw: Dict[str, Any], requested_id: str, requested_field: str) -> Dict[str, Any]:
    flat = flatten_value_field(raw)
    f = raw.get("F", {}) if isinstance(raw.get("F"), dict) else {}
    follow = raw.get("follow_info", {}) if isinstance(raw.get("follow_info"), dict) else {}
    ids = extract_known_identifiers(raw)

    current_ts = parse_datetime_any(f.get("T"))

    normalized = {
        "requested_id": requested_id,
        "requested_id_field": requested_field,
        "resolved_mmsi": ids.get("mmsi"),
        "resolved_imo": ids.get("imo"),
        "lat": maybe_float(f.get("LAT")),
        "lon": maybe_float(f.get("LNG")),
        "speed_knots": maybe_float(f.get("S")),
        "current_timestamp": current_ts.isoformat() if current_ts else None,
        "current_timestamp_raw": f.get("T"),
        "destination": follow.get("dest"),
        "eta_raw": follow.get("next_time"),
        "details_flat": flat,
    }

    return normalized


def fetch_history(
    session: requests.Session,
    identifier: str,
    id_field: str = "mmsi",
    days: int = 0,
    from_value: str = "null",
    to_value: str = "null",
) -> str:
    url = f"{BASE_URL}/vessel_history2.php"
    params = {
        id_field: identifier,
        "days": days,
        "from": from_value,
        "to": to_value,
    }
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_map_box(
    session: requests.Session,
    minlat: float,
    maxlat: float,
    minlon: float,
    maxlon: float,
    selid: Optional[str] = None,
    seltype: int = 7,
    zoom: int = 11,
) -> str:
    url = f"{BASE_URL}/vesselsonmaptempTTT.php"
    filters = {
        "vtypes": ",0,3,4,6,7,8,9,10,11,12,13",
        "ports": "1",
        "minsog": 0,
        "maxsog": 60,
        "minsz": 0,
        "maxsz": 500,
        "minyr": 1950,
        "maxyr": 2030,
        "status": "",
        "mapflt_from": "",
        "mapflt_dest": "",
    }
    params = {
        "type": "json",
        "minlat": minlat,
        "maxlat": maxlat,
        "minlon": minlon,
        "maxlon": maxlon,
        "zoom": zoom,
        "selid": selid or "",
        "seltype": seltype,
        "timecode": -1,
        "filters": json.dumps(filters, separators=(",", ":")),
    }
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.text


def split_tokens(line: str) -> List[str]:
    if "\t" in line:
        parts = [p.strip() for p in line.split("\t")]
    elif ";" in line:
        parts = [p.strip() for p in line.split(";")]
    else:
        parts = [p.strip() for p in line.split(",")]
    return [p for p in parts if p != ""]


def find_timestamp_in_tokens(tokens: List[str]) -> Tuple[Optional[str], Optional[datetime]]:
    # Direct token match
    for token in tokens:
        dt = parse_datetime_any(token)
        if dt:
            return token, dt

    # Adjacent date + time pair
    for i in range(len(tokens) - 1):
        combined = f"{tokens[i]} {tokens[i+1]}"
        dt = parse_datetime_any(combined)
        if dt:
            return combined, dt

    return None, None


def find_lat_lon_in_tokens(tokens: List[str]) -> Tuple[Optional[float], Optional[float]]:
    numeric_candidates: List[Tuple[str, float]] = []
    for t in tokens:
        v = maybe_float(t)
        if v is not None:
            numeric_candidates.append((t, v))

    for i in range(len(numeric_candidates) - 1):
        a = numeric_candidates[i][1]
        b = numeric_candidates[i + 1][1]
        if -90 <= a <= 90 and -180 <= b <= 180:
            return a, b

    return None, None


def parse_history_text(raw_text: str) -> Dict[str, Any]:
    """
    Best-effort parser.
    Returns a structure with:
    - raw_format_guess
    - points[]
    - line_samples[]
    """
    text = raw_text.strip()
    result: Dict[str, Any] = {
        "raw_format_guess": "unknown",
        "points": [],
        "line_samples": [],
    }

    # 1) If it is JSON, try to normalize common shapes.
    j = try_json_loads(text)
    if j is not None:
        result["raw_format_guess"] = "json"
        points = normalize_points_from_json(j)
        result["points"] = points
        return result

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result["line_samples"] = lines[:10]

    if not lines:
        result["raw_format_guess"] = "empty"
        return result

    # Guess delimiter
    if any("\t" in ln for ln in lines[:5]):
        result["raw_format_guess"] = "tabular_text"
    elif any(";" in ln for ln in lines[:5]):
        result["raw_format_guess"] = "semicolon_text"
    elif any("," in ln for ln in lines[:5]):
        result["raw_format_guess"] = "comma_text"

    points: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines):
        tokens = split_tokens(line)
        if len(tokens) < 2:
            continue

        _, dt = find_timestamp_in_tokens(tokens)
        lat, lon = find_lat_lon_in_tokens(tokens)

        if lat is None or lon is None:
            continue

        point: Dict[str, Any] = {
            "line_index": idx,
            "raw_line": line,
            "lat": lat,
            "lon": lon,
        }

        if dt:
            point["timestamp"] = dt.isoformat()

        # Try to find speed/course from remaining numeric-ish tokens
        numeric_vals = [maybe_float(t) for t in tokens]
        numeric_vals = [v for v in numeric_vals if v is not None]

        if len(numeric_vals) >= 4:
            # Soft guess only; these may be wrong for some lines
            point.setdefault("speed_knots_guess", numeric_vals[2])
            point.setdefault("course_deg_guess", numeric_vals[3])

        points.append(point)

    result["points"] = dedupe_points(points)
    return result


def normalize_points_from_json(obj: Any) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []

    def add_point(lat: Any, lon: Any, t: Any, extra: Optional[Dict[str, Any]] = None) -> None:
        plat = maybe_float(lat)
        plon = maybe_float(lon)
        if plat is None or plon is None:
            return
        point: Dict[str, Any] = {"lat": plat, "lon": plon}
        dt = parse_datetime_any(t)
        if dt:
            point["timestamp"] = dt.isoformat()
        elif t is not None:
            point["timestamp_raw"] = str(t)
        if extra:
            point.update(extra)
        points.append(point)

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            keys = {str(k).lower(): k for k in x.keys()}

            lat_key = next((keys[k] for k in keys if k in {"lat", "latitude"}), None)
            lon_key = next((keys[k] for k in keys if k in {"lon", "lng", "longitude"}), None)
            time_key = next((keys[k] for k in keys if k in {"time", "timestamp", "t", "date"}), None)

            if lat_key and lon_key:
                extra = {}
                for speed_key in ("speed", "sog", "knots"):
                    if speed_key in keys:
                        extra["speed_knots"] = maybe_float(x[keys[speed_key]])
                        break
                for course_key in ("course", "cog", "heading"):
                    if course_key in keys:
                        extra["course_deg"] = maybe_float(x[keys[course_key]])
                        break
                add_point(
                    x.get(lat_key),
                    x.get(lon_key),
                    x.get(time_key) if time_key else None,
                    extra=extra,
                )

            for v in x.values():
                walk(v)

        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)
    return dedupe_points(points)


def dedupe_points(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []

    for p in points:
        key = (
            round(float(p["lat"]), 6),
            round(float(p["lon"]), 6),
            p.get("timestamp"),
            p.get("timestamp_raw"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(p)

    return out


def choose_nearest_snapshot(
    normalized_details: Dict[str, Any],
    parsed_history: Dict[str, Any],
    target_timestamp: str,
) -> Dict[str, Any]:
    target_dt = parse_datetime_any(target_timestamp)
    if not target_dt:
        raise ValueError(f"Could not parse timestamp: {target_timestamp}")

    points = parsed_history.get("points", []) if isinstance(parsed_history, dict) else []
    candidates = []

    for p in points:
        pdt = parse_datetime_any(p.get("timestamp") or p.get("timestamp_raw"))
        if not pdt:
            continue
        delta = abs((pdt - target_dt).total_seconds())
        candidates.append((delta, pdt, p))

    if candidates:
        candidates.sort(key=lambda x: x[0])
        delta, pdt, best = candidates[0]
        return {
            "mode": "history_nearest_point",
            "requested_timestamp": target_dt.isoformat(),
            "matched_timestamp": pdt.isoformat(),
            "difference_seconds": delta,
            "point": best,
        }

    # Fallback to current details if no parseable historical points exist
    current_dt = parse_datetime_any(normalized_details.get("current_timestamp"))
    current_delta = None
    if current_dt:
        current_delta = abs((current_dt - target_dt).total_seconds())

    return {
        "mode": "current_details_fallback",
        "requested_timestamp": target_dt.isoformat(),
        "matched_timestamp": current_dt.isoformat() if current_dt else None,
        "difference_seconds": current_delta,
        "point": {
            "lat": normalized_details.get("lat"),
            "lon": normalized_details.get("lon"),
            "speed_knots": normalized_details.get("speed_knots"),
        },
        "warning": (
            "No parseable historical points were found in vessel_history2.php. "
            "Returned the current details snapshot instead."
        ),
    }


def build_bbox(lat: float, lon: float, padding: float = 0.15) -> Tuple[float, float, float, float]:
    return lat - padding, lat + padding, lon - padding, lon + padding


def parse_map_text(raw_text: str) -> Dict[str, Any]:
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    result = {
        "line_count": len(lines),
        "header": lines[:2],
        "ports": [],
        "objects": [],
        "samples": lines[:10],
    }

    for line in lines[2:]:
        parts = split_tokens(line)
        if not parts:
            continue

        rec_type = parts[0]
        if rec_type == "1" and len(parts) >= 5:
            result["ports"].append(
                {
                    "type": "port",
                    "port_id": parts[1],
                    "name": parts[2],
                    "lat": maybe_float(parts[3]),
                    "lon": maybe_float(parts[4]),
                    "raw": parts,
                }
            )
        else:
            result["objects"].append(
                {
                    "record_type": rec_type,
                    "raw": parts,
                }
            )

    return result


def print_or_save(stdout_only: bool, out_dir: Path, filename: str, content: Any) -> None:
    if stdout_only:
        if isinstance(content, str):
            print(content)
        else:
            print(json.dumps(content, indent=2, ensure_ascii=False))
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename

    if isinstance(content, str):
        save_text(path, content)
    else:
        save_json(path, content)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch MyShipTracking internal endpoint data.")
    parser.add_argument("identifier", help="MMSI or IMO value")
    parser.add_argument(
        "--id-field",
        default="mmsi",
        choices=["mmsi", "imo"],
        help="Which identifier you are passing",
    )
    parser.add_argument(
        "--timestamp",
        help="Target timestamp for nearest snapshot, e.g. 2026-03-10T12:30:00Z",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="Value passed to vessel_history2.php (default: 0)",
    )
    parser.add_argument(
        "--from-value",
        default="null",
        help='Raw "from" value for vessel_history2.php (default: null)',
    )
    parser.add_argument(
        "--to-value",
        default="null",
        help='Raw "to" value for vessel_history2.php (default: null)',
    )
    parser.add_argument(
        "--include-map",
        action="store_true",
        help="Fetch surrounding map data using a box around the current position",
    )
    parser.add_argument(
        "--map-padding",
        type=float,
        default=0.15,
        help="Latitude/longitude padding for the map box (default: 0.15)",
    )
    parser.add_argument(
        "--out-dir",
        default="mst_output",
        help="Output directory (default: mst_output)",
    )
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Print results instead of writing files",
    )

    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    session = make_session()

    manifest: Dict[str, Any] = {
        "requested_identifier": args.identifier,
        "requested_id_field": args.id_field,
        "timestamp_requested": args.timestamp,
        "include_map": args.include_map,
        "outputs": {},
    }

    # DETAILS
    details_raw, details_raw_text = fetch_details(session, args.identifier, args.id_field)
    details_normalized = normalize_details(details_raw, args.identifier, args.id_field)

    print_or_save(args.stdout_only, out_dir, "details_raw.json", details_raw)
    print_or_save(args.stdout_only, out_dir, "details_normalized.json", details_normalized)
    manifest["outputs"]["details_raw"] = "details_raw.json"
    manifest["outputs"]["details_normalized"] = "details_normalized.json"

    # HISTORY
    history_id = args.identifier
    history_id_field = args.id_field

    history_raw_text = fetch_history(
        session,
        identifier=history_id,
        id_field=history_id_field,
        days=args.days,
        from_value=args.from_value,
        to_value=args.to_value,
    )

    # If user passed IMO and history looks empty/useless, try resolved MMSI from details
    if args.id_field == "imo":
        stripped = history_raw_text.strip()
        if (not stripped or stripped.lower() in {"[]", "{}", "null"}) and details_normalized.get("resolved_mmsi"):
            history_id = str(details_normalized["resolved_mmsi"])
            history_id_field = "mmsi"
            history_raw_text = fetch_history(
                session,
                identifier=history_id,
                id_field=history_id_field,
                days=args.days,
                from_value=args.from_value,
                to_value=args.to_value,
            )
            manifest["history_retry_with_mmsi"] = history_id

    history_parsed = parse_history_text(history_raw_text)

    print_or_save(args.stdout_only, out_dir, "history_raw.txt", history_raw_text)
    print_or_save(args.stdout_only, out_dir, "history_parsed.json", history_parsed)
    manifest["outputs"]["history_raw"] = "history_raw.txt"
    manifest["outputs"]["history_parsed"] = "history_parsed.json"

    # SNAPSHOT
    if args.timestamp:
        snapshot = choose_nearest_snapshot(details_normalized, history_parsed, args.timestamp)
        print_or_save(args.stdout_only, out_dir, "snapshot.json", snapshot)
        manifest["outputs"]["snapshot"] = "snapshot.json"

    # MAP
    if args.include_map:
        lat = details_normalized.get("lat")
        lon = details_normalized.get("lon")
        if lat is None or lon is None:
            map_error = {
                "error": "Could not determine current lat/lon from details response."
            }
            print_or_save(args.stdout_only, out_dir, "map_error.json", map_error)
            manifest["outputs"]["map_error"] = "map_error.json"
        else:
            minlat, maxlat, minlon, maxlon = build_bbox(float(lat), float(lon), padding=args.map_padding)
            selid = details_normalized.get("resolved_mmsi") or args.identifier
            map_raw_text = fetch_map_box(
                session,
                minlat=minlat,
                maxlat=maxlat,
                minlon=minlon,
                maxlon=maxlon,
                selid=str(selid),
            )
            map_parsed = parse_map_text(map_raw_text)

            print_or_save(args.stdout_only, out_dir, "map_raw.txt", map_raw_text)
            print_or_save(args.stdout_only, out_dir, "map_parsed.json", map_parsed)
            manifest["outputs"]["map_raw"] = "map_raw.txt"
            manifest["outputs"]["map_parsed"] = "map_parsed.json"

    # MANIFEST
    if not args.stdout_only:
        save_json(out_dir / "manifest.json", manifest)

    if args.stdout_only:
        # Friendly summary to stderr would pollute stdout, so skip it.
        pass
    else:
        print(f"Saved output to: {out_dir.resolve()}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        body = e.response.text[:500] if e.response is not None and e.response.text else ""
        print(f"HTTP error: {status}\n{body}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)