"""Basic mapping-card editing and runtime deletion isolation."""

from __future__ import annotations

import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout,
)

from config.schema import MAX_MAPPINGS_PER_SCOPE
from core.constants import *
from engine.trigger_resolver import combo_text
from ui.editors import HotkeyEdit


class MappingEditorMixin:
    def add_mapping(self, rule=None):
        if len(self.mapping_cards) >= MAX_MAPPINGS_PER_SCOPE:
            QMessageBox.warning(
                self, "无法添加映射",
                f"当前配置的映射数量已达到上限 {MAX_MAPPINGS_PER_SCOPE}。",
            )
            return None
        rule = dict(rule or {
            "id": uuid.uuid4().hex,
            "enabled": False,
            "name": f"未配置映射 {len(self.mapping_cards) + 1}",
            "source_modifiers": "无", "source": "F6",
            "target_modifiers": "无", "target": "鼠标左键",
            "condition_enabled": False,
            "condition_input": "鼠标左键",
            "condition_state": "按住时",
            "mode": "同步按住", "hold_ms": 100,
            "hold_jitter_ms": 0,
            "loop_count": 1, "loop_interval_ms": 0,
            "loop_interval_jitter_ms": 0,
            "speed_percent": 100, "max_runtime_s": 0,
        })
        if rule.get("mode") == "单次触发":
            rule["mode"] = "执行一次"

        card = QFrame()
        card.setObjectName("mappingCard")
        card.mapping_id = rule.get("id") or uuid.uuid4().hex
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 7, 12, 7)
        card_layout.setSpacing(5)

        summary = QHBoxLayout()
        summary.setSpacing(7)
        card.enabled = QCheckBox("启用")
        card.enabled.setChecked(bool(rule.get("enabled", False)))
        # Keep the enable area as compact as the preset card. Without an
        # explicit width/stretch sink, QHBoxLayout may assign most spare width
        # to this first widget and leave a long empty clickable strip.
        card.enabled.setFixedWidth(78)
        summary.addWidget(card.enabled)

        card.name = QLineEdit(rule.get("name", ""))
        card.name.setPlaceholderText(
            f"未配置映射 {len(self.mapping_cards) + 1}"
        )
        card.name.setFixedWidth(200)
        name_group = self.labeled_control("方案名称", card.name)
        name_group.setFixedWidth(210)
        summary.addWidget(name_group)

        card.source_hotkey = HotkeyEdit(
            rule.get("source_modifiers", "无"),
            rule.get("source", "F6"),
            SOURCE_NAMES,
            allow_condition=True,
            condition_enabled=bool(rule.get("condition_enabled", False)),
            condition_key=rule.get("condition_input", "鼠标左键"),
            condition_state=rule.get("condition_state", "按住时"),
            condition_options=CONDITION_INPUT_NAMES,
        )
        card.source_hotkey.setFixedWidth(185)
        source_group = self.labeled_control("来源快捷键", card.source_hotkey)
        source_group.setFixedWidth(195)
        summary.addWidget(source_group)

        arrow = QLabel("→")
        arrow.setObjectName("mappingArrow")
        arrow.setAlignment(Qt.AlignCenter)
        arrow.setFixedWidth(18)
        summary.addWidget(arrow)

        card.target_hotkey = HotkeyEdit(
            rule.get("target_modifiers", "无"),
            rule.get("target", "鼠标左键"),
            INPUT_NAMES,
        )
        card.target_hotkey.setFixedWidth(185)
        target_group = self.labeled_control("目标快捷键", card.target_hotkey)
        target_group.setFixedWidth(195)
        summary.addWidget(target_group)

        card.mode = self.combo(
            MAPPING_MODES, rule.get("mode", "同步按住"), lambda *_: None
        )
        summary.addWidget(self.labeled_control("执行模式", card.mode, 125))

        card.priority_label = QLabel("优先级：—")
        card.priority_label.setObjectName("muted")
        card.priority_label.setFixedWidth(86)
        card.priority_label.setToolTip(
            "同一来源快捷键内：条件映射优先；同级按列表顺序；只执行第一条满足条件的映射。"
        )
        summary.addWidget(card.priority_label)

        card.parameters_button = QPushButton("参数 ▸")
        card.parameters_button.setObjectName("collapseButton")
        card.parameters_button.setCheckable(True)
        card.parameters_button.setFixedWidth(82)
        summary.addWidget(card.parameters_button)
        # Match the preset header: all spare horizontal space is consumed by a
        # dedicated stretch instead of expanding the enable checkbox/fields.
        summary.addStretch(1)

        delete = QPushButton("删除")
        delete.setObjectName("dangerGhost")
        delete.setFixedWidth(66)
        delete.clicked.connect(
            lambda _checked=False, c=card: self.delete_mapping(c)
        )
        summary.addWidget(delete)
        card_layout.addLayout(summary)

        card.parameter_panel = QFrame()
        card.parameter_panel.setObjectName("parameterArea")
        settings = QHBoxLayout(card.parameter_panel)
        settings.setContentsMargins(10, 6, 10, 6)
        settings.setSpacing(6)

        card.hold = QSpinBox()
        card.hold.setRange(1, 600_000)
        card.hold.setSingleStep(10)
        card.hold.setSuffix(" ms")
        card.hold.setValue(int(rule.get("hold_ms", 100)))
        card.hold.setToolTip("每次目标按键或鼠标动作保持的时间")
        card.hold_group = self.labeled_control("动作按住", card.hold)
        settings.addWidget(card.hold_group)

        card.hold_jitter = QSpinBox()
        card.hold_jitter.setRange(0, 600_000)
        card.hold_jitter.setSingleStep(10)
        card.hold_jitter.setSuffix(" ms")
        card.hold_jitter.setSpecialValueText("固定")
        card.hold_jitter.setValue(int(rule.get("hold_jitter_ms", 0)))
        card.hold_jitter.setToolTip("每次动作按住时间随机上下浮动的最大范围")
        card.hold_jitter_group = self.labeled_control("按住随机 ±", card.hold_jitter)
        settings.addWidget(card.hold_jitter_group)

        card.loop_count = QSpinBox()
        card.loop_count.setRange(1, 100000)
        card.loop_count.setValue(int(rule.get("loop_count", 1)))
        card.count_group = self.labeled_control("执行次数", card.loop_count)
        settings.addWidget(card.count_group)

        card.loop_interval = QSpinBox()
        card.loop_interval.setRange(0, 600000)
        card.loop_interval.setSuffix(" ms")
        card.loop_interval.setValue(int(rule.get("loop_interval_ms", 0)))
        card.interval_group = self.labeled_control("轮间隔", card.loop_interval)
        settings.addWidget(card.interval_group)

        card.loop_interval_jitter = QSpinBox()
        card.loop_interval_jitter.setRange(0, 600000)
        card.loop_interval_jitter.setSuffix(" ms")
        card.loop_interval_jitter.setSpecialValueText("固定")
        card.loop_interval_jitter.setValue(
            int(rule.get("loop_interval_jitter_ms", 0))
        )
        card.loop_interval_jitter.setToolTip("每轮间隔随机上下浮动的最大范围")
        card.interval_jitter_group = self.labeled_control(
            "间隔随机 ±", card.loop_interval_jitter
        )
        settings.addWidget(card.interval_jitter_group)

        # 保留旧配置中的速度值以保证兼容。基础映射的保持时间和轮间隔
        # 已可直接设置，因此不再在界面中重复显示速度参数。
        card.speed = QSpinBox()
        card.speed.setRange(10, 500)
        card.speed.setSuffix(" %")
        card.speed.setValue(int(rule.get("speed_percent", 100)))
        card.speed_group = self.labeled_control("执行速度", card.speed)
        settings.addWidget(card.speed_group)

        card.max_runtime = QSpinBox()
        card.max_runtime.setRange(0, 86400)
        card.max_runtime.setSuffix(" 秒")
        card.max_runtime.setSpecialValueText("不限")
        card.max_runtime.setValue(int(rule.get("max_runtime_s", 0)))
        card.runtime_group = self.labeled_control("最长运行", card.max_runtime)
        settings.addWidget(card.runtime_group)
        settings.addStretch(1)
        card_layout.addWidget(card.parameter_panel)

        self.mapping_cards.append(card)
        self.mapping_layout.insertWidget(
            max(0, self.mapping_layout.count() - 1), card
        )

        def changed(*_args):
            if hasattr(self, "refresh_mapping_filters"):
                self.refresh_mapping_filters()
            self.data_changed()
        card.enabled.stateChanged.connect(changed)
        card.name.textChanged.connect(changed)
        card.source_hotkey.changed.connect(changed)
        card.target_hotkey.changed.connect(changed)
        card.hold.valueChanged.connect(changed)
        card.hold_jitter.valueChanged.connect(changed)
        card.loop_count.valueChanged.connect(changed)
        card.loop_interval.valueChanged.connect(changed)
        card.loop_interval_jitter.valueChanged.connect(changed)
        card.speed.valueChanged.connect(changed)
        card.max_runtime.valueChanged.connect(changed)
        card.mode.currentTextChanged.connect(
            lambda text, c=card: self.on_mapping_mode_changed(c, text)
        )
        card.parameters_button.toggled.connect(
            lambda expanded, c=card: self.toggle_mapping_parameters(c, expanded)
        )

        self.refresh_mapping_parameter_panel(card)
        self.refresh_mapping_priority_labels()
        if (
            not getattr(self, "initializing", False)
            and hasattr(self, "refresh_mapping_filters")
        ):
            self.refresh_mapping_filters()
        self._loading_checkpoint()
        self.data_changed()


    def refresh_mapping_priority_labels(self):
        """Show deterministic priority for mappings sharing one source combo."""
        cards = list(getattr(self, "mapping_cards", []) or [])
        snapshots = []
        for index, card in enumerate(cards):
            label = getattr(card, "priority_label", None)
            if label is None:
                continue
            source_modifiers, source_key = card.source_hotkey.value()
            condition_enabled, _condition_input, _condition_state = (
                card.source_hotkey.condition_value()
            )
            enabled = bool(card.enabled.isChecked())
            group_key = (source_modifiers, source_key)
            snapshots.append({
                "index": index,
                "card": card,
                "label": label,
                "enabled": enabled,
                "condition_enabled": bool(condition_enabled),
                "combo": combo_text(source_modifiers, source_key),
                "group_key": group_key,
            })

        enabled_groups = {}
        for item in snapshots:
            if item["enabled"]:
                enabled_groups.setdefault(item["group_key"], []).append(item)

        for group in enabled_groups.values():
            group.sort(key=lambda item: (
                0 if item["condition_enabled"] else 1,
                item["index"],
            ))

        for item in snapshots:
            label = item["label"]
            if not item["enabled"]:
                label.setText("优先级：关闭")
                label.setToolTip("该映射未启用，不参与运行时触发优先级。")
                continue
            group = enabled_groups.get(item["group_key"], [])
            rank = group.index(item) + 1 if item in group else 1
            total = max(1, len(group))
            suffix = "条件" if item["condition_enabled"] else "回退"
            label.setText(f"优先级：{rank}/{total}")
            label.setToolTip(
                f"来源：{item['combo']}\n"
                f"类型：{suffix}\n"
                "同一来源快捷键内：条件映射优先；同级按列表顺序；只执行第一条满足条件的映射。"
            )

    def on_mapping_mode_changed(self, card, _mode):
        self.refresh_mapping_parameter_panel(card)
        self.data_changed()

    def toggle_mapping_parameters(self, card, expanded):
        mode = card.mode.currentText()
        can_expand = mode != "同步按住"
        visible = bool(expanded and can_expand)
        card.parameter_panel.setVisible(visible)
        if can_expand:
            card.parameters_button.setText("参数 ▾" if visible else "参数 ▸")
        else:
            card.parameters_button.setText("无需参数")

    def refresh_mapping_parameter_panel(self, card):
        mode = card.mode.currentText()
        visible_groups = {
            "同步按住": set(),
            "执行一次": {"hold", "hold_jitter"},
            "固定次数": {
                "hold", "hold_jitter", "count", "interval", "interval_jitter"
            },
            "按住循环": {"hold", "hold_jitter", "interval", "interval_jitter"},
            "开关循环": {
                "hold", "hold_jitter", "interval", "interval_jitter", "runtime"
            },
            "无限循环": {
                "hold", "hold_jitter", "interval", "interval_jitter", "runtime"
            },
        }.get(mode, {"hold"})

        groups = {
            "hold": card.hold_group,
            "hold_jitter": card.hold_jitter_group,
            "count": card.count_group,
            "interval": card.interval_group,
            "interval_jitter": card.interval_jitter_group,
            "speed": card.speed_group,
            "runtime": card.runtime_group,
        }
        for name, group in groups.items():
            group.setVisible(name in visible_groups)

        if mode == "同步按住":
            card.parameters_button.blockSignals(True)
            card.parameters_button.setChecked(False)
            card.parameters_button.blockSignals(False)
            card.parameters_button.setEnabled(False)
            card.parameters_button.setText("无需参数")
            card.parameter_panel.hide()
        else:
            card.parameters_button.setEnabled(True)
            self.toggle_mapping_parameters(
                card, card.parameters_button.isChecked()
            )

    def _suspend_mapping_runtime_for_delete(self, mapping):
        """Immediately isolate a mapping before its unapplied card is removed."""
        mapping_id = str(mapping.get("id") or "")
        if not mapping_id:
            return None
        direct_kanata = (
            mapping.get("source") in MOUSE_NAMES
            or mapping.get("mode", "同步按住") == "同步按住"
            or mapping.get("condition_enabled", False)
        )
        snapshot = {
            "profile_id": self._visible_editor_profile_id(),
            "mappings": [],
            "rules": [],
            "direct_kanata": direct_kanata,
            "was_running": bool(self.running),
        }

        # Mouse-source, synchronous and conditional mappings are compiled into
        # the live Kanata layer, so Python-side table removal cannot disable
        # them.  Complete the full stop transaction before changing any editor
        # or runtime state.  If stop fails, the mapping remains visible and its
        # runtime tables remain intact.
        if direct_kanata and self.running and not self._runtime_is_game_mode():
            stopped = self.set_running(
                False, allow_owned_mouse_force_release=True
            )
            if stopped is False or self.running:
                self.write_diagnostic(
                    "mapping_delete_aborted_stop_failed",
                    force=True,
                    mapping_id=mapping_id,
                    error=(
                        self.engine.last_command_error
                        or self.keyboard_engine.last_command_error
                    ),
                )
                return None
            self.restart_engine_after_apply = True
            self.engine_hint.setStyleSheet("color: #fbbf24;")
            self.engine_hint.setText(
                "已删除 Kanata 直接映射；输入引擎已安全停止，请应用更改后重新启动"
            )

        self.suspended_mapping_ids.add(mapping_id)
        with self.data_lock:
            for index, item in enumerate(self.runtime_mappings):
                if str(item.get("id") or "") == mapping_id:
                    snapshot["mappings"].append((index, item))
            for index, rule in enumerate(self.runtime_trigger_rules):
                if (
                    rule.get("_runtime_kind", "mapping") == "mapping"
                    and str(rule.get("id") or "") == mapping_id
                ):
                    snapshot["rules"].append((index, rule))
            self.runtime_mappings = [
                item for item in self.runtime_mappings
                if str(item.get("id") or "") != mapping_id
            ]
            self.runtime_trigger_rules = [
                rule for rule in self.runtime_trigger_rules
                if not (
                    rule.get("_runtime_kind", "mapping") == "mapping"
                    and str(rule.get("id") or "") == mapping_id
                )
            ]

        mapping_task_id = f"mapping:{mapping_id}"
        stop_task = getattr(self, "_request_stop_macro_task", None)
        if callable(stop_task):
            stop_task(mapping_task_id, "正在停止被删除映射的运行任务")
        else:
            self.macro_controller.stop(mapping_task_id)
        pending_sync_releases = []
        normalized_id = "".join(
            character.lower() for character in mapping_id if character.isalnum()
        )
        with self.input_state_lock:
            for trigger_token in list(self.held_trigger_ids):
                task_ids = self.held_trigger_ids.get(trigger_token)
                if not task_ids:
                    self.held_trigger_ids.pop(trigger_token, None)
                    continue
                task_ids.discard(mapping_task_id)
                if not task_ids:
                    self.held_trigger_ids.pop(trigger_token, None)

            for trigger_token in list(self.active_sync_by_source):
                active = self.active_sync_by_source.get(trigger_token, {})
                held = active.pop(mapping_id, None)
                if held is not None:
                    pending_sync_releases.append(
                        (trigger_token, mapping_id, held)
                    )
                if not active:
                    self.active_sync_by_source.pop(trigger_token, None)

            self.kanata_trigger_down = {
                token for token in self.kanata_trigger_down
                if not token.endswith(f":mapping:{normalized_id}")
            }
        release_detached = getattr(
            self, "_release_detached_sync_mappings", None
        )
        if callable(release_detached):
            release_detached(pending_sync_releases)
        else:
            # Lightweight editor test harnesses may not include the input
            # runtime mixin. Keep the same ownership-preserving fallback.
            for trigger_token, held_mapping_id, held in pending_sync_releases:
                if self._release_sync_mapping(held):
                    continue
                with self.input_state_lock:
                    self.active_sync_by_source.setdefault(
                        trigger_token, {}
                    ).setdefault(held_mapping_id, held)
        return snapshot

    def _restore_suspended_mapping_runtime(self, mapping_id, snapshot):
        """Undo the immediate runtime isolation when deletion is discarded."""
        if not snapshot:
            return False
        mapping_id = str(mapping_id or "")
        self.suspended_mapping_ids.discard(mapping_id)
        with self.data_lock:
            for index, item in sorted(snapshot.get("mappings", [])):
                if not any(
                    str(current.get("id") or "") == mapping_id
                    for current in self.runtime_mappings
                ):
                    self.runtime_mappings.insert(
                        min(index, len(self.runtime_mappings)), item
                    )
            for index, rule in sorted(snapshot.get("rules", [])):
                if not any(
                    current.get("_runtime_kind", "mapping") == "mapping"
                    and str(current.get("id") or "") == mapping_id
                    for current in self.runtime_trigger_rules
                ):
                    self.runtime_trigger_rules.insert(
                        min(index, len(self.runtime_trigger_rules)), rule
                    )
        if snapshot.get("direct_kanata") and snapshot.get("was_running"):
            self.restart_engine_after_apply = True
            return True
        return False

    def _restore_discarded_mapping_deletions(self, profile_id):
        restart_needed = False
        profile_id = str(profile_id or "")
        for mapping_id, snapshot in list(self.pending_mapping_deletions.items()):
            if str(snapshot.get("profile_id") or "") != profile_id:
                continue
            restart_needed = bool(
                self._restore_suspended_mapping_runtime(mapping_id, snapshot)
                or restart_needed
            )
            self.pending_mapping_deletions.pop(mapping_id, None)
        return restart_needed

    def delete_mapping(self, card):
        if self._configuration_change_blocked_by_recording():
            return
        if card not in self.mapping_cards:
            return
        mapping = next(
            (
                item for item in self.collect_mappings()
                if str(item.get("id") or "") == str(card.mapping_id or "")
            ),
            {"id": str(card.mapping_id or "")},
        )
        mapping_name = str(mapping.get("name") or "未命名映射").strip()
        stops_engine = bool(
            self.running
            and not self._runtime_is_game_mode()
            and (
                mapping.get("source") in MOUSE_NAMES
                or mapping.get("mode", "同步按住") == "同步按住"
                or mapping.get("condition_enabled", False)
            )
        )
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("确认删除映射")
        confirm.setText(f"确定删除映射“{mapping_name}”吗？")
        detail = "确认后，该映射会立即从当前运行触发中暂停。"
        if stops_engine:
            detail += "\n\n此映射由 Kanata 直接处理，删除会立即停止输入引擎；应用更改后才能重新启动。"
        confirm.setInformativeText(detail)
        delete_button = confirm.addButton(
            "删除", QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_button = confirm.addButton(
            "取消", QMessageBox.ButtonRole.RejectRole
        )
        confirm.setDefaultButton(cancel_button)
        confirm.exec()
        if confirm.clickedButton() is not delete_button:
            return

        snapshot = self._suspend_mapping_runtime_for_delete(mapping)
        if snapshot is None:
            return
        self.pending_mapping_deletions[str(mapping.get("id") or "")] = snapshot
        self.mapping_cards.remove(card)
        self.mapping_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
        self.data_changed()
        if hasattr(self, "refresh_mapping_filters"):
            self.refresh_mapping_filters()

    @staticmethod
    def update_mapping_mode_fields(*_args):
        # 兼容旧调用；新版由 refresh_mapping_parameter_panel 控制显示。
        return

    @staticmethod
    def update_execution_mode_fields(*_args):
        # 兼容旧调用；新版由 refresh_preset_parameter_panel 控制显示。
        return
