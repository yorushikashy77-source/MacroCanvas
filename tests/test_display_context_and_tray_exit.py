import threading
import unittest

from core.constants import ConfigState
from ui.input_runtime import InputRuntimeMixin
from ui.shutdown_coordinator import ShutdownCoordinatorMixin
from ui.system_tray import SystemTrayMixin


class _Signal:
    def __init__(self):
        self.items = []

    def emit(self, item):
        self.items.append(item)


class _Controller:
    def __init__(self):
        self.started = []

    @staticmethod
    def is_running(_task_id):
        return False

    def start(self, task):
        self.started.append(dict(task))
        return True


class _RuntimeHarness(InputRuntimeMixin):
    def __init__(self, geometry=(0, 0, 2048, 1152)):
        self.geometry = geometry
        self.input_state_lock = threading.RLock()
        self.held_trigger_ids = {}
        self.active_profile_id = "profile-a"
        self.macro_controller = _Controller()
        self.recorded_mouse_context_mismatch_signal = _Signal()
        self.diagnostics = []
        self.feedback = []

    @staticmethod
    def _macro_backend_active():
        return True

    @staticmethod
    def _runtime_cleanup_blocks_new_output():
        return False

    def _virtual_screen_geometry(self):
        return self.geometry

    def write_diagnostic(self, event, **fields):
        self.diagnostics.append((event, fields))

    def _play_feedback(self, kind):
        self.feedback.append(kind)


def _screen_move(geometry):
    return {
        "type": "鼠标移动",
        "target": "100,200",
        "recording_context": {
            "mode": "screen",
            "virtual_screen": list(geometry),
        },
    }


class RecordedDisplayContextTests(unittest.TestCase):
    def test_hotkey_start_is_blocked_before_any_output_when_screen_layout_changed(self):
        harness = _RuntimeHarness()
        task = {
            "id": "preset-a",
            "name": "布局敏感宏",
            "execution_mode": "执行一次",
            "actions": [
                {"type": "鼠标点击", "target": "鼠标左键"},
                _screen_move((0, 0, 5120, 1600)),
            ],
        }

        self.assertFalse(harness.handle_trigger_task(task, "F6", True, False))

        self.assertEqual(harness.macro_controller.started, [])
        self.assertEqual(len(harness.recorded_mouse_context_mismatch_signal.items), 1)
        issue = harness.recorded_mouse_context_mismatch_signal.items[0]
        self.assertEqual(issue["expected"], (0, 0, 5120, 1600))
        self.assertEqual(issue["current"], (0, 0, 2048, 1152))
        self.assertEqual(issue["preset_name"], "布局敏感宏")
        self.assertEqual(harness.feedback, ["error"])

    def test_hotkey_start_continues_when_recorded_screen_layout_matches(self):
        harness = _RuntimeHarness((0, 0, 5120, 1600))
        task = {
            "id": "preset-a",
            "name": "布局敏感宏",
            "execution_mode": "执行一次",
            "actions": [_screen_move((0, 0, 5120, 1600))],
        }

        self.assertTrue(harness.handle_trigger_task(task, "F6", True, False))

        self.assertEqual(len(harness.macro_controller.started), 1)
        self.assertEqual(harness.recorded_mouse_context_mismatch_signal.items, [])


class _Event:
    def __init__(self):
        self.accepted = False
        self.ignored = False

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


class _ShutdownHarness(ShutdownCoordinatorMixin):
    def __init__(self, state=ConfigState.APPLIED):
        self.config_state = state
        self.applied_config_signature = "same"
        self.recording_session_active = False
        self._tray_exit_requested = True
        self.notices = []
        self.shutdown_calls = 0

    @staticmethod
    def _shutdown_quarantined_mouse_names():
        return []

    def shutdown(self):
        self.shutdown_calls += 1
        return True

    def show_tray_exit_blocked_notice(self, reason):
        self.notices.append(reason)


class SilentTrayExitTests(unittest.TestCase):
    def test_silent_tray_exit_accepts_when_no_confirmation_is_needed(self):
        harness = _ShutdownHarness()
        event = _Event()

        harness._close_from_system_tray_silently(event)

        self.assertTrue(event.accepted)
        self.assertFalse(event.ignored)
        self.assertEqual(harness.shutdown_calls, 1)
        self.assertEqual(harness.notices, [])

    def test_silent_tray_exit_keeps_running_when_unsaved_changes_need_a_choice(self):
        harness = _ShutdownHarness(ConfigState.DIRTY)
        event = _Event()

        harness._close_from_system_tray_silently(event)

        self.assertFalse(event.accepted)
        self.assertTrue(event.ignored)
        self.assertEqual(harness.shutdown_calls, 0)
        self.assertFalse(harness._tray_exit_requested)
        self.assertIn("未应用的修改", harness.notices[0])


class _TrayRequestHarness(SystemTrayMixin):
    def __init__(self):
        self._shutdown_complete = False
        self._tray_exit_requested = False
        self.visible = False
        self.foreground_calls = 0
        self.close_calls = 0
        self.quit_calls = 0

    def isVisible(self):
        return self.visible

    def showNormal(self):
        self.foreground_calls += 1
        self.visible = True

    def raise_(self):
        self.foreground_calls += 1

    def activateWindow(self):
        self.foreground_calls += 1

    def close(self):
        self.close_calls += 1
        self._shutdown_complete = True

    def _quit_application_after_tray_exit(self):
        self.quit_calls += 1


class TrayExitRequestTests(unittest.TestCase):
    def test_tray_exit_does_not_foreground_a_hidden_main_window(self):
        harness = _TrayRequestHarness()

        harness.request_exit_from_system_tray()

        self.assertEqual(harness.close_calls, 1)
        self.assertEqual(harness.foreground_calls, 0)
        self.assertFalse(harness.visible)
        self.assertEqual(harness.quit_calls, 1)


if __name__ == "__main__":
    unittest.main()
