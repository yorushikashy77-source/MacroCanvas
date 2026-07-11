import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import ui.input_runtime as input_runtime_module
import ui.runtime_diagnostics as diagnostics_module
from ui.input_runtime import InputRuntimeMixin
from ui.runtime_diagnostics import RuntimeDiagnosticsMixin


ROOT = Path(__file__).resolve().parents[1]


class _Engine:
    @staticmethod
    def is_running():
        return False


class _Controller:
    def __init__(self):
        self.tasks = {}

    @staticmethod
    def stop(_task_id):
        return None


class _InputHarness(InputRuntimeMixin):
    def __init__(self):
        self.input_state_lock = threading.RLock()
        self.interception_forwarded_down = set()
        self.engine = _Engine()
        self.running = True
        self.settings_dialog_active = True
        self.interception_input_control_only = False
        self.physical_modifiers = set()
        self.physical_down = set()
        self.system_hotkey_latched = set()
        self.runtime_emergency_key = "F8"
        self.runtime_emergency_modifiers = "无"
        self.runtime_global_toggle_enabled = False
        self.runtime_global_toggle_key = "F10"
        self.runtime_global_toggle_modifiers = "无"
        self.runtime_macro_pause_enabled = False
        self.runtime_macro_pause_key = "F9"
        self.runtime_macro_pause_modifiers = "无"
        self.runtime_recording_cancel_key = "F7"
        self.runtime_recording_finish_key = "F8"
        self.suppressed_trigger_names = set()
        self.active_sync_by_source = {}
        self.held_trigger_ids = {}
        self.global_toggle_latched = False
        self.macro_controller = _Controller()
        self.profile_trigger_allowed = True
        self.mappings_enabled = True

    @staticmethod
    def write_diagnostic(*_args, **_kwargs):
        return None

    @staticmethod
    def _consume_expected_kanata_event(_name, _down):
        return False

    @staticmethod
    def _release_invalid_conditional_holds():
        return False


class SettingsInputStateTests(unittest.TestCase):
    def test_release_during_settings_clears_stale_state_and_keeps_route(self):
        harness = _InputHarness()
        harness.physical_down.add("A")
        harness.suppressed_trigger_names.add("A")

        self.assertTrue(harness._global_hook_callback("A", False, interception=True))
        self.assertNotIn("A", harness.physical_down)
        self.assertNotIn("A", harness.suppressed_trigger_names)

    def test_key_still_held_when_settings_close_remains_physically_down(self):
        harness = _InputHarness()

        self.assertFalse(harness._global_hook_callback("B", True, interception=True))
        self.assertIn("B", harness.physical_down)

        harness.settings_dialog_active = False
        harness.running = False
        self.assertFalse(harness._global_hook_callback("B", True, interception=True))
        self.assertIn("B", harness.physical_down)


class DispatchRejectionTests(unittest.TestCase):
    def test_rejected_rule_does_not_swallow_source_down(self):
        harness = _InputHarness()
        harness.settings_dialog_active = False
        harness._runtime_mapping_rules = lambda: [{
            "id": "mapping-1",
            "enabled": True,
            "source": "A",
            "source_modifiers": "无",
            "condition_enabled": False,
            "_runtime_kind": "mapping",
            "mode": "执行一次",
        }]
        harness._dispatch_runtime_mapping_rule = lambda *_args: False

        with patch.object(
            input_runtime_module,
            "foreground_window_belongs_to_current_process",
            return_value=False,
        ):
            self.assertFalse(
                harness._global_hook_callback("A", True, interception=True)
            )
        self.assertNotIn("A", harness.suppressed_trigger_names)


