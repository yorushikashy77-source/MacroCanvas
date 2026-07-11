import inspect
import threading
import time
import unittest

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal

from ui.mapping_editor import MappingEditorMixin
from ui.profile_workflow import ProfileWorkflowMixin
from ui.recording_workflow import RecordingWorkflowMixin
from ui.shutdown_coordinator import ShutdownCoordinatorMixin


class _EngineStub:
    def __init__(self, error=""):
        self.last_command_error = error


class _HintStub:
    def __init__(self):
        self.style = ""
        self.text = ""

    def setStyleSheet(self, value):
        self.style = value

    def setText(self, value):
        self.text = value


class _MacroControllerStub:
    def __init__(self):
        self.stopped = []

    def stop(self, task_id):
        self.stopped.append(task_id)
        return True


class _RecordingRestoreProbe(QObject, RecordingWorkflowMixin):
    restore_signal = Signal()

    def __init__(self):
        super().__init__()
        self.recording_restore_signal = self.restore_signal
        self.callback_thread = None
        self.callback_event = threading.Event()
        self.recording_restore_signal.connect(
            self._complete_recording_restore_if_ready,
            Qt.ConnectionType.QueuedConnection,
        )

    def _complete_recording_restore_if_ready(self):
        self.callback_thread = threading.get_ident()
        self.callback_event.set()
        return True


class _SettingsHarness(ProfileWorkflowMixin):
    def __init__(self, stop_succeeds):
        self.settings_dialog_active = False
        self.settings_input_mode_active = False
        self.running = True
        self.stop_succeeds = stop_succeeds
        self.engine = _EngineStub("layer failed")
        self.keyboard_engine = _EngineStub()
        self.diagnostics = []

    def _suspend_active_profile_input(self, **_kwargs):
        return False

    def _set_running_impl(self, value, allow_owned_mouse_force_release=False):
        del value, allow_owned_mouse_force_release
        if self.stop_succeeds:
            self.running = False
            return None
        return False

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _MappingDeleteHarness(MappingEditorMixin):
    def __init__(self, stop_succeeds):
        self.running = True
        self.stop_succeeds = stop_succeeds
        self.restart_engine_after_apply = False
        self.suspended_mapping_ids = set()
        self.runtime_mappings = [{"id": "map-1", "name": "A"}]
        self.runtime_trigger_rules = [
            {"id": "map-1", "_runtime_kind": "mapping"}
        ]
        self.data_lock = threading.RLock()
        self.input_state_lock = threading.RLock()
        self.held_trigger_ids = {"source": {"mapping:map-1"}}
        self.active_sync_by_source = {}
        self.kanata_trigger_down = {"x:mapping:map1"}
        self.macro_controller = _MacroControllerStub()
        self.engine = _EngineStub("stop failed")
        self.keyboard_engine = _EngineStub()
        self.engine_hint = _HintStub()
        self.diagnostics = []

    def _visible_editor_profile_id(self):
        return ""

    def _runtime_is_game_mode(self):
        return False

    def set_running(self, value, allow_owned_mouse_force_release=False):
        del value, allow_owned_mouse_force_release
        if self.stop_succeeds:
            self.running = False
            return None
        return False

    def _release_sync_mapping(self, _held):
        return True

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _ShutdownHarness(ShutdownCoordinatorMixin):
    def __init__(self):
        self._shutdown_complete = False
        self._shutdown_in_progress = False
        self._shutdown_started = False
        self._shutdown_errors = []
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.recording_session_active = True
        self.recording = True
        self.running = True
        self.direct_interception_active = True
        self.observed_backend_flags = None

    def _owned_output_names_snapshot(self, include_mouse=False):
        del include_mouse
        return []

    def _stop_macro_runtime_for_shutdown(self, issues):
        del issues
        self.observed_backend_flags = (
            self.running,
            self.direct_interception_active,
        )
        return False

    def _finalize_shutdown_diagnostics(self, issues, complete):
        del issues, complete


class InteractionRiskFixes32Tests(unittest.TestCase):
    def test_recording_restore_signal_runs_on_gui_thread(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        probe = _RecordingRestoreProbe()
        gui_thread_id = threading.get_ident()

        worker = threading.Thread(target=probe._request_recording_restore_check)
        worker.start()
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())

        deadline = time.monotonic() + 1.0
        while not probe.callback_event.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)

        self.assertTrue(probe.callback_event.is_set())
        self.assertEqual(probe.callback_thread, gui_thread_id)

    def test_settings_dialog_mode_rolls_back_when_isolation_and_stop_fail(self):
        harness = _SettingsHarness(stop_succeeds=False)
        self.assertFalse(harness._enter_settings_input_mode())
        self.assertFalse(harness.settings_dialog_active)
        self.assertFalse(harness.settings_input_mode_active)
        self.assertTrue(harness.running)
        self.assertEqual(harness.diagnostics[0][0], "settings_dialog_open_aborted")

    def test_settings_dialog_mode_accepts_successful_stop_fallback(self):
        harness = _SettingsHarness(stop_succeeds=True)
        self.assertTrue(harness._enter_settings_input_mode())
        self.assertTrue(harness.settings_dialog_active)
        self.assertTrue(harness.settings_input_mode_active)
        self.assertFalse(harness.running)

    def test_direct_mapping_delete_does_not_mutate_state_when_stop_fails(self):
        harness = _MappingDeleteHarness(stop_succeeds=False)
        mapping = {
            "id": "map-1",
            "source": "鼠标左键",
            "mode": "同步按住",
        }
        snapshot = harness._suspend_mapping_runtime_for_delete(mapping)

        self.assertIsNone(snapshot)
        self.assertEqual(harness.runtime_mappings, [{"id": "map-1", "name": "A"}])
        self.assertEqual(
            harness.runtime_trigger_rules,
            [{"id": "map-1", "_runtime_kind": "mapping"}],
        )
        self.assertNotIn("map-1", harness.suspended_mapping_ids)
        self.assertFalse(harness.restart_engine_after_apply)
        self.assertEqual(harness.macro_controller.stopped, [])

    def test_direct_mapping_delete_mutates_state_only_after_successful_stop(self):
        harness = _MappingDeleteHarness(stop_succeeds=True)
        mapping = {
            "id": "map-1",
            "source": "鼠标左键",
            "mode": "同步按住",
        }
        snapshot = harness._suspend_mapping_runtime_for_delete(mapping)

        self.assertIsNotNone(snapshot)
        self.assertFalse(harness.running)
        self.assertTrue(harness.restart_engine_after_apply)
        self.assertEqual(harness.runtime_mappings, [])
        self.assertEqual(harness.runtime_trigger_rules, [])
        self.assertIn("map-1", harness.suspended_mapping_ids)
        self.assertEqual(harness.macro_controller.stopped, ["mapping:map-1"])

    def test_mapping_card_delete_returns_when_runtime_isolation_fails(self):
        source = inspect.getsource(MappingEditorMixin.delete_mapping)
        isolation = source.index(
            "snapshot = self._suspend_mapping_runtime_for_delete(mapping)"
        )
        abort = source.index("if snapshot is None:", isolation)
        widget_remove = source.index("self.mapping_cards.remove(card)", abort)
        self.assertLess(isolation, abort)
        self.assertLess(abort, widget_remove)

    def test_shutdown_keeps_backend_ownership_until_macro_workers_exit(self):
        harness = _ShutdownHarness()
        self.assertFalse(harness.shutdown())
        self.assertEqual(harness.observed_backend_flags, (True, True))
        self.assertTrue(harness.running)
        self.assertTrue(harness.direct_interception_active)


if __name__ == "__main__":
    unittest.main()
