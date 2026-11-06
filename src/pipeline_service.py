"""Pipeline Service for BnB AI Analyzer.

Manages search workspaces, Airbnb scraping, and AI evaluation routines.
Designed for programmatic invocation by Streamlit or batch workers with
real-time progress reporting.
"""

import os
import yaml
import json
import logging
import re
import tempfile
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, date

from .utils import extract_total_price
from .ai_utils import get_ai_rating, clean_ai_review_summary, extract_ai_rating
from .feature_extractor import enrich_properties_dataframe
from .logging_config import setup_logger, CONSOLIDATED_LOG
from . import storage_service as storage
from .search_service import (
    load_search_definition,
    normalize_map_zoom,
    save_search_definition,
    validate_existing_search_name,
    validate_search_name,
)

logger = setup_logger(__name__, CONSOLIDATED_LOG)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _workspace_path(search_name: str) -> Path:
    """Resolve a workspace only after validating its path-safe identifier."""
    name = validate_existing_search_name(search_name)
    searches_root = (PROJECT_ROOT / "searches").resolve()
    workspace = (searches_root / name).resolve()
    if workspace.parent != searches_root:
        raise ValueError("Search workspace path escapes the searches directory.")
    return workspace


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _expand_focus_prompt(config: Dict[str, Any]) -> Dict[str, Any]:
    focuses = {
        f"default_focus_{index}": config.get(f"default_focus_{index}", "")
        for index in range(1, 6)
    }

    def visit(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return value.format(**focuses)
            except (KeyError, ValueError):
                return value
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        return value

    return visit(config)


def _is_provider_error(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith(("api request failed", "error:", "error ", "unsupported file type"))


def _atomic_csv(df: pd.DataFrame, path: Path) -> None:
    """Commit a SQLite snapshot and refresh its compatibility CSV export."""
    storage.write_frame(path, df)


def list_available_searches() -> List[str]:
    """Enumerate existing search workspaces inside the searches directory."""
    searches_dir = PROJECT_ROOT / "searches"
    if not searches_dir.exists():
        return []
    
    contexts = []
    for item in sorted(searches_dir.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            try:
                validate_existing_search_name(item.name)
                if item.resolve().parent != searches_dir.resolve():
                    continue
            except ValueError:
                continue
            config_file = item / "config.yaml"
            output_dir = item / "output_data"
            if config_file.exists() or output_dir.exists() or (item / "analyzer.sqlite3").exists():
                contexts.append(item.name)
    return contexts


def load_search_config(search_name: str) -> Dict[str, Any]:
    """Load configuration for a specific search workspace merged with global defaults."""
    global_config_path = PROJECT_ROOT / "config.yaml"
    global_cfg: Dict[str, Any] = {}
    if global_config_path.exists():
        with open(global_config_path, "r", encoding="utf-8") as f:
            global_cfg = yaml.safe_load(f) or {}

    workspace = _workspace_path(search_name)
    search_config_path = workspace / "config.yaml"
    search_cfg: Dict[str, Any] = {}
    if search_config_path.exists():
        with open(search_config_path, "r", encoding="utf-8") as f:
            search_cfg = yaml.safe_load(f) or {}

    # Deep-merge nested settings, then bridge current and legacy prompt names.
    merged = _deep_merge(global_cfg, search_cfg)
    ai_section = merged.get("ai", {}) if isinstance(merged.get("ai"), dict) else {}
    if "ai_review_summary" not in merged and isinstance(ai_section.get("review_summary"), dict):
        merged["ai_review_summary"] = dict(ai_section["review_summary"])
    if "ai_rating" not in merged and isinstance(ai_section.get("rating_analysis"), dict):
        merged["ai_rating"] = dict(ai_section["rating_analysis"])
    default_model = (
        merged.get("model_name") or merged.get("ai_model") or ai_section.get("default_model") or "gpt-6-luna"
    )
    for key in ("ai_review_summary", "ai_rating"):
        block = merged.setdefault(key, {})
        if isinstance(block, dict):
            block.setdefault("model_name", default_model)
    merged = _expand_focus_prompt(merged)

    # Browser-created trip fields are authoritative; old workspaces continue to
    # read their original YAML context unchanged.
    definition = load_search_definition(search_name, root=PROJECT_ROOT / "searches")
    if definition:
        merged.update(definition)
    merged["search_name"] = search_name
    # Normalize legacy values in memory only; reading a workspace never rewrites it.
    merged["zoom_value"] = normalize_map_zoom(merged.get("zoom_value"))
    merged["base_dir"] = str(PROJECT_ROOT)
    merged["search_subdir"] = search_name
    merged["output_subdir"] = f"{search_name}/output_data"
    return merged


def create_search_workspace(
    name: str,
    check_in: str,
    check_out: str,
    location_query: str,
    ne_lat: float,
    ne_lng: float,
    sw_lat: float,
    sw_lng: float,
    zoom: int = 15,
    currency: str = "EUR",
    model_name: str = "gpt-6-luna",
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    pets: int = 0,
    total_budget: Optional[float] = None,
    geometry: Optional[Dict[str, Any]] = None,
    free_cancellation: bool = False,
) -> Path:
    """Create a validated, non-overwriting search workspace."""
    name = validate_search_name(name)
    if geometry is None:
        geometry = {"type": "bbox", "south": sw_lat, "west": sw_lng, "north": ne_lat, "east": ne_lng}
    search_dir = _workspace_path(name)
    if search_dir.exists() and any(search_dir.iterdir()):
        raise FileExistsError(f"Search '{name}' already exists; choose another name.")
    save_search_definition(name, {
        "location": location_query,
        "check_in": check_in,
        "check_out": check_out,
        "adults": adults,
        "children": children,
        "infants": infants,
        "pets": pets,
        "currency": currency,
        "total_budget": total_budget,
        "free_cancellation": free_cancellation,
        "geometry": geometry,
    }, create=True, root=PROJECT_ROOT / "searches")
    # Preserve configuration knobs used by older CLI scripts.
    config_path = search_dir / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config_data.update({
        "zoom_value": normalize_map_zoom(zoom),
        "model_name": model_name,
        "ai_model": model_name,
        "default_min_price": 0,
        "default_max_price": 2000,
        "default_min_ai_rating": 0,
        "default_max_ai_rating": 5,
        "default_min_abnb_rating": 0,
        "default_max_abnb_rating": 5,
        "default_min_user_rating": 0,
        "default_max_user_rating": 6,
    })
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=search_dir, prefix=".config.", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            yaml.safe_dump(config_data, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, config_path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return search_dir


def get_search_status(search_name: str) -> Dict[str, Any]:
    """Retrieve data health, record counts, and AI evaluation metrics for a search context."""
    output_dir = _workspace_path(search_name) / "output_data"
    status = {
        "exists": output_dir.exists() or storage.exists(output_dir / "merged_df.csv"),
        "results_count": 0,
        "details_count": 0,
        "reviews_count": 0,
        "merged_count": 0,
        "ai_rated_count": 0,
        "ai_unrated_count": 0,
        "has_failed_rooms": False,
        "failed_rooms_count": 0,
    }

    if not output_dir.exists() and not storage.exists(output_dir / "merged_df.csv"):
        return status

    # Results
    results_path = output_dir / "results_df.csv"
    if storage.exists(results_path):
        try:
            rdf = storage.read_frame(results_path, usecols=["room_id"])
            status["results_count"] = len(rdf)
        except Exception:
            pass

    # Details
    details_path = output_dir / "details_df.csv"
    if storage.exists(details_path):
        try:
            ddf = storage.read_frame(details_path, usecols=["room_id"])
            status["details_count"] = len(ddf)
        except Exception:
            pass

    # Reviews & AI Ratings
    reviews_path = output_dir / "reviews_df.csv"
    if storage.exists(reviews_path):
        try:
            rev_df = storage.read_frame(reviews_path)
            status["reviews_count"] = len(rev_df)
            if "AI_rating" in rev_df.columns:
                valid_ratings = pd.to_numeric(rev_df["AI_rating"], errors="coerce").dropna()
                status["ai_rated_count"] = len(valid_ratings)
                status["ai_unrated_count"] = len(rev_df) - len(valid_ratings)
        except Exception:
            pass

    # Merged
    merged_path = output_dir / "merged_df.csv"
    if storage.exists(merged_path):
        try:
            mdf = storage.read_frame(merged_path, usecols=["room_id"])
            status["merged_count"] = len(mdf)
        except Exception:
            pass

    # Failed rooms
    failed_path = output_dir / "failed_rooms.txt"
    if failed_path.exists() and failed_path.stat().st_size > 0:
        try:
            with open(failed_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
                status["has_failed_rooms"] = bool(lines)
                status["failed_rooms_count"] = len(lines)
        except Exception:
            pass

    return status


def run_ai_evaluation_pipeline(
    search_name: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Evaluate legacy unrated properties without re-scraping Airbnb.

    The success count includes only numeric ratings parsed from successful
    provider responses. Each completed row is checkpointed to the CSV snapshot.
    """
    output_dir = _workspace_path(search_name) / "output_data"
    reviews_path = output_dir / "reviews_df.csv"
    merged_path = output_dir / "merged_df.csv"
    config = load_search_config(search_name)

    if not storage.exists(reviews_path):
        raise FileNotFoundError(f"Reviews file not found at {reviews_path}")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY to run AI evaluation. No provider request was sent.")

    logger.info("Loading reviews dataset from %s", reviews_path)
    reviews_df = storage.read_frame(reviews_path, dtype={"room_id": str})
    
    # Identify unrated properties
    if "AI_rating" not in reviews_df.columns:
        reviews_df["AI_rating"] = np.nan
    else:
        reviews_df["AI_rating"] = pd.to_numeric(reviews_df["AI_rating"], errors="coerce")

    if "AI_review_summary" not in reviews_df.columns:
        reviews_df["AI_review_summary"] = ""
    else:
        reviews_df["AI_review_summary"] = reviews_df["AI_review_summary"].map(
            lambda value: "" if pd.isna(value) else str(value)
        ).astype(object)

    unrated_mask = reviews_df["AI_rating"].isna() | (reviews_df["AI_rating"] == "")
    total_unrated = int(unrated_mask.sum())
    logger.info("Identified %d unrated properties out of %d total", total_unrated, len(reviews_df))

    if total_unrated == 0:
        return {"status": "success", "rated": 0, "total": len(reviews_df), "message": "All properties are already rated."}

    # Configuration for AI
    ai_rating_cfg = config.get("ai_rating", {})
    if not ai_rating_cfg.get("model_name"):
        ai_rating_cfg["model_name"] = config.get("model_name") or config.get("ai_model") or "gpt-6-luna"

    # Evaluate unrated properties
    attempted = 0
    successful = 0
    failed_room_ids: List[str] = []
    cancelled = False
    reviews_text_col = "reviews_text" if "reviews_text" in reviews_df.columns else None
    for idx in reviews_df[unrated_mask].index:
        if should_cancel and should_cancel():
            cancelled = True
            break
        attempted += 1
        room_id = reviews_df.at[idx, "room_id"]
        summary_value = reviews_df.at[idx, "AI_review_summary"]
        summary_text = "" if pd.isna(summary_value) else str(summary_value).strip()
        
        if progress_callback:
            progress_callback(attempted, total_unrated, f"Evaluating room {room_id} ({attempted}/{total_unrated})")

        # If summary is missing, summarize from reviews_text first
        if not summary_text or len(summary_text.strip()) < 10:
            review_value = reviews_df.at[idx, reviews_text_col] if reviews_text_col else ""
            rev_text = "" if pd.isna(review_value) else str(review_value).strip()
            if rev_text.strip():
                try:
                    summary_cfg = config.get("ai_review_summary", {})
                    if not summary_cfg.get("model_name"):
                        summary_cfg["model_name"] = ai_rating_cfg["model_name"]
                    temp_df = pd.DataFrame([{"reviews_text": rev_text, "room_id": room_id}])
                    temp_df = get_ai_rating(temp_df, ["reviews_text"], summary_cfg, "AI_review_summary")
                    candidate_summary = temp_df.iloc[0]["AI_review_summary"]
                    if not _is_provider_error(candidate_summary):
                        summary_text = "" if pd.isna(candidate_summary) else str(candidate_summary).strip()
                        if len(summary_text) >= 10:
                            reviews_df.at[idx, "AI_review_summary"] = summary_text
                    else:
                        failed_room_ids.append(str(room_id))
                except Exception as e:
                    logger.error("Failed to generate AI summary for room %s: %s", room_id, e)

        # Rate based on AI review summary
        if summary_text and len(summary_text) >= 10 and not _is_provider_error(summary_text):
            try:
                temp_df = pd.DataFrame([{"AI_review_summary": summary_text, "room_id": room_id}])
                temp_df = get_ai_rating(temp_df, ["AI_review_summary"], ai_rating_cfg, "AI_rating")
                raw_rating = temp_df.iloc[0]["AI_rating"]
                num_rating = None if _is_provider_error(raw_rating) else extract_ai_rating(str(raw_rating))
                if num_rating is not None:
                    reviews_df.at[idx, "AI_rating"] = num_rating
                    successful += 1
                else:
                    failed_room_ids.append(str(room_id))
            except Exception as e:
                logger.error("Failed to score AI rating for room %s: %s", room_id, e)
                failed_room_ids.append(str(room_id))
        else:
            failed_room_ids.append(str(room_id))

        # Persist each completed listing. A rerun can safely retry unrated rows.
        _atomic_csv(reviews_df, reviews_path)

    # Persist updated reviews
    _atomic_csv(reviews_df, reviews_path)
    logger.info("Saved updated reviews_df to %s", reviews_path)

    # Propagate into merged_df
    if storage.exists(merged_path):
        merged_df = storage.read_frame(merged_path, dtype={"room_id": str})
        # Drop redundant rating columns before merge
        drop_cols = [c for c in ["AI_rating", "AI_rating_x", "AI_rating_y", "AI_review_summary", "reviews_text"] if c in merged_df.columns]
        if drop_cols:
            merged_df = merged_df.drop(columns=drop_cols)
        
        merged_df = pd.merge(merged_df, reviews_df[["room_id", "AI_rating", "AI_review_summary", "reviews_text"]], on="room_id", how="left")
        merged_df = enrich_properties_dataframe(merged_df, search_context=config)
        _atomic_csv(merged_df, merged_path)
        logger.info("Propagated AI ratings to %s", merged_path)

    status = "cancelled" if cancelled else "success" if successful == total_unrated else "partial"
    return {
        "status": status,
        "rated": successful,
        "attempted": attempted,
        "failed_room_ids": list(dict.fromkeys(failed_room_ids)),
        "total": len(reviews_df),
    }
