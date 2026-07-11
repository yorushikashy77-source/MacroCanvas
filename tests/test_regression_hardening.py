import threading
import unittest
from pathlib import Path

from core.constants import ConfigState
from engine.interception import InterceptionOutput
from engine.win_input import WinInput
from ui.input_runtime import InputRuntimeMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin


ROOT = Path(__file__).resolve().parents[1]


class RegressionHardeningTests(unittest.TestCase):
    def test_single_instance_lock_precedes_main_window_creation(self):
        text = (ROOT / "main.py").read_text("utf-8")
        self.assertIn("QLockFile", text)
        self.assertLess(text.index("tryLock(100)"), text.index("window = MainWindow()"))

    def test_discarded_mapping_deletion_restores_runtime_snapshot(self):
        editor = (ROOT / "ui" / "mapping_editor.py").read_text("utf-8")
        workflow = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        self.assertIn("pending_mapping_deletions", editor)
        self.assertIn("def _restore_suspended_mapping_runtime", editor)
        self.assertIn("_restore_discarded_mapping_deletions(", workflow)

    def test_failed_apply_keeps_candidate_but_does_not_restart_runtime(self):
        text = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        failure = text[text.index("        except Exception as error:"):]
        self.assertIn("restart_runtime=False", failure)
        self.assertIn("_reload_full_configuration_into_window(candidate)", failure)
        self.assertIn("输入引擎保持停止", failure)

    def test_backend_validation_keeps_ui_painting_without_user_input(self):
        text = (ROOT / "ui" / "engine_configuration.py").read_text("utf-8")
        self.assertIn("MacroCanvas-KanataValidation", text)
        self.assertIn("ExcludeUserInputEvents", text)

    def test_large_dialogs_are_resizable(self):
        backup = (ROOT / "ui" / "backup_manager.py").read_text("utf-8")
        profile = (ROOT / "ui" / "profile_manager.py").read_text("utf-8")
        self.assertNotIn("setFixedSize(930, 590)", backup)
        self.assertNotIn("setFixedSize(980, 650)", profile)

    def test_cancelled_auto_apply_cannot_continue_engine_start(self):
        class Toggle:
            @staticmethod
            def isChecked():
                return True

        class Combo:
            @staticmethod
            def currentText():
                return "游戏模式（Interception）"

        class Harness(RuntimeLifecycleMixin):
            def __init__(self):
                self.running = False
                self.runtime_diagnostic_enabled = False
                self.config_state = ConfigState.DIRTY
                self.auto_apply_checkbox = Toggle()
                self.backend_combo = Combo()
                self.physical_down = set()
                self.physical_modifiers = set()
                self.held_trigger_ids = {}
                self.kanata_trigger_down = set()
                self.active_sync_by_source = {}
                self.sync_output_counts = {}
                self.sync_output_lock = threading.RLock()
                self.expected_kanata_events = []
                self.expected_kanata_event_lock = threading.RLock()
                self.suppressed_trigger_names = set()
                self.system_hotkey_latched = set()
                self.recording_control_modifiers = set()
                self.profile_trigger_allowed = True
                self.apply_calls = 0
                self.snapshot_calls = 0

            def write_diagnostic(self, *_args, **_kwargs):
                pass

            def _set_loading_message(self, *_args, **_kwargs):
                pass

            def apply_changes(self):
                self.apply_calls += 1
                return False

            def _snapshot_runtime_config(self):
                self.snapshot_calls += 1

        harness = Harness()
        harness._set_running_impl(True)
        self.assertEqual(harness.apply_calls, 1)
        self.assertEqual(harness.snapshot_calls, 0)
        self.assertFalse(harness.running)
        self.assertEqual(harness.config_state, ConfigState.DIRTY)

    def test_destructive_editor_actions_are_confirmed_and_recoverable(self):
        mapping = (ROOT / "ui" / "mapping_editor.py").read_text("utf-8")
        delete_mapping = mapping[mapping.index("    def delete_mapping"):]
        self.assertLess(
            delete_mapping.index("confirm.exec()"),
            delete_mapping.index("_suspend_mapping_runtime_for_delete(mapping)"),
        )
        self.assertIn("确认删除映射", delete_mapping)

        preset = (ROOT / "ui" / "preset_editor.py").read_text("utf-8")
        self.assertIn('QPushButton("撤销")', preset)
        self.assertIn('QPushButton("重做")', preset)
        self.assertIn("QKeySequence.StandardKey.Undo", preset)
        self.assertIn("card.delete_shortcut", preset)

        actions = (ROOT / "ui" / "editor_workflow.py").read_text("utf-8")
        self.assertIn("def _confirm_action_deletion", actions)
        self.assertIn("删除后可在动作窗口中使用“撤销”恢复", actions)

    def test_profile_manager_stages_changes_until_main_apply(self):
        workflow = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        start = workflow.index("    def _commit_profile_manager_settings")
        end = workflow.index("    def open_profile_settings", start)
        commit = workflow[start:end]
        self.assertNotIn("_persist_profile_manager_settings", commit)
        self.assertIn("尚未写入配置", commit)

        manager = (ROOT / "ui" / "profile_manager.py").read_text("utf-8")
        self.assertIn("暂存档案修改", manager)

    def test_explicit_stop_can_release_only_owned_quarantined_mouse_state(self):
        runtime = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        toggle_start = runtime.index("    def toggle_running")
        toggle_end = runtime.index("    @Slot(bool)", toggle_start)
        toggle = runtime[toggle_start:toggle_end]
        self.assertIn("allow_owned_mouse_force_release=not target_enabled", toggle)
        self.assertIn(
            'output.pending_release_summary()["quarantined_mouse"]', runtime
        )
        self.assertIn("output.release_all(force=True) and output.stop()", runtime)
        self.assertGreaterEqual(
            runtime.count("self._failsafe_release_runtime_targets(force_all=True)"),
            2,
        )
        self.assertIn("self._start_stop_release_guard()", runtime)

    def test_failsafe_only_sends_up_for_configured_targets_that_are_down(self):
        class User32:
            @staticmethod
            def GetAsyncKeyState(vk):
                return 0x8000 if vk in (0x41, 0x01) else 0

        output = WinInput.__new__(WinInput)
        output.user32 = User32()
        sent = []
        output.send = lambda name, down: sent.append((name, down)) or True
        self.assertTrue(output.force_release_names(
            ["A", "B", "鼠标左键", "鼠标右键"], attempts=2
        ))
        self.assertEqual(sent, [("A", False), ("鼠标左键", False)])

    def test_driver_failsafe_releases_mouse_without_ownership_record(self):
        class Driver:
            calls = 0

            def interception_send(self, *_args):
                self.calls += 1
                return 1

        class Recovery:
            @staticmethod
            def send(*_args):
                return False

        output = InterceptionOutput.__new__(InterceptionOutput)
        output.lock = threading.RLock()
        output.context = 1
        output.mouse_device = 12
        output.keyboard_device = 0
        output.mouse_pressed = set()
        output.mouse_press_counts = {}
        output.mouse_press_contexts = {}
        output.mouse_release_quarantined = set()
        output.key_pressed = []
        output.key_press_counts = {}
        output.dll = Driver()
        output.recovery_output = Recovery()

        self.assertTrue(output.force_release_names_untracked(["鼠标左键"]))
        self.assertEqual(output.dll.calls, 1)

        main_window = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        self.assertIn("force_release_names_untracked(names)", main_window)
        self.assertIn("only_if_down=not force_all", main_window)

    def test_failed_interception_key_release_keeps_retryable_ownership(self):
        class Driver:
            def __init__(self):
                self.destroy_count = 0

            def interception_send(self, *_args):
                return 0

            def interception_destroy_context(self, _context):
                self.destroy_count += 1

        class Recovery:
            succeed = False

            def send(self, _name, _down):
                return self.succeed

        output = InterceptionOutput.__new__(InterceptionOutput)
        output.lock = threading.RLock()
        output.context = 1
        output.mouse_device = 0
        output.keyboard_device = 5
        output.mouse_pressed = set()
        output.mouse_press_counts = {}
        output.mouse_press_contexts = {}
        output.mouse_release_quarantined = set()
        key_id = ("A", 30, False)
        output.key_pressed = [key_id]
        output.key_press_counts = {key_id: 1}
        output.dll = Driver()
        output.recovery_output = Recovery()

        self.assertFalse(output.release_all())
        self.assertEqual(output.key_pressed, [key_id])
        self.assertEqual(output.key_press_counts, {key_id: 1})
        self.assertFalse(output.stop())
        self.assertEqual(output.context, 1)
        self.assertEqual(output.dll.destroy_count, 0)

        output.recovery_output.succeed = True
        self.assertTrue(output.stop())
        self.assertIsNone(output.context)
        self.assertEqual(output.dll.destroy_count, 1)
        self.assertEqual(output.key_pressed, [])
        self.assertEqual(output.key_press_counts, {})

    def test_fresh_mouse_down_recovers_a_missing_previous_release(self):
        class Harness(InputRuntimeMixin):
            def __init__(self):
                self.suspended_preset_ids = set()
                self.suspended_mapping_ids = set()
                self.active_sync_by_source = {
                    "鼠标侧键 1": {"mapping": {"id": "mapping"}}
                }
                self.events = []

            def _release_sync_mapping(self, mapping):
                self.events.append(("release", mapping["id"]))
                return True

            def _press_sync_mapping(self, mapping):
                self.events.append(("press", mapping["id"]))
                return True

        harness = Harness()
        rule = {
            "id": "mapping", "_runtime_kind": "mapping", "mode": "同步按住"
        }
        self.assertTrue(harness._dispatch_runtime_mapping_rule(
            rule, "鼠标侧键 1", True, repeated=False
        ))
        self.assertEqual(
            harness.events, [("release", "mapping"), ("press", "mapping")]
        )
        self.assertIn(
            "mapping", harness.active_sync_by_source["鼠标侧键 1"]
        )

        harness.active_sync_by_source = {
            "鼠标侧键 1": {"mapping": {"id": "mapping"}}
        }
        harness.events.clear()
        harness._release_sync_mapping = lambda mapping: (
            harness.events.append(("failed-release", mapping["id"])) or False
        )
        harness._dispatch_runtime_mapping_rule(
            rule, "鼠标侧键 1", False, repeated=False
        )
        self.assertIn(
            "mapping", harness.active_sync_by_source["鼠标侧键 1"]
        )


if __name__ == "__main__":
    unittest.main()
