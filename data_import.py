import pandas as pd
from os import path

vessels = pd.read_csv(path.join("data", "Vessels.csv"), sep=";")
radar_pos = pd.read_csv(path.join("data","AIS_RADAR_streams202260306", "radar_positionrecord_202603061623.csv"))
vessel_ann = pd.read_csv(path.join("data","AIS_RADAR_streams202260306", "vessel_annotatedpositionrecord_202603061619.csv"))

print(f"vessels: {len(vessels)}, radar_pos: {len(radar_pos)}, vessel_ann: {len(vessel_ann)}")

radar_pos[["lng", "lat"]] = radar_pos["position"].str.extract(r"POINT \(([^ ]+) ([^ ]+)\)").astype(float)
vessel_ann[["lng", "lat"]] = vessel_ann["position"].str.extract(r"POINT \(([^ ]+) ([^ ]+)\)").astype(float)

print(vessel_ann)