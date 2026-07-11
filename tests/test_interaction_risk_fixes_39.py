import threading
import unittest
from unittest.mock import patch

from core.constants import EngineState, MacroState, ConfigState
from macro.scheduler import MacroController
from ui.action_execution import ActionExecutionMixin
from ui.input_runtime import InputRuntimeMixin
from ui.macro_controls import MacroControlsMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin
from ui.shutdown_coordinator import ShutdownCoordinatorMixin


class _TextStub:
    def __init__(self):
        self.text = ""
        self.style = ""

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)


class _ButtonStub(_TextStub):
    def setEnabled(self, _enabled):
        pass


class _NameStub:
    def __init__(self):
        self._text = ""

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


class _SyncHarness(InputRuntimeMixin, MacroControlsMixin):
    def __init__(self):
        self.sync_output_lock = threading.RLock()
        self.input_state_lock = threading.RLock()
        self.active_sync_by_source = {}
        self.sync_output_counts = {
            ("无", "A"): {
                "count": 1,
                "action": {"type": "键盘点击", "target": "A", "modifiers": "无"},
            }
        }
        self.last_macro_release_failures = []
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        self.diagnostics = []

    def _send_kanata_action(self, _action, phase):
        return phase != "Release"

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _ConditionalSyncHarness(_SyncHarness):
    def __init__(self):
        super().__init__()
        self.physical_down = set()
        self.held_trigger_ids = {}
        self.active_sync_by_source = {
            "src": {
                "m1": {
                    "id": "m1",
                    "target_modifiers": "无",
                    "target": "A",
                    "condition_enabled": True,
                    "condition_input": "鼠标左键",
                    "condition_state": "按住时",
                }
            }
        }

    def _runtime_mapping_rules(self):
        return []


class _FinishedTask:
    def __init__(self, preset_id="p1", cleanup_failed=True):
        self.preset = {"id": preset_id, "name": preset_id}
        self.release_cleanup_failed = cleanup_failed
        self.stopped = False

    def stop(self):
        self.stopped = True
        return True

    def wait_for_exit(self, timeout=0):
        return True

    def has_live_threads(self):
        return False


class _EngineActive:
    def is_running(self):
        return True


class _RuntimeStartHarness(RuntimeLifecycleMixin):
    def __init__(self):
        self.running = False
        self.runtime_diagnostic_enabled = False
        self.output_shutdown_in_progress = True
        self.last_macro_release_failures = []
        self.profile_trigger_allowed = True
        self.engine_state = EngineState.STOPPED
        self.backend_combo = type("Combo", (), {"currentText": lambda _self: "普通模式"})()
        self.engine_hint = _TextStub()
        self.refreshed = 0
        self.diagnostics = []

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    def refresh_status_ui(self):
        self.refreshed += 1


class _MenuCountdownHarness(ActionExecutionMixin, MacroControlsMixin):
    def __init__(self):
        self.config_state = ConfigState.SAVED
        self.output_shutdown_in_progress = False
        self.last_macro_release_failures = []
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        self._test_countdown_generation = 0
        self._test_countdown_preset_id = None
        self.engine_hint = _TextStub()
        self.activity_overlay = _OverlayStub()
        self.macro_controller = type("Controller", (), {
            "lock": threading.RLock(),
            "tasks": {},
            "last_release_failures": [],
            "start": lambda _self, _task: True,
        })()
        self.preset_cards = [type("Card", (), {
            "preset_id": "p1",
            "name": _NameStub(),
        })()]
        self.preset_cards[0].name.setText("Preset")
        self.editor_profile_id = "profile-a"
        self.active_profile_id = "profile-a"
        self.mappings_enabled = True
        self.runtime_presets = [{
            "id": "p1",
            "name": "Preset",
            "enabled": True,
            "execution_mode": "执行一次",
            "actions": [{"type": "等待", "duration_ms": 1}],
        }]
        self.data_lock = threading.RLock()
        self.refreshed = 0
        self.diagnostics = []

    def selected_preset_row(self):
        return 0

    def _macro_backend_active(self):
        return True

    def refresh_status_ui(self):
        self.refreshed += 1

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _SingleStopTask:
    def __init__(self):
        self.preset = {"id": "p1", "name": "Preset"}
        self._live = True
        self.run_event = threading.Event()
        self.run_event.set()

    def has_live_threads(self):
        return self._live


class _SingleStopController:
    def __init__(self):
        self.lock = threading.RLock()
        self.task = _SingleStopTask()
        self.tasks = {"p1": self.task}
        self.last_release_failures = []
        self.stopped = []

    def stop(self, preset_id):
        self.stopped.append(preset_id)
        return True


