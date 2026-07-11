import threading
import unittest
from pathlib import Path

from engine.kanata import (
    kanata_exact_modifier_condition, kanata_source_modifier_condition,
)
from ui.editor_workflow import EditorWorkflowMixin
from ui.input_runtime import InputRuntimeMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin


ROOT = Path(__file__).resolve().parents[1]


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _Engine:
    @staticmethod
    def is_running():
        return False


class _MacroController:
    def __init__(self):
        self.tasks = {}

    @staticmethod
    def stop(_task_id):
        return None


class _EmergencyHarness(InputRuntimeMixin):
    def __init__(self):
        self.input_state_lock = threading.RLock()
        self.interception_forwarded_down = set()
        self.engine = _Engine()
        self.running = False
        self.settings_dialog_active = False
        self.interception_input_control_only = False
        self.physical_modifiers = set()
        self.physical_down = set()
        self.system_hotkey_latched = set()
        self.runtime_emergency_key = "F8"
        self.runtime_emergency_modifiers = "无"
        self.runtime_global_toggle_enabled = False
        self.runtime_global_toggle_key = "F10"
        self.runtime_global_toggle_modifiers = "无"
        self.runtime_macro_pause_enabled = False
        self.runtime_macro_pause_key = "F9"
        self.runtime_macro_pause_modifiers = "无"
        self.suppressed_trigger_names = set()
        self.active_sync_by_source = {}
        self.held_trigger_ids = {}
        self.global_toggle_latched = False
        self.macro_controller = _MacroController()
        self.emergency_signal = _Signal()

    @staticmethod
    def write_diagnostic(*_args, **_kwargs):
        return None

    @staticmethod
    def _consume_expected_kanata_event(_name, _down):
        return False

    @staticmethod
    def _release_invalid_conditional_holds():
        return False


class EmergencyRoutingTests(unittest.TestCase):
    def test_stopped_engine_passes_both_emergency_edges(self):
        harness = _EmergencyHarness()
        self.assertFalse(harness._global_hook_callback("F8", True))
        self.assertNotIn("emergency", harness.system_hotkey_latched)
        self.assertFalse(harness._global_hook_callback("F8", False))
        self.assertEqual(harness.emergency_signal.calls, [])

    def test_consumed_emergency_down_also_consumes_release(self):
        harness = _EmergencyHarness()
        harness.running = True
        self.assertTrue(harness._global_hook_callback("F8", True, interception=True))
        self.assertIn("emergency", harness.system_hotkey_latched)
        self.assertTrue(harness._global_hook_callback("F8", False, interception=True))
        self.assertNotIn("emergency", harness.system_hotkey_latched)
        self.assertEqual(len(harness.emergency_signal.calls), 1)


class ExactModifierTests(unittest.TestCase):
    def test_no_modifier_rule_excludes_all_extra_modifiers(self):
        condition = kanata_exact_modifier_condition("无", "F6")
        self.assertIn("(not (or (input real lctl) (input real rctl)))", condition)
        self.assertIn("(not (or (input real lsft) (input real rsft)))", condition)
        self.assertIn("(not (or (input real lalt) (input real ralt)))", condition)

    def test_ctrl_rule_excludes_shift_and_alt(self):
        condition = kanata_exact_modifier_condition("Ctrl", "F6")
        self.assertIn("(or (input real lctl) (input real rctl))", condition)
        self.assertIn("(not (or (input real lsft) (input real rsft)))", condition)
        self.assertIn("(not (or (input real lalt) (input real ralt)))", condition)


class LooseSourceModifierTests(unittest.TestCase):
    def test_no_modifier_source_allows_temporary_extra_modifiers(self):
        self.assertIsNone(kanata_source_modifier_condition("无", "Caps Lock"))

    def test_required_source_modifier_does_not_exclude_other_modifiers(self):
        condition = kanata_source_modifier_condition("Ctrl", "Caps Lock")
        self.assertIn("(or (input real lctl) (input real rctl))", condition)
        self.assertNotIn("(not (or (input real lsft) (input real rsft)))", condition)
        self.assertNotIn("(not (or (input real lalt) (input real ralt)))", condition)

    def test_exact_modifier_condition_remains_available_for_system_controls(self):
        condition = kanata_exact_modifier_condition("Ctrl", "F10")
        self.assertIn("(not (or (input real lsft) (input real rsft)))", condition)
        self.assertIn("(not (or (input real lalt) (input real ralt)))", condition)


class _Timer:
    def __init__(self):
        self.stopped = 0
        self.started = []

    def stop(self):
        self.stopped += 1

    def start(self, *args):
        self.started.append(args)


class _RecordingAutoApplyHarness(EditorWorkflowMixin):
    def __init__(self):
        self.recording_session_active = True
        self.auto_apply_timer = _Timer()
        self.apply_calls = 0

    def apply_changes(self):
        self.apply_calls += 1


class RecordingAutoApplyTests(unittest.TestCase):
    def test_auto_apply_timer_is_silently_deferred_during_recording(self):
        harness = _RecordingAutoApplyHarness()
        harness.auto_apply_config()
        self.assertEqual(harness.apply_calls, 0)
        self.assertEqual(harness.auto_apply_timer.stopped, 1)
        self.assertTrue(harness._auto_apply_deferred_for_recording)


class _LifecycleHarness(RuntimeLifecycleMixin):
    def __init__(self, succeed):
        self.running = False
        self.succeed = succeed
        self.feedback = []

    def set_running(self, enabled, allow_owned_mouse_force_release=False):
        if self.succeed:
            self.running = bool(enabled)
            return None
        return False

    def _play_feedback(self, kind):
        self.feedback.append(kind)


class FeedbackOrderingTests(unittest.TestCase):
    def test_success_sound_is_not_played_when_start_fails(self):
        harness = _LifecycleHarness(False)
        harness.toggle_running()
        self.assertEqual(harness.feedback, ["error"])

    def test_success_sound_is_played_after_start_succeeds(self):
        harness = _LifecycleHarness(True)
        harness.toggle_running()
        self.assertEqual(harness.feedback, ["enabled"])


class StaticRecordingWriteTests(unittest.TestCase):
    def test_recording_success_path_requires_load_actions_success(self):
        text = (ROOT / "ui" / "recording_workflow.py").read_text("utf-8")
        start = text.index("    def finish_recording")
        end = text.index("    def preview_recording_import", start)
        method = text[start:end]
        self.assertIn("if self.load_actions(actions, target_card) is not True", method)
        self.assertLess(method.index("if write_rejected:"), method.index("录制完成：已载入"))

    def test_load_actions_returns_true_after_complete_rebuild(self):
        text = (ROOT / "ui" / "editor_workflow.py").read_text("utf-8")
        start = text.index("    def load_actions")
        end = text.index("    def open_action_cleanup_dialog", start)
        method = text[start:end]
        self.assertIn("return False", method)
        self.assertIn("return True", method)


if __name__ == "__main__":
    unittest.main()
