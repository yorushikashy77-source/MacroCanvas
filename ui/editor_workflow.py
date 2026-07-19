"""映射、预设与动作树编辑流程。"""

from __future__ import annotations

import copy
import json
import re
import uuid
import weakref

from PySide6.QtCore import QEvent, QSize, QTimer, Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from config.schema import (
    MAX_ACTION_COUNT, MAX_MAPPINGS_PER_SCOPE, MAX_PRESETS_PER_SCOPE,
)
from config.storage import atomic_write_text
from config.transfer import remap_action_ids
from config.profiles import normalize_profile, profile_payload
from core.constants import *
from engine.kanata import (
    KanataConfigBuilder, interception_keyboard_hwids, interception_mouse_hwids,
)
from macro.actions import clone_action_tree, iter_action_tree
from macro.recording import simplify_recorded_actions
from macro.parameters import (
    ACTION_PARAMETER_FIELDS, FIELD_LABELS, MAX_PRESET_PARAMETERS,
    PARAMETER_DURATION, PARAMETER_INPUT, PARAMETER_TYPES,
    coerce_parameter_value, value_matches_action_field,
)
from ui.editors import (
    ActionDurationEditor, ActionTargetEditor, ActionTreeWidget, HotkeyEdit,
)


ACTION_RECORDING_CONTEXT_ROLE = ACTION_ID_ROLE + 1
ACTION_LEGACY_MODIFIERS_ROLE = ACTION_RECORDING_CONTEXT_ROLE + 1
ACTION_BRANCH_TYPE_ROLE = ACTION_LEGACY_MODIFIERS_ROLE + 1
ACTION_PARAMETER_BINDINGS_ROLE = ACTION_BRANCH_TYPE_ROLE + 1
ACTION_PARAMETER_VALUES_ROLE = ACTION_PARAMETER_BINDINGS_ROLE + 1


def _select_editor_table_cell(editor, table):
    viewport = table.viewport()
    position = editor.mapTo(viewport, editor.rect().center())
    index = table.indexAt(position)
    if index.isValid():
        table.setCurrentCell(index.row(), index.column())


class _TableCellLineEdit(QLineEdit):
    """A permanent cell editor that also selects its containing row."""

    def __init__(self, text, table):
        super().__init__(text)
        self._table_ref = weakref.ref(table)

    def focusInEvent(self, event):
        table = self._table_ref()
        if table is not None:
            _select_editor_table_cell(self, table)
        super().focusInEvent(event)


def _select_editor_table_cell_from_refs(editor_ref, table_ref):
    editor = editor_ref()
    table = table_ref()
    if editor is not None and table is not None:
        _select_editor_table_cell(editor, table)