class _SingleStopHarness(MacroControlsMixin):
    def __init__(self):
        self.macro_controller = _SingleStopController()
        self.active_macro_id = "p1"
        self.output_shutdown_in_progress = False
        self._macro_stop_gate_restore = None
        self.last_macro_release_failures = []
        self.last_action_activity = {}
        self.macro_state = MacroState.RUNNING
        self.macro_status_detail = ""
        self.running = True
        self.recording_session_active = False
        self.execution_info = _TextStub()
        self.engine_hint = _TextStub()
        self.pause_button = _ButtonStub()
        self.stop_current_button = _ButtonStub()
        self.refreshed = 0

    def refresh_status_ui(self):
        self.refreshed += 1


class _ShutdownLateController:
    def __init__(self):
        self.last_release_failures = []

    def stop_all(self, timeout=2.5):
        return ["p1"]

    def force_release_all(self):
        return []

    def wait_for_all(self, timeout=6.0):
        self.last_release_failures = ["p1"]
        return []

    def remaining_task_details(self):
        return {}


class _ShutdownLateHarness(ShutdownCoordinatorMixin):
    def __init__(self):
        self.macro_controller = _ShutdownLateController()

    def _shutdown_issue(self, issues, step, error=None, detail="", critical=False):
        item = {"step": step, "message": detail or str(error), "critical": critical}
        issues.append(item)
        return item


class InteractionRiskFixes39Tests(unittest.TestCase):
    def test_sync_release_failure_latches_cleanup_state(self):
        harness = _SyncHarness()
        mapping = {"id": "m1", "target_modifiers": "无", "target": "A"}

        released, failed = harness._release_detached_sync_mappings([
            ("src", "m1", mapping)
        ])

        self.assertEqual(released, [])
        self.assertEqual(failed, ["m1"])
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertEqual(harness.macro_state, MacroState.STOP_TIMEOUT)
        self.assertIn("同步映射释放失败(1)", harness.last_macro_release_failures)
        self.assertIn("src", harness.active_sync_by_source)

    def test_conditional_sync_release_failure_reports_activity(self):
        harness = _ConditionalSyncHarness()

        self.assertTrue(harness._release_invalid_conditional_holds())

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertIn("同步映射释放失败(1)", harness.last_macro_release_failures)
        self.assertIn("src", harness.active_sync_by_source)

    def test_wait_for_all_preserves_finished_task_release_failure(self):
        controller = MacroController(_EngineActive())
        controller.tasks["p1"] = _FinishedTask("p1", cleanup_failed=True)

        remaining = controller.wait_for_all(timeout=0.01)

        self.assertEqual(remaining, [])
        self.assertEqual(controller.last_release_failures, ["p1"])
        self.assertNotIn("p1", controller.tasks)

    def test_start_engine_refuses_output_shutdown_without_failure_list(self):
        harness = _RuntimeStartHarness()

        with patch("ui.runtime_lifecycle.QMessageBox.warning") as warning:
            result = harness._set_running_impl(True)

        self.assertFalse(result)
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertIn("清理", harness.engine_hint.text)
        self.assertTrue(warning.called)

    def test_menu_countdown_cleanup_block_does_not_clear_stop_timeout(self):
        harness = _MenuCountdownHarness()
        calls = {"count": 0}

        def immediate_timer(_ms, callback):
            calls["count"] += 1
            if calls["count"] >= 5:
                harness.output_shutdown_in_progress = True
                harness.last_macro_release_failures = ["Kanata 虚拟键"]
                harness.macro_state = MacroState.STOP_TIMEOUT
                harness.macro_status_detail = "释放失败"
            callback()

        with patch("ui.action_execution.QTimer.singleShot", side_effect=immediate_timer), \
             patch("ui.action_execution.QMessageBox.warning"):
            harness.test_selected_preset()

        self.assertEqual(harness.macro_state, MacroState.STOP_TIMEOUT)
        self.assertEqual(harness.macro_status_detail, "释放失败")
        self.assertTrue(harness.output_shutdown_in_progress)

    def test_stop_current_macro_closes_output_gate_until_poll(self):
        harness = _SingleStopHarness()

        with patch("ui.macro_controls.QTimer.singleShot") as timer:
            harness.stop_current_macro()

        self.assertEqual(harness.macro_controller.stopped, ["p1"])
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness._macro_stop_gate_restore)
        self.assertEqual(harness.macro_state, MacroState.STOPPING)
        self.assertTrue(timer.called)

    def test_shutdown_rereads_late_wait_for_all_release_failures(self):
        harness = _ShutdownLateHarness()
        issues = []

        self.assertTrue(harness._stop_macro_runtime_for_shutdown(issues))

        self.assertTrue(issues)
        self.assertEqual(issues[-1]["step"], "释放宏任务持有输入")
        self.assertTrue(issues[-1]["critical"])
        self.assertIn("p1", issues[-1]["message"])


if __name__ == "__main__":
    unittest.main()
