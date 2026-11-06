"""Unit tests for feature_extractor module."""

import unittest
import pandas as pd
from src.feature_extractor import (
    extract_amenity_evidence,
    extract_bathroom_evidence,
    extract_bathroom_privacy,
    has_air_conditioning,
    has_clothes_washer,
    enrich_properties_dataframe,
    extract_accommodation_type_evidence,
    extract_bed_count_evidence,
    extract_bedroom_count_evidence,
    extract_cancellation_evidence,
)


class TestFeatureExtractor(unittest.TestCase):
    def test_entire_home_does_not_prove_bathroom_exclusivity(self):
        row = pd.Series({
            "room_type": "Entire home/apt",
            "name": "Chic 1-Bed Flat",
            "images_details": "[]"
        })
        self.assertEqual(extract_bathroom_privacy(row), "Undetermined")

    def test_shared_bedroom_does_not_prove_shared_bathroom(self):
        row = pd.Series({
            "room_type": "Shared room",
            "name": "Cozy shared room in London",
            "images_details": "[]"
        })
        self.assertEqual(extract_bathroom_privacy(row), "Undetermined")

    def test_bathroom_privacy_private_room_shared_image(self):
        row = pd.Series({
            "room_type": "Private room",
            "name": "Double Room near station",
            "images_details": "[{'title': 'Bedroom'}, {'title': 'Shared full bathroom'}]"
        })
        self.assertEqual(extract_bathroom_privacy(row), "Shared")

    def test_bathroom_privacy_private_room_ensuite_image(self):
        row = pd.Series({
            "room_type": "Private room",
            "name": "Double Room",
            "images_details": "[{'title': 'Bedroom'}, {'title': 'Private bathroom inside bedroom'}]"
        })
        self.assertEqual(extract_bathroom_privacy(row), "Private / Dedicated")

    def test_bathroom_privacy_private_room_name_keyword(self):
        row = pd.Series({
            "room_type": "Private room",
            "name": "Bright room with ensuite bathroom",
            "images_details": "[]"
        })
        self.assertEqual(extract_bathroom_privacy(row), "Private / Dedicated")

    def test_has_air_conditioning(self):
        row_with_ac = pd.Series({
            "amenities": "[{'title': 'Heating and cooling', 'values': [{'title': 'Central air conditioning', 'available': True}]}]",
            "name": "Modern Flat"
        })
        self.assertTrue(has_air_conditioning(row_with_ac))

        row_without_ac = pd.Series({
            "amenities": "[{'title': 'Heating and cooling', 'values': [{'title': 'Heating', 'available': True}]}]",
            "name": "Historic Cottage"
        })
        self.assertFalse(has_air_conditioning(row_without_ac))

    def test_has_clothes_washer(self):
        row_with_washer = pd.Series({
            "amenities": "[{'title': 'Laundry', 'values': [{'title': 'Free washer – In unit', 'available': True}]}]",
            "name": "City Studio"
        })
        self.assertTrue(has_clothes_washer(row_with_washer))

        row_with_dishwasher = pd.Series({
            "amenities": "[{'title': 'Kitchen', 'values': [{'title': 'Dishwasher', 'available': True}]}]",
            "name": "Chef Suite"
        })
        self.assertFalse(has_clothes_washer(row_with_dishwasher))

    def test_enrich_properties_dataframe(self):
        df = pd.DataFrame([
            {
                "room_id": "1",
                "room_type": "Entire home/apt",
                "name": "Flat with AC",
                "amenities": "[{'title': 'Cooling', 'values': [{'title': 'Air conditioning', 'available': True}]}]",
                "images_details": "[]"
            },
            {
                "room_id": "2",
                "room_type": "Private room",
                "name": "Room with shared bath and washer",
                "amenities": "[{'title': 'Laundry', 'values': [{'title': 'Washer', 'available': True}]}]",
                "images_details": "[{'title': 'Shared full bathroom'}]"
            }
        ])
        enriched = enrich_properties_dataframe(df)
        self.assertEqual(len(enriched), 2)
        self.assertEqual(enriched.iloc[0]["bathroom_privacy"], "Undetermined")
        self.assertTrue(enriched.iloc[0]["has_ac"])
        self.assertEqual(enriched.iloc[1]["bathroom_privacy"], "Shared")
        self.assertTrue(enriched.iloc[1]["has_washer"])

    def test_bathroom_external_private_and_conflicting_evidence(self):
        external = pd.Series({"description": "A private bathroom is located just outside the bedroom."})
        self.assertEqual(extract_bathroom_evidence(external)["state"], "external_exclusive")

        conflict = pd.Series({"description": "Private ensuite; the bathroom is shared with other guests."})
        fact = extract_bathroom_evidence(conflict)
        self.assertEqual(fact["state"], "conflict")
        self.assertTrue(fact["conflict"])

    def test_bathroom_negation_does_not_invent_shared_access(self):
        row = pd.Series({"description": "No ensuite, but there is a shared bathroom."})
        self.assertEqual(extract_bathroom_evidence(row)["state"], "shared")
        negated = pd.Series({"description": "The bathroom is not shared."})
        self.assertEqual(extract_bathroom_evidence(negated)["state"], "unknown")

    def test_amenity_evidence_unknown_absent_and_negation(self):
        missing = pd.Series({"amenities": [], "description": "Laundry access nearby."})
        self.assertEqual(extract_amenity_evidence(missing, "washer")["state"], "unknown")

        for description in (
            "The air conditioning is not available.",
            "No AC is provided.",
        ):
            fact = extract_amenity_evidence(pd.Series({"description": description}), "ac")
            self.assertEqual(fact["state"], "absent", description)

        for description in (
            "Washer unavailable.",
            "The washer is not included.",
            "Dishwasher is available, but washer is not.",
        ):
            fact = extract_amenity_evidence(pd.Series({"description": description}), "washer")
            self.assertEqual(fact["state"], "absent", description)

    def test_native_amenity_collections_and_explicit_availability(self):
        present = pd.Series({"amenities": [{"title": "Cooling", "values": [{"title": "Air conditioning", "available": True}]}]})
        absent = pd.Series({"amenities": [{"title": "Cooling", "values": [{"title": "Air conditioning", "available": False}]}]})
        self.assertEqual(extract_amenity_evidence(present, "ac")["state"], "present")
        self.assertEqual(extract_amenity_evidence(absent, "ac")["state"], "absent")
        self.assertEqual(extract_amenity_evidence(pd.Series({"amenities": ["Dishwasher"]}), "washer")["state"], "unknown")

    def test_accommodation_type_uses_room_type_only(self):
        self.assertEqual(
            extract_accommodation_type_evidence(pd.Series({"room_type": "Entire home/apt"}))["state"],
            "Entire home/apt",
        )
        self.assertEqual(
            extract_accommodation_type_evidence(pd.Series({"room_type": "Hotel room"}))["state"],
            "Hotel room",
        )
        generic = extract_accommodation_type_evidence(pd.Series({"type": "Entire home/apt"}))
        self.assertEqual(generic["state"], "Unknown")
        self.assertEqual(generic["evidence"], [])

    def test_structured_bedinfo_is_deduplicated_and_keeps_beds_separate_from_bedrooms(self):
        primary = [
            {"body": "1 bedroom", "type": "BEDINFO"},
            {"body": "1 king bed", "type": "BEDINFO"},
            {"body": "1 sofa bed", "type": "BEDINFO"},
        ]
        row = pd.Series({
            "name": "Chic 4-Bedroom Flat",
            "structuredContent": {"primaryLine": primary, "mapPrimaryLine": primary},
        })
        bedrooms = extract_bedroom_count_evidence(row)
        beds = extract_bed_count_evidence(row)
        self.assertEqual(bedrooms["state"], "known")
        self.assertEqual(bedrooms["value"], 1)
        self.assertEqual(beds["state"], "known")
        self.assertEqual(beds["value"], 2)
        self.assertEqual(len(beds["evidence"]), 2)

    def test_explicit_counts_studio_conflicts_and_missing_values(self):
        studio = pd.Series({"bedrooms": "Studio", "beds": 0})
        self.assertEqual(extract_bedroom_count_evidence(studio)["value"], 0)
        self.assertEqual(extract_bed_count_evidence(studio)["value"], 0)

        missing = pd.Series({"name": "Two Bedroom Apartment"})
        self.assertEqual(extract_bedroom_count_evidence(missing)["state"], "unknown")
        self.assertIsNone(extract_bed_count_evidence(missing)["value"])

        conflict = pd.Series({
            "bedroom_count": 1,
            "bedrooms": 2,
            "structuredContent": {"primaryLine": [{"body": "1 bedroom", "type": "BEDINFO"}]},
        })
        fact = extract_bedroom_count_evidence(conflict)
        self.assertEqual(fact["state"], "conflict")
        self.assertIsNone(fact["value"])

    def test_bed_count_includes_sofa_and_multidigit_counts(self):
        sofa = pd.Series({
            "structuredContent": {"primaryLine": [{"body": "1 sofa bed", "type": "BEDINFO"}]},
        })
        self.assertEqual(extract_bed_count_evidence(sofa)["value"], 1)
        many = pd.Series({
            "structuredContent": {"primaryLine": [{"body": "15 beds", "type": "BEDINFO"}]},
        })
        self.assertEqual(extract_bed_count_evidence(many)["value"], 15)

    def test_bed_count_prefers_aggregate_and_reports_total_component_conflicts(self):
        matched_total = pd.Series({
            "structuredContent": {"primaryLine": [
                {"body": "3 beds", "type": "BEDINFO"},
                {"body": "2 double beds", "type": "BEDINFO"},
                {"body": "1 sofa bed", "type": "BEDINFO"},
            ]},
        })
        fact = extract_bed_count_evidence(matched_total)
        self.assertEqual(fact["state"], "known")
        self.assertEqual(fact["value"], 3)

        inconsistent = pd.Series({
            "structuredContent": {"primaryLine": [
                {"body": "4 beds", "type": "BEDINFO"},
                {"body": "2 double beds", "type": "BEDINFO"},
                {"body": "1 sofa bed", "type": "BEDINFO"},
            ]},
        })
        fact = extract_bed_count_evidence(inconsistent)
        self.assertEqual(fact["state"], "conflict")
        self.assertIsNone(fact["value"])

        explicit_matches_aggregate = pd.Series({
            "beds": 3,
            "structuredContent": {"primaryLine": [
                {"body": "3 beds", "type": "BEDINFO"},
                {"body": "2 double beds", "type": "BEDINFO"},
                {"body": "2 sofa beds", "type": "BEDINFO"},
            ]},
        })
        fact = extract_bed_count_evidence(explicit_matches_aggregate)
        self.assertEqual(fact["state"], "conflict")
        self.assertIsNone(fact["value"])

    def test_cancellation_evidence_requires_direct_or_context_matched_evidence(self):
        self.assertEqual(extract_cancellation_evidence(pd.Series({"reviews_text": "Free cancellation was helpful"}))["state"], "unknown")
        self.assertEqual(extract_cancellation_evidence(pd.Series({"cancellation_policy": "Flexible"}))["state"], "unknown")

        nonrefundable = extract_cancellation_evidence(pd.Series({
            "structuredContent": {"cancellationPolicy": "This booking is non-refundable."}
        }))
        self.assertEqual(nonrefundable["state"], "nonrefundable")

        conditional = extract_cancellation_evidence(pd.Series({
            "cancellation_policy_text": "Free cancellation until 2027-02-01."
        }))
        self.assertEqual(conditional["state"], "conditional")

        explicit_false = extract_cancellation_evidence(pd.Series({"free_cancellation": False}))
        self.assertEqual(explicit_false["state"], "not_free")
        negated_free = extract_cancellation_evidence(pd.Series({
            "cancellation_policy": "No free cancellation is offered."
        }))
        self.assertEqual(negated_free["state"], "not_free")

        context = {
            "free_cancellation": True,
            "check_in": "2027-02-01",
            "check_out": "2027-02-05",
            "currency": "EUR",
            "trip_revision": 3,
        }
        matched = pd.Series({
            "free_cancellation_search_match": True,
            "cancellation_search_check_in": "2027-02-01",
            "cancellation_search_check_out": "2027-02-05",
            "cancellation_search_currency": "eur",
            "cancellation_search_trip_revision": 3,
        })
        self.assertEqual(extract_cancellation_evidence(matched, context)["state"], "free")
        self.assertEqual(extract_cancellation_evidence(matched)["state"], "unknown")
        matched["cancellation_search_trip_revision"] = 2
        self.assertEqual(extract_cancellation_evidence(matched, context)["state"], "unknown")
        matched["cancellation_search_trip_revision"] = 3
        matched["cancellation_search_check_in"] = "2027-02-02"
        self.assertEqual(extract_cancellation_evidence(matched, context)["state"], "unknown")

    def test_enrichment_adds_filter_fields_and_preserves_legacy_columns(self):
        df = pd.DataFrame([{
            "room_id": "1",
            "room_type": "Private room",
            "beds": 2,
            "bedrooms": 1,
            "free_cancellation": False,
        }])
        enriched = enrich_properties_dataframe(df)
        self.assertEqual(enriched.iloc[0]["accommodation_type"], "Private room")
        self.assertEqual(enriched.iloc[0]["bed_count"], 2)
        self.assertEqual(enriched.iloc[0]["bedroom_count"], 1)
        self.assertEqual(enriched.iloc[0]["cancellation_state"], "not_free")
        self.assertIn("has_ac", enriched.columns)
        self.assertIn("bathroom_privacy", enriched.columns)


if __name__ == "__main__":
    unittest.main()
