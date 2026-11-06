"""In-process browser workflow smoke tests; no collector or AI requests are made."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys
from types import ModuleType

import pandas as pd
import numpy as np
import pytest
from streamlit.testing.v1 import AppTest

import review_app
import src.airbnb_service as airbnb_service
import src.curation_service as curation_service
import src.evaluation_service as evaluation_service
import src.job_service as job_service
import src.pipeline_service as pipeline_service
import src.search_service as search_service
from src.ui import browser_app


def _app_test(*, browse=True):
    app = AppTest.from_file(str(Path(review_app.__file__)))
    # Existing explorer checks opt into the optional compatibility browser.
    app.session_state["browse_properties_turin-fixture"] = browse
    return app


def _isolated_roots(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for module in (airbnb_service, curation_service, evaluation_service, job_service, pipeline_service, search_service):
        monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(search_service, "SEARCHES_ROOT", tmp_path / "searches")


def _fake_map(*, key, center, zoom, **kwargs):
    return {"center": {"lat": center[0], "lng": center[1]}, "zoom": zoom}


def _sample_rows() -> pd.DataFrame:
    check_in = (date.today() + timedelta(days=35)).isoformat()
    check_out = (date.today() + timedelta(days=40)).isoformat()
    return pd.DataFrame([
        {
            "room_id": "fixture-101",
            "name": "Bright apartment near transit",
            "description": "Ensuite bathroom. Air conditioning. Washer.",
            "price": 840,
            "total_price": 840,
            "quote_status": "current",
            "quote_source": "details:saved_explicit_detail_total",
            "quote_total_completeness": "saved_explicit_detail_total",
            "quote_check_in": check_in,
            "quote_check_out": check_out,
            "quote_currency": "EUR",
            "quote_party_supported": True,
            "quote_adults": 2,
            "quote_children": 0,
            "quote_infants": 0,
            "quote_pets": 0,
            "trip_revision": 1,
            "trip_revision_details": 1,
            "details_fetched_at": "2026-10-01T12:00:00+00:00",
            "Airbnb_rating": 4.91,
            "personal_fit_score": 4.2,
            "personal_fit_coverage": 0.75,
            "personal_fit_status": "scored",
            "personal_fit_profile": "Default",
            "personal_fit_model": "gpt-6-luna",
            "personal_fit_reasoning_effort": "xhigh",
            "personal_fit_breakdown": '{"cleanliness":{"score":5,"rationale":"Guests repeatedly mention clean rooms.","evidence":[{"quote":"Very clean."}]}}',
            "person_capacity": 3,
            "room_type": "Entire home/apt",
            "bed_count": 2,
            "bedroom_count": 1,
            "latitude": 45.071,
            "longitude": 7.687,
            "amenities": ["Air conditioning", "Washer"],
            "reviews_text": "2026-09-15 Very clean and quiet. Rating: 5 [review_id=r1]",
            "AI_review_summary": "Cleanliness: Very clean\nQuiet: Calm at night",
        },
        {
            "room_id": "fixture-202",
            "name": "Loft with location not shared",
            "description": "Bright loft with a kitchen.",
            "price": None,
            "total_price": None,
            "quote_status": "missing",
            "Airbnb_rating": None,
            "person_capacity": 2,
            "room_type": "Private room",
            "bed_count": 1,
            "bedroom_count": 1,
            "latitude": None,
            "longitude": None,
            "amenities": ["Kitchen"],
            "reviews_text": None,
        },
        {
            "room_id": "fixture-303",
            "name": "Unavailable for these dates",
            "description": "Private bathroom.",
            "total_price": None,
            "quote_status": "unavailable",
            "availability_state": "unavailable",
            "person_capacity": 4,
            "latitude": 45.073,
            "longitude": 7.69,
            "amenities": ["Wifi"],
        },
    ])


class _FakeEvaluationService:
    def __init__(self, root):
        self.PROJECT_ROOT = root
        self.saved = []

    def default_evaluation_profile(self):
        return {"name": "Default", "weights": {name: 3 for name in browser_app.PRIORITY_LABELS}, "additional_instructions": "", "hard_filters": {}, "model_name": "gpt-6-luna", "reasoning_effort": "xhigh"}

    def list_evaluation_profiles(self, search_name):
        path = self.PROJECT_ROOT / "searches" / search_name / "evaluation_profiles.json"
        if path.exists():
            import json
            return list(json.loads(path.read_text())["profiles"])
        return ["Default"]

    def load_evaluation_profile(self, search_name, profile_name="Default"):
        path = self.PROJECT_ROOT / "searches" / search_name / "evaluation_profiles.json"
        saved = {}
        if path.exists():
            import json
            saved = json.loads(path.read_text()).get("profiles", {}).get(profile_name, {})
        return self.default_evaluation_profile() | saved | {"name": profile_name}

    def profile_preview(self, profile):
        return "Saved profile preview"

    def save_evaluation_profile(self, search_name, profile_name, weights, additional_instructions="", hard_filters=None):
        import json
        from pathlib import Path
        path = Path(self.PROJECT_ROOT) / "searches" / search_name / "evaluation_profiles.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        old = json.loads(path.read_text()) if path.exists() else {"profiles": {}}
        old["profiles"][profile_name] = {"weights": weights, "additional_instructions": additional_instructions, "hard_filters": hard_filters or {}}
        path.write_text(json.dumps(old))
        self.saved.append(profile_name)
        return old["profiles"][profile_name]


class _FakeJobService:
    def __init__(self):
        self.started = []
        self.cancelled = []
        self.job = None
        self.pipeline_status = {
            "next_stage": "evaluate",
            "active_job": None,
            "stages": {
                "discover": {"state": "success", "count": 3, "total": 3, "completed": 3, "failed": 0, "missing": 0, "can_start": False, "reason": None},
                "details": {
                    "state": "success", "count": 3, "total": 3, "completed": 3, "failed": 0, "missing": 0, "can_start": False, "reason": None,
                    "details": {"attempted": 3, "succeeded": 3, "unavailable": 0, "failed": 0, "missing": 0},
                    "reviews": {"attempted": 3, "succeeded": 3, "failed": 0, "missing": 0},
                },
                "evaluate": {"state": "ready", "count": 1, "total": 3, "completed": 1, "failed": 0, "missing": 2, "can_start": True, "reason": None},
            },
        }
        self.after_start = {}
        self.on_start = None
        self.status_sequence = []
        self.status_calls = 0

    def get_search_job(self, search_name):
        return self.job

    def get_pipeline_status(self, search_name, profile_name="Default"):
        self.status_calls += 1
        if self.status_sequence:
            return self.status_sequence.pop(0)
        if self.job and self.job["status"] in {"queued", "running", "cancelling"}:
            snapshot = {**self.pipeline_status, "stages": {key: dict(value) for key, value in self.pipeline_status["stages"].items()}}
            stage = self.job["stage"]
            if stage in snapshot["stages"]:
                snapshot["stages"][stage]["state"] = "running"
                snapshot["stages"][stage]["can_start"] = False
            snapshot["active_job"] = {
                "job_id": self.job["job_id"],
                "stage": stage,
                "current_stage": stage,
                "status": self.job["status"],
                "completed": 0,
                "total": 0,
                "message": self.job["message"],
                "profile_name": profile_name,
            }
            return snapshot
        return {**self.pipeline_status, "stages": {key: dict(value) for key, value in self.pipeline_status["stages"].items()}}

    def start_search_job(self, search_name, stage, *, profile_name="Default", run_options=None):
        self.started.append((search_name, stage, profile_name))
        self.job = {"job_id": "fake-job", "search_name": search_name, "stage": stage, "status": "queued", "message": "Queued test job", "completed": 0, "total": 0}
        if stage in self.after_start:
            self.pipeline_status = self.after_start[stage]
            self.job = None
        if self.on_start:
            self.on_start(stage)
        return self.job

    def cancel_search_job(self, search_name):
        self.cancelled.append(search_name)
        return self.job


def _status_stage(
    state,
    *,
    count=0,
    total=0,
    completed=0,
    failed=0,
    missing=0,
    can_start=False,
    reason=None,
    details=None,
    reviews=None,
    stale=0,
    evaluated_count=None,
    scored_count=None,
    insufficient_evidence_count=None,
):
    return {
        "state": state,
        "count": count,
        "total": total,
        "completed": completed,
        "failed": failed,
        "missing": missing,
        "stale": stale,
        "can_start": can_start,
        "reason": reason,
        "details": details or {},
        "reviews": reviews or {},
        "job": None,
        "evaluated_count": evaluated_count,
        "scored_count": scored_count,
        "insufficient_evidence_count": insufficient_evidence_count,
    }


def _pipeline_snapshot(discover, details, evaluate, next_stage):
    return {
        "next_stage": next_stage,
        "active_job": None,
        "stages": {"discover": discover, "details": details, "evaluate": evaluate},
    }


def _setup_existing_app(monkeypatch, tmp_path):
    _isolated_roots(monkeypatch, tmp_path)
    evaluation = _FakeEvaluationService(tmp_path)
    jobs = _FakeJobService()
    monkeypatch.setattr(browser_app, "_service", lambda name: evaluation if name.endswith("evaluation_service") else jobs)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: ["turin-fixture"])
    monkeypatch.setattr(browser_app, "load_search_config", lambda name: {"location": "Turin", "currency": "EUR", "zoom_value": 13})
    definition = search_service.validate_trip_definition({
        "location": "Turin",
        "check_in": (date.today() + timedelta(days=35)).isoformat(),
        "check_out": (date.today() + timedelta(days=40)).isoformat(),
        "adults": 2,
        "children": 0,
        "infants": 0,
        "pets": 0,
        "currency": "EUR",
        "geometry": {"type": "bbox", "south": 45.05, "west": 7.65, "north": 45.09, "east": 7.72},
    })
    monkeypatch.setattr(browser_app, "_active_definition", lambda name: definition)
    monkeypatch.setattr(review_app, "load_search_dataset", lambda name, *, enrich=True: _sample_rows())
    monkeypatch.setattr(browser_app, "render_area_map", _fake_map)
    monkeypatch.setattr(browser_app, "render_listing_map", lambda listings, **kwargs: {})
    monkeypatch.setattr(browser_app, "collector_capabilities", lambda: {"available": False, "version": None, "functions": {}})
    return evaluation, jobs


def _install_fake_search_catalog(monkeypatch, records):
    module = ModuleType("src.search_catalog")

    def list_search_summaries(root=None):
        return [dict(summary) for summary in records.values()]

    def set_search_archived(name, archived, root=None):
        summary = records[name]
        summary["archived"] = bool(archived)
        summary["archive_reason"] = "Manually archived" if archived else None
        return dict(summary)

    def delete_search(name, confirmation, root=None):
        if confirmation != name:
            raise ValueError("Type the exact search ID to confirm.")
        records.pop(name)
        return {"name": name, "restore_name": name, "trash_path": f".trash/{name}"}

    module.list_search_summaries = list_search_summaries
    module.set_search_archived = set_search_archived
    module.delete_search = delete_search
    def delete_searches(names, confirmation, root=None):
        assert list(confirmation) == list(names)
        return [delete_search(name, name, root=root) for name in names]
    module.delete_searches = delete_searches
    monkeypatch.setitem(sys.modules, "src.search_catalog", module)
    monkeypatch.setattr(job_service, "get_search_job", lambda search_name: None)
    return module


def test_empty_state_renders_new_search_without_saved_workspaces(monkeypatch, tmp_path):
    _isolated_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: [])
    monkeypatch.setattr(browser_app, "render_area_map", _fake_map)

    app = _app_test().run()

    assert not app.exception
    assert any(widget.label == "Destination or neighborhood" for widget in app.text_input)
    assert any(widget.label == "Check-in" for widget in app.date_input)
    assert any(widget.label == "Find" for widget in app.button)
    assert any(widget.label == "Discover listings" for widget in app.button)


def test_sidebar_new_search_button_opens_new_search_form_from_saved_workspace(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test().run()
    assert not app.exception
    assert app.session_state["active_search_selection"] == "turin-fixture"

    app.button(key="new_search_button").click().run()
    assert not app.exception
    assert app.session_state["active_search_selection"] == "＋ New Search"
    assert any(widget.key == "new_destination" for widget in app.text_input)


def test_search_table_includes_all_searches_and_requested_columns(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    records = {
        "turin-fixture": {"name": "turin-fixture", "location": "Turin", "check_in": "2027-01-19", "check_out": "2027-01-24",
                          "people": 3, "properties_count": 129, "listings_updated_at": "2026-11-05T15:30:00Z"},
        "old-search": {"name": "old-search", "location": "Rome", "archived": True},
    }
    _install_fake_search_catalog(monkeypatch, records)
    app = _app_test(browse=False).run()
    assert not app.exception
    frame = browser_app._search_table_rows(list(records.values()))
    assert frame.columns.tolist() == ["Location", "Stay dates", "People", "Properties", "Last updated", "Search"]
    assert len(frame) == 2  # Old searches remain visible.
    assert frame.iloc[0]["People"] == 3
    assert frame.iloc[0]["Properties"] == 129
    assert frame.iloc[0]["Last updated"] == "5 Nov 2026 16:30 CET"
    assert not any(widget.label in {"Search", "Show"} for widget in app.selectbox)
    assert not any("Hard filters" in item.value for item in app.subheader)
    assert not any(widget.key and widget.key.startswith("capacity_filter_") for widget in app.number_input)


def test_search_cell_click_opens_search_without_marking_for_deletion(monkeypatch):
    from types import SimpleNamespace
    state = {"active_search_selection": "cheyenne-test"}
    monkeypatch.setattr(browser_app.st, "session_state", state)
    response = SimpleNamespace(event_data={"type": "cellClicked", "data": {"Search": "putney"}},
                               selected_rows=None)
    assert browser_app._apply_search_table_event(response, ["cheyenne-test", "putney"])
    assert state["active_search_selection"] == "putney"
    assert state.get("_checked_search_deletions", []) == []


def test_search_delete_requires_explicit_checkbox_and_has_labeled_column(monkeypatch, tmp_path):
    from types import SimpleNamespace
    _setup_existing_app(monkeypatch, tmp_path)
    captured = []
    def grid(frame, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(event_data=None, selected_rows=None)
    monkeypatch.setattr(browser_app, "AgGrid", grid)
    app = _app_test(browse=False)
    # Old row-highlighting state must not become a deletion selection.
    app.session_state["_selected_search_names"] = ["turin-fixture"]
    app.run()
    assert not app.exception
    assert app.button(key="delete_search_button").disabled
    assert app.button(key="delete_search_button").label == "Delete checked (0)"
    options = captured[0]["gridOptions"]
    assert options["suppressRowClickSelection"] is True
    first = options["columnDefs"][0]
    assert first["headerName"] == "Delete"
    assert first["checkboxSelection"] is True


def test_search_panel_can_close_and_open_without_switching_search(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test(browse=False).run()
    app.button(key="toggle_search_panel").click().run()
    assert not app.exception
    assert app.session_state["search_panel_open"] is False
    assert app.button(key="toggle_search_panel").label == "☰ Show searches"
    assert app.session_state["active_search_selection"] == "turin-fixture"
    app.button(key="toggle_search_panel").click().run()
    assert not app.exception
    assert app.session_state["search_panel_open"] is True
    assert app.session_state["active_search_selection"] == "turin-fixture"


def test_only_checkbox_selection_marks_searches_for_deletion(monkeypatch):
    from types import SimpleNamespace
    state = {"active_search_selection": "second"}
    monkeypatch.setattr(browser_app.st, "session_state", state)
    response = SimpleNamespace(event_data={"type": "selectionChanged"},
                               selected_rows=pd.DataFrame({"Search": ["first", "third"]}))
    assert browser_app._apply_search_table_event(response, ["first", "second", "third"])
    assert state["_checked_search_deletions"] == ["first", "third"]
    assert state["active_search_selection"] == "second"


def test_batch_delete_confirms_once_and_preserves_unselected_search(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    records = {name: {"name": name, "location": "Turin"} for name in ["first", "keep", "third"]}
    _install_fake_search_catalog(monkeypatch, records)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: list(records))
    app = _app_test(browse=False)
    app.session_state["active_search_selection"] = "first"
    app.session_state["_checked_search_deletions"] = ["first", "third"]
    app.run()
    assert app.button(key="delete_search_button").label == "Delete checked (2)"
    app.button(key="delete_search_button").click().run()
    assert len(records) == 3
    app.button(key="cancel_delete_search").click().run()
    assert len(records) == 3
    app.button(key="delete_search_button").click().run()
    app.button(key="confirm_delete_search").click().run()
    assert not app.exception
    assert list(records) == ["keep"]
    assert app.session_state["active_search_selection"] == "keep"
    assert app.session_state["_checked_search_deletions"] == []


def test_batch_delete_disabled_if_any_selected_search_has_active_job(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    records = {name: {"name": name} for name in ["first", "busy"]}
    _install_fake_search_catalog(monkeypatch, records)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: list(records))
    monkeypatch.setattr(browser_app, "_search_job_is_active", lambda name: (name == "busy", False))
    app = _app_test(browse=False)
    app.session_state["_checked_search_deletions"] = ["first", "busy"]
    app.run()
    assert not app.exception
    assert app.button(key="delete_search_button").disabled


def test_search_selection_survives_collection_metadata_updates(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    records = {
        "cheyenne-test": {"name": "cheyenne-test", "location": "Cheyenne"},
        "turin-fixture": {"name": "turin-fixture", "location": "Turin"},
    }
    _install_fake_search_catalog(monkeypatch, records)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: list(records))
    app = _app_test().run()
    app.session_state["active_search_selection"] = "turin-fixture"
    app.run()
    assert not app.exception
    assert app.session_state["active_search_selection"] == "turin-fixture"

    # Discovery changes the formatted option from "L —" to a collection date.
    records["turin-fixture"]["listings_updated_at"] = "2026-11-05T16:53:26Z"
    app.run()
    assert not app.exception
    assert app.session_state["active_search_selection"] == "turin-fixture"

    # Reviews and changes to another workspace must also preserve the choice.
    records["turin-fixture"]["reviews_updated_at"] = "2026-11-05T17:00:00Z"
    records["cheyenne-test"]["listings_updated_at"] = "2026-11-05T17:01:00Z"
    app.run()
    assert not app.exception
    assert app.session_state["active_search_selection"] == "turin-fixture"


def test_search_delete_requires_confirmation_and_cancel_keeps_search(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    records = {"turin-fixture": {"name": "turin-fixture", "location": "Turin"}}
    _install_fake_search_catalog(monkeypatch, records)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: list(records))
    app = _app_test(browse=False)
    app.session_state["_checked_search_deletions"] = ["turin-fixture"]
    app.run()
    app.button(key="delete_search_button").click().run()
    assert "turin-fixture" in records
    app.button(key="cancel_delete_search").click().run()
    assert "turin-fixture" in records
    app.button(key="delete_search_button").click().run()
    app.button(key="confirm_delete_search").click().run()
    assert not app.exception
    assert "turin-fixture" not in records
    assert app.session_state["active_search_selection"] == "＋ New Search"
    assert any(widget.key == "new_destination" for widget in app.text_input)


def test_delete_selects_next_search_and_allows_successive_deletions(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    records = {name: {"name": name, "location": "Turin"} for name in
               ("first-search", "turin-fixture", "last-search")}
    _install_fake_search_catalog(monkeypatch, records)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: list(records))
    app = _app_test(browse=False)
    app.session_state["active_search_selection"] = "turin-fixture"
    app.run()
    for deleted, selected in [("turin-fixture", "last-search"), ("last-search", "first-search"),
                              ("first-search", "＋ New Search")]:
        app.session_state["_checked_search_deletions"] = [deleted]
        app.run()
        assert not app.button(key="delete_search_button").disabled
        app.button(key="delete_search_button").click().run()
        app.button(key="confirm_delete_search").click().run()
        assert not app.exception
        assert deleted not in records
        assert app.session_state["active_search_selection"] == selected
    assert app.button(key="delete_search_button").disabled


def test_new_search_checkout_tracks_moved_checkin_and_preserves_valid_future_checkout(monkeypatch, tmp_path):
    _isolated_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: [])
    monkeypatch.setattr(browser_app, "render_area_map", _fake_map)
    app = _app_test().run()
    target = max(date(2027, 1, 19), date.today() + timedelta(days=90))

    app.date_input(key="new_check_in").set_value(target).run()
    assert not app.exception
    checkout = app.date_input(key="new_check_out").value
    assert checkout >= target + timedelta(days=1)

    farther = target + timedelta(days=31)
    app.date_input(key="new_check_in").set_value(farther).run()
    assert not app.exception
    assert app.date_input(key="new_check_out").value == farther + timedelta(days=5)

    app.date_input(key="new_check_in").set_value(target).run()
    assert not app.exception
    assert app.date_input(key="new_check_out").value == farther + timedelta(days=5)


def test_existing_trip_editor_checkout_tracks_moved_checkin(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test().run()
    target = max(date(2027, 1, 19), date.today() + timedelta(days=90))

    app.date_input(key="edit_checkin_turin-fixture").set_value(target).run()
    assert not app.exception
    assert app.date_input(key="edit_checkout_turin-fixture").value >= target + timedelta(days=1)

    farther = target + timedelta(days=31)
    app.date_input(key="edit_checkin_turin-fixture").set_value(farther).run()
    assert not app.exception
    assert app.date_input(key="edit_checkout_turin-fixture").value == farther + timedelta(days=5)

    app.date_input(key="edit_checkin_turin-fixture").set_value(target).run()
    assert not app.exception
    assert app.date_input(key="edit_checkout_turin-fixture").value == farther + timedelta(days=5)


def test_trip_editor_omits_budget_and_cancellation_search_controls(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test().run()
    assert not app.exception
    assert not any(widget.key == 'edit_free_cancellation_turin-fixture' for widget in app.checkbox)
    assert not any(widget.key == 'edit_budget_turin-fixture' for widget in app.number_input)


def test_fractional_config_zoom_recovers_center_only_state_and_discovery(monkeypatch, tmp_path):
    _, jobs = _setup_existing_app(monkeypatch, tmp_path)
    jobs.pipeline_status = {
        "next_stage": "discover",
        "active_job": None,
        "stages": {
            "discover": {"state": "ready", "count": 0, "total": 0, "completed": 0, "failed": 0, "missing": 1, "can_start": True, "reason": "Ready to discover."},
            "details": {"state": "blocked", "count": 0, "total": 0, "completed": 0, "failed": 0, "missing": 0, "can_start": False, "reason": "Discover listings first."},
            "evaluate": {"state": "blocked", "count": 0, "total": 0, "completed": 0, "failed": 0, "missing": 0, "can_start": False, "reason": "Complete details first."},
        },
    }
    monkeypatch.setattr(browser_app, "load_search_config", lambda name: {
        "location": "Turin", "currency": "EUR", "zoom_value": "18.352051374073234",
    })
    monkeypatch.setattr(review_app, "load_search_dataset", lambda name, *, enrich=True: pd.DataFrame())
    area_zooms = []
    monkeypatch.setattr(browser_app, "render_area_map", lambda *, zoom, **kwargs: area_zooms.append(zoom) or {
        "center": {"lat": 45.07, "lng": 7.68}, "zoom": zoom,
    })

    # A previous partial render may have saved the center but failed before zoom.
    app = _app_test()
    app.session_state["edit_map_center_turin-fixture"] = (45.08, 7.68)
    app.run()

    assert not app.exception
    assert app.session_state["edit_map_zoom_turin-fixture"] == 18
    assert area_zooms and area_zooms[0] == 18
    app.button(key="pipeline_step_turin-fixture_discover").click().run()
    assert not app.exception
    assert jobs.started == [("turin-fixture", "discover", "Default")]
    assert app.session_state["edit_map_zoom_turin-fixture"] == 18


def test_fractional_config_zoom_initializes_listing_map_with_center_only_state(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    monkeypatch.setattr(browser_app, "load_search_config", lambda name: {
        "location": "Turin", "currency": "EUR", "zoom_value": "18.352051374073234",
    })
    listing_zooms = []
    monkeypatch.setattr(browser_app, "render_listing_map", lambda listings, *, zoom, **kwargs: listing_zooms.append(zoom) or {})

    app = _app_test()
    app.session_state["map_center_turin-fixture"] = (45.08, 7.68)
    app.run()

    assert not app.exception
    assert listing_zooms and listing_zooms[0] == 18
    assert app.session_state["map_zoom_turin-fixture"] == 18


def test_pipeline_progress_gates_discovery_details_and_current_profile_evaluation(monkeypatch, tmp_path):
    _, jobs = _setup_existing_app(monkeypatch, tmp_path)
    has_dataset = {"value": False}
    monkeypatch.setattr(review_app, "load_search_dataset", lambda name, *, enrich=True: _sample_rows() if has_dataset["value"] else pd.DataFrame())
    jobs.pipeline_status = _pipeline_snapshot(
        _status_stage("ready", can_start=True, reason="Ready to discover listings."),
        _status_stage("blocked", reason="Discover current listings first."),
        _status_stage("blocked", reason="Complete current details and reviews first."),
        "discover",
    )
    jobs.after_start["discover"] = _pipeline_snapshot(
        _status_stage("success", count=153, total=153, completed=153),
        _status_stage("ready", total=153, missing=153, can_start=True, reason="Ready to fetch details and reviews."),
        _status_stage("blocked", total=153, missing=153, reason="Complete current details and reviews first."),
        "details",
    )
    jobs.after_start["details"] = _pipeline_snapshot(
        _status_stage("success", count=153, total=153, completed=153),
        _status_stage(
            "success", count=153, total=153, completed=153,
            details={"attempted": 153, "succeeded": 151, "unavailable": 2, "failed": 0, "missing": 0},
            reviews={"attempted": 151, "succeeded": 151, "failed": 0, "missing": 0},
        ),
        _status_stage("ready", total=151, missing=151, can_start=True, reason="Ready to evaluate current evidence."),
        "evaluate",
    )
    jobs.after_start["evaluate"] = _pipeline_snapshot(
        _status_stage("success", count=153, total=153, completed=153),
        _status_stage(
            "success", count=153, total=153, completed=153,
            details={"attempted": 153, "succeeded": 151, "unavailable": 2, "failed": 0, "missing": 0},
            reviews={"attempted": 151, "succeeded": 151, "failed": 0, "missing": 0},
        ),
        _status_stage("success", count=151, total=151, completed=151, evaluated_count=151, scored_count=140, insufficient_evidence_count=11),
        None,
    )
    def publish_discovery_snapshot(stage):
        if stage == "discover":
            has_dataset["value"] = True
            source_path = search_service.SEARCHES_ROOT / "turin-fixture" / "output_data" / "merged_df.csv"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("new snapshot", encoding="utf-8")

    jobs.on_start = publish_discovery_snapshot

    app = _app_test().run()
    assert not app.exception
    assert not app.button(key="pipeline_step_turin-fixture_discover").disabled
    assert app.button(key="pipeline_step_turin-fixture_details").disabled
    assert app.button(key="pipeline_step_turin-fixture_evaluate").disabled
    assert not any("Preparation is complete" in item.value for item in app.success)

    app.button(key="pipeline_step_turin-fixture_discover").click().run()
    assert not app.exception
    assert "153 listings found." in [item.value for item in app.caption]
    assert app.button(key="pipeline_step_turin-fixture_details").disabled
    from src.processing_service import save_selection
    save_selection(tmp_path / 'searches' / 'turin-fixture', browser_app._active_definition('turin-fixture'), {'max_total': None, 'include_unknown': True})
    app.run()
    assert not app.button(key="pipeline_step_turin-fixture_details").disabled
    assert app.button(key="pipeline_step_turin-fixture_evaluate").disabled

    app.button(key="pipeline_step_turin-fixture_details").click().run()
    assert not app.exception
    assert any("Details: 151 ready" in item.value for item in app.caption)
    assert any("Reviews: 151 ready" in item.value for item in app.caption)
    assert app.button(key="pipeline_step_turin-fixture_evaluate").disabled

    # Saving must be available in the main workflow even with priorities collapsed.
    save_key = "save_profile_turin-fixture"
    assert not app.main.button(key=save_key).disabled
    assert all(button.key != save_key for button in app.sidebar.button)
    priorities = next(item for item in app.expander if item.label == "AI evaluation priorities")
    assert priorities.proto.expanded
    assert all(button.key != save_key for button in priorities.button)
    assert any("Your search is saved" in item.value for item in app.main.info)
    app.button(key="save_profile_turin-fixture").click().run()
    assert not app.exception
    assert not app.button(key="pipeline_step_turin-fixture_evaluate").disabled
    app.button(key="pipeline_step_turin-fixture_evaluate").click().run()
    assert not app.exception
    assert any("Preparation is complete" in item.value for item in app.success)
    assert sum(item.value == "✅ Complete" for item in app.success) >= 3
    assert any("140 scored · 11 insufficient evidence" in item.value for item in app.caption)


def test_running_fragment_progress_and_partial_details_retry(monkeypatch, tmp_path):
    _, jobs = _setup_existing_app(monkeypatch, tmp_path)
    from src.processing_service import save_selection
    save_selection(tmp_path / 'searches' / 'turin-fixture', browser_app._active_definition('turin-fixture'), {'max_total': None, 'include_unknown': True})
    jobs.pipeline_status = _pipeline_snapshot(
        _status_stage("success", count=3, total=3, completed=3),
        _status_stage(
            "running", count=1, total=3, completed=1, failed=1, missing=1,
            details={"attempted": 2, "succeeded": 1, "unavailable": 0, "failed": 1, "missing": 1},
            reviews={"attempted": 1, "succeeded": 1, "failed": None, "unclassified_failures": 1, "missing": 2},
        ),
        _status_stage("blocked", total=3, missing=3, reason="Complete current details and reviews first."),
        "details",
    )
    jobs.pipeline_status["active_job"] = {
        "job_id": "running-job", "stage": "details", "current_stage": "details", "status": "running",
        "completed": 1, "total": 3, "message": "Fetching detail evidence.",
    }
    running = jobs.pipeline_status
    partial = _pipeline_snapshot(
        _status_stage("success", count=3, total=3, completed=3),
        _status_stage(
            "partial", count=2, total=3, completed=2, failed=1, missing=1, can_start=True,
            reason="Retry missing detail and review records.",
            details={"attempted": 3, "succeeded": 2, "unavailable": 0, "failed": 1, "missing": 1},
            reviews={"attempted": 2, "succeeded": 2, "failed": None, "unclassified_failures": 1, "missing": 1},
        ),
        _status_stage("blocked", total=3, missing=3, reason="Complete current details and reviews first."),
        "details",
    )
    jobs.pipeline_status = partial
    jobs.status_sequence = [running, partial]

    app = _app_test().run()
    assert not app.exception
    assert any("1 of 3 processed in this stage" in item.value for item in app.caption)
    assert any("Running now: Details" in item.value for item in app.info)

    app.button(key="poll_job_turin-fixture").click().run()
    assert not app.exception
    assert any("⚠️ Partial" in item.value for item in app.warning)
    assert any("1 unclassified failures" in item.value for item in app.caption)
    retry = app.button(key="pipeline_step_turin-fixture_details")
    assert not retry.disabled
    assert retry.label.startswith("Retry")
    retry.click().run()
    assert not app.exception
    assert jobs.started[-1] == ("turin-fixture", "details", "Default")


def test_terminal_fragment_refreshes_full_app_once(monkeypatch, tmp_path):
    _, jobs = _setup_existing_app(monkeypatch, tmp_path)
    running = _pipeline_snapshot(
        _status_stage("success", count=3, total=3, completed=3),
        _status_stage("running", count=2, total=3, completed=2),
        _status_stage("blocked", total=3, missing=3, reason="Details are still running."),
        "details",
    )
    running["active_job"] = {"job_id": "running-job", "stage": "details", "current_stage": "details", "status": "running", "completed": 2, "total": 3, "message": "Collecting."}
    complete = _pipeline_snapshot(
        _status_stage("success", count=3, total=3, completed=3),
        _status_stage("success", count=3, total=3, completed=3),
        _status_stage("ready", total=3, missing=3, can_start=True, reason="Ready to evaluate."),
        "evaluate",
    )
    jobs.pipeline_status = complete
    jobs.status_sequence = [running, complete]
    load_count = {"value": 0}
    original_loader = review_app.load_search_dataset

    def count_loads(name, *, enrich=True):
        load_count["value"] += 1
        return original_loader(name, enrich=enrich)

    monkeypatch.setattr(review_app, "load_search_dataset", count_loads)
    app = _app_test().run()
    assert not app.exception
    assert app.session_state["_pipeline_was_active_turin-fixture"] is True

    app.button(key="poll_job_turin-fixture").click().run()
    assert not app.exception
    assert app.session_state["_pipeline_was_active_turin-fixture"] is False
    assert load_count["value"] == 1
    assert jobs.status_calls == 3


def test_prepared_snapshot_cache_skips_warm_work_and_invalidates_on_source_and_profile(monkeypatch, tmp_path):
    evaluation, _ = _setup_existing_app(monkeypatch, tmp_path)
    load_count = {"value": 0}
    prepare_count = {"value": 0}
    hydrate_count = {"value": 0}
    profile_changed = {"value": False}
    original_loader = review_app.load_search_dataset
    original_prepare = browser_app._prepare_dataset
    original_profile_loader = evaluation.load_evaluation_profile

    def count_loads(name, *, enrich=True):
        load_count["value"] += 1
        return original_loader(name, enrich=enrich)

    def count_prepares(frame, definition):
        prepare_count["value"] += 1
        return original_prepare(frame, definition)

    def hydrate(name, frame, profile_name="Default"):
        hydrate_count["value"] += 1
        return frame

    def load_profile(name, profile_name="Default"):
        profile = original_profile_loader(name, profile_name)
        if profile_changed["value"]:
            profile = dict(profile)
            profile["weights"] = dict(profile["weights"])
            profile["weights"]["quiet_sleep"] = 4
        return profile

    evaluation.hydrate_personal_evaluations = hydrate
    evaluation.load_evaluation_profile = load_profile
    monkeypatch.setattr(review_app, "load_search_dataset", count_loads)
    monkeypatch.setattr(browser_app, "_prepare_dataset", count_prepares)

    app = _app_test().run()
    assert not app.exception
    assert (load_count["value"], prepare_count["value"], hydrate_count["value"]) == (1, 1, 1)

    app.number_input(key="min_beds_turin-fixture").set_value(2).run()
    assert not app.exception
    curation_service.toggle_shortlist_property("turin-fixture", "fixture-101")
    app.run()
    assert not app.exception
    assert (load_count["value"], prepare_count["value"], hydrate_count["value"]) == (1, 1, 1)

    source_path = search_service.SEARCHES_ROOT / "turin-fixture" / "output_data" / "merged_df.csv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("changed merged source", encoding="utf-8")
    app.run()
    assert not app.exception
    assert (load_count["value"], prepare_count["value"], hydrate_count["value"]) == (2, 2, 2)

    profile_changed["value"] = True
    app.run()
    assert not app.exception
    assert (load_count["value"], prepare_count["value"], hydrate_count["value"]) == (3, 3, 3)


def test_snapshot_fingerprint_ignores_definition_audit_timestamps(monkeypatch, tmp_path):
    evaluation, _ = _setup_existing_app(monkeypatch, tmp_path)
    initial = browser_app._active_definition("turin-fixture")
    search_service.save_search_definition(
        "turin-fixture", initial, create=True, root=search_service.SEARCHES_ROOT,
    )
    first = search_service.load_search_definition("turin-fixture")
    second = search_service.load_search_definition("turin-fixture")
    assert first["updated_at"] == second["updated_at"]
    profile = evaluation.load_evaluation_profile("turin-fixture", "Default")

    first_key = browser_app._prepared_snapshot_key("turin-fixture", first, "Default", profile, evaluation)
    second_key = browser_app._prepared_snapshot_key("turin-fixture", second, "Default", profile, evaluation)
    changed_audit = dict(second, created_at="another audit time", updated_at="another update time")
    audit_key = browser_app._prepared_snapshot_key("turin-fixture", changed_audit, "Default", profile, evaluation)
    changed_trip = dict(second, location="Different neighborhood")
    changed_key = browser_app._prepared_snapshot_key("turin-fixture", changed_trip, "Default", profile, evaluation)

    assert first_key == second_key
    assert first_key == audit_key
    assert first_key != changed_key


def test_storage_revision_alone_invalidates_prepared_snapshot(monkeypatch, tmp_path):
    from src import storage_service

    _setup_existing_app(monkeypatch, tmp_path)
    source_path = search_service.SEARCHES_ROOT / "turin-fixture" / "output_data" / "merged_df.csv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"room_id": "old", "description": "legacy source"}]).to_csv(source_path, index=False)
    file_token_before = browser_app._file_signature(source_path)
    load_count = {"value": 0}
    original_loader = review_app.load_search_dataset

    def count_loads(name, *, enrich=True):
        load_count["value"] += 1
        return original_loader(name, enrich=enrich)

    monkeypatch.setattr(review_app, "load_search_dataset", count_loads)
    app = _app_test().run()
    assert not app.exception
    app.run()
    assert load_count["value"] == 1

    # Update the SQLite resource without exporting a compatibility CSV.
    storage_service.write_frame(
        source_path,
        pd.DataFrame([{"room_id": "new", "description": "SQLite-only source update"}]),
        export=False,
    )
    assert browser_app._file_signature(source_path) == file_token_before
    app.run()
    assert not app.exception
    assert load_count["value"] == 2


def test_initial_storage_import_transition_accepts_only_unchanged_revision_one():
    file_token = ("/workspace/searches/demo/output_data/merged_df.csv", 100, 123, 124, 7)
    unchanged_inputs = (
        ("resource", "frame:search_definition.json:1:50:120:121:8", "file", ("/workspace/searches/demo/search_definition.json", 50, 120, 121, 8)),
        ("resource", "legacy:evaluation_profiles.json:1:125:126:9", "file", ("/workspace/searches/demo/evaluation_profiles.json", 1, 125, 126, 9)),
        ("resource", "None", "file", ("/workspace/searches/demo/output_data/personal_evaluations.json", None)),
    )
    before_inputs = (
        ("resource", "legacy:output_data/merged_df.csv:100:123:124:7", "file", file_token),
        *unchanged_inputs,
    )
    after_revision_one_inputs = (
        ("resource", "frame:output_data/merged_df.csv:1:100:123:124:7", "file", file_token),
        *unchanged_inputs,
    )
    after_revision_two_inputs = (
        ("resource", "frame:output_data/merged_df.csv:2:100:123:124:7", "file", file_token),
        *unchanged_inputs,
    )
    changed_file = ("/workspace/searches/demo/output_data/merged_df.csv", 101, 123, 124, 7)
    after_changed_file_inputs = (
        ("resource", "frame:output_data/merged_df.csv:1:101:123:124:7", "file", changed_file),
        *unchanged_inputs,
    )

    def cache_key(inputs):
        return ("/workspace/searches/demo", "demo", 1, inputs, ("implementation",), "trip", "Default", "profile")

    before = cache_key(before_inputs)
    assert browser_app._is_initial_storage_import_transition(before, cache_key(after_revision_one_inputs))
    assert not browser_app._is_initial_storage_import_transition(before, cache_key(after_revision_two_inputs))
    assert not browser_app._is_initial_storage_import_transition(before, cache_key(after_changed_file_inputs))


def test_cache_skips_a_legacy_read_that_overlaps_revision_two_write(monkeypatch, tmp_path):
    from src import storage_service

    _setup_existing_app(monkeypatch, tmp_path)
    source_path = search_service.SEARCHES_ROOT / "turin-fixture" / "output_data" / "merged_df.csv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"room_id": "legacy", "description": "legacy source"}]).to_csv(source_path, index=False)
    load_count = {"value": 0}
    write_overlap = {"pending": True}
    sample_loader = review_app.load_search_dataset

    def load_with_storage_read(name, *, enrich=True):
        load_count["value"] += 1
        storage_service.read_frame(source_path)
        if write_overlap["pending"]:
            write_overlap["pending"] = False
            storage_service.write_frame(
                source_path,
                pd.DataFrame([{"room_id": "writer-update", "description": "overlapped write"}]),
                export=False,
            )
        return sample_loader(name, enrich=enrich)

    monkeypatch.setattr(review_app, "load_search_dataset", load_with_storage_read)
    app = _app_test().run()
    assert not app.exception
    assert load_count["value"] == 1

    # Revision 2 landed while the first frame was being built, so it must not
    # be cached under that newer token. A stable second read can then be cached.
    app.run()
    assert not app.exception
    assert load_count["value"] == 2
    app.run()
    assert not app.exception
    assert load_count["value"] == 2


def test_save_search_rerun_selects_new_workspace_safely(monkeypatch, tmp_path):
    _isolated_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(browser_app, "geocode_location", lambda query: None)
    monkeypatch.setattr(job_service, "start_search_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: pipeline_service.list_available_searches())
    feature = {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[[7.65, 45.05], [7.72, 45.05], [7.72, 45.09], [7.65, 45.09], [7.65, 45.05]]]},
    }
    monkeypatch.setattr(browser_app, "render_area_map", lambda **kwargs: {
        "all_drawings": [feature],
        "center": {"lat": 45.07, "lng": 7.68},
        "zoom": 13,
        "bounds": {"_southWest": {"lat": 45.05, "lng": 7.65}, "_northEast": {"lat": 45.09, "lng": 7.72}},
    })
    app = _app_test().run()
    app.text_input(key="new_destination").set_value("Turin")
    app.run()
    assert not app.exception
    app.button(key="save_new_search").click().run()

    assert not app.exception
    created_searches = pipeline_service.list_available_searches()
    assert len(created_searches) == 1
    assert search_service.load_search_definition(created_searches[0])["free_cancellation"] is False
    assert app.session_state["active_search_selection"] == created_searches[0]


def test_fixture_explorer_shows_verified_and_unknown_location_results_and_job_scope(monkeypatch, tmp_path):
    evaluation, jobs = _setup_existing_app(monkeypatch, tmp_path)
    from src.processing_service import save_selection
    save_selection(tmp_path / 'searches' / 'turin-fixture', browser_app._active_definition('turin-fixture'), {'max_total': None, 'include_unknown': True})

    app = _app_test().run()

    assert not app.exception
    rendered_text = "\n".join(element.value for element in app.markdown if hasattr(element, "value"))
    rendered_captions = "\n".join(element.value for element in app.caption if hasattr(element, "value"))
    assert "1 shown of 1 matching · 3 collected" in rendered_captions
    assert "Only positive, current Airbnb quoted amounts appear by default" in rendered_captions
    assert any(button.label == "Evaluate with AI" and button.disabled for button in app.button)
    app.checkbox(key="include_unknown_prices_turin-fixture").set_value(True).run()
    assert not app.exception
    rendered_captions = "\n".join(element.value for element in app.caption if hasattr(element, "value"))
    rendered_text = "\n".join(element.value for element in app.markdown if hasattr(element, "value"))
    assert "2 shown of 2 matching · 3 collected" in rendered_captions
    assert "Location unknown" in rendered_text
    app.selectbox(key="unknown_location_select_turin-fixture").set_value("fixture-202").run()
    assert not app.exception
    assert any("Loft with location not shared" in item.value for item in app.markdown)
    assert any("Private room · 1 bed · 1 bedroom · Unknown cancellation terms" in item.value for item in app.markdown)
    trip = browser_app._airbnb_url("fixture-202", browser_app._active_definition("turin-fixture"))
    assert "check_in=" in trip and "adults=2" in trip and "children=0" in trip and "pets=0" in trip
    app.button(key="hide_turin-fixture_fixture-202").click().run()
    assert not app.exception
    assert "fixture-202" in curation_service.load_user_curation("turin-fixture")["hidden_room_ids"]
    assert any(button.label == "Undo last hide" for button in app.button)
    app.button(key="undo_hide_turin-fixture").click().run()
    assert not app.exception
    assert "fixture-202" not in curation_service.load_user_curation("turin-fixture")["hidden_room_ids"]
    app.text_input(key="profile_name_turin-fixture_Default").set_value("Quiet")
    app.number_input(key="weight_turin-fixture_Default_quiet_sleep").set_value(5)
    app.text_area(key="instructions_turin-fixture_Default").set_value("Street noise matters most.")
    app.run()
    assert not app.exception
    assert app.button(key="pipeline_step_turin-fixture_evaluate").disabled
    app.button(key="save_profile_turin-fixture").click().run()
    assert not app.exception
    assert app.selectbox(key="profile_select_turin-fixture").value == "Quiet"
    assert app.number_input(key="weight_turin-fixture_Quiet_quiet_sleep").value == 5
    assert app.text_area(key="instructions_turin-fixture_Quiet").value == "Street noise matters most."
    assert any(button.label == "Evaluate with AI" and not button.disabled for button in app.button)
    app.button(key="pipeline_step_turin-fixture_evaluate").click().run()
    assert not app.exception
    assert jobs.started == [("turin-fixture", "evaluate", "Quiet")]
    assert any("Queued test job" in element.value for element in app.info if hasattr(element, "value"))


def test_map_marker_room_id_is_explicit_and_price_extraction_never_uses_nightly_rate():
    assert browser_app._explicit_total('{"unit":{"amount":120},"total":{"amount":840}}') == 840
    assert pd.isna(browser_app._explicit_total('{"unit":{"amount":120}}'))
    for invalid_total in (0, -10, float("nan"), float("inf"), -float("inf")):
        assert pd.isna(browser_app._explicit_total(invalid_total))
    assert browser_app.room_id_from_tooltip("BNBID::abc-123 · EUR 840 · A title") == "abc-123"
    assert browser_app.room_id_from_tooltip("some other tooltip 45.07,7.68") is None


def test_map_does_not_render_nonpositive_totals_as_a_price(monkeypatch):
    from src.ui import map_view

    captured = {}

    def capture_map(rendered_map, **kwargs):
        captured["map"] = rendered_map
        return {}

    monkeypatch.setattr(map_view, "st_folium", capture_map)
    map_view.render_listing_map(
        pd.DataFrame([{
            "room_id": "zero-total",
            "name": "Unknown final total",
            "latitude": 45.07,
            "longitude": 7.68,
            "verified_total_price": 0,
            "total_price": 0,
            "currency_ui": "EUR",
        }]),
        center=(45.07, 7.68),
        zoom=13,
        selected_room_id=None,
        search_definition={"currency": "EUR"},
        key="price-display-test",
    )
    html = captured["map"].get_root().render()

    assert "Airbnb quote unavailable" in html
    assert "EUR 0" not in html


def test_quote_copy_reports_airbnb_quote_context_and_unknown_fee_completeness():
    definition = {
        "check_in": (date.today() + timedelta(days=35)).isoformat(),
        "check_out": (date.today() + timedelta(days=42)).isoformat(),
        "currency": "EUR",
        "trip_revision": 1,
        "adults": 2,
        "children": 0,
        "infants": 0,
        "pets": 0,
    }
    sample = _sample_rows().iloc[[0]].copy()
    sample.loc[:, "quote_check_out"] = definition["check_out"]
    frame = browser_app._prepare_dataset(sample, definition)
    row = frame.iloc[0]

    label = browser_app._quote_label(row, "EUR", definition)
    metadata = browser_app._quote_metadata_lines(row)

    assert label == "Airbnb quoted stay total: EUR 840.00 · 7 nights · average EUR 120.00/night"
    assert any("Source: Airbnb detail response" in line for line in metadata)
    assert any("Taxes and fees: inclusion is not confirmed" in line for line in metadata)
    assert any("Retrieved: 2026-10-01T12:00:00+00:00" in line for line in metadata)


def test_search_geometry_excludes_known_outside_locations_but_keeps_unknowns():
    definition = {
        "location": "Turin",
        "check_in": (date.today() + timedelta(days=35)).isoformat(),
        "check_out": (date.today() + timedelta(days=40)).isoformat(),
        "adults": 2,
        "children": 0,
        "infants": 0,
        "pets": 0,
        "currency": "EUR",
        "trip_revision": 1,
        "geometry": {"type": "bbox", "south": 45.05, "west": 7.65, "north": 45.09, "east": 7.72},
    }
    frame = browser_app._prepare_dataset(_sample_rows(), definition)
    frame.loc[frame["room_id"].eq("fixture-101"), ["latitude", "longitude"]] = [46.0, 8.0]
    _, matching, _ = browser_app._filter_listings(
        frame,
        definition=definition,
        curation={"manual_order": [], "hidden_room_ids": [], "shortlisted_room_ids": []},
        party_size=1,
        bathroom_mode="Any",
        ac_mode="Any",
        washer_mode="Any",
        availability_mode="Available or unknown",
        min_price=0,
        max_price=5000,
        min_rating=0,
        sort_by="Manual Order",
        top_n=0,
        include_unknown_prices=True,
    )
    matching_ids = set(matching["room_id"].astype(str))

    assert "fixture-101" not in matching_ids
    assert "fixture-202" in matching_ids


def test_default_price_filter_excludes_nonpositive_and_unknown_totals_and_opt_in_sorts_unknown_last():
    definition = {
        "check_in": (date.today() + timedelta(days=35)).isoformat(),
        "check_out": (date.today() + timedelta(days=40)).isoformat(),
        "currency": "EUR",
        "trip_revision": 1,
        "adults": 2,
        "children": 0,
        "infants": 0,
        "pets": 0,
    }
    rows = _sample_rows().iloc[:1].copy()
    rows.loc[:, "room_id"] = "positive"
    invalid_rows = pd.DataFrame([
        {**rows.iloc[0].to_dict(), "room_id": "zero", "total_price": 0, "price": 0},
        {**rows.iloc[0].to_dict(), "room_id": "negative", "total_price": -25, "price": -25},
        {**rows.iloc[0].to_dict(), "room_id": "nan", "total_price": float("nan"), "price": float("nan"), "quote_status": "missing"},
        {**rows.iloc[0].to_dict(), "room_id": "infinite", "total_price": float("inf"), "price": float("inf")},
        {**rows.iloc[0].to_dict(), "room_id": "stale", "total_price": 600, "price": 600, "quote_status": "stale", "trip_revision_details": 0},
    ])
    frame = browser_app._prepare_dataset(pd.concat([rows, invalid_rows], ignore_index=True), definition)
    base = {
        "definition": definition,
        "curation": {"manual_order": [], "hidden_room_ids": [], "shortlisted_room_ids": []},
        "party_size": 1,
        "bathroom_mode": "Any",
        "ac_mode": "Any",
        "washer_mode": "Any",
        "availability_mode": "Available or unknown",
        "min_price": 0,
        "max_price": 5000,
        "min_rating": 0,
        "sort_by": "Total Price",
        "top_n": 10,
    }

    shown, matching, _ = browser_app._filter_listings(frame, **base)
    assert matching["room_id"].astype(str).tolist() == ["positive"]
    assert shown["room_id"].astype(str).tolist() == ["positive"]
    assert frame.loc[frame["room_id"].isin(["zero", "negative", "infinite"]), "total_price"].isna().all()

    shown, matching, _ = browser_app._filter_listings(frame, **base, include_unknown_prices=True)
    assert matching["room_id"].astype(str).tolist()[0] == "positive"
    assert set(matching["room_id"].astype(str)) == {"positive", "zero", "negative", "nan", "infinite", "stale"}
    assert shown["room_id"].astype(str).tolist()[0] == "positive"
    assert matching.loc[matching["room_id"].ne("positive"), "verified_total_price"].isna().all()


def test_home_type_bedroom_and_cancellation_filters_require_explicit_evidence():
    frame = pd.DataFrame([
        {"room_id": "entire", "accommodation_type": "Entire home/apt", "bed_count": 2, "bedroom_count": 1, "cancellation_state": "free"},
        {"room_id": "private", "accommodation_type": "Private room", "bed_count": 1, "bedroom_count": 1, "cancellation_state": "conditional"},
        {"room_id": "shared", "accommodation_type": "Shared room", "bed_count": 2, "bedroom_count": None, "cancellation_state": "not_free"},
        {"room_id": "hotel_studio", "accommodation_type": "Hotel room", "bed_count": 1, "bedroom_count": 0, "cancellation_state": "nonrefundable"},
        {"room_id": "unknown", "accommodation_type": "Unknown", "bed_count": None, "bedroom_count": None, "cancellation_state": "unknown"},
    ])
    frame["name"] = frame["room_id"]
    frame["latitude"] = np.nan
    frame["longitude"] = np.nan
    frame["person_capacity"] = 4
    frame["bathroom_privacy"] = "Undetermined"
    frame["bathroom_evidence"] = None
    frame["ac_state"] = "unknown"
    frame["washer_state"] = "unknown"
    frame["quote_status_ui"] = "current"
    frame["verified_total_price"] = 500.0
    frame["total_price"] = 500.0
    frame["Airbnb_rating"] = 4.8
    frame["is_shortlisted"] = False
    params = {
        "definition": None,
        "curation": {"manual_order": [], "hidden_room_ids": [], "shortlisted_room_ids": []},
        "party_size": 1,
        "bathroom_mode": "Any",
        "ac_mode": "Any",
        "washer_mode": "Any",
        "availability_mode": "Available or unknown",
        "min_price": 0,
        "max_price": 5000,
        "min_rating": 0,
        "sort_by": "Room ID",
        "top_n": 0,
    }

    _, selected, _ = browser_app._filter_listings(
        frame,
        **params,
        home_types=["Entire home/apt", "Private room"],
        min_beds=1,
        min_bedrooms=1,
    )
    assert set(selected["room_id"]) == {"entire", "private"}

    _, no_home_type, _ = browser_app._filter_listings(frame, **params, home_types=[])
    assert no_home_type.empty

    _, no_free_evidence, _ = browser_app._filter_listings(
        frame, **params, home_types=browser_app.HOME_TYPE_OPTIONS,
        cancellation_mode="Free cancellation offered",
    )
    assert set(no_free_evidence["room_id"]) == {"entire"}

    _, not_confirmed_free, _ = browser_app._filter_listings(
        frame, **params, home_types=browser_app.HOME_TYPE_OPTIONS,
        cancellation_mode="Not free or unknown",
    )
    assert set(not_confirmed_free["room_id"]) == {"private", "shared", "hotel_studio", "unknown"}

    _, zero_minimum, _ = browser_app._filter_listings(
        frame, **params, home_types=browser_app.HOME_TYPE_OPTIONS,
        min_beds=0, min_bedrooms=0,
    )
    assert "unknown" in set(zero_minimum["room_id"])

    _, include_unknown, _ = browser_app._filter_listings(
        frame, **params, home_types=browser_app.HOME_TYPE_OPTIONS,
        min_beds=2, min_bedrooms=1, include_unknown_bed_counts=True,
    )
    assert set(include_unknown["room_id"]) == {"entire", "shared", "unknown"}


def test_new_home_type_and_cancellation_filter_controls_render(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test().run()

    assert not app.exception
    assert app.multiselect(key="home_type_filter_turin-fixture").value == browser_app.HOME_TYPE_OPTIONS
    assert app.number_input(key="min_beds_turin-fixture").value == 0
    assert app.number_input(key="min_bedrooms_turin-fixture").value == 0
    assert app.selectbox(key="cancellation_filter_turin-fixture").value == "Any"
    assert app.checkbox(key="include_unknown_bed_counts_turin-fixture").value is False


def test_map_uses_quiet_background_and_attribution():
    from src.ui.map_view import TILE_ATTR, TILE_URL, _add_base_map

    map_view = _add_base_map((45.07, 7.68), 13)
    html = map_view.get_root().render()

    assert "Canvas/World_Light_Gray_Base/MapServer/tile/" in TILE_URL
    assert '"maxNativeZoom": 16' in html
    assert "OpenStreetMap contributors" in TILE_ATTR
    assert "https://www.openstreetmap.org/copyright" in TILE_ATTR
    assert TILE_URL in html
    assert '"maxZoom": 19' in html
    assert '"subdomains": ""' in html
    assert '"detectRetina": false' in html
    assert "OpenStreetMap contributors" in html
    assert "cartocdn" not in html.lower()


def test_drag_event_persists_visible_order_and_preserves_unshown_ids(monkeypatch, tmp_path):
    _isolated_roots(monkeypatch, tmp_path)
    all_ids = ["a", "b", "hidden", "c", "d"]
    curation_service.seed_manual_order("turin-fixture", all_ids)

    changed, signature = browser_app._persist_grid_drag_once(
        "turin-fixture",
        {"type": "rowDragEnd"},
        ["c", "a"],
        all_ids,
    )
    repeated, repeated_signature = browser_app._persist_grid_drag_once(
        "turin-fixture",
        {"type": "rowDragEnd"},
        ["c", "a"],
        all_ids,
        previous_signature=signature,
    )
    unchanged, _ = browser_app._persist_grid_drag_once(
        "turin-fixture",
        {"type": "selectionChanged"},
        ["a", "c"],
        all_ids,
    )

    assert changed is True
    assert repeated is False and repeated_signature == signature
    assert unchanged is False
    persisted = curation_service.load_user_curation("turin-fixture")["manual_order"]
    assert persisted == ["c", "b", "hidden", "a", "d"]
    assert [item for item in persisted if item in {"b", "hidden", "d"}] == ["b", "hidden", "d"]


def test_marker_click_selects_same_id_in_grid_and_details(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    captured_grid = []
    captured_map = []
    first_map_event = {"pending": True}

    class EmptyGridResponse:
        event_data = None
        rows_id_after_sort_and_filter = []
        selected_rows = None

    def fake_grid(data, **kwargs):
        captured_grid.append(kwargs)
        return EmptyGridResponse()

    def fake_listing_map(listings, **kwargs):
        captured_map.append(kwargs)
        if first_map_event["pending"]:
            first_map_event["pending"] = False
            return {"last_object_clicked_tooltip": "BNBID::fixture-101 · EUR 840 · Bright apartment"}
        return {}

    monkeypatch.setattr(browser_app, "AgGrid", fake_grid)
    monkeypatch.setattr(browser_app, "render_listing_map", fake_listing_map)
    app = _app_test().run()

    assert not app.exception
    assert any(kwargs.get("selected_room_id") == "fixture-101" for kwargs in captured_map)
    selected_grid = [kwargs for kwargs in captured_grid if "fixture-101" in str(kwargs.get("key", ""))]
    assert selected_grid
    row_style = selected_grid[-1]["gridOptions"]["getRowStyle"].js_code
    assert "fixture-101" in row_style
    assert any("Bright apartment near transit" in item.value for item in app.markdown)


def test_new_search_form_geocoding_and_map_locking(monkeypatch, tmp_path):
    _isolated_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(browser_app, "list_available_searches", lambda: [])
    monkeypatch.setattr(browser_app, "render_area_map", lambda *, center=(45.07, 7.68), **kwargs: {
        "center": {"lat": center[0], "lng": center[1]},
        "zoom": 13,
        "bounds": {"_southWest": {"lat": 45.05, "lng": 7.65}, "_northEast": {"lat": 45.09, "lng": 7.72}},
    })
    monkeypatch.setattr(browser_app, "geocode_location", lambda query: {
        "lat": 45.067, "lng": 7.682, "zoom": 13,
        "bounds": {"south": 45.05, "north": 45.09, "west": 7.65, "east": 7.72},
        "display_name": "Turin, Italy",
    })

    app = _app_test().run()
    assert not app.exception

    # Initially before entering location, date input is disabled
    date_widget = [w for w in app.date_input if w.key == "new_check_in"][0]
    assert date_widget.disabled is True

    # Enter destination and click Find
    app.text_input(key="new_destination").set_value("Turin").run()
    app.button(key="new_find_location").click().run()
    assert not app.exception
    assert app.session_state["new_map_center"] == (45.067, 7.682)

    # After location is entered, inputs are enabled
    date_widget = [w for w in app.date_input if w.key == "new_check_in"][0]
    assert date_widget.disabled is False

    # Click Use Map Area to lock
    app.button(key="new_use_visible_area").click().run()
    assert not app.exception
    assert app.session_state["new_map_area_locked"] is True

    # Unlock Map Area
    app.button(key="new_unlock_area").click().run()
    assert not app.exception
    assert app.session_state["new_map_area_locked"] is False



def test_preparation_view_exports_standalone_instead_of_rendering_browser(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test(browse=False).run()
    assert not app.exception
    assert any(item.value == "4. Review and share" for item in app.subheader)
    assert app.text_input(key="report_title_turin-fixture").value
    assert app.selectbox(key="report_scope_turin-fixture").value == "All properties"
    assert len(app.get("download_button")) == 1
    assert not any(item.label == "Minimum beds" for item in app.number_input)
    assert not any(item.value == "#### Map" for item in app.markdown)
    app.selectbox(key="report_scope_turin-fixture").set_value("Shortlisted properties").run()
    assert not app.exception
    assert any("No properties match this export selection" in item.value for item in app.info)


def test_run_all_saves_current_priorities_and_launches_without_separate_save(monkeypatch, tmp_path):
    evaluation, jobs = _setup_existing_app(monkeypatch, tmp_path)
    from src import storage_service
    rows = pd.DataFrame([{'room_id': 'fixture-101', 'trip_revision': 1, 'search_quote_total': 840,
                         'search_quote_check_in': browser_app._active_definition('turin-fixture')['check_in'], 'search_quote_check_out': browser_app._active_definition('turin-fixture')['check_out'],
                         'search_quote_currency': 'EUR', 'search_quote_party_supported': True}])
    storage_service.write_frame(tmp_path / 'searches' / 'turin-fixture' / 'output_data' / 'results_df.csv', rows)
    app = _app_test(browse=False).run()
    assert not app.button(key='run_all_turin-fixture').disabled
    app.number_input(key='weight_turin-fixture_Default_cleanliness').set_value(5).run()
    app.button(key='run_all_turin-fixture').click().run()
    assert not app.exception
    assert evaluation.saved == ['Default']
    assert jobs.started == [('turin-fixture', 'prepare', 'Default')]
    saved = evaluation.load_evaluation_profile('turin-fixture')
    assert saved['weights']['cleanliness'] == 5
    from src.processing_service import load_selection
    selection = load_selection(tmp_path / 'searches' / 'turin-fixture', browser_app._active_definition('turin-fixture'))
    assert selection['max_total'] == 850
    assert selection['trip_revision'] == 1


def test_reusable_profile_ui_defaults_to_newest_and_can_save_copy_and_delete(monkeypatch, tmp_path):
    _, jobs = _setup_existing_app(monkeypatch, tmp_path)
    evaluation_service.save_priority_profile('City', {'cleanliness': 4})
    evaluation_service.save_priority_profile('Beach', {'cleanliness': 2})
    monkeypatch.setattr(browser_app, '_service', lambda name: evaluation_service if name.endswith('evaluation_service') else jobs)
    app = _app_test(browse=False).run()
    assert not app.exception
    assert app.selectbox(key='profile_select_turin-fixture').value == 'Beach'
    assert app.number_input(key='weight_turin-fixture_Beach_cleanliness').value == 2
    assert not any(slider.key and slider.key.startswith('weight_') for slider in app.slider)
    app.text_input(key='profile_name_turin-fixture_Beach').set_value('Enrica').run()
    app.number_input(key='weight_turin-fixture_Beach_cleanliness').set_value(5).run()
    app.button(key='save_profile_turin-fixture').click().run()
    assert not app.exception
    assert app.selectbox(key='profile_select_turin-fixture').value == 'Enrica'
    assert evaluation_service.list_priority_profiles()[0]['name'] == 'Enrica'
    assert evaluation_service.load_priority_profile('Beach')['weights']['cleanliness'] == 2
    app.button(key='delete_profile_turin-fixture').click().run()
    app.button(key='confirm_delete_profile_turin-fixture').click().run()
    assert not app.exception
    assert app.selectbox(key='profile_select_turin-fixture').value == 'Beach'
    assert 'Enrica' not in [profile['name'] for profile in evaluation_service.list_priority_profiles()]
    assert evaluation_service.load_evaluation_profile('turin-fixture', 'Enrica')['weights']['cleanliness'] == 5


def test_new_profile_elsewhere_does_not_reset_explicit_current_selection(monkeypatch, tmp_path):
    _, jobs = _setup_existing_app(monkeypatch, tmp_path)
    evaluation_service.save_priority_profile('City', {'cleanliness': 4})
    evaluation_service.save_priority_profile('Beach', {'cleanliness': 2})
    monkeypatch.setattr(browser_app, '_service', lambda name: evaluation_service if name.endswith('evaluation_service') else jobs)
    app = _app_test(browse=False).run()
    app.selectbox(key='profile_select_turin-fixture').set_value('City').run()
    assert not app.exception
    evaluation_service.save_priority_profile('Enrica', {'cleanliness': 5})
    app.run()
    assert not app.exception
    assert app.selectbox(key='profile_select_turin-fixture').value == 'City'


def test_retry_countdown_uses_live_deadline_and_never_promises_terminal_retry():
    job = {'status': 'running', 'request_wait': {'kind': 'retry', 'until': 1300},
           'collection_retry': {'state': 'waiting', 'round': 1, 'room_ids': ['a', 'b']}}
    assert '05:00' in browser_app._retry_countdown_message(job, now=1000)
    assert '04:59' in browser_app._retry_countdown_message(job, now=1001)
    assert 'round 1 of 2' in browser_app._retry_countdown_message(job, now=1000)
    assert '2 listing(s) remaining' in browser_app._retry_countdown_message(job, now=1000)
    assert 'retry is due' in browser_app._retry_countdown_message(job, now=1301)
    for status in ('completed', 'partial', 'cancelled', 'interrupted'):
        assert browser_app._retry_countdown_message(dict(job, status=status), now=1000) is None
    assert browser_app._retry_countdown_message(dict(job, request_wait=None), now=1000) is None


def test_retry_countdown_visible_in_running_stage(monkeypatch, tmp_path):
    import time
    _, jobs = _setup_existing_app(monkeypatch, tmp_path)
    jobs.pipeline_status = _pipeline_snapshot(
        _status_stage('success', count=3, total=3, completed=3),
        dict(_status_stage('running', count=1, total=3, completed=1, failed=1), rate_limited=True),
        _status_stage('blocked', total=3), 'details')
    jobs.pipeline_status['active_job'] = {
        'job_id': 'cooldown-job', 'stage': 'prepare', 'current_stage': 'details', 'status': 'running',
        'request_wait': {'kind': 'retry', 'until': time.time()+300},
        'collection_retry': {'state': 'waiting', 'round': 1, 'room_ids': ['a', 'b']}}
    app = _app_test().run()
    assert not app.exception
    assert any('Automatic retry in' in item.value and 'listing(s) remaining' in item.value for item in app.warning)
    assert all('check the run status' not in item.value for item in app.warning)


def test_workflow_has_one_numbered_sequence_with_updates_in_context(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test(browse=False).run()
    assert not app.exception
    headings = [item.value for item in app.subheader if item.value[:1].isdigit()]
    assert headings == ['1. Search area and listings','2. Budget and priorities',
                        '3. Prepare selected properties','4. Review and share']
    assert not any('1.5' in str(item.value) for item in app.markdown)
    assert any(item.label == 'Update collected prices' for item in app.expander)
    assert not any(item.label == 'Other collection actions' for item in app.expander)
    assert not any(item.label == 'Refresh discovery only' for item in app.button)
    # Discovery refresh is now above budget and preparation; no duplicate numbered cards.
    assert app.button(key='rediscover_turin-fixture').label == 'Refresh listings + prices'
    assert not any(item.value.startswith('**1.') for item in app.markdown)


def test_refresh_discovery_is_available_independently_of_preparation(monkeypatch, tmp_path):
    _, jobs = _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test(browse=False).run()
    app.button(key='rediscover_turin-fixture').click().run()
    assert not app.exception
    assert jobs.started[-1][1] == 'discover'


def test_update_prices_requires_saved_selection_and_does_not_start_ai(monkeypatch, tmp_path):
    _, jobs = _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test(browse=False).run()
    assert app.button(key='start_turin-fixture_refresh').disabled
    from src.processing_service import save_selection
    save_selection(tmp_path/'searches/turin-fixture', browser_app._active_definition('turin-fixture'),
                   {'max_total':700,'include_unknown':False})
    app.run()
    assert not app.exception
    assert not app.button(key='start_turin-fixture_refresh').disabled
    app.button(key='start_turin-fixture_refresh').click().run()
    assert not app.exception
    assert [stage for _,stage,_ in jobs.started] == ['refresh']


def test_default_browser_sort_is_ai_fit_descending_with_unrated_last(monkeypatch, tmp_path):
    _setup_existing_app(monkeypatch, tmp_path)
    app = _app_test().run()
    assert not app.exception
    assert app.selectbox(key='sort_choice_turin-fixture').value == 'AI Fit'
    frame = pd.DataFrame([
        {'room_id':'unrated','personal_fit':None,'evaluation_score':5,'personal_fit_status':'unknown'},
        {'room_id':'lower','personal_fit':3,'evaluation_score':5,'personal_fit_status':'scored'},
        {'room_id':'higher','personal_fit':4.8,'evaluation_score':1,'personal_fit_status':'scored'},
        {'room_id':'stale','personal_fit':5,'evaluation_score':5,'personal_fit_status':'stale'}])
    assert browser_app._auto_sort(frame,'AI Fit')['room_id'].tolist() == ['higher','lower','stale','unrated']
