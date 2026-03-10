# Data format for Boatstories map (POC)

Place your data in `data.json` next to `map.html`. The page loads it on startup. If the file is missing or invalid, the embedded demo is shown.

**Custom file:** `map.html?data=track.geojson` loads from a different file in the same folder.

**Data sources (overlay layers):** Add `dataSources` to load infrastructure and ship tracks:
```json
"dataSources": {
  "infrastructure": "../data/openstreetmapdata-export.geojson",
  "etheraLine": "../data/ethera/ethera.line.geojson",
  "etheraPoints": "../data/ethera/ethera.points.geojson"
}
```
Serve from project root: `cd boatstories && python -m http.server 8765`, then open `http://localhost:8765/output/map.html`.

## Option A: Full config (recommended)

```json
{
  "title": "Vessel Name — Transit Log",
  "reference": "REF-XXX-2024-0001",
  "eventCount": 4,
  "report": {
    "sections": [
      {
        "title": "// voyage summary",
        "paragraphs": ["First paragraph...", "Second paragraph..."]
      },
      {
        "title": "// cable crossing events",
        "paragraphs": [
          "At 0312 UTC, [event-id] was recorded...",
          "Use [event-id] to link to map markers. IDs must match Point features in geojson."
        ]
      }
    ]
  },
  "geojson": {
    "type": "FeatureCollection",
    "features": [...]
  }
}
```

## Option B: Map only (raw GeoJSON)

```json
{
  "type": "FeatureCollection",
  "features": [...]
}
```

## GeoJSON features

- **LineString** — vessel tracks, cable routes. Use `properties: { "label": "Track name", "status": "nominal" }`.
- **Point** — event markers. Use `properties`:
  - `id` — unique id, used for `[id]` links in report text
  - `name` — display name
  - `status` — `"priority"` (red) | `"moderate"` (amber) | `"nominal"` (green)
  - `icon` — `"warn"` | `"info"` | `"check"`
  - `desc` — popup description

Coordinates: `[longitude, latitude]` (GeoJSON standard).
