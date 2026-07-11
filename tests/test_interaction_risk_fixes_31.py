import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from config.schema import repair_duplicate_runtime_ids, validate_config_payload
from core.constants import MacroState
from engine.interception import InterceptionInputHook
from macro.scheduler import MacroTask
from ui.input_listener_lifecycle import InputListenerLifecycleMixin
from ui.input_runtime import InputRuntimeMixin
from ui.macro_controls import MacroControlsMixin


class _CaptureSignal:
    def __init__(self):
        self.values = []

    def emit(self, *args, **kwargs):
        self.values.append((args, kwargs))


class _Signals:
    def __init__(self):
        self.progress = _CaptureSignal()
        self.action_activity = _CaptureSignal()
        self.task_finished = _CaptureSignal()
        self.state_changed = _CaptureSignal()


class _Engine:
    def is_running(self):
        return True


class _InputStateHarness(InputRuntimeMixin):
    def __init__(self):
        self.input_state_lock = threading.RLock()
        self.physical_input_sources = {}
        self.physical_down = set()
        self.physical_modifiers = set()
        self.interception_control_sources = {}
        self.interception_control_modifiers = set()
        self.system_hotkey_latched = set()
        self.system_hotkey_latched_sources = {}
        self.global_toggle_latched = False
        self.global_toggle_latched_source = None
        self.interception_input_hook = None
        self.global_hook = None
        self.diagnostics = []

    def write_diagnostic(self, event, **fields):
        self.diagnostics.append((event, fields))


class _SnapshotListener:
    def __init__(self, snapshot):
        self.snapshot = list(snapshot)

    def is_alive(self):
        return True

    def pressed_input_snapshot(self):
        return list(self.snapshot)


class PhysicalSourceStateTests(unittest.TestCase):
    def test_one_modifier_source_up_does_not_clear_another_source(self):
        harness = _InputStateHarness()
        with harness.input_state_lock:
            harness._update_physical_input_state_locked(
                "Ctrl", True, "kbd-1-left-ctrl"
            )
            harness._update_physical_input_state_locked(
                "Ctrl", True, "kbd-2-right-ctrl"
            )
            harness._update_physical_input_state_locked(
                "Ctrl", False, "kbd-1-left-ctrl"
            )
        self.assertEqual(harness.physical_down, {"Ctrl"})
        self.assertEqual(harness.physical_modifiers, {"Ctrl"})
        self.assertEqual(
            harness.physical_input_sources,
            {"kbd-2-right-ctrl": "Ctrl"},
        )

        with harness.input_state_lock:
            harness._update_physical_input_state_locked(
                "Ctrl", False, "kbd-2-right-ctrl"
            )
        self.assertEqual(harness.physical_down, set())
        self.assertEqual(harness.physical_modifiers, set())

    def test_system_hotkey_release_must_come_from_latched_source(self):
        harness = _InputStateHarness()
        harness._latch_system_hotkey("emergency", "keyboard-1-f8")
        self.assertFalse(
            harness._unlatch_system_hotkey("emergency", "keyboard-2-f8")
        )
        self.assertIn("emergency", harness.system_hotkey_latched)
        self.assertTrue(
            harness._unlatch_system_hotkey("emergency", "keyboard-1-f8")
        )
        self.assertNotIn("emergency", harness.system_hotkey_latched)

    def test_reseed_restores_keys_still_held_during_listener_switch(self):
        harness = _InputStateHarness()
        harness.global_hook = _SnapshotListener([
            ("Ctrl", "win:kbd:A2"),
            ("A", "win:kbd:41"),
        ])
        harness.physical_input_sources = {"stale": "Shift"}
        harness.physical_down = {"Shift"}
        harness.physical_modifiers = {"Shift"}

        self.assertTrue(harness._reseed_physical_input_state())
        self.assertEqual(harness.physical_down, {"Ctrl", "A"})
        self.assertEqual(harness.physical_modifiers, {"Ctrl"})
        self.assertEqual(
            harness.physical_input_sources,
            {"win:kbd:A2": "Ctrl", "win:kbd:41": "A"},
        )

    def test_reseeded_toggle_repeat_does_not_retrigger_and_release_owner_survives(self):
        harness = _InputStateHarness()
        harness.runtime_global_toggle_enabled = True
        harness.runtime_global_toggle_key = "F10"
        harness.runtime_global_toggle_modifiers = "无"
        harness.global_toggle_signal = _CaptureSignal()
        source = "interception:kbd:1:44:0"

        # A key already held while the listener is switched is reseeded. Its
        # following auto-repeat Down must not become a fresh engine toggle.
        harness.physical_input_sources[source] = "F10"
        harness.physical_down = {"F10"}
        self.assertFalse(
            harness._handle_interception_control_event("F10", True, source)
        )
        self.assertEqual(harness.global_toggle_signal.values, [])
        harness._handle_interception_control_event("F10", False, source)

        # A fresh edge toggles once. The latch owner must remain valid across a
        # listener/runtime transition until the matching physical Up arrives.
        self.assertTrue(
            harness._handle_interception_control_event("F10", True, source)
        )
        self.assertEqual(len(harness.global_toggle_signal.values), 1)
        self.assertEqual(harness.global_toggle_latched_source, source)
        harness._clear_physical_input_state()
        harness.physical_input_sources[source] = "F10"
        harness.physical_down = {"F10"}
        self.assertTrue(
            harness._handle_interception_control_event("F10", True, source)
        )
        self.assertEqual(len(harness.global_toggle_signal.values), 1)
        self.assertTrue(
            harness._handle_interception_control_event("F10", False, source)
        )
        self.assertFalse(harness.global_toggle_latched)
        self.assertIsNone(harness.global_toggle_latched_source)

    def test_interception_snapshot_tracks_device_aware_sources(self):
        hook = InterceptionInputHook.__new__(InterceptionInputHook)
        hook.callback = lambda _name, _down: False
        hook.source_callback = lambda _name, _down, _source: False
        hook.pressed_sources_lock = threading.RLock()
        hook.pressed_sources = {}

        hook._dispatch_source("A", True, "interception:kbd:1:1E:0")
        hook._dispatch_source("A", True, "interception:kbd:2:1E:0")
        hook._dispatch_source("A", False, "interception:kbd:1:1E:0")

        self.assertEqual(
            hook.pressed_input_snapshot(),
            [("A", "interception:kbd:2:1E:0")],
        )


