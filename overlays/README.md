# Station and place overlays

The maps can show saved station data independently of the background tiles.
See the [map user guide](../docs/user_guide.md#maps) for visible behavior.

## Bundled London stations

- `LondonTubeStops.csv`: saved London Tube/DLR locations.
- `LondonRailStations.csv`: saved London rail locations.

These two files are deliberately included in version control. They are existing
saved datasets, not a live feed, and may omit recent openings, closures or names.
The loader in `src/transit_service.py` normalizes the older CamelCase Tube names
and rail-name footnote suffixes for display.

The search map and in-app results map use a shared station layer. Rail stations
have a blue train pictogram; Tube/Metro stations have a red roundel-style symbol.
Station names and markers remain visible at every zoom level for saved London
data. They can be hidden using the in-app layer menu. The HTML report embeds the
same saved London station records/icons so that they work offline too.

The maps do not currently draw transit route lines or calculate station walking
times. Property pins use Airbnb-supplied coordinates and may be approximate.

## Stations outside London

The in-app station overlay can query OpenStreetMap/Overpass for station/halt
features in a suitably zoomed viewport. Results are cached in the browser session
with a seven-day freshness limit. This is best-effort, may be unavailable or rate
limited, and does not prevent normal search-area selection when it fails.

Exported reports do not make live Overpass queries or automatically embed those
non-London browser-session results. Their station layer is the saved London data.
Background tiles need internet even when station markers are available offline.

## Optional custom CSV layers

The in-app listing map also supports two configured place/station CSV overlays.
They need these columns:

| Column | Value |
|---|---|
| `Station` | Display name |
| `Latitude` | Decimal latitude |
| `Longitude` | Decimal longitude |

```csv
Station,Latitude,Longitude
Example Central Station,45.0703,7.6869
Example Landmark,45.0730,7.6900
```

Configure their paths in the search configuration:

```yaml
map_overlay_file_1: overlays/my_stations.csv
map_overlay_file_2: overlays/my_places.csv
```

Custom points appear as colored circle markers with hover/click names in the
listing map. Known bundled London coordinates are not drawn a second time as
plain circles. Arbitrary custom CSV layers are not automatically included in HTML
exports. Personal/custom CSVs remain ignored by Git unless explicitly included.
