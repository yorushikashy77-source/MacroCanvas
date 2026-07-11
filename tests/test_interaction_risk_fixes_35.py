import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.constants import EngineState, MacroState
from ui.input_runtime import InputRuntimeMixin
from ui.main_window import MainWindow
from ui.recording_workflow import RecordingWorkflowMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin


ROOT = Path(__file__).resolve().parents[1]


class _TextStub:
    def __init__(self):
        self.text = ""
        self.style = ""

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)


class _EngineStub:
    last_command_error = ""

    def is_running(self):
        return False


class _SyncReleaseHarness(InputRuntimeMixin):
    def __init__(self):
        self.input_state_lock = threading.RLock()
        self.sync_output_lock = threading.RLock()
        self.active_sync_by_source = {}
        self.sync_output_counts = {}
        self.release_ok = False
        self.diagnostics = []

    def _release_sync_mapping(self, _mapping):
        return self.release_ok

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _TransactionHarness(RuntimeLifecycleMixin):
    def __init__(self):
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.active_profile_layer = "layer-a"
        self.mappings_enabled = True
        self.running = True
        self.direct_interception_active = False
        self.output_dispatch_lock = threading.RLock()
        self.input_state_lock = threading.RLock()
        self.sync_output_lock = threading.RLock()
        self.active_sync_by_source = {"A": {"m1": {"id": "m1"}}}
        self.sync_output_counts = {"sig": {"count": 1}}
        self.last_macro_release_failures = ["旧失败"]
        self.active_macro_id = "old"
        self.last_action_activity = {"old": 1}
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = "旧释放失败"
        self.release_sync_ok = False
        self.engine_state = EngineState.RUNNING
        self.engine_hint = _TextStub()
        self.engine = _EngineStub()
        self.keyboard_engine = _EngineStub()
        self.global_hook = None
        self.interception_input_hook = None
        self.interception_output = None
        self.layer_changes = []
        self.backend_stop_reached = False

    def _runtime_is_game_mode(self):
        return False

    def _change_runtime_profile_layer(self, layer, wait=True):
        self.layer_changes.append((layer, wait))
        return True

    def stop_all_macros(self, **_kwargs):
        return []

    def _release_all_sync_mappings(self):
        return self.release_sync_ok

    def _release_runtime_virtual_keys(self, **_kwargs):
        return True

    def _release_interception_output(self):
        return True

    def _failsafe_release_runtime_targets(self, **_kwargs):
        return True

    def _kanata_engine_has_runtime(self, _engine):
        self.backend_stop_reached = True
        return False


class _RecordingPrepareHarness(RecordingWorkflowMixin):
    def __init__(self):
        self.active_profile_layer = "layer-a"
        self.mappings_enabled = True
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.output_dispatch_lock = threading.RLock()
        self.running = True
        self.recording_options = {}
        self.last_macro_release_failures = ["Kanata 虚拟键"]
        self._macro_stop_gate_restore = None
        self._deferred_profile_input_restore = None
        self.engine_hint = _TextStub()
        self.engine = _EngineStub()
        self.keyboard_engine = _EngineStub()
        self.diagnostics = []

    def _runtime_is_game_mode(self):
        return False

    def _change_runtime_profile_layer(self, _layer, wait=True):
        del wait
        return True

    def stop_all_macros(self, **_kwargs):
        return []

    def _defer_profile_input_restore(self, **kwargs):
        self._deferred_profile_input_restore = kwargs

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class _RecordingRestoreHarness(RecordingWorkflowMixin):
    def __init__(self):
        self.recording_restore_layer = "layer-a"
        self.running = True
        self.profile_trigger_allowed = False
        self.macrocanvas_foreground_suspended = False
        self.runtime_profile_auto_switch_enabled = True
        self.activated = []
        self.restored = []

    def _runtime_is_game_mode(self):
        return False

    def _foreground_profile_id(self):
        return "other", "game.exe", "game"

    def _activate_profile_by_id(self, profile_id, **kwargs):
        self.activated.append((profile_id, kwargs))
        return True

    def _restore_active_profile_input(self, **kwargs):
        self.restored.append(kwargs)
        return True


class _ForceReleaseHarness:
    force_release_held_inputs = MainWindow.force_release_held_inputs

    def __init__(self):
        self.interception_output = None
        self.quarantined_mouse_release_lock = threading.RLock()
        self.quarantined_mouse_releases = []
        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        self.output_dispatch_lock = threading.RLock()
        self.running = True
        self.mappings_enabled = True
        self.active_profile_layer = "layer-a"
        self.settings_input_mode_active = False
        self.recording_session_active = False
        self._shutdown_started = False
        self.engine = _EngineStub()
        self.keyboard_engine = _EngineStub()
        self.runtime_release_target_history = {"A"}
        self.runtime_release_vkey_history = {"vk-a"}
        self.last_macro_release_failures = ["Kanata 虚拟键"]
        self.active_macro_id = "old"
        self.last_action_activity = {"old": 1}
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = "释放失败"
        self.execution_info = _TextStub()
        self.refresh_status_count = 0
        self.refresh_controls_count = 0
        self.layer_changes = []

    def held_input_snapshot(self):
        return []

    def _runtime_is_game_mode(self):
        return True

    def stop_all_macros(self, **_kwargs):
        return []

    def _retry_quarantined_mouse_releases(self, force=False):
        del force
        return True

    def _release_all_sync_mappings(self):
        return True

    def _kanata_engine_has_runtime(self, _engine):
        return False

    def _failsafe_release_runtime_targets(self, **_kwargs):
        return True

    def _change_runtime_profile_layer(self, layer, wait=True):
        self.layer_changes.append((layer, wait))
        return True

    def _start_stop_release_guard(self):
        pass

    def write_diagnostic(self, *_args, **_kwargs):
        pass

    def refresh_status_ui(self):
        self.refresh_status_count += 1

    def refresh_macro_controls(self):
        self.refresh_controls_count += 1


