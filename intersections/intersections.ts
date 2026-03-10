import * as fs from "fs";
import * as turf from "@turf/turf";
import {
  Feature,
  FeatureCollection,
  LineString,
  MultiLineString,
  MultiPolygon,
  Point,
  Polygon,
  Geometry,
  GeoJsonProperties,
  Position,
} from "geojson";

// Buffer distance in nautical miles
const BUFFER_DISTANCE_NM = 0.1;
// Turf uses kilometres; 1 nautical mile = 1.852 km
const BUFFER_DISTANCE_KM = BUFFER_DISTANCE_NM * 1.852;

// ─── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Explode a Polygon or MultiPolygon overlap result into individual Polygon
 * features so that each disjoint overlap zone gets its own centroid / hit.
 */
function explodeOverlapPolygons(
  overlap: Feature<Polygon | MultiPolygon>
): Feature<Polygon>[] {
  if (overlap.geometry.type === "Polygon") {
    return [overlap as Feature<Polygon>];
  }
  return (overlap.geometry as MultiPolygon).coordinates.map(
    (coords): Feature<Polygon> =>
      turf.polygon(coords, overlap.properties ?? {})
  );
}

/**
 * Keys whose values are considered temporal and should be copied to the
 * intersection centroid. Matched case-insensitively against property names.
 */
const TEMPORAL_KEY_PATTERN = /time|timestamp|date|datetime/i;
const TEMPORAL_ALIASES = ["t", "ts"];

/**
 * Return ALL temporal field names found in a properties object.
 */
function detectTemporalKeys(props: Record<string, unknown>): string[] {
  const matched = Object.keys(props).filter((k) => TEMPORAL_KEY_PATTERN.test(k));
  const aliases = TEMPORAL_ALIASES.filter((a) => a in props && !matched.includes(a));
  return [...matched, ...aliases];
}

/**
 * Given a centroid Point and a FeatureCollection of timestamped Points, find
 * the nearest point and return ALL its temporal field values plus distanceKm.
 * Returns null if the collection is empty.
 */
function lookupNearestTemporalFields(
  centroid: Feature<Point>,
  timedPoints: FeatureCollection<Point>
): { fields: Record<string, unknown>; distanceKm: number } | null {
  if (timedPoints.features.length === 0) return null;

  const nearest = turf.nearestPoint(centroid, timedPoints);
  if (!nearest?.properties) return null;

  const props = nearest.properties as Record<string, unknown>;
  const keys = detectTemporalKeys(props);

  const fields: Record<string, unknown> = {};
  for (const key of keys) {
    fields[key] = props[key];
  }

  const distanceKm = (props.distanceToPoint as number) ?? 0;
  return { fields, distanceKm };
}


// ─── Core analysis ────────────────────────────────────────────────────────────

/**
 * Buffers both the input line and every feature in the comparison collection
 * by the given distance (km), intersects the two buffer polygons, then:
 *
 *   - Splits any MultiPolygon result into individual Polygon zones
 *   - Emits one "hit" per zone, each consisting of:
 *       • the original intersecting feature  (_role: "intersected_feature")
 *       • a centroid Point for that specific zone  (_role: "intersection_centroid")
 *
 * If timedPoints is provided, each centroid also receives:
 *   _timestamp    – value from the nearest timed point's timestamp field
 *   _nearestDistKm – distance in km to that nearest timed point
 *
 * All output features carry:
 *   _role          – "intersected_feature" | "intersection_centroid"
 *   _featureIndex  – index in the comparison FeatureCollection
 *   _featureType   – original geometry type string
 *   _hitIndex      – 0-based zone index within this feature's overlap
 *   _hitCount      – total disjoint overlap zones for this feature
 *
 * @param inputLine    - GeoJSON LineString / MultiLineString feature
 * @param compareFC    - FeatureCollection of features to test
 * @param bufferDistKm - Buffer radius in kilometres applied to both sides
 * @param timedPoints  - Optional FeatureCollection<Point> with timestamp properties
 */
