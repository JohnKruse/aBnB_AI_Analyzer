"""Build a portable property report without a server or runtime dependencies."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlencode

import pandas as pd
from .branding import github_logo_link
from .transit_service import STATION_ASSETS, saved_london_stations

ASSETS = Path(__file__).with_name("report_assets")


def build_property_report(
    data: pd.DataFrame,
    *,
    title: str,
    search_name: str,
    definition: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    curation: Mapping[str, Any] | None = None,
    created_at: datetime | None = None,
) -> str:
    """Embed the prepared joined table, trip and review state in one HTML file.

    Input must be the prepared snapshot: verified_total_price and personal_fit
    already exclude stale quotes and assessments. No collection or AI calls occur.
    """
    trip = dict(definition or {})
    records = json.loads(data.to_json(orient="records", date_format="iso", default_handler=str))
    curated = curation or {}
    hidden = set(map(str, curated.get("hidden_room_ids", [])))
    favorites = set(map(str, curated.get("shortlisted_room_ids", [])))
    order = {str(rid): i for i, rid in enumerate(curated.get("manual_order", []))}
    params = {key: trip[key] for key in ("check_in", "check_out", "adults", "children", "infants", "pets") if trip.get(key) is not None}
    rows = []
    for row in records:
        rid = str(row.get("room_id", ""))
        if not rid or rid in hidden:
            continue
        row["room_id"] = rid
        row["airbnb_url"] = "https://www.airbnb.com/rooms/" + quote(rid, safe="") + ("?" + urlencode(params) if params else "")
        rows.append(row)
    rows.sort(key=lambda row: order.get(row["room_id"], len(order)))
    favorites.intersection_update(row["room_id"] for row in rows)
    stamp = created_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    # Only evaluation metadata goes into the report; never export app config or credentials.
    safe_profile = {key: (profile or {}).get(key) for key in ("name", "weights", "additional_instructions", "model_name", "reasoning_effort")}
    payload = {
        "version": 1, "title": title.strip() or search_name, "search_name": search_name,
        "created_at": stamp.isoformat(), "trip": trip, "profile": safe_profile,
        "transit": saved_london_stations(),
        "rows": rows, "state": {"favorites": sorted(favorites), "notes": {}, "selected": None, "filters": {}, "sort": {"key": "fit", "direction": -1}},
    }
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = (ASSETS / "report.html").read_text(encoding="utf-8")
    css = (ASSETS / "report.css").read_text(encoding="utf-8")
    css += "\n" + (STATION_ASSETS / "station_labels.css").read_text(encoding="utf-8")
    script = (STATION_ASSETS / "station_labels.js").read_text(encoding="utf-8")
    script += "\n" + (ASSETS / "report.js").read_text(encoding="utf-8")
    return (template.replace("/* REPORT_CSS */", css)
            .replace("/* REPORT_JS */", script)
            .replace("<!-- PROJECT_LOGO -->", github_logo_link())
            .replace("REPORT_PAYLOAD", encoded))
