import threading
import unittest
from unittest.mock import patch

from core.constants import ConfigState, EngineState, MacroState
from config.profiles import DISABLED_LAYER_NAME
from ui.macro_controls import MacroControlsMixin
from ui.profile_workflow import ProfileWorkflowMixin
from ui.recording_workflow import RecordingWorkflowMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin


class _StyleStub:
    def unpolish(self, _widget):
        pass
    def polish(self, _widget):
        pass


class _TextStub:
    def __init__(self):
        self.text = ""
        self.style_text = ""
        self.name = ""
    def setText(self, text):
        self.text = str(text)
    def setStyleSheet(self, style):
        self.style_text = str(style)
    def setObjectName(self, name):
        self.name = str(name)
    def style(self):
        return _StyleStub()


class _OverlayStub:
    def __init__(self):
        self.messages = []
        self.hidden = False
    def show_message(self, *args):
        self.messages.append(args)
    def hide_message(self):
        self.hidden = True


class _ControllerStub:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}
        self.last_release_failures = []


class _RecordingLeaveHarness(RecordingWorkflowMixin, MacroControlsMixin):
    def __init__(self):
        self.recording_restore_layer = "profile-layer"
        self.running = True
        self.runtime_profile_auto_switch_enabled = False
        self.profile_trigger_allowed = False
        self.output_shutdown_in_progress = True
        self.last_macro_release_failures = ["恢复失败"]
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = ""
        self.profile_switch_in_progress = False
        self.engine_hint = _TextStub()
        self.execution_info = _TextStub()
        self.activity_overlay = _OverlayStub()
        self.recording_session_active = False
        self.macro_controller = _ControllerStub()
        self.restore_calls = 0
        self.diagnostics = []
    def _runtime_is_game_mode(self):
        return False
    def _restore_active_profile_input(self, **_kwargs):
        self.restore_calls += 1
        return False
    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))
    def refresh_status_ui(self):
        pass
    def refresh_macro_controls(self):
        pass


class _ProfileHarness(ProfileWorkflowMixin, MacroControlsMixin):
    def __init__(self):
        self.running = True
        self.settings_dialog_active = True
        self.settings_input_mode_active = True
        self.runtime_profile_auto_switch_enabled = False
        self.recording_session_active = False
        self.profile_trigger_allowed = True
        self.output_shutdown_in_progress = False
        self.last_macro_release_failures = ["恢复失败"]
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = ""
        self.active_profile_id = "p1"
        self.active_profile_layer = "layer-p1"
        self.mappings_enabled = True
        self._shutdown_started = False
        self._profile_input_paused_macro_ids = {"task1"}
        self.profile_input_temporarily_suspended = True
        self.profile_input_suspend_reason = "test"
        self.profile_switch_in_progress = False
        self.input_state_lock = threading.RLock()
        self.held_trigger_ids = {}
        self.kanata_trigger_down = set()
        self.suppressed_trigger_names = set()
        self.active_sync_by_source = {}
        self.physical_modifiers = set()
        self.physical_down = set()
        self.physical_input_sources = {}
        self.interception_forwarded_down = set()
        self.expected_kanata_event_lock = threading.RLock()
        self.expected_kanata_events = []
        self.system_hotkey_latched = set()
        self.system_hotkey_latched_sources = {}
        self.recording_control_modifiers = set()
        self.recording_control_sources = {}
        self.engine_hint = _TextStub()
        self.execution_info = _TextStub()
        self.activity_overlay = _OverlayStub()
        self.macro_controller = _ControllerStub()
        self.engine = type("Engine", (), {"last_command_error": "", "change_layer": lambda *_a, **_k: True})()
        self.keyboard_engine = type("Keyboard", (), {"last_command_error": ""})()
        self.layer_changes = []
        self.refreshed = 0
        self.diagnostics = []
    def _runtime_cleanup_blocks_new_output(self):
        return False
    def _runtime_profile_entry(self, profile_id):
        if profile_id == "p1":
            return {"id": "p1", "layer": "layer-p1"}
        return None
    def _runtime_is_game_mode(self):
        return False
    def _change_runtime_profile_layer(self, layer, **_kwargs):
        self.layer_changes.append(layer)
        return True
    def _clear_process_guard_input_state(self):
        pass
    def _refresh_logical_physical_sets_locked(self):
        pass
    def _resume_profile_suspended_macros(self, **_kwargs):
        self._remember_macro_cleanup_failure("恢复失败", ["task1"])
        return False
    def _restore_active_profile_input(self, **_kwargs):
        self._remember_macro_cleanup_failure("设置关闭恢复失败", ["设置"])
        return False
    def refresh_status_ui(self):
        self.refreshed += 1
    def refresh_macro_controls(self):
        pass
    def refresh_profile_selector_state(self):
        pass
    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _StartupEngine:
    def __init__(self):
        self.last_command_error = ""
        self.stopped = False
    def start(self, *_args, **_kwargs):
        return True, "Kanata 已启动"
    def change_layer(self, *_args, **_kwargs):
        return False
    def stop(self, **_kwargs):
        self.stopped = True
        return True
    def is_running(self):
        return False


