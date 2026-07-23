import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QTreeWidget, QWidget

from core.constants import ConfigState, MacroState
from ui.macro_controls import MacroControlsMixin
from ui.overlays import ActivityOverlay
from ui.profile_workflow import ProfileWorkflowMixin
from ui.recording_workflow import RecordingWorkflowMixin
from ui.trigger_conflicts import TriggerConflictMixin
from macro.scheduler import MacroTask


ROOT = Path(__file__).resolve().parents[1]


class _TextWidget:
    def __init__(self):
        self.text = ""
        self.enabled = True

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, _style):
        pass

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)

    def isEnabled(self):
        return self.enabled


class _Task:
    def __init__(self, preset_id, *, running=True, live=True):
        self.preset = {"id": preset_id, "name": f"任务 {preset_id}"}
        self.run_event = threading.Event()
        if running:
            self.run_event.set()
        self.stop_event = threading.Event()
        self.live = live
        self.pause_count = 0
        self.resume_count = 0

    def has_live_threads(self):
        return self.live

    def pause(self):
        self.pause_count += 1
        self.run_event.clear()

    def resume(self):
        self.resume_count += 1
        self.run_event.set()


class _Controller:
    def __init__(self, tasks):
        self.lock = threading.RLock()
        self.tasks = {task.preset["id"]: task for task in tasks}

    def finish(self, preset_id):
        self.tasks.pop(preset_id, None)


class _ProfileSuspendHarness(ProfileWorkflowMixin):
    def __init__(self, tasks):
        self.output_shutdown_in_progress = False
        self.profile_trigger_allowed = True
        self.output_dispatch_lock = threading.RLock()
        self.macro_controller = _Controller(tasks)
        self._profile_input_paused_macro_ids = set()
        self.profile_input_temporarily_suspended = False
        self.profile_input_suspend_reason = ""
        self.active_profile_id = ""
        self.active_profile_layer = "base"
        self.mappings_enabled = True
        self.settings_input_mode_active = False
        self.recording_session_active = False
        self._shutdown_started = False
        self.running = True
        self._deferred_profile_input_restore = None
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        self.execution_info = _TextWidget()
        self.diagnostics = []
        self.layers = []

    def _change_runtime_profile_layer(self, layer, *, wait=True):
        self.layers.append((layer, wait))
        return True

    def _clear_profile_transition_state(self, release_outputs=True):
        self.cleared = bool(release_outputs)

    def write_diagnostic(self, event, **fields):
        self.diagnostics.append((event, fields))

    def refresh_status_ui(self):
        pass

    def refresh_macro_controls(self):
        pass


class _MacroFinishHarness(MacroControlsMixin):
    def __init__(self, tasks):
        self.macro_controller = _Controller(tasks)
        self.active_macro_id = "finished"
        self.execution_info = _TextWidget()
        self.engine_hint = _TextWidget()
        self.last_action_activity = {}
        self.macro_state = MacroState.RUNNING
        self.macro_status_detail = ""
        self.running = True
        self.recording_session_active = False
        self.auto_apply_checkbox = None

    def refresh_status_ui(self):
        pass

    def refresh_macro_controls(self):
        pass


class _MacroHistoryLocationHarness(MacroControlsMixin):
    def __init__(self):
        self.target_card = SimpleNamespace(preset_id="child")
        self.preset_cards = [self.target_card]
        self.opened_card = None
        self.focused = None

    def open_preset_actions_dialog(self, card):
        self.opened_card = card

    def _focus_submacro_overview_action(self, card, action_id):
        self.focused = (card, action_id)
        return action_id == "wait-space"


