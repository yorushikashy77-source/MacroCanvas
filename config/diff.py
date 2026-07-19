"""Configuration-section diffing and dependency-safe selective restore."""

from __future__ import annotations

import copy
import json


SECTION_DEFINITIONS = (
    {
        "key": "content",
        "label": "全部映射、预设与配置档案",
        "description": "作为一个整体恢复，避免跨档案 ID 或循环引用失效。",
        "fields": (
            "mappings", "presets", "profiles", "profile_auto_switch_enabled",
            "active_profile_id", "editor_profile_id",
        ),
    },
    {
        "key": "runtime_controls",
        "label": "输入后端与全局控制快捷键",
        "description": "包括输入后端、总开关、暂停、紧急停止和录制控制键。",
        "fields": (
            "engine_backend",
            "global_toggle_enabled", "global_toggle_modifiers", "global_toggle_key",
            "macro_pause_enabled", "macro_pause_modifiers", "macro_pause_key",
            "emergency_modifiers", "emergency_key",
            "recording_cancel_modifiers", "recording_cancel_key",
            "recording_finish_modifiers", "recording_finish_key",
        ),
    },
    {
        "key": "preferences",
        "label": "自动应用与诊断偏好",
        "description": "只恢复不会改变映射内容的程序偏好。",
        "fields": ("auto_apply", "diagnostic_enabled"),
    },
)


_FIELD_LABELS = {
    "engine_backend": "输入后端",
    "global_toggle_enabled": "全局开关是否启用",
    "global_toggle_modifiers": "全局开关修饰键",
    "global_toggle_key": "全局开关键",
    "macro_pause_enabled": "宏暂停快捷键是否启用",
    "macro_pause_modifiers": "宏暂停修饰键",
    "macro_pause_key": "宏暂停键",
    "emergency_modifiers": "紧急停止修饰键",
    "emergency_key": "紧急停止键",
    "recording_cancel_modifiers": "取消录制修饰键",
    "recording_cancel_key": "取消录制键",
    "recording_finish_modifiers": "完成录制修饰键",
    "recording_finish_key": "完成录制键",
    "auto_apply": "自动应用",
    "diagnostic_enabled": "本地诊断日志",
}


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _named_entries(items, fallback_prefix):
    result = {}
    order = []
    for index, item in enumerate(items or []):
        identifier = str(item.get("id") or f"{fallback_prefix}:{index}")
        result[identifier] = item
        order.append(identifier)
    return result, order


def _list_delta(current_items, backup_items, noun):
    current, current_order = _named_entries(current_items, noun)
    backup, backup_order = _named_entries(backup_items, noun)
    added_ids = [identifier for identifier in backup if identifier not in current]
    removed_ids = [identifier for identifier in current if identifier not in backup]
    modified_ids = [
        identifier for identifier in sorted(backup.keys() & current.keys())
        if _canonical(backup[identifier]) != _canonical(current[identifier])
    ]
    common_ids = current.keys() & backup.keys()
    current_common_order = [
        identifier for identifier in current_order if identifier in common_ids
    ]
    backup_common_order = [
        identifier for identifier in backup_order if identifier in common_ids
    ]
    order_changed = current_common_order != backup_common_order

    def names(index, identifiers):
        return [
            str(index[identifier].get("name") or "未命名")
            for identifier in identifiers[:8]
        ]

    details = []
    if added_ids:
        details.append(f"备份新增 {noun}：" + "、".join(names(backup, added_ids)))
    if removed_ids:
        details.append(f"备份中缺少 {noun}：" + "、".join(names(current, removed_ids)))
    if modified_ids:
        details.append(f"内容不同的 {noun}：" + "、".join(names(backup, modified_ids)))
    if order_changed:
        order_kind = "优先级顺序" if noun in ("映射", "配置档案") else "显示顺序"
        details.append(
            f"备份中的{noun}{order_kind}与当前配置不同；恢复后将采用备份顺序"
        )
    return {
        "added": len(added_ids),
        "removed": len(removed_ids),
        "modified": len(modified_ids),
        "reordered": int(order_changed),
        "details": details,
    }


def _content_section_diff(current, backup, definition):
    groups = (
        ("映射", current.get("mappings", []), backup.get("mappings", [])),
        ("预设", current.get("presets", []), backup.get("presets", [])),
        ("配置档案", current.get("profiles", []), backup.get("profiles", [])),
    )
    details = []
    changed_items = 0
    summaries = []
    for noun, current_items, backup_items in groups:
        delta = _list_delta(current_items, backup_items, noun)
        item_changes = delta["added"] + delta["removed"] + delta["modified"]
        total = item_changes + delta["reordered"]
        changed_items += total
        if item_changes:
            summaries.append(
                f"{noun} +{delta['added']} / -{delta['removed']} / 修改 {delta['modified']}"
            )
        if delta["reordered"]:
            summaries.append(f"{noun}顺序变化")
        if total:
            details.extend(delta["details"])
    selector_fields = (
        "profile_auto_switch_enabled", "active_profile_id", "editor_profile_id"
    )
    selector_changes = [
        field for field in selector_fields
        if _canonical(current.get(field)) != _canonical(backup.get(field))
    ]
    if selector_changes:
        changed_items += len(selector_changes)
        details.append("配置档案启用或选择状态不同")
        summaries.append(f"档案状态 {len(selector_changes)} 项")
    return {
        **definition,
        "changed": bool(changed_items),
        "change_count": changed_items,
        "summary": "；".join(summaries) if summaries else "无差异",
        "details": details,
    }


def _settings_section_diff(current, backup, definition):
    changed_fields = [
        field for field in definition["fields"]
        if _canonical(current.get(field)) != _canonical(backup.get(field))
    ]
    details = [
        f"{_FIELD_LABELS.get(field, field)}：当前 {current.get(field)!r} → "
        f"备份 {backup.get(field)!r}"
        for field in changed_fields
    ]
    return {
        **definition,
        "changed": bool(changed_fields),
        "change_count": len(changed_fields),
        "summary": f"{len(changed_fields)} 项设置不同" if changed_fields else "无差异",
        "details": details,
    }


def build_config_diff(current, backup):
    current = current or {}
    backup = backup or {}
    sections = []
    for definition in SECTION_DEFINITIONS:
        if definition["key"] == "content":
            sections.append(_content_section_diff(current, backup, definition))
        else:
            sections.append(_settings_section_diff(current, backup, definition))
    return {
        "sections": sections,
        "changed_sections": [item["key"] for item in sections if item["changed"]],
        "change_count": sum(item["change_count"] for item in sections),
    }


def merge_config_sections(current, backup, selected_sections):
    """Copy selected atomic sections from backup into current configuration."""
    selected = {str(value) for value in selected_sections or []}
    result = copy.deepcopy(current or {})
    backup = backup or {}
    for definition in SECTION_DEFINITIONS:
        if definition["key"] not in selected:
            continue
        for field in definition["fields"]:
            if field in backup:
                result[field] = copy.deepcopy(backup[field])
            else:
                result.pop(field, None)
    return result


def selected_section_labels(selected_sections):
    selected = {str(value) for value in selected_sections or []}
    return [
        definition["label"] for definition in SECTION_DEFINITIONS
        if definition["key"] in selected
    ]
