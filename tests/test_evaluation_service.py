"""Tests for profile persistence and evidence-backed personal fit evaluation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import src.evaluation_service as evaluation
from src.search_service import save_search_definition


class EvaluationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_root = evaluation.PROJECT_ROOT
        evaluation.PROJECT_ROOT = self.root
        self.search_name = "Legacy Search"
        self.output_dir = self.root / "searches" / self.search_name / "output_data"
        self.output_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        evaluation.PROJECT_ROOT = self.original_root
        self.temp_dir.cleanup()

    def write_merged(self, rows: list[dict]) -> None:
        pd.DataFrame(rows).to_csv(self.output_dir / "merged_df.csv", index=False)

    def save_current_trip(self) -> dict:
        check_in = date.today() + timedelta(days=10)
        values = {
            "location": "Turin",
            "check_in": check_in.isoformat(),
            "check_out": (check_in + timedelta(days=3)).isoformat(),
            "adults": 2,
            "children": 0,
            "infants": 0,
            "pets": 0,
            "currency": "EUR",
            "geometry": {
                "type": "bbox", "south": 45.0, "west": 7.0, "north": 45.1, "east": 7.1,
            },
        }
        return save_search_definition(
            self.search_name,
            values,
            root=self.root / "searches",
        )

    def listing_row(self, room_id: str = "123") -> dict:
        return {
            "room_id": room_id,
            "name": "Quiet flat near the station",
            "description": "The bedroom faces a quiet courtyard.",
            "amenities": json.dumps(["Wi-Fi", "Kitchen"]),
            "total_price": 640,
            "AI_rating": 2.5,
            "AI_review_summary": "Legacy summary must remain untouched.",
            "reviews_json": json.dumps([
                {
                    "id": "review-77",
                    "created_at": "2025-06-01",
                    "rating": 5,
                    "comments": "The apartment was quiet and clean, and the bed was comfortable.",
                },
                {
                    "id": "review-78",
                    "created_at": "2025-05-10",
                    "rating": 4,
                    "comments": "Easy walk to transit and restaurants.",
                },
            ]),
            "reviews_retrieved_count": 2,
        }

    def fake_provider(self, calls: list[dict], *, bad_source: bool = False):
        def provider(payload: dict) -> dict:
            calls.append(payload)
            request = json.loads(payload["messages"][1]["content"])
            evidence = request["evidence"]
            source_id = evidence["reviews"][0]["source_id"]
            quote = evidence["reviews"][0]["text"][:40]
            assessments = {}
            scores = {"cleanliness": 5, "quiet_sleep": 2, "beds": 4}
            for criterion in (entry["key"] for entry in request["profile"]["criteria"]):
                assessments[criterion] = {
                    "finding": "positive",
                    "score": scores.get(criterion, 4),
                    "rationale": "Supported by the cited guest report.",
                    "evidence": [{
                        "source_id": "review:missing" if bad_source else source_id,
                        "quote": quote,
                    }],
                }
            return {"assessments": assessments}

        return provider

    def current_detail_quote_row(
        self,
        room_id: str,
        price_payload: dict,
        total_price: float,
        *,
        trip: dict | None = None,
    ) -> dict:
        trip = trip or self.save_current_trip()
        price_text = json.dumps(price_payload, ensure_ascii=False)
        row = self.listing_row(room_id)
        row.update({
            "price": price_text,
            "price_details": price_text,
            "total_price": total_price,
            "trip_revision": trip["trip_revision"],
            "trip_revision_details": trip["trip_revision"],
            "quote_status": "current",
            "quote_source": "details:price.break_down.Total",
            "quote_total_completeness": "provider_breakdown_total",
            "quote_attempt_status": "success",
            "availability_state": "available",
            "details_fetched_at": "2026-10-30T12:00:00+00:00",
            "quote_party_supported": True,
            "quote_adults": trip["adults"],
            "quote_children": trip["children"],
            "quote_infants": trip["infants"],
            "quote_pets": trip["pets"],
            "quote_check_in": trip["check_in"],
            "quote_check_out": trip["check_out"],
            "quote_currency": trip["currency"],
        })
        return row

    def test_threshold_and_discovery_price_changes_reuse_recent_assessment(self) -> None:
        from datetime import datetime, timezone
        from src.processing_service import save_selection
        trip = self.save_current_trip()
        row = self.current_detail_quote_row('123', {'total': {'amount': 640}}, 640, trip=trip)
        row['total_price_details'] = 640
        row['details_fetched_at'] = datetime.now(timezone.utc).isoformat()
        row.update({'search_quote_total': 640, 'search_quote_check_in': trip['check_in'],
                    'search_quote_check_out': trip['check_out'], 'search_quote_currency': 'EUR',
                    'search_quote_party_supported': True, 'discovered_at': row['details_fetched_at']})
        save_selection(self.root / 'searches' / self.search_name, trip, {'max_total': 700})
        self.write_merged([row])
        calls = []
        provider = self.fake_provider(calls)
        first = evaluation.run_personalized_evaluation(self.search_name, provider=provider)
        self.assertEqual(first['status'], 'success')
        self.assertEqual(len(calls), 1)
        save_selection(self.root / 'searches' / self.search_name, trip, {'max_total': 1000})
        row['search_quote_total'] = 800
        row['total_price'] = 800
        row['price'] = json.dumps({'total': {'amount': 800}})
        row['discovered_at'] = datetime.now(timezone.utc).isoformat()
        self.write_merged([row])
        second = evaluation.run_personalized_evaluation(self.search_name, provider=provider)
        self.assertEqual(second['status'], 'success')
        self.assertEqual(len(calls), 1)
        hydrated = evaluation.hydrate_personal_evaluations(self.search_name, pd.DataFrame([row]))
        self.assertNotEqual(hydrated.iloc[0]['personal_fit_status'], 'stale')

    def test_profile_persistence_and_search_path_validation(self) -> None:
        profile = evaluation.load_evaluation_profile(self.search_name)
        self.assertEqual(profile["name"], "Default")
        self.assertEqual(profile["model_name"], "gpt-6-luna")
        self.assertEqual(profile["reasoning_effort"], None)

        saved = evaluation.save_evaluation_profile(
            self.search_name,
            "Family trip",
            {"cleanliness": 5, "quiet_sleep": 4},
            "Noise matters more than decor.",
            {"minimum_capacity": 2, "ac_mode": "present"},
            model_name="gpt-6-luna",
            reasoning_effort="high",
        )
        loaded = evaluation.load_evaluation_profile(self.search_name, "Family trip")
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(loaded["weights"]["cleanliness"], 5)
        self.assertEqual(loaded["additional_instructions"], "Noise matters more than decor.")
        self.assertEqual(loaded["hard_filters"]["minimum_capacity"], 2)
        self.assertEqual(loaded["reasoning_effort"], "high")
        evaluation.save_evaluation_profile(self.search_name, "Family trip", {"cleanliness": 4})
        self.assertEqual(evaluation.load_evaluation_profile(self.search_name, "Family trip")["reasoning_effort"], "high")
        evaluation.save_evaluation_profile(self.search_name, "Family trip", {"cleanliness": 4}, reasoning_effort=None)
        self.assertIsNone(evaluation.load_evaluation_profile(self.search_name, "Family trip")["reasoning_effort"])
        with self.assertRaises(ValueError):
            evaluation.save_evaluation_profile("../outside", "Default", {})

    def test_fingerprints_separate_weight_reranking_from_assessment_inputs(self) -> None:
        profile = evaluation.default_evaluation_profile()
        source_hash = evaluation.source_fingerprint({"review": "Quiet room"})
        changed_weights = {**profile, "weights": {**profile["weights"], "cleanliness": 5}}
        self.assertNotEqual(evaluation.profile_fingerprint(profile), evaluation.profile_fingerprint(changed_weights))
        self.assertEqual(
            evaluation.assessment_fingerprint(profile, source_hash),
            evaluation.assessment_fingerprint(changed_weights, source_hash),
        )
        changed_instructions = {**profile, "additional_instructions": "Stairs are acceptable."}
        self.assertNotEqual(
            evaluation.assessment_fingerprint(profile, source_hash),
            evaluation.assessment_fingerprint(changed_instructions, source_hash),
        )

    def test_weighted_score_requires_evidence_and_excludes_zero_weights(self) -> None:
        profile = {
            "name": "Default",
            "weights": {"cleanliness": 4, "quiet_sleep": 0, "beds": 2},
        }
        scored = evaluation.score_assessments({
            "cleanliness": {
                "finding": "positive", "score": 5,
                "evidence": [{"source_id": "review:1", "quote": "clean"}],
            },
            "quiet_sleep": {"finding": "negative", "score": 1, "evidence": [{"source_id": "review:1", "quote": "loud"}]},
            "beds": {"finding": "positive", "score": 4, "evidence": []},
        }, profile)
        self.assertEqual(scored["personal_fit"], 5.0)
        self.assertAlmostEqual(scored["coverage"], 4 / 6)
        self.assertEqual(scored["breakdown"]["quiet_sleep"]["weight"], 0)
        self.assertEqual(scored["breakdown"]["quiet_sleep"]["score"], 1)

        zero = evaluation.score_assessments({}, {"weights": {"cleanliness": 0, "beds": 0}})
        self.assertIsNone(zero["personal_fit"])
        self.assertEqual(zero["coverage"], 0)
        self.assertEqual(zero["status"], "no_personalized_score")

    def test_evaluation_cites_raw_reviews_and_preserves_legacy_ai_columns(self) -> None:
        self.write_merged([self.listing_row()])
        calls: list[dict] = []
        profile = evaluation.load_evaluation_profile(self.search_name)
        profile["reasoning_effort"] = "high"
        result = evaluation.run_personalized_evaluation(
            self.search_name,
            profile_snapshot=profile,
            provider=self.fake_provider(calls),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["rated"], 1)
        self.assertEqual(len(calls), 1)
        payload = calls[0]
        self.assertEqual(payload["model"], profile["model_name"])
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertNotIn("Legacy summary must remain untouched", payload["messages"][1]["content"])

        merged = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        self.assertAlmostEqual(float(merged.loc[0, "personal_fit_score"]), 81 / 21, places=2)
        self.assertAlmostEqual(float(merged.loc[0, "personal_fit_coverage"]), 1.0)
        self.assertEqual(merged.loc[0, "AI_rating"], 2.5)
        self.assertEqual(merged.loc[0, "AI_review_summary"], "Legacy summary must remain untouched.")
        self.assertEqual(json.loads(merged.loc[0, "reviews_json"])[0]["id"], "review-77")
        breakdown = json.loads(merged.loc[0, "personal_fit_breakdown"])
        self.assertEqual(breakdown["cleanliness"]["evidence"][0]["source_id"], "review:review-77")
        self.assertIn("review_date_min", json.loads(merged.loc[0, "personal_fit_review_coverage"]))
        sidecar = json.loads((self.output_dir / "personal_evaluations.json").read_text(encoding="utf-8"))
        self.assertIn("review:review-77", json.dumps(sidecar))
        self.assertTrue(list((self.output_dir / "evaluation_runs").rglob("*.json")))

    def test_review_truncation_is_reported_and_full_source_changes_invalidate_cache(self) -> None:
        long_review = "A quiet apartment. " + ("more evidence " * 2500) + "tail-x"
        row = self.listing_row()
        row["reviews_json"] = json.dumps([{"id": "long-review", "comments": long_review}])
        self.write_merged([row])
        calls: list[dict] = []
        provider = self.fake_provider(calls)
        evaluation.run_personalized_evaluation(self.search_name, provider=provider)
        first_request = json.loads(calls[0]["messages"][1]["content"])
        self.assertTrue(first_request["evidence"]["review_coverage"]["reviews_truncated"])
        self.assertLess(len(first_request["evidence"]["reviews"][0]["text"]), len(long_review))

        # Change only the part beyond the prompt cap; source fingerprint still invalidates.
        row["reviews_json"] = json.dumps([{"id": "long-review", "comments": long_review[:-6] + "tail-y"}])
        self.write_merged([row])
        evaluation.run_personalized_evaluation(self.search_name, provider=provider)
        self.assertEqual(len(calls), 2)

    def test_weight_only_edit_uses_cached_assessments_and_reranks(self) -> None:
        self.write_merged([self.listing_row()])
        calls: list[dict] = []
        provider = self.fake_provider(calls)
        first_weights = {key: 0 for key in evaluation.DEFAULT_WEIGHTS}
        first_weights.update({"cleanliness": 5, "quiet_sleep": 1})
        evaluation.save_evaluation_profile(self.search_name, "Default", first_weights)
        first = evaluation.run_personalized_evaluation(self.search_name, provider=provider)
        first_df = pd.read_csv(self.output_dir / "merged_df.csv")
        first_score = float(first_df.loc[0, "personal_fit_score"])
        self.assertEqual(len(calls), 1)

        second_weights = {key: 0 for key in evaluation.DEFAULT_WEIGHTS}
        second_weights.update({"cleanliness": 1, "quiet_sleep": 5})
        evaluation.save_evaluation_profile(self.search_name, "Default", second_weights)
        second = evaluation.run_personalized_evaluation(self.search_name, provider=provider)
        second_df = pd.read_csv(self.output_dir / "merged_df.csv")
        second_score = float(second_df.loc[0, "personal_fit_score"])

        self.assertEqual(len(calls), 1)
        self.assertNotEqual(first_score, second_score)
        self.assertNotEqual(first["profile_fingerprint"], second["profile_fingerprint"])
        self.assertTrue(second_df.loc[0, "personal_fit_evaluation_fingerprint"])

    def test_bad_schema_or_source_reference_preserves_previous_evaluation(self) -> None:
        self.write_merged([self.listing_row()])
        success = evaluation.run_personalized_evaluation(
            self.search_name, provider=self.fake_provider([])
        )
        before = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        score_before = before.loc[0, "personal_fit_score"]
        summary_before = before.loc[0, "AI_review_summary"]

        changed = before.loc[0].to_dict()
        changed["reviews_json"] = json.dumps([{
            "id": "review-new", "created_at": "2025-07-01", "comments": "The bedroom has excellent curtains."
        }])
        self.write_merged([changed])
        failed = evaluation.run_personalized_evaluation(
            self.search_name, provider=self.fake_provider([], bad_source=True)
        )
        after = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        self.assertIn(failed["status"], {"failed", "partial"})
        self.assertEqual(failed["failed"], 1)
        self.assertEqual(after.loc[0, "personal_fit_score"], score_before)
        self.assertEqual(after.loc[0, "AI_review_summary"], summary_before)
        self.assertTrue(success["profile_fingerprint"])
        failure_files = list((self.output_dir / "evaluation_failures").glob("*.json"))
        self.assertEqual(len(failure_files), 1)
        failure_snapshot = json.loads(failure_files[0].read_text(encoding="utf-8"))
        self.assertIn("unknown source ID", failure_snapshot["failures"][0]["error"])

    def test_missing_key_blocks_without_request_or_overwrite(self) -> None:
        self.write_merged([self.listing_row()])
        with patch.dict(os.environ, {}, clear=True):
            result = evaluation.run_personalized_evaluation(self.search_name)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("OPENAI_API_KEY", result["message"])
        self.assertFalse((self.output_dir / "evaluation_runs").exists())
        merged = pd.read_csv(self.output_dir / "merged_df.csv")
        self.assertNotIn("personal_fit_score", merged.columns)

    def test_corrupt_current_sidecar_blocks_without_overwriting_it(self) -> None:
        self.write_merged([self.listing_row()])
        sidecar = self.output_dir / "personal_evaluations.json"
        sidecar.write_text("{broken", encoding="utf-8")
        calls: list[dict] = []
        result = evaluation.run_personalized_evaluation(
            self.search_name, provider=self.fake_provider(calls)
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(calls, [])
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "{broken")

    def test_zero_weight_profile_skips_ai_and_sets_no_score(self) -> None:
        self.write_merged([self.listing_row()])
        zero_weights = {key: 0 for key in evaluation.DEFAULT_WEIGHTS}
        evaluation.save_evaluation_profile(self.search_name, "Default", zero_weights)
        calls: list[dict] = []
        result = evaluation.run_personalized_evaluation(
            self.search_name, provider=self.fake_provider(calls)
        )
        self.assertEqual(result["status"], "no_personalized_score")
        self.assertEqual(calls, [])
        merged = pd.read_csv(self.output_dir / "merged_df.csv")
        self.assertTrue(pd.isna(merged.loc[0, "personal_fit_score"]))
        self.assertEqual(float(merged.loc[0, "personal_fit_coverage"]), 0.0)

    def test_invalid_or_nightly_only_price_is_omitted_from_ai_evidence(self) -> None:
        row = self.current_detail_quote_row(
            "zero",
            {
                "unit": {"amount": 100, "qualifier": "per night"},
                "total": {"amount": 0},
                "break_down": [
                    {"description": "Nightly price", "amount": 100},
                    {"description": "Subtotal", "amount": 400},
                ],
            },
            0,
            trip=self.save_current_trip(),
        )
        self.write_merged([row])
        original_bytes = (self.output_dir / "merged_df.csv").read_bytes()
        original_frame = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        normalized = evaluation._evaluation_quote_frame(self.search_name, original_frame)
        normalized_row = normalized.iloc[0].to_dict()
        evidence, source_map, _ = evaluation._build_evidence_bundle(
            normalized_row,
            evaluation._trip_context(self.search_name),
        )
        self.assertNotIn("price", evidence["listing"])
        self.assertNotIn("total_price", evidence["listing"])
        self.assertNotIn("listing:price", source_map)
        self.assertNotIn("listing:total_price", source_map)
        self.assertNotIn("$0", json.dumps(evidence["listing"]))
        self.assertNotIn("400", json.dumps(evidence["listing"]))
        self.assertEqual((self.output_dir / "merged_df.csv").read_bytes(), original_bytes)

        calls: list[dict] = []
        result = evaluation.run_personalized_evaluation(
            self.search_name,
            provider=self.fake_provider(calls),
        )
        self.assertEqual(result["status"], "success")
        request = json.loads(calls[0]["messages"][1]["content"])
        self.assertNotIn("price", request["evidence"]["listing"])
        self.assertNotIn("total_price", request["evidence"]["listing"])

    def test_zero_negative_nonfinite_and_boolean_totals_are_not_price_evidence(self) -> None:
        trip = self.save_current_trip()
        invalid_values = [0, -1, float("nan"), float("inf"), True]
        rows = [
            self.current_detail_quote_row(
                f"invalid-{index}",
                {
                    "unit": {"amount": 100, "qualifier": "per night"},
                    "total": {"amount": value},
                },
                value,
                trip=trip,
            )
            for index, value in enumerate(invalid_values)
        ]
        self.write_merged(rows)
        original = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        normalized = evaluation._evaluation_quote_frame(self.search_name, original)
        for _, row in normalized.iterrows():
            with self.subTest(room_id=row["room_id"]):
                evidence, source_map, _ = evaluation._build_evidence_bundle(
                    row.to_dict(), evaluation._trip_context(self.search_name)
                )
                self.assertNotIn("price", evidence["listing"])
                self.assertNotIn("total_price", evidence["listing"])
                self.assertNotIn("listing:price", source_map)
                self.assertNotIn("listing:total_price", source_map)

    def test_hydration_marks_only_legacy_zero_price_assessment_stale_without_writing(self) -> None:
        trip = self.save_current_trip()
        valid_payload = {
            "unit": {"amount": 160, "qualifier": "per night"},
            "total": {"amount": 640},
            "break_down": [{"description": "Total", "amount": 640}],
        }
        invalid_payload = {
            "unit": {"amount": 160, "qualifier": "per night"},
            "total": {"amount": 0},
            "break_down": [
                {"description": "Nightly price", "amount": 160},
                {"description": "Subtotal", "amount": 640},
            ],
        }
        positive = self.current_detail_quote_row("positive", valid_payload, 640, trip=trip)
        zero = self.current_detail_quote_row("zero", invalid_payload, 0, trip=trip)
        self.write_merged([positive, zero])

        # Seed legacy runs using the old unfiltered source fingerprint.
        calls: list[dict] = []
        with patch.object(
            evaluation,
            "_evaluation_quote_frame",
            side_effect=lambda search_name, frame: frame.copy(),
        ):
            result = evaluation.run_personalized_evaluation(
                self.search_name,
                provider=self.fake_provider(calls),
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(calls), 2)
        before = (self.output_dir / "merged_df.csv").read_bytes()

        frame = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        hydrated = evaluation.hydrate_personal_evaluations(self.search_name, frame)
        statuses = hydrated.set_index("room_id")
        self.assertEqual(statuses.loc["positive", "personal_fit_status"], "scored")
        self.assertEqual(statuses.loc["positive", "personal_fit_stale_reason"], "")
        self.assertEqual(statuses.loc["zero", "personal_fit_status"], "stale")
        self.assertEqual(statuses.loc["zero", "personal_fit_stale_reason"], "source_changed")
        self.assertEqual((self.output_dir / "merged_df.csv").read_bytes(), before)

    def test_evaluation_state_reopens_from_sqlite_after_exports_are_removed(self) -> None:
        evaluation.save_evaluation_profile(
            self.search_name,
            "Default",
            evaluation.default_evaluation_profile()["weights"],
        )
        self.write_merged([self.listing_row()])
        initial_calls: list[dict] = []
        first = evaluation.run_personalized_evaluation(
            self.search_name,
            provider=self.fake_provider(initial_calls),
        )
        self.assertEqual(first["status"], "success")
        self.assertEqual(len(initial_calls), 1)

        workspace = self.root / "searches" / self.search_name
        database_path = workspace / "analyzer.sqlite3"
        self.assertTrue(database_path.exists())
        compatibility_exports = [
            workspace / "evaluation_profiles.json",
            self.output_dir / "merged_df.csv",
            self.output_dir / "personal_evaluations.json",
            *self.output_dir.glob("evaluation_cache/**/*.json"),
            *self.output_dir.glob("evaluation_runs/**/*.json"),
        ]
        self.assertGreaterEqual(len(compatibility_exports), 5)
        for export_path in compatibility_exports:
            export_path.unlink()

        reopened_profile = evaluation.load_evaluation_profile(self.search_name)
        self.assertEqual(reopened_profile["name"], "Default")
        loaded_frame = evaluation._read_merged_data(self.output_dir)
        self.assertEqual(loaded_frame["room_id"].astype(str).tolist(), ["123"])
        hydrated = evaluation.hydrate_personal_evaluations(self.search_name, loaded_frame)
        self.assertEqual(hydrated.loc[0, "personal_fit_status"], "scored")

        rerun_calls: list[dict] = []
        second = evaluation.run_personalized_evaluation(
            self.search_name,
            provider=self.fake_provider(rerun_calls),
        )
        self.assertEqual(second["status"], "success")
        self.assertEqual(second["rated"], 1)
        self.assertEqual(rerun_calls, [])

    def test_current_trip_unavailable_rows_are_skipped_and_preserved(self) -> None:
        trip = self.save_current_trip()
        unavailable = self.listing_row("unavailable-1")
        unavailable.update({
            "trip_revision": trip["trip_revision"],
            "availability_state": "unavailable",
            "personal_fit_score": 3.25,
            "personal_fit_status": "scored",
        })
        available = self.listing_row("available-1")
        available.update({
            "trip_revision": trip["trip_revision"],
            "availability_state": "available",
        })
        self.write_merged([unavailable, available])
        calls: list[dict] = []
        result = evaluation.run_personalized_evaluation(
            self.search_name, provider=self.fake_provider(calls)
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["candidate_total"], 1)
        self.assertEqual(result["skipped_unavailable"], 1)
        self.assertEqual(len(calls), 1)
        request = json.loads(calls[0]["messages"][1]["content"])
        self.assertEqual(request["evidence"]["room_id"], "available-1")

        merged = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        unavailable_row = merged.loc[merged["room_id"].eq("unavailable-1")].iloc[0]
        self.assertEqual(unavailable_row["personal_fit_score"], 3.25)
        self.assertEqual(unavailable_row["personal_fit_status"], "scored")
        self.assertTrue(pd.notna(merged.loc[merged["room_id"].eq("available-1"), "personal_fit_score"]).all())

    def test_explicit_scope_preserves_unselected_rows_and_scores(self) -> None:
        rows = []
        for room_id, score in (("1", 1.25), ("2", 2.25), ("3", 3.25)):
            row = self.listing_row(room_id)
            row["personal_fit_score"] = score
            row["personal_fit_status"] = "scored"
            rows.append(row)
        self.write_merged(rows)
        calls: list[dict] = []
        result = evaluation.run_personalized_evaluation(
            self.search_name,
            provider=self.fake_provider(calls),
            eligible_room_ids=["2"],
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["candidate_total"], 1)
        self.assertEqual(result["skipped_scope"], 2)
        self.assertEqual(len(calls), 1)
        request = json.loads(calls[0]["messages"][1]["content"])
        self.assertEqual(request["evidence"]["room_id"], "2")

        merged = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        scores = dict(zip(merged["room_id"], merged["personal_fit_score"]))
        self.assertEqual(scores["1"], 1.25)
        self.assertEqual(scores["3"], 3.25)
        self.assertAlmostEqual(scores["2"], 81 / 21, places=2)

    def test_each_pending_run_persists_its_own_listing_sources(self) -> None:
        first = self.listing_row("first")
        first["reviews_json"] = json.dumps([{
            "id": "first-review", "rating": 5, "comments": "The bedroom was quiet and spotless."
        }])
        second = self.listing_row("second")
        second["reviews_json"] = json.dumps([{
            "id": "second-review", "rating": 2, "comments": "The bedroom was loud and dirty."
        }])
        rows = [first, second]
        self.write_merged(rows)
        frame = evaluation._evaluation_quote_frame(
            self.search_name,
            evaluation._read_merged_data(self.output_dir),
        )
        expected_sources = {
            str(row["room_id"]): evaluation._raw_source_record(row.to_dict(), None)
            for _, row in frame.iterrows()
        }
        saved_sources: dict[str, dict] = {}
        save_run = evaluation.save_evaluation_run

        def capture_saved_source(*args, **kwargs):
            saved_sources[str(args[1])] = args[3]
            return save_run(*args, **kwargs)

        calls: list[dict] = []
        with patch.object(evaluation, "save_evaluation_run", side_effect=capture_saved_source):
            result = evaluation.run_personalized_evaluation(
                self.search_name, provider=self.fake_provider(calls)
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(set(saved_sources), {"first", "second"})
        for room_id, expected in expected_sources.items():
            self.assertEqual(saved_sources[room_id], expected)
            matching_run = next(
                path for path in (self.output_dir / "evaluation_runs").rglob("*.json")
                if path.name.startswith(f"{room_id}-")
            )
            run = json.loads(matching_run.read_text(encoding="utf-8"))
            self.assertEqual(run["source_hash"], evaluation.source_fingerprint(expected))

        current_frame = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        hydrated = evaluation.hydrate_personal_evaluations(self.search_name, current_frame)
        self.assertTrue(hydrated["personal_fit_status"].eq("scored").all())
        self.assertFalse(hydrated["personal_fit_stale_reason"].fillna("").astype(str).str.len().gt(0).any())

        profile = evaluation.load_evaluation_profile(self.search_name)
        profile["weights"]["quiet_sleep"] = 5
        evaluation.save_evaluation_profile(
            self.search_name,
            "Default",
            profile["weights"],
            profile["additional_instructions"],
            profile["hard_filters"],
            model_name=profile["model_name"],
            reasoning_effort=profile["reasoning_effort"],
        )
        rerank_calls: list[dict] = []
        reranked = evaluation.run_personalized_evaluation(
            self.search_name, provider=self.fake_provider(rerank_calls)
        )
        self.assertEqual(reranked["status"], "success")
        self.assertEqual(reranked["rated"], 2)
        self.assertEqual(rerank_calls, [])

    def test_all_current_unavailable_returns_no_candidates_without_writes(self) -> None:
        trip = self.save_current_trip()
        row = self.listing_row("unavailable-only")
        row.update({
            "trip_revision": trip["trip_revision"],
            "trip_revision_details": trip["trip_revision"],
            "quote_status": "unavailable",
            "personal_fit_score": 4.25,
        })
        self.write_merged([row])
        before = (self.output_dir / "merged_df.csv").read_bytes()
        calls: list[dict] = []
        result = evaluation.run_personalized_evaluation(
            self.search_name, provider=self.fake_provider(calls)
        )

        self.assertEqual(result["status"], "no_candidates")
        self.assertEqual(result["rated"], 0)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["candidate_total"], 0)
        self.assertEqual(result["skipped_unavailable"], 1)
        self.assertEqual(calls, [])
        self.assertEqual((self.output_dir / "merged_df.csv").read_bytes(), before)

    def test_hydration_restores_sidecar_and_marks_changed_sources_stale(self) -> None:
        original = self.listing_row()
        self.write_merged([original])
        evaluation.run_personalized_evaluation(self.search_name, provider=self.fake_provider([]))

        # Collection rebuilds the merged CSV without personal_fit columns.
        self.write_merged([original])
        reloaded = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        hydrated = evaluation.hydrate_personal_evaluations(self.search_name, reloaded)
        self.assertEqual(hydrated.loc[0, "personal_fit_status"], "scored")
        self.assertAlmostEqual(float(hydrated.loc[0, "personal_fit_score"]), 81 / 21, places=2)

        changed = dict(original)
        changed["reviews_json"] = json.dumps([{
            "id": "new-review", "comments": "The street was loud all night."
        }])
        stale_frame = pd.DataFrame([changed])
        stale = evaluation.hydrate_personal_evaluations(self.search_name, stale_frame)
        self.assertEqual(stale.loc[0, "personal_fit_status"], "stale")
        self.assertEqual(stale.loc[0, "personal_fit_stale_reason"], "source_changed")

    def test_hydration_reranks_weight_only_changes_without_ai(self) -> None:
        self.write_merged([self.listing_row()])
        calls: list[dict] = []
        initial = {key: 0 for key in evaluation.DEFAULT_WEIGHTS}
        initial.update({"cleanliness": 5, "quiet_sleep": 1})
        evaluation.save_evaluation_profile(self.search_name, "Default", initial)
        evaluation.run_personalized_evaluation(self.search_name, provider=self.fake_provider(calls))
        old = pd.read_csv(self.output_dir / "merged_df.csv")
        old_score = float(old.loc[0, "personal_fit_score"])

        updated = {key: 0 for key in evaluation.DEFAULT_WEIGHTS}
        updated.update({"cleanliness": 1, "quiet_sleep": 5})
        evaluation.save_evaluation_profile(self.search_name, "Default", updated)
        reloaded = pd.read_csv(self.output_dir / "merged_df.csv", dtype={"room_id": str})
        hydrated = evaluation.hydrate_personal_evaluations(self.search_name, reloaded)
        self.assertEqual(len(calls), 1)
        self.assertEqual(hydrated.loc[0, "personal_fit_status"], "scored")
        self.assertNotEqual(float(hydrated.loc[0, "personal_fit_score"]), old_score)

    def test_cancellation_before_request_preserves_existing_file(self) -> None:
        self.write_merged([self.listing_row()])
        before = (self.output_dir / "merged_df.csv").read_bytes()
        calls: list[dict] = []
        result = evaluation.run_personalized_evaluation(
            self.search_name,
            provider=self.fake_provider(calls),
            should_cancel=lambda: True,
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(calls, [])
        self.assertEqual((self.output_dir / "merged_df.csv").read_bytes(), before)


    def test_rejected_ai_answer_moves_to_tail_then_recovers(self) -> None:
        self.write_merged([self.listing_row(rid) for rid in ['a','b','c']])
        order = []
        good = self.fake_provider([])
        bad = self.fake_provider([], bad_source=True)
        def provider(payload):
            rid = json.loads(payload['messages'][1]['content'])['evidence']['room_id']
            order.append(rid)
            source_ids = json.loads(payload['messages'][-1]['content'])['allowed_source_ids']
            self.assertIn('review:review-77', source_ids)
            if rid == 'a' and order.count('a') == 1:
                return bad(payload)
            return good(payload)
        result = evaluation.run_personalized_evaluation(self.search_name,provider=provider)
        self.assertEqual(order,['a','b','c','a'])
        self.assertEqual(result['rated'],3)
        self.assertEqual(result['failed'],0)
        self.assertEqual(result['recovered_after_retry'],1)
        self.assertEqual(result['request_attempts'],{'a':2,'b':1,'c':1})

    def test_invalid_answers_exhaust_three_attempts_without_blocking_other_listings(self) -> None:
        self.write_merged([self.listing_row(rid) for rid in ['a','b']])
        order = []
        good, bad = self.fake_provider([]), self.fake_provider([],bad_source=True)
        def provider(payload):
            rid = json.loads(payload['messages'][1]['content'])['evidence']['room_id']
            order.append(rid)
            return bad(payload) if rid == 'a' else good(payload)
        result = evaluation.run_personalized_evaluation(self.search_name,provider=provider)
        self.assertEqual(order,['a','b','a','a'])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['rated'],1)
        self.assertEqual(result['failed_room_ids'],['a'])
        self.assertEqual(result['failure_details'][0]['attempts'],3)
        self.assertEqual(result['unattempted_room_ids'],[])
        self.assertFalse(result['failure_details'][0]['retry_queued'])

    def test_temporary_network_failure_is_retried_after_other_listings(self) -> None:
        import requests
        self.write_merged([self.listing_row(rid) for rid in ['a','b']])
        order = []
        good = self.fake_provider([])
        def provider(payload):
            rid = json.loads(payload['messages'][1]['content'])['evidence']['room_id']
            order.append(rid)
            if rid == 'a' and order.count('a') == 1:
                raise requests.Timeout('temporary')
            return good(payload)
        result = evaluation.run_personalized_evaluation(self.search_name,provider=provider)
        self.assertEqual(order,['a','b','a'])
        self.assertEqual(result['status'],'success')

    def test_ai_rate_limit_honors_shared_cooldown_then_continues_queue(self) -> None:
        import requests
        from src import request_policy as rp
        self.write_merged([self.listing_row(rid) for rid in ['a','b']])
        order = []
        good = self.fake_provider([])
        policy = rp.RequestPolicy(rp.automatic_options())
        policy.wait = lambda *args: None
        wait_retry = policy.wait_retry
        def cooldown(seconds,message):
            order.append('cooldown')
            return wait_retry(seconds,message)
        policy.wait_retry = cooldown
        def provider(payload):
            rid = json.loads(payload['messages'][1]['content'])['evidence']['room_id']
            order.append(rid)
            if rid == 'a' and order.count('a') == 1:
                response = requests.Response()
                response.status_code=429
                response._content=b'{}'
                response.headers['Retry-After']='60'
                raise requests.HTTPError(response=response)
            return good(payload)
        with patch.object(rp,'_NEXT_REQUEST',{}):
            result = evaluation.run_personalized_evaluation(self.search_name,provider=provider,request_policy=policy)
        self.assertEqual(order,['a','cooldown','b','a'])
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['request_attempts']['a'],2)
        self.assertGreaterEqual(policy.retry_waited_seconds,59)

    def test_billing_quota_stops_batch_without_repeated_billable_attempts(self) -> None:
        import requests
        self.write_merged([self.listing_row(rid) for rid in ['a','b']])
        calls = []
        def provider(payload):
            calls.append(payload)
            response=requests.Response()
            response.status_code=429
            response._content=b'{"error":{"code":"insufficient_quota"}}'
            raise requests.HTTPError(response=response)
        result=evaluation.run_personalized_evaluation(self.search_name,provider=provider)
        self.assertEqual(len(calls),1)
        self.assertEqual(result['failed_room_ids'],['a'])
        self.assertEqual(result['unattempted_room_ids'],['b'])
        self.assertIn('billing quota',result['stop_reason'])

    def test_ai_cooldown_stops_at_retry_budget_and_preserves_remaining_queue(self) -> None:
        import requests
        from src import request_policy as rp
        self.write_merged([self.listing_row(rid) for rid in ['a','b']])
        policy=rp.RequestPolicy(rp.automatic_options())
        policy.wait=lambda *args:None
        calls=[]
        def provider(payload):
            calls.append(payload)
            response=requests.Response()
            response.status_code=429
            response._content=b'{}'
            response.headers['Retry-After']='3601'
            raise requests.HTTPError(response=response)
        with patch.object(rp,'_NEXT_REQUEST',{}):
            result=evaluation.run_personalized_evaluation(self.search_name,provider=provider,request_policy=policy)
        self.assertEqual(len(calls),1)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['unattempted_room_ids'],['b'])
        self.assertIn('budget',result['stop_reason'])


if __name__ == "__main__":
    unittest.main()
