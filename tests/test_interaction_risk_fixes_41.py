import threading
import unittest
from unittest.mock import patch

from core.constants import ConfigState, MacroState
from ui.action_execution import ActionExecutionMixin
from ui.input_runtime import InputRuntimeMixin
from ui.macro_controls import MacroControlsMixin
from ui.recording_workflow import RecordingWorkflowMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin


class _TextStub:
    def __init__(self):
        self.text = ""
        self.style = ""

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)


class _NameStub:
    def __init__(self, text=""):
        self._text = str(text)

    def setText(self, text):
        self._text = str(text)

    def text(self):
        return self._text


class _OverlayStub:
    def __init__(self):
        self.hidden = False
        self.messages = []

    def hide_message(self):
        self.hidden = True

    def show_message(self, *args):
        self.messages.append(args)


class _ControllerStub:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}
        self.last_release_failures = []

    def stop_all(self):
        return []

    def force_release_all(self):
        return []


class _UiLatchHarness(MacroControlsMixin):
    def __init__(self):
        self.recording_session_active = False
        self.last_macro_release_failures = ["旧释放失败"]
        self.output_shutdown_in_progress = True
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = "释放失败"
        self.macro_controller = _ControllerStub()
        self.engine_hint = _TextStub()
        self.execution_info = _TextStub()
        self.activity_overlay = _OverlayStub()
        self.last_action_activity = {}
        self.active_macro_id = None
        self.refreshed = 0
        self.controls_refreshed = 0
        self.diagnostics = []

    def refresh_status_ui(self):
        self.refreshed += 1

    def refresh_macro_controls(self):
        self.controls_refreshed += 1

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _DeferredRestoreHarness(MacroControlsMixin):
    def __init__(self):
        self.macro_controller = _ControllerStub()
        self.last_macro_release_failures = []
        self.output_shutdown_in_progress = False
        self._macro_stop_gate_restore = False
        self._deferred_profile_input_restore = {"layer": "x"}
        self.active_macro_id = "p1"
        self.last_action_activity = {"id": "p1"}
        self.macro_state = MacroState.STOPPING
        self.macro_status_detail = "stopping"
        self.engine_hint = _TextStub()
        self.execution_info = _TextStub()
        self.activity_overlay = _OverlayStub()
        self.recording_session_active = False
        self.running = True
        self.discarded = []
        self.refreshed = 0
        self.controls_refreshed = 0
        self.diagnostics = []

    def _apply_deferred_profile_input_restore(self):
        self.last_macro_release_failures = ["恢复映射层失败"]
        self.output_shutdown_in_progress = True
        self.macro_state = MacroState.STOP_TIMEOUT
        return False

    def _discard_profile_suspended_macros(self, reason=""):
        self.discarded.append(reason)

    def refresh_status_ui(self):
        self.refreshed += 1

    def refresh_macro_controls(self):
        self.controls_refreshed += 1

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _MappingDisableHarness(InputRuntimeMixin, MacroControlsMixin):
    def __init__(self):
        self.running = True
        self.mappings_enabled = True
        self.output_shutdown_in_progress = False
        self.last_macro_release_failures = []
        self.profile_trigger_allowed = True
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        self.engine_hint = _TextStub()
        self.execution_info = _TextStub()
        self.activity_overlay = _OverlayStub()
        self.recording_session_active = False
        self.input_state_lock = threading.RLock()
        self.held_trigger_ids = {}
        self.kanata_trigger_down = set()
        self.macro_controller = _ControllerStub()
        self.refreshed = 0
        self.controls_refreshed = 0
        self.diagnostics = []

    def _runtime_is_game_mode(self):
        return True

    def _interception_source_ready(self):
        return True

    def stop_all_macros(self, **_kwargs):
        return []

    def _failsafe_release_runtime_targets(self, **_kwargs):
        return False

    def _play_feedback(self, _name):
        pass

    def refresh_status_ui(self):
        self.refreshed += 1

    def refresh_macro_controls(self):
        self.controls_refreshed += 1

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _RecordingRestoreHarness(RecordingWorkflowMixin, MacroControlsMixin):
    def __init__(self):
        self.recording_restore_pending = True
        self.recording_workflow_complete = True
        self.recording_session_active = True
        self.running = True
        self.last_macro_release_failures = []
        self.output_shutdown_in_progress = False
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = "recording"
        self.engine_hint = _TextStub()
        self.execution_info = _TextStub()
        self.activity_overlay = _OverlayStub()
        self.recording_control_modifiers = set()
        self.recording_control_sources = {}
        self.runtime_global_toggle_enabled = False
        self.auto_apply_checkbox = None
        self.config_state = ConfigState.SAVED
        self._auto_apply_deferred_for_recording = False
        self.macro_controller = _ControllerStub()
        self.refreshed = 0
        self.config_actions_enabled = []
        self.diagnostics = []

    def _recording_control_still_held(self):
        return False

    def _set_recording_configuration_actions_enabled(self, enabled):
        self.config_actions_enabled.append(bool(enabled))

    def _unlatch_system_hotkey(self, *_args, **_kwargs):
        pass

    def _leave_recording_input_mode(self):
        self.last_macro_release_failures = ["录制恢复失败"]
        self.output_shutdown_in_progress = True
        self.macro_state = MacroState.STOP_TIMEOUT
        return False

    def update_global_hook_for_backend(self):
        return False

    def _runtime_is_game_mode(self):
        return False

    def refresh_status_ui(self):
        self.refreshed += 1

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _RuntimeTransactionHarness(RuntimeLifecycleMixin):
    def __init__(self):
        self.last_macro_release_failures = ["旧失败"]
        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        self.running = True
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = "old"
        self.refreshed = 0
        self.diagnostics = []

    def stop_all_macros(self, **_kwargs):
        # The method under test must not clear this list before stop_all_macros runs.
        self.seen_failures_at_stop = list(self.last_macro_release_failures)
        return []

    def refresh_status_ui(self):
        self.refreshed += 1

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _MenuStartFailHarness(ActionExecutionMixin, MacroControlsMixin):
    def __init__(self):
        self.config_state = ConfigState.SAVED
        self.output_shutdown_in_progress = False
        self.last_macro_release_failures = []
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        self._test_countdown_generation = 0
        self._test_countdown_preset_id = None
        self.engine_hint = _TextStub()
        self.execution_info = _TextStub()
        self.activity_overlay = _OverlayStub()
        self.active_profile_id = "profile-a"
        self.editor_profile_id = "profile-a"
        self.mappings_enabled = True
        self.data_lock = threading.RLock()
        self.runtime_presets = [{
            "id": "p1", "name": "Preset", "enabled": True,
            "actions": [{"type": "等待", "duration": 1}],
        }]
        self.preset_cards = [type("Card", (), {
            "preset_id": "p1",
            "name": _NameStub("Preset"),
        })()]
        self.macro_controller = type("Controller", (), {})()
        self.macro_controller.lock = threading.RLock()
        self.macro_controller.tasks = {}
        self.macro_controller.last_release_failures = []
        def start(_task):
            self.macro_controller.last_release_failures = ["p1"]
            return False
        self.macro_controller.start = start
        self.refreshed = 0
        self.controls_refreshed = 0
        self.diagnostics = []

    def selected_preset_row(self):
        return 0

    def _macro_backend_active(self):
        return True

    def _preset_as_mapping_rule(self, preset):
        return dict(preset)

    def mapping_to_task(self, rule):
        return dict(rule)

    def refresh_status_ui(self):
        self.refreshed += 1

    def refresh_macro_controls(self):
        self.controls_refreshed += 1

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class InteractionRiskFixes41Tests(unittest.TestCase):
    def test_progress_signal_does_not_overwrite_cleanup_failure(self):
        h = _UiLatchHarness()
        h.update_macro_progress({
            "id": "other", "name": "Other", "loop": 1,
            "loop_total": 1, "step": 1, "step_total": 1,
            "action": "动作", "paused": False,
        })
        self.assertEqual(h.macro_state, MacroState.STOP_TIMEOUT)
        self.assertTrue(h.output_shutdown_in_progress)
        self.assertIn("旧释放失败", h.last_macro_release_failures)
        self.assertIn("按键释放未完成", h.engine_hint.text)

    def test_action_signal_does_not_overwrite_cleanup_failure(self):
        h = _UiLatchHarness()
        h.update_action_activity({"id": "other", "name": "Other", "action": "动作"})
        self.assertEqual(h.macro_state, MacroState.STOP_TIMEOUT)
        self.assertTrue(h.output_shutdown_in_progress)
        self.assertIn("旧释放失败", h.last_macro_release_failures)

    def test_deferred_restore_failure_keeps_stop_timeout_state(self):
        h = _DeferredRestoreHarness()
        h._poll_stopping_macros()
        self.assertEqual(h.macro_state, MacroState.STOP_TIMEOUT)
        self.assertTrue(h.output_shutdown_in_progress)
        self.assertIn("恢复映射层失败", h.last_macro_release_failures)
        self.assertEqual(h.discarded, [])

    def test_mapping_disable_failsafe_failure_latches_cleanup(self):
        h = _MappingDisableHarness()
        self.assertFalse(h.set_mappings_enabled(False))
        self.assertEqual(h.macro_state, MacroState.STOP_TIMEOUT)
        self.assertTrue(h.output_shutdown_in_progress)
        self.assertIn("系统兜底释放", h.last_macro_release_failures)

    def test_recording_restore_failure_does_not_set_idle(self):
        h = _RecordingRestoreHarness()
        self.assertTrue(h._complete_recording_restore_if_ready())
        self.assertEqual(h.macro_state, MacroState.STOP_TIMEOUT)
        self.assertTrue(h.output_shutdown_in_progress)
        self.assertIn("录制恢复失败", h.last_macro_release_failures)

    def test_stop_all_merges_previous_failures_only_when_new_failure_occurs(self):
        class H(MacroControlsMixin):
            def __init__(self):
                self.output_shutdown_in_progress = False
                self.output_dispatch_lock = threading.RLock()
                self.input_state_lock = threading.RLock()
                self.held_trigger_ids = {}
                self.kanata_trigger_down = set()
                self.last_macro_release_failures = ["旧失败"]
                self.macro_controller = _ControllerStub()
                self.macro_state = MacroState.IDLE
                self.macro_status_detail = ""
                self.running = True
                self.engine_hint = _TextStub()
                self.execution_info = _TextStub()
                self.activity_overlay = _OverlayStub()
                self.recording_session_active = False
                self._test_countdown_generation = 0
                self._test_countdown_preset_id = None
                self.diagnostics = []
                self.refreshed = 0
                self.controls_refreshed = 0
            def _remember_runtime_release_state(self): return set(), set()
            def _release_all_sync_mappings(self): return False
            def _release_runtime_virtual_keys(self, **_kwargs): return True
            def _release_interception_output(self): return True
            def _runtime_is_game_mode(self): return True
            def _discard_profile_suspended_macros(self, reason=""): pass
            def refresh_status_ui(self): self.refreshed += 1
            def refresh_macro_controls(self): self.controls_refreshed += 1
            def write_diagnostic(self, event, **payload): self.diagnostics.append((event, payload))
        h = H()
        h.stop_all_macros()
        self.assertIn("旧失败", h.last_macro_release_failures)
        self.assertIn("同步映射输出", h.last_macro_release_failures)
        self.assertTrue(h.output_shutdown_in_progress)

    def test_menu_test_start_failure_keeps_late_cleanup_latch(self):
        h = _MenuStartFailHarness()
        with patch("ui.action_execution.QTimer.singleShot", side_effect=lambda _ms, cb: cb()), \
             patch("ui.action_execution.QMessageBox.information"), \
             patch("ui.action_execution.QMessageBox.warning"):
            h.test_selected_preset()
        self.assertEqual(h.macro_state, MacroState.STOP_TIMEOUT)
        self.assertTrue(h.output_shutdown_in_progress)
        self.assertIn("宏任务释放失败(1)", h.last_macro_release_failures)


if __name__ == "__main__":
    unittest.main()
