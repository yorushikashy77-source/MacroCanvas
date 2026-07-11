import importlib
import sys
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ShutdownStaticTests(unittest.TestCase):
    def test_shutdown_waits_before_retiring_backends(self):
        text = (ROOT / "ui" / "shutdown_coordinator.py").read_text("utf-8")
        self.assertLess(
            text.index("_stop_macro_runtime_for_shutdown"),
            text.index("self.output_backend_retired = True"),
        )
        self.assertIn("wait_for_all(timeout=6.0)", text)
        self.assertIn("shutdown_completed_with_warnings", text)
        self.assertIn("shutdown_failed", text)
        shutdown_body = text[text.index("    def shutdown(self):"):text.index(
            "    def shutdown_error_summary", text.index("    def shutdown(self):")
        )]
        self.assertNotIn("except Exception:\n            pass", shutdown_body)

    def test_listener_and_runtime_threads_are_non_daemon(self):
        paths = [
            ROOT / "macro" / "scheduler.py",
            ROOT / "engine" / "interception.py",
            ROOT / "engine" / "win_input.py",
            ROOT / "engine" / "kanata.py",
            ROOT / "engine" / "kanata_command_runtime.py",
        ]
        combined = "\n".join(path.read_text("utf-8") for path in paths)
        self.assertGreaterEqual(combined.count("daemon=False"), 5)
        scheduler = paths[0].read_text("utf-8")
        self.assertIn("worker_threads", scheduler)
        self.assertIn("def wait_for_all", scheduler)
        self.assertIn("def remaining_task_details", scheduler)

    def test_main_window_delegates_shutdown(self):
        text = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        self.assertIn("ShutdownCoordinatorMixin", text)
        self.assertIn("InputListenerLifecycleMixin", text)
        self.assertIn("MappingEditorMixin", text)
        self.assertIn("PresetEditorMixin", text)
        self.assertIn("RuntimeLifecycleMixin", text)
        self.assertNotIn("    def shutdown(self):", text)
        self.assertNotIn("    def closeEvent(self, event):", text)

    def test_manual_engine_stop_keeps_backends_until_tasks_exit(self):
        text = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        marker = "# Keep all output resources alive while task-owned Release packets"
        stop_branch = text[text.index(marker):]
        self.assertLess(
            stop_branch.index("remaining = self.stop_all_macros"),
            stop_branch.index("self.running = False"),
        )
        self.assertIn("if remaining:", stop_branch)
        self.assertIn("return False", stop_branch)
        self.assertIn("remaining_task_details", stop_branch)

    def test_native_context_handles_survive_failed_destruction(self):
        text = (ROOT / "engine" / "interception.py").read_text("utf-8")
        self.assertIn("Preserve ownership so stop() can retry destruction", text)
        self.assertIn("Do not lose the native handle on failure", text)
        coordinator = (ROOT / "ui" / "shutdown_coordinator.py").read_text("utf-8")
        output_call = coordinator[coordinator.index("销毁Interception输出上下文"):]
        self.assertIn("critical=True", output_call[:220])

    def test_interception_normal_release_only_releases_owned_mouse_buttons(self):
        text = (ROOT / "engine" / "interception.py").read_text("utf-8")
        start = text.index("    def release_all")
        end = text.index("    def _emit_raw", start)
        method = text[start:end]
        self.assertIn("force=False", method)
        self.assertIn(
            "list(self.mouse_pressed) + list(self.mouse_press_counts)", method
        )
        self.assertIn("mouse_press_contexts", method)
        self.assertIn("foreground_window_identity_matches", method)
        automatic_runtime = "\n".join(
            (ROOT / path).read_text("utf-8")
            for path in (
                "ui/input_runtime.py",
                "ui/shutdown_coordinator.py",
                "ui/macro_controls.py",
            )
        )
        self.assertNotIn("release_all(force=True)", automatic_runtime)
        runtime = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        self.assertIn("allow_owned_mouse_force_release", runtime)
        self.assertIn('pending_release_summary()["quarantined_mouse"]', runtime)
        main_window = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        force_start = main_window.index("    def force_release_held_inputs")
        force_end = main_window.index("    def _build_runtime_entry", force_start)
        self.assertIn("release_all(force=True)", main_window[force_start:force_end])
        self.assertIn("确认跨窗口释放", main_window[force_start:force_end])

    def test_normal_shutdown_does_not_force_mouse_button_release(self):
        win_input = (ROOT / "engine" / "win_input.py").read_text("utf-8")
        self.assertIn("def force_release_all(self, include_mouse=False)", win_input)
        self.assertIn("if include_mouse:", win_input)
        coordinator = (ROOT / "ui" / "shutdown_coordinator.py").read_text("utf-8")
        shutdown_body = coordinator[
            coordinator.index("    def shutdown(self):"):
            coordinator.index("    def shutdown_error_summary")
        ]
        self.assertIn("self._force_release_system_inputs", shutdown_body)
        self.assertNotIn("include_mouse=True", shutdown_body)
        emergency_body = coordinator[
            coordinator.index("    def emergency_shutdown_fallback"):
            coordinator.index("    def closeEvent")
        ]
        self.assertNotIn("include_mouse=True", emergency_body)
        ui_text = "\n".join(
            (ROOT / path).read_text("utf-8")
            for path in (
                "ui/main_window.py",
                "ui/input_runtime.py",
                "ui/shutdown_coordinator.py",
                "ui/runtime_lifecycle.py",
                "ui/macro_controls.py",
            )
        )
        self.assertNotIn("include_mouse=True", ui_text)

    def test_open_profile_settings_refreshes_pending_payload_before_summary(self):
        text = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        start = text.index("    def open_profile_settings")
        end = text.index("    def _profile_name", start)
        method = text[start:end]
        self.assertIn("_store_editor_payload()", method)
        self.assertLess(
            method.index("_store_editor_payload()"),
            method.index("ProfileManagerDialog("),
        )
        self.assertIn("self.base_profile_payload", method)

    def test_profile_switch_stores_visible_profile_not_combo_target(self):
        text = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        store_start = text.index("    def _store_editor_payload")
        store_end = text.index("    @staticmethod", store_start)
        store_method = text[store_start:store_end]
        self.assertIn("_visible_editor_profile_id()", store_method)

        switch_start = text.index("    def _switch_editor_profile")
        switch_end = text.index("    def _reload_full_configuration_into_window", switch_start)
        switch_method = text[switch_start:switch_end]
        self.assertLess(
            switch_method.index("_load_profile_payload_into_editor"),
            switch_method.index("self.editor_profile_id = profile_id"),
        )

    def test_profile_selector_uses_committed_index_change(self):
        main = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        selector_setup = main[
            main.index("self.profile_selector_combo = QComboBox"):
            main.index("profile_selector_layout.addWidget", main.index(
                "self.profile_selector_combo = QComboBox"
            ))
        ]
        self.assertIn("currentIndexChanged.connect", selector_setup)
        self.assertIn("on_main_profile_index_changed", selector_setup)
        self.assertIn(".activated.connect", selector_setup)
        self.assertIn("on_main_profile_activated", selector_setup)
        self.assertIn("view().clicked.connect", selector_setup)
        self.assertIn("on_main_profile_view_clicked", selector_setup)

        workflow = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        start = workflow.index("    def on_main_profile_index_changed")
        end = workflow.index("    def on_main_profile_selected", start)
        method = workflow[start:end]
        self.assertIn("target_id = str(combo.itemData(index) or", method)
        self.assertIn("_profile_selector_change_generation", method)
        self.assertIn("QTimer.singleShot", method)
        self.assertNotIn('if str(combo.currentData() or "") != target_id', method)
        self.assertIn("self.on_main_profile_selected(index, target_id=target_id)", method)
        self.assertIn("_finalize_main_profile_selection", method)
        self.assertIn("self._sync_profile_selector_to_visible()", method)

        view_start = workflow.index("    def on_main_profile_view_clicked")
        view_end = workflow.index("    def on_main_profile_selected", view_start)
        view_method = workflow[view_start:view_end]
        self.assertIn("model_index.row()", view_method)
        self.assertIn("combo.hidePopup()", view_method)
        self.assertLess(
            view_method.index("combo.hidePopup()"),
            view_method.index("self.on_main_profile_index_changed"),
        )
        self.assertIn("self.on_main_profile_index_changed", view_method)

    def test_profile_selector_treats_base_id_as_valid_visible_profile(self):
        workflow = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        helper_start = workflow.index("    def _visible_editor_profile_id")
        helper_end = workflow.index("    def _payload_for_profile_id", helper_start)
        helper = workflow[helper_start:helper_end]
        self.assertIn("loaded_profile_id is None", helper)
        self.assertIn("return str(loaded_profile_id or", helper)

        refresh_start = workflow.index("    def refresh_profile_selector")
        refresh_end = workflow.index(
            "    @staticmethod\n    def _set_profile_selector_index", refresh_start
        )
        refresh_method = workflow[refresh_start:refresh_end]
        self.assertIn("_visible_editor_profile_id()", refresh_method)

        sync_start = workflow.index("    def _sync_profile_selector_to_visible")
        sync_end = workflow.index("    def refresh_profile_selector_state", sync_start)
        sync_method = workflow[sync_start:sync_end]
        self.assertIn("combo.findData(visible_profile_id)", sync_method)
        self.assertNotIn("combo.clear()", sync_method)


    def test_editor_profile_selection_is_not_blocked_by_runtime_transition(self):
        workflow = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        start = workflow.index("    def on_main_profile_selected")
        end = workflow.index("    def _runtime_profile_entry", start)
        method = workflow[start:end]
        self.assertIn("if self.profile_form_loading:", method)
        self.assertNotIn(
            "self.profile_form_loading or self.profile_switch_in_progress",
            method,
        )
        self.assertIn("_finalize_main_profile_selection", workflow)

    def test_runtime_profile_activation_does_not_rebuild_editor_selector(self):
        workflow = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        start = workflow.index("    def _activate_profile_by_id")
        end = workflow.index("    def _foreground_profile_id", start)
        method = workflow[start:end]
        self.assertNotIn("refresh_profile_selector()", method)
        self.assertIn("refresh_status_ui()", method)

    def test_profile_switch_rolls_back_editor_on_rebuild_failure(self):
        workflow = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        start = workflow.index("    def _switch_editor_profile")
        end = workflow.index("    def _reload_full_configuration_into_window", start)
        method = workflow[start:end]
        self.assertIn("previous_profile_id = self._visible_editor_profile_id()", method)
        self.assertIn("except Exception as error:", method)
        self.assertIn("previous_payload", method)
        self.assertIn("_last_profile_switch_error", method)


class SchedulerThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "PySide6.QtCore" not in sys.modules:
            qtcore = types.ModuleType("PySide6.QtCore")

            class QObject:
                pass

            class Signal:
                def __init__(self, *_args, **_kwargs):
                    pass

                def emit(self, *_args, **_kwargs):
                    pass

            qtcore.QObject = QObject
            qtcore.Signal = Signal
            pyside = types.ModuleType("PySide6")
            pyside.QtCore = qtcore
            sys.modules["PySide6"] = pyside
            sys.modules["PySide6.QtCore"] = qtcore
        cls.scheduler = importlib.import_module("macro.scheduler")

    def test_parallel_worker_is_tracked_and_waited(self):
        class Engine:
            @staticmethod
            def is_running():
                return True

        class Signal:
            @staticmethod
            def emit(*_args, **_kwargs):
                pass

        class Signals:
            progress = Signal()
            action_activity = Signal()
            task_finished = Signal()
            state_changed = Signal()

        task = self.scheduler.MacroTask(
            {"id": "tracked", "name": "tracked", "actions": []},
            Engine(), Signals(), is_active=lambda: True,
        )
        workers, results = [], []
        lock = threading.Lock()
        release = threading.Event()
        task._launch_parallel(
            lambda: release.wait(0.5) or True,
            "tracked-worker", workers, results, lock,
        )
        deadline = time.time() + 0.5
        while time.time() < deadline and not task.live_threads():
            time.sleep(0.01)
        self.assertTrue(any(t.name == "tracked-worker" for t in task.live_threads()))
        self.assertTrue(all(not t.daemon for t in task.live_threads()))
        release.set()
        self.assertTrue(task.wait_for_exit(timeout=1.0))

    def test_controller_reports_parallel_worker_timeout(self):
        class Engine:
            @staticmethod
            def is_running():
                return True

        class Signal:
            @staticmethod
            def emit(*_args, **_kwargs):
                pass

        class Signals:
            progress = Signal()
            action_activity = Signal()
            task_finished = Signal()
            state_changed = Signal()

        controller = self.scheduler.MacroController(
            Engine(), is_active=lambda: True
        )
        task = self.scheduler.MacroTask(
            {"id": "blocked", "name": "blocked", "actions": []},
            Engine(), Signals(), is_active=lambda: True,
        )
        blocker = threading.Event()
        workers, results = [], []
        lock = threading.Lock()
        task._launch_parallel(
            lambda: blocker.wait(1.0) or True,
            "blocked-worker", workers, results, lock,
        )
        controller.tasks["blocked"] = task
        remaining = controller.stop_all(timeout=0.05)
        self.assertEqual(remaining, ["blocked"])
        self.assertIn("blocked", controller.remaining_task_details())
        self.assertFalse(controller.start({
            "id": "blocked", "name": "replacement", "actions": []
        }))
        blocker.set()
        self.assertEqual(controller.wait_for_all(timeout=1.0), [])

    def test_mouse_release_is_quarantined_after_foreground_change(self):
        class Engine:
            @staticmethod
            def is_running():
                return True

        class Signal:
            @staticmethod
            def emit(*_args, **_kwargs):
                pass

        class Signals:
            progress = Signal()
            action_activity = Signal()
            task_finished = Signal()
            state_changed = Signal()

        sent = []
        quarantined = []
        original_identity = self.scheduler.foreground_window_identity
        original_matches = self.scheduler.foreground_window_identity_matches
        self.scheduler.foreground_window_identity = lambda: {
            "hwnd": 100, "pid": 200, "process": "target.exe", "title": "target"
        }
        self.scheduler.foreground_window_identity_matches = lambda _identity: False
        try:
            task = self.scheduler.MacroTask(
                {"id": "mouse", "name": "mouse", "actions": []},
                Engine(), Signals(), is_active=lambda: True,
                send_output=lambda action, phase, **_kwargs: sent.append(phase) or True,
                quarantine_release=lambda action, context: (
                    quarantined.append((action, context)) or True
                ),
            )
            action = {"type": "鼠标点击", "target": "鼠标右键", "modifiers": "无"}
            self.assertTrue(task.press(action))
            self.assertTrue(task.release(action))
        finally:
            self.scheduler.foreground_window_identity = original_identity
            self.scheduler.foreground_window_identity_matches = original_matches
        self.assertEqual(sent, ["Press"])
        self.assertFalse(task.pressed)
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0][1]["hwnd"], 100)

    def test_mouse_release_requires_an_exact_candidate_window(self):
        window_context_path = ROOT / "engine" / "window_context.py"
        spec = importlib.util.spec_from_file_location(
            "window_context_exact_window_test", window_context_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_identity = module.foreground_window_identity
        module.foreground_window_identity = lambda: {
            "hwnd": 222, "pid": 77, "process": "same.exe", "title": "dialog"
        }
        try:
            self.assertFalse(module.foreground_window_identity_matches({
                "hwnd": 111, "pid": 77, "process": "same.exe", "title": "main"
            }))
            self.assertTrue(module.foreground_window_identity_matches({
                "_unstable": True,
                "before": {"hwnd": 111, "pid": 77},
                "after": {"hwnd": 222, "pid": 77},
            }))
            module.foreground_window_identity = lambda: {
                "hwnd": 333, "pid": 77, "process": "same.exe", "title": "other"
            }
            self.assertFalse(module.foreground_window_identity_matches({
                "_unstable": True,
                "before": {"hwnd": 111, "pid": 77},
                "after": {"hwnd": 222, "pid": 77},
            }))
        finally:
            module.foreground_window_identity = original_identity


if __name__ == "__main__":
    unittest.main()
