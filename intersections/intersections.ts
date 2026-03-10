import * as fs from "fs";
import * as turf from "@turf/turf";
import {
  Feature,
  FeatureCollection,
  LineString,
  MultiLineString,
  Geometry,
  GeoJsonProperties,
} from "geojson";

// Buffer distance in nautical miles
const BUFFER_DISTANCE_NM = 0.1;
// Turf uses kilometres by default; 1 nautical mile = 1.852 km
const BUFFER_DISTANCE_KM = BUFFER_DISTANCE_NM * 1.852;

/**
 * Buffers both the input line and every feature in the comparison collection
 * by the given distance (km), intersects the two buffer polygons to find the
 * overlapping area, and returns a FeatureCollection containing:
 *
 *   - The original intersecting feature  (_role: "intersected_feature")
 *   - A Point at the centroid of the buffer-overlap polygon  (_role: "intersection_centroid")
 *
 * @param inputLine    - A GeoJSON LineString / MultiLineString feature
 * @param compareFC    - A FeatureCollection of features to test
 * @param bufferDistKm - Buffer radius in kilometres applied to both sides
 */
function findIntersections(
  inputLine: Feature<LineString | MultiLineString>,
  compareFC: FeatureCollection,
  bufferDistKm: number = BUFFER_DISTANCE_KM
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

      // Quick boolean check first (avoids expensive intersect call on misses)
      if (!turf.booleanIntersects(lineBuffer, featureBuffer)) {
        return;
      }

      // Compute the actual polygon overlap between the two buffers
      const overlap = turf.intersect(
        turf.featureCollection([lineBuffer, featureBuffer])
      );

      if (!overlap) {
        // Buffers touch only at an edge — no area overlap
        return;
      }

      // Centroid of the overlap polygon
      const centroid = turf.centroid(overlap, {
        properties: {
          _role: "intersection_centroid",
          _featureIndex: index,
          _featureType: geomType,
          // Mirror the original feature's properties for easy cross-reference
          ...((feature.properties ?? {}) as Record<string, unknown>),
        },
      });

      // Original feature — preserve all existing properties, add metadata
      const annotatedFeature: Feature<Geometry, GeoJsonProperties> = {
        ...feature,
        properties: {
          ...((feature.properties ?? {}) as Record<string, unknown>),
          _role: "intersected_feature",
          _featureIndex: index,
          _featureType: geomType,
        },
      };

      outputFeatures.push(annotatedFeature, centroid);

      console.log(
        `  [${index}] ${geomType} intersects — centroid at [${centroid.geometry.coordinates.map((c: number) => c.toFixed(6)).join(", ")}]`
      );
    } catch (err) {
      console.warn(`Error processing feature at index ${index} (${geomType}):`, err);
    }
  });

  return turf.featureCollection(outputFeatures);
}

/**
 * Main entry point.
 *
 * Usage:
 *   npx ts-node intersections.ts <input-line.geojson> <compare-features.geojson> [output.geojson]
 *
 * File 1: GeoJSON Feature (LineString / MultiLineString) or a FeatureCollection
 *         whose first feature is a line.
 * File 2: GeoJSON FeatureCollection of Lines, Points, and/or Polygons.
 * File 3: Optional output path (default: intersection-results.geojson)
 *
 * Output FeatureCollection contains pairs of features for each hit:
 *   - The original intersecting feature  (_role: "intersected_feature")
 *   - A centroid Point of the buffer overlap  (_role: "intersection_centroid")
 */
function main() {
  const [, , lineFilePath, compareFilePath, outputFilePath = "intersection-results.geojson"] =
    process.argv;

  if (!lineFilePath || !compareFilePath) {
    console.error(
      "Usage: npx ts-node intersections.ts <input-line.geojson> <compare-features.geojson> [output.geojson]"
    );
    process.exit(1);
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

  console.log(`Input line         : ${lineFilePath}`);
  console.log(`Comparison features: ${compareFilePath} (${compareFC.features.length} features)`);
  console.log(`Buffer distance    : ${BUFFER_DISTANCE_NM} NM (${BUFFER_DISTANCE_KM.toFixed(4)} km)`);
  console.log(`Output             : ${outputFilePath}\n`);

  // --- Run intersection analysis ---
  const resultFC = findIntersections(inputLine, compareFC);

  const intersectedCount = resultFC.features.filter(
    (f) => f.properties?._role === "intersected_feature"
  ).length;

  if (intersectedCount === 0) {
    console.log("\nNo intersections found within the buffer.");
  } else {
    console.log(`\nFound ${intersectedCount} intersecting feature(s).`);
  }

  // --- Write GeoJSON output ---
  fs.writeFileSync(outputFilePath, JSON.stringify(resultFC, null, 2), "utf-8");
  console.log(`Results saved to ${outputFilePath}`);
}

console.log("starting parse")
main();