import threading
import unittest

from ui.hotkey_settings import HotkeySettingsMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin
from ui.shutdown_coordinator import ShutdownCoordinatorMixin


class _ToggleHarness(RuntimeLifecycleMixin):
    def __init__(self):
        self._shutdown_started = False
        self._runtime_operation_active = False
        self._config_apply_transaction_active = False
        self.loading_task_stack = []
        self.running = False
        self.calls = []
        self.diagnostics = []

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    def _set_running_impl(self, value, allow_owned_mouse_force_release=False):
        self.calls.append((value, allow_owned_mouse_force_release))
        self.running = bool(value)
        return True

    def _begin_loading(self, *_args, **_kwargs):
        self.loading_task_stack.append("runtime")

    def _end_loading(self):
        self.loading_task_stack.pop()


class _HotkeyHarness(HotkeySettingsMixin):
    def __init__(self):
        self.base_profile_payload = {
            "mappings": [],
            "presets": [],
        }
        self.profiles = []
        self.stored = 0

    def _store_editor_payload(self):
        self.stored += 1


class _WidgetStub:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _TimerStub:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _HintStub:
    def __init__(self):
        self.style = ""
        self.text = ""

    def setStyleSheet(self, value):
        self.style = value

    def setText(self, value):
        self.text = value


class _ShutdownFailureHarness(ShutdownCoordinatorMixin):
    def __init__(self):
        self._shutdown_complete = False
        self._shutdown_in_progress = False
        self._shutdown_started = False
        self._shutdown_errors = []
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.recording_session_active = False
        self.recording = False
        self.running = True
        self.direct_interception_active = True
        self.profile_timer = _TimerStub()
        self.auto_apply_timer = _TimerStub()
        self.central = _WidgetStub()
        self.menu = _WidgetStub()
        self.engine_hint = _HintStub()
        self.refresh_count = 0
        self.diagnostics = []

    def centralWidget(self):
        return self.central

    def menuBar(self):
        return self.menu

    def refresh_status_ui(self):
        self.refresh_count += 1

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    def _owned_output_names_snapshot(self, include_mouse=False):
        del include_mouse
        return []

    def _stop_macro_runtime_for_shutdown(self, issues):
        issues.append({
            "step": "等待宏线程退出",
            "message": "测试超时",
            "critical": True,
        })
        return False

    def _finalize_shutdown_diagnostics(self, issues, complete):
        self.diagnostics.append(("finalized", {"issues": issues, "complete": complete}))


class InteractionRiskFixes33Tests(unittest.TestCase):
    def test_public_runtime_commands_are_rejected_during_loading(self):
        harness = _ToggleHarness()
        harness.loading_task_stack.append("config")

        self.assertFalse(harness.set_running(True))
        self.assertEqual(harness.calls, [])
        self.assertFalse(harness.running)
        self.assertEqual(harness.diagnostics[-1][0], "runtime_control_rejected")

    def test_public_runtime_commands_are_rejected_until_outer_operation_returns(self):
        harness = _ToggleHarness()
        harness._runtime_operation_active = True

        self.assertFalse(harness.set_running(True))
        self.assertEqual(harness.calls, [])

    def test_public_runtime_commands_are_rejected_after_shutdown_starts(self):
        harness = _ToggleHarness()
        harness._shutdown_started = True

        self.assertFalse(harness.set_running(True))
        self.assertEqual(harness.calls, [])

    def test_global_hotkey_conflict_checks_all_enabled_profiles(self):
        harness = _HotkeyHarness()
        harness.profiles = [
            {
                "id": "game-a",
                "name": "游戏 A",
                "enabled": True,
                "payload": {
                    "mappings": [{
                        "enabled": True,
                        "name": "跳跃映射",
                        "source_modifiers": "Ctrl",
                        "source": "F6",
                    }],
                    "presets": [],
                },
            }
        ]

        conflict = harness.global_hotkey_conflict("Ctrl", "F6")

        self.assertEqual(conflict, "档案“游戏 A” · 跳跃映射")
        self.assertEqual(harness.stored, 1)

    def test_global_hotkey_conflict_ignores_disabled_profiles(self):
        harness = _HotkeyHarness()
        harness.profiles = [
            {
                "name": "已停用",
                "enabled": False,
                "payload": {
                    "mappings": [{
                        "enabled": True,
                        "name": "旧映射",
                        "source_modifiers": "Ctrl",
                        "source": "F6",
                    }],
                    "presets": [],
                },
            }
        ]

        self.assertEqual(harness.global_hotkey_conflict("Ctrl", "F6"), "")

    def test_shutdown_failure_locks_interactive_surface_for_retry_only(self):
        harness = _ShutdownFailureHarness()

        self.assertFalse(harness.shutdown())
        self.assertTrue(harness._shutdown_started)
        self.assertFalse(harness._shutdown_in_progress)
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertFalse(harness.central.enabled)
        self.assertFalse(harness.menu.enabled)
        self.assertTrue(harness.profile_timer.stopped)
        self.assertTrue(harness.auto_apply_timer.stopped)
        self.assertIn("主界面已锁定", harness.engine_hint.text)


if __name__ == "__main__":
    unittest.main()
