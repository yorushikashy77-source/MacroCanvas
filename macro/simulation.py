"""Side-effect-free macro duration and action timeline preview."""

from __future__ import annotations

from collections import Counter

from core.constants import (
    CONDITION_ACTION_TYPE, CONDITION_BRANCH_TYPES,
    SUBMACRO_ACTION_TYPE, WAIT_CONDITION_ACTION_TYPE,
)
from macro.scheduler import LOOP_ACTION_TYPE, MacroTask


def _bounded_int(value, default=0, minimum=0, maximum=2_147_483_647):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(minimum, min(maximum, number))


def _scaled_range(base, jitter, speed, minimum=0):
    base = _bounded_int(base, minimum, minimum)
    jitter = _bounded_int(jitter, 0, 0)
    speed = _bounded_int(speed, 100, 10, 500)
    scale = 100.0 / speed
    return (
        round(max(minimum, base - jitter) * scale),
        round(max(minimum, base + jitter) * scale),
    )


def _action_own_duration(action, speed):
    kind = str(action.get("type") or "动作")
    if kind in (
        CONDITION_ACTION_TYPE, SUBMACRO_ACTION_TYPE, *CONDITION_BRANCH_TYPES,
    ):
        return 0, 0
    if kind == WAIT_CONDITION_ACTION_TYPE:
        timeout = _bounded_int(action.get("timeout_ms", 0), 0, 0, 600_000)
        return 0, timeout or None
    if kind == "等待":
        return _scaled_range(
            action.get("wait_ms", 1), action.get("jitter_ms", 0), speed, 1
        )
    if kind == "鼠标滚轮":
        duration = max(0, _bounded_int(action.get("steps", 1), 1, 1) - 1) * 2
        return duration, duration
    if kind == "鼠标移动":
        return 0, 0
    return _scaled_range(
        action.get("hold_ms", 100), action.get("jitter_ms", 0), speed, 1
    )


def _loop_controls(actions):
    controls = {}
    for action in actions or []:
        if action.get("type") != LOOP_ACTION_TYPE:
            continue
        target_ids = [
            str(value) for value in action.get("target_action_ids", []) or []
            if value
        ]
        if target_ids:
            controls.setdefault(target_ids[0], []).append((action, target_ids))
    return controls


def _action_group_duration(action, speed, library=None, call_stack=()):
    kind = str(action.get("type") or "动作")
    if kind == CONDITION_ACTION_TYPE:
        true_actions, else_actions = MacroTask.condition_branch_actions(action)
        true_min, true_max = _sequence_duration(
            true_actions, speed, timeline_mode="sequential",
            library=library, call_stack=call_stack,
        )
        else_min, else_max = _sequence_duration(
            else_actions, speed, timeline_mode="sequential",
            library=library, call_stack=call_stack,
        )
        maxima = (true_max, else_max)
        return (
            min(true_min, else_min),
            None if any(value is None for value in maxima) else max(maxima),
        )
    if kind == SUBMACRO_ACTION_TYPE:
        target_id = str(action.get("preset_id") or "")
        target = (library or {}).get(target_id)
        if not target or target_id in call_stack or len(call_stack) >= 16:
            called_min = called_max = 0
        else:
            local_speed = _bounded_int(
                action.get("speed_percent", 100), 100, 10, 500
            )
            effective_speed = max(
                10, min(500, round(speed * local_speed / 100))
            )
            called_min, called_max = _sequence_duration(
                target.get("actions", []), effective_speed,
                library=library, call_stack=call_stack + (target_id,),
            )
            repeats = _bounded_int(
                action.get("repeat_count", 1), 1, 1, 100_000
            )
            called_min *= repeats
            called_max = None if called_max is None else called_max * repeats
        child_min, child_max = _sequence_duration(
            action.get("children", []) or [], speed,
            library=library, call_stack=call_stack,
        )
        return (
            called_min + child_min,
            None if called_max is None or child_max is None
            else called_max + child_max,
        )
    own_min, own_max = _action_own_duration(action, speed)
    child_min, child_max = _sequence_duration(
        action.get("children", []) or [], speed,
        timeline_mode=(
            "sequential" if kind in (
                CONDITION_ACTION_TYPE, WAIT_CONDITION_ACTION_TYPE,
                *CONDITION_BRANCH_TYPES,
            ) else "parallel"
        ),
        library=library, call_stack=call_stack,
    )
    if kind == WAIT_CONDITION_ACTION_TYPE:
        return (
            child_min,
            None if own_max is None or child_max is None else own_max + child_max,
        )
    return max(own_min, child_min), max(own_max, child_max)


