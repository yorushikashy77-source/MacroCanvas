import threading
import time
import unittest
from unittest.mock import patch

from config.schema import (
    repair_overlapping_loop_controls, validate_config_payload,
)
from core.constants import ACTION_ID_ROLE, LOOP_DATA_ROLE
from macro.scheduler import MacroController, MacroTask
from ui.editor_workflow import EditorWorkflowMixin


class _Signal:
    def emit(self, *_args, **_kwargs):
        pass


class _Signals:
    progress = _Signal()
    action_activity = _Signal()
    task_finished = _Signal()
    state_changed = _Signal()


class _Engine:
    @staticmethod
    def is_running():
        return True


class MacroOutputBarrierTests(unittest.TestCase):
    @staticmethod
    def make_task(send):
        task = MacroTask(
            {"id": "task", "name": "任务", "actions": []},
            _Engine(),
            _Signals(),
            send_output=send,
            is_active=lambda: True,
        )
        task.started_at = time.perf_counter()
        return task

    def test_worker_created_before_pause_cannot_press_during_pause(self):
        sent = []
        task = self.make_task(
            lambda _action, phase, **_kwargs: sent.append(phase) or True
        )
        start_press = threading.Event()
        result = []
        worker = threading.Thread(
            target=lambda: (
                start_press.wait(),
                result.append(task.press({"type": "键盘点击", "target": "A"})),
            )
        )
        worker.start()
        self.assertTrue(task.pause())
        start_press.set()
        time.sleep(0.08)
        self.assertEqual(sent, [])
        self.assertTrue(worker.is_alive())
        self.assertTrue(task.resume())
        worker.join(1.0)
        self.assertEqual(result, [True])
        self.assertEqual(sent, ["Press"])
        self.assertTrue(task.force_release())

    def test_pause_waits_for_inflight_press_and_releases_it(self):
        sent = []
        press_entered = threading.Event()
        allow_press = threading.Event()

        def send(_action, phase, **_kwargs):
            sent.append(phase)
            if phase == "Press":
                press_entered.set()
                allow_press.wait(1.0)
            return True

        task = self.make_task(send)
        press_result = []
        press_worker = threading.Thread(
            target=lambda: press_result.append(
                task.press({"type": "键盘点击", "target": "A"})
            )
        )
        press_worker.start()
        self.assertTrue(press_entered.wait(1.0))

        pause_result = []
        pause_worker = threading.Thread(
            target=lambda: pause_result.append(task.pause())
        )
        pause_worker.start()
        time.sleep(0.05)
        allow_press.set()
        press_worker.join(1.0)
        pause_worker.join(1.0)

        self.assertEqual(press_result, [True])
        self.assertEqual(pause_result, [True])
        self.assertEqual(sent, ["Press", "Release"])
        self.assertEqual(task.pressed, {})

    def test_pause_after_stop_is_rejected(self):
        task = self.make_task(lambda *_args, **_kwargs: True)
        task.stop()
        self.assertFalse(task.pause())

    def test_stop_rejects_delayed_press(self):
        sent = []
        task = self.make_task(
            lambda _action, phase, **_kwargs: sent.append(phase) or True
        )
        start_press = threading.Event()
        result = []
        worker = threading.Thread(
            target=lambda: (
                start_press.wait(),
                result.append(task.press({"type": "键盘点击", "target": "A"})),
            )
        )
        worker.start()
        task.stop()
        start_press.set()
        worker.join(1.0)
        self.assertEqual(result, [False])
        self.assertEqual(sent, [])

    def test_pause_release_failure_is_reported_and_stops_task(self):
        sent = []

        def send(_action, phase, **_kwargs):
            sent.append(phase)
            return phase != "Release"

        task = self.make_task(send)
        self.assertTrue(task.press({"type": "键盘点击", "target": "A"}))
        self.assertFalse(task.pause())
        self.assertTrue(task.stop_event.is_set())
        self.assertFalse(task.run_event.is_set())
        self.assertTrue(task.pressed)

    def test_resume_failure_does_not_mark_task_running(self):
        sent = []
        press_count = [0]

        def send(_action, phase, **_kwargs):
            sent.append(phase)
            if phase == "Press":
                press_count[0] += 1
                return press_count[0] == 1
            return True

        task = self.make_task(send)
        self.assertTrue(task.press({"type": "键盘点击", "target": "A"}))
        self.assertTrue(task.pause())
        self.assertFalse(task.resume())
        self.assertTrue(task.stop_event.is_set())
        self.assertFalse(task.run_event.is_set())
        self.assertEqual(task.pressed, {})

    def test_release_while_paused_consumes_saved_holder(self):
        sent = []
        task = self.make_task(
            lambda _action, phase, **_kwargs: sent.append(phase) or True
        )
        action = {"type": "键盘点击", "target": "A"}
        self.assertTrue(task.press(action))
        self.assertTrue(task.pause())
        self.assertEqual(len(task.paused_actions), 1)
        self.assertTrue(task.release(action))
        self.assertEqual(task.paused_actions, [])
        self.assertTrue(task.resume())
        self.assertEqual(sent, ["Press", "Release"])


class _StoppedTask:
    def __init__(self, release_ok):
        self.preset = {"id": "stale", "name": "stale"}
        self.release_ok = release_ok

    def stop(self):
        pass

    def wait_for_exit(self, timeout=0):
        return True

    def force_release(self):
        return self.release_ok

    def has_live_threads(self):
        return False


