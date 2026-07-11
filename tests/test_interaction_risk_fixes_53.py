import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QMessageBox

import core.constants as constants
import ui.config_persistence as persistence_module
import ui.shutdown_coordinator as shutdown_module
from ui.config_persistence import ConfigPersistenceMixin
from ui.shutdown_coordinator import ShutdownCoordinatorMixin
from tests.test_interaction_risk_fixes_35 import _ForceReleaseHarness


class _CheckBox:
    def isChecked(self):
        return True


class _Hint:
    def __init__(self):
        self.text = ""

    def setStyleSheet(self, _style):
        pass

    def setText(self, text):
        self.text = text


class _PreferenceHarness(ConfigPersistenceMixin):
    auto_apply_checkbox = _CheckBox()

    def __init__(self, pending=False):
        self.startup_recovery_pending_save = pending
        self.engine_hint = _Hint()
        self.save_calls = 0

    def _save_config_payload(self, data, create_backup=True):
        self.save_calls += 1


class _ForceChoiceBox:
    class Icon:
        Warning = 1

    class ButtonRole:
        DestructiveRole = 1
        RejectRole = 2

    def __init__(self, _parent):
        self.force = None

    def setIcon(self, _icon):
        pass

    def setWindowTitle(self, _title):
        pass

    def setText(self, _text):
        pass

    def setInformativeText(self, _text):
        pass

    def addButton(self, _label, role):
        button = object()
        if role == self.ButtonRole.DestructiveRole:
            self.force = button
        return button

    def setDefaultButton(self, _button):
        pass

    def exec(self):
        pass

    def clickedButton(self):
        return self.force


class _ShutdownHarness(ShutdownCoordinatorMixin):
    def __init__(self):
        self.interception_output = None
        self.quarantined_mouse_release_lock = threading.RLock()
        self.quarantined_mouse_releases = [{
            "action": {"target": "鼠标左键"}
        }]
        self.force_kwargs = None

    def force_release_held_inputs(self, **kwargs):
        self.force_kwargs = kwargs
        self.quarantined_mouse_releases.clear()
        return True


class InteractionRiskFixes53Tests(unittest.TestCase):
    def test_recovery_pending_preference_never_writes_main_config(self):
        harness = _PreferenceHarness(pending=True)
        self.assertFalse(harness.save_auto_apply_preference())
        self.assertEqual(harness.save_calls, 0)
        self.assertIn("原配置文件尚未覆盖", harness.engine_hint.text)

    def test_invalid_main_config_is_not_replaced_by_preference_save(self):
        harness = _PreferenceHarness(pending=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = "{broken but recoverable"
            path.write_text(original, encoding="utf-8")
            with (
                patch.object(persistence_module, "APP_DIR", Path(directory)),
                patch.object(persistence_module, "CONFIG_PATH", path),
                patch.object(persistence_module.QMessageBox, "warning"),
            ):
                self.assertFalse(harness.save_auto_apply_preference())
            self.assertEqual(path.read_text("utf-8"), original)
            self.assertEqual(harness.save_calls, 0)

    def test_component_setting_overrides_legacy_drive_location(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "components.json"
            chosen = Path(directory) / "portable-kanata"
            settings.write_text(
                json.dumps({"kanata_dir": str(chosen)}), encoding="utf-8"
            )
            with (
                patch.object(constants, "KANATA_SETTINGS_PATH", settings),
                patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop("MACROCANVAS_KANATA_DIR", None)
                self.assertEqual(constants.kanata_dir(), chosen)

    def test_shutdown_force_choice_uses_preconfirmed_single_release(self):
        harness = _ShutdownHarness()
        with patch.object(shutdown_module, "QMessageBox", _ForceChoiceBox):
            self.assertTrue(harness._offer_forced_mouse_release_for_shutdown())
        self.assertEqual(harness.force_kwargs, {
            "show_feedback": False,
            "_cross_window_release_confirmed": True,
        })

    def test_manual_force_release_prompts_only_once_per_operation(self):
        harness = _ForceReleaseHarness()
        harness.quarantined_mouse_releases = [{
            "action": {"target": "鼠标左键"}
        }]
        with (
            patch(
                "ui.main_window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ) as question,
            patch(
                "ui.main_window.foreground_window_belongs_to_current_process",
                return_value=False,
            ),
        ):
            self.assertTrue(harness.force_release_held_inputs(show_feedback=True))
        self.assertEqual(question.call_count, 1)

    def test_shutdown_force_release_does_not_depend_on_layer_command(self):
        harness = _ForceReleaseHarness()
        harness._shutdown_started = True
        harness.quarantined_mouse_releases = [{
            "action": {"target": "鼠标左键"}
        }]
        harness._runtime_is_game_mode = lambda: False
        harness._change_runtime_profile_layer = lambda *_args, **_kwargs: False
        with patch(
            "ui.main_window.foreground_window_belongs_to_current_process",
            return_value=True,
        ):
            self.assertTrue(harness.force_release_held_inputs(
                show_feedback=False,
                _cross_window_release_confirmed=True,
            ))

    def test_recovery_and_single_confirmation_guards_remain_wired(self):
        root = Path(__file__).resolve().parents[1]
        main = (root / "ui" / "main_window.py").read_text("utf-8")
        coordinator = (root / "ui" / "shutdown_coordinator.py").read_text("utf-8")
        editor = (root / "ui" / "editor_workflow.py").read_text("utf-8")
        self.assertIn("recovery_requires_save = True", main)
        self.assertIn("nonlocal cross_window_release_confirmed", main)
        self.assertIn(
            'if getattr(self, "startup_recovery_pending_save", False):', editor
        )
        self.assertIn("self.auto_apply_timer.stop()", editor)
        self.assertIn("_offer_forced_mouse_release_for_shutdown", coordinator)
        self.assertIn("再次关闭窗口可重试", coordinator)


if __name__ == "__main__":
    unittest.main()