class RuntimeIdTests(unittest.TestCase):
    @staticmethod
    def _duplicate_mapping_payload():
        return {
            "mappings": [{"id": "duplicate"}],
            "presets": [],
            "profiles": [{
                "id": "profile-1",
                "name": "档案一",
                "process_names": ["game.exe"],
                "payload": {
                    "mappings": [{"id": "duplicate"}],
                    "presets": [],
                },
            }],
        }

    def test_duplicate_mapping_id_across_profiles_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "跨档案唯一"):
            validate_config_payload(self._duplicate_mapping_payload())

    def test_duplicate_mapping_id_is_repaired_without_mutating_source(self):
        source = self._duplicate_mapping_payload()
        repaired, changes = repair_duplicate_runtime_ids(source)
        self.assertEqual(source["profiles"][0]["payload"]["mappings"][0]["id"], "duplicate")
        self.assertEqual(len(changes), 1)
        self.assertNotEqual(
            repaired["mappings"][0]["id"],
            repaired["profiles"][0]["payload"]["mappings"][0]["id"],
        )
        self.assertIs(validate_config_payload(repaired), repaired)

    def test_mapping_and_preset_may_keep_same_raw_id(self):
        payload = {
            "mappings": [{"id": "same"}],
            "presets": [{"id": "same", "actions": []}],
        }
        self.assertIs(validate_config_payload(payload), payload)
        repaired, changes = repair_duplicate_runtime_ids(payload)
        self.assertEqual(changes, [])
        self.assertEqual(repaired, payload)


class ParallelExceptionTests(unittest.TestCase):
    def test_parallel_exception_is_reported_and_recorded(self):
        signals = _Signals()
        task = MacroTask(
            {"id": "p1", "name": "并行测试", "actions": []},
            _Engine(), signals, send_output=lambda *_args, **_kwargs: True,
            is_active=lambda: True,
        )
        acquired = 0
        while task.parallel_slots.acquire(blocking=False):
            acquired += 1
        results = []
        try:
            def fail():
                raise RuntimeError("parallel boom")

            task._launch_parallel(
                fail, "MacroCanvas-TestWorker", [], results, threading.RLock()
            )
        finally:
            for _ in range(acquired):
                task.parallel_slots.release()

        self.assertEqual(results, [False])
        self.assertEqual(task.parallel_errors[-1]["type"], "RuntimeError")
        info = signals.action_activity.values[-1][0][0]
        self.assertEqual(info["phase"], "error")
        self.assertEqual(info["error_type"], "parallel_exception")
        self.assertIn("parallel boom", info["action"])


class _TextWidget:
    def __init__(self):
        self.text = ""
        self.style = ""

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)


class _CallableTextWidget:
    def __init__(self):
        self.value = ""
        self.style = ""

    def text(self):
        return self.value

    def setText(self, text):
        self.value = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)