class EditorWorkflowMixin:
    @staticmethod
    def _preset_parameter_definitions(card):
        return copy.deepcopy(getattr(card, "parameter_definitions", []) or [])

    def _card_for_action_table(self, table):
        return next(
            (
                card for card in getattr(self, "preset_cards", [])
                if getattr(card, "action_table", None) is table
            ),
            None,
        )

    def _update_action_variable_marker(self, item, card=None):
        if item is None or self.is_condition_branch_item(item):
            return
        card = card or self._card_for_action_table(item.treeWidget())
        bindings = dict(item.data(0, ACTION_PARAMETER_BINDINGS_ROLE) or {})
        values = dict(item.data(0, ACTION_PARAMETER_VALUES_ROLE) or {})
        action_id = str(item.data(0, ACTION_ID_ROLE) or "")
        preset_id = str(getattr(card, "preset_id", "") or "")
        key = (preset_id, action_id)
        breakpoints = set(getattr(self, "runtime_debug_breakpoints", set()) or set())
        current = dict(getattr(self, "runtime_debug_current_action", {}) or {})
        base = (
            "循环卡片" if self.is_loop_action_item(item)
            else "子动作" if item.parent() is not None else "动作"
        )
        markers = []
        if (
            preset_id
            and action_id
            and str(current.get("source_preset_id") or "") == preset_id
            and str(current.get("action_id") or "") == action_id
        ):
            markers.append("▶")
        if preset_id and action_id and key in breakpoints:
            markers.append("●")
        if bindings or values:
            markers.append("◇")
        item.setText(0, base + (" " + "".join(markers) if markers else ""))
        original_tip = str(item.toolTip(0) or "")
        for section in ("\n变量：", "\n调试："):
            original_tip = original_tip.split(section, 1)[0]
        details = []
        if bindings:
            details.append(f"已绑定 {len(bindings)} 个字段")
        if values:
            details.append(f"已覆盖 {len(values)} 个子宏变量")
        debug_details = []
        if key in breakpoints:
            debug_details.append("本次会话已设置断点")
        if markers and markers[0] == "▶":
            debug_details.append("当前正在执行")
        tip = original_tip
        if details:
            tip += "\n变量：" + "；".join(details)
        if debug_details:
            tip += "\n调试：" + "；".join(debug_details)
        item.setToolTip(0, tip)

    def edit_preset_variables(self, card):
        """Edit named defaults declared by one reusable preset."""
        dialog = QDialog(getattr(card, "action_dialog", None) or self)
        dialog.setWindowTitle("定义预设变量")
        dialog.resize(610, 420)
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "变量让同一预设在被子宏调用时使用不同的按键、次数或时长。"
            "名称在当前预设内不能重复；重命名或删除变量会清理原有引用。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["变量名称", "类型", "默认值"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(table, 1)

        def add_row(definition=None):
            active_rows = [
                row for row in range(table.rowCount())
                if not table.isRowHidden(row)
            ]
            if len(active_rows) >= MAX_PRESET_PARAMETERS:
                QMessageBox.information(
                    dialog, "已达到上限",
                    f"每个预设最多可定义 {MAX_PRESET_PARAMETERS} 个变量。",
                )
                return
            definition = dict(definition or {})
            row = table.rowCount()
            table.insertRow(row)
            name = _TableCellLineEdit(
                str(definition.get("name") or ""), table
            )
            name.setPlaceholderText("变量名称")
            table.setCellWidget(row, 0, name)
            # Keep the stock focus handling: selecting the table cell from a
            # combo's focusInEvent interrupts mouse commits in its popup list.
            kind = QComboBox()
            kind.addItems(PARAMETER_TYPES)
            kind.setCurrentText(str(definition.get("type") or PARAMETER_INPUT))
            kind.activated.connect(
                lambda _index, editor_ref=weakref.ref(kind),
                table_ref=weakref.ref(table):
                _select_editor_table_cell_from_refs(editor_ref, table_ref)
            )
            table.setCellWidget(row, 1, kind)
            default = _TableCellLineEdit(
                str(definition.get("default", "A")), table
            )
            default.setPlaceholderText("默认值")
            table.setCellWidget(row, 2, default)
            # Permanent editors avoid Qt 6.11's unsafe delegate-editor teardown
            # when the dialog closes, while the adaptive row height keeps the
            # styled controls from being clipped at different font scales.
            table.setRowHeight(
                row,
                max(
                    kind.sizeHint().height() + 8,
                    name.sizeHint().height() + 8,
                    default.sizeHint().height() + 8,
                    table.fontMetrics().height() + 24,
                ),
            )

        for definition in self._preset_parameter_definitions(card):
            add_row(definition)
        row_buttons = QHBoxLayout()
        add_button = QPushButton("＋ 添加变量")
        add_button.setObjectName("secondary")
        add_button.clicked.connect(lambda _checked=False: add_row())
        remove_button = QPushButton("删除选中")
        remove_button.setObjectName("dangerGhost")

        def remove_selected_row():
            row = table.currentRow()
            if row < 0 or table.isRowHidden(row):
                return
            # Removing a row while one of its permanent editors owns focus can
            # make Qt 6.11 destroy the widget twice during dialog teardown.
            # Keep it hidden until the table itself is safely destroyed.
            remove_button.setFocus(Qt.FocusReason.OtherFocusReason)
            table.setRowHidden(row, True)
            remaining = [
                candidate for candidate in range(table.rowCount())
                if not table.isRowHidden(candidate)
            ]
            if remaining:
                table.setCurrentCell(remaining[min(row, len(remaining) - 1)], 0)
            else:
                table.clearSelection()
                table.setCurrentCell(-1, -1)

        remove_button.clicked.connect(
            lambda _checked=False: remove_selected_row()
        )
        row_buttons.addWidget(add_button)
        row_buttons.addWidget(remove_button)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def accept():
            definitions = []
            names = set()
            visible_index = 0
            for row in range(table.rowCount()):
                if table.isRowHidden(row):
                    continue
                visible_index += 1
                name_editor = table.cellWidget(row, 0)
                default_editor = table.cellWidget(row, 2)
                name = str(name_editor.text() if name_editor else "").strip()
                kind = table.cellWidget(row, 1).currentText()
                if not name:
                    QMessageBox.warning(
                        dialog, "变量无效", f"第 {visible_index} 行没有名称。"
                    )
                    return
                if len(name) > 32:
                    QMessageBox.warning(dialog, "变量无效", f"变量“{name}”名称超过 32 个字符。")
                    return
                if name in names:
                    QMessageBox.warning(dialog, "变量无效", f"变量“{name}”重复。")
                    return
                try:
                    default = coerce_parameter_value(
                        kind, default_editor.text() if default_editor else ""
                    )
                except ValueError as error:
                    QMessageBox.warning(
                        dialog, "默认值无效", f"变量“{name}”：{error}"
                    )
                    return
                names.add(name)
                definitions.append({"name": name, "type": kind, "default": default})
            card.parameter_definitions = definitions
            self._sanitize_named_parameter_metadata()
            self.data_changed()
            dialog.accept()

        buttons.accepted.connect(accept)
        try:
            dialog.exec()
        finally:
            # Dispose the editor tree while the event loop is still alive.
            # Leaving several closed dialogs queued until interpreter shutdown
            # makes Qt 6.11 tear their item views down after Python finalization.
            dialog.deleteLater()
            QApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)

    def edit_selected_action_variables(self, card):
        selected = [
            item for item in self.selected_action_items(card)
            if not self.is_condition_branch_item(item)
        ]
        if len(selected) != 1:
            QMessageBox.information(
                getattr(card, "action_dialog", None) or self,
                "请选择一个动作", "请先选中一个普通动作或子宏动作。",
            )
            return
        item = selected[0]
        action = self.action_from_item(card.action_table, item)
        if action.get("type") == SUBMACRO_ACTION_TYPE:
            self._edit_submacro_parameter_values(card, item, action)
        else:
            self._edit_action_parameter_bindings(card, item, action)

    def toggle_selected_action_breakpoints(self, card):
        selected = [
            item for item in self.selected_action_items(card)
            if not self.is_condition_branch_item(item)
        ]
        if not selected:
            QMessageBox.information(
                card.action_dialog,
                "请选择动作",
                "请先选择一个或多个普通动作、控制动作或循环卡片。",
            )
            return False
        preset_id = str(getattr(card, "preset_id", "") or "")
        keys = {
            (preset_id, str(item.data(0, ACTION_ID_ROLE) or ""))
            for item in selected
            if preset_id and str(item.data(0, ACTION_ID_ROLE) or "")
        }
        if not keys:
            return False
        breakpoints = set(getattr(self, "runtime_debug_breakpoints", set()) or set())
        enabling = not keys.issubset(breakpoints)
        if enabling:
            breakpoints.update(keys)
        else:
            breakpoints.difference_update(keys)
        self.runtime_debug_breakpoints = breakpoints
        controller = getattr(self, "macro_controller", None)
        if controller is not None:
            controller.set_debug_breakpoints(breakpoints)
        for item in selected:
            self._update_action_variable_marker(item, card)
        if enabling and hasattr(self, "open_runtime_debugger"):
            self.open_runtime_debugger()
        if hasattr(self, "engine_hint"):
            self.engine_hint.setStyleSheet("")
            self.engine_hint.setText(
                f"已为 {len(keys)} 个动作{'设置' if enabling else '移除'}本次会话断点"
            )
        return True

    def clear_runtime_debug_breakpoints(self):
        """Remove session-only breakpoints and refresh their action markers."""
        breakpoints = set(
            getattr(self, "runtime_debug_breakpoints", set()) or set()
        )
        self.runtime_debug_breakpoints = set()
        controller = getattr(self, "macro_controller", None)
        if controller is not None:
            controller.set_debug_breakpoints(set())
        if not breakpoints:
            return False
        for card in getattr(self, "preset_cards", []):
            if not getattr(card, "_actions_loaded", False):
                continue
            for item in card.action_table.iter_items():
                self._update_action_variable_marker(item, card)
        return True

    def _prune_runtime_debug_breakpoints(
        self, card=None, removed_preset_id="",
    ):
        breakpoints = set(getattr(self, "runtime_debug_breakpoints", set()) or set())
        original = set(breakpoints)
        removed_preset_id = str(removed_preset_id or "")
        current = dict(getattr(self, "runtime_debug_current_action", {}) or {})
        current_key = (
            str(current.get("source_preset_id") or ""),
            str(current.get("action_id") or ""),
        )
        current_invalid = False
        if removed_preset_id:
            breakpoints = {
                key for key in breakpoints if str(key[0]) != removed_preset_id
            }
            current_invalid = current_key[0] == removed_preset_id
        elif card is not None and getattr(card, "_actions_loaded", False):
            preset_id = str(getattr(card, "preset_id", "") or "")
            valid_ids = {
                str(item.data(0, ACTION_ID_ROLE) or "")
                for item in card.action_table.iter_items()
                if str(item.data(0, ACTION_ID_ROLE) or "")
            }
            breakpoints = {
                key for key in breakpoints
                if str(key[0]) != preset_id or str(key[1]) in valid_ids
            }
            current_invalid = (
                current_key[0] == preset_id
                and bool(current_key[1])
                and current_key[1] not in valid_ids
            )
        if breakpoints == original and not current_invalid:
            return False
        self.runtime_debug_breakpoints = breakpoints
        controller = getattr(self, "macro_controller", None)
        if controller is not None and breakpoints != original:
            controller.set_debug_breakpoints(breakpoints)
        if current_invalid:
            if hasattr(self, "_set_runtime_debug_current_action"):
                self._set_runtime_debug_current_action({})
            else:
                self.runtime_debug_current_action = {}
        return True

    def _edit_action_parameter_bindings(self, card, item, action):
        fields = ACTION_PARAMETER_FIELDS.get(action.get("type"), {})
        definitions = self._preset_parameter_definitions(card)
        compatible = {
            field: [
                definition for definition in definitions
                if value_matches_action_field(
                    action.get("type"), field, definition.get("type"),
                    definition.get("default"),
                )
            ]
            for field in fields
        }
        if not fields or not any(compatible.values()):
            QMessageBox.information(
                card.action_dialog, "没有可用变量",
                "当前动作没有可绑定字段，或本预设尚未定义类型匹配的变量。\n"
                "可先点击“定义变量”添加默认值。",
            )
            return
        dialog = QDialog(card.action_dialog)
        dialog.setWindowTitle("绑定动作变量")
        form = QFormLayout(dialog)
        current = dict(item.data(0, ACTION_PARAMETER_BINDINGS_ROLE) or {})
        controls = {}
        for field in fields:
            combo = QComboBox()
            combo.addItem("不使用变量", "")
            for definition in compatible[field]:
                combo.addItem(definition["name"], definition["name"])
            selected = combo.findData(str(current.get(field) or ""))
            combo.setCurrentIndex(max(0, selected))
            literal = action.get(field, "")
            form.addRow(f"{FIELD_LABELS.get(field, field)}（当前 {literal}）", combo)
            controls[field] = combo
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        bindings = {
            field: str(combo.currentData())
            for field, combo in controls.items() if combo.currentData()
        }
        item.setData(0, ACTION_PARAMETER_BINDINGS_ROLE, bindings)
        if self.is_loop_action_item(item):
            data = dict(item.data(0, LOOP_DATA_ROLE) or {})
            data["parameter_bindings"] = bindings
            item.setData(0, LOOP_DATA_ROLE, data)
        self._update_action_variable_marker(item, card)
        self.action_changed(card)

    def _edit_submacro_parameter_values(self, card, item, action):
        target_id = str(action.get("preset_id") or "")
        target_card = next(
            (other for other in self.preset_cards if other.preset_id == target_id),
            None,
        )
        definitions = self._preset_parameter_definitions(target_card) if target_card else []
        if not definitions:
            QMessageBox.information(
                card.action_dialog, "目标没有变量",
                "当前子宏目标尚未定义可覆盖的变量。",
            )
            return
        dialog = QDialog(card.action_dialog)
        dialog.setWindowTitle("设置子宏变量")
        form = QFormLayout(dialog)
        current = dict(item.data(0, ACTION_PARAMETER_VALUES_ROLE) or {})
        usages = {}
        target_actions = (
            self.collect_visible_actions(target_card) if target_card is not None else []
        )
        for target_action in iter_action_tree(target_actions):
            action_specs = ACTION_PARAMETER_FIELDS.get(
                target_action.get("type"), {}
            )
            for field, parameter_name in (
                target_action.get("parameter_bindings", {}) or {}
            ).items():
                spec = action_specs.get(field)
                if spec is not None:
                    usages.setdefault(str(parameter_name), []).append(spec)
        controls = {}
        for definition in definitions:
            name = definition["name"]
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            enabled = QCheckBox("覆盖默认值")
            enabled.setChecked(name in current)
            if definition["type"] == PARAMETER_INPUT:
                editor = QComboBox()
                allowed = set(INPUT_NAMES)
                for _kind, _minimum, _maximum, choices in usages.get(name, []):
                    if choices is not None:
                        allowed.intersection_update(choices)
                editor.addItems([value for value in INPUT_NAMES if value in allowed])
                editor.setCurrentText(str(current.get(name, definition["default"])))
            else:
                editor = QSpinBox()
                minimum = 0 if definition["type"] == PARAMETER_DURATION else 1
                maximum = (
                    600_000 if definition["type"] == PARAMETER_DURATION else 100_000
                )
                for _kind, field_minimum, field_maximum, _choices in usages.get(name, []):
                    if field_minimum is not None:
                        minimum = max(minimum, field_minimum)
                    if field_maximum is not None:
                        maximum = min(maximum, field_maximum)
                editor.setRange(minimum, maximum)
                editor.setSuffix(" ms" if definition["type"] == PARAMETER_DURATION else "")
                editor.setValue(int(current.get(name, definition["default"])))
            editor.setEnabled(enabled.isChecked())
            enabled.toggled.connect(editor.setEnabled)
            row_layout.addWidget(enabled)
            row_layout.addWidget(editor, 1)
            form.addRow(f"{name}（{definition['type']}）", row)
            controls[name] = (enabled, editor, definition)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = {}
        for name, (enabled, editor, definition) in controls.items():
            if not enabled.isChecked():
                continue
            raw = editor.currentText() if isinstance(editor, QComboBox) else editor.value()
            values[name] = coerce_parameter_value(definition["type"], raw)
        item.setData(0, ACTION_PARAMETER_VALUES_ROLE, values)
        self._update_action_variable_marker(item, card)
        self.action_changed(card)

    def _sanitize_named_parameter_metadata(self):
        """Drop stale bindings/overrides after declarations or targets change."""
        definitions_by_id = {
            card.preset_id: {
                item["name"]: item for item in self._preset_parameter_definitions(card)
            }
            for card in getattr(self, "preset_cards", [])
        }
        for card in getattr(self, "preset_cards", []):
            own = definitions_by_id.get(card.preset_id, {})
            if not getattr(card, "_actions_loaded", True):
                actions = clone_action_tree(getattr(card, "_pending_actions", []) or [])
                for action in iter_action_tree(actions):
                    action["parameter_bindings"] = {
                        field: name for field, name in
                        (action.get("parameter_bindings", {}) or {}).items()
                        if (
                            name in own
                            and value_matches_action_field(
                                action.get("type"), field,
                                own[name].get("type"), own[name].get("default"),
                            )
                        )
                    }
                    if not action["parameter_bindings"]:
                        action.pop("parameter_bindings", None)
                    if action.get("type") == SUBMACRO_ACTION_TYPE:
                        target = definitions_by_id.get(str(action.get("preset_id") or ""), {})
                        valid_values = {}
                        for name, value in (action.get("parameter_values", {}) or {}).items():
                            definition = target.get(name)
                            if definition is None:
                                continue
                            try:
                                valid_values[name] = coerce_parameter_value(
                                    definition["type"], value
                                )
                            except ValueError:
                                continue
                        action["parameter_values"] = valid_values
                        if not action["parameter_values"]:
                            action.pop("parameter_values", None)
                card._pending_actions = actions
                continue
            for item in card.action_table.iter_items():
                action_type = (
                    LOOP_ACTION_TYPE if self.is_loop_action_item(item) else ""
                )
                kind_editor = card.action_table.itemWidget(item, 1)
                if not action_type and kind_editor is not None and hasattr(kind_editor, "currentText"):
                    action_type = kind_editor.currentText()
                bindings = {
                    field: name for field, name in
                    (item.data(0, ACTION_PARAMETER_BINDINGS_ROLE) or {}).items()
                    if (
                        name in own
                        and value_matches_action_field(
                            action_type, field, own[name].get("type"),
                            own[name].get("default"),
                        )
                    )
                }
                item.setData(0, ACTION_PARAMETER_BINDINGS_ROLE, bindings)
                if self.is_loop_action_item(item):
                    data = dict(item.data(0, LOOP_DATA_ROLE) or {})
                    if bindings:
                        data["parameter_bindings"] = bindings
                    else:
                        data.pop("parameter_bindings", None)
                    item.setData(0, LOOP_DATA_ROLE, data)
                if action_type == SUBMACRO_ACTION_TYPE:
                    target_editor = card.action_table.itemWidget(item, 2)
                    target = definitions_by_id.get(str(target_editor.currentText() or ""), {})
                    values = {}
                    for name, value in (item.data(0, ACTION_PARAMETER_VALUES_ROLE) or {}).items():
                        definition = target.get(name)
                        if definition is None:
                            continue
                        try:
                            values[name] = coerce_parameter_value(
                                definition["type"], value
                            )
                        except ValueError:
                            continue
                    item.setData(0, ACTION_PARAMETER_VALUES_ROLE, values)
                self._update_action_variable_marker(item, card)

    @staticmethod
    def semantic_action_count(actions):
        return sum(
            1 for action in iter_action_tree(actions)
            if action.get("type") not in CONDITION_BRANCH_TYPES
        )

    def table_semantic_action_count(self, table):
        return sum(
            1 for item in table.iter_items()
            if not self.is_condition_branch_item(item)
        )

    @staticmethod
    def is_condition_branch_item(item):
        return bool(
            item is not None
            and item.data(0, ACTION_BRANCH_TYPE_ROLE) in CONDITION_BRANCH_TYPES
        )

    @staticmethod
    def normalize_condition_action_branches(action):
        """Wrap legacy true-only children in two fixed branch containers."""
        copied = dict(action or {})
        if copied.get("type") != CONDITION_ACTION_TYPE:
            return copied
        children = list(copied.get("children", []) or [])
        by_type = {
            child.get("type"): dict(child)
            for child in children
            if isinstance(child, dict)
            and child.get("type") in CONDITION_BRANCH_TYPES
        }
        if by_type:
            true_branch = by_type.get(CONDITION_TRUE_BRANCH_TYPE, {
                "type": CONDITION_TRUE_BRANCH_TYPE, "children": [],
            })
            else_branch = by_type.get(CONDITION_ELSE_BRANCH_TYPE, {
                "type": CONDITION_ELSE_BRANCH_TYPE, "children": [],
            })
        else:
            true_branch = {
                "type": CONDITION_TRUE_BRANCH_TYPE,
                "children": children,
            }
            else_branch = {
                "type": CONDITION_ELSE_BRANCH_TYPE,
                "children": [],
            }
        for branch in (true_branch, else_branch):
            branch["action_id"] = str(
                branch.get("action_id") or uuid.uuid4().hex
            )
            branch["children"] = list(branch.get("children", []) or [])
        copied["children"] = [true_branch, else_branch]
        return copied

    @staticmethod
    def _condition_branch_child(item, branch_type):
        if item is None:
            return None
        for index in range(item.childCount()):
            child = item.child(index)
            if child.data(0, ACTION_BRANCH_TYPE_ROLE) == branch_type:
                return child
        return None

    @staticmethod
    def update_condition_branch_summary(table, item):
        if item is None or item.data(0, ACTION_BRANCH_TYPE_ROLE) not in CONDITION_BRANCH_TYPES:
            return
        label = table.itemWidget(item, 3)
        if isinstance(label, QLabel):
            label.setText(
                f"{item.childCount()} 个直接动作 · 选中后可继续添加"
            )

    def _submacro_preset_options(self, card=None):
        return [
            (str(other.preset_id), other.name.text().strip() or "未命名预设")
            for other in getattr(self, "preset_cards", [])
            if other is not card and str(getattr(other, "preset_id", "") or "")
        ]

    def _default_submacro_target(self, card=None):
        options = self._submacro_preset_options(card)
        return options[0][0] if options else ""

    def add_submacro_action_from_menu(self, card=None):
        target_id = self._default_submacro_target(card)
        if not target_id:
            QMessageBox.information(
                getattr(card, "action_dialog", None) or self,
                "暂无可调用的子宏",
                "请先在当前配置方案中再添加一个预设，"
                "然后返回此处添加“调用子宏”动作。",
            )
            return None
        return self.add_action_from_menu({
            "type": SUBMACRO_ACTION_TYPE,
            "preset_id": target_id,
            "repeat_count": 1,
            "speed_percent": 100,
            "children": [],
        }, card=card)

    def refresh_submacro_target_editors(self, card):
        if card is None or not getattr(card, "_actions_loaded", False):
            return
        options = self._submacro_preset_options(card)
        table = card.action_table
        for item in table.iter_items():
            if (
                self.is_loop_action_item(item)
                or self.is_condition_branch_item(item)
            ):
                continue
            kind = table.itemWidget(item, 1)
            target = table.itemWidget(item, 2)
            if (
                hasattr(kind, "currentText")
                and hasattr(target, "set_submacro_options")
                and kind.currentText() == SUBMACRO_ACTION_TYPE
            ):
                target.set_submacro_options(
                    options, target.currentText(), emit=False
                )

    @staticmethod
    def labeled_control(title, control, stretch=0):
        holder = QWidget()
        holder.setObjectName("fieldGroup")
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(1)
        label = QLabel(title)
        label.setObjectName("fieldLabel")
        box.addWidget(label)
        box.addWidget(control)
        if stretch:
            holder.setMinimumWidth(stretch)
        return holder

    @staticmethod
    def setup_table(table, labels):
        table.setColumnCount(len(labels))
        table.setHorizontalHeaderLabels(labels)
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setFocusPolicy(Qt.StrongFocus)

    @staticmethod
    def checkbox_widget(checked, callback):
        checkbox = QCheckBox()
        checkbox.setChecked(checked)
        checkbox.stateChanged.connect(callback)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(14, 0, 8, 0)
        row.addWidget(checkbox)
        return holder

    @staticmethod
    def combo(options, current, callback):
        widget = QComboBox()
        widget.addItems(options)
        widget.setCurrentText(current)
        widget.currentTextChanged.connect(callback)
        return widget


    def clear_action_table(self):
        # 兼容旧调用：统一走清空动作路径，以便大型动作表显示加载卡片，
        # 并确保“应用更改”立即进入未保存状态。
        if self.action_table is not None:
            self.clear_visible_actions(self.selected_preset_card)

    def load_selected_actions(self):
        if self.selected_preset_card:
            self.select_preset_card(self.selected_preset_card)

    @staticmethod
    def _action_item_path(table, item):
        if item is None:
            return None
        path = []
        current = item
        while current.parent() is not None:
            parent = current.parent()
            path.append(parent.indexOfChild(current))
            current = parent
        root_index = table.indexOfTopLevelItem(current)
        if root_index < 0:
            return None
        path.append(root_index)
        return tuple(reversed(path))

    @staticmethod
    def _action_at_path(actions, path):
        if not path:
            return None
        sequence = actions
        node = None
        for index in path:
            if not 0 <= index < len(sequence):
                return None
            node = sequence[index]
            sequence = node.setdefault("children", [])
        return node

    @staticmethod
    def _action_parent_list(actions, path):
        if not path:
            return None
        sequence = actions
        for index in path[:-1]:
            if not 0 <= index < len(sequence):
                return None
            sequence = sequence[index].setdefault("children", [])
        return sequence

    @classmethod
    def _find_action_path(cls, actions, target):
        def visit(sequence, prefix):
            for index, action in enumerate(sequence):
                path = prefix + (index,)
                if action is target:
                    return path
                found = visit(action.get("children", []), path)
                if found is not None:
                    return found
            return None

        return visit(actions, ())

    @staticmethod
    def _item_at_path(table, path):
        if not path:
            return None
        item = table.topLevelItem(path[0])
        for index in path[1:]:
            if item is None or not 0 <= index < item.childCount():
                return None
            item = item.child(index)
        return item

    @staticmethod
    def _repolish_widget(widget):
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def _set_loop_point_button_state(self, card, stage):
        card.loop_point_stage = int(stage)
        button = card.loop_points_button
        if stage == 0:
            button.setText("插入循环点位")
            button.setObjectName("loopActionButton")
            button.setToolTip(
                "依次选择同一层级的开始动作和结束动作；循环卡片会添加到方案最下方，不移动原动作"
            )
        elif stage == 1:
            button.setText("选择开始点…")
            button.setObjectName("loopActionSelecting")
            button.setToolTip("请点击动作行左侧的“层级 / 拖拽”区域；再次点击此按钮可取消")
        elif stage == 2:
            button.setText("选择结束点…")
            button.setObjectName("loopActionSelecting")
            button.setToolTip("请选择与开始点处于同一层级的结束动作；再次点击此按钮可取消")
        else:
            button.setText("添加循环动作")
            button.setObjectName("loopActionReady")
            button.setToolTip("在方案最下方添加一个引用该范围的循环卡片；原动作保持原位")
        self._repolish_widget(button)

    def reset_loop_point_selection(self, card):
        if card is None or not hasattr(card, "action_table"):
            return
        card.loop_start_item = None
        card.loop_end_item = None
        card.action_table.clear_loop_points()
        self._set_loop_point_button_state(card, 0)

    def loop_points_button_clicked(self, card):
        if card not in self.preset_cards:
            return
        self.select_preset_card(card)
        stage = getattr(card, "loop_point_stage", 0)
        if stage == 3:
            self.add_loop_action_from_points(card)
            return
        if stage in (1, 2):
            self.reset_loop_point_selection(card)
            return
        card.loop_start_item = None
        card.loop_end_item = None
        card.action_table.set_loop_point_mode(True, None, None)
        self._set_loop_point_button_state(card, 1)

    @staticmethod
    def _action_sibling_index(table, item):
        if item is None:
            return -1
        parent = item.parent()
        return (
            table.indexOfTopLevelItem(item)
            if parent is None else parent.indexOfChild(item)
        )

    def select_loop_point(self, card, item):
        if card not in self.preset_cards or item is None:
            return
        if self.is_loop_action_item(item):
            QMessageBox.information(
                card.action_dialog,
                "无法选择循环卡片",
                "循环卡片不能作为新的循环开始点或结束点。请选择普通动作。",
            )
            return
        if self.is_condition_branch_item(item):
            QMessageBox.information(
                card.action_dialog,
                "无法选择分支容器",
                "请选择“条件成立”或“否则”分支里的普通动作。",
            )
            return
        stage = getattr(card, "loop_point_stage", 0)
        if stage == 1:
            card.loop_start_item = item
            card.loop_end_item = None
            card.action_table.set_loop_point_mode(True, item, None)
            self._set_loop_point_button_state(card, 2)
            return
        if stage != 2 or card.loop_start_item is None:
            return
        if item.parent() is not card.loop_start_item.parent():
            QMessageBox.information(
                card.action_dialog,
                "循环点层级不一致",
                "循环开始点和结束点必须位于同一层级。\n"
                "可以先调整动作层级，再重新选择结束点。",
            )
            return
        table = card.action_table
        first = card.loop_start_item
        second = item
        first_index = self._action_sibling_index(table, first)
        second_index = self._action_sibling_index(table, second)
        if first_index < 0 or second_index < 0:
            self.reset_loop_point_selection(card)
            return
        if second_index < first_index:
            first, second = second, first
        card.loop_start_item = first
        card.loop_end_item = second
        # Selection is complete: restore normal row interaction while retaining
        # both visible boundary markers until the user confirms the insertion.
        card.action_table.set_loop_point_mode(False, first, second)
        self._set_loop_point_button_state(card, 3)

    def _next_loop_action_number(self, actions):
        highest = 0
        for action in iter_action_tree(actions):
            if action.get("type") != LOOP_ACTION_TYPE:
                continue
            highest = max(highest, int(action.get("sequence_number", 0) or 0))
            match = re.fullmatch(r"循环项目(\d+)", str(action.get("name", "")))
            if match:
                highest = max(highest, int(match.group(1)))
        return highest + 1

    def add_loop_action_from_points(self, card):
        if card not in self.preset_cards:
            return
        start_item = getattr(card, "loop_start_item", None)
        end_item = getattr(card, "loop_end_item", None)
        if start_item is None or end_item is None:
            self.reset_loop_point_selection(card)
            return
        table = card.action_table
        start_path = self._action_item_path(table, start_item)
        end_path = self._action_item_path(table, end_item)
        if (
            not start_path or not end_path
            or start_path[:-1] != end_path[:-1]
        ):
            self.reset_loop_point_selection(card)
            return
        actions = self.collect_visible_actions(card)
        siblings = self._action_parent_list(actions, start_path)
        if siblings is None:
            self.reset_loop_point_selection(card)
            return
        first_index, last_index = sorted((start_path[-1], end_path[-1]))
        if not (0 <= first_index <= last_index < len(siblings)):
            self.reset_loop_point_selection(card)
            return
        selected_actions = siblings[first_index:last_index + 1]
        if any(action.get("type") == LOOP_ACTION_TYPE for action in selected_actions):
            QMessageBox.information(
                card.action_dialog,
                "循环范围无效",
                "循环范围内不能包含其他循环卡片。请选择连续的普通动作。",
            )
            return
        target_action_ids = []
        for action in selected_actions:
            action_id = str(action.get("action_id") or uuid.uuid4().hex)
            action["action_id"] = action_id
            target_action_ids.append(action_id)

        selected_id_set = set(target_action_ids)
        for existing_loop in actions:
            if existing_loop.get("type") != LOOP_ACTION_TYPE:
                continue
            existing_ids = set(existing_loop.get("target_action_ids", []) or [])
            if selected_id_set & existing_ids:
                QMessageBox.information(
                    card.action_dialog,
                    "循环范围重叠",
                    "当前范围与已有循环项目重叠。请为多个循环项目选择互不重叠的动作范围。",
                )
                return

        sequence_number = self._next_loop_action_number(actions)
        parent_item = start_item.parent()
        loop_action = {
            "id": uuid.uuid4().hex,
            "type": LOOP_ACTION_TYPE,
            "sequence_number": sequence_number,
            "name": f"循环项目{sequence_number}",
            "execution_mode": "执行次数",
            "loop_count": 2,
            "loop_interval_ms": 0,
            "loop_interval_jitter_ms": 0,
            "speed_percent": 100,
            "max_runtime_s": 0,
            "timeline_mode": (
                "parallel"
                if parent_item is not None else "sequential"
            ),
            "target_action_ids": target_action_ids,
            "color_index": (sequence_number - 1) % len(LOOP_COLOR_THEMES),
            "children": [],
        }
        # The loop card is a separate control item at the very end of the preset.
        # It only references the selected actions and never removes or reparents them.
        actions.append(loop_action)
        new_path = (len(actions) - 1,)
        self.reset_loop_point_selection(card)
        self.load_actions(actions, card)
        selected = self._item_at_path(card.action_table, new_path)
        if selected is not None:
            card.action_table.setCurrentItem(selected)
            selected.setSelected(True)
            card.action_table.scrollToItem(
                selected, QAbstractItemView.ScrollHint.PositionAtCenter
            )
        self.action_changed(card)

    def handle_action_drop(self, card, source_item, target_item, position):
        """Move ordinary actions without allowing loop cards to own or reorder them."""
        if card not in self.preset_cards or source_item is None:
            return
        if self.is_loop_action_item(source_item):
            return
        if self.is_condition_branch_item(source_item):
            return
        self.reset_loop_point_selection(card)
        table = card.action_table

        ancestor = target_item
        while ancestor is not None:
            if ancestor is source_item:
                return
            ancestor = ancestor.parent()

        actions = self.collect_visible_actions(card)
        source_path = self._action_item_path(table, source_item)
        target_path = self._action_item_path(table, target_item)
        source_action = self._action_at_path(actions, source_path)
        target_action = self._action_at_path(actions, target_path)
        if source_action is None or source_action is target_action:
            return
        source_parent = self._action_parent_list(actions, source_path)
        if source_parent is None:
            return
        source_parent.pop(source_path[-1])

        on_item = QAbstractItemView.DropIndicatorPosition.OnItem
        above_item = QAbstractItemView.DropIndicatorPosition.AboveItem
        below_item = QAbstractItemView.DropIndicatorPosition.BelowItem
        on_viewport = QAbstractItemView.DropIndicatorPosition.OnViewport

        # Loop cards are fixed control rows at the end. Dropping on one places the
        # ordinary action immediately before the first loop card, never inside it.
        first_loop_index = next(
            (i for i, action in enumerate(actions)
             if action.get("type") == LOOP_ACTION_TYPE),
            len(actions),
        )
        new_path = None
        target_path = self._find_action_path(actions, target_action)
        target_type = target_action.get("type") if target_action else ""
        if target_action is not None and target_type == LOOP_ACTION_TYPE:
            actions.insert(first_loop_index, source_action)
            new_path = (first_loop_index,)
        elif position == on_viewport or target_action is None or target_path is None:
            actions.insert(first_loop_index, source_action)
            new_path = (first_loop_index,)
        elif target_type == CONDITION_ACTION_TYPE and position == on_item:
            branches = target_action.get("children", []) or []
            true_branch = next(
                (
                    branch for branch in branches
                    if branch.get("type") == CONDITION_TRUE_BRANCH_TYPE
                ),
                None,
            )
            if true_branch is None:
                return
            children = true_branch.setdefault("children", [])
            children.append(source_action)
            branch_path = self._find_action_path(actions, true_branch)
            new_path = branch_path + (len(children) - 1,)
        elif target_type in CONDITION_BRANCH_TYPES:
            children = target_action.setdefault("children", [])
            insert_at = 0 if position == above_item else len(children)
            children.insert(insert_at, source_action)
            branch_path = self._find_action_path(actions, target_action)
            new_path = branch_path + (insert_at,)
        elif position == on_item:
            children = target_action.setdefault("children", [])
            children.append(source_action)
            new_path = target_path + (len(children) - 1,)
        elif position in (above_item, below_item):
            target_parent = self._action_parent_list(actions, target_path)
            if target_parent is None:
                actions.insert(first_loop_index, source_action)
                new_path = (first_loop_index,)
            else:
                insert_at = target_path[-1]
                if position == below_item:
                    insert_at += 1
                target_parent.insert(insert_at, source_action)
                new_path = target_path[:-1] + (insert_at,)
        else:
            actions.insert(first_loop_index, source_action)
            new_path = (first_loop_index,)

        self.load_actions(actions, card)
        selected = self._item_at_path(card.action_table, new_path)
        if selected is not None:
            card.action_table.setCurrentItem(selected)
            selected.setSelected(True)
            card.action_table.scrollToItem(
                selected, QAbstractItemView.ScrollHint.PositionAtCenter
            )
        self.action_changed(card)

    @staticmethod
    def normalize_loop_action_data(action):
        raw_mode = action.get("execution_mode", "执行次数")
        mode = (
            "执行次数"
            if raw_mode in ("执行次数", "固定次数", "执行一次")
            else "无限循环"
        )
        name = str(action.get("name") or "循环项目")
        name_match = re.fullmatch(r"循环项目(\d+)", name)
        sequence_number = max(
            1,
            int(
                action.get("sequence_number")
                or (name_match.group(1) if name_match else 1)
            ),
        )
        target_ids = []
        seen = set()
        for value in action.get("target_action_ids", []) or []:
            value = str(value)
            if value and value not in seen:
                seen.add(value)
                target_ids.append(value)
        normalized = {
            "id": str(action.get("id") or uuid.uuid4().hex),
            "type": LOOP_ACTION_TYPE,
            "sequence_number": sequence_number,
            "name": name,
            "execution_mode": mode,
            "loop_count": max(1, int(action.get("loop_count", 2))),
            "loop_interval_ms": max(0, int(action.get("loop_interval_ms", 0))),
            "loop_interval_jitter_ms": max(
                0, int(action.get("loop_interval_jitter_ms", 0))
            ),
            "speed_percent": max(10, min(500, int(action.get("speed_percent", 100)))),
            "max_runtime_s": max(0, int(action.get("max_runtime_s", 0))),
            "timeline_mode": (
                "parallel"
                if action.get("timeline_mode") == "parallel" else "sequential"
            ),
            "target_action_ids": target_ids,
            "color_index": int(
                action.get("color_index", sequence_number - 1)
            ) % len(LOOP_COLOR_THEMES),
        }
        bindings = dict(action.get("parameter_bindings", {}) or {})
        if bindings:
            normalized["parameter_bindings"] = bindings
        return normalized

    @staticmethod
    def is_loop_action_item(item):
        return bool(item is not None and item.data(0, LOOP_TYPE_ROLE) == LOOP_ACTION_TYPE)

    def update_loop_action_summary(self, card, item):
        if not self.is_loop_action_item(item):
            return
        data = dict(item.data(0, LOOP_DATA_ROLE) or {})
        mode_editor = card.action_table.itemWidget(item, 2)
        mode = mode_editor.currentText() if mode_editor is not None else data.get(
            "execution_mode", "执行次数"
        )
        host = card.action_table.itemWidget(item, 3)
        if host is None or not hasattr(host, "summary_label"):
            return
        target_count = len(data.get("target_action_ids", []) or [])
        interval = max(0, int(data.get("loop_interval_ms", 0)))
        if mode == "执行次数":
            text = (
                f"{max(1, int(data.get('loop_count', 2)))} 次 · "
                f"引用 {target_count} 项 · 间隔 {interval} ms"
            )
        else:
            runtime = max(0, int(data.get("max_runtime_s", 0)))
            text = f"无限循环 · 引用 {target_count} 项 · 间隔 {interval} ms"
            if runtime:
                text += f" · 最长 {runtime} 秒"
        host.summary_label.setText(text)

    def on_loop_action_name_changed(self, card, item, text):
        if not self.is_loop_action_item(item):
            return
        data = dict(item.data(0, LOOP_DATA_ROLE) or {})
        data["name"] = text.strip() or "循环项目"
        item.setData(0, LOOP_DATA_ROLE, data)
        self.action_changed(card)

    def on_loop_action_mode_changed(self, card, item, mode):
        if not self.is_loop_action_item(item):
            return
        data = dict(item.data(0, LOOP_DATA_ROLE) or {})
        data["execution_mode"] = mode
        if mode == "执行次数":
            data["max_runtime_s"] = 0
        item.setData(0, LOOP_DATA_ROLE, data)
        self.update_loop_action_summary(card, item)
        self.action_changed(card)

    def edit_loop_action_parameters(self, card, item):
        if not self.is_loop_action_item(item):
            return
        data = dict(item.data(0, LOOP_DATA_ROLE) or {})
        mode_editor = card.action_table.itemWidget(item, 2)
        mode = mode_editor.currentText() if mode_editor is not None else data.get(
            "execution_mode", "执行次数"
        )
        dialog = QDialog(card.action_dialog)
        dialog.setWindowTitle(f"循环参数 · {data.get('name', '循环项目')}")
        dialog.setMinimumWidth(430)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()

        count = QSpinBox()
        count.setRange(1, 100000)
        count.setValue(max(1, int(data.get("loop_count", 2))))
        interval = QSpinBox()
        interval.setRange(0, 600000)
        interval.setSuffix(" ms")
        interval.setValue(max(0, int(data.get("loop_interval_ms", 0))))
        interval_jitter = QSpinBox()
        interval_jitter.setRange(0, 600000)
        interval_jitter.setSuffix(" ms")
        interval_jitter.setSpecialValueText("固定")
        interval_jitter.setValue(
            max(0, int(data.get("loop_interval_jitter_ms", 0)))
        )
        speed = QSpinBox()
        speed.setRange(10, 500)
        speed.setSuffix(" %")
        speed.setValue(max(10, min(500, int(data.get("speed_percent", 100)))))
        runtime = QSpinBox()
        runtime.setRange(0, 86400)
        runtime.setSuffix(" 秒")
        runtime.setSpecialValueText("不限")
        runtime.setValue(max(0, int(data.get("max_runtime_s", 0))))

        if mode == "执行次数":
            form.addRow("执行次数", count)
        form.addRow("轮间隔", interval)
        form.addRow("间隔随机 ±", interval_jitter)
        form.addRow("执行速度", speed)
        if mode == "无限循环":
            form.addRow("最长运行", runtime)
        layout.addLayout(form)
        hint = QLabel(
            "循环卡片只引用选定动作，不会移动、收纳或删除原动作。删除循环卡片也不会影响被引用动作。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data.update({
            "execution_mode": mode,
            "loop_count": count.value(),
            "loop_interval_ms": interval.value(),
            "loop_interval_jitter_ms": interval_jitter.value(),
            "speed_percent": speed.value(),
            "max_runtime_s": runtime.value() if mode == "无限循环" else 0,
        })
        item.setData(0, LOOP_DATA_ROLE, data)
        self.update_loop_action_summary(card, item)
        self.action_changed(card)

    @staticmethod
    def _loop_theme(data):
        index = int(data.get("color_index", 0)) % len(LOOP_COLOR_THEMES)
        return LOOP_COLOR_THEMES[index]

    def _style_loop_action_row(self, table, item, data):
        theme = self._loop_theme(data)
        for column in range(table.columnCount()):
            item.setBackground(column, QColor(theme["background"]))
            item.setForeground(column, QColor(theme["text"]))
        item.setToolTip(
            0,
            "独立循环卡片：引用所选动作，不拥有子动作；删除本卡片不会删除原动作。",
        )
        for column in (1, 2, 3, 4):
            widget = table.itemWidget(item, column)
            if widget is None:
                continue
            widget.setStyleSheet(
                f"background: {theme['control']}; "
                f"color: {theme['text']}; "
                f"border: 1px solid {theme['accent']}; "
                "border-radius: 7px;"
            )

    def _action_insert_position_after_current(self, card):
        table = getattr(card, "action_table", None)
        if table is None:
            return None, None
        current = table.currentItem()
        if current is None:
            return None, None
        if self.is_condition_branch_item(current):
            return current, current.childCount()
        if (
            table.itemWidget(current, 1) is not None
            and hasattr(table.itemWidget(current, 1), "currentText")
            and table.itemWidget(current, 1).currentText()
            == CONDITION_ACTION_TYPE
        ):
            true_branch = self._condition_branch_child(
                current, CONDITION_TRUE_BRANCH_TYPE
            )
            if true_branch is not None:
                return true_branch, true_branch.childCount()

        parent = current.parent()
        if parent is not None and not self.is_loop_action_item(current):
            index = parent.indexOfChild(current)
            return parent, index + 1 if index >= 0 else None

        index = table.indexOfTopLevelItem(current)
        if index < 0:
            return None, None
        # 循环卡片固定在预设末尾。若当前选中循环卡片，普通动作放到
        # 该循环卡片前方，避免破坏“循环项目始终在末尾”的旧语义。
        return None, index if self.is_loop_action_item(current) else index + 1

    def add_action_from_menu(self, action=None, card=None):
        card = card or self.selected_preset_card
        parent_item, insert_index = self._action_insert_position_after_current(card)
        return self.add_action(
            action, card=card, parent_item=parent_item, insert_index=insert_index
        )

    def add_action(
        self, action=None, save=True, card=None, parent_item=None,
        insert_index=None,
    ):
        card = card or self.selected_preset_card
        if card is None:
            QMessageBox.information(self, "提示", "请先添加或选择一个预设方案。")
            return None
        self.select_preset_card(card)
        if self.table_semantic_action_count(card.action_table) >= MAX_ACTION_COUNT:
            QMessageBox.warning(
                getattr(card, "action_dialog", None) or self,
                "无法添加动作",
                f"单个预设的动作数量已达到上限 {MAX_ACTION_COUNT}。",
            )
            return None
        action = dict(action or {
            "type": "键盘点击", "target": "A", "hold_ms": 100,
            "jitter_ms": 0, "children": [],
        })
        action = self.normalize_condition_action_branches(action)
        table = card.action_table
        item = QTreeWidgetItem()
        is_loop = action.get("type") == LOOP_ACTION_TYPE
        is_condition_branch = action.get("type") in CONDITION_BRANCH_TYPES
        if is_loop:
            parent_item = None
            insert_index = None
        elif parent_item is None:
            first_loop_index = next(
                (index for index in range(table.topLevelItemCount())
                 if self.is_loop_action_item(table.topLevelItem(index))),
                table.topLevelItemCount(),
            )
            if insert_index is None or insert_index > first_loop_index:
                insert_index = first_loop_index
        if is_loop:
            item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        elif is_condition_branch:
            item.setFlags(
                Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDropEnabled
            )
        else:
            item.setFlags(
                item.flags() | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled
                | Qt.ItemIsSelectable | Qt.ItemIsEnabled
            )
        item.setSizeHint(0, QSize(0, 44))
        item.setData(0, ACTION_ID_ROLE, str(action.get("action_id") or uuid.uuid4().hex))
        legacy_modifiers = str(action.get("modifiers") or "无")
        if legacy_modifiers not in MODIFIER_OPTIONS:
            legacy_modifiers = "无"
        item.setData(0, ACTION_LEGACY_MODIFIERS_ROLE, legacy_modifiers)
        item.setData(
            0, ACTION_PARAMETER_BINDINGS_ROLE,
            copy.deepcopy(action.get("parameter_bindings", {}) or {}),
        )
        item.setData(
            0, ACTION_PARAMETER_VALUES_ROLE,
            copy.deepcopy(action.get("parameter_values", {}) or {}),
        )
        recording_context = action.get("recording_context")
        if (
            action.get("type") == "鼠标移动"
            and isinstance(recording_context, dict)
        ):
            item.setData(0, ACTION_RECORDING_CONTEXT_ROLE, {
                "context": copy.deepcopy(recording_context),
                "target": str(action.get("target") or ""),
            })
        item.setText(
            0,
            "循环卡片" if is_loop
            else (
                "成立分支" if action.get("type") == CONDITION_TRUE_BRANCH_TYPE
                else "否则分支" if is_condition_branch
                else "子动作" if parent_item is not None else "动作"
            )
        )
        item.setToolTip(
            0,
            "循环卡片固定在方案末尾" if is_loop
            else "分支容器固定在条件动作下" if is_condition_branch
            else "按住此处拖拽，可改变顺序或层级",
        )
        if parent_item is not None:
            if insert_index is None:
                parent_item.addChild(item)
            else:
                parent_item.insertChild(insert_index, item)
            parent_item.setExpanded(True)
        else:
            if insert_index is None:
                table.addTopLevelItem(item)
            else:
                table.insertTopLevelItem(insert_index, item)

        changed = lambda *_args, c=card: self.action_changed(c)
        if is_loop:
            data = self.normalize_loop_action_data(action)
            item.setData(0, LOOP_TYPE_ROLE, LOOP_ACTION_TYPE)
            item.setData(0, LOOP_DATA_ROLE, data)
            name_editor = QLineEdit(data["name"])
            name_editor.setPlaceholderText("循环项目名称")
            name_editor.textChanged.connect(
                lambda text, c=card, i=item:
                self.on_loop_action_name_changed(c, i, text)
            )
            table.setItemWidget(item, 1, name_editor)

            mode_editor = self.combo(
                LOOP_EXECUTION_MODES, data["execution_mode"], lambda *_: None
            )
            mode_editor.currentTextChanged.connect(
                lambda text, c=card, i=item:
                self.on_loop_action_mode_changed(c, i, text)
            )
            table.setItemWidget(item, 2, mode_editor)

            parameter_host = QWidget()
            parameter_layout = QHBoxLayout(parameter_host)
            parameter_layout.setContentsMargins(0, 0, 0, 0)
            parameter_layout.setSpacing(8)
            parameter_host.summary_label = QLabel()
            parameter_host.summary_label.setObjectName("muted")
            parameter_button = QPushButton("参数 ▸")
            parameter_button.setObjectName("secondary")
            parameter_button.setFixedWidth(78)
            parameter_button.clicked.connect(
                lambda _checked=False, c=card, i=item:
                self.edit_loop_action_parameters(c, i)
            )
            parameter_layout.addWidget(parameter_host.summary_label)
            parameter_layout.addStretch(1)
            parameter_layout.addWidget(parameter_button)
            table.setItemWidget(item, 3, parameter_host)
            self.update_loop_action_summary(card, item)
        elif is_condition_branch:
            branch_type = action.get("type")
            item.setData(0, ACTION_BRANCH_TYPE_ROLE, branch_type)
            branch_name = (
                "条件成立" if branch_type == CONDITION_TRUE_BRANCH_TYPE else "否则"
            )
            name_label = QLabel(branch_name)
            name_label.setObjectName("sectionLabel")
            target_label = QLabel(
                "条件匹配时执行" if branch_type == CONDITION_TRUE_BRANCH_TYPE
                else "条件不匹配时执行"
            )
            target_label.setObjectName("muted")
            summary_label = QLabel(
                f"{len(action.get('children', []) or [])} 个直接动作 · 选中后可继续添加"
            )
            summary_label.setObjectName("muted")
            table.setItemWidget(item, 1, name_label)
            table.setItemWidget(item, 2, target_label)
            table.setItemWidget(item, 3, summary_label)
        else:
            kind = self.combo(ACTION_TYPES, action.get("type", "键盘点击"), changed)
            target = ActionTargetEditor(
                action.get("type", "键盘点击"),
                (
                    action.get("preset_id", "")
                    if action.get("type") == SUBMACRO_ACTION_TYPE
                    else action.get("condition_input", action.get("target", "A"))
                ),
                preset_options=self._submacro_preset_options(card),
            )
            if legacy_modifiers != "无":
                target.setToolTip(
                    f"兼容旧配置：执行时同时按住 {legacy_modifiers}；"
                    "该组合会按固定顺序整体按下和松开。"
                )
            target.changed.connect(changed)
            kind.currentTextChanged.connect(
                lambda text, editor=target, c=card:
                self.update_action_target(editor, text, card=c)
            )
            table.setItemWidget(item, 1, kind)
            table.setItemWidget(item, 2, target)
            self.update_action_target(
                target, kind.currentText(), action.get("target"),
                notify=False, card=card,
            )

            if action.get("type") == WAIT_CONDITION_ACTION_TYPE:
                duration_value = int(action.get("timeout_ms", 0))
            elif action.get("type") == SUBMACRO_ACTION_TYPE:
                duration_value = int(action.get("repeat_count", 1))
            elif action.get("type") == "等待":
                duration_value = int(action.get("wait_ms", action.get("delay_ms", 500)))
            elif action.get("type") == "鼠标滚轮":
                duration_value = int(action.get("steps", 1))
            else:
                duration_value = int(action.get("hold_ms", 100))
            duration = ActionDurationEditor(
                action.get("type", "键盘点击"), duration_value,
                int(action.get("jitter_ms", 0)),
            )
            if action.get("type") in (
                CONDITION_ACTION_TYPE, WAIT_CONDITION_ACTION_TYPE,
            ):
                duration.setConditionState(
                    action.get("condition_state", "按住时"), emit=False
                )
            if action.get("type") == SUBMACRO_ACTION_TYPE:
                duration.setCallSpeedValue(
                    action.get("speed_percent", 100), emit=False
                )
            if action.get("type") == "鼠标移动":
                duration.setMoveText(target.moveCoordinate(), emit=False)
                duration.setMoveMode(target.moveModeText())
            duration.changed.connect(
                lambda editor=target, control=duration:
                editor.setMoveCoordinate(control.moveText(), emit=False)
                if editor.action_type == "鼠标移动" else None
            )
            target.move_mode.currentTextChanged.connect(duration.setMoveMode)
            duration.changed.connect(changed)
            table.setItemWidget(item, 3, duration)
            kind.currentTextChanged.connect(
                lambda text, control=duration, editor=target:
                self.update_action_duration_field(text, control, editor)
            )
            kind.currentTextChanged.connect(
                lambda text, c=card, i=item:
                self.schedule_action_type_structure_update(c, i, text)
            )
            self.update_action_duration_field(kind.currentText(), duration, target)

        if is_condition_branch:
            fixed_label = QLabel("固定")
            fixed_label.setObjectName("muted")
            fixed_label.setAlignment(Qt.AlignCenter)
            table.setItemWidget(item, 4, fixed_label)
        else:
            delete = QPushButton("删除")
            delete.setObjectName("dangerGhost")
            delete.setFixedWidth(62)
            delete.setMinimumHeight(34)
            delete.clicked.connect(
                lambda _checked=False, i=item, c=card: self.delete_action_item(i, c)
            )
            table.setItemWidget(item, 4, delete)

        if not is_loop:
            for child in action.get("children", []) or []:
                self.add_action(
                    child, save=False, card=card, parent_item=item
                )
        else:
            self._style_loop_action_row(table, item, data)
        if is_condition_branch:
            self.update_condition_branch_summary(table, item)
        if parent_item is not None and self.is_condition_branch_item(parent_item):
            self.update_condition_branch_summary(table, parent_item)
        self._update_action_variable_marker(item, card)
        if not getattr(self, "_bulk_loading_actions", False):
            table.setCurrentItem(item)
            self.update_card_action_summary(card)
            self._loading_checkpoint()
        if save:
            self.action_changed(card)
        return item

    @staticmethod
    def update_action_duration_field(
        action_type, duration_control, target_control=None,
    ):
        # The action-type combo already marks the row dirty. Reconfigure the
        # editor without emitting a second change while rows are rebuilt.
        duration_control.set_action_type(action_type, emit=False)
        if action_type == "鼠标移动" and target_control is not None:
            duration_control.setMoveText(target_control.moveCoordinate(), emit=False)
            duration_control.setMoveMode(target_control.moveModeText())

    def update_action_target(
        self, editor, action_type, preferred=None, notify=True, card=None,
    ):
        old = preferred if preferred is not None else editor.currentText()
        editor.set_action_type(action_type, old, emit=False)
        if notify:
            self.action_changed(card)

    def schedule_action_type_structure_update(self, card, item, action_type):
        QTimer.singleShot(
            0,
            lambda c=card, i=item, kind=str(action_type):
            self._update_action_type_structure(c, i, kind),
        )

    def _update_action_type_structure(self, card, item, action_type):
        if (
            card not in getattr(self, "preset_cards", [])
            or item is None
            or self.is_loop_action_item(item)
            or self.is_condition_branch_item(item)
        ):
            return
        table = card.action_table
        path = self._action_item_path(table, item)
        if not path:
            return
        kind_widget = table.itemWidget(item, 1)
        if (
            kind_widget is None
            or not hasattr(kind_widget, "currentText")
            or kind_widget.currentText() != action_type
        ):
            return
        children = [
            self.action_from_item(table, item.child(index))
            for index in range(item.childCount())
        ]
        has_branches = bool(children) and all(
            child.get("type") in CONDITION_BRANCH_TYPES for child in children
        )
        if action_type == CONDITION_ACTION_TYPE and not has_branches:
            replacement_children = self.normalize_condition_action_branches({
                "type": CONDITION_ACTION_TYPE, "children": children,
            })["children"]
        elif action_type != CONDITION_ACTION_TYPE and has_branches:
            replacement_children = []
            for branch in children:
                replacement_children.extend(branch.get("children", []) or [])
        else:
            return

        actions = self.collect_visible_actions(card)
        action = self._action_at_path(actions, path)
        if action is None:
            return
        action["children"] = replacement_children
        self.load_actions(actions, card)
        selected = self._item_at_path(card.action_table, path)
        if selected is not None:
            card.action_table.setCurrentItem(selected)
            selected.setSelected(True)
            selected.setExpanded(True)
        self.action_changed(card)

    def update_card_action_summary(self, card):
        if card is None:
            return
        if not getattr(card, "_actions_loaded", True):
            pending = getattr(card, "_pending_actions", []) or []
            groups = len(pending)
            count = int(
                getattr(card, "_pending_action_count", 0)
                or sum(1 for _ in iter_action_tree(pending))
            )
            card._pending_action_count = count
        else:
            groups = card.action_table.topLevelItemCount()
            count = card.action_table.total_item_count()
        card.action_title.setText(f"动作组 · {groups} 组 / {count} 项")
        if hasattr(card, "actions_button"):
            button_text = f"动作 {count} ↗"
            card.actions_button.setText(button_text)
            # 样式表为按钮左右各保留 11px padding；额外预留空间给
            # 字体回退、DPI 缩放和边框，避免三位数及更大数量被裁剪。
            text_width = card.actions_button.fontMetrics().horizontalAdvance(button_text)
            card.actions_button.setFixedWidth(max(104, text_width + 40))
            card.actions_button.setToolTip(
                f"在独立窗口中编辑动作序列（共 {count} 项）"
            )
        # 独立窗口中的动作树应占满可用高度，条目过多时由树自身滚动。
        if getattr(card, "_actions_loaded", True):
            card.action_table.setMinimumHeight(360)
            card.action_table.setMaximumHeight(16_777_215)

    def delete_action_item(self, item, card=None):
        if self._configuration_change_blocked_by_recording():
            return
        card = card or self.selected_preset_card
        if card is None or item is None:
            return
        if self.is_condition_branch_item(item):
            QMessageBox.information(
                getattr(card, "action_dialog", None) or self,
                "分支容器不可删除",
                "“条件成立”和“否则”是条件动作的固定结构。"
                "可以删除其中的动作，或删除整个条件动作。",
            )
            return
        subtree_count = sum(
            1 for _ in iter_action_tree([self.action_from_item(card.action_table, item)])
        )
        if not self._confirm_action_deletion(subtree_count):
            return
        own_loading = subtree_count >= 60 and not self.loading_task_stack
        if own_loading:
            self._begin_loading(
                "正在删除动作",
                f"正在移除 {subtree_count} 个动作项并更新引用……",
                host=getattr(card, "action_dialog", None) or self,
            )
        try:
            self.reset_loop_point_selection(card)
            self.select_preset_card(card)
            parent = item.parent()
            if parent is None:
                index = card.action_table.indexOfTopLevelItem(item)
                if index >= 0:
                    card.action_table.takeTopLevelItem(index)
            else:
                parent.removeChild(item)
                self.update_condition_branch_summary(card.action_table, parent)
            self.update_card_action_summary(card)
            self._prune_runtime_debug_breakpoints(card)
            self.action_changed(card)
        finally:
            if own_loading:
                self._end_loading()

    def delete_action(self, button, card=None):
        card = card or self.selected_preset_card
        if card is None:
            return
        for item in card.action_table.iter_items():
            if card.action_table.itemWidget(item, 4) is button:
                self.delete_action_item(item, card)
                return

    def move_action(self, direction, card=None):
        card = card or self.selected_preset_card
        if card is None:
            return
        self.reset_loop_point_selection(card)
        self.select_preset_card(card)
        table = card.action_table
        item = table.currentItem()
        if self.is_loop_action_item(item) or self.is_condition_branch_item(item):
            return
        path = self._action_item_path(table, item)
        if not path:
            return
        actions = self.collect_visible_actions(card)
        siblings = self._action_parent_list(actions, path)
        if siblings is None:
            return
        index = path[-1]
        target = index + direction
        if not 0 <= target < len(siblings):
            return
        if siblings[target].get("type") == LOOP_ACTION_TYPE:
            return
        siblings[index], siblings[target] = siblings[target], siblings[index]
        new_path = path[:-1] + (target,)
        self.load_actions(actions, card)
        selected = self._item_at_path(card.action_table, new_path)
        if selected is not None:
            card.action_table.setCurrentItem(selected)
            selected.setSelected(True)
            card.action_table.scrollToItem(selected)
        self.action_changed(card)

    def load_actions(self, actions, card=None):
        card = card or self.selected_preset_card
        if card is None:
            return
        actions = list(actions or [])
        action_count = self.semantic_action_count(actions)
        if action_count > MAX_ACTION_COUNT:
            QMessageBox.warning(
                getattr(card, "action_dialog", None) or self,
                "动作数量超限",
                f"单个预设最多包含 {MAX_ACTION_COUNT} 个动作；当前内容有 "
                f"{action_count} 个。",
            )
            return False
        own_loading = action_count >= 60 and not self.loading_task_stack
        if own_loading:
            preset_name = (
                card.name.text().strip()
                if hasattr(card, "name") else "当前预设"
            ) or "当前预设"
            self._begin_loading(
                "正在载入动作",
                f"正在重建“{preset_name}”的 {action_count} 个动作项……",
                host=getattr(card, "action_dialog", None) or self,
            )
        try:
            self.reset_loop_point_selection(card)
            self.select_preset_card(card)
            table = card.action_table
            table.setUpdatesEnabled(False)
            table.blockSignals(True)
            self._bulk_loading_actions = True
            try:
                table.clear()
                for index, action in enumerate(actions):
                    self.add_action(action, save=False, card=card)
                    if index and index % 100 == 0:
                        self._loading_checkpoint()
            finally:
                self._bulk_loading_actions = False
                table.blockSignals(False)
                table.setUpdatesEnabled(True)
            card._actions_loaded = True
            card._pending_actions = None
            card._pending_action_count = action_count
            self.update_card_action_summary(card)
            self._loading_checkpoint(force=True)
            self._prune_runtime_debug_breakpoints(card)
            return True
        finally:
            if own_loading:
                self._end_loading()

    def open_action_cleanup_dialog(self, card=None):
        if self._configuration_change_blocked_by_recording():
            return
        card = card or self.selected_preset_card
        if card is None:
            return
        self.select_preset_card(card)
        actions = self.collect_visible_actions(card)
        ordinary_count = sum(
            1 for action in iter_action_tree(actions)
            if action.get("type") != LOOP_ACTION_TYPE
        )
        if ordinary_count == 0:
            QMessageBox.information(self, "没有可整理动作", "当前预设中没有普通动作。")
            return

        protected_ids = {
            str(action_id)
            for action in actions
            if action.get("type") == LOOP_ACTION_TYPE
            for action_id in action.get("target_action_ids", []) or []
            if str(action_id)
        }
        has_relative_moves = any(
            action.get("type") == "鼠标移动"
            and str(action.get("target") or "").startswith("rel:")
            for action in iter_action_tree(actions)
        )
        dialog = QDialog(card.action_dialog if hasattr(card, "action_dialog") else self)
        dialog.setWindowTitle("整理当前预设动作")
        form = QFormLayout(dialog)
        simplify_moves = QCheckBox("简化密集鼠标移动轨迹")
        simplify_moves.setChecked(not has_relative_moves)
        if has_relative_moves:
            simplify_moves.setEnabled(False)
            simplify_moves.setToolTip(
                "当前预设包含游戏原始相对移动。为避免改变输入包密度和视角结果，"
                "相对移动轨迹禁止自动简化。"
            )
        merge_wheel = QCheckBox("合并短间隔内的同方向滚轮")
        merge_wheel.setChecked(True)
        gap = QSpinBox()
        gap.setRange(0, 2000)
        gap.setValue(120)
        gap.setSuffix(" ms")
        gap.setToolTip("只有不超过该时长的等待，才会被视为同一段移动或滚轮序列")
        tolerance = QSpinBox()
        tolerance.setRange(0, 100)
        tolerance.setValue(6)
        tolerance.setSuffix(" px")
        tolerance.setToolTip("数值越大，保留的鼠标轨迹点越少；0 表示不删除轨迹点")
        note_text = (
            "只整理同一层级内的连续序列，不跨越键盘点击、鼠标点击、循环卡片或子动作边界。"
            "短等待会累加到保留下来的轨迹点之间；被循环卡片引用的动作会强制保留。"
        )
        if has_relative_moves:
            note_text += " 当前预设含相对视角移动，本次仅允许整理滚轮，不简化鼠标轨迹。"
        note = QLabel(note_text)
        note.setWordWrap(True)
        note.setObjectName("muted")
        summary = QLabel()
        summary.setWordWrap(True)
        _left, _top, virtual_width, virtual_height = self._virtual_screen_geometry()

        def prepared_actions():
            return simplify_recorded_actions(
                actions,
                simplify_moves=simplify_moves.isChecked(),
                merge_wheel=merge_wheel.isChecked(),
                merge_gap_ms=gap.value(),
                move_tolerance=tolerance.value(),
                protected_action_ids=protected_ids,
                adjust_timing=False,
                trim_edge_waits=False,
                percentage_size=(virtual_width, virtual_height),
            )

        def counts(items):
            all_actions = list(iter_action_tree(items))
            return {
                "total": len(all_actions),
                "move": sum(a.get("type") == "鼠标移动" for a in all_actions),
                "wait": sum(a.get("type") == "等待" for a in all_actions),
                "wheel": sum(a.get("type") == "鼠标滚轮" for a in all_actions),
            }

        original_counts = counts(actions)

        def refresh_summary(*_args):
            cleaned_counts = counts(prepared_actions())
            summary.setText(
                f"总动作：{original_counts['total']} → {cleaned_counts['total']}　"
                f"鼠标移动：{original_counts['move']} → {cleaned_counts['move']}　"
                f"等待：{original_counts['wait']} → {cleaned_counts['wait']}　"
                f"滚轮：{original_counts['wheel']} → {cleaned_counts['wheel']}"
            )

        simplify_moves.toggled.connect(refresh_summary)
        merge_wheel.toggled.connect(refresh_summary)
        gap.valueChanged.connect(refresh_summary)
        tolerance.valueChanged.connect(refresh_summary)
        form.addRow("", simplify_moves)
        form.addRow("", merge_wheel)
        form.addRow("合并间隔上限", gap)
        form.addRow("轨迹容差", tolerance)
        form.addRow("规则", note)
        form.addRow("预览", summary)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用整理")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        refresh_summary()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._begin_loading(
            "正在整理动作",
            f"正在分析并重建 {original_counts['total']} 个动作项……",
            host=getattr(card, "action_dialog", None) or self,
        )
        try:
            cleaned = prepared_actions()
            unchanged = (
                self._action_history_signature(cleaned)
                == self._action_history_signature(actions)
            )
            if not unchanged:
                self.load_actions(cleaned, card)
                self.action_changed(card)
        finally:
            self._end_loading()
        if unchanged:
            QMessageBox.information(
                self, "无需整理", "按照当前参数，没有可合并或可简化的动作。"
            )
            return
        self.engine_hint.setText("动作整理完成；请检查预览后再应用配置。")

    def selected_action_items(self, card=None):
        card = card or self.selected_preset_card
        if card is None:
            return []
        selected = list(card.action_table.selectedItems())
        selected_set = set(selected)
        result = []
        for item in selected:
            ancestor = item.parent()
            nested_under_selected = False
            while ancestor is not None:
                if ancestor in selected_set:
                    nested_under_selected = True
                    break
                ancestor = ancestor.parent()
            if not nested_under_selected:
                result.append(item)
        return result

    def action_from_item(self, table, item):
        if self.is_loop_action_item(item):
            data = dict(item.data(0, LOOP_DATA_ROLE) or {})
            name_editor = table.itemWidget(item, 1)
            mode_editor = table.itemWidget(item, 2)
            data.update({
                "type": LOOP_ACTION_TYPE,
                "name": (
                    name_editor.text().strip()
                    if name_editor is not None else data.get("name", "循环项目")
                ) or "循环项目",
                "execution_mode": (
                    mode_editor.currentText()
                    if mode_editor is not None else data.get("execution_mode", "执行次数")
                ),
            })
            data["children"] = []
            return data

        if self.is_condition_branch_item(item):
            return {
                "type": item.data(0, ACTION_BRANCH_TYPE_ROLE),
                "action_id": str(
                    item.data(0, ACTION_ID_ROLE) or uuid.uuid4().hex
                ),
                "children": [
                    self.action_from_item(table, item.child(index))
                    for index in range(item.childCount())
                ],
            }

        action_type = table.itemWidget(item, 1).currentText()
        action = {
            "type": action_type,
            "action_id": str(item.data(0, ACTION_ID_ROLE) or uuid.uuid4().hex),
        }
        bindings = dict(item.data(0, ACTION_PARAMETER_BINDINGS_ROLE) or {})
        allowed_fields = ACTION_PARAMETER_FIELDS.get(action_type, {})
        card = self._card_for_action_table(table)
        definitions = {
            definition["name"]: definition
            for definition in self._preset_parameter_definitions(card)
        } if card is not None else {}
        bindings = {
            field: name for field, name in bindings.items()
            if (
                field in allowed_fields
                and name in definitions
                and value_matches_action_field(
                    action_type, field, definitions[name].get("type"),
                    definitions[name].get("default"),
                )
            )
        }
        if bindings:
            action["parameter_bindings"] = bindings
        stored_recording = item.data(0, ACTION_RECORDING_CONTEXT_ROLE)
        duration = table.itemWidget(item, 3)
        if action_type == CONDITION_ACTION_TYPE:
            action.update({
                "condition_input": table.itemWidget(item, 2).currentText(),
                "condition_state": duration.conditionState(),
            })
        elif action_type == WAIT_CONDITION_ACTION_TYPE:
            action.update({
                "condition_input": table.itemWidget(item, 2).currentText(),
                "condition_state": duration.conditionState(),
                "timeout_ms": duration.value(),
                "poll_ms": 20,
            })
        elif action_type == SUBMACRO_ACTION_TYPE:
            action.update({
                "preset_id": table.itemWidget(item, 2).currentText(),
                "repeat_count": duration.value(),
                "speed_percent": duration.callSpeedValue(),
            })
            target_card = next(
                (
                    other for other in getattr(self, "preset_cards", [])
                    if other.preset_id == action["preset_id"]
                ),
                None,
            )
            target_names = {
                definition["name"]
                for definition in self._preset_parameter_definitions(target_card)
            } if target_card is not None else set()
            target_definitions = {
                definition["name"]: definition
                for definition in self._preset_parameter_definitions(target_card)
            } if target_card is not None else {}
            values = {}
            for name, value in (
                item.data(0, ACTION_PARAMETER_VALUES_ROLE) or {}
            ).items():
                if name not in target_names:
                    continue
                try:
                    values[name] = coerce_parameter_value(
                        target_definitions[name]["type"], value
                    )
                except ValueError:
                    continue
            if values:
                action["parameter_values"] = values
        elif action_type == "等待":
            action["wait_ms"] = duration.value()
            action["jitter_ms"] = duration.jitterValue()
        elif action_type == "鼠标滚轮":
            action.update({
                "target": table.itemWidget(item, 2).currentText(),
                "steps": duration.value(),
            })
        elif action_type == "鼠标移动":
            action["target"] = table.itemWidget(item, 2).currentText()
            if (
                isinstance(stored_recording, dict)
                and action["target"] == str(stored_recording.get("target") or "")
                and isinstance(stored_recording.get("context"), dict)
            ):
                action["recording_context"] = copy.deepcopy(
                    stored_recording["context"]
                )
        else:
            action.update({
                "target": table.itemWidget(item, 2).currentText(),
                "hold_ms": duration.value(),
                "jitter_ms": duration.jitterValue(),
            })
            legacy_modifiers = str(
                item.data(0, ACTION_LEGACY_MODIFIERS_ROLE) or "无"
            )
            if legacy_modifiers in MODIFIER_OPTIONS and legacy_modifiers != "无":
                action["modifiers"] = legacy_modifiers
        action["children"] = [
            self.action_from_item(table, item.child(index))
            for index in range(item.childCount())
        ]
        return action

    def copy_selected_actions(self, card=None):
        card = card or self.selected_preset_card
        if card is None:
            return
        self.select_preset_card(card)
        table = card.action_table
        selected = [
            item for item in self.selected_action_items(card)
            if not self.is_condition_branch_item(item)
        ]
        self.action_clipboard = [
            self.action_from_item(table, item)
            for item in selected
        ]

    def paste_actions(self, card=None):
        card = card or self.selected_preset_card
        if card is None or not self.action_clipboard:
            return
        self.reset_loop_point_selection(card)
        self.select_preset_card(card)
        table = card.action_table
        current = table.currentItem()
        parent = current.parent() if current is not None else None
        if self.is_loop_action_item(current):
            parent = None
        if current is None:
            insert_at = table.topLevelItemCount()
        elif parent is None:
            insert_at = table.indexOfTopLevelItem(current) + 1
        else:
            insert_at = parent.indexOfChild(current) + 1

        # Rewrite ordinary action IDs and loop references as one operation.
        # External references are retained long enough to distinguish a complete
        # copied range from a loop-only duplicate; the preflight below rejects
        # missing or overlapping references before any tree item is inserted.
        payload = remap_action_ids(
            self.action_clipboard, preserve_external=True
        )
        payload = [
            action for action in payload
            if action.get("type") not in CONDITION_BRANCH_TYPES
        ]
        if not payload:
            return
        existing_actions = self.collect_visible_actions(card)
        existing_ordinary_ids = {
            str(action.get("action_id"))
            for action in iter_action_tree(existing_actions)
            if action.get("type") not in (
                LOOP_ACTION_TYPE, *CONDITION_BRANCH_TYPES,
            ) and action.get("action_id")
        }
        existing_loop_ids = {
            str(action_id)
            for action in existing_actions
            if action.get("type") == LOOP_ACTION_TYPE
            for action_id in action.get("target_action_ids", []) or []
            if str(action_id)
        }
        payload_ordinary_ids = {
            str(action.get("action_id"))
            for action in iter_action_tree(payload)
            if action.get("type") not in (
                LOOP_ACTION_TYPE, *CONDITION_BRANCH_TYPES,
            ) and action.get("action_id")
        }
        available_ids = existing_ordinary_ids | payload_ordinary_ids
        payload_claimed_ids = set()
        for loop_action in (
            action for action in payload
            if action.get("type") == LOOP_ACTION_TYPE
        ):
            target_ids = [
                str(action_id)
                for action_id in loop_action.get("target_action_ids", []) or []
                if str(action_id)
            ]
            if not target_ids or any(
                action_id not in available_ids for action_id in target_ids
            ):
                QMessageBox.information(
                    getattr(card, "action_dialog", None) or self,
                    "无法粘贴循环项目",
                    "该循环项目引用的动作不在当前预设中。请同时复制被引用的动作。",
                )
                return
            if (
                set(target_ids) & existing_loop_ids
                or set(target_ids) & payload_claimed_ids
            ):
                QMessageBox.information(
                    getattr(card, "action_dialog", None) or self,
                    "循环范围重叠",
                    "粘贴内容与已有循环项目重叠。请同时复制为新的动作范围，或先删除原循环项目。",
                )
                return
            payload_claimed_ids.update(target_ids)
        pasted_count = self.semantic_action_count(payload)
        current_count = self.table_semantic_action_count(table)
        if current_count + pasted_count > MAX_ACTION_COUNT:
            QMessageBox.warning(
                getattr(card, "action_dialog", None) or self,
                "无法粘贴动作",
                f"粘贴后将达到 {current_count + pasted_count} 个动作，超过单个"
                f"预设上限 {MAX_ACTION_COUNT}。",
            )
            return
        own_loading = pasted_count >= 40 and not self.loading_task_stack
        if own_loading:
            self._begin_loading(
                "正在粘贴动作",
                f"正在创建 {pasted_count} 个动作项……",
                host=getattr(card, "action_dialog", None) or self,
            )
        try:
            first = None
            ordinary_offset = 0
            for action in payload:
                is_loop = action.get("type") == LOOP_ACTION_TYPE
                item = self.add_action(
                    action, save=False, card=card,
                    parent_item=None if is_loop else parent,
                    insert_index=None if is_loop else insert_at + ordinary_offset,
                )
                if not is_loop:
                    ordinary_offset += 1
                first = first or item
            self._loading_checkpoint(force=True)
        finally:
            if own_loading:
                self._end_loading()
        if first is not None:
            table.setCurrentItem(first)
            first.setSelected(True)
        self.action_changed(card)

    def duplicate_selected_actions(self, card=None):
        self.copy_selected_actions(card)
        self.paste_actions(card)

    def delete_selected_actions(self, card=None):
        if self._configuration_change_blocked_by_recording():
            return
        card = card or self.selected_preset_card
        if card is None:
            return
        self.reset_loop_point_selection(card)
        self.select_preset_card(card)
        items = [
            item for item in self.selected_action_items(card)
            if not self.is_condition_branch_item(item)
        ]
        if not items:
            if any(
                self.is_condition_branch_item(item)
                for item in card.action_table.selectedItems()
            ):
                QMessageBox.information(
                    getattr(card, "action_dialog", None) or self,
                    "分支容器不可删除",
                    "请删除分支内的动作，或选中上层条件动作后删除。",
                )
            return
        delete_count = sum(
            sum(
                1 for _ in iter_action_tree([
                    self.action_from_item(card.action_table, item)
                ])
            )
            for item in items
        )
        if not self._confirm_action_deletion(delete_count):
            return
        own_loading = delete_count >= 40 and not self.loading_task_stack
        if own_loading:
            self._begin_loading(
                "正在删除动作",
                f"正在移除选中的 {delete_count} 个动作项……",
                host=getattr(card, "action_dialog", None) or self,
            )
        try:
            branch_parents = {
                item.parent() for item in items
                if self.is_condition_branch_item(item.parent())
            }
            for item in items:
                parent = item.parent()
                if parent is None:
                    index = card.action_table.indexOfTopLevelItem(item)
                    if index >= 0:
                        card.action_table.takeTopLevelItem(index)
                else:
                    parent.removeChild(item)
            for branch_parent in branch_parents:
                self.update_condition_branch_summary(
                    card.action_table, branch_parent
                )
            self.update_card_action_summary(card)
            self._prune_runtime_debug_breakpoints(card)
            self.action_changed(card)
        finally:
            if own_loading:
                self._end_loading()

    def clear_visible_actions(self, card=None):
        if self._configuration_change_blocked_by_recording():
            return
        card = card or self.selected_preset_card
        if card is not None:
            action_count = card.action_table.total_item_count()
            if action_count and not self._confirm_action_deletion(
                action_count, clear_all=True
            ):
                return
            own_loading = action_count >= 60 and not self.loading_task_stack
            if own_loading:
                self._begin_loading(
                    "正在清空动作",
                    f"正在移除当前预设中的 {action_count} 个动作项……",
                    host=getattr(card, "action_dialog", None) or self,
                )
            try:
                self.reset_loop_point_selection(card)
                card.action_table.clear()
                self._prune_runtime_debug_breakpoints(card)
                self.update_card_action_summary(card)
                self.action_changed(card)
            finally:
                if own_loading:
                    self._end_loading()

    def keyPressEvent(self, event):
        focus = QApplication.focusWidget()
        active_card = None
        for card in self.preset_cards:
            tree = card.action_table
            if focus is tree or (focus is not None and tree.isAncestorOf(focus)):
                active_card = card
                break
        if active_card:
            self.select_preset_card(active_card)
            if event.key() == Qt.Key_Delete:
                self.delete_selected_actions(active_card)
                return
            if event.modifiers() & Qt.ControlModifier:
                if event.key() == Qt.Key_Z:
                    if event.modifiers() & Qt.ShiftModifier:
                        self.redo_actions(active_card)
                    else:
                        self.undo_actions(active_card)
                    return
                if event.key() == Qt.Key_Y:
                    self.redo_actions(active_card)
                    return
                if event.key() == Qt.Key_C:
                    self.copy_selected_actions(active_card)
                    return
                if event.key() == Qt.Key_V:
                    self.paste_actions(active_card)
                    return
                if event.key() == Qt.Key_D:
                    self.duplicate_selected_actions(active_card)
                    return
        super().keyPressEvent(event)

    def collect_visible_actions(self, card=None):
        card = card or self.selected_preset_card
        if card is None:
            return []
        if not getattr(card, "_actions_loaded", True):
            return clone_action_tree(getattr(card, "_pending_actions", []) or [])
        table = card.action_table
        actions = [
            self.action_from_item(table, table.topLevelItem(index))
            for index in range(table.topLevelItemCount())
        ]
        ordinary = [a for a in actions if a.get("type") != LOOP_ACTION_TYPE]
        loops = [a for a in actions if a.get("type") == LOOP_ACTION_TYPE]
        valid_ids = {
            str(action.get("action_id"))
            for action in iter_action_tree(ordinary)
            if action.get("type") not in (
                LOOP_ACTION_TYPE, *CONDITION_BRANCH_TYPES,
            ) and action.get("action_id")
        }
        for loop in loops:
            loop["target_action_ids"] = [
                action_id for action_id in loop.get("target_action_ids", [])
                if str(action_id) in valid_ids
            ]
            loop["children"] = []
        return ordinary + loops

    def synchronize_loop_references(self, card):
        """Repair loop references immediately after action-tree edits.

        Reordering inside the same contiguous range updates the stored ID order.
        A loop whose referenced actions were removed, split across levels, or
        overlap an earlier loop is removed immediately instead of leaving the
        editor in a configuration that can only fail during Apply.
        """
        if card is None or not hasattr(card, "action_table"):
            return
        table = card.action_table
        sibling_sequences = []

        def collect_siblings(parent=None):
            if parent is None:
                items = [
                    table.topLevelItem(index)
                    for index in range(table.topLevelItemCount())
                ]
            else:
                items = [parent.child(index) for index in range(parent.childCount())]
            ordinary_items = [
                item for item in items if not self.is_loop_action_item(item)
            ]
            ids = [
                str(item.data(0, ACTION_ID_ROLE))
                for item in ordinary_items
                if (
                    item.data(0, ACTION_ID_ROLE)
                    and not self.is_condition_branch_item(item)
                )
            ]
            if ids:
                sibling_sequences.append(ids)
            for item in ordinary_items:
                collect_siblings(item)

        collect_siblings()
        position_map = {
            action_id: (sequence_index, position)
            for sequence_index, sequence in enumerate(sibling_sequences)
            for position, action_id in enumerate(sequence)
        }
        loop_items = [
            item for item in list(table.iter_items())
            if self.is_loop_action_item(item)
        ]
        claimed_ids = set()
        removed = []
        for item in loop_items:
            data = dict(item.data(0, LOOP_DATA_ROLE) or {})
            name = str(data.get("name") or "循环项目")
            target_ids = []
            seen = set()
            for action_id in data.get("target_action_ids", []) or []:
                action_id = str(action_id)
                if action_id in position_map and action_id not in seen:
                    seen.add(action_id)
                    target_ids.append(action_id)

            valid = bool(target_ids)
            ordered_ids = []
            if valid:
                sequence_index = position_map[target_ids[0]][0]
                positions = []
                for action_id in target_ids:
                    current_sequence, position = position_map[action_id]
                    if current_sequence != sequence_index:
                        valid = False
                        break
                    positions.append(position)
                if valid:
                    ordered_positions = sorted(positions)
                    valid = ordered_positions == list(range(
                        ordered_positions[0],
                        ordered_positions[0] + len(ordered_positions),
                    ))
                    if valid:
                        sequence = sibling_sequences[sequence_index]
                        ordered_ids = [sequence[position] for position in ordered_positions]
            if valid and claimed_ids.intersection(ordered_ids):
                valid = False

            if not valid:
                parent = item.parent()
                if parent is None:
                    index = table.indexOfTopLevelItem(item)
                    if index >= 0:
                        table.takeTopLevelItem(index)
                else:
                    parent.removeChild(item)
                removed.append(name)
                continue

            data["target_action_ids"] = ordered_ids
            claimed_ids.update(ordered_ids)
            # Remove every legacy snapshot field. The referenced ordinary action
            # rows are the only source of timing, target and child parameters.
            for stale_key in (
                "target_actions", "action_snapshot", "actions_snapshot",
                "referenced_actions", "source_actions",
            ):
                data.pop(stale_key, None)
            data["children"] = []
            item.setData(0, LOOP_DATA_ROLE, data)
            self.update_loop_action_summary(card, item)
        if removed:
            QMessageBox.information(
                getattr(card, "action_dialog", None) or self,
                "循环项目已同步",
                "以下循环项目因引用范围已删除、跨层级、非连续或与其他循环重叠，"
                "已从当前预设移除：\n" + "、".join(removed),
            )

    def action_changed(self, card=None):
        card = card or self.selected_preset_card
        if card is not None:
            self.select_preset_card(card)
            self.synchronize_loop_references(card)
            self.update_card_action_summary(card)
            self._record_action_history(card)
        self.data_changed()

    def _action_history_snapshot(self, card):
        return clone_action_tree(self.collect_visible_actions(card))

    @staticmethod
    def _action_history_signature(actions):
        return json.dumps(
            actions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def _record_action_history(self, card, force=False):
        if card is None or getattr(card, "action_history_suspended", False):
            return
        history = getattr(card, "action_undo_history", None)
        if history is None:
            card.action_undo_history = []
            card.action_redo_history = []
            history = card.action_undo_history
        snapshot = self._action_history_snapshot(card)
        if (
            not force and history
            and self._action_history_signature(history[-1])
            == self._action_history_signature(snapshot)
        ):
            return
        history.append(snapshot)
        del history[:-self.action_history_limit]
        card.action_redo_history.clear()
        self._refresh_action_history_controls(card)

    @staticmethod
    def _refresh_action_history_controls(card):
        if card is None:
            return
        undo_button = getattr(card, "undo_button", None)
        redo_button = getattr(card, "redo_button", None)
        if undo_button is not None:
            undo_button.setEnabled(len(getattr(card, "action_undo_history", [])) >= 2)
        if redo_button is not None:
            redo_button.setEnabled(bool(getattr(card, "action_redo_history", [])))

    def _confirm_action_deletion(self, count, *, clear_all=False):
        count = max(1, int(count or 1))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("确认清空动作" if clear_all else "确认删除动作")
        box.setText(
            f"确定清空当前预设中的 {count} 个动作项吗？"
            if clear_all else f"确定删除选中的 {count} 个动作项吗？"
        )
        box.setInformativeText("删除后可在动作窗口中使用“撤销”恢复。")
        delete_button = box.addButton(
            "清空" if clear_all else "删除", QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_button)
        box.exec()
        return box.clickedButton() is delete_button

    def _restore_action_history(self, card, snapshot):
        card.action_history_suspended = True
        try:
            self.load_actions(clone_action_tree(snapshot), card)
            self.synchronize_loop_references(card)
            self.update_card_action_summary(card)
        finally:
            card.action_history_suspended = False
        self.data_changed()
        self._refresh_action_history_controls(card)

    def undo_actions(self, card=None):
        card = card or self.selected_preset_card
        history = getattr(card, "action_undo_history", []) if card else []
        if len(history) < 2:
            return False
        current = history.pop()
        card.action_redo_history.append(current)
        self._restore_action_history(card, history[-1])
        self._refresh_action_history_controls(card)
        return True

    def redo_actions(self, card=None):
        card = card or self.selected_preset_card
        redo = getattr(card, "action_redo_history", []) if card else []
        if not redo:
            return False
        snapshot = redo.pop()
        card.action_undo_history.append(clone_action_tree(snapshot))
        self._restore_action_history(card, snapshot)
        self._refresh_action_history_controls(card)
        return True

    def store_actions_for_row(self, _row):
        # 动作直接保存在每张预设卡片自己的树中，无需切换时搬运。
        return

    def collect_mappings(self):
        result = []
        for card in self.mapping_cards:
            source_modifiers, source_key = card.source_hotkey.value()
            condition_enabled, condition_input, condition_state = (
                card.source_hotkey.condition_value()
            )
            target_modifiers, target_key = card.target_hotkey.value()
            mode = card.mode.currentText()
            result.append({
                "id": card.mapping_id,
                "enabled": card.enabled.isChecked(),
                "name": card.name.text().strip() or f"基础映射 {len(result) + 1}",
                "source_modifiers": source_modifiers,
                "source": source_key,
                "target_modifiers": target_modifiers,
                "target": target_key,
                "condition_enabled": condition_enabled,
                "condition_input": condition_input,
                "condition_state": condition_state,
                "mode": mode,
                "hold_ms": card.hold.value(),
                "hold_jitter_ms": card.hold_jitter.value(),
                "loop_count": card.loop_count.value() if mode == "固定次数" else 1,
                "loop_interval_ms": (
                    card.loop_interval.value()
                    if mode in ("固定次数", "按住循环", "开关循环", "无限循环")
                    else 0
                ),
                "loop_interval_jitter_ms": (
                    card.loop_interval_jitter.value()
                    if mode in ("固定次数", "按住循环", "开关循环", "无限循环")
                    else 0
                ),
                # 基础映射直接使用“动作按住”和“轮间隔”，不再叠加隐藏速度。
                "speed_percent": 100,
                "max_runtime_s": (
                    card.max_runtime.value()
                    if mode in ("开关循环", "无限循环") else 0
                ),
            })
        return result

    def collect_presets(self):
        result = []
        for index, card in enumerate(self.preset_cards):
            trigger_modifiers, trigger = card.trigger_hotkey.value()
            condition_enabled, condition_input, condition_state = (
                card.trigger_hotkey.condition_value()
            )
            mode = card.execution_mode.currentText()
            result.append({
                "id": card.preset_id,
                "enabled": card.enabled.isChecked(),
                "name": card.name.text().strip() or f"预设 {index + 1}",
                "trigger_modifiers": trigger_modifiers,
                "trigger": trigger,
                "condition_enabled": condition_enabled,
                "condition_input": condition_input,
                "condition_state": condition_state,
                "execution_mode": mode,
                "loop_count": card.loop_count.value() if mode == "固定次数" else 1,
                "loop_interval_ms": (
                    card.loop_interval.value()
                    if mode in ("固定次数", "按住循环", "开关循环", "无限循环")
                    else 0
                ),
                "loop_interval_jitter_ms": (
                    card.loop_interval_jitter.value()
                    if mode in ("固定次数", "按住循环", "开关循环", "无限循环")
                    else 0
                ),
                "speed_percent": card.speed.value(),
                "max_runtime_s": (
                    card.max_runtime.value()
                    if mode in ("开关循环", "无限循环") else 0
                ),
                "parameters": self._preset_parameter_definitions(card),
                "actions": self.collect_visible_actions(card),
            })
        return result

    @Slot()
    def data_changed(self):
        # 批量载入配置表单时会创建大量控件。初始化期间直接返回，
        # 避免每创建一个控件都重新遍历整张映射/预设表。
        if self.initializing:
            return
        cancel_test_countdown = getattr(
            self, "_cancel_manual_test_countdown", None
        )
        if callable(cancel_test_countdown):
            cancel_test_countdown("配置已修改，原测试倒计时已取消")
        self.refresh_mapping_priority_labels()
        self.refresh_cache()

        # 先直接比较当前可见表单与载入基线。这样删除动作、拖拽、
        # 清空动作、撤销/重做等树结构变化即使尚未写回档案模型，
        # 也会立即启用“应用更改”。过去只比较序列化后的全局模型，
        # 某些动作删除必须等到切换方案时写回模型后才会被识别。
        current_editor_payload = self._current_profile_snapshot()
        editor_dirty = (
            self._profile_payload_signature(current_editor_payload)
            != self._profile_payload_signature(self.editor_loaded_payload)
        )
        if getattr(self, "startup_recovery_pending_save", False):
            config_dirty = True
        elif editor_dirty:
            config_dirty = True
        else:
            # 表单本身未改动时，再检查档案规则、全局快捷键、后端等
            # 非表单设置是否与已应用版本不同。
            current_signature = self.current_config_signature()
            config_dirty = current_signature != self.applied_config_signature
        if config_dirty:
            self.config_state = ConfigState.DIRTY
        elif getattr(self, "running", False):
            self.config_state = ConfigState.APPLIED
        else:
            self.config_state = ConfigState.SAVED
        self.reload_button.setEnabled(self.config_state == ConfigState.DIRTY)
        self.engine_hint.setStyleSheet("")
        if self.config_state == ConfigState.DIRTY:
            hint_text = "配置已修改，但尚未保存或应用"
        elif self.config_state == ConfigState.APPLIED:
            hint_text = "配置与当前运行中的已应用版本一致"
        else:
            hint_text = "配置已保存；启动输入引擎后确认运行"
        self.engine_hint.setText(hint_text)
        if (
            self.config_state == ConfigState.DIRTY
            and self.auto_apply_checkbox.isChecked()
            and not getattr(self, "startup_recovery_pending_save", False)
        ):
            self.auto_apply_timer.start()
        else:
            self.auto_apply_timer.stop()
        self.refresh_status_ui()

    @Slot()
    def auto_apply_config(self):
        if getattr(self, "startup_recovery_pending_save", False):
            # Startup recovery deliberately preserves the unreadable/original
            # file until the user explicitly chooses Apply. Merely enabling the
            # preference must not turn into a delayed full-config overwrite.
            self.auto_apply_timer.stop()
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "恢复后的配置需要手动确认；请检查后点击“应用更改”"
                )
            return
        if getattr(self, "recording_session_active", False):
            # Recording can run for an arbitrary duration.  Do not poll or show
            # the manual apply warning from a timer callback; completion restores
            # the timer when a dirty configuration still needs to be applied.
            self._auto_apply_deferred_for_recording = True
            self.auto_apply_timer.stop()
            return
        if (
            getattr(self, "profile_switch_confirmation_active", False)
            or getattr(self, "settings_dialog_active", False)
            or QApplication.activeModalWidget() is not None
        ):
            # Modal editors run a nested Qt event loop. The timer may therefore
            # fire while the user is still deciding whether to keep/discard a
            # form. Poll until the modal transaction has fully closed instead
            # of changing the applied baseline behind the dialog.
            self.auto_apply_timer.start(500)
            return
        if self.loading_task_stack:
            # Bulk form loading pumps the Qt event loop so the spinner remains
            # animated. Defer auto-apply instead of starting a nested engine
            # reload from that timer callback.
            self.auto_apply_timer.start(500)
            return
        if (
            self.auto_apply_checkbox.isChecked()
            and self.config_state == ConfigState.DIRTY
        ):
            with self.macro_controller.lock:
                active_tasks = [
                    task for task in self.macro_controller.tasks.values()
                    if task.has_live_threads()
                ]
            if active_tasks:
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "配置已修改；当前宏结束后再自动应用，避免中断正在执行的动作"
                )
                self.auto_apply_timer.start(500)
                self.refresh_status_ui()
                return
            self._auto_apply_in_progress = True
            try:
                self.apply_changes()
            finally:
                self._auto_apply_in_progress = False

    @Slot()
    def on_auto_apply_changed(self):
        # Persist only this preference; never serialize pending mappings.
        if not self.initializing:
            self.save_auto_apply_preference()
        if getattr(self, "startup_recovery_pending_save", False):
            self.auto_apply_timer.stop()
        elif not self.auto_apply_checkbox.isChecked():
            self.auto_apply_timer.stop()
        elif self.config_state == ConfigState.DIRTY:
            self.auto_apply_timer.start()
