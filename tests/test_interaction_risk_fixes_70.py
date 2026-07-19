import threading
import unittest

from core.constants import MacroState
from ui.input_listener_lifecycle import InputListenerLifecycleMixin
from ui.macro_controls import MacroControlsMixin
from ui.main_window import MainWindow
from ui.recording_workflow import RecordingWorkflowMixin
from ui.system_tray import SystemTrayMixin


class _Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}

    def finish(self, preset_id):
        return self.tasks.pop(preset_id, None)


class _MacroShutdownHarness(MacroControlsMixin):
    def __init__(self):
        self._shutdown_started = True
        self.output_shutdown_in_progress = True
        self._macro_stop_gate_restore = False
        self._deferred_profile_input_restore = {"layer": "old"}
        self.macro_controller = _Controller()
        self.macro_state = MacroState.STOPPING


class _RecordingShutdownHarness(RecordingWorkflowMixin):
    def __init__(self):
        self._shutdown_started = True
        self.recording_restore_pending = True
        self.recording_workflow_complete = True
        self.recording_session_active = True
        self.recording_restore_layer = "old"
        self.listener_updates = 0

    def update_global_hook_for_backend(self):
        self.listener_updates += 1
        return True


class _ListenerShutdownHarness(InputListenerLifecycleMixin):
    def __init__(self):
        self._shutdown_started = True
        self.output_backend_retired = False
        self.global_hook = None
        self.interception_input_hook = None
        self.interception_output = None

    def _runtime_is_game_mode(self):
        return False


class _Combo:
    def currentText(self):
        return "游戏模式（Interception）"


class _StartupRetryHarness:
    def __init__(self):
        self._shutdown_started = True
        self.output_backend_retired = False
        self.backend_combo = _Combo()
        self.running = False
        self.runtime_global_toggle_enabled = True
        self._startup_listener_retry_count = 0
        self.listener_updates = 0

    def update_global_hook_for_backend(self):
        self.listener_updates += 1
        return True


class _TrayHarness(SystemTrayMixin):
    def __init__(self):
        self.visible = True
        self.recording_session_active = True
        self.macro_state = MacroState.RECORDING
        self._test_countdown_preset_id = None
        self.hidden = 0
        self.raised = 0
        self.activated = 0

    def isVisible(self):
        return self.visible

    def hide(self):
        self.hidden += 1
        self.visible = False

    def showNormal(self):
        self.visible = True

    def raise_(self):
        self.raised += 1

    def activateWindow(self):
        self.activated += 1


class InteractionRiskFixes70Tests(unittest.TestCase):
    def test_delayed_macro_poller_cannot_reopen_output_during_shutdown(self):
        harness = _MacroShutdownHarness()

        harness._poll_stopping_macros()

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertIsNone(harness._macro_stop_gate_restore)
        self.assertIsNone(harness._deferred_profile_input_restore)
        self.assertEqual(harness.macro_state, MacroState.STOPPING)

    def test_macro_finished_signal_only_retires_task_during_shutdown(self):
        harness = _MacroShutdownHarness()

        harness.on_macro_finished("finished")

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertIsNone(harness._macro_stop_gate_restore)
        self.assertEqual(harness.macro_state, MacroState.STOPPING)

    def test_recording_restore_cannot_restart_listener_during_shutdown(self):
        harness = _RecordingShutdownHarness()

        self.assertFalse(harness._complete_recording_restore_if_ready())

        self.assertEqual(harness.listener_updates, 0)
        self.assertFalse(harness.recording_restore_pending)
        self.assertFalse(harness.recording_workflow_complete)
        self.assertFalse(harness.recording_session_active)
        self.assertIsNone(harness.recording_restore_layer)

    def test_listener_entry_points_reject_start_after_shutdown(self):
        harness = _ListenerShutdownHarness()

        self.assertFalse(harness.start_global_hook())
        self.assertFalse(harness.restart_global_hook())
        self.assertFalse(harness.update_global_hook_for_backend())

    def test_startup_retry_does_not_update_listener_after_shutdown(self):
        harness = _StartupRetryHarness()

        self.assertFalse(MainWindow.initialize_startup_input_listener(harness))
        self.assertEqual(harness.listener_updates, 0)

    def test_tray_cannot_hide_visible_recording_window(self):
        harness = _TrayHarness()

        self.assertFalse(harness.show_from_system_tray())

        self.assertTrue(harness.visible)
        self.assertEqual(harness.hidden, 0)
        self.assertEqual(harness.raised, 1)
        self.assertEqual(harness.activated, 1)

    def test_tray_can_hide_window_again_after_recording(self):
        harness = _TrayHarness()
        harness.recording_session_active = False
        harness.macro_state = MacroState.IDLE

        self.assertTrue(harness.show_from_system_tray())

        self.assertFalse(harness.visible)
        self.assertEqual(harness.hidden, 1)


if __name__ == "__main__":
    unittest.main()
