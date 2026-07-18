import threading
import time
import unittest

from PySide6.QtWidgets import QApplication

from config.schema import validate_config_payload
from core.constants import (
    GLOBAL_TOGGLE_KEYS, SOURCE_NAMES, SYSTEM_HOTKEY_KEYS, TRIGGER_NAMES,
)
from engine.kanata import KanataConfigBuilder
from macro.scheduler import MacroTask
from macro.simulation import simulate_preset
from ui.editors import ActionDurationEditor, ActionTargetEditor, HotkeyEdit


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


def _preset(preset_id, actions, enabled=True):
    return {
        "id": preset_id,
        "enabled": enabled,
        "name": preset_id,
        "trigger_modifiers": "无",
        "trigger": "F1" if preset_id == "root" else "F2",
        "execution_mode": "执行一次",
        "actions": actions,
    }


class Phase3SchemaTests(unittest.TestCase):
    def test_valid_submacro_and_conditions(self):
        payload = {
            "presets": [
                _preset("child", [
                    {"action_id": "c1", "type": "等待", "wait_ms": 10},
                ]),
                _preset("root", [
                    {
                        "action_id": "r1", "type": "条件分支",
                        "condition_input": "鼠标左键", "condition_state": "按住时",
                        "children": [{
                            "action_id": "r2", "type": "调用子宏",
                            "preset_id": "child", "repeat_count": 2,
                            "speed_percent": 150,
                        }],
                    },
                    {
                        "action_id": "r3", "type": "等待条件",
                        "condition_input": "A", "condition_state": "松开时",
                        "timeout_ms": 500, "poll_ms": 20,
                    },
                ]),
            ],
        }
        self.assertIs(validate_config_payload(payload), payload)

    def test_missing_target_and_call_cycle_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "不存在"):
            validate_config_payload({
                "presets": [_preset("root", [{
                    "action_id": "r1", "type": "调用子宏",
                    "preset_id": "missing",
                }])],
            })
        with self.assertRaisesRegex(ValueError, "循环"):
            validate_config_payload({
                "presets": [
                    _preset("root", [{
                        "action_id": "r1", "type": "调用子宏",
                        "preset_id": "child",
                    }]),
                    _preset("child", [{
                        "action_id": "c1", "type": "调用子宏",
                        "preset_id": "root",
                    }]),
                ],
            })


class Phase3RuntimeTests(unittest.TestCase):
    def make_task(self, preset, condition=lambda *_args: False):
        sent = []

        def send(action, phase, **_kwargs):
            sent.append((action.get("target"), phase))
            return True

        task = MacroTask(
            preset, _Engine(), _Signals(), send_output=send,
            is_active=lambda: True, condition_state=condition,
        )
        task.started_at = time.perf_counter()
        return task, sent

    def test_condition_branch_skips_or_executes_children(self):
        action = {
            "type": "条件分支", "condition_input": "A",
            "condition_state": "按住时",
            "children": [{
                "type": "键盘点击", "target": "B", "hold_ms": 1,
            }],
        }
        task, sent = self.make_task({"id": "root", "name": "root"})
        self.assertTrue(task.run_action_group(action, 100))
        self.assertEqual(sent, [])
        task, sent = self.make_task(
            {"id": "root", "name": "root"}, lambda *_args: True
        )
        self.assertTrue(task.run_action_group(action, 100))
        self.assertEqual(sent, [("B", "Press"), ("B", "Release")])

    def test_wait_condition_unblocks_and_times_out(self):
        held = threading.Event()
        task, _sent = self.make_task(
            {"id": "root", "name": "root"},
            lambda *_args: held.is_set(),
        )
        action = {
            "type": "等待条件", "condition_input": "A",
            "condition_state": "按住时", "timeout_ms": 300, "poll_ms": 10,
        }
        threading.Timer(0.03, held.set).start()
        self.assertTrue(task.run_action_group(action, 100))
        task, _sent = self.make_task({"id": "root", "name": "root"})
        action["timeout_ms"] = 25
        self.assertFalse(task.run_action_group(action, 100))

    def test_submacro_repeat_and_speed_execute_without_new_task(self):
        child = _preset("child", [{
            "action_id": "c1", "type": "键盘点击",
            "target": "C", "hold_ms": 1,
        }])
        root = _preset("root", [])
        library = {"root": root, "child": child}
        root["_preset_library"] = library
        child["_preset_library"] = library
        task, sent = self.make_task(root)
        self.assertTrue(task.run_action_group({
            "type": "调用子宏", "preset_id": "child",
            "repeat_count": 2, "speed_percent": 200,
        }, 100))
        self.assertEqual(sent.count(("C", "Press")), 2)
        self.assertEqual(sent.count(("C", "Release")), 2)