class MacroControllerCleanupTests(unittest.TestCase):
    def test_stale_task_release_failure_is_retained_after_task_removal(self):
        controller = MacroController(_Engine())
        controller.tasks["stale"] = _StoppedTask(release_ok=False)
        self.assertEqual(controller.stop_all(), [])
        self.assertEqual(controller.last_release_failures, ["stale"])
        self.assertEqual(controller.tasks, {})


class LoopReferenceValidationTests(unittest.TestCase):
    @staticmethod
    def payload():
        return {
            "mappings": [],
            "presets": [
                {
                    "id": "preset",
                    "name": "预设",
                    "actions": [
                        {
                            "type": "键盘点击",
                            "action_id": "a",
                            "target": "A",
                            "children": [],
                        },
                        {
                            "type": "键盘点击",
                            "action_id": "b",
                            "target": "B",
                            "children": [],
                        },
                        {
                            "type": "循环动作",
                            "id": "loop-1",
                            "name": "循环项目1",
                            "target_action_ids": ["a"],
                            "children": [],
                        },
                        {
                            "type": "循环动作",
                            "id": "loop-2",
                            "name": "循环项目2",
                            "target_action_ids": ["a", "b"],
                            "children": [],
                        },
                    ],
                }
            ],
        }

    def test_overlapping_loop_ranges_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "引用范围重叠"):
            validate_config_payload(self.payload())

    def test_startup_repair_removes_only_later_overlapping_loop(self):
        repaired, removed = repair_overlapping_loop_controls(self.payload())
        validated = validate_config_payload(repaired)
        actions = validated["presets"][0]["actions"]
        self.assertEqual(
            [item.get("id") for item in actions if item.get("type") == "循环动作"],
            ["loop-1"],
        )
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["loop"], "循环项目2")

    def test_startup_repair_covers_profile_payloads(self):
        payload = {
            "mappings": [],
            "presets": [],
            "profiles": [{
                "id": "profile",
                "name": "档案",
                "process_names": ["game.exe"],
                "payload": self.payload(),
            }],
        }
        repaired, removed = repair_overlapping_loop_controls(payload)
        validate_config_payload(repaired)
        profile_actions = repaired["profiles"][0]["payload"]["presets"][0]["actions"]
        self.assertEqual(
            [item.get("id") for item in profile_actions if item.get("type") == "循环动作"],
            ["loop-1"],
        )
        self.assertEqual(len(removed), 1)


class _FakeItem:
    def __init__(self, action_id=None, loop_data=None, children=None):
        self._parent = None
        self._children = list(children or [])
        for child in self._children:
            child._parent = self
        self._data = {}
        if action_id is not None:
            self._data[ACTION_ID_ROLE] = action_id
        if loop_data is not None:
            self._data[LOOP_DATA_ROLE] = dict(loop_data)

    def parent(self):
        return self._parent

    def childCount(self):
        return len(self._children)

    def child(self, index):
        return self._children[index]

    def removeChild(self, item):
        self._children.remove(item)
        item._parent = None

    def data(self, _column, role):
        return self._data.get(role)

    def setData(self, _column, role, value):
        self._data[role] = value


class _FakeTable:
    def __init__(self, items):
        self.items = list(items)

    def topLevelItemCount(self):
        return len(self.items)

    def topLevelItem(self, index):
        return self.items[index]

    def indexOfTopLevelItem(self, item):
        try:
            return self.items.index(item)
        except ValueError:
            return -1

    def takeTopLevelItem(self, index):
        return self.items.pop(index)

    def iter_items(self):
        def walk(item):
            yield item
            for child_index in range(item.childCount()):
                yield from walk(item.child(child_index))

        for item in list(self.items):
            yield from walk(item)


class _FakeCard:
    def __init__(self, items):
        self.action_table = _FakeTable(items)
        self.action_dialog = None


class _EditorHarness(EditorWorkflowMixin):
    @staticmethod
    def is_loop_action_item(item):
        return bool(item and item.data(0, LOOP_DATA_ROLE) is not None)

    @staticmethod
    def update_loop_action_summary(_card, _item):
        pass


class LoopReferenceEditorRepairTests(unittest.TestCase):
    def test_reordered_contiguous_range_updates_reference_order(self):
        loop = _FakeItem(loop_data={
            "name": "循环项目1",
            "target_action_ids": ["a", "b"],
        })
        card = _FakeCard([_FakeItem("b"), _FakeItem("a"), loop])
        _EditorHarness().synchronize_loop_references(card)
        self.assertEqual(
            loop.data(0, LOOP_DATA_ROLE)["target_action_ids"],
            ["b", "a"],
        )

    def test_cross_level_reference_removes_invalid_loop_immediately(self):
        parent = _FakeItem("a", children=[_FakeItem("b")])
        loop = _FakeItem(loop_data={
            "name": "循环项目1",
            "target_action_ids": ["a", "b"],
        })
        card = _FakeCard([parent, loop])
        with patch("ui.editor_workflow.QMessageBox.information") as information:
            _EditorHarness().synchronize_loop_references(card)
        self.assertNotIn(loop, card.action_table.items)
        information.assert_called_once()

    def test_later_overlapping_loop_is_removed(self):
        first = _FakeItem(loop_data={
            "name": "循环项目1",
            "target_action_ids": ["a"],
        })
        second = _FakeItem(loop_data={
            "name": "循环项目2",
            "target_action_ids": ["a"],
        })
        card = _FakeCard([_FakeItem("a"), first, second])
        with patch("ui.editor_workflow.QMessageBox.information"):
            _EditorHarness().synchronize_loop_references(card)
        self.assertIn(first, card.action_table.items)
        self.assertNotIn(second, card.action_table.items)


if __name__ == "__main__":
    unittest.main()