function findIntersections(
  inputLine: Feature<LineString | MultiLineString>,
  compareFC: FeatureCollection,
  bufferDistKm: number = BUFFER_DISTANCE_KM,
  timedPoints: FeatureCollection<Point> | null = null
): FeatureCollection {
  // 1. Buffer the input line into a corridor polygon
  const lineBuffer = turf.buffer(inputLine, bufferDistKm, { units: "kilometers" });
  if (!lineBuffer) {
    throw new Error("Failed to create buffer around the input line.");
  }

  const outputFeatures: Feature<Geometry, GeoJsonProperties>[] = [];

  // 2. Process each comparison feature
  compareFC.features.forEach((feature, index) => {
    const geomType = feature.geometry?.type ?? "Unknown";

    try {
      // Buffer the comparison feature
      const featureBuffer = turf.buffer(feature, bufferDistKm, { units: "kilometers" });
      if (!featureBuffer) {
        console.warn(`Could not buffer feature at index ${index} (${geomType}), skipping.`);
        return;
      }

      // Quick boolean check (avoids expensive intersect on misses)
      if (!turf.booleanIntersects(lineBuffer, featureBuffer)) {
        return;
      }

      // Compute actual polygon overlap
      const overlap = turf.intersect(
        turf.featureCollection([lineBuffer, featureBuffer])
      );
      if (!overlap) {
        // Buffers only touch at an edge — no area overlap
        return;
      }

      // Split MultiPolygon results into individual zones
      const zones = explodeOverlapPolygons(overlap as Feature<Polygon | MultiPolygon>);
      const hitCount = zones.length;

      const sharedProps = {
        ...((feature.properties ?? {}) as Record<string, unknown>),
        _featureIndex: index,
        _featureType: geomType,
        _hitCount: hitCount,
      };

      zones.forEach((zone, hitIndex) => {
        // Centroid of this specific polygon zone
        const centroid = turf.centroid(zone) as Feature<Point>;
        const coords = centroid.geometry.coordinates as Position;

        // Look up the nearest timed point (if a track was provided)
        const trackInfo = timedPoints
          ? lookupNearestTemporalFields(centroid, timedPoints)
          : null;

        centroid.properties = {
          ...sharedProps,
          _role: "intersection_centroid",
          _hitIndex: hitIndex,
          ...(trackInfo !== null
            ? {
                ...trackInfo.fields,
                _nearestDistKm: parseFloat(trackInfo.distanceKm.toFixed(6)),
              }
            : {}),
        };

        // Emit the original feature once per zone so each hit is self-contained
        const annotatedFeature: Feature<Geometry, GeoJsonProperties> = {
          ...feature,
          properties: {
            ...sharedProps,
            _role: "intersected_feature",
            _hitIndex: hitIndex,
          },
        };

        outputFeatures.push(annotatedFeature, centroid);

        const tsLabel = trackInfo
          ? ` — ${Object.entries(trackInfo.fields).map(([k, v]) => `${k}: ${v}`).join(", ")}`
          : "";
        console.log(
          `  [${index}] ${geomType} — hit ${hitIndex + 1}/${hitCount}` +
          ` — centroid [${coords.map((c) => c.toFixed(6)).join(", ")}]${tsLabel}`
        );
      });
    } catch (err) {
      console.warn(`Error processing feature at index ${index} (${geomType}):`, err);
    }
  });

  return turf.featureCollection(outputFeatures);
}

// ─── Main ─────────────────────────────────────────────────────────────────────

/**
 * Usage:
 *   npx ts-node intersections.ts \
 *     <input-line.geojson> \
 *     <compare-features.geojson> \
 *     [output.geojson] \
 *     [--track <timed-points.geojson>]
 *
 * File 1: GeoJSON Feature (LineString / MultiLineString) or a FeatureCollection
 *         whose first feature is a line.
 * File 2: GeoJSON FeatureCollection of Lines, Points, and/or Polygons.
 * File 3: Optional output path (default: intersection-results.geojson)
 * --track: Optional path to a FeatureCollection<Point> where each point has a
 *          timestamp property (any key matching /time|timestamp|date|datetime/i,
 *          or short aliases "t" / "ts"). The nearest point to each intersection
 *          centroid is found and its timestamp is added as _timestamp.
 *
 * Output FeatureCollection — for each disjoint overlap zone:
 *   { _role: "intersected_feature" }   — original geometry with metadata
 *   { _role: "intersection_centroid" } — Point at the zone centre,
 *                                        optionally with _timestamp / _nearestDistKm
 */
