"""Trigger conflict analysis shared by configuration generation and apply."""

from PySide6.QtWidgets import QMessageBox

from config.profiles import profile_match_overlaps, profile_payload
from core.constants import ConfigState
from engine.trigger_resolver import MODIFIER_ORDER, combo_text


class TriggerConflictMixin:
    @staticmethod
    def _mapping_condition(mapping):
        if not mapping.get("condition_enabled", False):
            return None
        return (
            str(mapping.get("condition_input") or ""),
            str(mapping.get("condition_state") or "按住时"),
        )

    @classmethod
    def _entry(
        cls, modifiers, key, label, context, rule_type, mapping=None,
        control_id=None,
    ):
        return {
            "modifiers": modifiers,
            "key": key,
            "label": label,
            "context": context,
            "rule_type": rule_type,
            "condition": cls._mapping_condition(mapping or {}),
            "control_id": control_id,
        }

    @staticmethod
    def _mapping_condition_impossibility(mapping):
        """Describe a condition that can never be true on the source Down edge."""
        if not mapping.get("condition_enabled", False):
            return None
        if str(mapping.get("condition_state") or "按住时") != "松开时":
            return None
        condition_input = str(mapping.get("condition_input") or "")
        source = str(mapping.get("source") or "")
        source_modifiers = {
            item for item in str(mapping.get("source_modifiers") or "无").split("+")
            if item and item != "无"
        }
        if condition_input == source:
            return "条件键与来源主键相同；来源键按下时不可能同时处于松开状态"
        if condition_input in source_modifiers:
            return "条件键属于来源快捷键的修饰键；匹配来源快捷键时不可能处于松开状态"
        return None

    @classmethod
    def _mapping_condition_report(cls, mapping, label, prefix=""):
        reason = cls._mapping_condition_impossibility(mapping)
        if not reason:
            return None
        return {
            "severity": "error",
            "message": f"{prefix}{label} 的条件无法成立：{reason}。",
        }

    @staticmethod
    def _mapping_condition_conflict(left, right):
        """Return ``None`` when same-trigger mappings have deterministic order."""
        left_condition = left.get("condition")
        right_condition = right.get("condition")
        if left_condition is None and right_condition is None:
            return "error"
        # A conditional mapping may intentionally override an unconditional
        # fallback using the same source shortcut.
        if left_condition is None or right_condition is None:
            return None
        # The same input cannot be both held and released, so these branches are
        # mutually exclusive and can safely share one source shortcut.
        if (
            left_condition[0] == right_condition[0]
            and left_condition[1] != right_condition[1]
        ):
            return None
        if left_condition == right_condition:
            return "error"
        # Conditions on different inputs can both be true. Runtime order is
        # deterministic, but warn because only the first matching rule executes.
        return "warning"

    @classmethod
    def _same_trigger_report(cls, entry, other, prefix=""):
        if {
            entry.get("rule_type"), other.get("rule_type")
        } <= {"mapping", "preset"}:
            relation = cls._mapping_condition_conflict(entry, other)
            if relation is None:
                return None
            if relation == "warning":
                return {
                    "severity": "warning",
                    "message": (
                        f"{prefix}{entry['label']} 与 {other['label']} 使用相同快捷键 "
                        f"{combo_text(entry['modifiers'], entry['key'])}，且两个条件可能同时成立；"
                            "运行时只执行优先级最高的一条。优先级规则：条件规则优先，"
                            "同级按基础映射、预设列表顺序。"
                    ),
                }
        return {
            "severity": "error",
            "message": (
                f"{prefix}{entry['label']} 与 {other['label']} 使用相同快捷键 "
                f"{combo_text(entry['modifiers'], entry['key'])}。"
            ),
        }

    def detect_trigger_conflicts(self):
        reports = self.analyze_trigger_conflicts()
        reports.extend(self.analyze_profile_trigger_conflicts())
        reports.extend(self.analyze_profile_match_overlaps())
        return [
            item["message"] for item in reports
            if item["severity"] == "error"
        ]

    def analyze_profile_match_overlaps(self):
        reports = []
        enabled = [
            profile for profile in getattr(self, "profiles", []) or []
            if profile.get("enabled", False)
        ]
        for earlier_index, earlier in enumerate(enabled):
            earlier_name = str(earlier.get("name") or "未命名档案")
            for later in enabled[earlier_index + 1:]:
                if not profile_match_overlaps(earlier, later):
                    continue
                later_name = str(later.get("name") or "未命名档案")
                reports.append({
                    "severity": "warning",
                    "message": (
                        f"档案“{earlier_name}”与档案“{later_name}”的前台匹配条件可能重叠；"
                        f"运行时会按档案列表顺序优先使用“{earlier_name}”。"
                    ),
                })
        return reports

    def analyze_trigger_conflicts(self):
        reports = []
        entries = []
        if self.global_toggle_enabled:
            entries.append(self._entry(
                self.global_toggle_modifiers, self.global_toggle_key,
                "全局映射开关", "global", "global", control_id="toggle",
            ))
        if self.macro_pause_enabled:
            entries.append(self._entry(
                self.macro_pause_modifiers, self.macro_pause_key,
                "暂停 / 继续全部宏", "global", "global", control_id="pause",
            ))
        entries.append(self._entry(
            self.emergency_modifiers, self.emergency_key,
            "停止全部 / 急停", "global", "global", control_id="emergency",
        ))
        entries.extend([
            self._entry(
                self.recording_cancel_modifiers, self.recording_cancel_key,
                "取消录制", "recording", "recording", control_id="record_cancel",
            ),
            self._entry(
                self.recording_finish_modifiers, self.recording_finish_key,
                "完成录制", "recording", "recording", control_id="record_finish",
            ),
        ])
        self._store_editor_payload()
        base_payload = profile_payload({"payload": self.base_profile_payload})
        for index, mapping in enumerate(base_payload.get("mappings", []), 1):
            if mapping.get("enabled"):
                label = mapping.get("name") or f"基础映射 {index}"
                condition_report = self._mapping_condition_report(mapping, label)
                if condition_report is not None:
                    reports.append(condition_report)
                entries.append(self._entry(
                    mapping.get("source_modifiers", "无"),
                    mapping["source"],
                    label,
                    "runtime", "mapping", mapping,
                ))
        for index, preset in enumerate(base_payload.get("presets", []), 1):
            if preset.get("enabled"):
                label = preset.get("name") or f"预设 {index}"
                condition_report = self._mapping_condition_report(preset, label)
                if condition_report is not None:
                    reports.append(condition_report)
                entries.append(self._entry(
                    preset.get("trigger_modifiers", "无"),
                    preset["trigger"],
                    label, "runtime", "preset", preset,
                ))

        for index, entry in enumerate(entries):
            mods = entry["modifiers"]
            key = entry["key"]
            label = entry["label"]
            context = entry["context"]
            trigger = combo_text(mods, key)
            if key in MODIFIER_ORDER:
                reports.append({
                    "severity": "error",
                    "message": (
                        f"{label} 使用修饰键 {trigger} 作为触发主键。"
                        "请改用非修饰键作为主键，Ctrl / Shift / Alt 只能作为组合修饰键。"
                    ),
                })
            for other in entries[:index]:
                if key != other["key"]:
                    continue
                other_trigger = combo_text(
                    other["modifiers"], other["key"]
                )
                if {context, other["context"]} == {"global", "recording"}:
                    control_pair = {
                        entry.get("control_id"), other.get("control_id")
                    }
                    # Recording has priority in the direct-input path, while the
                    # Kanata control callback explicitly turns this exact shared
                    # shortcut into “finish recording”. This is the shipped F8
                    # default and is safe only when the whole combo is identical.
                    if (
                        control_pair == {"emergency", "record_finish"}
                        and trigger == other_trigger
                    ):
                        continue
                    reports.append({
                        "severity": "error",
                        "message": (
                            f"{label}（{trigger}）与 {other['label']}（{other_trigger}）"
                            f"不能共用主键 {key}；普通模式录制时该主键会先被"
                            "全局控制层截获。"
                        ),
                    })
                    continue
                if trigger == other_trigger:
                    if context != other["context"] and "recording" in (
                        context, other["context"]
                    ):
                        continue
                    report = self._same_trigger_report(entry, other)
                    if report is not None:
                        reports.append(report)
                else:
                    reports.append({
                        "severity": "warning",
                        "message": (
                            f"{label}（{trigger}）与 {other['label']}（{other_trigger}）"
                            f"共用主键 {key}。运行时来源映射允许临时额外修饰键，"
                            "会优先执行修饰键更具体、条件优先级更高的一条；"
                            "若不是刻意设计，建议改用不同主键。"
                        ),
                    })

        reserved = {
            "Alt+F4", "Alt+Tab", "Ctrl+Alt+Delete", "Ctrl+Shift+Esc",
            "Win+L", "Win+D", "Win+Tab",
        }
        for entry in entries:
            trigger = combo_text(entry["modifiers"], entry["key"])
            if trigger in reserved:
                reports.append({
                    "severity": "warning",
                    "message": f"{entry['label']} 使用 Windows 常见系统快捷键 {trigger}。",
                })
        return reports

    def analyze_profile_trigger_conflicts(self):
        reports = []
        global_entries = []
        if self.global_toggle_enabled:
            global_entries.append(self._entry(
                self.global_toggle_modifiers, self.global_toggle_key,
                "全局映射开关", "global", "global", control_id="toggle",
            ))
        if self.macro_pause_enabled:
            global_entries.append(self._entry(
                self.macro_pause_modifiers, self.macro_pause_key,
                "暂停 / 继续全部宏", "global", "global", control_id="pause",
            ))
        global_entries.append(self._entry(
            self.emergency_modifiers, self.emergency_key,
            "停止全部 / 急停", "global", "global", control_id="emergency",
        ))
        for profile in self.profiles:
            if not profile.get("enabled", False):
                continue
            profile_name = str(profile.get("name") or "未命名档案")
            payload = profile_payload(profile)
            entries = list(global_entries)
            for index, mapping in enumerate(payload.get("mappings", []), 1):
                if mapping.get("enabled"):
                    label = mapping.get("name") or f"基础映射 {index}"
                    condition_report = self._mapping_condition_report(
                        mapping, label, prefix=f"档案“{profile_name}”中 "
                    )
                    if condition_report is not None:
                        reports.append(condition_report)
                    entries.append(self._entry(
                        mapping.get("source_modifiers", "无"),
                        mapping.get("source", "F6"),
                        label,
                        "runtime", "mapping", mapping,
                    ))
            for index, preset in enumerate(payload.get("presets", []), 1):
                if preset.get("enabled"):
                    label = preset.get("name") or f"预设 {index}"
                    condition_report = self._mapping_condition_report(
                        preset, label, prefix=f"档案“{profile_name}”中 "
                    )
                    if condition_report is not None:
                        reports.append(condition_report)
                    entries.append(self._entry(
                        preset.get("trigger_modifiers", "无"),
                        preset.get("trigger", "F1"),
                        label, "runtime", "preset", preset,
                    ))
            for index, entry in enumerate(entries):
                trigger = combo_text(entry["modifiers"], entry["key"])
                if entry["context"] == "runtime" and entry["key"] in MODIFIER_ORDER:
                    reports.append({
                        "severity": "error",
                        "message": (
                            f"档案“{profile_name}”中 {entry['label']} 使用修饰键 {trigger} "
                            "作为触发主键。请改用非修饰键作为主键，"
                            "Ctrl / Shift / Alt 只能作为组合修饰键。"
                        ),
                    })
                for other in entries[:index]:
                    if entry["key"] != other["key"]:
                        continue
                    other_trigger = combo_text(
                        other["modifiers"], other["key"]
                    )
                    if trigger == other_trigger:
                        report = self._same_trigger_report(
                            entry, other, prefix=f"档案“{profile_name}”中 "
                        )
                        if report is not None:
                            reports.append(report)
                    elif (
                        entry["context"] == "runtime"
                        or other["context"] == "runtime"
                    ):
                        reports.append({
                            "severity": "warning",
                            "message": (
                                f"档案“{profile_name}”中 {entry['label']}（{trigger}）与 "
                                f"{other['label']}（{other_trigger}）共用主键 {entry['key']}。"
                                "来源映射允许临时额外修饰键，会优先执行修饰键更具体、"
                                "条件优先级更高的一条。"
                            ),
                        })
        return reports

    def confirm_trigger_conflict_report(self):
        reports = self.analyze_trigger_conflicts()
        reports.extend(self.analyze_profile_trigger_conflicts())
        reports.extend(self.analyze_profile_match_overlaps())
        errors = [item["message"] for item in reports if item["severity"] == "error"]
        warnings = [item["message"] for item in reports if item["severity"] == "warning"]
        if errors:
            QMessageBox.warning(
                self, "快捷键冲突",
                "以下冲突必须修正后才能应用：\n\n" + "\n".join(f"• {x}" for x in errors),
            )
            return False
        if warnings:
            if getattr(self, "_auto_apply_in_progress", False):
                self.config_state = ConfigState.DIRTY
                if hasattr(self, "engine_hint"):
                    self.engine_hint.setStyleSheet("color: #fbbf24;")
                    self.engine_hint.setText(
                        "存在快捷键或档案匹配风险，自动应用已暂停；"
                        "请手动点击“应用更改”确认"
                    )
                if hasattr(self, "auto_apply_timer"):
                    self.auto_apply_timer.stop()
                if hasattr(self, "reload_button"):
                    self.reload_button.setEnabled(True)
                if hasattr(self, "write_diagnostic"):
                    self.write_diagnostic(
                        "auto_apply_trigger_warning_deferred",
                        force=True,
                        warnings=warnings,
                    )
                if hasattr(self, "refresh_status_ui"):
                    self.refresh_status_ui()
                return False
            answer = QMessageBox.question(
                self, "快捷键风险提示",
                "检测到以下潜在风险：\n\n"
                + "\n".join(f"• {x}" for x in warnings)
                + "\n\n仍要继续应用吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return answer == QMessageBox.StandardButton.Yes
        return True
