"""Configuration structure and semantic validation.

The validator intentionally remains independent from Qt so damaged configuration
files can be checked before the UI and input backends are created.
"""

import copy
import uuid

MAX_ACTION_DEPTH = 64
MAX_ACTION_COUNT = 10_000
MAX_PROFILE_COUNT = 128
MAX_MAPPINGS_PER_SCOPE = 2_000
MAX_PRESETS_PER_SCOPE = 500
MAX_TOTAL_MAPPINGS = 5_000
MAX_TOTAL_PRESETS = 2_000
MAX_TOTAL_ACTIONS = 100_000
MAX_CONFIG_FILE_BYTES = 25 * 1024 * 1024
# One action nesting level contains both an action object and its ``children``
# list. Keep ample room above MAX_ACTION_DEPTH while staying well below the
# interpreter recursion limit used by deepcopy and the legacy repair walkers.
MAX_CONFIG_STRUCTURE_DEPTH = 256

MOUSE_NAMES = {
    "鼠标左键", "鼠标右键", "鼠标中键", "鼠标侧键 1", "鼠标侧键 2",
}
KEY_NAMES = {
    "Ctrl", "Shift", "Alt", "Caps Lock", "Tab", "Enter", "Space", "Esc",
    "Backspace", "Delete", "Insert", "Home", "End", "Page Up", "Page Down",
    "Print Screen", "Pause", "Menu", "方向上", "方向下", "方向左", "方向右",
    "静音", "音量减", "音量加", "上一曲", "下一曲", "播放/暂停",
    "`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/",
    *list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    *list("0123456789"),
    *(f"F{index}" for index in range(1, 25)),
}
INPUT_NAMES = MOUSE_NAMES | KEY_NAMES
SOURCE_NAMES = INPUT_NAMES - {"Esc"}
MODIFIER_OPTIONS = {
    "无", "Ctrl", "Shift", "Alt", "Ctrl+Shift", "Ctrl+Alt", "Shift+Alt",
    "Ctrl+Shift+Alt",
}
ACTION_TYPES = {
    "键盘点击", "鼠标点击", "鼠标滚轮", "鼠标移动", "等待", "循环动作",
}
MAPPING_MODES = {
    "同步按住", "执行一次", "固定次数", "按住循环", "开关循环", "无限循环",
    "单次触发",  # 旧配置名称，载入时会迁移为“执行一次”。
}
MAPPING_CONDITION_STATES = {"按住时", "松开时"}
PRESET_MODES = {
    "执行一次", "固定次数", "按住循环", "开关循环", "无限循环",
}
LOOP_MODES = {
    "执行次数", "无限循环", "固定次数", "执行一次",
}
ENGINE_BACKENDS = {
    "普通模式（winIOv2）", "游戏模式（Interception）",
}


def validate_config_structure_depth(data, maximum=MAX_CONFIG_STRUCTURE_DEPTH):
    """Reject pathological nesting before deepcopy or recursive repair work.

    JSON size limits do not bound nesting depth. A small hand-crafted file can
    otherwise exhaust Python's recursion limit while the startup/import repair
    helpers are making their defensive copy, before normal action-depth
    validation gets a chance to report a useful error.
    """
    stack = [(data, 0)]
    seen = set()
    while stack:
        value, depth = stack.pop()
        if not isinstance(value, (dict, list)):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if depth > int(maximum):
            raise ValueError(f"配置结构嵌套层级超过 {int(maximum)}")
        children = value.values() if isinstance(value, dict) else value
        stack.extend((child, depth + 1) for child in children)
    return data


