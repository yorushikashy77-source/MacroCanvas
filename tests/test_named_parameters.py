import time
import unittest

from config.schema import validate_config_payload
from macro.parameters import resolve_action_parameters
from macro.scheduler import MacroTask
from macro.simulation import simulate_preset
from ui.main_window import MainWindow


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


def _preset(preset_id, actions, parameters=None):
    result = {
        "id": preset_id,
        "enabled": False,
        "name": preset_id,
        "trigger_modifiers": "无",
        "trigger": "F1",
        "execution_mode": "执行一次",
        "actions": actions,
    }
    if parameters is not None:
        result["parameters"] = parameters
    return result


class NamedParameterSchemaTests(unittest.TestCase):
    def test_old_presets_without_parameters_remain_valid(self):
        payload = {"presets": [_preset("root", [
            {"action_id": "a", "type": "等待", "wait_ms": 10},
        ])]}
        self.assertIs(validate_config_payload(payload), payload)

    def test_bindings_and_submacro_overrides_are_valid(self):
        child = _preset("child", [{
            "action_id": "key", "type": "键盘点击", "target": "A",
            "hold_ms": 10,
            "parameter_bindings": {"target": "目标键", "hold_ms": "时长"},
        }], [
            {"name": "目标键", "type": "按键", "default": "A"},
            {"name": "时长", "type": "时长", "default": 10},
        ])
        root = _preset("root", [{
            "action_id": "call", "type": "调用子宏", "preset_id": "child",
            "repeat_count": 1, "speed_percent": 100,
            "parameter_values": {"目标键": "B", "时长": 25},
        }])
        payload = {"presets": [root, child]}
        self.assertIs(validate_config_payload(payload), payload)

    def test_invalid_binding_and_unknown_override_are_rejected(self):
        child = _preset("child", [{
            "action_id": "key", "type": "键盘点击", "target": "A",
            "hold_ms": 10, "parameter_bindings": {"hold_ms": "目标键"},
        }], [{"name": "目标键", "type": "按键", "default": "A"}])
        with self.assertRaisesRegex(ValueError, "变量类型必须是 时长"):
            validate_config_payload({"presets": [child]})

        child["actions"][0]["parameter_bindings"] = {"target": "目标键"}
        root = _preset("root", [{
            "action_id": "call", "type": "调用子宏", "preset_id": "child",
            "parameter_values": {"不存在": "B"},
        }])
        with self.assertRaisesRegex(ValueError, "不是目标预设声明的变量"):
            validate_config_payload({"presets": [root, child]})

    def test_duplicate_names_and_field_incompatible_defaults_are_rejected(self):
        duplicate = _preset("root", [], [
            {"name": "次数", "type": "整数", "default": 1},
            {"name": "次数", "type": "整数", "default": 2},
        ])
        with self.assertRaisesRegex(ValueError, "与其他变量重复"):
            validate_config_payload({"presets": [duplicate]})

        mouse = _preset("mouse", [{
            "action_id": "click", "type": "鼠标点击",
            "target": "鼠标左键", "hold_ms": 10,
            "parameter_bindings": {"target": "目标键"},
        }], [{"name": "目标键", "type": "按键", "default": "Esc"}])
        with self.assertRaisesRegex(ValueError, "默认按键不适用于"):
            validate_config_payload({"presets": [mouse]})

    def test_override_must_remain_valid_for_every_bound_target_field(self):
        child = _preset("child", [{
            "action_id": "wheel", "type": "鼠标滚轮", "target": "向上",
            "steps": 5, "parameter_bindings": {"steps": "格数"},
        }], [{"name": "格数", "type": "整数", "default": 5}])
        root = _preset("root", [{
            "action_id": "call", "type": "调用子宏", "preset_id": "child",
            "parameter_values": {"格数": 101},
        }])
        with self.assertRaisesRegex(ValueError, "不能大于 100"):
            validate_config_payload({"presets": [root, child]})


class NamedParameterRuntimeTests(unittest.TestCase):
    def test_runtime_mapping_rule_carries_an_isolated_definition_copy(self):
        preset = _preset("root", [], [
            {"name": "延迟", "type": "时长", "default": 50},
        ])
        rule = MainWindow._preset_as_mapping_rule(preset)
        self.assertEqual(rule["parameters"], preset["parameters"])
        rule["parameters"][0]["default"] = 99
        self.assertEqual(preset["parameters"][0]["default"], 50)

    def test_defaults_resolve_without_mutating_source_actions(self):
        preset = _preset("root", [{
            "action_id": "wait", "type": "等待", "wait_ms": 20,
            "parameter_bindings": {"wait_ms": "延迟"},
        }], [{"name": "延迟", "type": "时长", "default": 75}])
        resolved = resolve_action_parameters(preset["actions"], preset)
        self.assertEqual(resolved[0]["wait_ms"], 75)
        self.assertEqual(preset["actions"][0]["wait_ms"], 20)

    def test_submacro_call_override_changes_output_key(self):
        child = _preset("child", [{
            "action_id": "key", "type": "键盘点击", "target": "A",
            "hold_ms": 1, "parameter_bindings": {"target": "目标键"},
        }], [{"name": "目标键", "type": "按键", "default": "A"}])
        root = _preset("root", [])
        library = {"root": root, "child": child}
        root["_preset_library"] = library
        child["_preset_library"] = library
        sent = []

        def send(action, phase, **_kwargs):
            sent.append((action.get("target"), phase))
            return True

        task = MacroTask(
            root, _Engine(), _Signals(), send_output=send,
            is_active=lambda: True,
        )
        task.started_at = time.perf_counter()
        self.assertTrue(task.run_action_group({
            "type": "调用子宏", "preset_id": "child",
            "repeat_count": 1, "speed_percent": 100,
            "parameter_values": {"目标键": "B"},
        }, 100))
        self.assertEqual(sent, [("B", "Press"), ("B", "Release")])

    def test_simulation_uses_duration_override(self):
        child = _preset("child", [{
            "action_id": "wait", "type": "等待", "wait_ms": 10,
            "parameter_bindings": {"wait_ms": "延迟"},
        }], [{"name": "延迟", "type": "时长", "default": 40}])
        root = _preset("root", [{
            "action_id": "call", "type": "调用子宏", "preset_id": "child",
            "repeat_count": 2, "speed_percent": 100,
            "parameter_values": {"延迟": 75},
        }])
        library = {"root": root, "child": child}
        root["_preset_library"] = library
        child["_preset_library"] = library
        report = simulate_preset(root)
        self.assertEqual(report["one_cycle_min_ms"], 150)
        self.assertEqual(report["one_cycle_max_ms"], 150)


if __name__ == "__main__":
    unittest.main()
