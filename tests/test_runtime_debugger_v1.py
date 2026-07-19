import threading
import time
import unittest
from types import SimpleNamespace

from PySide6.QtWidgets import (
    QApplication, QDialog, QLabel, QLineEdit, QPushButton,
)

from macro.scheduler import MacroController, MacroTask
from ui.editors import ActionTreeWidget
from ui.editor_workflow import EditorWorkflowMixin
from ui.runtime_diagnostics import RuntimeDiagnosticsMixin


class _Signal:
    def __init__(self):
        self.values = []
        self.lock = threading.RLock()

    def emit(self, value=None):
        with self.lock:
            self.values.append(value)


class _Signals:
    def __init__(self):
        self.progress = _Signal()
        self.action_activity = _Signal()
        self.task_finished = _Signal()
        self.state_changed = _Signal()


class _Engine:
    @staticmethod
    def is_running():
        return True


def _preset(preset_id, actions, parameters=None):
    return {
        "id": preset_id,
        "name": preset_id,
        "enabled": True,
        "execution_mode": "执行一次",
        "speed_percent": 100,
        "actions": actions,
        "parameters": list(parameters or []),
    }


def _wait_until(predicate, timeout=1.5):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class RuntimeDebuggerTaskTests(unittest.TestCase):
    def make_task(self, preset, breakpoints=None):
        sent = []
        signals = _Signals()

        def send(action, phase, **_kwargs):
            sent.append((action.get("target"), phase))
            return True

        state = {
            "enabled": True,
            "breakpoints": set(breakpoints or set()),
        }
        task = MacroTask(
            preset,
            _Engine(),
            signals,
            send_output=send,
            is_active=lambda: True,
            debug_state=lambda: state,
        )
        return task, signals, sent, state

    def test_breakpoint_pauses_before_output_then_continue_runs_action(self):
        task, signals, sent, _state = self.make_task(
            _preset("root", [{
                "action_id": "first",
                "type": "键盘点击",
                "target": "A",
                "hold_ms": 1,
            }]),
            {("root", "first")},
        )
        task.start()
        self.assertTrue(_wait_until(lambda: task.debug_pause_info is not None))
        self.assertFalse(task.run_event.is_set())
        self.assertEqual(sent, [])
        self.assertEqual(task.debug_pause_info["action_id"], "first")
        self.assertTrue(task.debug_continue())
        self.assertTrue(task.wait_for_exit(timeout=1.5))
        self.assertEqual(sent, [("A", "Press"), ("A", "Release")])
        pauses = [
            item for item in signals.action_activity.values
            if isinstance(item, dict) and item.get("phase") == "debug_pause"
        ]
        self.assertEqual(pauses[0]["debug_reason"], "breakpoint")
        finished = [
            item for item in signals.action_activity.values
            if isinstance(item, dict) and item.get("phase") == "finished"
        ]
        self.assertEqual(finished[-1]["finish_reason"], "completed")

    def test_single_step_pauses_before_the_following_action(self):
        actions = [
            {
                "action_id": "first", "type": "键盘点击",
                "target": "A", "hold_ms": 1,
            },
            {
                "action_id": "second", "type": "键盘点击",
                "target": "B", "hold_ms": 1,
            },
        ]
        task, _signals, sent, _state = self.make_task(
            _preset("root", actions), {("root", "first")}
        )
        task.start()
        self.assertTrue(_wait_until(
            lambda: (task.debug_pause_info or {}).get("action_id") == "first"
        ))
        self.assertTrue(task.debug_step())
        self.assertTrue(_wait_until(
            lambda: (task.debug_pause_info or {}).get("action_id") == "second"
        ))
        self.assertEqual(sent, [("A", "Press"), ("A", "Release")])
        self.assertEqual(task.debug_pause_info["reason"], "step")
        self.assertTrue(task.debug_continue())
        self.assertTrue(task.wait_for_exit(timeout=1.5))
        self.assertEqual(
            sent,
            [
                ("A", "Press"), ("A", "Release"),
                ("B", "Press"), ("B", "Release"),
            ],
        )

    def test_breakpoint_releases_and_restores_an_existing_held_output(self):
        task, _signals, sent, _state = self.make_task(
            _preset("root", []), {("root", "next")}
        )
        held = {
            "action_id": "held", "type": "键盘点击",
            "target": "A", "hold_ms": 100,
        }
        self.assertTrue(task.press(held))
        results = []
        worker = threading.Thread(
            target=lambda: results.append(task._debug_before_action({
                "action_id": "next", "type": "等待", "wait_ms": 5,
            }))
        )
        worker.start()
        self.assertTrue(_wait_until(lambda: task.debug_pause_info is not None))
        self.assertEqual(sent, [("A", "Press"), ("A", "Release")])
        self.assertTrue(task.debug_continue())
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True])
        self.assertEqual(sent[-1], ("A", "Press"))
        self.assertTrue(task.force_release())
        self.assertEqual(sent[-1], ("A", "Release"))

    def test_parallel_breakpoints_do_not_hold_debug_lock_while_waiting(self):
        task, _signals, _sent, _state = self.make_task(
            _preset("root", []),
            {("root", "left"), ("root", "right")},
        )
        results = []
        workers = [
            threading.Thread(
                target=lambda action_id=action_id: results.append(
                    task._debug_before_action({
                        "action_id": action_id,
                        "type": "等待",
                        "wait_ms": 1,
                    })
                )
            )
            for action_id in ("left", "right")
        ]
        for worker in workers:
            worker.start()

        seen = set()
        for _index in range(2):
            self.assertTrue(_wait_until(
                lambda: bool(task.debug_pause_info)
                and task.debug_pause_info.get("action_id") not in seen
            ))
            seen.add(task.debug_pause_info["action_id"])
            self.assertTrue(task.debug_continue())

        for worker in workers:
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
        self.assertEqual(seen, {"left", "right"})
        self.assertEqual(results, [True, True])

    def test_pause_next_and_step_serialize_parallel_action_frontier(self):
        task, _signals, _sent, _state = self.make_task(_preset("root", []))
        self.assertTrue(task.debug_pause_next_action())
        results = []
        workers = [
            threading.Thread(
                target=lambda action_id=action_id: results.append(
                    task._debug_before_action({
                        "action_id": action_id,
                        "type": "等待",
                        "wait_ms": 1,
                    })
                )
            )
            for action_id in ("left", "right")
        ]
        for worker in workers:
            worker.start()

        self.assertTrue(_wait_until(lambda: bool(task.debug_pause_info)))
        first = task.debug_pause_info["action_id"]
        self.assertEqual(task.debug_pause_info["reason"], "step")
        self.assertTrue(task.debug_step())
        self.assertTrue(_wait_until(
            lambda: bool(task.debug_pause_info)
            and task.debug_pause_info.get("action_id") != first
        ))
        self.assertEqual(task.debug_pause_info["reason"], "step")
        self.assertTrue(task.debug_continue())
        for worker in workers:
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True, True])

    def test_submacro_activity_reports_path_and_resolved_parameters(self):
        child = _preset("child", [{
            "action_id": "child-key",
            "type": "键盘点击",
            "target": "A",
            "hold_ms": 1,
            "parameter_bindings": {"target": "目标键"},
        }], [{"name": "目标键", "type": "按键", "default": "A"}])
        root = _preset("root", [{
            "action_id": "call-child",
            "type": "调用子宏",
            "preset_id": "child",
            "repeat_count": 1,
            "speed_percent": 100,
            "parameter_values": {"目标键": "B"},
        }])
        library = {"root": root, "child": child}
        root["_preset_library"] = library
        child["_preset_library"] = library
        task, signals, sent, state = self.make_task(root)
        state["enabled"] = False
        task.started_at = time.perf_counter()
        self.assertTrue(task.run_action_group(root["actions"][0], 100))
        self.assertEqual(sent, [("B", "Press"), ("B", "Release")])
        child_events = [
            item for item in signals.action_activity.values
            if isinstance(item, dict) and item.get("action_id") == "child-key"
        ]
        self.assertEqual(child_events[-1]["source_preset_id"], "child")
        self.assertEqual(child_events[-1]["path"], ["root", "child"])
        self.assertEqual(child_events[-1]["parameters"], {"目标键": "B"})