def repair_duplicate_runtime_ids(data):
    """Regenerate missing/duplicate mapping and preset IDs across all profiles.

    Runtime ownership is global across profiles, so mapping IDs must be unique
    among mappings and preset IDs among presets. The returned payload is a deep
    copy; callers can warn before saving it.
    """
    validate_config_structure_depth(data)
    repaired = copy.deepcopy(data)
    changes = []
    seen_by_kind = {"mapping": set(), "preset": set()}

    def repair_items(items, kind, scope):
        seen = seen_by_kind[kind]
        if not isinstance(items, list):
            return
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            old_id = str(item.get("id") or "")
            if old_id and old_id not in seen:
                seen.add(old_id)
                continue
            new_id = uuid.uuid4().hex
            while new_id in seen:
                new_id = uuid.uuid4().hex
            item["id"] = new_id
            seen.add(new_id)
            changes.append({
                "scope": str(scope),
                "kind": str(kind),
                "index": index,
                "old_id": old_id,
                "new_id": new_id,
            })

    if isinstance(repaired, dict):
        repair_items(repaired.get("mappings", []), "mapping", "基础配置")
        repair_items(repaired.get("presets", []), "preset", "基础配置")
        profiles = repaired.get("profiles", [])
        if isinstance(profiles, list):
            for profile_index, profile in enumerate(profiles, 1):
                if not isinstance(profile, dict):
                    continue
                payload = profile.get("payload")
                if not isinstance(payload, dict):
                    continue
                scope = str(
                    profile.get("name") or f"配置档案 {profile_index}"
                )
                repair_items(payload.get("mappings", []), "mapping", scope)
                repair_items(payload.get("presets", []), "preset", scope)
    return repaired, changes


def repair_duplicate_action_tree_ids(data):
    """Regenerate missing/duplicate action and loop IDs inside each preset.

    Older or hand-edited files can contain repeated ordinary ``action_id``
    values or repeated loop-control ``id`` values.  Runtime loop references are
    ambiguous in such files, so the first ordinary action that owns one ID keeps
    it and later duplicates receive fresh IDs.  Existing loop ``target_action_ids``
    are left pointing at that first owner rather than being guessed.
    """
    validate_config_structure_depth(data)
    repaired = copy.deepcopy(data)
    changes = []

    def new_unique_id(seen):
        value = uuid.uuid4().hex
        while value in seen:
            value = uuid.uuid4().hex
        return value

    def repair_actions(actions, scope, preset_name):
        if not isinstance(actions, list):
            return
        seen_action_ids = set()
        seen_loop_ids = set()

        def walk(items, path):
            if not isinstance(items, list):
                return
            for index, action in enumerate(items, 1):
                if not isinstance(action, dict):
                    continue
                is_loop = action.get("type") == "循环动作"
                field = "id" if is_loop else "action_id"
                seen = seen_loop_ids if is_loop else seen_action_ids
                old_id = str(action.get(field) or "")
                if old_id and old_id not in seen:
                    seen.add(old_id)
                else:
                    new_id = new_unique_id(seen)
                    action[field] = new_id
                    seen.add(new_id)
                    changes.append({
                        "scope": str(scope),
                        "preset": str(preset_name),
                        "path": "/".join(str(part) for part in (*path, index)),
                        "kind": "loop" if is_loop else "action",
                        "field": field,
                        "old_id": old_id,
                        "new_id": new_id,
                    })
                walk(action.get("children", []), (*path, index, "children"))

        walk(actions, ())

    def repair_presets(presets, scope_name):
        if not isinstance(presets, list):
            return
        for preset_index, preset in enumerate(presets, 1):
            if not isinstance(preset, dict):
                continue
            preset_name = str(preset.get("name") or f"预设 {preset_index}")
            repair_actions(preset.get("actions", []), scope_name, preset_name)

    if isinstance(repaired, dict):
        repair_presets(repaired.get("presets", []), "基础配置")
        profiles = repaired.get("profiles", [])
        if isinstance(profiles, list):
            for profile_index, profile in enumerate(profiles, 1):
                if not isinstance(profile, dict):
                    continue
                payload = profile.get("payload")
                if not isinstance(payload, dict):
                    continue
                scope = str(profile.get("name") or f"配置档案 {profile_index}")
                repair_presets(payload.get("presets", []), scope)
    return repaired, changes


