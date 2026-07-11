import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.constants import (
    MAX_RECORDING_DURATION_MS,
    MAX_RECORDING_RAW_EVENTS,
    MacroState,
)
from ui.input_runtime import InputRuntimeMixin
from ui.preset_editor import PresetEditorMixin
from ui.profile_workflow import ProfileWorkflowMixin
from ui.recording_workflow import RecordingWorkflowMixin


ROOT = Path(__file__).resolve().parents[1]


class _TextStub:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, _style):
        pass


class _SignalStub:
    def __init__(self):
        self.count = 0

    def emit(self, *_args):
        self.count += 1


class _Task:
    def __init__(self, task_id, required_profile="game", running=True):
        self.preset = {
            "id": task_id,
            "name": task_id,
            "_required_profile_id": required_profile,
        }
        self.run_event = threading.Event()
        if running:
            self.run_event.set()
        self.stop_event = threading.Event()
        self.live = True
        self.pause_count = 0
        self.resume_count = 0
        self.stop_count = 0

    def has_live_threads(self):
        return self.live

    def pause(self):
        self.pause_count += 1
        self.run_event.clear()
        return True

    def resume(self):
        self.resume_count += 1
        self.run_event.set()
        return True

    def stop(self):
        self.stop_count += 1
        self.stop_event.set()
        return True


class _Controller:
    def __init__(self, tasks=()):
        self.lock = threading.RLock()
        self.tasks = {task.preset["id"]: task for task in tasks}
        self.stopped = []

    def stop(self, task_id):
        self.stopped.append(str(task_id))
        task = self.tasks.get(task_id)
        if task is not None:
            return task.stop()
        return False


class _ProcessGuardHarness(ProfileWorkflowMixin):
    def __init__(self, tasks=()):
        self._shutdown_started = False
        self.recording_guard_profile_id = None
        self.recording = False
        self._recording_guard_candidate = None
        self._recording_guard_candidate_since = 0.0
        self._recording_guard_candidate_hits = 0
        self._process_guard_warning_active = False
        self._process_guard_candidate = None
        self._process_guard_candidate_since = 0.0
        self._process_guard_candidate_hits = 0
        self._process_guard_input_suspended = False
        self._process_guard_suspended_profile_ids = set()
        self.foreground_profile_stable_seconds = 0.2
        self.foreground_profile_candidate = None
        self.foreground_profile_candidate_hits = 0
        self.foreground_candidate_input_suspended = False
        self.foreground = "game"
        self.macro_controller = _Controller(tasks)
        self.output_shutdown_in_progress = False
        self.output_dispatch_lock = threading.RLock()
        self.profile_trigger_allowed = True
        self._profile_input_paused_macro_ids = set()
        self.profile_input_temporarily_suspended = False
        self.profile_input_suspend_reason = ""
        self.active_profile_id = "game"
        self.active_profile_layer = "layer-game"
        self.mappings_enabled = True
        self.settings_input_mode_active = False
        self.recording_session_active = False
        self.running = True
        self._deferred_profile_input_restore = None
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        self.execution_info = _TextStub()
        self.engine_hint = _TextStub()
        self.layers = []
        self.cancel_count = 0

    def _foreground_profile_id(self):
        return self.foreground, "process.exe", "window"

    def _retry_quarantined_mouse_releases(self, force=False):
        return True

    def _change_runtime_profile_layer(self, layer, *, wait=True):
        self.layers.append((layer, wait))
        return True

    def _clear_profile_transition_state(self, release_outputs=True):
        pass

    def _profile_name(self, profile_id):
        return str(profile_id)

    def cancel_recording(self):
        self.cancel_count += 1
        self.recording = False

    def write_diagnostic(self, *_args, **_kwargs):
        pass

    def refresh_status_ui(self):
        pass

    def refresh_macro_controls(self):
        pass