class _MacroHistoryDialogHarness(QWidget, MacroControlsMixin):
    def __init__(self):
        super().__init__()
        self.macro_run_history = [{
            "finished_at": time.time(),
            "preset_id": "child",
            "preset_name": "技能连招",
            "source": "测试方案",
            "status": "失败",
            "detail": "等待条件超时",
            "duration_ms": 100,
            "failure_action": "等待 Space 按住时",
            "action_preset_id": "child",
            "action_id": "wait-space",
        }]
        self.location_calls = []

    def locate_macro_run_history_action(self, preset_id, action_id=""):
        self.location_calls.append((
            preset_id, action_id, self.macro_run_history_dialog,
        ))
        return True


class _RecordingHarness(RecordingWorkflowMixin):
    def __init__(self):
        self.recording_restore_pending = False
        self.recording = False
        self.recording_session_active = True
        self.cancel_count = 0
        self.config_state = ConfigState.DIRTY
        self.reload_button = _TextWidget()
        self.toggle_button = _TextWidget()
        self.backend_combo = _TextWidget()
        self.tabs = _TextWidget()
        self.auto_apply_checkbox = _TextWidget()
        self.profile_selector_combo = _TextWidget()
        self.preset_cards = []

    def cancel_recording(self):
        self.cancel_count += 1


class _ConflictHarness(TriggerConflictMixin):
    global_toggle_enabled = True
    global_toggle_modifiers = "Ctrl"
    global_toggle_key = "F10"
    macro_pause_enabled = False
    macro_pause_modifiers = "无"
    macro_pause_key = "F9"
    emergency_modifiers = "无"
    emergency_key = "F8"
    recording_cancel_modifiers = "Shift"
    recording_cancel_key = "F10"
    recording_finish_modifiers = "无"
    recording_finish_key = "F7"
    base_profile_payload = {"mappings": [], "presets": []}
    profiles = []

    def _store_editor_payload(self):
        pass


class InteractionLogicFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_temporary_profile_suspend_resumes_only_tasks_it_paused(self):
        running = _Task("running", running=True)
        user_paused = _Task("user-paused", running=False)
        harness = _ProfileSuspendHarness([running, user_paused])

        self.assertTrue(harness._suspend_active_profile_input(reason="test"))
        self.assertEqual(running.pause_count, 1)
        self.assertEqual(user_paused.pause_count, 0)
        self.assertTrue(harness.profile_input_temporarily_suspended)

        self.assertTrue(harness._restore_active_profile_input(reason="test_restore"))
        self.assertEqual(running.resume_count, 1)
        self.assertEqual(user_paused.resume_count, 0)
        self.assertTrue(running.run_event.is_set())
        self.assertFalse(user_paused.run_event.is_set())
        self.assertFalse(harness.profile_input_temporarily_suspended)

    def test_profile_restore_waits_only_for_stopping_tasks(self):
        stopping = _Task("stopping", running=False)
        stopping.stop_event.set()
        harness = _ProfileSuspendHarness([stopping])
        harness._profile_input_paused_macro_ids.add("stopping")
        harness.profile_input_temporarily_suspended = True

        self.assertFalse(harness._restore_active_profile_input(reason="stop_pending"))
        self.assertIsNotNone(harness._deferred_profile_input_restore)

    def test_finished_active_macro_falls_back_to_another_live_task(self):
        finished = _Task("finished", live=False)
        remaining = _Task("remaining", running=True)
        harness = _MacroFinishHarness([finished, remaining])

        harness.on_macro_finished("finished")

        self.assertEqual(harness.active_macro_id, "remaining")
        self.assertEqual(harness.macro_state, MacroState.RUNNING)
        self.assertIn("任务 remaining", harness.execution_info.text)

    def test_macro_run_history_keeps_macro_result_without_mapping_noise(self):
        harness = _MacroFinishHarness([])
        completed = SimpleNamespace(
            preset={
                "id": "preset-a", "name": "技能连招",
                "_history_source": "快捷键 F1",
            },
            history_started_at=time.time() - 0.125,
            finish_reason="completed",
            last_action_context={},
        )
        entry = harness._record_macro_run_history(completed)
        self.assertEqual(entry["preset_name"], "技能连招")
        self.assertEqual(entry["source"], "快捷键 F1")
        self.assertEqual(entry["status"], "完成")
        self.assertGreaterEqual(entry["duration_ms"], 100)
        self.assertEqual(len(harness.macro_run_history), 1)

        failed = SimpleNamespace(
            preset={"id": "preset-a", "name": "技能连招"},
            history_started_at=time.time(), finish_reason="condition_timeout",
            last_action_context={
                "action": "等待条件 Space", "source_preset_id": "child",
                "action_id": "wait-space",
                "call_chain_ids": ["preset-a", "child"],
            },
        )
        failure = harness._record_macro_run_history(failed)
        self.assertEqual(failure["failure_action"], "等待条件 Space")
        self.assertEqual(failure["failure_action_type"], "")
        self.assertEqual(failure["action_preset_id"], "child")
        self.assertEqual(failure["action_id"], "wait-space")
        self.assertEqual(failure["call_chain_ids"], ["preset-a", "child"])

        mapping = SimpleNamespace(
            preset={"id": "mapping:basic", "name": "基础映射"},
            history_started_at=time.time(), finish_reason="completed",
        )
        self.assertIsNone(harness._record_macro_run_history(mapping))
        self.assertEqual(len(harness.macro_run_history), 2)

    def test_macro_history_location_opens_the_action_editor_and_focuses_action(self):
        harness = _MacroHistoryLocationHarness()

        self.assertTrue(
            harness.locate_macro_run_history_action("child", "wait-space")
        )
        self.assertIs(harness.opened_card, harness.target_card)
        self.assertEqual(harness.focused, (harness.target_card, "wait-space"))
        self.assertFalse(
            harness.locate_macro_run_history_action("missing", "wait-space")
        )

    def test_failed_macro_history_persists_only_redacted_failure_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "macro-run-history.json"
            harness = _MacroFinishHarness([])
            failed = SimpleNamespace(
                preset={"id": "preset-a", "name": "私人方案"},
                history_started_at=time.time(), finish_reason="condition_timeout",
                last_action_context={
                    "action": "等待条件 Space", "source_preset_id": "child",
                    "action_id": "wait-space", "action_type": "等待条件",
                    "call_chain_ids": ["preset-a", "child"],
                },
            )
            with patch("ui.macro_controls.MACRO_RUN_HISTORY_PATH", path):
                harness._record_macro_run_history(failed)
                payload = json.loads(path.read_text("utf-8"))
                self.assertEqual(len(payload["entries"]), 1)
                stored = payload["entries"][0]
                self.assertNotIn("preset_name", stored)
                self.assertNotIn("source", stored)
                self.assertNotIn("failure_action", stored)
                self.assertEqual(stored["action_id"], "wait-space")

                restored = _MacroFinishHarness([])
                self.assertTrue(restored.load_persistent_macro_run_history())
                self.assertEqual(restored.macro_run_history[0]["source"], "上次启动")
                self.assertEqual(restored.macro_run_history[0]["action_id"], "wait-space")

    def test_persistent_macro_history_drops_entries_past_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "macro-run-history.json"
            stale = time.time() - 3 * 24 * 60 * 60
            path.write_text(json.dumps({
                "version": 1,
                "retention_days": 1,
                "entries": [{
                    "finished_at": stale, "status": "失败",
                    "detail": "等待条件超时", "duration_ms": 1,
                    "action_id": "stale-action",
                }],
            }), "utf-8")
            harness = _MacroFinishHarness([])
            with patch("ui.macro_controls.MACRO_RUN_HISTORY_PATH", path):
                self.assertFalse(harness.load_persistent_macro_run_history())
                self.assertEqual(harness.macro_run_history, [])

    def test_history_retention_keeps_current_session_records(self):
        stale = time.time() - 3 * 24 * 60 * 60
        current = {
            "finished_at": stale, "status": "完成", "persisted": False,
        }
        restored = {
            "finished_at": stale, "status": "失败", "persisted": True,
        }
        harness = _MacroFinishHarness([])
        harness.macro_run_history = [current, restored]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "macro-run-history.json"
            with patch("ui.macro_controls.MACRO_RUN_HISTORY_PATH", path):
                harness.set_macro_run_history_retention_days(1)
                self.assertEqual(harness.macro_run_history, [current])
                self.assertEqual(
                    json.loads(path.read_text("utf-8"))["entries"], []
                )

    def test_persistent_macro_history_load_prunes_file_to_limit(self):
        now = time.time()
        entries = [{
            "finished_at": now - index,
            "status": "失败",
            "detail": "等待条件超时",
            "duration_ms": 1,
            "action_id": f"action-{index}",
        } for index in range(52)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "macro-run-history.json"
            path.write_text(json.dumps({
                "version": 1,
                "retention_days": 30,
                "entries": entries,
            }), "utf-8")
            harness = _MacroFinishHarness([])
            with patch("ui.macro_controls.MACRO_RUN_HISTORY_PATH", path):
                self.assertTrue(harness.load_persistent_macro_run_history())
                self.assertEqual(len(harness.macro_run_history), 50)
                stored = json.loads(path.read_text("utf-8"))["entries"]
                self.assertEqual(len(stored), 50)

    def test_persisted_macro_history_offers_only_locator_export(self):
        current = {
            "status": "失败",
        }
        persisted = {
            "status": "失败",
            "persisted": True,
        }
        self.assertEqual(
            _MacroFinishHarness([]).macro_history_export_options(current),
            {"locator": True, "full": True},
        )
        self.assertEqual(
            _MacroFinishHarness([]).macro_history_export_options(persisted),
            {"locator": True, "full": False},
        )

    def test_macro_history_filters_and_deletes_one_record(self):
        finished_at = time.time()
        current_failure = {
            "status": "失败", "preset_name": "当前方案",
            "finished_at": finished_at, "duration_ms": 1,
        }
        current_success = {
            "status": "完成", "preset_name": "当前方案",
            "finished_at": finished_at, "duration_ms": 1,
        }
        persisted_failure = {
            "status": "失败", "preset_name": "已脱敏宏", "persisted": True,
            "finished_at": finished_at, "duration_ms": 1,
        }
        harness = _MacroFinishHarness([])
        harness.macro_run_history = [
            current_failure, current_success, persisted_failure,
        ]

        self.assertEqual(
            harness.filtered_macro_run_history("失败", "全部"),
            [current_failure, persisted_failure],
        )
        self.assertEqual(
            harness.filtered_macro_run_history("全部", "本次启动"),
            [current_failure, current_success],
        )
        self.assertEqual(
            harness.filtered_macro_run_history("失败", "上次启动"),
            [persisted_failure],
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "macro-run-history.json"
            with patch("ui.macro_controls.MACRO_RUN_HISTORY_PATH", path):
                token = harness._macro_history_entry_token(current_success)
                self.assertIs(
                    harness._macro_history_entry_from_token(token),
                    current_success,
                )
                self.assertTrue(harness.delete_macro_run_history_entry(
                    token
                ))
                self.assertEqual(
                    harness.macro_run_history,
                    [current_failure, persisted_failure],
                )
                stored = json.loads(path.read_text("utf-8"))["entries"]
                self.assertEqual(len(stored), 2)
                self.assertTrue(all(
                    entry["status"] == "失败" for entry in stored
                ))
                self.assertFalse(harness.delete_macro_run_history_entry(
                    "not-a-history-entry"
                ))

    def test_clear_macro_history_requires_confirmation(self):
        entry = {"status": "失败", "finished_at": time.time()}
        harness = _MacroFinishHarness([])
        harness.macro_run_history = [entry]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "macro-run-history.json"
            with patch("ui.macro_controls.MACRO_RUN_HISTORY_PATH", path):
                with patch(
                    "ui.macro_controls.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.No,
                ):
                    self.assertFalse(
                        harness.clear_macro_run_history_with_confirmation()
                    )
                self.assertEqual(harness.macro_run_history, [entry])
                with patch(
                    "ui.macro_controls.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    self.assertTrue(
                        harness.clear_macro_run_history_with_confirmation()
                    )
                self.assertEqual(harness.macro_run_history, [])

    def test_macro_history_location_waits_for_dialog_to_close(self):
        harness = _MacroHistoryDialogHarness()

        def double_click_failure():
            dialog = harness.macro_run_history_dialog
            tree = dialog.findChild(QTreeWidget, "macroRunHistory")
            row = tree.topLevelItem(0)
            tree.setCurrentItem(row)
            tree.itemDoubleClicked.emit(row, 0)

        QTimer.singleShot(0, double_click_failure)
        harness.open_macro_run_history()
        self.app.processEvents()

        self.assertEqual(
            harness.location_calls,
            [("child", "wait-space", None)],
        )

    def test_finish_hotkey_during_countdown_does_not_cancel(self):
        harness = _RecordingHarness()
        harness.finish_recording()
        self.assertEqual(harness.cancel_count, 0)
        self.assertTrue(harness.recording_session_active)

    def test_recording_session_disables_apply_and_restores_dirty_state(self):
        harness = _RecordingHarness()
        harness._set_recording_configuration_actions_enabled(False)
        self.assertFalse(harness.reload_button.enabled)
        self.assertFalse(harness.toggle_button.enabled)
        self.assertFalse(harness.backend_combo.enabled)
        self.assertFalse(harness.tabs.enabled)
        harness._set_recording_configuration_actions_enabled(True)
        self.assertTrue(harness.reload_button.enabled)
        self.assertTrue(harness.toggle_button.enabled)
        self.assertTrue(harness.backend_combo.enabled)
        self.assertTrue(harness.tabs.enabled)

    def test_recording_restore_preserves_previously_disabled_controls(self):
        harness = _RecordingHarness()
        harness.backend_combo.setEnabled(False)
        harness._set_recording_configuration_actions_enabled(False)
        harness._set_recording_configuration_actions_enabled(True)
        self.assertFalse(harness.backend_combo.enabled)
        self.assertTrue(harness.toggle_button.enabled)

    def test_recording_dialog_rejects_reentrant_countdown_session(self):
        harness = _RecordingHarness()
        with patch(
            "ui.recording_workflow.QMessageBox.information"
        ) as information:
            harness.open_recording_dialog()
        information.assert_called_once()

    def test_recording_guards_engine_and_destructive_editor_entries(self):
        runtime = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        start = runtime.index("    def set_running(")
        end = runtime.index("    def _set_running_impl", start)
        self.assertIn("recording_session_active", runtime[start:end])

        for relative, method_name in (
            ("ui/mapping_editor.py", "delete_mapping"),
            ("ui/preset_editor.py", "delete_preset"),
            ("ui/editor_workflow.py", "delete_action_item"),
            ("ui/editor_workflow.py", "delete_selected_actions"),
            ("ui/editor_workflow.py", "clear_visible_actions"),
        ):
            source = (ROOT / relative).read_text("utf-8")
            method = source[source.index(f"    def {method_name}("):]
            self.assertIn(
                "_configuration_change_blocked_by_recording()",
                method.split("\n    def ", 1)[0],
            )

    def test_recording_guards_apply_and_window_close(self):
        runtime = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        apply_start = runtime.index("    def apply_changes(self):")
        apply_end = runtime.index("    def _apply_changes_impl", apply_start)
        self.assertIn("recording_session_active", runtime[apply_start:apply_end])

        shutdown = (ROOT / "ui" / "shutdown_coordinator.py").read_text("utf-8")
        close_start = shutdown.index("    def closeEvent(self, event):")
        close_method = shutdown[close_start:]
        self.assertIn("完成录制并继续退出", close_method)
        self.assertIn("放弃录制并退出", close_method)
        self.assertLess(
            close_method.index("recording_session_active"),
            close_method.index("has_unapplied_changes"),
        )

    def test_macro_output_waits_through_transient_profile_mismatch(self):
        task = MacroTask.__new__(MacroTask)
        task.stop_event = threading.Event()
        task.run_event = threading.Event()
        task.run_event.set()
        task.preset = {"_required_profile_id": "profile-a"}
        task.is_active = lambda: True
        profile_matches = threading.Event()
        task.profile_active = lambda _profile_id: profile_matches.is_set()
        sent = []
        task.send_output = lambda action, phase, **_kwargs: sent.append(
            (action, phase)
        ) or True
        task.expect_output = None
        task.engine = None

        timer = threading.Timer(0.04, profile_matches.set)
        timer.start()
        started = time.perf_counter()
        try:
            self.assertTrue(task._send({"type": "等待"}, "Tap"))
        finally:
            timer.cancel()
        self.assertGreaterEqual(time.perf_counter() - started, 0.03)
        self.assertFalse(task.stop_event.is_set())
        self.assertEqual(len(sent), 1)

    def test_overlay_delayed_hide_token_cannot_hide_newer_message(self):
        overlay = ActivityOverlay()
        first = overlay.show_message("第一条")
        second = overlay.show_message("第二条")
        self.assertFalse(overlay.hide_message(first))
        self.assertTrue(overlay.isVisible())
        self.assertTrue(overlay.hide_message(second))
        self.assertFalse(overlay.isVisible())
        overlay.close()

    def test_recording_and_global_controls_reject_shared_primary_key(self):
        text = (ROOT / "ui" / "hotkey_settings.py").read_text("utf-8")
        self.assertIn("recording_key != control_key", text)
        self.assertIn("不能与{control_label}", text)
        reports = _ConflictHarness().analyze_trigger_conflicts()
        self.assertTrue(any(
            item["severity"] == "error" and "共用主键 F10" in item["message"]
            for item in reports
        ))

    def test_stopped_engine_save_skips_runtime_backend_validation(self):
        text = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        start = text.index("    def _apply_changes_impl")
        end = text.index("    def set_running", start)
        method = text[start:end]
        self.assertIn('if transaction["was_running"]:', method)
        self.assertIn("listener_warning", method)
        self.assertIn("配置已保存，但当前系统环境未能建立全局控制监听", method)

    def test_process_guard_uses_same_stability_window_as_profile_switch(self):
        text = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        start = text.index("    def check_active_process_guards")
        end = text.index("    def check_foreground_profile", start)
        method = text[start:end]
        self.assertIn("_process_guard_candidate_since", method)
        self.assertIn("foreground_profile_stable_seconds", method)
        self.assertIn('reason="process_guard_candidate"', method)

    def test_process_guard_force_releases_only_quarantined_owned_mouse(self):
        text = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        start = text.index("    def check_active_process_guards")
        end = text.index("    def check_foreground_profile", start)
        method = text[start:end]
        stop = method.index("self.stop_all_macros(play_sound=False)")
        release = method.index(
            "self._retry_quarantined_mouse_releases(force=True)"
        )
        self.assertLess(stop, release)
        self.assertNotIn("_failsafe_release_runtime_targets", method)

    def test_quarantined_release_result_includes_interception_backend(self):
        text = (ROOT / "ui" / "input_runtime.py").read_text("utf-8")
        start = text.index("    def _retry_quarantined_mouse_releases")
        end = text.index("    def _receive_kanata_message", start)
        method = text[start:end]
        self.assertIn("output_released = bool", method)
        self.assertIn("macro_released and output_released", method)


if __name__ == "__main__":
    unittest.main()