def _validate_global_runtime_ids(data):
    seen_by_kind = {"映射": {}, "预设": {}}

    def check(items, kind, scope):
        seen = seen_by_kind[kind]
        if not isinstance(items, list):
            return
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            current = f"{scope} > {kind} {index} > id"
            previous = seen.get(item_id)
            if previous is not None:
                raise ValueError(
                    f"{current} 与 {previous} 重复；同类运行项 ID 必须跨档案唯一"
                )
            seen[item_id] = current

    check(data.get("mappings", []), "映射", "基础配置")
    check(data.get("presets", []), "预设", "基础配置")
    profiles = data.get("profiles", [])
    if isinstance(profiles, list):
        for profile_index, profile in enumerate(profiles, 1):
            if not isinstance(profile, dict):
                continue
            payload = profile.get("payload")
            if not isinstance(payload, dict):
                continue
            scope = str(
                profile.get("name") or f"配置档案 {profile_index}"
            )
            check(payload.get("mappings", []), "映射", scope)
            check(payload.get("presets", []), "预设", scope)


def _path_text(path):
    return " > ".join(str(part) for part in path)


def _require_dict(value, path):
    if not isinstance(value, dict):
        raise ValueError(f"{_path_text(path)} 必须是对象")
    return value


def _require_list(value, path):
    if not isinstance(value, list):
        raise ValueError(f"{_path_text(path)} 必须是列表")
    return value


def _validate_string(value, path, *, allow_empty=True, maximum=512):
    if not isinstance(value, str):
        raise ValueError(f"{_path_text(path)} 必须是文本")
    if not allow_empty and not value.strip():
        raise ValueError(f"{_path_text(path)} 不能为空")
    if len(value) > maximum:
        raise ValueError(f"{_path_text(path)} 长度超过 {maximum} 个字符")
    return value


def _validate_bool(value, path):
    if not isinstance(value, bool):
        raise ValueError(f"{_path_text(path)} 必须是布尔值")
    return value