class LooseSourceModifierRuntimeTests(unittest.TestCase):
    def test_bare_source_mapping_triggers_while_extra_modifiers_are_held(self):
        harness = _InputHarness()
        harness.settings_dialog_active = False
        harness.physical_input_sources = {
            "held-ctrl": "Ctrl",
            "held-shift": "Shift",
            "held-alt": "Alt",
        }
        harness._refresh_logical_physical_sets_locked()
        harness._runtime_mapping_rules = lambda: [{
            "id": "bare-caps",
            "enabled": True,
            "source": "Caps Lock",
            "source_modifiers": "无",
            "condition_enabled": False,
            "_runtime_kind": "mapping",
            "mode": "执行一次",
        }]
        dispatched = []

        def dispatch(rule, *_args):
            dispatched.append(rule["id"])
            return True

        harness._dispatch_runtime_mapping_rule = dispatch

        with patch.object(
            input_runtime_module,
            "foreground_window_belongs_to_current_process",
            return_value=False,
        ):
            self.assertTrue(
                harness._global_hook_callback(
                    "Caps Lock", True, interception=True
                )
            )
        self.assertEqual(dispatched, ["bare-caps"])

    def test_more_specific_source_modifier_rule_wins_before_bare_fallback(self):
        harness = _InputHarness()
        harness.settings_dialog_active = False
        harness.physical_input_sources = {
            "held-ctrl": "Ctrl",
            "held-shift": "Shift",
        }
        harness._refresh_logical_physical_sets_locked()
        harness._runtime_mapping_rules = lambda: [
            {
                "id": "bare-caps",
                "enabled": True,
                "source": "Caps Lock",
                "source_modifiers": "无",
                "condition_enabled": False,
                "_runtime_kind": "mapping",
                "mode": "执行一次",
            },
            {
                "id": "ctrl-caps",
                "enabled": True,
                "source": "Caps Lock",
                "source_modifiers": "Ctrl",
                "condition_enabled": False,
                "_runtime_kind": "mapping",
                "mode": "执行一次",
            },
        ]
        dispatched = []

        def dispatch(rule, *_args):
            dispatched.append(rule["id"])
            return True

        harness._dispatch_runtime_mapping_rule = dispatch

        with patch.object(
            input_runtime_module,
            "foreground_window_belongs_to_current_process",
            return_value=False,
        ):
            self.assertTrue(
                harness._global_hook_callback(
                    "Caps Lock", True, interception=True
                )
            )
        self.assertEqual(dispatched, ["ctrl-caps"])


class _DiagnosticHarness(RuntimeDiagnosticsMixin):
    def __init__(self):
        self.runtime_debug_enabled = False
        self.runtime_debug_events = []
        self.runtime_debug_lock = threading.RLock()
        self.runtime_debug_sequence = 0
        self.runtime_diagnostic_enabled = True
        self.running = True
        self.diagnostic_lock = threading.RLock()
        self.diagnostic_queue = queue.Queue(maxsize=128)
        self.diagnostic_writer_stop = threading.Event()
        self.diagnostic_writer_thread = None
        self.diagnostic_generation = 0
        self.diagnostic_dropped_count = 0
        self.diagnostic_session_id = "test"
        self.diagnostic_write_count = 0


class DiagnosticWriterTests(unittest.TestCase):
    def test_diagnostic_events_are_flushed_by_background_writer(self):
        harness = _DiagnosticHarness()
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            log_path = directory_path / "diagnostic.log"
            with (
                patch.object(diagnostics_module, "APP_DIR", directory_path),
                patch.object(diagnostics_module, "DIAGNOSTIC_LOG_PATH", log_path),
            ):
                for index in range(20):
                    harness.write_diagnostic(
                        "runtime_trigger_match", index=index
                    )
                self.assertTrue(harness._flush_diagnostic_queue(timeout=2.0))
                self.assertTrue(harness._stop_diagnostic_writer(timeout=2.0))
                lines = log_path.read_text("utf-8").splitlines()

        self.assertEqual(len(lines), 20)
        self.assertTrue(all('"event": "runtime_trigger_match"' in line for line in lines))


class StaticRegressionTests(unittest.TestCase):
    def test_all_task_liveness_checks_include_parallel_workers(self):
        for relative in (
            "ui/runtime_lifecycle.py",
            "ui/editor_workflow.py",
            "ui/profile_workflow.py",
        ):
            text = (ROOT / relative).read_text("utf-8")
            self.assertNotIn("task.thread.is_alive()", text)
            self.assertIn("task.has_live_threads()", text)

    def test_preset_is_suspended_only_after_delete_confirmation(self):
        text = (ROOT / "ui" / "preset_editor.py").read_text("utf-8")
        start = text.index("    def delete_preset")
        end = text.index("    def selected_preset_row", start)
        method = text[start:end]
        self.assertLess(
            method.index("if confirm.clickedButton() is not delete_button"),
            method.index("_suspend_preset_runtime_for_delete"),
        )

    def test_debugger_uses_incremental_event_rows(self):
        text = (ROOT / "ui" / "runtime_diagnostics.py").read_text("utf-8")
        self.assertIn("rendered_ids", text)
        self.assertIn("def sync_events(events):", text)
        self.assertIn("table.insertRow(row)", text)


if __name__ == "__main__":
    unittest.main()
