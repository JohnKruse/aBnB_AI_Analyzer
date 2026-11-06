"""Browser-first Airbnb search, review, and curation application.

The service modules own persisted search data and collection jobs.  This module
keeps the legacy filtering helpers importable for existing scripts and hands the
interactive workflow to :mod:`src.ui.browser_app`.
"""

from __future__ import annotations

import ast
import logging
import re
from datetime import date, datetime
from typing import Any, List, Tuple, Union

import pandas as pd
import streamlit as st

from src.feature_extractor import enrich_properties_dataframe
from src.logging_config import REVIEW_APP_LOG, setup_logger
from src.utils import extract_coordinates


logger = logging.getLogger(__name__)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, set)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def format_date(date_val: Union[str, date, datetime]) -> str:
    """Format dates into standard YYYY-MM-DD string."""
    if isinstance(date_val, (date, datetime)):
        return date_val.strftime("%Y-%m-%d")
    return str(date_val)


def filter_by_price_range(df: pd.DataFrame, min_price: float, max_price: float) -> pd.DataFrame:
    """Filter by total price when available, otherwise nightly price."""
    if df.empty or min_price < 0 or max_price < min_price:
        return pd.DataFrame(columns=df.columns)
    price_col = "total_price" if "total_price" in df.columns else "price"
    if price_col not in df.columns:
        return df
    values = pd.to_numeric(df[price_col], errors="coerce")
    return df[(values >= min_price) & (values <= max_price)]


def filter_by_rating(df: pd.DataFrame, min_rating: float) -> pd.DataFrame:
    """Filter by minimum Airbnb rating while keeping legacy missing behavior."""
    if df.empty or min_rating > 5.0 or min_rating < 0.0:
        return pd.DataFrame(columns=df.columns)
    rating_col = "Airbnb_rating" if "Airbnb_rating" in df.columns else "rating"
    if rating_col not in df.columns:
        return df
    values = pd.to_numeric(df[rating_col], errors="coerce")
    return df[values >= min_rating]


def filter_by_amenities(df: pd.DataFrame, required_amenities: List[str]) -> pd.DataFrame:
    """Legacy helper: retain rows mentioning any requested amenity.

    Collection-valued fields are handled directly; ``pd.isna(list)`` raises an
    ambiguous truth-value error and was a frequent source of UI crashes.
    """
    if df.empty or not required_amenities or "amenities" not in df.columns:
        return df

    def mentions_any(value: Any) -> bool:
        if _is_missing(value):
            return False
        if isinstance(value, dict):
            text = " ".join(map(str, value.values()))
        elif isinstance(value, (list, tuple, set)):
            text = " ".join(map(str, value))
        elif isinstance(value, str):
            text = value
            try:
                parsed = ast.literal_eval(value)
                if isinstance(parsed, (list, tuple, set, dict)):
                    text = " ".join(map(str, parsed.values() if isinstance(parsed, dict) else parsed))
            except (ValueError, SyntaxError):
                pass
        else:
            text = str(value)
        lowered = text.casefold()
        return any(str(required).casefold() in lowered for required in required_amenities)

    return df[df["amenities"].apply(mentions_any)]


def filter_by_capacity(df: pd.DataFrame, min_capacity: int) -> pd.DataFrame:
    """Filter by advertised party capacity."""
    if df.empty or min_capacity <= 0 or min_capacity > 50:
        return pd.DataFrame(columns=df.columns)
    if "person_capacity" not in df.columns:
        return df
    capacities = pd.to_numeric(df["person_capacity"], errors="coerce").fillna(0)
    return df[capacities >= min_capacity]


def clean_description(desc: Any) -> str:
    """Remove HTML from a listing description."""
    if _is_missing(desc) or not desc or desc == "N/A":
        return "No description available."
    cleaned = re.sub(r"<br\s*/?>", "\n", str(desc), flags=re.I)
    return re.sub(r"<[^>]+>", "", cleaned).strip()


