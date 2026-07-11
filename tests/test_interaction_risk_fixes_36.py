import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from core.constants import EngineState, MacroState
from config.profiles import DISABLED_LAYER_NAME
from ui.action_execution import ActionExecutionMixin
from ui.editors import HotkeyEdit
from ui.input_listener_lifecycle import InputListenerLifecycleMixin
from ui.input_runtime import InputRuntimeMixin
from ui.profile_workflow import ProfileWorkflowMixin


ROOT = Path(__file__).resolve().parents[1]


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


class _EngineStub:
    def __init__(self, running=False, stop_ok=True):
        self.running = running
        self.stop_ok = stop_ok
        self.last_command_error = ""

    def is_running(self):
        return self.running

    def stop(self, timeout=0):
        del timeout
        return self.stop_ok

    def change_layer(self, _layer, wait=True, timeout=0):
        del wait, timeout
        return True


class _ProfileSwitchHarness(ProfileWorkflowMixin):
    def __init__(self):
        self.profile_switch_in_progress = False
        self.active_profile_id = "old"
        self.active_profile_layer = "old-layer"
        self.profile_trigger_allowed = True
        self.output_shutdown_in_progress = False
        self.running = True
        self.settings_input_mode_active = False
        self.recording_session_active = False
        self.mappings_enabled = True
        self.last_macro_release_failures = []
        self.engine_hint = _TextStub()
        self.engine = _EngineStub(running=True)
        self.keyboard_engine = _EngineStub()
        self.output_dispatch_lock = threading.RLock()
        self.installed = []
        self.layers = []
        self.failures = []
        self.diagnostics = []
        self._macro_stop_gate_restore = None
        self._deferred_profile_input_restore = None
        self.remaining_tasks = []

    def _runtime_profile_entry(self, profile_id):
        if profile_id == "new":
            return {"id": "new", "layer": "new-layer"}
        if profile_id == "old":
            return {"id": "old", "layer": "old-layer"}
        return None

    def _runtime_is_game_mode(self):
        return False

    def _change_runtime_profile_layer(self, layer, wait=True):
        self.layers.append((layer, wait))
        return True

    def stop_all_macros(self, **_kwargs):
        self.last_macro_release_failures = ["Kanata 虚拟键"]
        return list(self.remaining_tasks)

    def _clear_profile_transition_state(self, release_outputs=True):
        del release_outputs

    def _install_runtime_profile_entry(self, entry):
        self.installed.append(entry)
        return True

    def _show_macro_cleanup_failure(self, title, failures):
        self.failures.append((title, list(failures)))

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    def refresh_status_ui(self):
        pass

    def refresh_profile_selector_state(self):
        pass

    def _profile_name(self, profile_id):
        return profile_id


class _BackendFailureHarness(InputListenerLifecycleMixin):
    def __init__(self):
        self._backend_failure_handling = False
        self.profile_trigger_allowed = True
        self.output_shutdown_in_progress = False
        self.last_macro_release_failures = []
        self.interception_output = _OutputStub(stop_ok=False)
        self.engine = _EngineStub(running=True, stop_ok=False)
        self.keyboard_engine = _EngineStub(running=True, stop_ok=False)
        self.running = True
        self.direct_interception_active = True
        self.mappings_enabled = True
        self.engine_state = EngineState.RUNNING
        self.toggle_button = _TextStub()
        self.runtime_global_toggle_enabled = True
        self.interception_input_hook = None
        self.degraded = ""
        self.diagnostics = []

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))

    def stop_all_macros(self, **_kwargs):
        return []

    def _release_all_sync_mappings(self):
        return True

    def _release_interception_output(self):
        return True

    def _failsafe_release_runtime_targets(self, **_kwargs):
        return False

    def _runtime_is_game_mode(self):
        return False

    def _kanata_engine_has_runtime(self, engine):
        return engine.running

    def restart_global_hook(self):
        return True

    def _reseed_physical_input_state(self, **_kwargs):
        pass

    def _set_listener_degraded(self, detail):
        self.degraded = str(detail)

    def refresh_macro_controls(self):
        pass


class _OutputStub:
    def __init__(self, stop_ok=True):
        self.stop_ok = stop_ok

    def stop(self):
        return self.stop_ok