def _segment_duration(
    segment, speed, timeline_mode, library=None, call_stack=(),
):
    durations = [
        _action_group_duration(action, speed, library, call_stack)
        for action in segment
    ]
    if not durations:
        return 0, 0
    if timeline_mode == "parallel":
        return max(item[0] for item in durations), max(item[1] for item in durations)
    return sum(item[0] for item in durations), sum(item[1] for item in durations)


def _loop_duration(
    control, segment, parent_speed, timeline_mode, library=None, call_stack=(),
):
    local_speed = _bounded_int(control.get("speed_percent", 100), 100, 10, 500)
    speed = max(10, min(500, round(parent_speed * local_speed / 100)))
    cycle_min, cycle_max = _segment_duration(
        segment, speed, timeline_mode, library, call_stack
    )
    mode = str(control.get("execution_mode") or "执行次数")
    if mode == "执行次数":
        cycles = _bounded_int(control.get("loop_count", 2), 2, 1, 100_000)
        interval_min, interval_max = _scaled_range(
            control.get("loop_interval_ms", 0),
            control.get("loop_interval_jitter_ms", 0),
            speed,
            0,
        )
        if interval_min == interval_max == 0 and cycles > 1:
            interval_min = interval_max = 1
        return (
            cycle_min * cycles + interval_min * max(0, cycles - 1),
            cycle_max * cycles + interval_max * max(0, cycles - 1),
            False,
        )
    cap_ms = _bounded_int(control.get("max_runtime_s", 0), 0, 0) * 1000
    maximum = (
        cap_ms + cycle_max
        if cap_ms and cycle_max is not None else None
    )
    return cycle_min, maximum, True


def _sequence_duration(
    actions, speed, timeline_mode="sequential", library=None, call_stack=(),
):
    ordinary = [
        action for action in actions or []
        if action.get("type") != LOOP_ACTION_TYPE
    ]
    controls = _loop_controls(actions)
    values = []
    index = 0
    while index < len(ordinary):
        action = ordinary[index]
        action_id = str(action.get("action_id") or "")
        matched = None
        for control, target_ids in controls.get(action_id, []):
            segment = ordinary[index:index + len(target_ids)]
            if [str(item.get("action_id") or "") for item in segment] == target_ids:
                matched = (control, segment)
                break
        if matched:
            loop_min, loop_max, _infinite = _loop_duration(
                matched[0], matched[1], speed,
                str(matched[0].get("timeline_mode") or timeline_mode),
                library, call_stack,
            )
            values.append((loop_min, loop_max))
            index += len(matched[1])
        else:
            values.append(_action_group_duration(
                action, speed, library, call_stack
            ))
            index += 1
    if not values:
        return 0, 0
    finite_max = [value[1] for value in values if value[1] is not None]
    if timeline_mode == "parallel":
        minimum = max(value[0] for value in values)
        maximum = None if len(finite_max) != len(values) else max(finite_max)
    else:
        minimum = sum(value[0] for value in values)
        maximum = None if len(finite_max) != len(values) else sum(finite_max)
    return minimum, maximum