class _RuntimeStartHarness(RuntimeLifecycleMixin):
    def __init__(self):
        self._shutdown_started = False
        self.runtime_diagnostic_enabled = False
        self.running = False
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = False
        self.backend_combo = type("Combo", (), {"currentText": lambda _self: "普通模式"})()
        self.input_state_lock = threading.RLock()
        self.expected_kanata_event_lock = threading.RLock()
        self.physical_down = set()
        self.physical_modifiers = set()
        self.physical_input_sources = {}
        self.held_trigger_ids = {}
        self.kanata_trigger_down = set()
        self.suppressed_trigger_names = set()
        self.interception_forwarded_down = set()
        self.expected_kanata_events = []
        self.system_hotkey_latched = set()
        self.system_hotkey_latched_sources = {}
        self.recording_control_modifiers = set()
        self.recording_control_sources = {}
        self.config_state = ConfigState.APPLIED
        self.auto_apply_checkbox = type("Auto", (), {"isChecked": lambda _self: False})()
        self.active_profile_layer = "layer-p1"
        self.interception_input_hook = None
        self.interception_output = None
        self.keyboard_engine = type("Keyboard", (), {"is_running": lambda _self: False, "stop": lambda _self, **_k: True})()
        self.engine = _StartupEngine()
        self.engine_state = EngineState.STOPPED
        self.engine_hint = _TextStub()
        self.toggle_button = _TextStub()
        self._config_apply_transaction_active = False
        self.applied_config_payload = None
        self.config_state = ConfigState.APPLIED
        self.mappings_enabled = False
        self.direct_interception_active = False
        self.refreshed = 0
        self.diagnostics = []
    def reset_diagnostic_log(self):
        pass
    def _set_loading_message(self, *_args):
        pass
    def _generated_kanata_configs_current(self):
        return True
    def generate_kanata_config(self):
        return True
    def _validate_selected_backend(self):
        return True, "ok"
    def _snapshot_runtime_config(self):
        pass
    def _runtime_is_game_mode(self):
        return False
    def update_global_hook_for_backend(self):
        return True
    def _reseed_physical_input_state(self, **_kwargs):
        pass
    def _record_applied_config_snapshot(self, *_args, **_kwargs):
        pass
    def _kanata_engine_has_runtime(self, engine):
        return False
    def refresh_status_ui(self):
        self.refreshed += 1
    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _ProgressPendingHarness(MacroControlsMixin):
    def __init__(self):
        self.recording_session_active = False
        self.last_macro_release_failures = []
        self.output_shutdown_in_progress = True
        self.macro_state = MacroState.STOPPING
        self.macro_status_detail = "正在停止"
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


class InteractionRiskFixes42Tests(unittest.TestCase):
    def test_recording_leave_propagates_restore_failure(self):
        h = _RecordingLeaveHarness()
        with patch("ui.recording_workflow.foreground_window_belongs_to_current_process", return_value=False):
            self.assertFalse(h._leave_recording_input_mode())
        self.assertEqual(h.restore_calls, 1)

    def test_settings_leave_reports_restore_failure(self):
        h = _ProfileHarness()
        with patch("ui.profile_workflow.foreground_window_belongs_to_current_process", return_value=False):
            self.assertFalse(h._leave_settings_input_mode())
        self.assertFalse(h.profile_trigger_allowed)
        self.assertTrue(h.output_shutdown_in_progress)
        self.assertIn("设置", h.last_macro_release_failures)

    def test_same_profile_reactivation_resume_failure_disables_layer(self):
        h = _ProfileHarness()
        h.settings_input_mode_active = False
        result = h._activate_profile_by_id("p1", reason="unit")
        self.assertFalse(result)
        self.assertFalse(h.profile_trigger_allowed)
        self.assertTrue(h.output_shutdown_in_progress)
        self.assertIn(DISABLED_LAYER_NAME, h.layer_changes)
        self.assertIn("task1", h.last_macro_release_failures)

    def test_profile_switch_rollback_resume_failure_disables_layer(self):
        h = _ProfileHarness()
        h.settings_input_mode_active = False
        h.active_profile_id = "p1"
        # Target profile exists but installing its runtime fails, forcing rollback.
        def entry(profile_id):
            if profile_id == "p1":
                return {"id": "p1", "layer": "layer-p1"}
            if profile_id == "p2":
                return {"id": "p2", "layer": "layer-p2"}
            return None
        h._runtime_profile_entry = entry
        h.stop_all_macros = lambda **_kwargs: []
        h._install_runtime_profile = lambda *_args, **_kwargs: False
        h._release_all_sync_mappings = lambda: True
        h._release_runtime_virtual_keys = lambda **_kwargs: True
        h._release_interception_output = lambda: True
        h._failsafe_release_runtime_targets = lambda **_kwargs: True
        self.assertFalse(h._activate_profile_by_id("p2", reason="unit"))
        self.assertFalse(h.profile_trigger_allowed)
        self.assertTrue(h.output_shutdown_in_progress)
        self.assertIn(DISABLED_LAYER_NAME, h.layer_changes)

    def test_kanata_start_partial_failure_clears_mappings_enabled(self):
        h = _RuntimeStartHarness()
        with patch("ui.runtime_lifecycle.QMessageBox.warning"):
            result = h._set_running_impl(True)
        self.assertFalse(result)
        self.assertFalse(h.running)
        self.assertFalse(h.mappings_enabled)
        self.assertFalse(h.profile_trigger_allowed)
        self.assertEqual(h.engine_state, EngineState.FAILED)

    def test_cleanup_pending_progress_does_not_show_release_failure(self):
        h = _ProgressPendingHarness()
        h.update_macro_progress({
            "id": "p1", "name": "Preset", "loop": 1,
            "loop_total": 1, "step": 1, "step_total": 1,
            "action": "动作", "paused": False,
        })
        self.assertEqual(h.macro_state, MacroState.STOPPING)
        self.assertEqual(h.last_macro_release_failures, [])
        self.assertNotIn("按键释放未完成", h.engine_hint.text)
        self.assertTrue(any(event == "macro_progress_suppressed" for event, _ in h.diagnostics))


if __name__ == "__main__":
    unittest.main()
