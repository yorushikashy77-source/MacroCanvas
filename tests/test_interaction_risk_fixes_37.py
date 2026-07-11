import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.constants import EngineState, MacroState
from ui.input_listener_lifecycle import InputListenerLifecycleMixin
from ui.macro_controls import MacroControlsMixin
from ui.profile_workflow import ProfileWorkflowMixin
from ui.shutdown_coordinator import ShutdownCoordinatorMixin


ROOT = Path(__file__).resolve().parents[1]


class _TextStub:
    def __init__(self):
        self.text = ""
        self.style = ""
        self.object_name = ""
        self.enabled = True

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)

    def setObjectName(self, name):
        self.object_name = str(name)

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _Task:
    def __init__(self, preset_id, name=None, live=False, cleanup_failed=False):
        self.preset = {"id": preset_id, "name": name or preset_id}
        self._live = live
        self.release_cleanup_failed = cleanup_failed
        self.run_event = threading.Event()
        self.run_event.set()
        self.stop_event = threading.Event()

    def has_live_threads(self):
        return self._live


class _Controller:
    def __init__(self, tasks):
        self.lock = threading.RLock()
        self.tasks = {task.preset["id"]: task for task in tasks}

    def finish(self, preset_id):
        with self.lock:
            task = self.tasks.get(preset_id)
            if task is not None and not task.has_live_threads():
                return self.tasks.pop(preset_id)
            return task


class _MacroHarness(MacroControlsMixin):
    def __init__(self, tasks):
        self.macro_controller = _Controller(tasks)
        self.active_macro_id = tasks[0].preset["id"] if tasks else None
        self.execution_info = _TextStub()
        self.engine_hint = _TextStub()
        self.pause_button = _TextStub()
        self.stop_current_button = _TextStub()
        self.last_action_activity = {}
        self.last_macro_release_failures = []
        self.output_shutdown_in_progress = False
        self._macro_stop_gate_restore = True
        self._deferred_profile_input_restore = {"layer": "old"}
        self.discard_reasons = []
        self.restore_called = False
        self.macro_state = MacroState.RUNNING
        self.macro_status_detail = ""
        self.running = True
        self.recording_session_active = False
        self.auto_apply_checkbox = None
        self.diagnostics = []

    def refresh_status_ui(self):
        pass

    def refresh_macro_controls(self):
        MacroControlsMixin.refresh_macro_controls(self)

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    def _apply_deferred_profile_input_restore(self):
        self.restore_called = True
        return True

    def _discard_profile_suspended_macros(self, reason=""):
        self.discard_reasons.append(reason)


class _ProfileClearHarness(ProfileWorkflowMixin):
    def __init__(self):
        self.last_macro_release_failures = []
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.input_state_lock = threading.RLock()
        self.physical_modifiers = set()
        self.physical_input_sources = {}
        self.held_trigger_ids = {}
        self.kanata_trigger_down = set()
        self.expected_kanata_event_lock = threading.RLock()
        self.expected_kanata_events = []
        self.system_hotkey_latched = set()
        self.system_hotkey_latched_sources = {}
        self.diagnostics = []

    def _runtime_mapping_rules(self):
        return []

    def _remember_runtime_release_state(self, _rules=None):
        return {"A"}, {"vk-a"}

    def _release_all_sync_mappings(self):
        return False

    def _release_runtime_virtual_keys(self, **_kwargs):
        return True

    def _failsafe_release_runtime_targets(self, **_kwargs):
        return False

    def _refresh_logical_physical_sets_locked(self):
        pass

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _ShutdownHarness(ShutdownCoordinatorMixin):
    def __init__(self):
        self._shutdown_complete = False
        self._shutdown_in_progress = False
        self._shutdown_started = False
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.recording_session_active = False
        self.recording = False
        self.held_inputs = set()
        self.diagnostic_lock = threading.RLock()
        self.engine_state = EngineState.RUNNING
        self.issues = []

    def _owned_output_names_snapshot(self, include_mouse=False):
        return ["A"]

    def _stop_macro_runtime_for_shutdown(self, issues):
        return True

    def _release_all_sync_mappings(self):
        return False

    def _release_interception_output(self):
        return True

    def _kanata_engine_has_runtime(self, _engine):
        return False

    def _stop_global_hook_for_shutdown(self, issues):
        return True

    def _stop_interception_hook_for_shutdown(self, issues):
        return True

    def _force_release_system_inputs(self, names=None):
        return True

    def held_input_snapshot(self):
        return []

    def write_diagnostic(self, event, **payload):
        if event == "shutdown_failed":
            self.issues = payload.get("issues", [])

    def _flush_diagnostic_queue(self, timeout=0):
        pass

    def _trim_diagnostic_log_locked(self):
        pass

    def centralWidget(self):
        return None

    def menuBar(self):
        return None

    def refresh_status_ui(self):
        pass

    class _Engine:
        def stop(self, timeout=0):
            return True

    engine = _Engine()
    keyboard_engine = _Engine()
    interception_output = None