def _timeline_events(
    actions, speed, events, warnings, path=(), max_events=500,
    base_min=0, base_max=0, library=None, call_stack=(),
):
    ordinary = [
        action for action in actions or []
        if action.get("type") != LOOP_ACTION_TYPE
    ]
    controls = _loop_controls(actions)
    cursor_min, cursor_max = base_min, base_max
    index = 0
    while index < len(ordinary) and len(events) < max_events:
        action = ordinary[index]
        action_id = str(action.get("action_id") or "")
        matched = None
        for control, target_ids in controls.get(action_id, []):
            segment = ordinary[index:index + len(target_ids)]
            if [str(item.get("action_id") or "") for item in segment] == target_ids:
                matched = (control, segment)
                break
        if matched:
            control, segment = matched
            mode = str(control.get("execution_mode") or "执行次数")
            loops = (
                f"{_bounded_int(control.get('loop_count', 2), 2, 1, 100_000)} 次"
                if mode == "执行次数" else "持续循环"
            )
            loop_min, loop_max, infinite = _loop_duration(
                control, segment, speed,
                str(control.get("timeline_mode") or "sequential"),
                library, call_stack,
            )
            events.append({
                "path": ".".join(map(str, path + (index + 1,))),
                "start_min_ms": cursor_min,
                "start_max_ms": cursor_max,
                "duration_min_ms": loop_min,
                "duration_max_ms": loop_max,
                "description": f"{MacroTask.describe(control)}；范围 {loops}",
                "kind": "loop",
            })
            local_speed = _bounded_int(
                control.get("speed_percent", 100), 100, 10, 500
            )
            effective_speed = max(
                10, min(500, round(speed * local_speed / 100))
            )
            _timeline_events(
                segment, effective_speed, events, warnings,
                path + (index + 1,), max_events,
                base_min=cursor_min, base_max=cursor_max,
                library=library, call_stack=call_stack,
            )
            if infinite:
                warnings.append(
                    f"{MacroTask.describe(control)}没有固定结束时间；预览仅展示首轮范围。"
                )
            cursor_min += loop_min
            if cursor_max is not None and loop_max is not None:
                cursor_max += loop_max
            else:
                cursor_max = None
            index += len(segment)
            continue

        own_min, own_max = _action_group_duration(
            action, speed, library, call_stack
        )
        events.append({
            "path": ".".join(map(str, path + (index + 1,))),
            "start_min_ms": cursor_min,
            "start_max_ms": cursor_max,
            "duration_min_ms": own_min,
            "duration_max_ms": own_max,
            "description": MacroTask.describe(action),
            "kind": str(action.get("type") or "动作"),
        })
        if action.get("children"):
            _timeline_events(
                action.get("children", []), speed, events, warnings,
                path + (index + 1,), max_events,
                base_min=cursor_min, base_max=cursor_max,
                library=library, call_stack=call_stack,
            )
        if action.get("type") in (CONDITION_ACTION_TYPE, WAIT_CONDITION_ACTION_TYPE):
            warnings.append(
                f"{MacroTask.describe(action)} 依赖预览时无法确定的实时输入状态。"
            )
        if action.get("type") == SUBMACRO_ACTION_TYPE:
            target_id = str(action.get("preset_id") or "")
            if target_id not in (library or {}):
                warnings.append(f"子宏目标 {target_id or '未设置'} 不存在。")
        cursor_min += own_min
        cursor_max = cursor_max + own_max if cursor_max is not None else None
        index += 1


def simulate_preset(preset, max_events=500):
    """Return a bounded preview without starting an engine or sending input."""
    actions = list(preset.get("actions", []) or [])
    speed = _bounded_int(preset.get("speed_percent", 100), 100, 10, 500)
    library = preset.get("_preset_library", {})
    if not isinstance(library, dict):
        library = {}
    call_stack = (str(preset.get("id") or ""),)
    one_min, one_max = _sequence_duration(
        actions, speed, library=library, call_stack=call_stack
    )
    mode = str(preset.get("execution_mode") or "执行一次")
    warnings = []
    if mode == "固定次数":
        cycles = _bounded_int(preset.get("loop_count", 1), 1, 1, 100_000)
    elif mode == "执行一次":
        cycles = 1
    else:
        cycles = None

    if cycles is not None:
        interval_min, interval_max = _scaled_range(
            preset.get("loop_interval_ms", 0),
            preset.get("loop_interval_jitter_ms", 0), speed, 0,
        )
        if interval_min == interval_max == 0 and cycles > 1:
            interval_min = interval_max = 1
        total_min = one_min * cycles + interval_min * max(0, cycles - 1)
        total_max = (
            None if one_max is None else
            one_max * cycles + interval_max * max(0, cycles - 1)
        )
    else:
        total_min = one_min
        cap_ms = _bounded_int(preset.get("max_runtime_s", 0), 0, 0) * 1000
        total_max = (
            cap_ms + one_max
            if cap_ms and one_max is not None else None
        )
        warnings.append(
            "该预设会持续循环；最长运行限制会在轮次边界检查，"
            "当前轮可能在限制到达后才结束。"
        )

    events = []
    _timeline_events(
        actions, speed, events, warnings, max_events=max_events,
        library=library, call_stack=call_stack,
    )
    action_types = Counter()
    stack = list(actions)
    while stack:
        action = stack.pop()
        action_types[str(action.get("type") or "动作")] += 1
        stack.extend(action.get("children", []) or [])
    if len(events) >= max_events:
        warnings.append(f"动作较多，时间线只展示前 {max_events} 项。")
    return {
        "name": str(preset.get("name") or "未命名预设"),
        "execution_mode": mode,
        "speed_percent": speed,
        "one_cycle_min_ms": one_min,
        "one_cycle_max_ms": one_max,
        "total_min_ms": total_min,
        "total_max_ms": total_max,
        "events": events,
        "warnings": list(dict.fromkeys(warnings)),
        "action_types": dict(sorted(action_types.items())),
    }