class InteractionRiskFixes35Tests(unittest.TestCase):
    def test_failed_sync_release_restores_exact_source_ownership(self):
        harness = _SyncReleaseHarness()
        mapping = {"id": "m1", "target": "A"}

        released, failed = harness._release_detached_sync_mappings([
            ("keyboard:1:A", "m1", mapping)
        ])

        self.assertEqual(released, [])
        self.assertEqual(failed, ["m1"])
        self.assertIs(
            harness.active_sync_by_source["keyboard:1:A"]["m1"], mapping
        )
        self.assertEqual(
            harness.diagnostics[-1][0], "sync_release_ownership_restored"
        )

    def test_config_apply_aborts_before_backend_retirement_on_release_failure(self):
        harness = _TransactionHarness()

        with self.assertRaisesRegex(RuntimeError, "同步映射输出"):
            harness._stop_runtime_backends_for_transaction()

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertEqual(harness.engine_state, EngineState.FAILED)
        self.assertIn("同步映射输出", harness.last_macro_release_failures)
        self.assertFalse(harness.backend_stop_reached)
        self.assertTrue(harness.active_sync_by_source)
        self.assertTrue(harness.sync_output_counts)

    def test_config_apply_success_clears_recovered_cleanup_state(self):
        harness = _TransactionHarness()
        harness.release_sync_ok = True

        harness._stop_runtime_backends_for_transaction()

        self.assertEqual(harness.engine_state, EngineState.STOPPED)
        self.assertEqual(harness.last_macro_release_failures, [])
        self.assertEqual(harness.macro_state, MacroState.IDLE)
        self.assertEqual(harness.macro_status_detail, "")
        self.assertIsNone(harness.active_macro_id)
        self.assertEqual(harness.last_action_activity, {})
        self.assertFalse(harness.running)
        self.assertFalse(harness.output_shutdown_in_progress)
        self.assertEqual(harness.active_sync_by_source, {})
        self.assertEqual(harness.sync_output_counts, {})

    def test_recording_prepare_rejects_release_failure_and_keeps_gate_closed(self):
        harness = _RecordingPrepareHarness()

        self.assertFalse(harness._enter_recording_input_mode())

        self.assertTrue(harness.output_shutdown_in_progress)
        self.assertFalse(harness.profile_trigger_allowed)
        self.assertIsNone(harness.recording_restore_layer)
        self.assertIsNone(harness._macro_stop_gate_restore)
        self.assertIn("强制释放键鼠", harness.engine_hint.text)
        self.assertEqual(
            harness.diagnostics[-1][0], "recording_prepare_release_failed"
        )

    def test_recording_finish_does_not_restore_layer_while_app_is_foreground(self):
        harness = _RecordingRestoreHarness()

        with patch(
            "ui.recording_workflow.foreground_window_belongs_to_current_process",
            return_value=True,
        ):
            harness._leave_recording_input_mode()

        self.assertFalse(harness.profile_trigger_allowed)
        self.assertTrue(harness.macrocanvas_foreground_suspended)
        self.assertEqual(harness.activated, [])
        self.assertEqual(harness.restored, [])

    def test_force_release_success_clears_timeout_and_unlocks_runtime_state(self):
        harness = _ForceReleaseHarness()

        with patch(
            "ui.main_window.foreground_window_belongs_to_current_process",
            return_value=False,
        ):
            self.assertTrue(
                harness.force_release_held_inputs(show_feedback=False)
            )

        self.assertEqual(harness.last_macro_release_failures, [])
        self.assertEqual(harness.macro_state, MacroState.IDLE)
        self.assertEqual(harness.macro_status_detail, "")
        self.assertIsNone(harness.active_macro_id)
        self.assertFalse(harness.output_shutdown_in_progress)
        self.assertTrue(harness.profile_trigger_allowed)
        self.assertEqual(harness.layer_changes[-1][0], "layer-a")
        self.assertGreater(harness.refresh_status_count, 0)
        self.assertGreater(harness.refresh_controls_count, 0)

    def test_full_config_overwrite_checks_stop_result_and_stopped_state(self):
        source = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        method = source[source.index("    def _overwrite_full_configuration_in_place"):
                        source.index("    def _persist_profile_manager_settings")]
        self.assertIn("stopped = self._set_running_impl(", method)
        self.assertIn("if stopped is False or self.running:", method)
        self.assertIn("remaining = self.stop_all_macros", method)
        self.assertIn("last_macro_release_failures", method)

    def test_process_guard_requires_all_cleanup_channels_before_success_message(self):
        source = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        method = source[source.index("    def check_active_process_guards"):
                        source.index("    def check_foreground_profile")]
        self.assertIn("remaining = self.stop_all_macros", method)
        self.assertIn("cleanup_failures = list", method)
        self.assertIn(
            "not remaining and not cleanup_failures and mouse_released", method
        )
        self.assertIn("if guarded_outputs_released:", method)

    def test_mapping_delete_uses_shared_release_ownership_recovery(self):
        source = (ROOT / "ui" / "mapping_editor.py").read_text("utf-8")
        method = source[source.index("    def _suspend_mapping_runtime_for_delete"):
                        source.index("    def _restore_suspended_mapping_runtime")]
        self.assertIn(
            "(trigger_token, mapping_id, held)", method
        )
        self.assertIn(
            'self, "_release_detached_sync_mappings", None', method
        )
        self.assertIn("release_detached(pending_sync_releases)", method)
        self.assertIn("setdefault(held_mapping_id, held)", method)


if __name__ == "__main__":
    unittest.main()