def extract_image_gallery(row: pd.Series) -> List[Tuple[str, str]]:
    """Extract image URL and caption pairs from normalized or legacy columns."""
    value = row.get("images_details")
    if _is_missing(value) or not value:
        value = row.get("images")
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return []
    if not isinstance(value, (list, tuple)):
        return []
    gallery = []
    for image in value:
        if isinstance(image, dict) and image.get("url"):
            gallery.append((str(image["url"]), str(image.get("title") or image.get("caption") or "")))
    return gallery


def extract_ai_highlights(summary_text: Any, max_points: int = 2) -> List[str]:
    """Extract concise bullet points from a stored review summary."""
    if _is_missing(summary_text) or not str(summary_text).strip():
        return []
    bullets = []
    for line in str(summary_text).splitlines():
        line = line.strip()
        if line.startswith(("-", "*")) and len(line) > 5:
            bullets.append(re.sub(r"\*\*([^*]+)\*\*", r"\1", line.lstrip("-* ")).strip())
        elif len(line) > 24 and not line.startswith("#") and not line[:1].isdigit():
            bullets.append(line)
        if len(bullets) >= max_points:
            break
    return [text[:82].rstrip() + "..." if len(text) > 85 else text for text in bullets]


def load_search_dataset(search_name: str, *, enrich: bool = True) -> pd.DataFrame:
    """Load a saved snapshot; defer enrichment when preparing a cached UI table."""
    from pathlib import Path
    from src.search_service import SEARCHES_ROOT, load_search_definition, validate_existing_search_name

    safe_name = validate_existing_search_name(search_name)
    searches_root = Path(SEARCHES_ROOT).resolve()
    workspace = (searches_root / safe_name).resolve()
    if workspace.parent != searches_root:
        return pd.DataFrame()
    path = workspace / "output_data" / "merged_df.csv"
    from src import storage_service as storage
    if not storage.exists(path):
        return pd.DataFrame()
    frame = storage.read_frame(path, dtype={"room_id": str})
    if "room_id" in frame.columns:
        frame["room_id"] = frame["room_id"].astype(str)
        frame = frame.drop_duplicates(subset=["room_id"], keep="first")
    if "latitude" not in frame.columns and "lat" in frame.columns:
        frame["latitude"] = frame["lat"]
    if "longitude" not in frame.columns:
        for candidate in ("lng", "lon", "long"):
            if candidate in frame.columns:
                frame["longitude"] = frame[candidate]
                break
    if "AI_rating" not in frame.columns:
        sources = [column for column in ("AI_rating_y", "AI_rating_x") if column in frame.columns]
        if sources:
            frame["AI_rating"] = frame[sources[0]]
            for source in sources[1:]:
                frame["AI_rating"] = frame["AI_rating"].combine_first(frame[source])
    frame = frame.drop(columns=[column for column in ("AI_rating_x", "AI_rating_y") if column in frame.columns])
    if "total_price" not in frame.columns:
        frame["total_price"] = pd.Series(float("nan"), index=frame.index, dtype=float)
    frame = extract_coordinates(frame)
    if not enrich:
        return frame
    try:
        definition = load_search_definition(safe_name)
    except Exception:
        definition = None
    from src import airbnb_service

    repair_quotes = getattr(airbnb_service, "repair_saved_quote_prices", None)
    if callable(repair_quotes):
        repaired = repair_quotes(frame, definition)
        if isinstance(repaired, pd.DataFrame):
            frame = repaired
    return enrich_properties_dataframe(frame, search_context=definition)


def main() -> None:
    """Run the Streamlit browser workflow."""
    from src.ui.browser_app import render_browser_app

    if not logger.handlers:
        setup_logger(__name__, REVIEW_APP_LOG)
    try:
        render_browser_app()
    except Exception:
        # Streamlit control-flow exceptions inherit directly from BaseException
        # and pass through. Ordinary failures are logged with traceback, then
        # re-raised so Streamlit still displays its normal error UI.
        logger.exception("Unexpected browser workflow failure")
        raise


if __name__ == "__main__":
    main()
