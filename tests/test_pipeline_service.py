"""Offline pipeline compatibility, configuration migration and checkpoint tests."""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

import src.pipeline_service as pipeline


def test_search_config_deep_merge_maps_prompts_and_expands_focus(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    (tmp_path / "searches" / "legacy.search").mkdir(parents=True)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "default_focus_1": "quiet sleep",
        "ai": {
            "default_model": "fake-model",
            "review_summary": {"questions": ["Prioritize {default_focus_1}"], "role_prompt": "global summary"},
            "rating_analysis": {"questions": ["Legacy rating"], "role_prompt": "global rating"},
        },
    }), encoding="utf-8")
    (tmp_path / "searches" / "legacy.search" / "config.yaml").write_text(yaml.safe_dump({
        "ai": {"review_summary": {"role_prompt": "search summary"}},
        "default_focus_2": "transit",
        "zoom_value": "18.352051374073234",
    }), encoding="utf-8")

    assert "legacy.search" in pipeline.list_available_searches()
    config = pipeline.load_search_config("legacy.search")
    assert config["ai_review_summary"]["questions"] == ["Prioritize quiet sleep"]
    assert config["ai_review_summary"]["role_prompt"] == "search summary"
    assert config["ai_review_summary"]["model_name"] == "fake-model"
    assert config["ai_rating"]["role_prompt"] == "global rating"
    assert config["default_focus_2"] == "transit"
    assert config["zoom_value"] == 18
    assert yaml.safe_load((tmp_path / "searches" / "legacy.search" / "config.yaml").read_text())["zoom_value"] == "18.352051374073234"


def test_create_workspace_is_non_overwriting_and_persists_party(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    name = "turin_trip"
    path = pipeline.create_search_workspace(
        name, "2027-02-01", "2027-02-05", "Turin",
        45.2, 7.8, 45.0, 7.6, zoom="18.352051374073234", adults=3, children=1, infants=0, pets=1,
    )
    config = yaml.safe_load((path / "config.yaml").read_text())
    assert config["adults"] == 3
    assert config["children"] == 1
    assert config["pets"] == 1
    assert config["default_occupants"] == 4
    assert config["zoom_value"] == 18
    assert config["free_cancellation"] is False
    with pytest.raises(FileExistsError):
        pipeline.create_search_workspace(name, "2027-02-01", "2027-02-05", "Turin", 45.2, 7.8, 45.0, 7.6)
    with pytest.raises(ValueError):
        pipeline.create_search_workspace("../unsafe", "2027-02-01", "2027-02-05", "Turin", 45.2, 7.8, 45.0, 7.6)


def test_create_workspace_persists_free_cancellation_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    path = pipeline.create_search_workspace(
        "free_trip", "2027-02-01", "2027-02-05", "Turin",
        45.2, 7.8, 45.0, 7.6, free_cancellation=True,
    )
    config = yaml.safe_load((path / "config.yaml").read_text())
    assert config["free_cancellation"] is True


def _create_reviews_fixture(tmp_path, rows):
    output_dir = tmp_path / "searches" / "trip" / "output_data"
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output_dir / "reviews_df.csv", index=False)
    return output_dir


def test_ai_pipeline_counts_only_successful_ratings_and_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    output = _create_reviews_fixture(tmp_path, [
        {"room_id": "1", "reviews_text": "Good quiet stay", "AI_review_summary": "", "AI_rating": np.nan},
        {"room_id": "2", "reviews_text": "Another stay", "AI_review_summary": "", "AI_rating": np.nan},
    ])
    (tmp_path / "searches" / "trip" / "config.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fake-key")

    def fake_ai(df, text_columns, ai_config, new_column_name, skip_existing=False):
        result = df.copy()
        if new_column_name == "AI_review_summary":
            result[new_column_name] = result["room_id"].map(lambda room: f"Summary from reviews for {room}.")
        else:
            result[new_column_name] = result["room_id"].map({"1": "4.5", "2": "API request failed with status code 429"})
        return result

    monkeypatch.setattr(pipeline, "get_ai_rating", fake_ai)
    progress = []
    result = pipeline.run_ai_evaluation_pipeline("trip", progress_callback=lambda *args: progress.append(args))
    assert result["status"] == "partial"
    assert result["rated"] == 1
    assert result["attempted"] == 2
    assert result["failed_room_ids"] == ["2"]
    saved = pd.read_csv(output / "reviews_df.csv", dtype={"room_id": str})
    assert saved.loc[saved.room_id == "1", "AI_rating"].iloc[0] == 4.5
    assert pd.isna(saved.loc[saved.room_id == "2", "AI_rating"].iloc[0])
    assert len(progress) == 2


def test_ai_pipeline_reports_cooperative_cancel_after_current_row(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    output = _create_reviews_fixture(tmp_path, [
        {"room_id": "1", "reviews_text": "Good quiet stay", "AI_review_summary": "Existing summary one", "AI_rating": np.nan},
        {"room_id": "2", "reviews_text": "Another stay", "AI_review_summary": "Existing summary two", "AI_rating": np.nan},
    ])
    (tmp_path / "searches" / "trip" / "config.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fake-key")
    cancel = {"value": False}

    def fake_ai(df, text_columns, ai_config, new_column_name, skip_existing=False):
        result = df.copy()
        result[new_column_name] = "4.0"
        cancel["value"] = True
        return result

    monkeypatch.setattr(pipeline, "get_ai_rating", fake_ai)
    result = pipeline.run_ai_evaluation_pipeline("trip", should_cancel=lambda: cancel["value"])
    assert result["status"] == "cancelled"
    assert result["rated"] == 1
    assert result["attempted"] == 1
    saved = pd.read_csv(output / "reviews_df.csv", dtype={"room_id": str})
    assert saved.loc[saved.room_id == "1", "AI_rating"].iloc[0] == 4.0
    assert pd.isna(saved.loc[saved.room_id == "2", "AI_rating"].iloc[0])


def test_ai_pipeline_requires_explicit_key_before_any_provider_call(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    output = _create_reviews_fixture(tmp_path, [{"room_id": "1", "reviews_text": "text", "AI_rating": np.nan}])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    called = {"value": False}
    monkeypatch.setattr(pipeline, "get_ai_rating", lambda *args, **kwargs: called.update(value=True))
    with pytest.raises(RuntimeError, match="No provider request was sent"):
        pipeline.run_ai_evaluation_pipeline("trip")
    assert called["value"] is False
    assert (output / "reviews_df.csv").exists()

