"""Preset-card creation, selection, and safe deletion workflow."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from config.schema import MAX_PRESETS_PER_SCOPE
from core.constants import *
from macro.actions import iter_action_tree
from ui.editors import ActionTreeWidget, HotkeyEdit


class PresetEditorMixin:
    def add_preset(self, preset=None):
        if len(self.preset_cards) >= MAX_PRESETS_PER_SCOPE:
            QMessageBox.warning(
                self, "无法添加预设",
                f"当前配置的预设数量已达到上限 {MAX_PRESETS_PER_SCOPE}。",
            )
            return None
        preset = dict(preset or {
            "id": uuid.uuid4().hex,
            "enabled": False,
            "name": f"未配置预设 {len(self.preset_cards) + 1}",
            "trigger_modifiers": "无",
            "trigger": "F1",
            "execution_mode": "执行一次",
            "loop_count": 1,
            "loop_interval_ms": 0,
            "loop_interval_jitter_ms": 0,
            "speed_percent": 100,
            "max_runtime_s": 0,
            "actions": [],
        })
        card = QFrame()
        card.setObjectName("presetCard")
        card.setProperty("selected", False)
        card.preset_id = preset.get("id") or uuid.uuid4().hex
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 11, 14, 12)
        card_layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(7)
        card.enabled = QCheckBox("启用")
        card.enabled.setChecked(bool(preset.get("enabled", False)))
        header.addWidget(card.enabled)

        card.name = QLineEdit(preset.get("name", "新预设"))
        card.name.setPlaceholderText("方案名称")
        card.name.setFixedWidth(220)
        name_group = self.labeled_control("方案名称", card.name)
        name_group.setFixedWidth(230)
        header.addWidget(name_group)

        card.trigger_hotkey = HotkeyEdit(
            preset.get("trigger_modifiers", "无"),
            preset.get("trigger", "F1"),
            TRIGGER_NAMES,
        )
        card.trigger_hotkey.setFixedWidth(220)
        trigger_group = self.labeled_control("触发快捷键", card.trigger_hotkey)
        trigger_group.setFixedWidth(230)
        header.addWidget(trigger_group)

        card.execution_mode = self.combo(
            EXECUTION_MODES,
            preset.get("execution_mode", "执行一次"),
            lambda *_: None,
        )
        mode_group = self.labeled_control("执行模式", card.execution_mode)
        mode_group.setFixedWidth(130)
        header.addWidget(mode_group)

        card.parameters_button = QPushButton("参数 ▸")
        card.parameters_button.setObjectName("collapseButton")
        card.parameters_button.setCheckable(True)
        card.parameters_button.setFixedWidth(78)
        header.addWidget(card.parameters_button)

        card.actions_button = QPushButton("动作 0 ↗")
        card.actions_button.setObjectName("collapseButton")
        card.actions_button.setCheckable(False)
        # 动作数量达到三位数后，固定 92px 会裁剪文字。先保留一个
        # 紧凑的最小宽度，实际宽度在摘要更新时按当前文字重新计算。
        card.actions_button.setFixedWidth(104)
        card.actions_button.setToolTip("在独立窗口中编辑动作序列")
        header.addWidget(card.actions_button)
        header.addStretch(1)

        delete = QPushButton("删除")
        delete.setObjectName("dangerGhost")
        delete.setFixedWidth(66)
        delete.clicked.connect(
            lambda _checked=False, c=card: self.delete_preset(c)
        )
        header.addWidget(delete)

        test = QPushButton("▶ 测试方案")
        test.setObjectName("testAction")
        test.setFixedWidth(104)
        test.setToolTip("运行当前已应用的预设方案")
        test.clicked.connect(
            lambda _checked=False, c=card: self.test_selected_preset(c)
        )
        header.addWidget(test)

        record = QPushButton("● 录制动作")
        record.setObjectName("recordAction")
        record.setFixedWidth(104)
        record.setToolTip("录制键盘和鼠标操作并写入当前预设")
        record.clicked.connect(
            lambda _checked=False, c=card: self.open_recording_dialog(c)
        )
        header.addWidget(record)
        card_layout.addLayout(header)

        card.parameter_panel = QFrame()
        card.parameter_panel.setObjectName("parameterArea")
        settings = QHBoxLayout(card.parameter_panel)
        settings.setContentsMargins(12, 8, 12, 8)
        settings.setSpacing(8)

        card.loop_count = QSpinBox()
        card.loop_count.setRange(1, 100000)
        card.loop_count.setValue(int(preset.get("loop_count", 1)))
        card.count_group = self.labeled_control("执行次数", card.loop_count)
        settings.addWidget(card.count_group)

        card.loop_interval = QSpinBox()
        card.loop_interval.setRange(0, 600000)
        card.loop_interval.setSuffix(" ms")
        card.loop_interval.setValue(int(preset.get("loop_interval_ms", 0)))
        card.interval_group = self.labeled_control("轮间隔", card.loop_interval)
        settings.addWidget(card.interval_group)

        card.loop_interval_jitter = QSpinBox()
        card.loop_interval_jitter.setRange(0, 600000)
        card.loop_interval_jitter.setSuffix(" ms")
        card.loop_interval_jitter.setSpecialValueText("固定")
        card.loop_interval_jitter.setValue(
            int(preset.get("loop_interval_jitter_ms", 0))
        )
        card.loop_interval_jitter.setToolTip("每轮间隔随机上下浮动的最大范围")
        card.interval_jitter_group = self.labeled_control(
            "间隔随机 ±", card.loop_interval_jitter
        )
        settings.addWidget(card.interval_jitter_group)

        card.speed = QSpinBox()
        card.speed.setRange(10, 500)
        card.speed.setSuffix(" %")
        card.speed.setValue(int(preset.get("speed_percent", 100)))
        card.speed.setToolTip("按比例缩放动作保持、等待和轮间隔")
        card.speed_group = self.labeled_control("执行速度", card.speed)
        settings.addWidget(card.speed_group)

        card.max_runtime = QSpinBox()
        card.max_runtime.setRange(0, 86400)
        card.max_runtime.setSuffix(" 秒")
        card.max_runtime.setSpecialValueText("不限")
        card.max_runtime.setValue(int(preset.get("max_runtime_s", 0)))
        card.runtime_group = self.labeled_control("最长运行", card.max_runtime)
        settings.addWidget(card.runtime_group)
        settings.addStretch(1)
        card_layout.addWidget(card.parameter_panel)

        # 动作编辑器使用独立的非模态窗口，不再挤压预设列表的可视空间。
        card.action_dialog = QDialog(self)
        card.action_dialog.setObjectName("actionDialog")
        card.action_dialog.setWindowModality(Qt.WindowModality.NonModal)
        card.action_dialog.setModal(False)
        card.action_dialog.setMinimumSize(760, 480)
        available_screen = self.screen() or QApplication.primaryScreen()
        if available_screen is not None:
            available = available_screen.availableGeometry()
            dialog_width = min(1280, max(900, int(available.width() * 0.82)))
            dialog_height = min(820, max(520, int(available.height() * 0.78)))
            card.action_dialog.resize(dialog_width, dialog_height)
        else:
            card.action_dialog.resize(1120, 680)
        dialog_layout = QVBoxLayout(card.action_dialog)
        dialog_layout.setContentsMargins(14, 14, 14, 14)
        dialog_layout.setSpacing(0)

        action_panel = QFrame(card.action_dialog)
        action_panel.setObjectName("actionArea")
        card.action_panel = action_panel
        action_layout = QVBoxLayout(action_panel)
        action_layout.setContentsMargins(12, 10, 12, 12)
        action_layout.setSpacing(8)
        action_header = QHBoxLayout()
        card.action_title = QLabel("动作组 · 0 组 / 0 项")
        card.action_title.setObjectName("sectionLabel")
        action_header.addWidget(card.action_title)
        action_header.addStretch()
        up = QPushButton("↑")
        up.setToolTip("在同一层级内上移")
        up.setObjectName("secondary")
        up.clicked.connect(lambda _checked=False, c=card: self.move_action(-1, c))
        down = QPushButton("↓")
        down.setToolTip("在同一层级内下移")
        down.setObjectName("secondary")
        down.clicked.connect(lambda _checked=False, c=card: self.move_action(1, c))
        duplicate = QPushButton("复制")
        duplicate.setObjectName("secondary")
        duplicate.clicked.connect(
            lambda _checked=False, c=card: self.duplicate_selected_actions(c)
        )
        card.undo_button = QPushButton("撤销")
        card.undo_button.setObjectName("secondary")
        card.undo_button.setEnabled(False)
        card.undo_button.clicked.connect(
            lambda _checked=False, c=card: self.undo_actions(c)
        )
        card.redo_button = QPushButton("重做")
        card.redo_button.setObjectName("secondary")
        card.redo_button.setEnabled(False)
        card.redo_button.clicked.connect(
            lambda _checked=False, c=card: self.redo_actions(c)
        )
        organize = QPushButton("整理动作")
        organize.setObjectName("secondary")
        organize.setToolTip("重新整理当前预设中的密集鼠标移动、短等待和连续滚轮")
        organize.clicked.connect(
            lambda _checked=False, c=card: self.open_action_cleanup_dialog(c)
        )
        delete_selected = QPushButton("删除动作")
        delete_selected.setObjectName("dangerGhost")
        delete_selected.clicked.connect(
            lambda _checked=False, c=card: self.delete_selected_actions(c)
        )
        run_from_current = QPushButton("执行当前层后续动作")
        run_from_current.setObjectName("testAction")
        run_from_current.setToolTip(
            "从当前选中动作开始，只执行其所在层级中的当前动作及后续同级动作"
        )
        run_from_current.clicked.connect(
            lambda _checked=False, c=card: self.run_from_current_action(c)
        )
        record_from_current = QPushButton("从当前动作录制")
        record_from_current.setObjectName("recordAction")
        record_from_current.setToolTip(
            "录制完成后，可插入到当前动作下方，或覆盖当前层级中其后的所有动作"
        )
        record_from_current.clicked.connect(
            lambda _checked=False, c=card: self.record_from_current_action(c)
        )
        card.loop_points_button = QPushButton("插入循环点位")
        card.loop_points_button.setObjectName("loopActionButton")
        card.loop_points_button.setToolTip(
            "依次选择同一层级的开始动作和结束动作；循环卡片会添加到方案最下方，不移动原动作"
        )
        card.loop_points_button.clicked.connect(
            lambda _checked=False, c=card: self.loop_points_button_clicked(c)
        )
        card.loop_point_stage = 0
        card.loop_start_item = None
        card.loop_end_item = None
        card.loop_counter = 0
        add_key = QPushButton("＋ 键盘")
        add_key.setObjectName("secondary")
        add_key.setToolTip("选中动作时添加到其下方；未选中时添加到普通动作末尾")
        add_key.clicked.connect(lambda _checked=False, c=card: self.add_action_from_menu({
            "type": "键盘点击", "target": "A", "hold_ms": 100,
            "jitter_ms": 0, "children": [],
        }, card=c))
        add_mouse = QPushButton("＋ 鼠标")
        add_mouse.setObjectName("secondary")
        add_mouse.setToolTip("选中动作时添加到其下方；未选中时添加到普通动作末尾")
        add_mouse.clicked.connect(lambda _checked=False, c=card: self.add_action_from_menu({
            "type": "鼠标点击", "target": "鼠标左键", "hold_ms": 100,
            "jitter_ms": 0, "children": [],
        }, card=c))
        add_wheel = QPushButton("＋ 滚轮")
        add_wheel.setObjectName("secondary")
        add_wheel.setToolTip("选中动作时添加到其下方；未选中时添加到普通动作末尾")
        add_wheel.clicked.connect(lambda _checked=False, c=card: self.add_action_from_menu({
            "type": "鼠标滚轮", "target": "向上", "steps": 1,
            "children": [],
        }, card=c))
        add_wait = QPushButton("＋ 等待")
        add_wait.setObjectName("secondary")
        add_wait.setToolTip("选中动作时添加到其下方；未选中时添加到普通动作末尾")
        add_wait.clicked.connect(lambda _checked=False, c=card: self.add_action_from_menu({
            "type": "等待", "target": "仅等待", "wait_ms": 500,
            "jitter_ms": 0, "children": [],
        }, card=c))
        for button in (
            up, down, duplicate, card.undo_button, card.redo_button,
            organize, delete_selected,
        ):
            action_header.addWidget(button)
        action_layout.addLayout(action_header)

        action_insert_header = QHBoxLayout()
        action_insert_header.addWidget(run_from_current)
        action_insert_header.addWidget(record_from_current)
        action_insert_header.addWidget(card.loop_points_button)
        action_insert_header.addStretch(1)
        for button in (add_key, add_mouse, add_wheel, add_wait):
            action_insert_header.addWidget(button)
        action_layout.addLayout(action_insert_header)

        card.action_table = ActionTreeWidget()
        card.action_table.setObjectName("actionTable")
        card.action_table.setColumnCount(5)
        card.action_table.setHeaderLabels(
            ["层级 / 拖拽", "动作", "目标 / 坐标模式", "坐标 / 时长 / 数量 / 随机浮动", "操作"]
        )
        columns = card.action_table.header()
        columns.setStretchLastSection(False)
        columns.setSectionResizeMode(0, QHeaderView.Fixed)
        columns.setSectionResizeMode(1, QHeaderView.Fixed)
        columns.setSectionResizeMode(2, QHeaderView.Fixed)
        columns.setSectionResizeMode(3, QHeaderView.Stretch)
        columns.setSectionResizeMode(4, QHeaderView.Fixed)
        card.action_table.setColumnWidth(0, 145)
        card.action_table.setColumnWidth(1, 220)
        card.action_table.setColumnWidth(2, 190)
        # The duration editor contains the base value, “随机 ±” label and
        # jitter value. Give the column a wider initial size so neither
        # spin box is compressed when the preset panel first opens.
        card.action_table.setColumnWidth(3, 340)
        card.action_table.setColumnWidth(4, 76)
        card.action_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        card.action_table.drop_requested.connect(
            lambda source, target, position, c=card:
            self.handle_action_drop(c, source, target, position)
        )
        card.action_table.loop_point_clicked.connect(
            lambda item, c=card: self.select_loop_point(c, item)
        )
        card.undo_shortcut = QShortcut(
            QKeySequence.StandardKey.Undo, card.action_dialog
        )
        card.undo_shortcut.activated.connect(lambda c=card: self.undo_actions(c))
        card.redo_shortcut = QShortcut(
            QKeySequence.StandardKey.Redo, card.action_dialog
        )
        card.redo_shortcut.activated.connect(lambda c=card: self.redo_actions(c))
        card.delete_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Delete), card.action_dialog
        )
        card.delete_shortcut.activated.connect(
            lambda c=card: self.delete_selected_actions(c)
        )
        action_layout.addWidget(card.action_table)
        help_label = QLabel(
            "动作和子动作都可继续拖到其他动作下方形成多级结构；拖到条目前后可调整同层顺序。"
            "拖到可视范围边缘会自动滚动，也可在拖动时使用滚轮。“等待”会推迟同层后续动作。"
            "时间型动作可设置随机 ± 浮动，每次执行都会重新取值。"
            "“整理动作”可压缩同层级的密集鼠标轨迹和连续滚轮，并保留循环引用目标。"
            "“插入循环点位”会在方案最下方添加一个独立循环卡片，引用所选范围但不移动原动作。"
        )
        help_label.setWordWrap(True)
        help_label.setObjectName("muted")
        action_layout.addWidget(help_label)
        dialog_layout.addWidget(action_panel, 1)

        self.preset_cards.append(card)
        self.preset_layout.insertWidget(
            max(0, self.preset_layout.count() - 1), card
        )

        changed = lambda *_args, c=card: self.preset_card_changed(c)
        card.enabled.stateChanged.connect(changed)
        card.name.textChanged.connect(changed)
        card.trigger_hotkey.changed.connect(changed)
        card.loop_count.valueChanged.connect(changed)
        card.loop_interval.valueChanged.connect(changed)
        card.loop_interval_jitter.valueChanged.connect(changed)
        card.speed.valueChanged.connect(changed)
        card.max_runtime.valueChanged.connect(changed)
        card.execution_mode.currentTextChanged.connect(
            lambda text, c=card: self.on_preset_mode_changed(c, text)
        )
        card.parameters_button.toggled.connect(
            lambda expanded, c=card: self.toggle_preset_parameters(c, expanded)
        )
        card.actions_button.clicked.connect(
            lambda _checked=False, c=card: self.open_preset_actions_dialog(c)
        )

        for widget in (
            card, card.enabled, card.name, card.trigger_hotkey,
            card.execution_mode, card.loop_count, card.loop_interval,
            card.loop_interval_jitter, card.speed, card.max_runtime,
            card.action_table,
        ):
            widget.installEventFilter(self)

        pending_actions = preset.get("actions", []) or []
        card._pending_actions = pending_actions
        card._pending_action_count = sum(1 for _ in iter_action_tree(pending_actions))
        card._actions_loaded = False
        if not getattr(self, "defer_preset_action_rows", False):
            self.load_actions(pending_actions, card)

        card.action_undo_history = []
        card.action_redo_history = []
        card.action_history_suspended = False
        if getattr(card, "_actions_loaded", False):
            self._record_action_history(card, force=True)

        card.parameters_button.setChecked(False)
        self.refresh_preset_parameter_panel(card)
        self.update_card_action_summary(card)
        self.update_preset_action_dialog_title(card)
        self.select_preset_card(card)
        self._loading_checkpoint(force=True)
        self.data_changed()

    def _ensure_preset_actions_loaded(self, card):
        if card not in self.preset_cards:
            return False
        if getattr(card, "_actions_loaded", False):
            return True
        actions = getattr(card, "_pending_actions", []) or []
        loaded = self.load_actions(actions, card)
        if loaded is False:
            return False
        card.action_undo_history = []
        card.action_redo_history = []
        self._record_action_history(card, force=True)
        return True

    def on_preset_mode_changed(self, card, _mode):
        self.select_preset_card(card)
        self.refresh_preset_parameter_panel(card)
        self.data_changed()

    def toggle_preset_parameters(self, card, expanded):
        card.parameter_panel.setVisible(bool(expanded))
        card.parameters_button.setText("参数 ▾" if expanded else "参数 ▸")

    def refresh_preset_parameter_panel(self, card):
        mode = card.execution_mode.currentText()
        visible_groups = {
            "执行一次": {"speed"},
            "固定次数": {"count", "interval", "interval_jitter", "speed"},
            "按住循环": {"interval", "interval_jitter", "speed"},
            "开关循环": {"interval", "interval_jitter", "speed", "runtime"},
            "无限循环": {"interval", "interval_jitter", "speed", "runtime"},
        }.get(mode, {"speed"})
        groups = {
            "count": card.count_group,
            "interval": card.interval_group,
            "interval_jitter": card.interval_jitter_group,
            "speed": card.speed_group,
            "runtime": card.runtime_group,
        }
        for name, group in groups.items():
            group.setVisible(name in visible_groups)
        self.toggle_preset_parameters(
            card, card.parameters_button.isChecked()
        )

    def update_preset_action_dialog_title(self, card):
        if card is None or not hasattr(card, "action_dialog"):
            return
        name = card.name.text().strip() if hasattr(card, "name") else ""
        card.action_dialog.setWindowTitle(
            f"动作编辑 · {name or '未命名预设'}"
        )

    def open_preset_actions_dialog(self, card):
        if card not in self.preset_cards or not hasattr(card, "action_dialog"):
            return
        self.select_preset_card(card)
        if not self._ensure_preset_actions_loaded(card):
            return
        self.update_preset_action_dialog_title(card)
        self.update_card_action_summary(card)

        # 同一时间只保留一个动作编辑窗口，避免多个方案窗口彼此遮挡。
        for other in self.preset_cards:
            if other is card or not hasattr(other, "action_dialog"):
                continue
            if other.action_dialog.isVisible():
                other.action_dialog.hide()

        dialog = card.action_dialog
        if not dialog.isVisible():
            main_geometry = self.frameGeometry()
            dialog_geometry = dialog.frameGeometry()
            dialog_geometry.moveCenter(main_geometry.center())
            dialog.move(dialog_geometry.topLeft())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        QTimer.singleShot(0, card.action_table._refresh_overlay)

    def toggle_preset_actions(self, card, expanded):
        # 兼容旧调用：动作面板已改为独立窗口。
        if expanded:
            self.open_preset_actions_dialog(card)
        elif hasattr(card, "action_dialog"):
            card.action_dialog.hide()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.MouseButtonPress:
            for card in getattr(self, "preset_cards", []):
                action_dialog = getattr(card, "action_dialog", None)
                inside_card = (
                    watched is card
                    or (
                        isinstance(watched, QWidget)
                        and card.isAncestorOf(watched)
                    )
                )
                inside_action_dialog = (
                    action_dialog is not None
                    and (
                        watched is action_dialog
                        or watched is getattr(card, "action_table", None)
                        or (
                            isinstance(watched, QWidget)
                            and action_dialog.isAncestorOf(watched)
                        )
                    )
                )
                if inside_card or inside_action_dialog:
                    self.select_preset_card(card)
                    break
        return super().eventFilter(watched, event)

    def preset_card_changed(self, card):
        self.select_preset_card(card)
        self.update_preset_action_dialog_title(card)
        self.data_changed()

    def select_preset_card(self, card):
        if card not in self.preset_cards:
            return
        self.selected_preset_card = card
        self.action_table = card.action_table
        self.action_title = card.action_title
        for item in self.preset_cards:
            item.setProperty("selected", item is card)
            item.style().unpolish(item)
            item.style().polish(item)

    def _suspend_preset_runtime_for_delete(self, preset_id):
        """Temporarily remove one preset from new-trigger eligibility only."""
        snapshot = {"presets": [], "rules": []}
        self.suspended_preset_ids.add(preset_id)
        with self.data_lock:
            for index, preset in enumerate(self.runtime_presets):
                if preset.get("id") == preset_id:
                    snapshot["presets"].append((index, preset))
            for index, rule in enumerate(self.runtime_trigger_rules):
                if (
                    rule.get("_runtime_kind") == "preset"
                    and rule.get("id") == preset_id
                ):
                    snapshot["rules"].append((index, rule))

            self.runtime_presets = [
                preset for preset in self.runtime_presets
                if preset.get("id") != preset_id
            ]
            self.runtime_trigger_rules = [
                rule for rule in self.runtime_trigger_rules
                if not (
                    rule.get("_runtime_kind") == "preset"
                    and rule.get("id") == preset_id
                )
            ]
        return snapshot

    def _stop_preset_runtime_for_delete(self, preset_id):
        """Stop only the confirmed preset after the delete decision is made."""
        if str(getattr(self, "_test_countdown_preset_id", "") or "") == str(
            preset_id or ""
        ):
            self._test_countdown_generation += 1
            self._test_countdown_preset_id = None
            if self.macro_state == MacroState.COUNTDOWN:
                self.macro_state = MacroState.IDLE
                self.macro_status_detail = ""
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()

        preset_id = str(preset_id or "")
        with self.macro_controller.lock:
            related_task_ids = {
                str(task_id)
                for task_id, task in self.macro_controller.tasks.items()
                if str(task_id) == preset_id
                or str(task.preset.get("_origin_preset_id") or "") == preset_id
            }
        related_task_ids.add(preset_id)
        stop_task = getattr(self, "_request_stop_macro_task", None)
        for task_id in related_task_ids:
            if callable(stop_task):
                stop_task(task_id, "正在停止被删除预设的运行任务")
            else:
                self.macro_controller.stop(task_id)

        normalized_id = "".join(
            character.lower() for character in preset_id
            if character.isalnum()
        )
        with self.input_state_lock:
            for trigger_token in list(self.held_trigger_ids):
                task_ids = self.held_trigger_ids.get(trigger_token)
                if not task_ids:
                    self.held_trigger_ids.pop(trigger_token, None)
                    continue
                task_ids.difference_update(related_task_ids)
                if not task_ids:
                    self.held_trigger_ids.pop(trigger_token, None)
            self.kanata_trigger_down = {
                token for token in self.kanata_trigger_down
                if not token.endswith(f":preset:{normalized_id}")
            }

    def _restore_suspended_preset_runtime(self, snapshot):
        """Restore trigger eligibility when the delete dialog is cancelled."""
        if not snapshot:
            return
        preset_ids = {
            preset.get("id")
            for _index, preset in snapshot.get("presets", [])
            if preset.get("id")
        }
        self.suspended_preset_ids.difference_update(preset_ids)
        with self.data_lock:
            for index, preset in sorted(snapshot.get("presets", [])):
                if not any(
                    item.get("id") == preset.get("id")
                    for item in self.runtime_presets
                ):
                    self.runtime_presets.insert(
                        min(index, len(self.runtime_presets)), preset
                    )
            for index, rule in sorted(snapshot.get("rules", [])):
                if not any(
                    item.get("_runtime_kind") == "preset"
                    and item.get("id") == rule.get("id")
                    for item in self.runtime_trigger_rules
                ):
                    self.runtime_trigger_rules.insert(
                        min(index, len(self.runtime_trigger_rules)), rule
                    )

    def _restore_discarded_preset_deletions(self, profile_id):
        profile_id = str(profile_id or "")
        restored = False
        for preset_id, snapshot in list(self.pending_preset_deletions.items()):
            if str(snapshot.get("profile_id") or "") != profile_id:
                continue
            self._restore_suspended_preset_runtime(snapshot)
            self.pending_preset_deletions.pop(preset_id, None)
            restored = True
        return restored

    def delete_preset(self, card):
        if self._configuration_change_blocked_by_recording():
            return
        if card not in self.preset_cards:
            return

        preset_id = card.preset_id
        preset_name = card.name.text().strip() or "未命名预设"

        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("确认删除预设")
        confirm.setText(f"确定删除预设“{preset_name}”吗？")
        confirm.setInformativeText(
            "删除确认后，该预设才会停止正在执行的任务并从当前运行触发中暂停；"
            "点击“应用更改”后才会写入本地配置。放弃当前修改则恢复该预设。"
        )
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

        runtime_snapshot = self._suspend_preset_runtime_for_delete(preset_id)
        runtime_snapshot["profile_id"] = str(self.editor_profile_id or "")
        self._stop_preset_runtime_for_delete(preset_id)
        self.pending_preset_deletions[str(preset_id or "")] = runtime_snapshot

        index = self.preset_cards.index(card)
        if hasattr(card, "action_dialog"):
            card.action_dialog.close()
            card.action_dialog.deleteLater()
        self.preset_cards.remove(card)
        self.preset_layout.removeWidget(card)
        card.deleteLater()

        if self.preset_cards:
            self.select_preset_card(
                self.preset_cards[min(index, len(self.preset_cards) - 1)]
            )
        else:
            self.selected_preset_card = None
            self.action_table = None
            self.action_title = None

        self.data_changed()
        self.engine_hint.setStyleSheet("")
        self.engine_hint.setText(
            f"已删除“{preset_name}”；运行触发已暂停，应用更改后写入配置"
        )

    def selected_preset_row(self):
        try:
            return self.preset_cards.index(self.selected_preset_card)
        except (ValueError, AttributeError):
            return -1