class _EmergencyHarness(InputRuntimeMixin):
    def __init__(self, layer_ok=True):
        self.output_shutdown_in_progress = False
        self.last_macro_release_failures = []
        self.mappings_enabled = True
        self.engine = _EngineStub(running=True)
        self.layer_ok = layer_ok
        self.gate_observations = []
        self._macro_stop_gate_restore = None

    def _play_feedback(self, _name):
        pass

    def stop_all_macros(self, **kwargs):
        self.gate_observations.append(("stop", self.output_shutdown_in_progress, kwargs))
        return []

    def _release_interception_output(self):
        self.gate_observations.append(("interception", self.output_shutdown_in_progress, {}))
        return True

    def _force_release_system_inputs(self):
        self.gate_observations.append(("system", self.output_shutdown_in_progress, {}))
        return True

    def _runtime_is_game_mode(self):
        return False

    def _show_macro_cleanup_failure(self, *_args, **_kwargs):
        pass

    def refresh_status_ui(self):
        pass


class _MenuTestHarness(ActionExecutionMixin):
    def __init__(self):
        self.active_profile_id = "profile-a"

    def _preset_as_mapping_rule(self, preset):
        return dict(preset)

    def mapping_to_task(self, rule):
        return {
            "id": rule["id"],
            "name": rule["name"],
            "execution_mode": rule["mode"],
            "loop_count": int(rule.get("loop_count", 1)),
            "loop_interval_ms": int(rule.get("loop_interval_ms", 0)),
            "loop_interval_jitter_ms": int(rule.get("loop_interval_jitter_ms", 0)),
            "max_runtime_s": int(rule.get("max_runtime_s", 0)),
            "actions": list(rule.get("actions", [])),
        }


class _FakeCaptureHook:
    def __init__(self, callback, stop_ok=False):
        self.callback = callback
        self.stop_ok = stop_ok
        self.start_count = 0
        self.stop_count = 0

    def start(self):
        self.start_count += 1
        return True

    def stop(self, timeout=1.5):
        del timeout
        self.stop_count += 1
        return self.stop_ok