class _DeleteHarness(PresetEditorMixin):
    def __init__(self, tasks):
        self.macro_controller = _Controller(tasks)
        self._test_countdown_preset_id = None
        self._test_countdown_generation = 0
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        self.input_state_lock = threading.RLock()
        self.held_trigger_ids = {
            "physical:A": {"preset-1", "debug:preset-1", "other"}
        }
        self.kanata_trigger_down = {"down:preset:preset1", "keep"}

    def refresh_status_ui(self):
        pass


class _RecordingStoreHarness(InputRuntimeMixin):
    def __init__(self):
        self.recording_lock = threading.RLock()
        self.recording = True
        self.recording_started_at = 100.0
        self.recording_options = {"record_move": False}
        self.recorded_events = []
        self.recording_pending_move = None
        self.recording_recent_events = {}
        self.recording_limit_reason = ""
        self.recording_limit_stop_requested = False
        self.recording_stop_signal = _SignalStub()
        self.last_recorded_move = 0.0


class _RecordingFinishHarness(InputRuntimeMixin, RecordingWorkflowMixin):
    def __init__(self):
        self.recording_restore_pending = False
        self.recording_session_active = True
        self.recording = True
        self.recording_lock = threading.RLock()
        self.recording_started_at = 1.0
        self.recording_finished_at = 0.0
        self.recording_generation = 1
        self.recording_limit_reason = "测试上限"
        self.recording_limit_stop_requested = True
        self.recorded_events = [
            {"kind": "key", "name": "A", "down": True, "time": 1.0},
            {"kind": "key", "name": "A", "down": False, "time": 1.1},
        ]
        self.recording_pending_move = None
        self.recording_recent_events = {("key", "A", True, None): 1.0}
        self.interception_input_hook = None
        self.recording_guard_profile_id = "game"
        self.recording_workflow_complete = False
        self.recording_move_origin = {}
        self.recording_target_card = None
        self.recording_insert_context = None
        self.input_state_lock = threading.RLock()
        self.physical_down = set()
        self.physical_modifiers = set()
        self.physical_input_sources = {}
        self.suppressed_trigger_names = set()
        self.interception_forwarded_down = set()
        self.global_toggle_latched = False
        self.global_toggle_latched_source = None
        self.preset_cards = []
        self.completed = False

    def _begin_loading(self, *_args, **_kwargs):
        pass

    def _end_loading(self):
        pass

    def convert_recording_to_actions(self, _events):
        return []

    def _mark_recording_workflow_complete(self):
        self.completed = True