class RuntimeDebuggerControllerTests(unittest.TestCase):
    def test_controller_normalizes_breakpoints_and_controls_debug_task(self):
        controller = MacroController(_Engine(), is_active=lambda: True)
        controller.set_debug_enabled(True)
        controller.set_debug_breakpoints({("root", "first"), ("", "bad")})
        snapshot = controller._debug_snapshot()
        self.assertTrue(snapshot["enabled"])
        self.assertEqual(snapshot["breakpoints"], {("root", "first")})

        task = MacroTask(
            _preset("root", []), _Engine(), _Signals(),
            is_active=lambda: True, debug_state=controller._debug_snapshot,
        )
        task.debug_pause_next = True
        controller.tasks["root"] = task
        controller.set_debug_enabled(False)
        self.assertFalse(task.debug_pause_next)

    def test_disabling_debugger_resumes_only_debugger_owned_pause(self):
        controller = MacroController(_Engine(), is_active=lambda: True)
        controller.set_debug_enabled(True)
        controller.set_debug_breakpoints({("root", "first")})
        sent = []

        def send(action, phase, **_kwargs):
            sent.append((action.get("target"), phase))
            return True

        task = MacroTask(
            _preset("root", [{
                "action_id": "first", "type": "键盘点击",
                "target": "A", "hold_ms": 1,
            }]),
            _Engine(), _Signals(), send_output=send,
            is_active=lambda: True, debug_state=controller._debug_snapshot,
        )
        controller.tasks["root"] = task
        task.start()
        try:
            self.assertTrue(_wait_until(lambda: bool(task.debug_pause_info)))
            self.assertFalse(task.run_event.is_set())
            controller.set_debug_enabled(False)
            self.assertTrue(task.wait_for_exit(timeout=1.5))
            self.assertEqual(sent, [("A", "Press"), ("A", "Release")])
            self.assertIsNone(task.debug_pause_info)
        finally:
            task.stop()
            task.wait_for_exit(timeout=1.0)

        manually_paused = MacroTask(
            _preset("manual", []), _Engine(), _Signals(),
            is_active=lambda: True, debug_state=controller._debug_snapshot,
        )
        self.assertTrue(manually_paused.pause())
        controller.tasks["manual"] = manually_paused
        controller.set_debug_enabled(False)
        self.assertFalse(manually_paused.run_event.is_set())
        manually_paused.stop()

    def test_nested_breakpoint_step_parameters_then_debugger_close_continues(self):
        grand = _preset("grand", [
            {
                "action_id": "grand-first", "type": "键盘点击",
                "target": "A", "hold_ms": 1,
                "parameter_bindings": {"target": "grand_key"},
            },
            {
                "action_id": "grand-second", "type": "键盘点击",
                "target": "D", "hold_ms": 1,
            },
        ], [{"name": "grand_key", "type": "按键", "default": "A"}])
        child = _preset("child", [
            {
                "action_id": "child-key", "type": "键盘点击",
                "target": "A", "hold_ms": 1,
                "parameter_bindings": {"target": "child_key"},
            },
            {
                "action_id": "call-grand", "type": "调用子宏",
                "preset_id": "grand", "repeat_count": 1,
                "speed_percent": 100,
                "parameter_values": {"grand_key": "C"},
            },
        ], [{"name": "child_key", "type": "按键", "default": "A"}])
        root = _preset("root", [
            {
                "action_id": "root-first", "type": "键盘点击",
                "target": "A", "hold_ms": 1,
            },
            {
                "action_id": "call-child", "type": "调用子宏",
                "preset_id": "child", "repeat_count": 1,
                "speed_percent": 100,
                "parameter_values": {"child_key": "B"},
            },
        ])
        library = {"root": root, "child": child, "grand": grand}
        for preset in library.values():
            preset["_preset_library"] = library

        controller = MacroController(_Engine(), is_active=lambda: True)
        controller.set_debug_enabled(True)
        controller.set_debug_breakpoints({("grand", "grand-first")})
        sent = []

        def send(action, phase, **_kwargs):
            sent.append((action.get("target"), phase))
            return True

        task = MacroTask(
            root, _Engine(), _Signals(), send_output=send,
            is_active=lambda: True, debug_state=controller._debug_snapshot,
        )
        controller.tasks["root"] = task
        task.start()
        try:
            self.assertTrue(_wait_until(
                lambda: (task.debug_pause_info or {}).get("action_id")
                == "grand-first"
            ))
            self.assertEqual(
                sent,
                [
                    ("A", "Press"), ("A", "Release"),
                    ("B", "Press"), ("B", "Release"),
                ],
            )
            self.assertEqual(
                task.debug_pause_info["path"], ["root", "child", "grand"]
            )
            self.assertEqual(
                task.debug_pause_info["parameters"], {"grand_key": "C"}
            )

            self.assertTrue(controller.debug_step("root"))
            self.assertTrue(_wait_until(
                lambda: (task.debug_pause_info or {}).get("action_id")
                == "grand-second"
            ))
            self.assertEqual(sent[-2:], [("C", "Press"), ("C", "Release")])
            self.assertEqual(task.debug_pause_info["reason"], "step")
            self.assertEqual(
                task.debug_pause_info["parameters"], {"grand_key": "C"}
            )

            controller.set_debug_enabled(False)
            self.assertTrue(task.wait_for_exit(timeout=1.5))
            self.assertEqual(sent[-2:], [("D", "Press"), ("D", "Release")])
            self.assertEqual(task.finish_reason, "completed")
        finally:
            task.stop()
            task.wait_for_exit(timeout=1.0)