class _Hook:
    def __init__(self, alive=False, stop_ok=False):
        self.alive = alive
        self.stop_ok = stop_ok
        self.last_stop_warning = "仍在退出"

    def is_alive(self):
        return self.alive

    def stop(self, timeout=0.5):
        return self.stop_ok


class _GlobalHookHarness(InputListenerLifecycleMixin):
    def __init__(self):
        self.global_hook = _Hook(alive=False, stop_ok=False)
        self.engine_hint = _TextStub()
        self.diagnostics = []
        self.created = 0

    def _clear_physical_input_state(self):
        pass

    def _global_hook_callback(self, *args):
        return False

    def _raw_recording_event(self, *args):
        pass

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class InteractionRiskFixes37Tests(unittest.TestCase):
    def test_natural_macro_release_failure_latches_output_gate(self):
        task = _Task("finished", "自然结束任务", cleanup_failed=True)
        harness = _MacroHarness([task])

        harness.on_macro_finished("finished")

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertEqual(harness.macro_state, MacroState.STOP_TIMEOUT)
        self.assertIn("自然结束任务", harness.last_macro_release_failures)
        self.assertIn("释放未完成", harness.engine_hint.text)

    def test_release_failure_is_preserved_when_other_macro_still_runs(self):
        failed = _Task("finished", "失败任务", cleanup_failed=True)
        running = _Task("running", "仍运行任务", live=True)
        harness = _MacroHarness([failed, running])

        harness.on_macro_finished("finished")

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertIn("失败任务", harness.last_macro_release_failures)
        self.assertIn("running", harness.macro_controller.tasks)
        self.assertEqual(harness.macro_state, MacroState.STOP_TIMEOUT)

    def test_stopping_poller_reads_stale_task_cleanup_failure_before_restore(self):
        stale = _Task("stale", "陈旧任务", live=False, cleanup_failed=True)
        harness = _MacroHarness([stale])

        with patch("ui.macro_controls.QTimer.singleShot"):
            harness._poll_stopping_macros()

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.restore_called)
        self.assertIsNone(harness._deferred_profile_input_restore)
        self.assertIn("macro_cleanup_failed", harness.discard_reasons)
        self.assertIn("陈旧任务", harness.last_macro_release_failures)

    def test_profile_transition_release_failure_latches_runtime_gate(self):
        harness = _ProfileClearHarness()

        self.assertFalse(harness._clear_profile_transition_state())

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertIn("同步映射输出", harness.last_macro_release_failures)
        self.assertIn("系统级兜底释放", harness.last_macro_release_failures)

    def test_shutdown_treats_release_failure_as_critical(self):
        harness = _ShutdownHarness()

        self.assertFalse(harness.shutdown())

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertTrue(any(item.get("critical") for item in harness.issues))
        self.assertTrue(any(item.get("step") == "释放同步映射" for item in harness.issues))

    def test_start_global_hook_keeps_old_reference_when_stop_times_out(self):
        harness = _GlobalHookHarness()
        old_hook = harness.global_hook

        self.assertFalse(harness.start_global_hook())

        self.assertIs(harness.global_hook, old_hook)
        self.assertIn("仍在退出", harness.engine_hint.text)
        self.assertTrue(any(event == "global_hook_start_blocked" for event, _ in harness.diagnostics))

    def test_game_mode_start_failure_marks_residual_interception_output(self):
        runtime_text = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        listener_text = (ROOT / "ui" / "input_listener_lifecycle.py").read_text("utf-8")
        self.assertIn("set_running_game_backend_start_residual", runtime_text)
        self.assertIn("Interception 输出上下文仍在退出", runtime_text)
        self.assertIn("self._interception_output_stop_failed = True", listener_text)


if __name__ == "__main__":
    unittest.main()