class InteractionRiskFixes36Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        HotkeyEdit._active_capture_ref = None
        HotkeyEdit._pending_capture_hooks = []
        HotkeyEdit._pending_capture_retry_scheduled = False

    def tearDown(self):
        for hook in list(HotkeyEdit._pending_capture_hooks):
            hook.stop_ok = True
        HotkeyEdit._retry_pending_capture_hooks()
        HotkeyEdit._active_capture_ref = None

    def test_profile_hot_switch_aborts_and_keeps_gate_closed_on_release_failure(self):
        harness = _ProfileSwitchHarness()

        self.assertFalse(harness._activate_profile_by_id("new"))

        self.assertEqual(harness.active_profile_id, "old")
        self.assertEqual(harness.installed, [])
        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertEqual(harness.layers[0][0], DISABLED_LAYER_NAME)
        self.assertIn("强制释放键鼠", harness.engine_hint.text)
        self.assertEqual(harness.failures[-1][1], ["Kanata 虚拟键"])
        self.assertEqual(harness.diagnostics[-1][0], "profile_transition_release_failed")


    def test_profile_hot_switch_does_not_schedule_restore_when_timeout_also_has_release_failure(self):
        harness = _ProfileSwitchHarness()
        harness.remaining_tasks = ["task-1"]

        self.assertFalse(harness._activate_profile_by_id("new"))

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertIsNone(harness._macro_stop_gate_restore)
        self.assertIsNone(harness._deferred_profile_input_restore)
        self.assertIn("释放失败", harness.engine_hint.text)

    def test_backend_failure_keeps_gate_closed_and_reports_all_cleanup_failures(self):
        harness = _BackendFailureHarness()

        harness._handle_runtime_backend_failure("监听线程退出")

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertTrue(harness.running)
        self.assertEqual(harness.engine_state, EngineState.FAILED)
        self.assertEqual(harness.toggle_button.text, "重试停止输入引擎")
        self.assertIn("系统级兜底释放", harness.last_macro_release_failures)
        self.assertIn("Interception 输出上下文停止", harness.last_macro_release_failures)
        self.assertIn("主 Kanata停止超时", harness.last_macro_release_failures)
        self.assertIsNotNone(harness.interception_output)

    def test_emergency_stop_holds_output_gate_until_final_cleanup_finishes(self):
        harness = _EmergencyHarness(layer_ok=True)

        with patch.object(
            harness.engine,
            "change_layer",
            side_effect=lambda *_args, **_kwargs: (
                harness.gate_observations.append(
                    ("layer", harness.output_shutdown_in_progress, {})
                )
                or True
            ),
        ):
            self.assertTrue(harness.emergency_stop(disable_mappings=True, sound=False))

        self.assertTrue(all(item[1] for item in harness.gate_observations))
        self.assertTrue(harness.gate_observations[0][2]["keep_output_gate"])
        self.assertFalse(harness.output_shutdown_in_progress)
        self.assertFalse(harness.mappings_enabled)

    def test_emergency_stop_keeps_gate_closed_when_layer_disable_fails(self):
        harness = _EmergencyHarness(layer_ok=False)
        harness.engine.change_layer = lambda *_args, **_kwargs: False

        self.assertFalse(harness.emergency_stop(disable_mappings=True, sound=False))

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertTrue(harness.mappings_enabled)

    def test_failed_capture_hook_is_retained_and_stops_suppressing_input(self):
        first_hook = None
        created = []

        def factory(callback):
            nonlocal first_hook
            hook = _FakeCaptureHook(callback, stop_ok=False)
            created.append(hook)
            if first_hook is None:
                first_hook = hook
            return hook

        with patch("ui.editors.WinInput", side_effect=factory):
            first = HotkeyEdit()
            second = HotkeyEdit()
            first.start_capture()
            self.assertFalse(first.stop_capture())

            self.assertIn(first_hook, HotkeyEdit._pending_capture_hooks)
            self.assertFalse(first_hook.callback("A", True))

            second.start_capture()
            self.assertFalse(second.capturing)
            self.assertEqual(len(created), 1)
            self.assertIn("仍在退出", second.feedback_text)

    def test_menu_test_task_has_independent_identity_and_no_hold_or_toggle_ownership(self):
        harness = _MenuTestHarness()
        hold_task = harness._build_menu_test_task({
            "id": "preset-1",
            "name": "按住测试",
            "mode": "按住循环",
            "actions": [{"type": "等待"}],
        })
        toggle_task = harness._build_menu_test_task({
            "id": "preset-2",
            "name": "开关测试",
            "mode": "开关循环",
            "actions": [{"type": "等待"}],
        })
        infinite_task = harness._build_menu_test_task({
            "id": "preset-infinite",
            "name": "无限测试",
            "mode": "无限循环",
            "actions": [{"type": "等待"}],
        })
        fixed_task = harness._build_menu_test_task({
            "id": "preset-3",
            "name": "固定测试",
            "mode": "固定次数",
            "loop_count": 3,
            "actions": [{"type": "等待"}],
        })

        self.assertEqual(hold_task["id"], "test:preset-1")
        self.assertEqual(hold_task["_origin_preset_id"], "preset-1")
        self.assertEqual(hold_task["execution_mode"], "执行一次")
        self.assertEqual(toggle_task["execution_mode"], "执行一次")
        self.assertEqual(infinite_task["execution_mode"], "执行一次")
        self.assertEqual(fixed_task["execution_mode"], "固定次数")
        self.assertEqual(fixed_task["loop_count"], 3)

    def test_menu_test_no_longer_dispatches_a_synthetic_physical_down(self):
        source = (ROOT / "ui" / "action_execution.py").read_text("utf-8")
        method = source[source.index("    def test_selected_preset"):]
        self.assertIn("_build_menu_test_task", method)
        self.assertNotIn("source=\"menu_test\"", method)
        self.assertNotIn("_dispatch_preset_trigger(", method)


    def test_runtime_start_blocks_unresolved_release_failures_and_reopens_gate_only_after_success(self):
        source = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        method = source[source.index("    def _set_running_impl"):]
        self.assertIn("output_shutdown_in_progress", method)
        self.assertIn("and pending_cleanup:", method)
        self.assertIn("请先执行“强制释放键鼠”", method)
        self.assertIn("self.output_shutdown_in_progress = False", method)

    def test_full_config_rollback_checks_engine_restart_result(self):
        source = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        method = source[source.index("    def _overwrite_full_configuration_in_place"):
                        source.index("    def _persist_profile_manager_settings")]
        self.assertIn("restart_result = self._set_running_impl(True)", method)
        self.assertIn("if restart_result is False or not self.running:", method)
        self.assertIn("输入引擎未能恢复运行", method)


if __name__ == "__main__":
    unittest.main()