class _FinishedController:
    def __init__(self, task):
        self.task = task
        self.tasks = {}
        self.lock = threading.RLock()

    def finish(self, _preset_id):
        return self.task


class _MacroFinishHarness(MacroControlsMixin):
    def __init__(self):
        failed_task = SimpleNamespace(
            release_cleanup_failed=True,
            preset={"name": "自然结束测试"},
        )
        self.macro_controller = _FinishedController(failed_task)
        self.active_macro_id = "p1"
        self.macro_state = MacroState.RUNNING
        self.macro_status_detail = ""
        self.last_action_activity = {}
        self.execution_info = _TextWidget()
        self.engine_hint = _TextWidget()
        self.running = True
        self.recording_session_active = False
        self.auto_apply_checkbox = None
        self.status_refreshes = 0
        self.control_refreshes = 0

    def refresh_status_ui(self):
        self.status_refreshes += 1

    def refresh_macro_controls(self):
        self.control_refreshes += 1


class NaturalCleanupFeedbackTests(unittest.TestCase):
    def test_natural_finish_keeps_release_failure_visible(self):
        harness = _MacroFinishHarness()
        harness.on_macro_finished("p1")
        self.assertEqual(harness.macro_state, MacroState.STOP_TIMEOUT)
        self.assertIn("最终按键释放未完成", harness.macro_status_detail)
        self.assertIn("自然结束测试", harness.engine_hint.text)
        self.assertIn("再次急停", harness.execution_info.text)


class _DeadEngine:
    command_thread = None
    command_stop = None

    def is_running(self):
        return False


class _HealthHarness(InputListenerLifecycleMixin):
    def __init__(self, *, running):
        self.initializing = False
        self._shutdown_in_progress = False
        self._shutdown_started = False
        self._backend_failure_handling = False
        self._config_apply_transaction_active = False
        self.loading_task_stack = []
        self.running = bool(running)
        self.engine = _DeadEngine()
        self.global_hook = None
        self.interception_input_hook = None
        self.interception_output = None
        self.runtime_global_toggle_enabled = True
        self.failures = []
        self.restarted = 0
        self.reseeded = 0
        self.degraded = []

    def _runtime_is_game_mode(self):
        return False

    def _handle_runtime_backend_failure(self, reason):
        self.failures.append(str(reason))

    def restart_global_hook(self):
        self.restarted += 1
        return True

    def _reseed_physical_input_state(self, seed_control=False):
        self.reseeded += 1
        return True

    def _set_listener_degraded(self, reason=""):
        self.degraded.append(str(reason))


class BackendHealthTests(unittest.TestCase):
    def test_dead_running_kanata_is_detected(self):
        harness = _HealthHarness(running=True)
        with mock.patch("ui.input_listener_lifecycle.os.name", "nt"):
            harness.check_input_backend_health()
        self.assertEqual(
            harness.failures,
            ["Kanata 进程或命令线程已意外退出"],
        )

    def test_stopped_runtime_rebuilds_dead_global_hook(self):
        harness = _HealthHarness(running=False)
        with mock.patch("ui.input_listener_lifecycle.os.name", "nt"):
            harness.check_input_backend_health()
        self.assertEqual(harness.failures, [])
        self.assertEqual(harness.restarted, 1)
        self.assertEqual(harness.reseeded, 1)
        self.assertEqual(harness.degraded[-1], "")


class ListenerWarningFeedbackTests(unittest.TestCase):
    def test_recovery_clears_only_the_stale_listener_warning(self):
        harness = SimpleNamespace(
            input_listener_degraded_reason="监听异常",
            engine_hint=_CallableTextWidget(),
            running=False,
            refresh_status_ui=lambda: None,
        )
        harness.engine_hint.setText("监听异常")
        harness.engine_hint.setStyleSheet("color: #fbbf24;")
        InputListenerLifecycleMixin._set_listener_degraded(harness, "")
        self.assertEqual(harness.engine_hint.value, "全局输入监听已恢复")
        self.assertEqual(harness.engine_hint.style, "")

    def test_recovery_does_not_erase_a_newer_runtime_message(self):
        harness = SimpleNamespace(
            input_listener_degraded_reason="监听异常",
            engine_hint=_CallableTextWidget(),
            running=True,
            refresh_status_ui=lambda: None,
        )
        harness.engine_hint.setText("宏最终按键释放未完成")
        harness.engine_hint.setStyleSheet("color: #ff8496;")
        InputListenerLifecycleMixin._set_listener_degraded(harness, "")
        self.assertEqual(harness.engine_hint.value, "宏最终按键释放未完成")
        self.assertEqual(harness.engine_hint.style, "color: #ff8496;")


if __name__ == "__main__":
    unittest.main()
