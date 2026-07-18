import threading
import time
import unittest

from macro.scheduler import MacroController, MacroTask
from ui.input_runtime import InputRuntimeMixin


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


class TriggerReleaseBarrierTests(unittest.TestCase):
    def test_first_output_waits_for_source_and_modifier_release(self):
        held = {"Ctrl", "Space"}
        held_lock = threading.RLock()
        sent = []

        def condition(name, state):
            with held_lock:
                down = name in held
            return down if state == "按住时" else not down

        def send(action, phase, **_kwargs):
            sent.append((action.get("target"), phase))
            return True

        task = MacroTask(
            {
                "id": "release-fence", "name": "release-fence",
                "execution_mode": "执行一次",
                "_trigger_release_inputs": ["Space", "Ctrl"],
                "actions": [{
                    "type": "键盘点击", "target": "A", "hold_ms": 1,
                }],
            },
            _Engine(), _Signals(), send_output=send,
            is_active=lambda: True, condition_state=condition,
        )
        task.start()
        time.sleep(0.04)
        self.assertEqual(sent, [])

        with held_lock:
            held.discard("Ctrl")
        time.sleep(0.03)
        self.assertEqual(sent, [])

        with held_lock:
            held.discard("Space")
        self.assertTrue(task.wait_for_exit(timeout=1.0))
        self.assertEqual(sent, [("A", "Press"), ("A", "Release")])

    def test_controller_restart_replaces_waiting_task(self):
        sent = []

        def send(action, phase, **_kwargs):
            sent.append((action.get("target"), phase))
            return True

        controller = MacroController(
            _Engine(), send_output=send, is_active=lambda: True,
            condition_state=lambda *_args: False,
        )
        waiting = {
            "id": "preset", "name": "waiting",
            "execution_mode": "执行一次",
            "actions": [{
                "type": "等待条件", "condition_input": "Space",
                "condition_state": "按住时", "timeout_ms": 0,
                "poll_ms": 20, "children": [],
            }],
        }
        replacement = {
            "id": "preset", "name": "replacement",
            "execution_mode": "执行一次",
            "actions": [{
                "type": "键盘点击", "target": "A", "hold_ms": 1,
            }],
        }
        self.assertTrue(controller.start(waiting))
        time.sleep(0.04)
        old_task = controller.tasks["preset"]

        self.assertTrue(controller.restart(replacement))
        self.assertIsNot(controller.tasks["preset"], old_task)
        controller.tasks["preset"].wait_for_exit(timeout=1.0)
        self.assertEqual(sent, [("A", "Press"), ("A", "Release")])

    def test_controller_restart_refuses_new_output_after_release_failure(self):
        class UnsafeTask:
            release_cleanup_failed = False

            def __init__(self):
                self.live = True

            def has_live_threads(self):
                return self.live

            def stop(self):
                self.live = False
                return True

            @staticmethod
            def force_release():
                return False

            @staticmethod
            def wait_for_exit(timeout=0):
                del timeout
                return True

        controller = MacroController(_Engine(), is_active=lambda: True)
        unsafe = UnsafeTask()
        controller.tasks["preset"] = unsafe

        self.assertFalse(controller.restart({
            "id": "preset", "name": "replacement", "actions": [],
        }))
        self.assertIs(controller.tasks["preset"], unsafe)
        self.assertEqual(controller.last_release_failures, ["preset"])


class _CaptureController:
    def __init__(self, running=False):
        self.running = bool(running)
        self.started = []
        self.restarted = []

    def is_running(self, _task_id):
        return self.running

    def start(self, task):
        self.started.append(dict(task))
        return True

    def restart(self, task):
        self.restarted.append(dict(task))
        return True


class _RuntimeHarness(InputRuntimeMixin):
    def __init__(self, running=False):
        self.input_state_lock = threading.RLock()
        self.physical_modifiers = {"Ctrl"}
        self.suspended_preset_ids = set()
        self.suspended_mapping_ids = set()
        self.active_profile_id = ""
        self.held_trigger_ids = {}
        self.macro_controller = _CaptureController(running=running)
        self.diagnostics = []
        self.captured_task = None

    @staticmethod
    def _macro_backend_active():
        return True

    @staticmethod
    def _runtime_cleanup_blocks_new_output():
        return False

    @staticmethod
    def _recorded_mouse_context_issue(_actions):
        return None

    def write_diagnostic(self, event, **fields):
        self.diagnostics.append((event, fields))


class TriggerRuntimeDispatchTests(unittest.TestCase):
    @staticmethod
    def rule():
        return {
            "id": "preset", "name": "preset", "enabled": True,
            "_runtime_kind": "preset", "mode": "执行一次",
            "source": "Space", "source_modifiers": "Ctrl",
            "condition_enabled": False, "actions": [],
        }

    def test_dispatch_passes_source_and_modifiers_to_release_barrier(self):
        harness = _RuntimeHarness()
        self.assertTrue(harness._dispatch_runtime_mapping_rule(
            self.rule(), "source-token", True, False, "Space",
        ))
        task = harness.macro_controller.started[0]
        self.assertEqual(
            set(task["_trigger_release_inputs"]), {"Ctrl", "Space"}
        )

    def test_running_one_shot_uses_restart_instead_of_rejected_start(self):
        harness = _RuntimeHarness(running=True)
        task = harness.mapping_to_task(self.rule())
        self.assertTrue(harness.handle_trigger_task(
            task, "source-token", True, False,
        ))
        self.assertEqual(harness.macro_controller.started, [])
        self.assertEqual(len(harness.macro_controller.restarted), 1)
        self.assertTrue(any(
            event == "trigger_task_restart" and fields.get("started")
            for event, fields in harness.diagnostics
        ))


if __name__ == "__main__":
    unittest.main()
