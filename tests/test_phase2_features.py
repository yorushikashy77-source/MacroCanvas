import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QCheckBox

from config.diff import (
    build_config_diff, merge_config_sections, selected_section_labels,
)
from core.constants import MacroState
from ui.backup_manager import BackupManagerDialog, _SnapshotLoadTask
from ui.shutdown_coordinator import ShutdownCoordinatorMixin
from ui.system_tray import SystemTrayMixin


def sample_config():
    return {
        "version": 24,
        "engine_backend": "普通模式（winIOv2）",
        "auto_apply": False,
        "diagnostic_enabled": False,
        "global_toggle_enabled": True,
        "global_toggle_modifiers": "Ctrl+Shift",
        "global_toggle_key": "F10",
        "macro_pause_enabled": True,
        "macro_pause_modifiers": "Ctrl",
        "macro_pause_key": "F9",
        "emergency_modifiers": "无",
        "emergency_key": "F8",
        "recording_cancel_modifiers": "无",
        "recording_cancel_key": "F7",
        "recording_finish_modifiers": "无",
        "recording_finish_key": "F8",
        "profile_auto_switch_enabled": True,
        "active_profile_id": "",
        "editor_profile_id": "",
        "mappings": [{
            "id": "mapping-a", "enabled": False, "name": "A",
            "source_modifiers": "无", "source": "F1",
            "target_modifiers": "无", "target": "A",
            "condition_enabled": False, "condition_input": "鼠标左键",
            "condition_state": "按住时", "mode": "执行一次",
            "hold_ms": 10, "hold_jitter_ms": 0, "loop_count": 1,
            "loop_interval_ms": 0, "loop_interval_jitter_ms": 0,
            "speed_percent": 100, "max_runtime_s": 0,
        }],
        "presets": [],
        "profiles": [],
    }


class ConfigDiffTests(unittest.TestCase):
    def test_diff_reports_atomic_content_and_independent_setting_sections(self):
        current = sample_config()
        backup = json.loads(json.dumps(current))
        backup["mappings"][0]["name"] = "B"
        backup["engine_backend"] = "游戏模式（Interception）"
        backup["auto_apply"] = True
        diff = build_config_diff(current, backup)
        self.assertEqual(
            set(diff["changed_sections"]),
            {"content", "runtime_controls", "preferences"},
        )
        content = next(item for item in diff["sections"] if item["key"] == "content")
        self.assertIn("映射", content["summary"])

    def test_selective_merge_does_not_mutate_inputs_or_unselected_sections(self):
        current = sample_config()
        backup = json.loads(json.dumps(current))
        backup["mappings"][0]["name"] = "backup-name"
        backup["auto_apply"] = True
        merged = merge_config_sections(current, backup, {"preferences"})
        self.assertTrue(merged["auto_apply"])
        self.assertEqual(merged["mappings"][0]["name"], "A")
        self.assertEqual(current["auto_apply"], False)
        self.assertEqual(backup["mappings"][0]["name"], "backup-name")

    def test_content_restore_copies_profile_selectors_as_one_unit(self):
        current = sample_config()
        backup = json.loads(json.dumps(current))
        backup.update({
            "profiles": [{
                "id": "profile-b", "name": "Profile B", "enabled": True,
                "process_name": "", "title_contains": "",
                "payload": {"mappings": [], "presets": []},
            }],
            "active_profile_id": "profile-b",
            "editor_profile_id": "profile-b",
        })
        merged = merge_config_sections(current, backup, {"content"})
        self.assertEqual(merged["active_profile_id"], "profile-b")
        self.assertEqual(merged["profiles"][0]["id"], "profile-b")
        self.assertEqual(selected_section_labels({"content"})[0][:4], "全部映射")

    def test_reordering_priority_lists_is_reported_and_restored(self):
        current = sample_config()
        second_mapping = json.loads(json.dumps(current["mappings"][0]))
        second_mapping.update({"id": "mapping-b", "name": "B", "source": "F2"})
        current["mappings"].append(second_mapping)
        current["profiles"] = [
            {"id": "profile-a", "name": "Profile A"},
            {"id": "profile-b", "name": "Profile B"},
        ]
        backup = json.loads(json.dumps(current))
        backup["mappings"].reverse()
        backup["profiles"].reverse()

        diff = build_config_diff(current, backup)
        content = next(item for item in diff["sections"] if item["key"] == "content")
        self.assertEqual(diff["changed_sections"], ["content"])
        self.assertEqual(content["change_count"], 2)
        self.assertIn("映射顺序变化", content["summary"])
        self.assertIn("配置档案顺序变化", content["summary"])
        self.assertTrue(any("优先级顺序" in detail for detail in content["details"]))

        merged = merge_config_sections(current, backup, {"content"})
        self.assertEqual(
            [item["id"] for item in merged["mappings"]],
            ["mapping-b", "mapping-a"],
        )
        self.assertEqual(
            [item["id"] for item in merged["profiles"]],
            ["profile-b", "profile-a"],
        )

    def test_adding_an_item_without_reordering_existing_items_is_not_mislabeled(self):
        current = sample_config()
        backup = json.loads(json.dumps(current))
        second_mapping = json.loads(json.dumps(current["mappings"][0]))
        second_mapping.update({"id": "mapping-b", "name": "B", "source": "F2"})
        backup["mappings"].append(second_mapping)

        diff = build_config_diff(current, backup)
        content = next(item for item in diff["sections"] if item["key"] == "content")
        self.assertNotIn("顺序变化", content["summary"])


class BackupDiffUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_background_snapshot_loader_returns_diff(self):
        current = sample_config()
        backup = json.loads(json.dumps(current))
        backup["auto_apply"] = True
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "saved-test.json"
            path.write_text(json.dumps(backup, ensure_ascii=False), "utf-8")
            results = []
            task = _SnapshotLoadTask(path, current)
            task.signals.finished.connect(results.append)
            task.run()
        self.assertEqual(results[0]["error"], "")
        self.assertEqual(results[0]["diff"]["changed_sections"], ["preferences"])

    def test_snapshot_loader_preserves_variables_submacro_and_loop_refs(self):
        current = sample_config()
        backup = json.loads(json.dumps(current))
        backup["presets"] = [
            {
                "id": "root", "enabled": False, "name": "调用方",
                "trigger_modifiers": "无", "trigger": "F2",
                "execution_mode": "执行一次",
                "actions": [
                    {
                        "type": "调用子宏", "action_id": "call-child",
                        "preset_id": "child", "repeat_count": 1,
                        "speed_percent": 100,
                        "parameter_values": {"技能键": "B"},
                    },
                    {
                        "type": "循环动作", "id": "loop-child",
                        "target_action_ids": ["call-child"],
                        "loop_count": 2, "loop_interval_ms": 50,
                        "speed_percent": 100,
                    },
                ],
            },
            {
                "id": "child", "enabled": False, "name": "被调用方",
                "trigger_modifiers": "无", "trigger": "F3",
                "execution_mode": "执行一次",
                "parameters": [
                    {"name": "技能键", "type": "按键", "default": "A"},
                ],
                "actions": [
                    {
                        "type": "键盘点击", "action_id": "child-key",
                        "target": "A", "hold_ms": 10,
                        "parameter_bindings": {"target": "技能键"},
                    },
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "saved-parameterized.json"
            path.write_text(json.dumps(backup, ensure_ascii=False), "utf-8")
            results = []
            task = _SnapshotLoadTask(path, current)
            task.signals.finished.connect(results.append)
            task.run()

        self.assertEqual(results[0]["error"], "")
        loaded = {item["id"]: item for item in results[0]["payload"]["presets"]}
        self.assertEqual(
            loaded["child"]["parameters"],
            [{"name": "技能键", "type": "按键", "default": "A"}],
        )
        self.assertEqual(
            loaded["child"]["actions"][0]["parameter_bindings"],
            {"target": "技能键"},
        )
        self.assertEqual(
            loaded["root"]["actions"][0]["parameter_values"],
            {"技能键": "B"},
        )
        self.assertEqual(
            loaded["root"]["actions"][1]["target_action_ids"],
            ["call-child"],
        )

    def test_diff_checkboxes_control_restore_button_and_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = BackupManagerDialog(folder, current_payload=sample_config())
            snapshot = {
                "selected_sections": None,
                "diff": {
                    "change_count": 2,
                    "sections": [
                        {
                            "key": "preferences", "label": "偏好",
                            "description": "", "changed": True,
                            "summary": "2 项设置不同", "details": [],
                        }
                    ],
                },
            }
            dialog._selected_snapshot = snapshot
            dialog._render_diff(snapshot)
            checkbox = dialog.diff_checks.findChild(QCheckBox)
            self.assertIsNotNone(checkbox)
            self.assertTrue(dialog.restore_button.isEnabled())
            checkbox.setChecked(False)
            self.assertFalse(dialog.restore_button.isEnabled())
            self.assertEqual(snapshot["selected_sections"], set())
            dialog.close()


class _TrayStub:
    def __init__(self):
        self.visible = True
        self.hidden = False

    def isVisible(self):
        return self.visible

    def hide(self):
        self.hidden = True


class _TrayExitHarness(SystemTrayMixin):
    def __init__(self):
        self._shutdown_complete = False
        self._tray_exit_requested = False
        self.visible = True
        self.close_saw_exit_request = False

    def isVisible(self):
        return self.visible

    def close(self):
        self.close_saw_exit_request = self._tray_exit_requested
        self._shutdown_complete = True


class _Event:
    def __init__(self):
        self.ignored = False

    def ignore(self):
        self.ignored = True


class _ToggleActionStub:
    def __init__(self, checked=False):
        self.checked = bool(checked)
        self.blocked = False
        self.tooltip = ""

    def blockSignals(self, blocked):
        self.blocked = bool(blocked)

    def setChecked(self, checked):
        self.checked = bool(checked)

    def setToolTip(self, text):
        self.tooltip = str(text)


class TraySafetyTests(unittest.TestCase):
    def test_normal_close_can_be_intercepted_before_shutdown(self):
        class Owner:
            def should_hide_close_to_tray(self):
                return True

            def hide_close_to_tray(self, event):
                event.ignore()
                self.hidden = True

        owner = Owner()
        event = _Event()
        ShutdownCoordinatorMixin.closeEvent(owner, event)
        self.assertTrue(event.ignored)
        self.assertTrue(owner.hidden)

    def test_tray_exit_sets_explicit_flag_before_reusing_window_close(self):
        owner = _TrayExitHarness()
        owner.request_exit_from_system_tray()
        self.assertTrue(owner.close_saw_exit_request)
        self.assertTrue(owner._shutdown_complete)

    def test_hide_condition_is_disabled_for_explicit_exit_or_shutdown(self):
        owner = _TrayExitHarness()
        owner.close_to_tray_enabled = True
        owner.system_tray = _TrayStub()
        owner._shutdown_started = False
        self.assertTrue(owner.should_hide_close_to_tray())
        owner._tray_exit_requested = True
        self.assertFalse(owner.should_hide_close_to_tray())
        owner._tray_exit_requested = False
        owner._shutdown_started = True
        self.assertFalse(owner.should_hide_close_to_tray())

    def test_hide_condition_is_disabled_during_recording_or_test_countdown(self):
        owner = _TrayExitHarness()
        owner.close_to_tray_enabled = True
        owner.system_tray = _TrayStub()
        owner._shutdown_started = False
        owner.recording_session_active = True
        self.assertFalse(owner.should_hide_close_to_tray())

        owner.recording_session_active = False
        owner.macro_state = MacroState.COUNTDOWN
        owner._test_countdown_preset_id = "preset-a"
        self.assertFalse(owner.should_hide_close_to_tray())

        owner.macro_state = MacroState.IDLE
        owner._test_countdown_preset_id = None
        self.assertTrue(owner.should_hide_close_to_tray())

    def test_preference_write_failure_rolls_back_memory_and_action(self):
        owner = _TrayExitHarness()
        owner.close_to_tray_enabled = True
        owner.close_to_tray_action = _ToggleActionStub(checked=False)
        with tempfile.TemporaryDirectory() as folder:
            settings_path = Path(folder) / "ui-settings.json"
            with (
                patch("ui.system_tray.UI_SETTINGS_PATH", settings_path),
                patch(
                    "ui.system_tray.atomic_write_text",
                    side_effect=OSError("permission denied"),
                ),
                patch("ui.system_tray.QMessageBox.warning") as warning,
            ):
                self.assertFalse(owner.set_close_to_tray_enabled(False))
        self.assertTrue(owner.close_to_tray_enabled)
        self.assertTrue(owner.close_to_tray_action.checked)
        warning.assert_called_once()

    def test_preference_write_preserves_other_valid_ui_settings(self):
        owner = _TrayExitHarness()
        owner.close_to_tray_enabled = True
        owner.close_to_tray_action = _ToggleActionStub(checked=False)
        with tempfile.TemporaryDirectory() as folder:
            settings_path = Path(folder) / "ui-settings.json"
            settings_path.write_text(
                json.dumps({"close_to_tray": True, "future_setting": 7}),
                "utf-8",
            )
            with patch("ui.system_tray.UI_SETTINGS_PATH", settings_path):
                self.assertTrue(owner.set_close_to_tray_enabled(False))
            saved = json.loads(settings_path.read_text("utf-8"))
        self.assertFalse(owner.close_to_tray_enabled)
        self.assertFalse(saved["close_to_tray"])
        self.assertEqual(saved["future_setting"], 7)

    def test_corrupt_existing_preference_fails_closed_with_visible_warning_state(self):
        owner = _TrayExitHarness()
        with tempfile.TemporaryDirectory() as folder:
            settings_path = Path(folder) / "ui-settings.json"
            settings_path.write_text("{broken", "utf-8")
            with patch("ui.system_tray.UI_SETTINGS_PATH", settings_path):
                owner._initialize_system_tray_state()
        self.assertFalse(owner.close_to_tray_enabled)
        self.assertTrue(owner._tray_settings_load_warning)

    def test_dispose_hides_tray_icon(self):
        owner = _TrayExitHarness()
        owner.system_tray = _TrayStub()
        owner.dispose_system_tray()
        self.assertTrue(owner.system_tray.hidden)


if __name__ == "__main__":
    unittest.main()
