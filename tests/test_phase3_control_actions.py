import threading
import time
import unittest
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from config.profiles import profile_summary
from config.schema import validate_config_payload
from core.constants import (
    GLOBAL_TOGGLE_KEYS, SOURCE_NAMES, SYSTEM_HOTKEY_KEYS, TRIGGER_NAMES,
)
from engine.kanata import KanataConfigBuilder
from macro.scheduler import MacroTask
from macro.recording import simplify_recorded_actions
from macro.simulation import simulate_preset
from ui.editors import ActionDurationEditor, ActionTargetEditor, HotkeyEdit
from ui.editors import ActionTreeWidget
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
    def test_explicit_true_and_else_branches_are_validated(self):
        condition = {
            "action_id": "condition",
            "type": "条件分支",
            "condition_input": "A",
            "condition_state": "按住时",
            "children": [
                {
                    "action_id": "true-branch",
                    "type": "条件成立分支",
                    "children": [{
                        "action_id": "true-action", "type": "等待",
                        "wait_ms": 10,
                    }],
                },
                {
                    "action_id": "else-branch",
                    "type": "否则分支",
                    "children": [{
                        "action_id": "else-action", "type": "等待",
                        "wait_ms": 20,
                    }],
                },
            ],
        }
        payload = {"presets": [_preset("root", [condition])]}
        self.assertIs(validate_config_payload(payload), payload)

        missing_else = {"presets": [_preset(
            "root", [dict(condition, children=condition["children"][:1])]
        )]}
        with self.assertRaisesRegex(ValueError, "必须各包含"):
            validate_config_payload(missing_else)

        top_level_branch = {"presets": [_preset("root", [
            condition["children"][0]
        ])]}
        with self.assertRaisesRegex(ValueError, "直接子项"):
            validate_config_payload(top_level_branch)

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

    def test_explicit_else_branch_executes_only_selected_path(self):
        action = {
            "type": "条件分支", "condition_input": "A",
            "condition_state": "按住时",
            "children": [
                {"type": "条件成立分支", "children": [{
                    "type": "键盘点击", "target": "B", "hold_ms": 1,
                }]},
                {"type": "否则分支", "children": [{
                    "type": "键盘点击", "target": "C", "hold_ms": 1,
                }]},
            ],
        }
        task, sent = self.make_task({"id": "root", "name": "root"})
        self.assertTrue(task.run_action_group(action, 100))
        self.assertEqual(sent, [("C", "Press"), ("C", "Release")])

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

    def test_explicit_branch_preview_uses_shorter_and_longer_paths(self):
        report = simulate_preset(_preset("root", [{
            "action_id": "condition", "type": "条件分支",
            "condition_input": "A", "condition_state": "按住时",
            "children": [
                {"action_id": "true", "type": "条件成立分支", "children": [
                    {"action_id": "t", "type": "等待", "wait_ms": 20},
                ]},
                {"action_id": "else", "type": "否则分支", "children": [
                    {"action_id": "e", "type": "等待", "wait_ms": 40},
                ]},
            ],
        }]))
        self.assertEqual(report["one_cycle_min_ms"], 20)
        self.assertEqual(report["one_cycle_max_ms"], 40)

    def test_branch_containers_are_not_counted_as_actions_or_outputs(self):
        condition = {
            "type": "条件分支", "condition_input": "A",
            "condition_state": "按住时", "children": [
                {"type": "条件成立分支", "children": [
                    {"type": "等待", "wait_ms": 20},
                ]},
                {"type": "否则分支", "children": [
                    {"type": "键盘点击", "target": "B", "hold_ms": 20},
                ]},
            ],
        }
        summary = profile_summary({
            "payload": {"mappings": [], "presets": [
                _preset("root", [condition], enabled=False),
            ]},
        })
        self.assertEqual(summary["actions"], 3)
        self.assertEqual(summary["virtual_keys"], 1)

    def test_action_cleanup_does_not_add_timing_to_branch_controls(self):
        actions = [{
            "type": "条件分支", "children": [
                {"type": "条件成立分支", "children": [
                    {"type": "等待", "wait_ms": 20},
                ]},
                {"type": "否则分支", "children": []},
            ],
        }]
        cleaned = simplify_recorded_actions(
            actions, trim_edge_waits=False, adjust_timing=True,
        )
        self.assertNotIn("hold_ms", cleaned[0])
        self.assertNotIn("hold_ms", cleaned[0]["children"][0])
        self.assertNotIn("hold_ms", cleaned[0]["children"][1])


