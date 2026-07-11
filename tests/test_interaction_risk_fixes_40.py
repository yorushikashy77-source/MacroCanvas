import threading
import unittest
from unittest.mock import patch

from core.constants import EngineState, MacroState
from macro.scheduler import MacroController
from ui.input_runtime import InputRuntimeMixin
from ui.macro_controls import MacroControlsMixin
from ui.mapping_editor import MappingEditorMixin
from ui.preset_editor import PresetEditorMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin
from ui.main_window import MainWindow


class _TextStub:
    def __init__(self):
        self.text = ""
        self.style = ""
        self.object_name = ""

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)

    def setObjectName(self, name):
        self.object_name = str(name)

    def setEnabled(self, _enabled):
        pass


class _EngineStub:
    def __init__(self, running=False):
        self.running = bool(running)
        self.last_command_error = ""

    def is_running(self):
        return self.running

    def release_all_virtual_keys(self, **_kwargs):
        return True

    def stop(self, timeout=0):
        self.running = False
        return True


class _MacroControllerStub:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}
        self.last_release_failures = []
        self.stopped = []

    def stop(self, preset_id):
        self.stopped.append(str(preset_id))
        return True

    def remaining_task_details(self):
        return {}


class _SyncKanataHarness(InputRuntimeMixin, MacroControlsMixin):
    def __init__(self):
        self.suspended_preset_ids = set()
        self.suspended_mapping_ids = set()
        self.input_state_lock = threading.RLock()
        self.sync_output_lock = threading.RLock()
        self.active_sync_by_source = {
            "src": {
                "m1": {"id": "m1", "target_modifiers": "无", "target": "A"}
            }
        }
        self.sync_output_counts = {
            ("无", "A"): {
                "count": 1,
                "action": {"type": "键盘点击", "modifiers": "无", "target": "A"},
            }
        }
        self.last_macro_release_failures = []
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        self.macro_controller = _MacroControllerStub()
        self.diagnostics = []

    def _send_kanata_action(self, _action, phase):
        return phase != "Release"

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _StopAllFailureRuntimeHarness(RuntimeLifecycleMixin):
    def __init__(self):
        self.running = True
        self.runtime_diagnostic_enabled = False
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.engine_state = EngineState.RUNNING
        self.backend_combo = type("Combo", (), {"currentText": lambda _self: "普通模式"})()
        self.engine_hint = _TextStub()
        self.toggle_button = _TextStub()
        self.engine = _EngineStub(True)
        self.keyboard_engine = _EngineStub(False)
        self.interception_output = None
        self.interception_input_hook = None
        self.global_hook = None
        self.direct_interception_active = False
        self.mappings_enabled = False
        self.active_profile_layer = "layer-a"
        self.last_macro_release_failures = []
        self.input_state_lock = threading.RLock()
        self.sync_output_lock = threading.RLock()
        self.held_trigger_ids = {}
        self.kanata_trigger_down = set()
        self.suppressed_trigger_names = set()
        self.interception_forwarded_down = set()
        self.expected_kanata_event_lock = threading.RLock()
        self.expected_kanata_events = []
        self.system_hotkey_latched = set()
        self.system_hotkey_latched_sources = {}
        self.recording_control_modifiers = set()
        self.recording_control_sources = {}
        self.activity_overlay = type("Overlay", (), {"hide_message": lambda _self: None})()
        self.runtime_release_target_history = set()
        self.runtime_release_vkey_history = set()
        self.active_sync_by_source = {}
        self.sync_output_counts = {}
        self.active_macro_id = "p1"
        self.last_action_activity = {}
        self.macro_state = MacroState.RUNNING
        self.macro_status_detail = ""
        self.refreshed = 0
        self.diagnostics = []

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    def _set_loading_message(self, *_args):
        pass

    def _runtime_is_game_mode(self):
        return False

    def _change_runtime_profile_layer(self, *_args, **_kwargs):
        return True

    def _kanata_engine_has_runtime(self, engine):
        return bool(engine and engine.is_running())

    def stop_all_macros(self, **_kwargs):
        self.last_macro_release_failures = ["宏任务释放失败(1)"]
        self.output_shutdown_in_progress = True
        return []

    def refresh_status_ui(self):
        self.refreshed += 1


