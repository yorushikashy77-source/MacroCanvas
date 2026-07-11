import threading
import unittest
from unittest.mock import patch

from core.constants import ConfigState, MacroState
from config.profiles import DISABLED_LAYER_NAME
from macro.scheduler import MacroController
from ui.action_execution import ActionExecutionMixin
from ui.input_runtime import InputRuntimeMixin
from ui.macro_controls import MacroControlsMixin
from ui.profile_workflow import ProfileWorkflowMixin
from ui.shutdown_coordinator import ShutdownCoordinatorMixin


class _TextStub:
    def __init__(self):
        self.text = ""
        self.style = ""
        self.enabled = True

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _ButtonStub(_TextStub):
    def __init__(self):
        super().__init__()
        self.object_name = ""

    def setObjectName(self, name):
        self.object_name = str(name)


class _EngineStub:
    def __init__(self, running=True):
        self.running = running
        self.layers = []
        self.last_command_error = ""

    def is_running(self):
        return self.running

    def change_layer(self, layer, wait=True, timeout=0):
        self.layers.append((layer, wait, timeout))
        return True

    def release_all_virtual_keys(self, timeout=0):
        return True


class _MacroControllerStub:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}
        self.last_release_failures = []
        self.started = []

    def stop_all(self, timeout=2.5):
        return []

    def force_release_all(self):
        return []

    def is_running(self, _task_id):
        return False

    def start(self, task):
        self.started.append(task)
        return True

    class _Signals:
        def __init__(self):
            self.state_changed = self
        def emit(self):
            pass

    signals = _Signals()


class _MacroStopHarness(MacroControlsMixin):
    def __init__(self):
        self.macro_controller = _MacroControllerStub()
        self.output_shutdown_in_progress = False
        self.last_macro_release_failures = []
        self.input_state_lock = threading.RLock()
        self.held_trigger_ids = {"A": {"p1"}}
        self.kanata_trigger_down = {"k"}
        self._macro_stop_gate_restore = None
        self.active_macro_id = "p1"
        self.last_action_activity = {"id": "p1"}
        self.macro_state = MacroState.RUNNING
        self.macro_status_detail = ""
        self.running = True
        self.recording_session_active = False
        self.engine_hint = _TextStub()
        self.execution_info = _TextStub()
        self.pause_button = _ButtonStub()
        self.stop_current_button = _ButtonStub()
        self.diagnostics = []

    def _remember_runtime_release_state(self):
        return set(), {"A"}

    def _release_all_sync_mappings(self):
        return False

    def _release_runtime_virtual_keys(self, **_kwargs):
        return True

    def _release_interception_output(self):
        return True

    def _runtime_is_game_mode(self):
        return False

    def _kanata_engine_has_runtime(self, _engine):
        return False

    def _discard_profile_suspended_macros(self, reason=""):
        self.discard_reason = reason

    def refresh_status_ui(self):
        pass

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    engine = _EngineStub(False)
    keyboard_engine = _EngineStub(False)


class _KanataTriggerHarness(InputRuntimeMixin):
    def __init__(self):
        self.running = True
        self.engine = _EngineStub(True)
        self.profile_switch_in_progress = False
        self.profile_trigger_allowed = True
        self.active_profile_layer = "layer-a"
        self.output_shutdown_in_progress = True
        self.last_macro_release_failures = []
        self.macro_state = MacroState.IDLE
        self.data_lock = threading.RLock()
        self.runtime_trigger_rules = [{
            "id": "preset-1",
            "enabled": True,
            "_runtime_kind": "preset",
            "name": "Preset",
            "mode": "执行一次",
            "actions": [{"type": "等待", "duration_ms": 1}],
        }]
        self.kanata_trigger_down = set()
        self.macro_controller = _MacroControllerStub()
        self.diagnostics = []

    def _runtime_is_game_mode(self):
        return False

    def _macro_backend_active(self):
        return True

    def _preset_as_mapping_rule(self, preset):
        return dict(preset)

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _MenuHarness(ActionExecutionMixin):
    def __init__(self):
        self.config_state = ConfigState.SAVED
        self.output_shutdown_in_progress = True
        self.last_macro_release_failures = ["Kanata 虚拟键"]
        self.macro_controller = _MacroControllerStub()
        self._test_countdown_generation = 0
        self._test_countdown_preset_id = None
        self.preset_cards = [type("Card", (), {
            "preset_id": "p1",
            "name": _TextStub(),
        })()]
        self.preset_cards[0].name.setText("Preset")
        self.warnings = []

    def selected_preset_row(self):
        return 0

    def _macro_backend_active(self):
        return True

    def write_diagnostic(self, *_args, **_kwargs):
        pass


class _FinishedTask:
    def __init__(self, cleanup_failed=True):
        self.release_cleanup_failed = cleanup_failed
        self._live = False
        self.preset = {"id": "p1", "name": "Preset"}

    def has_live_threads(self):
        return self._live


class _EngineActive:
    def is_running(self):
        return True


class _ShutdownController:
    def __init__(self):
        self.last_release_failures = []

    def stop_all(self, timeout=2.5):
        self.last_release_failures = ["p1"]
        return []

    def force_release_all(self):
        return []


class _ShutdownHarness(ShutdownCoordinatorMixin):
    def __init__(self):
        self.macro_controller = _ShutdownController()
        self.diagnostics = []

    def _shutdown_issue(self, issues, step, error=None, detail="", critical=False):
        item = {"step": step, "message": detail or str(error), "critical": critical}
        issues.append(item)
        return item


class _PauseTask:
    def __init__(self):
        self.preset = {"id": "p1", "name": "Preset"}
        self.run_event = threading.Event()
        self.run_event.set()

    def pause(self):
        return False

    def resume(self):
        return True