class _BreakpointHarness(EditorWorkflowMixin):
    def __init__(self):
        self.selected_preset_card = None
        self.preset_cards = []
        self.runtime_debug_breakpoints = set()
        self.runtime_debug_current_action = {}
        self.macro_controller = MacroController(_Engine(), is_active=lambda: True)
        self.debugger_open_count = 0
        self.engine_hint = QLabel()

    def select_preset_card(self, card):
        self.selected_preset_card = card
        self.action_table = card.action_table

    def update_card_action_summary(self, _card):
        pass

    def _loading_checkpoint(self, *_args, **_kwargs):
        pass

    def action_changed(self, _card=None):
        pass

    def open_runtime_debugger(self):
        self.debugger_open_count += 1


class _RuntimeDialogHarness(RuntimeDiagnosticsMixin, QDialog):
    def __init__(self):
        QDialog.__init__(self)
        self.runtime_debug_dialog = None
        self.runtime_debug_enabled = False
        self.runtime_debug_events = []
        self.runtime_debug_lock = threading.RLock()
        self.runtime_debug_sequence = 0
        self.runtime_debug_breakpoints = {("root", "first")}
        self.runtime_debug_current_action = {}
        self.macro_controller = MacroController(_Engine(), is_active=lambda: True)
        self.active_macro_id = None
        self.preset_cards = []

    @staticmethod
    def held_input_snapshot():
        return []

    @staticmethod
    def _runtime_is_game_mode():
        return False


class RuntimeDebuggerQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_action_breakpoint_toggle_updates_controller_and_markers(self):
        harness = _BreakpointHarness()
        table = ActionTreeWidget()
        table.setColumnCount(5)
        card = SimpleNamespace(
            preset_id="root",
            name=QLineEdit("root"),
            parameter_definitions=[],
            action_dialog=QDialog(),
            action_table=table,
            action_title=QLabel(),
            loop_points_button=QPushButton(),
            _actions_loaded=True,
            _pending_actions=[],
        )
        harness.preset_cards.append(card)
        harness.select_preset_card(card)
        item = harness.add_action({
            "action_id": "first", "type": "等待", "wait_ms": 5,
            "children": [],
        }, save=False, card=card)
        item.setSelected(True)

        self.assertTrue(harness.toggle_selected_action_breakpoints(card))
        self.assertEqual(
            harness.runtime_debug_breakpoints, {("root", "first")}
        )
        self.assertEqual(
            harness.macro_controller._debug_snapshot()["breakpoints"],
            {("root", "first")},
        )
        self.assertIn("●", item.text(0))
        self.assertEqual(harness.debugger_open_count, 1)

        harness.runtime_debug_current_action = {
            "source_preset_id": "root", "action_id": "first",
        }
        harness._update_action_variable_marker(item, card)
        self.assertIn("▶", item.text(0))

        self.assertTrue(harness.toggle_selected_action_breakpoints(card))
        self.assertEqual(harness.runtime_debug_breakpoints, set())
        self.assertNotIn("●", item.text(0))

        item.setSelected(True)
        self.assertTrue(harness.toggle_selected_action_breakpoints(card))
        harness.runtime_debug_current_action = {
            "source_preset_id": "root", "action_id": "first",
        }
        table.clear()
        self.assertTrue(harness._prune_runtime_debug_breakpoints(card))
        self.assertEqual(harness.runtime_debug_breakpoints, set())
        self.assertEqual(harness.runtime_debug_current_action, {})

    def test_dialog_enables_debugging_and_disables_it_when_closed(self):
        harness = _RuntimeDialogHarness()
        harness.open_runtime_debugger()
        self.app.processEvents()
        self.assertTrue(harness.runtime_debug_enabled)
        self.assertTrue(harness.macro_controller._debug_snapshot()["enabled"])
        labels = {
            button.text()
            for button in harness.runtime_debug_dialog.findChildren(QPushButton)
        }
        self.assertTrue(
            {"下一动作暂停", "单步", "继续", "定位动作"}.issubset(labels)
        )
        sent = []

        def send(action, phase, **_kwargs):
            sent.append((action.get("target"), phase))
            return True

        task = MacroTask(
            _preset("root", [{
                "action_id": "first", "type": "键盘点击",
                "target": "A", "hold_ms": 1,
            }]),
            _Engine(), _Signals(), send_output=send,
            is_active=lambda: True,
            debug_state=harness.macro_controller._debug_snapshot,
        )
        harness.macro_controller.tasks["root"] = task
        task.start()
        try:
            self.assertTrue(_wait_until(lambda: bool(task.debug_pause_info)))
            harness.runtime_debug_dialog.close()
            self.app.processEvents()
            self.assertFalse(harness.runtime_debug_enabled)
            self.assertFalse(
                harness.macro_controller._debug_snapshot()["enabled"]
            )
            self.assertTrue(task.wait_for_exit(timeout=1.5))
            self.assertEqual(sent, [("A", "Press"), ("A", "Release")])
        finally:
            task.stop()
            task.wait_for_exit(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
