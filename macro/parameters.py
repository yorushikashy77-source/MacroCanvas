"""Named preset parameters shared by validation, runtime and preview code."""

from __future__ import annotations

import copy

from core.constants import INPUT_NAMES, KEY_NAMES, MOUSE_NAMES


PARAMETER_INPUT = "按键"
PARAMETER_INTEGER = "整数"
PARAMETER_DURATION = "时长"
PARAMETER_TYPES = (PARAMETER_INPUT, PARAMETER_INTEGER, PARAMETER_DURATION)
MAX_PRESET_PARAMETERS = 32

# field -> (parameter type, minimum, maximum, optional allowed choices)
ACTION_PARAMETER_FIELDS = {
    "键盘点击": {
        "target": (PARAMETER_INPUT, None, None, set(KEY_NAMES)),
        "hold_ms": (PARAMETER_DURATION, 1, 600_000, None),
    },
    "鼠标点击": {
        "target": (PARAMETER_INPUT, None, None, set(MOUSE_NAMES)),
        "hold_ms": (PARAMETER_DURATION, 1, 600_000, None),
    },
    "鼠标滚轮": {
        "steps": (PARAMETER_INTEGER, 1, 100, None),
    },
    "等待": {
        "wait_ms": (PARAMETER_DURATION, 1, 600_000, None),
    },
    "条件分支": {
        "condition_input": (PARAMETER_INPUT, None, None, set(INPUT_NAMES)),
    },
    "等待条件": {
        "condition_input": (PARAMETER_INPUT, None, None, set(INPUT_NAMES)),
        "timeout_ms": (PARAMETER_DURATION, 0, 600_000, None),
    },
    "调用子宏": {
        "repeat_count": (PARAMETER_INTEGER, 1, 100_000, None),
        "speed_percent": (PARAMETER_INTEGER, 10, 500, None),
    },
    "循环动作": {
        "loop_count": (PARAMETER_INTEGER, 1, 100_000, None),
        "loop_interval_ms": (PARAMETER_DURATION, 0, 600_000, None),
        "speed_percent": (PARAMETER_INTEGER, 10, 500, None),
    },
}

FIELD_LABELS = {
    "target": "目标按键",
    "hold_ms": "按住时长",
    "steps": "滚轮格数",
    "wait_ms": "等待时长",
    "condition_input": "条件按键",
    "timeout_ms": "超时时间",
    "repeat_count": "调用次数",
    "speed_percent": "执行速度",
    "loop_count": "循环次数",
    "loop_interval_ms": "循环间隔",
}


def parameter_definitions(preset):
    """Return normalized, non-mutating definitions from one preset."""
    result = []
    for raw in preset.get("parameters", []) or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        kind = str(raw.get("type") or "")
        if name and kind in PARAMETER_TYPES:
            result.append({"name": name, "type": kind, "default": raw.get("default")})
    return result


def coerce_parameter_value(kind, value):
    """Coerce a definition/override value or raise ValueError."""
    if kind == PARAMETER_INPUT:
        text = str(value or "").strip()
        if text not in INPUT_NAMES:
            raise ValueError(f"不支持的按键：{text or '空值'}")
        return text
    if isinstance(value, bool):
        raise ValueError("必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("必须是整数") from None
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("必须是整数")
    if kind == PARAMETER_INTEGER:
        if not 1 <= number <= 100_000:
            raise ValueError("整数必须在 1 到 100000 之间")
        return number
    if kind == PARAMETER_DURATION:
        if not 0 <= number <= 600_000:
            raise ValueError("时长必须在 0 到 600000 ms 之间")
        return number
    raise ValueError(f"不支持的变量类型：{kind}")


def value_matches_action_field(action_type, field, kind, value):
    spec = ACTION_PARAMETER_FIELDS.get(str(action_type), {}).get(str(field))
    if spec is None or spec[0] != kind:
        return False
    try:
        converted = coerce_parameter_value(kind, value)
    except ValueError:
        return False
    _kind, minimum, maximum, choices = spec
    if choices is not None and converted not in choices:
        return False
    if minimum is not None and converted < minimum:
        return False
    if maximum is not None and converted > maximum:
        return False
    return True


def merged_parameter_values(preset, overrides=None):
    """Merge validated defaults with a call's validated known overrides."""
    definitions = parameter_definitions(preset)
    supplied = overrides if isinstance(overrides, dict) else {}
    values = {}
    for definition in definitions:
        name = definition["name"]
        raw = supplied.get(name, definition.get("default"))
        try:
            values[name] = coerce_parameter_value(definition["type"], raw)
        except ValueError:
            # Runtime remains defensive for hand-built objects that bypassed
            # schema validation: an unusable override falls back to default.
            try:
                values[name] = coerce_parameter_value(
                    definition["type"], definition.get("default")
                )
            except ValueError:
                continue
    return values


def resolve_action_parameters(actions, preset, overrides=None):
    """Clone an action tree and replace every declared field binding."""
    definitions = {
        item["name"]: item for item in parameter_definitions(preset)
    }
    values = merged_parameter_values(preset, overrides)

    def resolve(items):
        result = []
        for raw_action in items or []:
            action = {
                key: copy.deepcopy(value)
                for key, value in dict(raw_action).items()
                if key != "children"
            }
            bindings = action.get("parameter_bindings", {})
            if isinstance(bindings, dict):
                for field, name in bindings.items():
                    definition = definitions.get(str(name))
                    if definition is None or str(name) not in values:
                        continue
                    value = values[str(name)]
                    if value_matches_action_field(
                        action.get("type"), field, definition["type"], value
                    ):
                        action[str(field)] = value
            action["children"] = resolve(raw_action.get("children", []))
            result.append(action)
        return result

    return resolve(actions)
