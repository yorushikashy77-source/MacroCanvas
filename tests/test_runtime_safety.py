import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys


MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
sys.path.insert(0, str(MAIN_PATH.parent))

from config.schema import validate_config_payload
from config.storage import atomic_write_text
from macro.scheduler import MacroTask


class _Signal:
    def emit(self, *_args, **_kwargs):
        pass


class _Signals:
    progress = _Signal()
    action_activity = _Signal()
    task_finished = _Signal()
    state_changed = _Signal()


class _Engine:
    def is_running(self):
        return True


class RuntimeSafetyTests(unittest.TestCase):
    def make_task(self, sent=None):
        sent = sent if sent is not None else []

        def send(action, phase, **_kwargs):
            sent.append((action.get("target"), phase))
            return True

        task = MacroTask(
            {"id": "test", "name": "test", "actions": []},
            _Engine(), _Signals(), send_output=send, is_active=lambda: True,
        )
        task.started_at = time.perf_counter()
        return task, sent

    def test_pause_does_not_consume_sleep_time(self):
        task, _ = self.make_task()
        result = []
        worker = threading.Thread(target=lambda: result.append(task.sleep(180)))
        started = time.perf_counter()
        worker.start()
        time.sleep(0.05)
        task.pause()
        time.sleep(0.16)
        self.assertTrue(worker.is_alive())
        task.resume()
        worker.join(1.0)
        self.assertEqual(result, [True])
        self.assertGreaterEqual(time.perf_counter() - started, 0.30)

    def test_pause_releases_and_resume_represses_held_action(self):
        task, sent = self.make_task()
        action = {"type": "键盘点击", "target": "Alt"}
        self.assertTrue(task.press(action))
        task.pause()
        self.assertEqual(sent[-1], ("Alt", "Release"))
        task.resume()
        self.assertEqual(sent[-1], ("Alt", "Press"))
        task.force_release()
        self.assertEqual(sent[-1], ("Alt", "Release"))

    def test_pause_does_not_consume_runtime_deadline(self):
        task, _ = self.make_task()
        task.deadline = 0.12
        time.sleep(0.03)
        task.pause()
        time.sleep(0.15)
        task.resume()
        self.assertTrue(task.wait_ready())
        time.sleep(0.10)
        self.assertFalse(task.wait_ready())

    def test_parallel_worker_limit_is_bounded(self):
        task, _ = self.make_task()
        self.assertEqual(task.MAX_PARALLEL_WORKERS, 16)
        acquired = 0
        while task.parallel_slots.acquire(blocking=False):
            acquired += 1
        self.assertEqual(acquired, task.MAX_PARALLEL_WORKERS)
        for _ in range(acquired):
            task.parallel_slots.release()

    def test_saturated_parallel_limit_keeps_correct_completion_count(self):
        task, _ = self.make_task()
        acquired = 0
        while task.parallel_slots.acquire(blocking=False):
            acquired += 1
        try:
            actions = [
                {"type": "鼠标移动", "target": f"{index},{index}"}
                for index in range(3)
            ]
            self.assertTrue(task._run_action_sequence(
                actions, 100, timeline_mode="parallel"
            ))
        finally:
            for _ in range(acquired):
                task.parallel_slots.release()

    def test_atomic_write_never_leaves_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            atomic_write_text(path, '{"mappings": [], "presets": []}')
            self.assertEqual(json.loads(path.read_text("utf-8"))["presets"], [])
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_invalid_action_tree_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_config_payload({
                "mappings": [],
                "presets": [{"actions": [{"children": "invalid"}]}],
            })


if __name__ == "__main__":
    unittest.main()
