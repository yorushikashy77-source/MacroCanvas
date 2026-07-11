import json
import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from config.schema import (
    repair_duplicate_action_tree_ids,
    repair_duplicate_runtime_ids,
    repair_overlapping_loop_controls,
    validate_config_payload,
    validate_config_structure_depth,
)
from ui.configuration_transfer import ConfigurationTransferMixin
from ui.backup_manager import BackupManagerDialog


ROOT = Path(__file__).resolve().parents[1]


def _nested_list(depth):
    value = 0
    for _ in range(depth):
        value = [value]
    return value


class _Card:
    def __init__(self):
        self.hidden = False
        self.deleted = False

    def hide(self):
        self.hidden = True

    def deleteLater(self):
        self.deleted = True


class _Layout:
    def __init__(self):
        self.removed = []

    def removeWidget(self, card):
        self.removed.append(card)


class _Button:
    def __init__(self, enabled):
        self.enabled = enabled

    def isEnabled(self):
        return self.enabled

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _Hint:
    def __init__(self, text, style):
        self._text = text
        self._style = style

    def text(self):
        return self._text

    def styleSheet(self):
        return self._style

    def setText(self, text):
        self._text = text

    def setStyleSheet(self, style):
        self._style = style


class _ImportHarness(ConfigurationTransferMixin):
    def __init__(self):
        self.original_mapping = _Card()
        self.added_mapping = _Card()
        self.original_preset = _Card()
        self.added_preset = _Card()
        self.mapping_cards = [self.original_mapping, self.added_mapping]
        self.preset_cards = [self.original_preset, self.added_preset]
        self.mapping_layout = _Layout()
        self.preset_layout = _Layout()
        self.selected_preset_card = self.added_preset
        self.action_table = object()
        self.action_title = object()
        self.config_state = "dirty-after-partial-import"
        self.reload_button = _Button(True)
        self.engine_hint = _Hint("partial", "bad")
        self.calls = []

    def select_preset_card(self, card):
        self.selected_preset_card = card

    def refresh_mapping_priority_labels(self):
        self.calls.append("priorities")

    def refresh_cache(self):
        self.calls.append("cache")

    def _store_editor_payload(self):
        self.calls.append("store")

    def refresh_status_ui(self):
        self.calls.append("status")


class InteractionRiskFixes69Tests(unittest.TestCase):
    def test_pathological_nesting_is_rejected_before_deepcopy(self):
        payload = _nested_list(400)
        with self.assertRaisesRegex(ValueError, "嵌套层级"):
            validate_config_structure_depth(payload)
        for repair in (
            repair_overlapping_loop_controls,
            repair_duplicate_action_tree_ids,
            repair_duplicate_runtime_ids,
        ):
            with self.subTest(repair=repair.__name__):
                with self.assertRaisesRegex(ValueError, "嵌套层级"):
                    repair(payload)

    def test_maximum_supported_action_depth_still_validates(self):
        actions = []
        current = actions
        for index in range(64):
            action = {
                "type": "等待",
                "action_id": f"action-{index}",
                "wait_ms": 1,
                "children": [],
            }
            current.append(action)
            current = action["children"]
        payload = {
            "mappings": [],
            "presets": [{"id": "preset", "enabled": False, "actions": actions}],
            "profiles": [],
        }
        repaired, _changes = repair_duplicate_action_tree_ids(payload)
        self.assertIs(validate_config_payload(repaired), repaired)

    def test_failed_editor_import_removes_only_new_cards_and_restores_state(self):
        harness = _ImportHarness()
        transaction = {
            "mapping_cards": [harness.original_mapping],
            "preset_cards": [harness.original_preset],
            "selected_preset_card": harness.original_preset,
            "config_state": "saved-before-import",
            "reload_enabled": False,
            "hint_text": "original",
            "hint_style": "good",
        }
        harness._rollback_editor_import_transaction(transaction)
        self.assertEqual(harness.mapping_cards, [harness.original_mapping])
        self.assertEqual(harness.preset_cards, [harness.original_preset])
        self.assertTrue(harness.added_mapping.deleted)
        self.assertTrue(harness.added_preset.deleted)
        self.assertEqual(harness.selected_preset_card, harness.original_preset)
        self.assertEqual(harness.config_state, "saved-before-import")
        self.assertFalse(harness.reload_button.enabled)
        self.assertEqual(harness.engine_hint.text(), "original")
        self.assertEqual(harness.engine_hint.styleSheet(), "good")
        self.assertEqual(harness.calls, ["priorities", "cache", "store", "status"])

    def test_startup_recovery_preserves_source_and_first_run_is_separate(self):
        source = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        load_start = source.index("    def load_config")
        load_block = source[load_start:]
        self.assertIn("if not CONFIG_PATH.exists():", load_block)
        self.assertIn("shutil.copy2(CONFIG_PATH, preserved_path)", load_block)
        self.assertIn("主配置不会自动被覆盖", load_block)
        self.assertNotIn(
            "self._save_config_payload(data, create_backup=False)", load_block
        )

    def test_backup_dialog_loads_only_selected_snapshot_in_worker(self):
        source = (ROOT / "ui" / "backup_manager.py").read_text("utf-8")
        list_start = source.index("    def _load_snapshots")
        selection_start = source.index("    def _on_selection_changed", list_start)
        list_block = source[list_start:selection_start]
        self.assertNotIn("read_text", list_block)
        self.assertIn("class _SnapshotLoadTask(QRunnable)", source)
        self.assertIn("self._snapshot_pool.start(task)", source)

    def test_selected_backup_finishes_background_validation(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved-20260711-120000-test.json"
            path.write_text(json.dumps({
                "mappings": [], "presets": [], "profiles": [],
            }), encoding="utf-8")
            dialog = BackupManagerDialog(directory)
            deadline = time.monotonic() + 3.0
            while (
                dialog._snapshots
                and not dialog._snapshots[0]["loaded"]
                and time.monotonic() < deadline
            ):
                app.processEvents()
                time.sleep(0.01)
            self.assertTrue(dialog._snapshots[0]["loaded"])
            self.assertEqual(dialog._snapshots[0]["error"], "")
            self.assertIsNotNone(dialog._snapshots[0]["payload"])
            self.assertTrue(dialog.restore_button.isEnabled())
            dialog.close()
            dialog.deleteLater()
            app.processEvents()

    def test_pytest_excludes_archived_copies(self):
        settings = (ROOT / "pytest.ini").read_text("utf-8")
        self.assertIn("testpaths = tests", settings)
        self.assertIn("norecursedirs = 旧版备份", settings)

    def test_title_capture_requires_stable_fragment_confirmation(self):
        source = (ROOT / "ui" / "profile_manager.py").read_text("utf-8")
        start = source.index("    def use_current_title")
        block = source[start:source.index("    def test_current_window", start)]
        self.assertIn("QInputDialog.getText", block)
        self.assertIn("稳定", block)


if __name__ == "__main__":
    unittest.main()
