from __future__ import annotations

import time
import uuid

from PySide6.QtCore import QEventLoop, QSize, QTimer, Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QInputDialog, QMessageBox, QPushButton, QSplitter, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from config.profiles import normalize_profile, profile_matches
from config.schema import MAX_PROFILE_COUNT
from engine.window_context import (
    foreground_window_belongs_to_current_process, foreground_window_context,
)
from ui.overlays import LoadingOverlay


class ProfileManagerDialog(QDialog):
    """Single-window editor for profile matching rules and snapshots."""

    def __init__(
        self, profiles, base_payload, auto_switch_enabled,
        active_profile_id="", foreground_context=("", ""), parent=None,
        current_payload=None, selected_profile_id=None, save_callback=None,
        status_overlay_callback=None, status_overlay_hide_callback=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("配置档案与自动切换")
        # 档案管理窗口采用固定布局，避免拖动窗口或分隔条后造成
        # 左侧名称列、状态开关和删除按钮再次被挤压。
        self.resize(980, 650)
        self.setMinimumSize(760, 500)
        self.profiles = [
            self._normalize_profile_for_dialog(item) for item in profiles or []
        ]
        self.base_payload = self._payload_reference(
            base_payload or {"mappings": [], "presets": []}
        )
        # ``current_payload`` is accepted for older call sites, but the manager
        # does not render or mutate it.  Avoid deep-copying large action trees
        # every time the dialog opens.
        self.current_payload = None
        self.active_profile_id = str(active_profile_id or "")
        self.selected_profile_id = str(
            self.active_profile_id if selected_profile_id is None
            else selected_profile_id or ""
        )
        foreground_is_self = foreground_window_belongs_to_current_process()
        self.foreground_process = (
            "" if foreground_is_self else str(foreground_context[0] or "")
        )
        self.foreground_title = (
            "" if foreground_is_self else str(foreground_context[1] or "")
        )
        self.requested_load_payload = None  # legacy compatibility
        self.requested_load_id = None
        self.requested_activate_id = None
        self.save_callback = save_callback
        self.status_overlay_callback = status_overlay_callback
        self.status_overlay_hide_callback = status_overlay_hide_callback
        self._capture_countdown_generation = 0
        self._updating = False
        self._summary_cache = {}
        self._base_summary = None
        self._summary_update_generation = 0
        self._capacity_update_generation = 0
        self.loading_overlay = LoadingOverlay(self)

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        self.auto_switch = QCheckBox("根据前台窗口自动切换")
        self.auto_switch.setChecked(bool(auto_switch_enabled))
        self.auto_switch.setToolTip(
            "按档案列表顺序匹配。匹配不到任何档案时自动恢复基础配置。"
        )
        header.addWidget(self.auto_switch)
        header.addStretch(1)
        self.capacity_label = QLabel()
        self.capacity_label.setObjectName("muted")
        self.capacity_label.setToolTip(
            "普通模式会把基础配置和所有已启用档案一次性编译进 Kanata。"
        )
        header.addWidget(self.capacity_label)
        base_button = QPushButton("保存并编辑基础配置")
        base_button.setObjectName("secondary")
        base_button.setToolTip(
            "保存档案管理器中的修改并让主界面编辑基础配置；"
            "不会绕过“应用更改”直接替换当前运行档案。"
        )
        base_button.clicked.connect(self.activate_base)
        header.addWidget(base_button)
        root.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        root.addWidget(splitter, 1)

        left = QFrame()
        left.setObjectName("card")
        left.setFixedWidth(300)
        left_layout = QVBoxLayout(left)

        left_header = QHBoxLayout()
        left_title = QLabel("配置档案")
        left_title.setObjectName("sectionTitle")
        left_header.addWidget(left_title)
        left_header.addStretch(1)
        move_up_button = QPushButton("↑")
        move_up_button.setObjectName("secondary")
        move_up_button.setFixedWidth(34)
        move_up_button.setToolTip("提高当前档案的匹配优先级")
        move_up_button.clicked.connect(lambda: self.move_profile(-1))
        left_header.addWidget(move_up_button)
        move_down_button = QPushButton("↓")
        move_down_button.setObjectName("secondary")
        move_down_button.setFixedWidth(34)
        move_down_button.setToolTip("降低当前档案的匹配优先级")
        move_down_button.clicked.connect(lambda: self.move_profile(1))
        left_header.addWidget(move_down_button)
        add_button = QPushButton("＋ 新建")
        add_button.setObjectName("primary")
        add_button.setToolTip("创建一个默认停用、不含映射和预设的空白配置档案")
        add_button.clicked.connect(self.add_profile)
        left_header.addWidget(add_button)
        left_layout.addLayout(left_header)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["状态", "档案", ""])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        # 档案列表中的“选中”只用于切换右侧详情，不给名称单元格铺整块高亮背景。
        # 状态复选框和删除按钮使用单独控件，因此保持整行透明能避免中间名称区域突兀。
        self.tree.setStyleSheet(
            "QTreeWidget { background: transparent; }"
            "QTreeWidget::item { background: transparent; border: none; padding: 4px 2px; }"
            "QTreeWidget::item:selected { background: transparent; color: white; }"
            "QTreeWidget::item:hover { background: rgba(91, 99, 128, 0.16); }"
        )
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.setIndentation(0)
        header_view = self.tree.header()
        header_view.setMinimumSectionSize(28)
        header_view.setSectionResizeMode(0, QHeaderView.Fixed)
        header_view.setSectionResizeMode(1, QHeaderView.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tree.setColumnWidth(0, 42)
        self.tree.setColumnWidth(2, 36)
        self.tree.currentItemChanged.connect(self.load_selected)
        left_layout.addWidget(self.tree, 1)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("card")
        right_layout = QVBoxLayout(right)
        title = QLabel("档案详情")
        title.setObjectName("sectionTitle")
        right_layout.addWidget(title)

        self.empty_hint = QLabel("请在左侧选择档案，或新建一个配置档案。")
        self.empty_hint.setObjectName("muted")
        right_layout.addWidget(self.empty_hint)

        self.detail = QWidget()
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.process_edit = QLineEdit()
        self.process_edit.setPlaceholderText("例如 game.exe, launcher.exe")
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("例如 游戏名称, 登录界面；可留空")
        form.addRow("档案名称", self.name_edit)
        form.addRow("进程名", self.process_edit)
        form.addRow("标题包含", self.title_edit)
        detail_layout.addLayout(form)

        match_note = QLabel(
            "同一栏内多个条件为“任意一个”；进程名和标题同时填写时，两栏必须同时匹配。"
        )
        match_note.setWordWrap(True)
        match_note.setObjectName("muted")
        detail_layout.addWidget(match_note)

        current_box = QFrame()
        current_box.setObjectName("parameterArea")
        current_layout = QVBoxLayout(current_box)
        self.current_context = QLabel()
        self.current_context.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.current_context.setWordWrap(True)
        current_layout.addWidget(self.current_context)
        context_buttons = QHBoxLayout()
        self.use_process_button = QPushButton("使用已捕获程序")
        self.use_process_button.setObjectName("secondary")
        self.use_process_button.clicked.connect(self.use_current_process)
        self.use_title_button = QPushButton("使用已捕获标题")
        self.use_title_button.setObjectName("secondary")
        self.use_title_button.setToolTip(
            "先确认一个稳定的标题片段，避免文档名或网页标题变化后失去匹配"
        )
        self.use_title_button.clicked.connect(self.use_current_title)
        capture_button = QPushButton("捕获目标窗口（5 秒）")
        capture_button.setObjectName("secondary")
        capture_button.setToolTip(
            "点击后管理器会暂时隐藏，请在 5 秒内切换到需要匹配的窗口"
        )
        capture_button.clicked.connect(self.capture_target_window)
        test_button = QPushButton("测试已捕获窗口")
        test_button.setObjectName("secondary")
        test_button.clicked.connect(self.test_current_window)
        context_buttons.addWidget(capture_button)
        context_buttons.addWidget(self.use_process_button)
        context_buttons.addWidget(self.use_title_button)
        context_buttons.addWidget(test_button)
        context_buttons.addStretch(1)
        current_layout.addLayout(context_buttons)
        self.match_result = QLabel("")
        self.match_result.setObjectName("muted")
        current_layout.addWidget(self.match_result)
        detail_layout.addWidget(current_box)

        self.summary = QLabel()
        self.summary.setObjectName("muted")
        detail_layout.addWidget(self.summary)

        detail_layout.addStretch(1)
        right_layout.addWidget(self.detail, 1)
        splitter.addWidget(right)
        splitter.setSizes([300, 680])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        # 固定左右区域宽度，不允许用户拖动中间分隔条。
        splitter.handle(1).setEnabled(False)

        self.name_edit.textChanged.connect(self.editor_changed)
        self.process_edit.textChanged.connect(self.editor_changed)
        self.title_edit.textChanged.connect(self.editor_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Save).setText("暂存档案修改")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.refresh_tree()
        if self.profiles:
            selected = 0
            for index, profile in enumerate(self.profiles):
                if str(profile.get("id")) == self.selected_profile_id:
                    selected = index
                    break
            self.tree.setCurrentItem(self.tree.topLevelItem(selected))
        else:
            self.load_selected(None, None)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.loading_overlay.isVisible():
            self.loading_overlay.sync_geometry()

    @staticmethod
    def _split_values(text):
        return [
            value.strip()
            for value in str(text or "").replace("，", ",").split(",")
            if value.strip()
        ]

    @staticmethod
    def _profile_has_match_condition(profile):
        return bool(
            (profile.get("process_names") or [])
            or (profile.get("title_contains") or [])
        )

    @staticmethod
    def _payload_reference(payload):
        if not isinstance(payload, dict):
            return {"mappings": [], "presets": []}
        return {
            "mappings": payload.get("mappings", []) or [],
            "presets": payload.get("presets", []) or [],
        }

    @staticmethod
    def _normalize_profile_for_dialog(profile):
        """Normalize editable profile metadata without copying large actions."""
        item = profile if isinstance(profile, dict) else {}
        return {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or "未命名档案"),
            "enabled": bool(item.get("enabled", False)),
            "process_names": [
                str(value).strip()
                for value in item.get("process_names", [])
                if str(value).strip()
            ],
            "title_contains": [
                str(value).strip()
                for value in item.get("title_contains", [])
                if str(value).strip()
            ],
            # Payload is not edited in this dialog; keep the original reference
            # so opening the manager does not deep-copy long recorded macros.
            "payload": ProfileManagerDialog._payload_reference(
                item.get("payload") or {}
            ),
            "allow_other_windows": False,
        }

    @staticmethod
    def _summary_from_payload(payload):
        payload = ProfileManagerDialog._payload_reference(payload)
        mappings = payload["mappings"]
        presets = payload["presets"]
        mouse_names = {
            "鼠标左键", "鼠标右键", "鼠标中键", "鼠标侧键 1", "鼠标侧键 2",
        }
        action_total = 0
        action_outputs = 0
        branch_types = {"条件成立分支", "否则分支"}
        non_output_types = {
            "等待", "循环动作", "调用子宏", "条件分支", "等待条件",
            *branch_types,
        }
        for preset in presets:
            stack = list(preset.get("actions", []) or [])
            while stack:
                action = stack.pop()
                if action.get("type") not in branch_types:
                    action_total += 1
                if action.get("type") not in non_output_types:
                    action_outputs += 1
                stack.extend(action.get("children", []) or [])

        virtual_keys = len(mappings) + action_outputs
        for mapping in mappings:
            if not mapping.get("enabled"):
                continue
            source = mapping.get("source", "F6")
            mode = mapping.get("mode", "同步按住")
            if source not in mouse_names and mode != "同步按住":
                virtual_keys += 2
            elif source in mouse_names and mode in ("按住循环", "开关循环", "无限循环"):
                virtual_keys += 1
        virtual_keys += 2 * sum(1 for item in presets if item.get("enabled"))

        return {
            "mappings": len(mappings),
            "presets": len(presets),
            "actions": action_total,
            "virtual_keys": virtual_keys,
        }

    @staticmethod
    def _style_toggle_button(button, checked):
        # 使用与窗口顶部“根据前台窗口自动切换”一致的方形复选框，
        # 不再在狭窄状态列中显示“开 / 关”文字。
        button.setToolTip("点击停用此档案" if checked else "点击启用此档案")

    def selected_index(self):
        item = self.tree.currentItem()
        return self.tree.indexOfTopLevelItem(item) if item else -1

    def selected_profile(self):
        index = self.selected_index()
        if 0 <= index < len(self.profiles):
            return self.profiles[index]
        return None

    def _find_profile_index(self, profile_id):
        profile_id = str(profile_id or "")
        for index, profile in enumerate(self.profiles):
            if str(profile.get("id") or "") == profile_id:
                return index
        return -1

    def _save_editor(self):
        if self._updating:
            return
        profile = self.selected_profile()
        if not profile:
            return
        profile["name"] = self.name_edit.text().strip() or "未命名档案"
        profile["process_names"] = self._split_values(self.process_edit.text())
        profile["title_contains"] = self._split_values(self.title_edit.text())
        profile["allow_other_windows"] = False

    def _cached_profile_summary(self, profile):
        if not isinstance(profile, dict):
            return self._summary_from_payload({"mappings": [], "presets": []})
        profile_id = str(profile.get("id") or "")
        payload = profile.get("payload") or {}
        signature = (
            profile_id,
            id(payload),
            len(payload.get("mappings", []) or []),
            len(payload.get("presets", []) or []),
        )
        cached = self._summary_cache.get(profile_id)
        if cached and cached[0] == signature:
            return cached[1]
        summary = self._summary_from_payload(payload)
        self._summary_cache[profile_id] = (signature, summary)
        return summary

    def _cached_base_summary(self):
        if self._base_summary is None:
            self._base_summary = self._summary_from_payload(self.base_payload)
        return self._base_summary

    def editor_changed(self, *_args):
        if self._updating:
            return
        index = self.selected_index()
        self._save_editor()
        profile = self.selected_profile()
        item = self.tree.topLevelItem(index) if index >= 0 else None
        if profile is not None and item is not None:
            active = str(profile.get("id")) == self.active_profile_id
            name = str(profile.get("name") or "未命名档案")
            item.setText(1, name)
            item.setToolTip(1, "当前运行档案" if active else name)
        self.update_summary()

    def refresh_tree(self, selected_index=None):
        if selected_index is None:
            selected_index = self.selected_index()
        self.tree.blockSignals(True)
        self.tree.clear()
        for profile in self.profiles:
            profile_id = str(profile.get("id") or "")
            active = profile_id == self.active_profile_id
            name = str(profile.get("name") or "未命名档案")
            item = QTreeWidgetItem(["", name, ""])
            # 24px 方形开关和 28px 删除按钮需要略高于默认行高，
            # 否则在部分 DPI / 字体设置下会被上下裁剪。
            row_height = 38
            item.setSizeHint(0, QSize(42, row_height))
            item.setSizeHint(1, QSize(0, row_height))
            item.setSizeHint(2, QSize(36, row_height))
            item.setToolTip(1, "当前运行档案" if active else name)
            item.setData(0, Qt.UserRole, profile_id)
            self.tree.addTopLevelItem(item)

            toggle = QCheckBox()
            toggle.setChecked(bool(profile.get("enabled", False)))
            toggle.setFixedSize(24, 24)
            self._style_toggle_button(toggle, toggle.isChecked())
            toggle.toggled.connect(
                lambda checked, pid=profile_id, button=toggle, tree_item=item:
                self.set_profile_enabled(pid, checked, button, tree_item)
            )
            toggle_holder = QWidget()
            toggle_layout = QHBoxLayout(toggle_holder)
            toggle_layout.setContentsMargins(8, 2, 0, 2)
            toggle_layout.setSpacing(0)
            toggle_layout.addWidget(toggle)
            toggle_layout.addStretch(1)
            self.tree.setItemWidget(item, 0, toggle_holder)

            delete_button = QPushButton("×")
            delete_button.setFixedSize(28, 28)
            delete_button.setCursor(Qt.PointingHandCursor)
            # 全局 dangerGhost 样式带有较大的左右 padding，在 26px 小按钮内会把“×”挤掉。
            # 这里使用专用紧凑样式，保证图标始终居中可见。
            delete_button.setStyleSheet(
                "QPushButton {"
                " background: transparent; color: #ff8496; border: 1px solid transparent;"
                " border-radius: 6px; padding: 0; margin: 0;"
                " font-size: 18px; font-weight: 700;"
                "}"
                "QPushButton:hover { background: #34212a; border-color: #ff8496; }"
                "QPushButton:pressed { background: #492933; }"
            )
            delete_button.setToolTip(f"删除档案“{name}”")
            delete_button.clicked.connect(
                lambda _checked=False, pid=profile_id: self.delete_profile_by_id(pid)
            )
            delete_holder = QWidget()
            delete_holder.setStyleSheet("background: transparent;")
            delete_layout = QHBoxLayout(delete_holder)
            delete_layout.setContentsMargins(4, 2, 4, 2)
            delete_layout.setSpacing(0)
            delete_layout.addWidget(delete_button)
            self.tree.setItemWidget(item, 2, delete_holder)

        self.tree.blockSignals(False)
        if self.profiles:
            selected_index = max(
                0,
                min(
                    selected_index if selected_index is not None else 0,
                    len(self.profiles) - 1,
                ),
            )
            self.tree.setCurrentItem(self.tree.topLevelItem(selected_index))
        else:
            self.load_selected(None, None)
        self.capacity_label.setText("普通模式虚拟键预计：保存时校验")
        self.capacity_label.setStyleSheet("")
        self.capacity_label.setToolTip(
            "为保持档案管理器打开流畅，完整虚拟键容量会在保存/应用时校验。"
        )

    def set_profile_enabled(self, profile_id, checked, button=None, item=None):
        index = self._find_profile_index(profile_id)
        if index < 0:
            return
        self._save_editor()
        profile = self.profiles[index]
        if checked and not self._profile_has_match_condition(profile):
            QMessageBox.warning(
                self,
                "档案匹配条件",
                "该档案还没有填写进程名或标题包含，启用后也不会匹配任何窗口。"
                "请先设置至少一个匹配条件。",
            )
            profile["enabled"] = False
            if button is not None:
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
                self._style_toggle_button(button, False)
            if item is not None:
                self.tree.setCurrentItem(item)
            self.schedule_summary_update()
            self.capacity_label.setText("普通模式虚拟键预计：保存时校验")
            return
        profile["enabled"] = bool(checked)
        if not checked and str(profile.get("id") or "") == self.active_profile_id:
            self.active_profile_id = ""
            if item is not None:
                item.setText(1, str(profile.get("name") or "未命名档案"))
                item.setToolTip(1, str(profile.get("name") or "未命名档案"))
        if button is not None:
            self._style_toggle_button(button, bool(checked))
        if item is not None:
            self.tree.setCurrentItem(item)
        self.schedule_summary_update()
        self.capacity_label.setText("普通模式虚拟键预计：保存时校验")

    def load_selected(self, current, _previous):
        profile = self.selected_profile()
        self._updating = True
        self.detail.setVisible(profile is not None)
        self.empty_hint.setVisible(profile is None)
        if profile:
            self.name_edit.setText(str(profile.get("name") or ""))
            self.process_edit.setText(", ".join(profile.get("process_names", [])))
            self.title_edit.setText(", ".join(profile.get("title_contains", [])))
        self._updating = False
        self.refresh_current_context_text()
        self.match_result.clear()
        self.schedule_summary_update()

    def schedule_summary_update(self):
        self._summary_update_generation += 1
        generation = self._summary_update_generation
        profile = self.selected_profile()
        if not profile:
            self.summary.clear()
            return
        payload = self._payload_reference(profile.get("payload") or {})
        self.summary.setText(
            f"档案状态：{'已启用' if profile.get('enabled', False) else '已停用'}；"
            f"内容：{len(payload['mappings'])} 个基础映射，"
            f"{len(payload['presets'])} 个预设；动作项稍后统计。"
        )
        QTimer.singleShot(120, lambda: self._finish_summary_update(generation))

    def _finish_summary_update(self, generation):
        if generation != self._summary_update_generation:
            return
        self.update_summary()

    def update_summary(self):
        profile = self.selected_profile()
        if not profile:
            self.summary.clear()
            return
        summary = self._cached_profile_summary(profile)
        state = "已启用" if profile.get("enabled", False) else "已停用"
        self.summary.setText(
            f"档案状态：{state}；内容：{summary['mappings']} 个基础映射，"
            f"{summary['presets']} 个预设，{summary['actions']} 个动作项；"
            f"普通模式预计占用 {summary['virtual_keys']} 个 Kanata 虚拟键。\n"
            "档案只保存映射和预设；输入模式、急停键、录制键等继续使用全局设置。"
        )
        self.capacity_label.setText("普通模式虚拟键预计：保存时校验")

    def estimated_virtual_key_total(self):
        total = 1 + self._cached_base_summary()["virtual_keys"]
        total += sum(
            self._cached_profile_summary(profile)["virtual_keys"]
            for profile in self.profiles
            if profile.get("enabled", False)
        )
        return total

    def update_capacity_label(self):
        total = self.estimated_virtual_key_total()
        self.capacity_label.setText(f"普通模式虚拟键预计：{total} / 767")
        if total > 767:
            self.capacity_label.setStyleSheet("color: #ff8496;")
            self.capacity_label.setToolTip(
                "已超过普通模式的 Kanata 虚拟键上限。请禁用部分档案，"
                "或减少映射和动作后再应用。游戏模式不使用该上限。"
            )
        else:
            self.capacity_label.setStyleSheet("")
            self.capacity_label.setToolTip(
                "普通模式会把基础配置和所有已启用档案一次性编译进 Kanata。"
            )

    def schedule_capacity_label_update(self):
        self._capacity_update_generation += 1
        generation = self._capacity_update_generation
        self.capacity_label.setText("普通模式虚拟键预计：正在统计…")
        state = {
            "generation": generation,
            "index": 0,
            "total": 1 + self._cached_base_summary()["virtual_keys"],
        }
        QTimer.singleShot(0, lambda: self._capacity_label_step(state))

    def _capacity_label_step(self, state):
        if state.get("generation") != self._capacity_update_generation:
            return
        started = time.perf_counter()
        while state["index"] < len(self.profiles):
            profile = self.profiles[state["index"]]
            state["index"] += 1
            if profile.get("enabled", False):
                state["total"] += self._cached_profile_summary(profile)["virtual_keys"]
            if time.perf_counter() - started >= 0.006:
                self.capacity_label.setText(
                    f"普通模式虚拟键预计：正在统计 {state['index']} / "
                    f"{len(self.profiles)}…"
                )
                QTimer.singleShot(0, lambda: self._capacity_label_step(state))
                return
        self._apply_capacity_label_total(state["total"])

    def _apply_capacity_label_total(self, total):
        self.capacity_label.setText(f"普通模式虚拟键预计：{total} / 767")
        if total > 767:
            self.capacity_label.setStyleSheet("color: #ff8496;")
            self.capacity_label.setToolTip(
                "已超过普通模式的 Kanata 虚拟键上限。请禁用部分档案，"
                "或减少映射和动作后再应用。游戏模式不使用该上限。"
            )
        else:
            self.capacity_label.setStyleSheet("")
            self.capacity_label.setToolTip(
                "普通模式会把基础配置和所有已启用档案一次性编译进 Kanata。"
            )

    def move_profile(self, direction):
        """Move the selected profile and therefore its first-match priority."""
        self._save_editor()
        index = self.selected_index()
        target = index + int(direction)
        if index < 0 or target < 0 or target >= len(self.profiles):
            return
        self.profiles[index], self.profiles[target] = (
            self.profiles[target], self.profiles[index]
        )
        self.refresh_tree(target)
        self.match_result.setStyleSheet("")
        self.match_result.setText(
            "档案顺序已调整；列表越靠上，重叠规则的匹配优先级越高。"
        )

    def add_profile(self):
        self._save_editor()
        if len(self.profiles) >= MAX_PROFILE_COUNT:
            QMessageBox.warning(
                self,
                "无法新建档案",
                f"配置档案数量已达到上限 {MAX_PROFILE_COUNT}。",
            )
            return
        self.profiles.append(normalize_profile({
            "id": uuid.uuid4().hex,
            "name": f"新配置档案 {len(self.profiles) + 1}",
            "enabled": False,
            "process_names": [],
            "title_contains": [],
            # 新档案从空白内容开始，避免无意复制当前主界面的
            # 映射、预设和大量动作。主界面切换到该档案后再自行添加。
            "payload": {"mappings": [], "presets": []},
        }))
        self.refresh_tree(len(self.profiles) - 1)

    def delete_profile_by_id(self, profile_id):
        index = self._find_profile_index(profile_id)
        if index < 0:
            return
        profile = self.profiles[index]
        answer = QMessageBox.question(
            self,
            "删除配置档案",
            f"确定删除“{profile.get('name', '未命名档案')}”吗？\n"
            "该档案的匹配规则、映射和预设快照都会删除。若主界面正在编辑"
            "该档案，保存后会返回基础配置；基础配置本身不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed_id = str(profile.get("id") or "")
        self.profiles.pop(index)
        if removed_id == self.active_profile_id:
            self.active_profile_id = ""
        self.refresh_tree(min(index, len(self.profiles) - 1))

    def refresh_current_context_text(self):
        has_process = bool(self.foreground_process)
        has_title = bool(self.foreground_title)
        self.use_process_button.setEnabled(has_process)
        self.use_title_button.setEnabled(has_title)
        if not has_process and not has_title:
            self.current_context.setText(
                "尚未捕获有效目标窗口。请点击“捕获目标窗口（5 秒）”，"
                "再切换到需要绑定的程序。"
            )
            return
        self.current_context.setText(
            f"已捕获程序：{self.foreground_process or '未知'}\n"
            f"已捕获窗口标题：{self.foreground_title or '未知'}"
        )

    def capture_target_window(self):
        self.match_result.setStyleSheet("")
        self.match_result.setText("请在 5 秒内切换到目标窗口……")
        self._capture_countdown_generation += 1
        generation = self._capture_countdown_generation
        self.hide()

        def countdown_tick(remaining):
            if generation != self._capture_countdown_generation:
                return
            if remaining > 0:
                callback = self.status_overlay_callback
                if callable(callback):
                    callback(
                        "正在捕获目标窗口",
                        f"请切换到目标程序，{remaining} 秒后读取前台窗口",
                        "#fbbf24",
                    )
                QTimer.singleShot(1000, lambda: countdown_tick(remaining - 1))
                return
            self.finish_target_capture(generation)

        countdown_tick(5)

    def finish_target_capture(self, generation=None):
        if generation is not None and generation != self._capture_countdown_generation:
            return
        hide_callback = self.status_overlay_hide_callback
        if callable(hide_callback):
            hide_callback()
        process_name, title = foreground_window_context()
        process_name = str(process_name or "")
        title = str(title or "")
        invalid_reason = ""
        if foreground_window_belongs_to_current_process():
            invalid_reason = "捕获到 MacroCanvas 自身窗口，请切换到目标程序后重新捕获。"
        elif not process_name and not title:
            invalid_reason = "未读取到有效前台窗口，请重新捕获。"
        elif not process_name:
            invalid_reason = "未读取到目标程序进程名，请重新捕获。"
        if invalid_reason:
            self.match_result.setStyleSheet("color: #ff8496;")
            self.match_result.setText(invalid_reason)
            self.show()
            self.raise_()
            self.activateWindow()
            return
        self.foreground_process = process_name
        self.foreground_title = title
        self.refresh_current_context_text()
        self.match_result.setStyleSheet("")
        self.match_result.setText("目标窗口已捕获；可以写入条件或测试匹配结果。")
        self.show()
        self.raise_()
        self.activateWindow()

    def use_current_process(self):
        if self.foreground_process:
            self.process_edit.setText(self.foreground_process)

    def use_current_title(self):
        if not self.foreground_title:
            return
        fragment, accepted = QInputDialog.getText(
            self,
            "选择稳定的标题片段",
            "请保留能代表目标窗口且不会经常变化的部分。\n"
            "建议删除文档名、页面标题、进度或其他动态内容：",
            QLineEdit.EchoMode.Normal,
            self.foreground_title,
        )
        fragment = str(fragment or "").strip()
        if accepted and fragment:
            self.title_edit.setText(fragment)
            self.match_result.setStyleSheet("")
            self.match_result.setText(
                "已写入标题片段；如果目标标题会变化，请再缩短为稳定关键词。"
            )

    def test_current_window(self):
        self._save_editor()
        profile = self.selected_profile()
        if not profile:
            return
        matched = profile_matches(
            profile, self.foreground_process, self.foreground_title
        )
        self.match_result.setText(
            "已捕获窗口：匹配成功" if matched else "已捕获窗口：不匹配"
        )
        self.match_result.setStyleSheet(
            "color: #2dd4bf;" if matched else "color: #ff8496;"
        )

    def activate_base(self):
        self._save_editor()
        self.requested_activate_id = ""
        self.accept()

    def accept(self):
        self._save_editor()
        names = set()
        for index, profile in enumerate(self.profiles, 1):
            name = str(profile.get("name") or "").strip()
            if not name:
                QMessageBox.warning(
                    self, "档案名称", f"第 {index} 个档案名称不能为空。"
                )
                return
            lowered = name.lower()
            if lowered in names:
                QMessageBox.warning(self, "档案名称", f"档案名称重复：{name}")
                return
            names.add(lowered)
            if profile.get("enabled", False) and not self._profile_has_match_condition(profile):
                self.refresh_tree(index - 1)
                QMessageBox.warning(
                    self,
                    "档案匹配条件",
                    f"第 {index} 个档案已启用，但未填写进程名或标题包含。"
                    "请先设置至少一个匹配条件，或将该档案停用。",
                )
                return
        if self.save_callback is not None:
            self.loading_overlay.start_loading(
                "正在暂存档案修改",
                "正在校验档案列表并更新当前编辑内容……",
            )
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 12
            )
            try:
                saved = self.save_callback(
                    [normalize_profile(item) for item in self.profiles],
                    self.auto_switch.isChecked(),
                )
            finally:
                self.loading_overlay.stop_loading()
            if not saved:
                return
        super().accept()