class _EditorHarness(EditorWorkflowMixin):
    def __init__(self):
        self.selected_preset_card = None
        self.preset_cards = []
        self.loading_task_stack = []
        self.initializing = False

    def select_preset_card(self, card):
        self.selected_preset_card = card

    def update_card_action_summary(self, _card):
        pass

    def _loading_checkpoint(self, *_args, **_kwargs):
        pass

    def action_changed(self, _card=None):
        pass


def _action_card(preset_id="root", parameters=None):
    table = ActionTreeWidget()
    table.setColumnCount(5)
    return SimpleNamespace(
        preset_id=preset_id,
        name=QLineEdit(preset_id),
        parameter_definitions=list(parameters or []),
        action_table=table,
        action_title=QLabel(),
        loop_points_button=QPushButton(),
        _actions_loaded=True,
    )


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

    def test_named_bindings_and_call_overrides_survive_row_rebuild(self):
        harness = _EditorHarness()
        child = _action_card("child", [{
            "name": "目标键", "type": "按键", "default": "A",
        }])
        root = _action_card("root", [{
            "name": "延迟", "type": "时长", "default": 25,
        }])
        harness.preset_cards.extend([root, child])

        wait_item = harness.add_action({
            "action_id": "wait", "type": "等待", "wait_ms": 10,
            "parameter_bindings": {"wait_ms": "延迟"},
        }, save=False, card=root)
        rebuilt_wait = harness.action_from_item(root.action_table, wait_item)
        self.assertEqual(
            rebuilt_wait["parameter_bindings"], {"wait_ms": "延迟"}
        )

        call_item = harness.add_action({
            "action_id": "call", "type": "调用子宏",
            "preset_id": "child", "repeat_count": 1,
            "speed_percent": 100,
            "parameter_values": {"目标键": "B"},
        }, save=False, card=root)
        rebuilt_call = harness.action_from_item(root.action_table, call_item)
        self.assertEqual(rebuilt_call["parameter_values"], {"目标键": "B"})

    def test_legacy_condition_is_rendered_with_two_fixed_branches(self):
        harness = _EditorHarness()
        card = _action_card()
        harness.preset_cards.append(card)
        item = harness.add_action({
            "action_id": "condition",
            "type": "条件分支",
            "condition_input": "A",
            "condition_state": "按住时",
            "children": [{
                "action_id": "legacy", "type": "等待", "wait_ms": 10,
            }],
        }, save=False, card=card)

        self.assertEqual(item.childCount(), 2)
        true_branch, else_branch = item.child(0), item.child(1)
        self.assertTrue(harness.is_condition_branch_item(true_branch))
        self.assertTrue(harness.is_condition_branch_item(else_branch))
        self.assertEqual(true_branch.childCount(), 1)
        self.assertEqual(else_branch.childCount(), 0)
        self.assertFalse(
            bool(true_branch.flags() & Qt.ItemFlag.ItemIsDragEnabled)
        )
        self.assertIsInstance(card.action_table.itemWidget(true_branch, 4), QLabel)

        # Opening the action dialog refreshes every submacro target. Fixed
        # branch rows use labels instead of action-type combos and must be
        # ignored by that refresh pass.
        harness.refresh_submacro_target_editors(card)

        harness.add_action({
            "action_id": "else-wait", "type": "等待", "wait_ms": 25,
        }, save=False, card=card, parent_item=else_branch)
        rebuilt = harness.action_from_item(card.action_table, item)
        self.assertEqual(
            [child["type"] for child in rebuilt["children"]],
            ["条件成立分支", "否则分支"],
        )
        self.assertEqual(len(rebuilt["children"][1]["children"]), 1)

    def test_changing_action_type_wraps_and_unwraps_existing_children(self):
        harness = _EditorHarness()
        card = _action_card()
        harness.preset_cards.append(card)
        item = harness.add_action({
            "action_id": "parent", "type": "键盘点击", "target": "A",
            "hold_ms": 10, "children": [{
                "action_id": "child", "type": "等待", "wait_ms": 15,
            }],
        }, save=False, card=card)

        card.action_table.itemWidget(item, 1).setCurrentText("条件分支")
        QApplication.processEvents()
        condition_item = card.action_table.topLevelItem(0)
        self.assertEqual(condition_item.childCount(), 2)
        self.assertEqual(condition_item.child(0).childCount(), 1)
        self.assertEqual(
            harness.action_from_item(card.action_table, condition_item)
            ["children"][0]["children"][0]["action_id"],
            "child",
        )

        card.action_table.itemWidget(condition_item, 1).setCurrentText("等待")
        QApplication.processEvents()
        wait_item = card.action_table.topLevelItem(0)
        self.assertEqual(wait_item.childCount(), 1)
        self.assertEqual(
            harness.action_from_item(card.action_table, wait_item)
            ["children"][0]["action_id"],
            "child",
        )

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
