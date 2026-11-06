"""Unit tests for curation_service module."""

import unittest
import tempfile
import shutil
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from src.curation_service import (
    load_user_curation,
    save_user_curation,
    dismiss_property,
    restore_property,
    restore_all_properties,
    toggle_shortlist_property,
    reorder_visible_properties,
    move_property_priority,
    ensure_manual_order,
    CurationCorruptError,
)
import src.curation_service as cs


class TestCurationService(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_root = cs.PROJECT_ROOT
        cs.PROJECT_ROOT = Path(self.test_dir)
        self.search_name = "test_search"

    def tearDown(self):
        cs.PROJECT_ROOT = self.orig_root
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_dismiss_and_restore_property(self):
        curation = load_user_curation(self.search_name)
        self.assertEqual(curation["hidden_room_ids"], [])

        dismiss_property(self.search_name, "12345")
        curation = load_user_curation(self.search_name)
        self.assertIn("12345", curation["hidden_room_ids"])

        restore_property(self.search_name, "12345")
        curation = load_user_curation(self.search_name)
        self.assertNotIn("12345", curation["hidden_room_ids"])

    def test_toggle_shortlist(self):
        is_shortlisted = toggle_shortlist_property(self.search_name, "99999")
        self.assertTrue(is_shortlisted)
        curation = load_user_curation(self.search_name)
        self.assertIn("99999", curation["shortlisted_room_ids"])

        is_shortlisted = toggle_shortlist_property(self.search_name, "99999")
        self.assertFalse(is_shortlisted)
        curation = load_user_curation(self.search_name)
        self.assertNotIn("99999", curation["shortlisted_room_ids"])

    def test_shortlist_and_hidden_membership_are_independent(self):
        toggle_shortlist_property(self.search_name, "same-room")
        dismiss_property(self.search_name, "same-room")
        curation = load_user_curation(self.search_name)
        self.assertIn("same-room", curation["hidden_room_ids"])
        self.assertIn("same-room", curation["shortlisted_room_ids"])

        restore_property(self.search_name, "same-room")
        curation = load_user_curation(self.search_name)
        self.assertNotIn("same-room", curation["hidden_room_ids"])
        self.assertIn("same-room", curation["shortlisted_room_ids"])

    def test_restore_all_properties(self):
        dismiss_property(self.search_name, "111")
        dismiss_property(self.search_name, "222")
        curation = load_user_curation(self.search_name)
        self.assertEqual(len(curation["hidden_room_ids"]), 2)

        restore_all_properties(self.search_name)
        curation = load_user_curation(self.search_name)
        self.assertEqual(curation["hidden_room_ids"], [])

    def test_manual_reorder_preserves_nonvisible_relative_order_and_appends_new_ids(self):
        save_user_curation(self.search_name, {"custom_priority": ["a", "b", "c", "d"]})
        result = reorder_visible_properties(
            self.search_name,
            ["c", "a"],
            all_current_ids=["a", "b", "c", "d", "e"],
        )
        self.assertEqual(result, ["c", "b", "a", "d", "e"])
        self.assertEqual(load_user_curation(self.search_name)["custom_priority"], result)

    def test_keyboard_move_uses_visible_positions_without_dropping_hidden_order(self):
        save_user_curation(self.search_name, {"manual_order": ["a", "b", "c", "d"]})
        move_property_priority(self.search_name, "a", "down", ["a", "c"])
        self.assertEqual(load_user_curation(self.search_name)["manual_order"], ["c", "b", "a", "d"])

    def test_reorder_preserves_ids_temporarily_missing_from_refreshed_results(self):
        save_user_curation(self.search_name, {"manual_order": ["a", "temporarily-missing", "b"]})
        result = reorder_visible_properties(self.search_name, ["b", "a"], ["a", "b", "new"])
        self.assertEqual(result, ["b", "temporarily-missing", "a", "new"])

    def test_ensure_manual_order_appends_discovered_ids_without_losing_saved_order(self):
        save_user_curation(self.search_name, {"manual_order": ["b", "temporarily-missing", "a"]})
        visible = ensure_manual_order(self.search_name, ["a", "c", "b"], preferred_order=["a", "b"])
        self.assertEqual(visible, ["b", "a", "c"])
        self.assertEqual(
            load_user_curation(self.search_name)["manual_order"],
            ["b", "temporarily-missing", "a", "c"],
        )

    def test_ensure_manual_order_seeds_from_preferred_order_once(self):
        visible = ensure_manual_order(self.search_name, ["a", "b", "c"], preferred_order=["c", "a"])
        self.assertEqual(visible, ["c", "a", "b"])
        self.assertEqual(load_user_curation(self.search_name)["manual_order"], ["c", "a", "b"])

    def test_corrupt_curation_is_reported(self):
        path = cs.get_curation_file(self.search_name)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(CurationCorruptError):
            load_user_curation(self.search_name)

    def test_existing_legacy_name_with_dot_remains_usable(self):
        save_user_curation("New.York_2027-01-01", {"hidden_room_ids": ["r1"]})
        self.assertEqual(load_user_curation("New.York_2027-01-01")["hidden_room_ids"], ["r1"])

    def test_legacy_curation_import_is_idempotent_without_rewriting_export(self):
        path = cs.get_curation_file(self.search_name)
        raw = json.dumps({
            "hidden_room_ids": ["legacy-hidden"],
            "shortlisted_room_ids": ["legacy-shortlisted"],
            "custom_priority": ["b", "a"],
        }, indent=2).encode("utf-8")
        path.write_bytes(raw)
        before = path.stat()

        first = load_user_curation(self.search_name)
        second = load_user_curation(self.search_name)

        self.assertEqual(first, second)
        self.assertEqual(first["manual_order"], ["b", "a"])
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertTrue(path.parent.joinpath("analyzer.sqlite3").exists())

    def test_curation_is_database_backed_when_compatibility_export_is_removed(self):
        toggle_shortlist_property(self.search_name, "saved-room")
        path = cs.get_curation_file(self.search_name)
        lock_path = path.with_suffix(".lock")
        self.assertTrue(lock_path.exists())
        path.unlink()

        loaded = load_user_curation(self.search_name)

        self.assertIn("saved-room", loaded["shortlisted_room_ids"])
        self.assertFalse(path.exists())

    def test_external_curation_change_reimports_and_corrupt_edit_is_reported(self):
        toggle_shortlist_property(self.search_name, "original-room")
        path = cs.get_curation_file(self.search_name)
        path.write_text(json.dumps({
            "hidden_room_ids": ["external-hidden"],
            "shortlisted_room_ids": ["external-shortlisted"],
            "manual_order": ["external-b", "external-a"],
        }), encoding="utf-8")

        loaded = load_user_curation(self.search_name)
        self.assertEqual(loaded["hidden_room_ids"], ["external-hidden"])
        self.assertEqual(loaded["shortlisted_room_ids"], ["external-shortlisted"])
        self.assertEqual(loaded["manual_order"], ["external-b", "external-a"])

        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(CurationCorruptError):
            load_user_curation(self.search_name)
        path.unlink()
        self.assertEqual(
            load_user_curation(self.search_name)["shortlisted_room_ids"],
            ["external-shortlisted"],
        )

    def test_concurrent_curation_mutations_do_not_lose_updates(self):
        ids = [f"room-{index}" for index in range(24)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda room_id: toggle_shortlist_property(self.search_name, room_id), ids))
        self.assertEqual(set(load_user_curation(self.search_name)["shortlisted_room_ids"]), set(ids))


if __name__ == "__main__":
    unittest.main()