class _TransactionFailureHarness(_StopAllFailureRuntimeHarness):
    def _release_all_sync_mappings(self):
        return True

    def _release_runtime_virtual_keys(self, **_kwargs):
        return True

    def _release_interception_output(self):
        return True

    def _failsafe_release_runtime_targets(self, **_kwargs):
        return True


class _ExistingTask:
    def __init__(self):
        self.release_cleanup_failed = True
        self.preset = {"id": "p1"}

    def has_live_threads(self):
        return False


class _ControllerEngineActive:
    def is_running(self):
        return True


class _DeleteMappingHarness(MappingEditorMixin):
    def __init__(self):
        self.suspended_mapping_ids = set()
        self.data_lock = threading.RLock()
        self.input_state_lock = threading.RLock()
        self.runtime_mappings = [{"id": "m1"}]
        self.runtime_trigger_rules = []
        self.running = False
        self.kanata_trigger_down = set()
        self.macro_controller = _MacroControllerStub()
        self.held_trigger_ids = {}
        self.active_sync_by_source = {}
        self.stopped_via_guard = []

    def _visible_editor_profile_id(self):
        return "profile-a"

    def _request_stop_macro_task(self, preset_id, reason=""):
        self.stopped_via_guard.append((preset_id, reason))
        return True

    def _release_detached_sync_mappings(self, ownership):
        return [], []


class _DeletePresetHarness(PresetEditorMixin):
    def __init__(self):
        self._test_countdown_preset_id = None
        self._test_countdown_generation = 0
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        self.macro_controller = _MacroControllerStub()
        self.input_state_lock = threading.RLock()
        self.held_trigger_ids = {}
        self.kanata_trigger_down = set()
        self.stopped_via_guard = []

    def _request_stop_macro_task(self, preset_id, reason=""):
        self.stopped_via_guard.append((preset_id, reason))
        return True

    def refresh_status_ui(self):
        pass


class _ForceReleaseHarness:
    def __init__(self, success=True):
        self.running = False
        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        self.last_macro_release_failures = []
        self.macro_controller = _MacroControllerStub()
        self.macro_controller.last_release_failures = ["old"]
        self.active_macro_id = "p1"
        self.last_action_activity = {"id": "p1"}
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = "old"
        self.settings_input_mode_active = False
        self.recording_session_active = False
        self._shutdown_started = False
        self.mappings_enabled = False
        self.active_profile_layer = "layer-a"
        self.interception_output = None
        self.quarantined_mouse_release_lock = threading.RLock()
        self.quarantined_mouse_releases = []
        self.output_dispatch_lock = threading.RLock()
        self.engine = _EngineStub(False)
        self.keyboard_engine = _EngineStub(False)
        self.runtime_release_target_history = set()
        self.runtime_release_vkey_history = set()
        self.engine_hint = _TextStub()
        self.execution_info = _TextStub()
        self.refreshed = 0
        self.diagnostics = []
        self.success = success

    def held_input_snapshot(self):
        return []

    def stop_all_macros(self, **_kwargs):
        return []

    def _runtime_is_game_mode(self):
        return False

    def _change_runtime_profile_layer(self, *_args, **_kwargs):
        return True

    def _retry_quarantined_mouse_releases(self, **_kwargs):
        return self.success

    def _release_all_sync_mappings(self):
        return self.success

    def _kanata_engine_has_runtime(self, _engine):
        return False

    def _failsafe_release_runtime_targets(self, **_kwargs):
        return self.success

    def _start_stop_release_guard(self):
        pass

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    def refresh_status_ui(self):
        self.refreshed += 1

    def refresh_macro_controls(self):
        pass

    def _remember_macro_cleanup_failure(self, title, failures=None):
        remembered = list(self.last_macro_release_failures)
        for item in failures or []:
            if item not in remembered:
                remembered.append(item)
        self.last_macro_release_failures = remembered
        self.output_shutdown_in_progress = True
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = title