def _validate_int(value, path, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{_path_text(path)} 必须是整数")
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{_path_text(path)} 必须是整数") from None
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{_path_text(path)} 必须是整数")
    if isinstance(value, str) and str(converted) != value.strip():
        # Reject values such as "1.5" or arbitrary text accepted by int-like
        # custom objects, while preserving normal legacy numeric strings.
        if value.strip() not in (f"+{converted}", f"-{abs(converted)}"):
            raise ValueError(f"{_path_text(path)} 必须是整数")
    if minimum is not None and converted < minimum:
        raise ValueError(f"{_path_text(path)} 不能小于 {minimum}")
    if maximum is not None and converted > maximum:
        raise ValueError(f"{_path_text(path)} 不能大于 {maximum}")
    return converted


def _validate_choice(value, path, choices):
    _validate_string(value, path, allow_empty=False)
    if value not in choices:
        raise ValueError(f"{_path_text(path)} 包含不支持的值：{value}")
    return value


def _validate_optional_fields(container, validators):
    for field, validator in validators.items():
        if field in container:
            validator(container[field])


def _validate_hotkey(container, modifier_field, key_field, path, key_choices):
    if modifier_field in container:
        _validate_choice(
            container[modifier_field], path + (modifier_field,), MODIFIER_OPTIONS
        )
    if key_field in container:
        _validate_choice(container[key_field], path + (key_field,), key_choices)


def _field_has_text(container, field):
    return field in container and bool(str(container.get(field) or "").strip())


def _require_non_empty_field(container, field, path):
    if not _field_has_text(container, field):
        raise ValueError(f"{_path_text(path + (field,))} 不能为空")


def _validate_mouse_position(value, path):
    text = _validate_string(value, path, allow_empty=False, maximum=128)
    for prefix in ("rel:", "pct:", "window:", "client:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    try:
        raw_x, raw_y = text.split(",", 1)
        x = float(raw_x.strip())
        y = float(raw_y.strip())
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{_path_text(path)} 必须是 x,y 坐标") from None
    if not (-10_000_000 <= x <= 10_000_000 and -10_000_000 <= y <= 10_000_000):
        raise ValueError(f"{_path_text(path)} 坐标超出安全范围")


def _validate_mapping(mapping, index, seen_ids):
    path = (f"基础映射 {index}",)
    _require_dict(mapping, path)
    mapping_id = mapping.get("id")
    if mapping_id not in (None, ""):
        mapping_id = _validate_string(mapping_id, path + ("id",), allow_empty=False)
        if mapping_id in seen_ids:
            raise ValueError(f"{_path_text(path + ('id',))} 与其他基础映射重复")
        seen_ids.add(mapping_id)

    _validate_optional_fields(mapping, {
        "enabled": lambda value: _validate_bool(value, path + ("enabled",)),
        "name": lambda value: _validate_string(value, path + ("name",)),
        "mode": lambda value: _validate_choice(value, path + ("mode",), MAPPING_MODES),
        "hold_ms": lambda value: _validate_int(value, path + ("hold_ms",), 1, 600_000),
        "hold_jitter_ms": lambda value: _validate_int(value, path + ("hold_jitter_ms",), 0, 600_000),
        "loop_count": lambda value: _validate_int(value, path + ("loop_count",), 1, 100_000),
        "loop_interval_ms": lambda value: _validate_int(value, path + ("loop_interval_ms",), 0, 600_000),
        "loop_interval_jitter_ms": lambda value: _validate_int(value, path + ("loop_interval_jitter_ms",), 0, 600_000),
        "speed_percent": lambda value: _validate_int(value, path + ("speed_percent",), 10, 500),
        "max_runtime_s": lambda value: _validate_int(value, path + ("max_runtime_s",), 0, 86_400),
        "condition_enabled": lambda value: _validate_bool(value, path + ("condition_enabled",)),
        "condition_state": lambda value: _validate_choice(
            value, path + ("condition_state",), MAPPING_CONDITION_STATES
        ),
    })
    if mapping.get("enabled", False):
        for required_field in (
            "source_modifiers", "source", "target_modifiers", "target"
        ):
            _require_non_empty_field(mapping, required_field, path)
    _validate_hotkey(mapping, "source_modifiers", "source", path, SOURCE_NAMES)
    _validate_hotkey(mapping, "target_modifiers", "target", path, INPUT_NAMES)
    if "condition_input" in mapping:
        _validate_choice(
            mapping["condition_input"], path + ("condition_input",), SOURCE_NAMES
        )
    if mapping.get("condition_enabled"):
        if "condition_input" not in mapping:
            raise ValueError(f"{_path_text(path + ('condition_input',))} 不能为空")
        if "condition_state" not in mapping:
            raise ValueError(f"{_path_text(path + ('condition_state',))} 不能为空")


def _validate_action(action, path, state, depth, require_runtime_fields=False):
    if depth > MAX_ACTION_DEPTH:
        raise ValueError(f"{_path_text(path)} 超过最大嵌套层级 {MAX_ACTION_DEPTH}")
    _require_dict(action, path)
    state["count"] += 1
    if state["count"] > MAX_ACTION_COUNT:
        raise ValueError(f"{_path_text(path)} 的动作总数超过 {MAX_ACTION_COUNT}")

    if require_runtime_fields:
        _require_non_empty_field(action, "type", path)
    action_type = action.get("type", "键盘点击")
    _validate_choice(action_type, path + ("type",), ACTION_TYPES)
    children = action.get("children", [])
    _require_list(children, path + ("children",))

    if action_type == "循环动作":
        loop_id = action.get("id")
        if loop_id not in (None, ""):
            loop_id = _validate_string(loop_id, path + ("id",), allow_empty=False)
            if loop_id in state["loop_ids"]:
                raise ValueError(f"{_path_text(path + ('id',))} 与其他循环项目重复")
            state["loop_ids"].add(loop_id)
        _validate_optional_fields(action, {
            "name": lambda value: _validate_string(value, path + ("name",)),
            "sequence_number": lambda value: _validate_int(value, path + ("sequence_number",), 1, 100_000),
            "execution_mode": lambda value: _validate_choice(value, path + ("execution_mode",), LOOP_MODES),
            "loop_count": lambda value: _validate_int(value, path + ("loop_count",), 1, 100_000),
            "loop_interval_ms": lambda value: _validate_int(value, path + ("loop_interval_ms",), 0, 600_000),
            "loop_interval_jitter_ms": lambda value: _validate_int(value, path + ("loop_interval_jitter_ms",), 0, 600_000),
            "speed_percent": lambda value: _validate_int(value, path + ("speed_percent",), 10, 500),
            "max_runtime_s": lambda value: _validate_int(value, path + ("max_runtime_s",), 0, 86_400),
            "timeline_mode": lambda value: _validate_choice(value, path + ("timeline_mode",), {"sequential", "parallel"}),
            "color_index": lambda value: _validate_int(value, path + ("color_index",), 0, 1_000_000),
        })
        target_ids = action.get("target_action_ids", [])
        _require_list(target_ids, path + ("target_action_ids",))
        seen_targets = set()
        for target_index, target_id in enumerate(target_ids, 1):
            target_path = path + (f"引用 {target_index}",)
            target_id = _validate_string(target_id, target_path, allow_empty=False)
            if target_id in seen_targets:
                raise ValueError(f"{_path_text(target_path)} 重复引用同一个动作")
            seen_targets.add(target_id)
    else:
        action_id = action.get("action_id")
        if action_id not in (None, ""):
            action_id = _validate_string(
                action_id, path + ("action_id",), allow_empty=False
            )
            if action_id in state["action_ids"]:
                raise ValueError(
                    f"{_path_text(path + ('action_id',))} 与其他动作重复"
                )
            state["action_ids"].add(action_id)

        if action_type == "键盘点击":
            if require_runtime_fields:
                _require_non_empty_field(action, "target", path)
            if "target" in action:
                _validate_choice(action["target"], path + ("target",), KEY_NAMES)
            _validate_optional_fields(action, {
                "modifiers": lambda value: _validate_choice(
                    value, path + ("modifiers",), MODIFIER_OPTIONS
                ),
                "hold_ms": lambda value: _validate_int(value, path + ("hold_ms",), 1, 600_000),
                "jitter_ms": lambda value: _validate_int(value, path + ("jitter_ms",), 0, 600_000),
            })
        elif action_type == "鼠标点击":
            if require_runtime_fields:
                _require_non_empty_field(action, "target", path)
            if "target" in action:
                _validate_choice(action["target"], path + ("target",), MOUSE_NAMES)
            _validate_optional_fields(action, {
                "modifiers": lambda value: _validate_choice(
                    value, path + ("modifiers",), MODIFIER_OPTIONS
                ),
                "hold_ms": lambda value: _validate_int(value, path + ("hold_ms",), 1, 600_000),
                "jitter_ms": lambda value: _validate_int(value, path + ("jitter_ms",), 0, 600_000),
            })
        elif action_type == "鼠标滚轮":
            if require_runtime_fields:
                _require_non_empty_field(action, "target", path)
            if "target" in action:
                _validate_choice(action["target"], path + ("target",), {"向上", "向下"})
            if "steps" in action:
                _validate_int(action["steps"], path + ("steps",), 1, 100)
        elif action_type == "鼠标移动":
            if require_runtime_fields:
                _require_non_empty_field(action, "target", path)
            if "target" in action:
                _validate_mouse_position(action["target"], path + ("target",))
        elif action_type == "等待":
            if "wait_ms" in action:
                _validate_int(action["wait_ms"], path + ("wait_ms",), 1, 600_000)
            if "jitter_ms" in action:
                _validate_int(action["jitter_ms"], path + ("jitter_ms",), 0, 600_000)

    for child_index, child in enumerate(children, 1):
        _validate_action(
            child, path + (f"子动作 {child_index}",), state, depth + 1,
            require_runtime_fields=require_runtime_fields,
        )



def repair_overlapping_loop_controls(data):
    """Return a startup-safe copy with later overlapping loop cards removed.

    Older releases allowed a loop card to be duplicated while preserving its
    external action references.  Such files must remain loadable after overlap
    validation became strict.  Only the later conflicting control card is
    removed; ordinary actions and the first loop owning each action remain
    unchanged.
    """
    import copy

    validate_config_structure_depth(data)
    repaired = copy.deepcopy(data)
    removed = []

    def repair_presets(presets, scope_name):
        if not isinstance(presets, list):
            return
        for preset_index, preset in enumerate(presets, 1):
            if not isinstance(preset, dict):
                continue
            actions = preset.get("actions")
            if not isinstance(actions, list):
                continue
            claimed_ids = set()
            kept = []
            for action_index, action in enumerate(actions, 1):
                if not isinstance(action, dict) or action.get("type") != "循环动作":
                    kept.append(action)
                    continue
                target_ids = [
                    str(value)
                    for value in action.get("target_action_ids", []) or []
                    if str(value)
                ]
                overlapping = [value for value in target_ids if value in claimed_ids]
                if target_ids and overlapping:
                    removed.append({
                        "scope": scope_name,
                        "preset": str(preset.get("name") or f"预设 {preset_index}"),
                        "loop": str(action.get("name") or f"循环项目 {action_index}"),
                        "overlapping_ids": overlapping,
                    })
                    continue
                claimed_ids.update(target_ids)
                kept.append(action)
            preset["actions"] = kept

    if isinstance(repaired, dict):
        repair_presets(repaired.get("presets", []), "基础配置")
        profiles = repaired.get("profiles", [])
        if isinstance(profiles, list):
            for profile_index, profile in enumerate(profiles, 1):
                if not isinstance(profile, dict):
                    continue
                payload = profile.get("payload")
                if not isinstance(payload, dict):
                    continue
                profile_name = str(
                    profile.get("name") or f"配置档案 {profile_index}"
                )
                repair_presets(payload.get("presets", []), profile_name)
    return repaired, removed


def _validate_loop_references(actions, path):
    """Validate every reference loop against one contiguous sibling range.

    Loop control cards live at the preset root, but they may reference a
    continuous range inside any ordinary child level.  This mirrors the action
    editor and the scheduler, which reuses root loop controls while recursively
    executing child sequences.
    """
    sibling_sequences = []
    loop_controls = []

    def collect(level, level_path, depth):
        ordinary_ids = []
        for action_index, action in enumerate(level or [], 1):
            if not isinstance(action, dict):
                continue
            action_path = level_path + (f"动作 {action_index}",)
            if action.get("type") == "循环动作":
                loop_controls.append((action, action_path, depth))
            else:
                action_id = str(action.get("action_id") or "")
                if action_id:
                    ordinary_ids.append(action_id)
            collect(
                action.get("children", []) or [],
                action_path + ("children",),
                depth + 1,
            )
        if ordinary_ids:
            sibling_sequences.append(ordinary_ids)

    collect(actions, path, 0)
    position_map = {
        action_id: (sequence_index, position)
        for sequence_index, sequence in enumerate(sibling_sequences)
        for position, action_id in enumerate(sequence)
    }
    ordinary_set = set(position_map)
    claimed_ids = set()
    for action, action_path, depth in loop_controls:
        target_ids = [
            str(value) for value in action.get("target_action_ids", []) or []
        ]
        if not target_ids:
            # Legacy configurations physically nested their selected actions
            # under the loop card and are migrated after loading. Permit that
            # old representation so startup can still reach the migration step.
            if action.get("children"):
                continue
            raise ValueError(
                f"{_path_text(action_path + ('target_action_ids',))} 不能为空"
            )
        if depth:
            raise ValueError(
                f"{_path_text(action_path)} 循环控制卡只能位于预设根层级"
            )
        missing = [value for value in target_ids if value not in ordinary_set]
        if missing:
            raise ValueError(
                f"{_path_text(action_path + ('target_action_ids',))} 引用了当前预设中不存在的动作："
                + ", ".join(missing[:5])
            )
        sequence_index, start = position_map[target_ids[0]]
        sequence = sibling_sequences[sequence_index]
        contiguous = sequence[start:start + len(target_ids)] == target_ids
        if not contiguous:
            raise ValueError(
                f"{_path_text(action_path + ('target_action_ids',))} 必须引用同一层级中按顺序连续的动作"
            )
        overlapping = [value for value in target_ids if value in claimed_ids]
        if overlapping:
            raise ValueError(
                f"{_path_text(action_path + ('target_action_ids',))} 与已有循环项目的引用范围重叠："
                + ", ".join(overlapping[:5])
            )
        claimed_ids.update(target_ids)


def _validate_scope_sizes(mappings, presets, path, totals):
    if len(mappings) > MAX_MAPPINGS_PER_SCOPE:
        raise ValueError(
            f"{_path_text(path + ('mappings',))} 数量超过 {MAX_MAPPINGS_PER_SCOPE}"
        )
    if len(presets) > MAX_PRESETS_PER_SCOPE:
        raise ValueError(
            f"{_path_text(path + ('presets',))} 数量超过 {MAX_PRESETS_PER_SCOPE}"
        )
    totals["mappings"] += len(mappings)
    totals["presets"] += len(presets)
    if totals["mappings"] > MAX_TOTAL_MAPPINGS:
        raise ValueError(f"全部映射总数超过 {MAX_TOTAL_MAPPINGS}")
    if totals["presets"] > MAX_TOTAL_PRESETS:
        raise ValueError(f"全部预设总数超过 {MAX_TOTAL_PRESETS}")


def validate_preset_payload(preset, index=1, _global_state=None, _path=None):
    path = _path or (f"预设 {index}",)
    _require_dict(preset, path)
    _validate_optional_fields(preset, {
        "enabled": lambda value: _validate_bool(value, path + ("enabled",)),
        "name": lambda value: _validate_string(value, path + ("name",)),
        "execution_mode": lambda value: _validate_choice(value, path + ("execution_mode",), PRESET_MODES),
        "loop_count": lambda value: _validate_int(value, path + ("loop_count",), 1, 100_000),
        "loop_interval_ms": lambda value: _validate_int(value, path + ("loop_interval_ms",), 0, 600_000),
        "loop_interval_jitter_ms": lambda value: _validate_int(value, path + ("loop_interval_jitter_ms",), 0, 600_000),
        "speed_percent": lambda value: _validate_int(value, path + ("speed_percent",), 10, 500),
        "max_runtime_s": lambda value: _validate_int(value, path + ("max_runtime_s",), 0, 86_400),
    })
    preset_enabled = bool(preset.get("enabled", False))
    if preset_enabled:
        for required_field in ("trigger_modifiers", "trigger"):
            _require_non_empty_field(preset, required_field, path)
    _validate_hotkey(preset, "trigger_modifiers", "trigger", path, SOURCE_NAMES)
    actions = _require_list(preset.get("actions", []), path + ("actions",))
    if preset_enabled and not actions:
        raise ValueError(f"{_path_text(path + ('actions',))} 不能为空")
    state = {"count": 0, "action_ids": set(), "loop_ids": set()}
    for action_index, action in enumerate(actions, 1):
        _validate_action(
            action, path + (f"动作 {action_index}",), state, depth=1,
            require_runtime_fields=preset_enabled,
        )
    _validate_loop_references(actions, path + ("actions",))
    if _global_state is not None:
        _global_state["actions"] += state["count"]
        if _global_state["actions"] > MAX_TOTAL_ACTIONS:
            raise ValueError(f"全部动作总数超过 {MAX_TOTAL_ACTIONS}")
    return preset


def _validate_profile(profile, index, seen_ids, totals):
    path = (f"配置档案 {index}",)
    _require_dict(profile, path)
    profile_id = profile.get("id")
    if profile_id not in (None, ""):
        profile_id = _validate_string(profile_id, path + ("id",), allow_empty=False)
        if profile_id in seen_ids:
            raise ValueError(f"{_path_text(path + ('id',))} 与其他配置档案重复")
        seen_ids.add(profile_id)
    _validate_optional_fields(profile, {
        "name": lambda value: _validate_string(value, path + ("name",)),
        "enabled": lambda value: _validate_bool(value, path + ("enabled",)),
        "allow_other_windows": lambda value: _validate_bool(value, path + ("allow_other_windows",)),
    })
    match_field_present = False
    for field in ("process_names", "title_contains"):
        values = _require_list(profile.get(field, []), path + (field,))
        if values:
            match_field_present = True
        for value_index, value in enumerate(values, 1):
            _validate_string(
                value, path + (field, value_index), allow_empty=False, maximum=512
            )
    if profile.get("enabled", False) and not match_field_present:
        raise ValueError(
            f"{_path_text(path)} 已启用但未填写进程名或标题包含，无法匹配任何窗口"
        )
    payload = profile.get("payload")
    if payload is not None:
        payload_path = path + ("payload",)
        _require_dict(payload, payload_path)
        if "profiles" in payload:
            raise ValueError(f"{_path_text(payload_path)} 不能继续嵌套配置档案")
        mappings = _require_list(payload.get("mappings", []), payload_path + ("mappings",))
        presets = _require_list(payload.get("presets", []), payload_path + ("presets",))
        _validate_scope_sizes(mappings, presets, payload_path, totals)
        mapping_ids = set()
        preset_ids = set()
        for mapping_index, mapping in enumerate(mappings, 1):
            _validate_mapping(mapping, mapping_index, mapping_ids)
        for preset_index, preset in enumerate(presets, 1):
            validate_preset_payload(
                preset, preset_index, _global_state=totals,
                _path=payload_path + (f"预设 {preset_index}",),
            )
            preset_id = preset.get("id")
            if preset_id not in (None, ""):
                preset_id = _validate_string(
                    preset_id,
                    payload_path + ("presets", preset_index, "id"),
                    allow_empty=False,
                )
                if preset_id in preset_ids:
                    raise ValueError(
                        f"{_path_text(payload_path + ('presets', preset_index, 'id'))} "
                        "与同一档案中的其他预设重复"
                    )
                preset_ids.add(preset_id)


def validate_config_payload(data, *, allow_profiles=True):
    _require_dict(data, ("配置根节点",))
    try:
        import json
        serialized_size = len(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError):
        raise ValueError("配置包含无法序列化的数据") from None
    if serialized_size > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            f"配置序列化后超过 {MAX_CONFIG_FILE_BYTES // (1024 * 1024)} MB 上限"
        )
    totals = {"mappings": 0, "presets": 0, "actions": 0}

    _validate_optional_fields(data, {
        "version": lambda value: _validate_int(value, ("version",), 1, 1_000_000),
        "engine_backend": lambda value: _validate_choice(value, ("engine_backend",), ENGINE_BACKENDS),
        "auto_apply": lambda value: _validate_bool(value, ("auto_apply",)),
        "global_toggle_enabled": lambda value: _validate_bool(value, ("global_toggle_enabled",)),
        "macro_pause_enabled": lambda value: _validate_bool(value, ("macro_pause_enabled",)),
        "diagnostic_enabled": lambda value: _validate_bool(value, ("diagnostic_enabled",)),
        "profile_auto_switch_enabled": lambda value: _validate_bool(
            value, ("profile_auto_switch_enabled",)
        ),
        "active_profile_id": lambda value: _validate_string(value, ("active_profile_id",)),
        "editor_profile_id": lambda value: _validate_string(value, ("editor_profile_id",)),
    })
    for prefix in (
        "global_toggle", "macro_pause", "emergency",
        "recording_cancel", "recording_finish"
    ):
        _validate_hotkey(
            data, f"{prefix}_modifiers", f"{prefix}_key", (prefix,), SOURCE_NAMES
        )

    mappings = _require_list(data.get("mappings", []), ("mappings",))
    presets = _require_list(data.get("presets", []), ("presets",))
    _validate_scope_sizes(mappings, presets, ("基础配置",), totals)
    seen_mapping_ids = set()
    seen_preset_ids = set()
    for index, mapping in enumerate(mappings, 1):
        _validate_mapping(mapping, index, seen_mapping_ids)
    for index, preset in enumerate(presets, 1):
        preset_id = preset.get("id") if isinstance(preset, dict) else None
        if preset_id not in (None, ""):
            preset_id = _validate_string(
                preset_id, (f"预设 {index}", "id"), allow_empty=False
            )
            if preset_id in seen_preset_ids:
                raise ValueError(f"预设 {index} > id 与其他预设重复")
            seen_preset_ids.add(preset_id)
        validate_preset_payload(
            preset, index, _global_state=totals,
            _path=(f"预设 {index}",),
        )

    if allow_profiles:
        profiles = _require_list(data.get("profiles", []), ("profiles",))
        if len(profiles) > MAX_PROFILE_COUNT:
            raise ValueError(f"配置档案数量超过 {MAX_PROFILE_COUNT}")
        seen_profile_ids = set()
        for index, profile in enumerate(profiles, 1):
            _validate_profile(profile, index, seen_profile_ids, totals)
    elif "profiles" in data:
        raise ValueError("配置档案的 payload 不能继续嵌套 profiles")
    _validate_global_runtime_ids(data)
    return data