class _PauseHarness(_MacroStopHarness):
    def __init__(self):
        super().__init__()
        task = _PauseTask()
        self.macro_controller.tasks = {"p1": task}

    def _play_feedback(self, _name):
        pass


class _EnableHarness(InputRuntimeMixin):
    def __init__(self):
        self.output_shutdown_in_progress = True
        self.last_macro_release_failures = ["Kanata 虚拟键"]
        self.macro_state = MacroState.STOP_TIMEOUT
        self.running = True
        self.direct_interception_active = False
        self.mappings_enabled = False
        self.engine = _EngineStub(True)
        self.keyboard_engine = _EngineStub(False)
        self.active_profile_layer = "layer"
        self.engine_hint = _TextStub()
        self.macro_controller = _MacroControllerStub()

    def _runtime_is_game_mode(self):
        return False

    def _play_feedback(self, _name):
        pass

    def refresh_status_ui(self):
        pass

    def write_diagnostic(self, *_args, **_kwargs):
        pass


class _ResumeTask:
    def __init__(self):
        self.run_event = threading.Event()
        self.stop_event = threading.Event()

    def has_live_threads(self):
        return True

    def resume(self):
        return False


class _ProfileRestoreHarness(ProfileWorkflowMixin):
    def __init__(self):
        self._shutdown_started = False
        self.running = True
        self.settings_input_mode_active = False
        self.recording_session_active = False
        self.output_shutdown_in_progress = False
        self.last_macro_release_failures = []
        self.macro_state = MacroState.PAUSED
        self.macro_status_detail = ""
        self.profile_trigger_allowed = False
        self.active_profile_layer = "layer"
        self.mappings_enabled = True
        self.engine = _EngineStub(True)
        self.keyboard_engine = _EngineStub(False)
        self.engine_hint = _TextStub()
        self._profile_input_paused_macro_ids = {"p1"}
        self.profile_input_temporarily_suspended = True
        self.profile_input_suspend_reason = "macrocanvas_foreground"
        self.macro_controller = type("Controller", (), {
            "lock": threading.RLock(),
            "tasks": {"p1": _ResumeTask()},
            "last_release_failures": [],
        })()
        self.diagnostics = []

    def _change_runtime_profile_layer(self, layer, wait=True):
        return self.engine.change_layer(layer, wait=wait)

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    def refresh_status_ui(self):
        pass

    def refresh_macro_controls(self):
        pass


class InteractionRiskFixes38Tests(unittest.TestCase):
    def test_stop_all_macros_keeps_output_gate_closed_on_cleanup_failure(self):
        harness = _MacroStopHarness()

        remaining = harness.stop_all_macros()

        self.assertEqual(remaining, [])
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertIn("同步映射输出", harness.last_macro_release_failures)
        self.assertEqual(harness.macro_state, MacroState.STOP_TIMEOUT)
        self.assertEqual(harness.held_trigger_ids, {})

    def test_kanata_trigger_down_is_blocked_while_output_gate_is_closed(self):
        harness = _KanataTriggerHarness()

        harness.handle_kanata_trigger("layer-a", "preset", "preset-1", "down")

        self.assertEqual(harness.macro_controller.started, [])
        self.assertIn("runtime_output_blocked", [event for event, _ in harness.diagnostics])

    def test_menu_test_refuses_to_start_during_cleanup_latch(self):
        harness = _MenuHarness()

        with patch("ui.action_execution.QMessageBox.warning") as warning:
            harness.test_selected_preset()

        self.assertEqual(harness._test_countdown_generation, 0)
        self.assertEqual(harness.macro_controller.started, [])
        self.assertTrue(warning.called)

    def test_macro_controller_records_finished_task_cleanup_failure_before_pop(self):
        controller = MacroController(_EngineActive())
        controller.tasks["p1"] = _FinishedTask(cleanup_failed=True)

        self.assertFalse(controller.is_running("p1"))

        self.assertEqual(controller.last_release_failures, ["p1"])
        self.assertNotIn("p1", controller.tasks)

    def test_shutdown_reads_stop_all_release_failures(self):
        harness = _ShutdownHarness()
        issues = []

        self.assertTrue(harness._stop_macro_runtime_for_shutdown(issues))

        self.assertEqual(issues[0]["step"], "释放宏任务持有输入")
        self.assertTrue(issues[0]["critical"])
        self.assertIn("p1", issues[0]["message"])

    def test_pause_failure_latches_output_gate(self):
        harness = _PauseHarness()

        harness.toggle_all_macro_pause()

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertIn("Preset", harness.last_macro_release_failures)
        self.assertEqual(harness.macro_state, MacroState.STOP_TIMEOUT)

    def test_mapping_enable_refuses_cleanup_latch(self):
        harness = _EnableHarness()

        self.assertFalse(harness.set_mappings_enabled(True, sound=False))

        self.assertFalse(harness.mappings_enabled)
        self.assertEqual(harness.engine.layers, [])
        self.assertIn("强制释放键鼠", harness.engine_hint.text)

    def test_profile_restore_keeps_suspension_when_macro_resume_fails(self):
        harness = _ProfileRestoreHarness()

        self.assertFalse(harness._restore_active_profile_input(reason="test"))

        self.assertTrue(harness.profile_input_temporarily_suspended)
        self.assertEqual(harness._profile_input_paused_macro_ids, {"p1"})
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertIn("p1", harness.last_macro_release_failures)
        self.assertEqual(harness.engine.layers[-1][0], DISABLED_LAYER_NAME)


if __name__ == "__main__":
    unittest.main()
