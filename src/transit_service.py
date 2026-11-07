"""Shared saved transit locations and distinct station symbols for every map."""
from pathlib import Path
import math
import re
from typing import Any, Dict
import pandas as pd

STATION_ASSETS = Path(__file__).with_name("map_assets")

STATION_ICONS = {
    'metro': '<svg viewBox="0 0 32 32" width="32" height="32" aria-hidden="true"><circle cx="16" cy="16" r="11" fill="white" stroke="#d21f32" stroke-width="5"/><rect x="1" y="12" width="30" height="8" rx="1" fill="#154b99"/></svg>',
    'rail': '<svg viewBox="0 0 32 32" width="32" height="32" aria-hidden="true"><rect x="1" y="1" width="30" height="30" rx="6" fill="#1456a0"/><rect x="8" y="5" width="16" height="20" rx="4" fill="white"/><path d="M10 9h5v6h-5zm7 0h5v6h-5z" fill="#1456a0"/><circle cx="12" cy="21" r="1.5" fill="#1456a0"/><circle cx="20" cy="21" r="1.5" fill="#1456a0"/><path d="m11 25-3 4m13-4 3 4" stroke="white" stroke-width="2"/></svg>',
}

def saved_london_stations() -> Dict[str, Any]:
    """Bundle the existing London overlays so station display needs no service."""
    directory = Path(__file__).resolve().parent.parent / "overlays"
    elements = []
    for filename, metro in (("LondonTubeStops.csv", True), ("LondonRailStations.csv", False)):
        path = directory / filename
        if not path.exists():
            continue
        try:
            frame = pd.read_csv(path)
        except (OSError, ValueError):
            continue
        for _, row in frame.iterrows():
            try:
                lat, lon = float(row['Latitude']), float(row['Longitude'])
                if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                name = str(row['Station']).strip()
                if not name or name.lower() == 'nan':
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            # Older Tube CSV uses CamelCase; rail CSV suffixes are footnotes.
            if metro:
                name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
                name = re.sub(r'(?<=[A-Za-z])(?=[0-9])', ' ', name)
            else:
                name = re.sub(r'\d+$', '', name).strip()
            elements.append({'lat': lat, 'lon': lon, 'tags': {
                'name': name, 'railway': 'station',
                'station': 'subway' if metro else 'rail',
                'network': 'Saved London Tube / DLR data' if metro else 'Saved London rail data',
            }})
    return {'bounds': [51.25, -0.65, 51.8, 0.35], 'elements': elements,
            'source': 'Saved London station data; recent changes may be missing', 'icons': STATION_ICONS}