class InteractionRiskFixes34Tests(unittest.TestCase):
    def test_process_guard_restores_after_transient_foreground_return(self):
        task = _Task("preset-1")
        harness = _ProcessGuardHarness([task])
        harness.foreground = "other"

        with patch("ui.profile_workflow.time.monotonic", return_value=1.0):
            harness.check_active_process_guards()

        self.assertTrue(harness._process_guard_input_suspended)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertEqual(task.pause_count, 1)

        harness.foreground = "game"
        harness.check_active_process_guards()

        self.assertFalse(harness._process_guard_input_suspended)
        self.assertTrue(harness.profile_trigger_allowed)
        self.assertEqual(task.resume_count, 1)
        self.assertEqual(harness.layers[-1][0], "layer-game")

    def test_process_guard_restores_even_after_stop_clears_pause_ledger(self):
        task = _Task("preset-1")
        harness = _ProcessGuardHarness([task])
        harness.foreground = "other"
        with patch("ui.profile_workflow.time.monotonic", return_value=1.0):
            harness.check_active_process_guards()

        harness.macro_controller.tasks.clear()
        harness._discard_profile_suspended_macros(reason="confirmed_stop")
        self.assertTrue(harness._process_guard_input_suspended)
        self.assertFalse(harness.profile_trigger_allowed)

        harness.check_active_process_guards()
        self.assertFalse(harness.profile_trigger_allowed)

        harness.foreground = "game"
        harness.check_active_process_guards()
        self.assertTrue(harness.profile_trigger_allowed)
        self.assertFalse(harness._process_guard_input_suspended)

    def test_recording_process_guard_requires_stable_mismatch(self):
        harness = _ProcessGuardHarness()
        harness.recording_guard_profile_id = "game"
        harness.recording = True
        harness.foreground = "other"

        with patch("ui.profile_workflow.time.monotonic", side_effect=[1.0, 1.1]):
            harness.check_active_process_guards()
            harness.check_active_process_guards()
        self.assertEqual(harness.cancel_count, 0)

        harness.foreground = "game"
        harness.check_active_process_guards()
        self.assertEqual(harness.cancel_count, 0)

        harness.foreground = "other"
        with patch("ui.profile_workflow.time.monotonic", side_effect=[2.0, 2.25]), patch(
            "ui.profile_workflow.QMessageBox.warning"
        ):
            harness.check_active_process_guards()
            harness.check_active_process_guards()
        self.assertEqual(harness.cancel_count, 1)

    def test_debug_task_has_stable_id_and_origin_metadata(self):
        source = (ROOT / "ui" / "action_execution.py").read_text("utf-8")
        self.assertIn('debug_task_id = f"debug:{origin_preset_id}"', source)
        self.assertIn('"id": debug_task_id', source)
        self.assertIn('"_origin_preset_id": origin_preset_id', source)
        self.assertNotIn("uuid.uuid4", source)

    def test_deleting_preset_stops_origin_and_derived_debug_tasks(self):
        original = _Task("preset-1", required_profile="")
        debug = _Task("debug:preset-1", required_profile="")
        debug.preset["_origin_preset_id"] = "preset-1"
        other = _Task("other", required_profile="")
        harness = _DeleteHarness([original, debug, other])

        harness._stop_preset_runtime_for_delete("preset-1")

        self.assertIn("preset-1", harness.macro_controller.stopped)
        self.assertIn("debug:preset-1", harness.macro_controller.stopped)
        self.assertNotIn("other", harness.macro_controller.stopped)
        self.assertEqual(harness.held_trigger_ids["physical:A"], {"other"})
        self.assertEqual(harness.kanata_trigger_down, {"keep"})

    def test_recording_raw_event_limit_stops_once_without_growth(self):
        harness = _RecordingStoreHarness()
        for index in range(MAX_RECORDING_RAW_EVENTS):
            harness._store_recorded_event({
                "kind": "key",
                "name": "A",
                "down": bool(index % 2),
                "time": 100.0 + index * 0.01,
            })

        self.assertEqual(len(harness.recorded_events), MAX_RECORDING_RAW_EVENTS)
        self.assertTrue(harness.recording_limit_stop_requested)
        self.assertEqual(harness.recording_stop_signal.count, 1)

        harness._store_recorded_event({
            "kind": "key", "name": "B", "down": True, "time": 999.0
        })
        self.assertEqual(len(harness.recorded_events), MAX_RECORDING_RAW_EVENTS)
        self.assertEqual(harness.recording_stop_signal.count, 1)

    def test_recording_duration_limit_rejects_late_event(self):
        harness = _RecordingStoreHarness()
        harness._store_recorded_event({
            "kind": "key",
            "name": "A",
            "down": True,
            "time": 100.0 + MAX_RECORDING_DURATION_MS / 1000,
        })

        self.assertEqual(harness.recorded_events, [])
        self.assertTrue(harness.recording_limit_stop_requested)
        self.assertIn("时长", harness.recording_limit_reason)
        self.assertEqual(harness.recording_stop_signal.count, 1)

    def test_finish_recording_releases_shared_raw_event_queue(self):
        harness = _RecordingFinishHarness()
        with patch("ui.recording_workflow.QMessageBox.information"):
            harness.finish_recording()

        self.assertFalse(harness.recording)
        self.assertEqual(harness.recorded_events, [])
        self.assertEqual(harness.recording_recent_events, {})
        self.assertTrue(harness.completed)


if __name__ == "__main__":
    unittest.main()
