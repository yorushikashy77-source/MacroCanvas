import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication, QComboBox

from core.constants import MacroState
from ui.action_execution import ActionExecutionMixin
from ui.profile_workflow import ProfileWorkflowMixin


ROOT = Path(__file__).resolve().parents[1]


class _Overlay:
    def __init__(self):
        self.hidden = False

    def hide_message(self):
        self.hidden = True


class _Hint:
    def __init__(self):
        self.text = ""

    def setStyleSheet(self, _style):
        pass

    def setText(self, text):
        self.text = text


class _CountdownHarness(ActionExecutionMixin):
    def __init__(self):
        self.macro_state = MacroState.COUNTDOWN
        self.macro_status_detail = "5 秒后执行"
        self._test_countdown_generation = 7
        self._test_countdown_preset_id = "preset-1"
        self.activity_overlay = _Overlay()
        self.engine_hint = _Hint()
        self.status_refreshes = 0

    def refresh_status_ui(self):
        self.status_refreshes += 1


class _SelectorHarness(ProfileWorkflowMixin):
    def __init__(self):
        self.profile_selector_combo = QComboBox()
        self.profiles = [
            {"id": "enabled", "name": "启用档案", "enabled": True},
            {"id": "disabled", "name": "停用档案", "enabled": False},
        ]
        self.editor_profile_id = "disabled"
        self.editor_loaded_profile_id = "disabled"

    def _refresh_editor_profile_labels(self):
        pass

    def refresh_profile_selector_state(self):
        pass


class InteractionRiskFix52Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_configuration_change_cancels_manual_test_countdown(self):
        harness = _CountdownHarness()

        self.assertTrue(harness._cancel_manual_test_countdown("配置已修改"))
        self.assertEqual(harness._test_countdown_generation, 8)
        self.assertIsNone(harness._test_countdown_preset_id)
        self.assertEqual(harness.macro_state, MacroState.IDLE)
        self.assertEqual(harness.macro_status_detail, "")
        self.assertTrue(harness.activity_overlay.hidden)
        self.assertEqual(harness.engine_hint.text, "配置已修改")
        self.assertEqual(harness.status_refreshes, 1)

    def test_data_changed_routes_through_countdown_cancellation(self):
        source = (ROOT / "ui" / "editor_workflow.py").read_text("utf-8")
        method = source[source.index("    def data_changed"):source.index(
            "    def auto_apply_config"
        )]
        self.assertIn("_cancel_manual_test_countdown", method)

    def test_modal_configuration_operations_cancel_countdown(self):
        transfer = (ROOT / "ui" / "configuration_transfer.py").read_text("utf-8")
        persistence = (ROOT / "ui" / "config_persistence.py").read_text("utf-8")
        profiles = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")

        self.assertIn("_cancel_manual_test_countdown", transfer)
        self.assertIn("_cancel_manual_test_countdown", persistence)
        settings = profiles[profiles.index("    def _enter_settings_input_mode"):]
        self.assertIn("_cancel_manual_test_countdown", settings)

    def test_disabled_profiles_remain_available_to_the_editor(self):
        harness = _SelectorHarness()

        harness.refresh_profile_selector()

        combo = harness.profile_selector_combo
        self.assertEqual(combo.count(), 3)
        self.assertEqual(combo.currentData(), "disabled")
        self.assertIn("停用，仅编辑", combo.currentText())

    def test_startup_repair_stays_dirty_until_commit(self):
        main = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        editor = (ROOT / "ui" / "editor_workflow.py").read_text("utf-8")
        runtime = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")

        self.assertIn("recovery_requires_save = True", main)
        self.assertIn("generated and self.startup_recovery_pending_save", main)
        self.assertIn('getattr(self, "startup_recovery_pending_save", False)', editor)
        commit = runtime[runtime.index("    def _commit_applied_candidate"):]
        self.assertIn("self.startup_recovery_pending_save = False", commit)


if __name__ == "__main__":
    unittest.main()