class InteractionRiskFixes40Tests(unittest.TestCase):
    def test_kanata_sync_release_failure_latches_cleanup_state(self):
        harness = _SyncKanataHarness()
        rule = {"id": "m1", "mode": "同步按住", "target_modifiers": "无", "target": "A"}

        result = harness._dispatch_runtime_mapping_rule(
            rule, "src", False, False, trigger_name="A"
        )

        self.assertTrue(result)
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertEqual(harness.macro_state, MacroState.STOP_TIMEOUT)
        self.assertIn("同步映射释放失败(1)", harness.last_macro_release_failures)
        self.assertIn("m1", harness.active_sync_by_source["src"])

    def test_stop_engine_refuses_stop_all_release_failures(self):
        harness = _StopAllFailureRuntimeHarness()

        with patch("ui.runtime_lifecycle.QMessageBox.warning") as warning:
            result = harness._set_running_impl(False)

        self.assertFalse(result)
        self.assertTrue(harness.running)
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertEqual(harness.engine_state, EngineState.FAILED)
        self.assertIn("宏任务释放失败(1)", harness.last_macro_release_failures)
        self.assertTrue(warning.called)

    def test_config_transaction_refuses_stop_all_release_failures(self):
        harness = _TransactionFailureHarness()

        with self.assertRaisesRegex(RuntimeError, "输入未能确认释放"):
            harness._stop_runtime_backends_for_transaction()

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertEqual(harness.engine_state, EngineState.FAILED)
        self.assertIn("宏任务释放失败(1)", harness.last_macro_release_failures)

    def test_controller_start_refuses_stale_cleanup_failure_replacement(self):
        controller = MacroController(_ControllerEngineActive())
        controller.tasks["p1"] = _ExistingTask()

        result = controller.start({"id": "p1", "name": "Preset", "actions": []})

        self.assertFalse(result)
        self.assertEqual(controller.last_release_failures, ["p1"])
        self.assertIsInstance(controller.tasks["p1"], _ExistingTask)

    def test_delete_mapping_uses_single_task_stop_guard(self):
        harness = _DeleteMappingHarness()

        harness._suspend_mapping_runtime_for_delete({"id": "m1"})

        self.assertEqual(harness.stopped_via_guard[0][0], "mapping:m1")
        self.assertFalse(harness.macro_controller.stopped)

    def test_delete_preset_uses_single_task_stop_guard(self):
        harness = _DeletePresetHarness()

        harness._stop_preset_runtime_for_delete("p1")

        stopped_ids = {item[0] for item in harness.stopped_via_guard}
        self.assertIn("p1", stopped_ids)
        self.assertFalse(harness.macro_controller.stopped)

    def test_force_release_success_clears_gate_when_engine_already_stopped(self):
        harness = _ForceReleaseHarness(success=True)

        result = MainWindow.force_release_held_inputs(
            harness, show_feedback=False
        )

        self.assertTrue(result)
        self.assertFalse(harness.output_shutdown_in_progress)
        self.assertEqual(harness.last_macro_release_failures, [])
        self.assertEqual(harness.macro_controller.last_release_failures, [])
        self.assertEqual(harness.macro_state, MacroState.IDLE)

    def test_force_release_failure_latches_explicit_failure_state(self):
        harness = _ForceReleaseHarness(success=False)

        result = MainWindow.force_release_held_inputs(
            harness, show_feedback=False
        )

        self.assertFalse(result)
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertEqual(harness.macro_state, MacroState.STOP_TIMEOUT)
        self.assertTrue(harness.last_macro_release_failures)


if __name__ == "__main__":
    unittest.main()