function main() {
  const args = process.argv.slice(2);

  if (args.length < 2) {
    console.error(
      "Usage: npx ts-node intersections.ts " +
      "<input-line.geojson> <compare-features.geojson> " +
      "[output.geojson] [--track <timed-points.geojson>]"
    );
    process.exit(1);
  }

  // Parse positional and named arguments
  const lineFilePath = args[0];
  const compareFilePath = args[1];

  let outputFilePath = "intersection-results.geojson";
  let trackFilePath: string | null = null;

  for (let i = 2; i < args.length; i++) {
    if (args[i] === "--track" && args[i + 1]) {
      trackFilePath = args[++i];
    } else if (!args[i].startsWith("--")) {
      outputFilePath = args[i];
    }
  }

  // --- Load input line ---
  const rawLine = JSON.parse(fs.readFileSync(lineFilePath, "utf-8"));
  let inputLine: Feature<LineString | MultiLineString>;

  if (rawLine.type === "FeatureCollection") {
    const first = rawLine.features[0];
    if (!first || !["LineString", "MultiLineString"].includes(first.geometry?.type)) {
      console.error("The first feature in the FeatureCollection must be a LineString or MultiLineString.");
      process.exit(1);
    }
    inputLine = first as Feature<LineString | MultiLineString>;
  } else if (rawLine.type === "Feature") {
    if (!["LineString", "MultiLineString"].includes(rawLine.geometry?.type)) {
      console.error("The input GeoJSON feature must be a LineString or MultiLineString.");
      process.exit(1);
    }
    inputLine = rawLine as Feature<LineString | MultiLineString>;
  } else {
    console.error("Input line file must be a GeoJSON Feature or FeatureCollection.");
    process.exit(1);
  }

  // --- Load comparison features ---
  const rawCompare = JSON.parse(fs.readFileSync(compareFilePath, "utf-8"));
  let compareFC: FeatureCollection;

  if (rawCompare.type === "FeatureCollection") {
    compareFC = rawCompare as FeatureCollection;
  } else if (rawCompare.type === "Feature") {
    compareFC = turf.featureCollection([rawCompare]);
  } else {
    console.error("Comparison file must be a GeoJSON Feature or FeatureCollection.");
    process.exit(1);
  }

  // --- Load optional timed points ---
  let timedPoints: FeatureCollection<Point> | null = null;

  if (trackFilePath) {
    const rawTrack = JSON.parse(fs.readFileSync(trackFilePath, "utf-8"));
    if (rawTrack.type !== "FeatureCollection") {
      console.error("Track file must be a GeoJSON FeatureCollection of Points.");
      process.exit(1);
    }
    const pointFeatures = (rawTrack as FeatureCollection).features.filter(
      (f) => f.geometry?.type === "Point"
    ) as Feature<Point>[];

    if (pointFeatures.length === 0) {
      console.warn("Track file contains no Point features — timestamp lookup disabled.");
    } else {
      // Detect temporal keys from first feature as a sanity check
      const sampleProps = (pointFeatures[0].properties ?? {}) as Record<string, unknown>;
      const detectedKeys = detectTemporalKeys(sampleProps);
      if (detectedKeys.length === 0) {
        console.warn(
          "No temporal properties detected in track points " +
          `(checked keys: ${Object.keys(sampleProps).join(", ") || "none"}). ` +
          "Temporal field lookup will be skipped."
        );
      } else {
        console.log(`Track temporal fields detected: ${detectedKeys.map((k) => `"${k}"`).join(", ")}`);
      }
      timedPoints = turf.featureCollection(pointFeatures) as FeatureCollection<Point>;
    }
  }

  console.log(`Input line         : ${lineFilePath}`);
  console.log(`Comparison features: ${compareFilePath} (${compareFC.features.length} features)`);
  console.log(`Buffer distance    : ${BUFFER_DISTANCE_NM} NM (${BUFFER_DISTANCE_KM.toFixed(4)} km)`);
  if (timedPoints) {
    console.log(`Track points       : ${trackFilePath} (${timedPoints.features.length} points)`);
  }
  console.log(`Output             : ${outputFilePath}\n`);

  // --- Run intersection analysis ---
  const resultFC = findIntersections(inputLine, compareFC, BUFFER_DISTANCE_KM, timedPoints);

  const hitCount = resultFC.features.filter(
    (f) => f.properties?._role === "intersection_centroid"
  ).length;

  if (hitCount === 0) {
    console.log("\nNo intersections found within the buffer.");
  } else {
    const featureCount = new Set(
      resultFC.features
        .filter((f) => f.properties?._role === "intersection_centroid")
        .map((f) => f.properties?._featureIndex)
    ).size;
    console.log(`\nFound ${hitCount} hit(s) across ${featureCount} feature(s).`);
  }

  // --- Write GeoJSON output ---
  fs.writeFileSync(outputFilePath, JSON.stringify(resultFC, null, 2), "utf-8");
  console.log(`Results saved to ${outputFilePath}`);
}

main();