import pandas as pd
from os import path

# 'Name', 'Vessel Sanction Indicators', 'IMO', 'Flag', 'LLI Vessel Type', 'Vessel Type and Size', 'Built', 'Status', 'Beneficial Owner',
# 'Beneficial Owner Country/Region', 'Beneficial Owner Sanction Indicators', 'Beneficial Owner - Shareholders & UBO Sanctions',
# 'Commercial Operator', 'Commercial Operator Country/Region', 'Commercial Operator Sanction Indicators', 'Commercial Operator - Shareholders & UBO Sanctions',
# 'Registered Owner', 'Registered Owner Country/Region', 'Registered Owner Sanction Indicators', 'Registered Owner - Shareholders & UBO Sanctions', 'Technical Manager',
# 'Technical Manager Country/Region', 'Technical Manager Sanction Indicators', 'Technical Manager - Shareholders & UBO Sanctions', 'ISM Manager',
# 'ISM Manager Country/Region', 'ISM Manager Sanction Indicators', 'ISM Manager - Shareholders & UBO Sanctions', 'Third Party Operator', 'Third Party Operator Country/Region',
# 'Third Party Operator Sanction Indicators', 'Third Party Operator - Shareholders & UBO Sanctions', 'Length Overall (m)', 'Depth (m)', 'Maximum Draught (m)', 'Freeboard (m)'
vessels = pd.read_csv(path.join("data", "Vessels.csv"), sep=";")

# 'subjectid', 'timestamp', 'position', 'cog', 'sog', 'trueheading', 'navstatus', 'lng', 'lat'
radar_pos = pd.read_csv(path.join("data","AIS_RADAR_streams202260306", "radar_positionrecord_202603061623.csv"))

# 'vesselid', 'timestamp', 'position', 'cog', 'sog', 'trueheading', 'navstatus', 'lng', 'lat'
vessel_ann = pd.read_csv(path.join("data","AIS_RADAR_streams202260306", "vessel_annotatedpositionrecord_202603061619.csv"))

print(f"vessels: {len(vessels)}, radar_pos: {len(radar_pos)}, vessel_ann: {len(vessel_ann)}")

radar_pos[["lng", "lat"]] = radar_pos["position"].str.extract(r"POINT \(([^ ]+) ([^ ]+)\)").astype(float)
vessel_ann[["lng", "lat"]] = vessel_ann["position"].str.extract(r"POINT \(([^ ]+) ([^ ]+)\)").astype(float)

# tracks_pos = radar_pos.sort_values("timestamp").groupby("subjectid")[["timestamp", "lng", "lat", "sog", "cog"]].apply(lambda x: x.reset_index(drop=True))
# tracks_ann = vessel_ann.sort_values("timestamp").groupby("vesselid")[["timestamp", "lng", "lat", "sog", "cog"]].apply(lambda x: x.reset_index(drop=True))

print("radar_pos", radar_pos["subjectid"].nunique(), radar_pos["timestamp"].min(), "->", radar_pos["timestamp"].max())
print("vessel_ann", vessel_ann["vesselid"].nunique(), vessel_ann["timestamp"].min(), "->", vessel_ann["timestamp"].max())