class Phase3SimulationTests(unittest.TestCase):
    def test_submacro_and_condition_ranges_are_conservative(self):
        child = _preset("child", [{
            "action_id": "c1", "type": "等待", "wait_ms": 100,
        }])
        root = _preset("root", [{
            "action_id": "r1", "type": "调用子宏",
            "preset_id": "child", "repeat_count": 2, "speed_percent": 200,
        }, {
            "action_id": "r2", "type": "条件分支",
            "condition_input": "A", "condition_state": "按住时",
            "children": [{
                "action_id": "r3", "type": "等待", "wait_ms": 30,
            }],
        }])
        library = {"root": root, "child": child}
        root["_preset_library"] = library
        report = simulate_preset(root)
        self.assertEqual(report["one_cycle_min_ms"], 100)
        self.assertEqual(report["one_cycle_max_ms"], 130)
        self.assertTrue(any("实时输入状态" in item for item in report["warnings"]))


class Phase3EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_control_action_editors_preserve_ids_and_parameters(self):
        target = ActionTargetEditor(
            "调用子宏", "child",
            preset_options=[("child", "公共子宏"), ("other", "其他")],
        )
        self.assertEqual(target.currentText(), "child")
        target.set_submacro_options(
            [("child", "已改名的子宏")], "child", emit=False
        )
        self.assertEqual(target.currentText(), "child")

        duration = ActionDurationEditor("调用子宏", 3)
        duration.setCallSpeedValue(175)
        self.assertEqual(duration.value(), 3)
        self.assertEqual(duration.callSpeedValue(), 175)
        duration.set_action_type("等待条件", 0, emit=False)
        duration.setConditionState("松开时")
        self.assertEqual(duration.value(), 0)
        self.assertEqual(duration.conditionState(), "松开时")

    def test_escape_is_manual_source_option_but_not_system_hotkey(self):
        self.assertIn("Esc", SOURCE_NAMES)
        self.assertIn("Esc", TRIGGER_NAMES)
        self.assertNotIn("Esc", GLOBAL_TOGGLE_KEYS)
        self.assertNotIn("Esc", SYSTEM_HOTKEY_KEYS)

        source = HotkeyEdit("无", "F6", SOURCE_NAMES)
        self.assertIn("Esc", source.options)
        source.setCurrentText("Esc")
        self.assertEqual(source.currentText(), "Esc")
        source.setCurrentText("F6")
        source.capturing = True
        source.handle_global_input("Esc", True)
        self.assertEqual(source.currentText(), "F6")
        self.assertFalse(source.capturing)

    def test_control_actions_never_generate_kanata_outputs(self):
        for action_type in ("调用子宏", "条件分支", "等待条件"):
            self.assertIsNone(KanataConfigBuilder.action_output({
                "type": action_type,
            }))

    def test_condition_input_uses_capture_first_hotkey_editor(self):
        target = ActionTargetEditor("条件分支", "Esc")
        self.assertIsInstance(target.condition_editor, HotkeyEdit)
        self.assertIn("Esc", target.condition_editor.options)
        self.assertEqual(target.currentText(), "Esc")

        target.condition_editor.capturing = True
        target.condition_editor.handle_global_input("C", True)
        self.assertEqual(target.currentText(), "C")

        target.condition_editor.capturing = True
        target.condition_editor.handle_global_input("Esc", True)
        self.assertEqual(target.currentText(), "C")


if __name__ == "__main__":
    unittest.main()
