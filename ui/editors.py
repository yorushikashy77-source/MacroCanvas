import weakref

from PySide6.QtCore import QEvent, QItemSelectionModel, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTreeWidget,
    QVBoxLayout, QWidget,
)

from core.constants import *
from engine.win_input import WinInput
from engine.trigger_resolver import combo_text

# HotkeyEdit 需要固定顺序识别组合修饰键。
# 该常量不依赖 core.constants，避免启动时发生 ImportError。
MODIFIER_ORDER = ["Ctrl", "Shift", "Alt"]


class HotkeyEdit(QWidget):
    """Capture-first shortcut editor with a unified manual settings dialog.

    Mapping source editors may additionally own one physical-input condition.
    The condition is edited in the same dialog as the shortcut so the mapping
    card stays compact.  Other users of this widget (mapping targets, preset
    triggers, preset actions and global hotkeys) keep the same value API.

    Only one editor may own the global input hook at a time. Invalid input ends
    capture with inline feedback instead of opening a modal dialog; otherwise a
    click used to dismiss that dialog is captured again and can create an
    unbounded chain of warning windows.
    """

    changed = Signal()
    global_input = Signal(str, bool)
    _active_capture_ref = None
    _pending_capture_hooks = []
    _pending_capture_retry_scheduled = False

    def __init__(
        self, modifiers="无", key="F6", options=None, parent=None,
        allow_modifiers=True, reserved_keys=None, allow_condition=False,
        condition_enabled=False, condition_key="鼠标左键",
        condition_state="按住时", condition_options=None,
    ):
        super().__init__(parent)
        self.setObjectName("hotkeyEditor")
        self.allow_modifiers = bool(allow_modifiers)
        self.allow_condition = bool(allow_condition)
        self.reserved_keys = set(() if reserved_keys is None else reserved_keys)
        self.modifiers = (modifiers or "无") if self.allow_modifiers else "无"
        self.key = key
        self.options = list(options or INPUT_NAMES)
        self.condition_options = list(
            condition_options or CONDITION_INPUT_NAMES
        )
        self.condition_enabled = bool(condition_enabled) if self.allow_condition else False
        self.condition_key = self._normalized_condition_key(condition_key)
        self.condition_state = self._normalized_condition_state(condition_state)
        self.capturing = False
        self.capture_hook = None
        self.capture_modifiers = set()
        self.captured_main = False
        self.feedback_text = ""
        self.global_input.connect(self.handle_global_input)

        self.feedback_timer = QTimer(self)
        self.feedback_timer.setSingleShot(True)
        self.feedback_timer.timeout.connect(self._clear_feedback)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.capture_button = QPushButton()
        self.capture_button.setObjectName("hotkeyCaptureButton")
        self.capture_button.clicked.connect(self.start_capture)
        self.manual_button = QPushButton("设置")
        self.manual_button.setFixedWidth(52)
        self.manual_button.setObjectName("hotkeyManualButton")
        self.manual_button.clicked.connect(self.open_manual_editor)
        layout.addWidget(self.capture_button, 1)
        layout.addWidget(self.manual_button)
        self.refresh_text()

    def _normalized_condition_key(self, key):
        candidate = str(key or "")
        if candidate not in self.condition_options:
            candidate = self.condition_options[0] if self.condition_options else ""
        return candidate

    @staticmethod
    def _normalized_condition_state(state):
        candidate = str(state or "按住时")
        if candidate not in MAPPING_CONDITION_STATES:
            candidate = MAPPING_CONDITION_STATES[0]
        return candidate

    def value(self):
        return self.modifiers, self.key

    def condition_value(self):
        """Return condition fields in their configuration-storage order."""
        return self.condition_enabled, self.condition_key, self.condition_state

    def currentText(self):
        return self.key

    def setCurrentText(self, key):
        self.set_value("无" if not self.allow_modifiers else self.modifiers, key)

    def set_options(self, options, preferred=None, emit=False):
        self.options = list(options or [])
        candidate = preferred if preferred in self.options else self.key
        if candidate not in self.options:
            candidate = self.options[0] if self.options else ""
        self.set_value(
            self.modifiers if self.allow_modifiers else "无",
            candidate,
            emit=emit,
        )

    def set_value(self, modifiers, key, emit=True):
        self.set_configuration(modifiers, key, emit=emit)

    def set_condition(self, enabled, key=None, state=None, emit=True):
        self.set_configuration(
            self.modifiers,
            self.key,
            condition_enabled=enabled,
            condition_key=self.condition_key if key is None else key,
            condition_state=self.condition_state if state is None else state,
            emit=emit,
        )

    def set_configuration(
        self, modifiers, key, *, condition_enabled=None,
        condition_key=None, condition_state=None, emit=True,
    ):
        """Update shortcut and optional condition as one atomic UI change."""
        self.modifiers = (modifiers or "无") if self.allow_modifiers else "无"
        self.key = key
        if self.allow_condition:
            if condition_enabled is not None:
                self.condition_enabled = bool(condition_enabled)
            if condition_key is not None:
                self.condition_key = self._normalized_condition_key(condition_key)
            if condition_state is not None:
                self.condition_state = self._normalized_condition_state(
                    condition_state
                )
        else:
            self.condition_enabled = False
        self.feedback_text = ""
        self.feedback_timer.stop()
        self.refresh_text()
        if emit:
            self.changed.emit()

    def condition_summary(self):
        if not self.allow_condition or not self.condition_enabled:
            return "无附加触发条件"
        return f"{self.condition_key} {self.condition_state}"

    @staticmethod
    def _refresh_button_style(button):
        style = button.style()
        style.unpolish(button)
        style.polish(button)
        button.update()

    def refresh_text(self):
        main_text = combo_text(self.modifiers, self.key).replace("+", " + ")
        if self.feedback_text and not self.capturing:
            text = self.feedback_text
        elif self.capturing:
            text = "请按快捷键…（Esc 取消）"
        else:
            text = main_text
        self.capture_button.setText(text)

        condition_active = bool(
            self.allow_condition and self.condition_enabled
        )
        self.capture_button.setProperty("conditionActive", condition_active)
        self.manual_button.setProperty("conditionActive", condition_active)
        self.manual_button.setText("条件" if condition_active else "设置")

        capture_tip = f"当前快捷键：{main_text}\n点击后直接按下要使用的键位"
        if self.allow_condition:
            capture_tip += f"\n触发条件：{self.condition_summary()}"
        self.capture_button.setToolTip(capture_tip)
        self.manual_button.setToolTip(
            "手动选择快捷键并设置附加触发条件"
            if self.allow_condition else "手动选择快捷键"
        )
        self._refresh_button_style(self.capture_button)
        self._refresh_button_style(self.manual_button)

    def _clear_feedback(self):
        self.feedback_text = ""
        self.refresh_text()

    def _finish_capture_with_feedback(self, text):
        self.stop_capture()
        self.feedback_text = str(text)
        self.refresh_text()
        self.feedback_timer.start(1800)

    @classmethod
    def _active_capture_widget(cls):
        ref = cls._active_capture_ref
        if ref is None:
            return None
        try:
            return ref()
        except ReferenceError:
            cls._active_capture_ref = None
            return None

    @classmethod
    def _schedule_pending_capture_retry(cls):
        if cls._pending_capture_retry_scheduled or not cls._pending_capture_hooks:
            return
        cls._pending_capture_retry_scheduled = True
        QTimer.singleShot(200, cls._retry_pending_capture_hooks)

    @classmethod
    def _retry_pending_capture_hooks(cls):
        cls._pending_capture_retry_scheduled = False
        remaining = []
        for hook in list(cls._pending_capture_hooks):
            try:
                stopped = bool(hook.stop(timeout=0.2))
            except Exception:
                stopped = False
            if not stopped:
                remaining.append(hook)
        cls._pending_capture_hooks = remaining
        cls._schedule_pending_capture_retry()
        return not remaining

    @classmethod
    def _retain_pending_capture_hook(cls, hook):
        if hook is not None and all(item is not hook for item in cls._pending_capture_hooks):
            cls._pending_capture_hooks.append(hook)
        cls._schedule_pending_capture_retry()

    def _capture_input_callback(self, name, down):
        # stop_capture() clears this flag before waiting for the Windows hook
        # thread. Even if that thread needs another retry to exit, the stale
        # hook immediately stops suppressing system input.
        if not self.capturing:
            return False
        self.global_input.emit(name, down)
        return True

    def start_capture(self):
        if self.capturing:
            return

        if not type(self)._retry_pending_capture_hooks():
            self.feedback_text = "旧录入钩子仍在退出，请稍后重试"
            self.refresh_text()
            self.feedback_timer.start(1800)
            return

        # Rebuilt action rows and multiple editors must never leave more than one
        # WH_KEYBOARD_LL/WH_MOUSE_LL pair alive. Stop the previous owner first.
        active = self._active_capture_widget()
        if active is not None and active is not self:
            if not active.stop_capture():
                self.feedback_text = "旧录入钩子仍在退出，请稍后重试"
                self.refresh_text()
                self.feedback_timer.start(1800)
                return

        self.feedback_timer.stop()
        self.feedback_text = ""
        self.capturing = True
        self.capture_modifiers.clear()
        self.captured_main = False
        self.refresh_text()
        HotkeyEdit._active_capture_ref = weakref.ref(self)
        self.capture_hook = WinInput(self._capture_input_callback)
        if not self.capture_hook.start():
            hook = self.capture_hook
            self.capture_hook = None
            self.capturing = False
            if self._active_capture_widget() is self:
                HotkeyEdit._active_capture_ref = None
            try:
                stopped = bool(hook.stop(timeout=0.2))
            except Exception:
                stopped = False
            if not stopped:
                type(self)._retain_pending_capture_hook(hook)
            self._finish_capture_with_feedback("录入启动失败")

    def stop_capture(self):
        hook = self.capture_hook
        # Disable suppression before waiting for the hook thread. The callback
        # checks this flag and passes through all later events.
        self.capturing = False
        self.capture_modifiers.clear()
        self.captured_main = False
        if self._active_capture_widget() is self:
            HotkeyEdit._active_capture_ref = None
        stopped = True
        if hook:
            try:
                stopped = bool(hook.stop())
            except Exception:
                stopped = False
            if not stopped:
                type(self)._retain_pending_capture_hook(hook)
        self.capture_hook = None
        if not stopped and not self.feedback_text:
            self.feedback_text = "录入钩子仍在退出，已停止拦截输入"
            self.feedback_timer.start(1800)
        self.refresh_text()
        return stopped

    def event(self, event):
        # Rows are frequently rebuilt after drag/drop. Stop the hook before this
        # editor is hidden or deleted so a detached widget cannot keep capturing.
        if event.type() in (QEvent.Hide, QEvent.Close, QEvent.DeferredDelete):
            self.stop_capture()
        return super().event(event)

    @Slot(str, bool)
    def handle_global_input(self, key, down):
        if not self.capturing:
            return
        if key == "Esc" and down:
            self.stop_capture()
            return

        if not self.allow_modifiers:
            if not down:
                return
            if key.startswith("VK-") or key not in self.options:
                self._finish_capture_with_feedback(f"不支持：{key}")
                return
            if key in self.reserved_keys:
                self._finish_capture_with_feedback(f"{key} 为保留键")
                return
            self.stop_capture()
            self.set_value("无", key)
            return

        if key in MODIFIER_ORDER:
            if down:
                self.capture_modifiers.add(key)
            else:
                if not self.captured_main and key in self.options:
                    self.stop_capture()
                    self.set_value("无", key)
                    return
                self.capture_modifiers.discard(key)
            return
        if not down:
            return
        if key.startswith("VK-") or key not in self.options:
            self._finish_capture_with_feedback(f"不支持：{key}")
            return
        if key in self.reserved_keys:
            self._finish_capture_with_feedback(f"{key} 为保留键")
            return
        self.captured_main = True
        modifiers = "+".join(
            name for name in MODIFIER_ORDER if name in self.capture_modifiers
        ) or "无"
        self.stop_capture()
        self.set_value(modifiers, key)

    def open_manual_editor(self):
        # Opening the manual selector while another editor is listening must not
        # leave a global hook active behind the modal dialog.
        active = self._active_capture_widget()
        if active is not None:
            active.stop_capture()

        dialog = QDialog(self)
        dialog.setObjectName("hotkeyDialog")
        dialog.setWindowTitle("手动选择快捷键")
        dialog.setModal(True)
        dialog.setMinimumWidth(420 if self.allow_condition else 360)
        root = QVBoxLayout(dialog)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(13)

        title = QLabel(
            "快捷键与触发条件" if self.allow_condition else "快捷键设置"
        )
        title.setObjectName("hotkeyDialogTitle")
        root.addWidget(title)
        subtitle = QLabel(
            "选择主快捷键；附加条件仅限制该来源快捷键是否触发映射。"
            if self.allow_condition
            else "手动选择要使用的键位。"
        )
        subtitle.setObjectName("hotkeyDialogHint")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        shortcut_section = QFrame()
        shortcut_section.setObjectName("hotkeyDialogSection")
        shortcut_layout = QVBoxLayout(shortcut_section)
        shortcut_layout.setContentsMargins(14, 12, 14, 13)
        shortcut_layout.setSpacing(8)
        shortcut_title = QLabel("主快捷键")
        shortcut_title.setObjectName("hotkeyDialogSectionTitle")
        shortcut_layout.addWidget(shortcut_title)
        shortcut_form = QFormLayout()
        shortcut_form.setContentsMargins(0, 0, 0, 0)
        shortcut_form.setHorizontalSpacing(14)
        shortcut_form.setVerticalSpacing(6)
        modifiers = None
        if self.allow_modifiers:
            modifiers = QComboBox()
            modifiers.addItems(MODIFIER_OPTIONS)
            modifiers.setCurrentText(self.modifiers)
            shortcut_form.addRow("修饰键", modifiers)
        key = QComboBox()
        key.addItems(self.options)
        key.setCurrentText(self.key)
        shortcut_form.addRow("主键", key)
        shortcut_layout.addLayout(shortcut_form)
        root.addWidget(shortcut_section)

        condition_enabled = None
        condition_key = None
        condition_state = None
        if self.allow_condition:
            condition_section = QFrame()
            condition_section.setObjectName("hotkeyDialogSection")
            condition_layout = QVBoxLayout(condition_section)
            condition_layout.setContentsMargins(14, 12, 14, 13)
            condition_layout.setSpacing(8)

            condition_enabled = QCheckBox("启用附加触发条件")
            condition_enabled.setChecked(self.condition_enabled)
            condition_layout.addWidget(condition_enabled)

            condition_hint = QLabel(
                "只有条件输入处于指定状态时，主快捷键才会触发该映射。"
                "“松开时”表示该输入当前未被按住。"
            )
            condition_hint.setObjectName("hotkeyDialogHint")
            condition_hint.setWordWrap(True)
            condition_layout.addWidget(condition_hint)

            condition_fields = QWidget()
            condition_fields.setObjectName("hotkeyConditionFields")
            condition_form = QFormLayout(condition_fields)
            condition_form.setContentsMargins(0, 2, 0, 0)
            condition_form.setHorizontalSpacing(14)
            condition_form.setVerticalSpacing(6)
            condition_key = QComboBox()
            condition_key.addItems(self.condition_options)
            condition_key.setCurrentText(self.condition_key)
            condition_form.addRow("条件输入", condition_key)
            condition_state = QComboBox()
            condition_state.addItems(MAPPING_CONDITION_STATES)
            condition_state.setCurrentText(self.condition_state)
            condition_form.addRow("条件状态", condition_state)
            condition_layout.addWidget(condition_fields)
            condition_fields.setVisible(condition_enabled.isChecked())
            condition_enabled.toggled.connect(condition_fields.setVisible)
            root.addWidget(condition_section)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton(
            "取消", QDialogButtonBox.ButtonRole.RejectRole
        )
        cancel_button.setObjectName("hotkeyDialogCancel")
        save_button = buttons.addButton(
            "保存", QDialogButtonBox.ButtonRole.AcceptRole
        )
        save_button.setObjectName("hotkeyDialogSave")
        save_button.setDefault(True)
        self._refresh_button_style(cancel_button)
        self._refresh_button_style(save_button)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        root.addWidget(buttons)

        if dialog.exec() == QDialog.Accepted:
            self.set_configuration(
                modifiers.currentText() if modifiers is not None else "无",
                key.currentText(),
                condition_enabled=(
                    condition_enabled.isChecked()
                    if condition_enabled is not None else None
                ),
                condition_key=(
                    condition_key.currentText()
                    if condition_key is not None else None
                ),
                condition_state=(
                    condition_state.currentText()
                    if condition_state is not None else None
                ),
            )


class ActionTargetEditor(QWidget):
    """Target editor used by preset actions.

    Keyboard and mouse-button targets are capture-first. Manual selection is
    opened from the settings segment inside HotkeyEdit. Other action types retain
    the controls suited to their values.
    """

    changed = Signal()

    def __init__(
        self, action_type="键盘点击", target="A", parent=None,
        preset_options=None,
    ):
        super().__init__(parent)
        self.action_type = action_type
        self._updating = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.key_editor = HotkeyEdit(
            "无", target, KEY_NAMES, allow_modifiers=False
        )
        self.wheel_editor = QComboBox()
        self.wheel_editor.addItems(["向上", "向下"])
        # 鼠标移动的模式和坐标分别放在“目标”列与“参数”列。
        # 这里保留一个隐藏值用于统一生成 target 字符串，实际可编辑文本框
        # 由 ActionDurationEditor 放到右侧宽列中，避免坐标被压缩裁剪。
        self.move_editor = QLineEdit(self)
        self.move_editor.setPlaceholderText("x,y")
        self.move_editor.hide()
        self.move_mode = QComboBox()
        self.move_mode.addItems([
            "屏幕坐标", "相对移动", "屏幕比例", "前台窗口", "前台客户区"
        ])
        self.move_holder = QWidget()
        move_layout = QHBoxLayout(self.move_holder)
        move_layout.setContentsMargins(0, 0, 0, 0)
        move_layout.setSpacing(0)
        move_layout.addWidget(self.move_mode, 1)
        self.wait_label = QLabel("—")
        self.wait_label.setAlignment(Qt.AlignCenter)
        self.wait_label.setObjectName("muted")
        self.condition_editor = HotkeyEdit(
            "无",
            target if target in CONDITION_INPUT_NAMES else "鼠标左键",
            CONDITION_INPUT_NAMES,
            allow_modifiers=False,
        )
        self.submacro_editor = QComboBox()

        self.stack.addWidget(self.key_editor)
        self.stack.addWidget(self.wheel_editor)
        self.stack.addWidget(self.move_holder)
        self.stack.addWidget(self.wait_label)
        self.stack.addWidget(self.condition_editor)
        self.stack.addWidget(self.submacro_editor)
        layout.addWidget(self.stack)

        self.key_editor.changed.connect(self._emit_changed)
        self.wheel_editor.currentTextChanged.connect(self._emit_changed)
        self.move_mode.currentTextChanged.connect(self._emit_changed)
        self.condition_editor.changed.connect(self._emit_changed)
        self.submacro_editor.currentIndexChanged.connect(self._emit_changed)
        self.set_submacro_options(preset_options or [], target, emit=False)
        self.set_action_type(action_type, target, emit=False)

    def _emit_changed(self, *_args):
        if not self._updating:
            self.changed.emit()

    def currentText(self):
        if self.action_type in ("键盘点击", "鼠标点击"):
            return self.key_editor.currentText()
        if self.action_type == "鼠标滚轮":
            return self.wheel_editor.currentText()
        if self.action_type == "鼠标移动":
            value = self.move_editor.text().strip() or "0,0"
            prefix = {
                "屏幕坐标": "", "相对移动": "rel:",
                "屏幕比例": "pct:", "前台窗口": "window:",
                "前台客户区": "client:",
            }[self.move_mode.currentText()]
            return prefix + value
        if self.action_type in (CONDITION_ACTION_TYPE, WAIT_CONDITION_ACTION_TYPE):
            return self.condition_editor.currentText()
        if self.action_type == SUBMACRO_ACTION_TYPE:
            return str(self.submacro_editor.currentData() or "")
        return "仅等待"

    def set_submacro_options(self, options, preferred=None, emit=True):
        """Refresh reusable-preset choices while preserving the stable ID."""
        current = str(preferred or "")
        previous = self.submacro_editor.blockSignals(True)
        try:
            if not current and self.action_type == SUBMACRO_ACTION_TYPE:
                current = str(self.submacro_editor.currentData() or "")
            self.submacro_editor.clear()
            for preset_id, name in options or []:
                preset_id = str(preset_id or "")
                if preset_id:
                    self.submacro_editor.addItem(str(name or preset_id), preset_id)
            index = self.submacro_editor.findData(current)
            self.submacro_editor.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.submacro_editor.blockSignals(previous)
        if emit:
            self.changed.emit()

    def moveCoordinate(self):
        return self.move_editor.text().strip() or "0,0"

    def setMoveCoordinate(self, text, emit=False):
        value = str(text or "0,0").strip()
        if "," not in value:
            value = "0,0"
        changed = value != self.move_editor.text()
        self.move_editor.setText(value)
        if changed and emit and not self._updating:
            self.changed.emit()

    def moveModeText(self):
        return self.move_mode.currentText()

    def set_action_type(self, action_type, preferred=None, emit=True):
        old = preferred if preferred is not None else self.currentText()
        self._updating = True
        try:
            self.action_type = action_type
            if action_type == "键盘点击":
                value = old if old in KEY_NAMES else "A"
                self.key_editor.set_options(KEY_NAMES, value, emit=False)
                self.stack.setCurrentWidget(self.key_editor)
            elif action_type == "鼠标点击":
                value = old if old in MOUSE_NAMES else "鼠标左键"
                self.key_editor.set_options(MOUSE_NAMES, value, emit=False)
                self.stack.setCurrentWidget(self.key_editor)
            elif action_type == "鼠标滚轮":
                self.wheel_editor.setCurrentText(
                    old if old in ("向上", "向下") else "向上"
                )
                self.stack.setCurrentWidget(self.wheel_editor)
            elif action_type == "鼠标移动":
                text = str(old or "0,0")
                selected = "屏幕坐标"
                for prefix, label in {
                    "rel:": "相对移动", "pct:": "屏幕比例",
                    "window:": "前台窗口", "client:": "前台客户区",
                }.items():
                    if text.startswith(prefix):
                        selected = label
                        text = text[len(prefix):]
                        break
                # 从其他动作类型切换回来时，保留上一次坐标，而不是把
                # 无逗号的按键名称误当坐标并重置。
                if "," not in text:
                    text = self.moveCoordinate()
                self.move_mode.setCurrentText(selected)
                self.setMoveCoordinate(text, emit=False)
                self.stack.setCurrentWidget(self.move_holder)
            elif action_type in (CONDITION_ACTION_TYPE, WAIT_CONDITION_ACTION_TYPE):
                self.condition_editor.set_options(
                    CONDITION_INPUT_NAMES,
                    old if old in CONDITION_INPUT_NAMES else "鼠标左键",
                    emit=False,
                )
                self.stack.setCurrentWidget(self.condition_editor)
            elif action_type == SUBMACRO_ACTION_TYPE:
                index = self.submacro_editor.findData(str(old or ""))
                if index >= 0:
                    self.submacro_editor.setCurrentIndex(index)
                self.stack.setCurrentWidget(self.submacro_editor)
            else:
                self.stack.setCurrentWidget(self.wait_label)
        finally:
            self._updating = False
        if emit:
            self.changed.emit()


class ActionDurationEditor(QWidget):
    """Compact base duration/quantity editor with optional random jitter."""

    changed = Signal()

    def __init__(self, action_type="键盘点击", value=100, jitter_ms=0, parent=None):
        super().__init__(parent)
        self.action_type = action_type
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self.base = QSpinBox()
        # Keep every duration/quantity editor exactly the same width.  A
        # minimum/maximum range still lets QSpinBox choose different size hints
        # for “10 格”, “200 ms” and “1000 ms”, which makes rows look misaligned.
        self.base.setFixedWidth(180)
        self.random_label = QLabel("随机 ±")
        self.random_label.setObjectName("muted")
        self.random_label.setAlignment(Qt.AlignCenter)
        self.random_label.setFixedWidth(58)
        self.random = QSpinBox()
        self.random.setRange(0, 600_000)
        self.random.setSuffix(" ms")
        self.random.setSpecialValueText("固定")
        self.random.setFixedWidth(180)
        self.random.setToolTip(
            "每次执行时，在基础时长上随机加减该数值；结果不会低于该类型允许的最小值"
        )
        self.move_editor = QLineEdit()
        self.move_editor.setPlaceholderText("x,y")
        # 坐标输入框只需容纳完整坐标文本，不应占满整个参数列。
        # 固定宽度同时避免高 DPI 下再次退化成过窄输入框。
        self.move_editor.setFixedWidth(220)
        self.move_editor.setToolTip("输入鼠标坐标，例如 1920,1080")
        self.condition_state = QComboBox()
        self.condition_state.addItems(MAPPING_CONDITION_STATES)
        self.call_speed = QSpinBox()
        self.call_speed.setRange(10, 500)
        self.call_speed.setSuffix(" % 速度")
        self.call_speed.setValue(100)
        self.call_speed.setFixedWidth(180)

        layout.addWidget(self.base)
        layout.addWidget(self.random_label)
        layout.addWidget(self.random)
        layout.addWidget(self.move_editor)
        layout.addWidget(self.condition_state)
        layout.addWidget(self.call_speed)
        layout.addStretch(1)

        # QSpinBox.valueChanged emits the new integer value, while this
        # editor's public changed signal intentionally carries no arguments.
        # Discard the Qt payload instead of forwarding it to Signal().emit.
        self.base.valueChanged.connect(lambda _value: self.changed.emit())
        self.random.valueChanged.connect(lambda _value: self.changed.emit())
        self.move_editor.textChanged.connect(lambda _text: self.changed.emit())
        self.condition_state.currentTextChanged.connect(lambda _text: self.changed.emit())
        self.call_speed.valueChanged.connect(lambda _value: self.changed.emit())
        self.random.setValue(max(0, int(jitter_ms)))
        self.set_action_type(action_type, int(value), emit=False)

    def value(self):
        return self.base.value()

    def jitterValue(self):
        return (
            self.random.value()
            if self.action_type in ("键盘点击", "鼠标点击", "等待")
            else 0
        )

    def moveText(self):
        return self.move_editor.text().strip() or "0,0"

    def conditionState(self):
        return self.condition_state.currentText()

    def callSpeedValue(self):
        return self.call_speed.value()

    def setConditionState(self, state, emit=False):
        previous = self.condition_state.blockSignals(not emit)
        try:
            self.condition_state.setCurrentText(
                state if state in MAPPING_CONDITION_STATES else "按住时"
            )
        finally:
            self.condition_state.blockSignals(previous)

    def setCallSpeedValue(self, value, emit=False):
        previous = self.call_speed.blockSignals(not emit)
        try:
            self.call_speed.setValue(max(10, min(500, int(value))))
        finally:
            self.call_speed.blockSignals(previous)

    def setMoveText(self, text, emit=True):
        value = str(text or "0,0").strip()
        if "," not in value:
            value = "0,0"
        previous = self.move_editor.blockSignals(not emit)
        try:
            self.move_editor.setText(value)
        finally:
            self.move_editor.blockSignals(previous)

    def setMoveMode(self, mode):
        hints = {
            "屏幕坐标": ("x,y", "虚拟桌面的绝对像素坐标，例如 1920,1080"),
            "相对移动": ("dx,dy", "相对当前位置移动的像素差，例如 -20,15；仅游戏模式"),
            "屏幕比例": ("x%,y%", "虚拟桌面百分比坐标，例如 50,30"),
            "前台窗口": ("x,y", "相对于前台窗口左上角的像素坐标；仅游戏模式"),
            "前台客户区": ("x,y", "相对于前台窗口客户区原点的像素坐标；仅游戏模式"),
        }
        placeholder, tooltip = hints.get(mode, ("x,y", "输入鼠标坐标"))
        self.move_editor.setPlaceholderText(placeholder)
        self.move_editor.setToolTip(tooltip)

    def set_action_type(self, action_type, preferred=None, emit=True):
        old = self.base.value() if preferred is None else int(preferred)
        self.action_type = action_type
        timed = action_type in ("键盘点击", "鼠标点击", "等待")
        self.random_label.setVisible(timed)
        self.random.setVisible(timed)
        self.base.setVisible(action_type not in ("鼠标移动", CONDITION_ACTION_TYPE))
        self.move_editor.setVisible(action_type == "鼠标移动")
        self.condition_state.setVisible(
            action_type in (CONDITION_ACTION_TYPE, WAIT_CONDITION_ACTION_TYPE)
        )
        self.call_speed.setVisible(action_type == SUBMACRO_ACTION_TYPE)

        if action_type == WAIT_CONDITION_ACTION_TYPE:
            self.base.setToolTip("超时时间；0 表示一直等待")
            self.base.setRange(0, 600_000)
            self.base.setSuffix(" ms 超时")
            self.base.setSpecialValueText("一直等待")
        elif action_type == SUBMACRO_ACTION_TYPE:
            self.base.setToolTip("子宏重复次数")
            self.base.setRange(1, 100_000)
            self.base.setSuffix(" 次")
        elif action_type == "等待":
            self.base.setToolTip("等待持续时间")
            self.base.setRange(1, 600_000)
            self.base.setSuffix(" ms")
        elif action_type == "鼠标滚轮":
            self.base.setToolTip("滚轮格数")
            self.base.setRange(1, 100)
            self.base.setSuffix(" 格")
        elif action_type == "鼠标移动":
            self.base.setToolTip("")
        else:
            self.base.setToolTip("按键或鼠标保持时间")
            self.base.setRange(1, 600_000)
            self.base.setSuffix(" ms")
        if action_type != "鼠标移动":
            self.base.setValue(max(self.base.minimum(), min(self.base.maximum(), old)))
        if emit:
            self.changed.emit()


class ActionTreeOverlay(QWidget):
    """Paint action-row outlines and drag markers above embedded editors."""

    def __init__(self, tree):
        super().__init__(tree.viewport())
        self._tree_ref = weakref.ref(tree)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.show()

    def paintEvent(self, _event):
        tree = self._tree_ref()
        if tree is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Selection is represented by one clean outline rather than a partially
        # painted row background.  Embedded combo/spin/button widgets therefore
        # no longer leave rectangular holes in the selection feedback.
        selection_pen = QPen(QColor("#8B75FF"))
        selection_pen.setWidth(2)
        selection_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(selection_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for item in tree.selectedItems():
            rect = tree.visualItemRect(item)
            if not rect.isValid() or rect.height() <= 0:
                continue
            row_rect = rect.adjusted(2, 2, -3, -2)
            row_rect.setLeft(2)
            row_rect.setRight(max(3, self.width() - 3))
            painter.drawRoundedRect(row_rect, 5, 5)

        # Loop range points are explicit boundaries rather than a filled row.
        # The start marker sits above the first included action; the end marker
        # sits below the last included action.
        marker_specs = (
            (tree._loop_start_item, True, QColor("#72FF6A"), "循环开始"),
            (tree._loop_end_item, False, QColor("#FF62D0"), "循环结束"),
        )
        for item, is_start, color, label in marker_specs:
            if item is None:
                continue
            rect = tree.visualItemRect(item)
            if not rect.isValid() or rect.height() <= 0:
                continue
            y = rect.top() if is_start else rect.bottom() + 1
            y = max(4, min(self.height() - 5, y))
            depth = tree._item_depth(item)
            x1 = max(8, depth * tree.indentation() + 10)
            x2 = max(x1 + 80, self.width() - 8)
            shadow = QPen(QColor(0, 0, 0, 230))
            shadow.setWidth(7)
            painter.setPen(shadow)
            painter.drawLine(x1, y, x2, y)
            pen = QPen(color)
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawLine(x1, y, x2, y)
            painter.setPen(color)
            text_y = y - 5 if is_start else y + 16
            painter.drawText(x1 + 6, text_y, label)

        if tree._drag_active:
            on_item = QAbstractItemView.DropIndicatorPosition.OnItem
            target = tree._drop_target
            # Dropping directly on a row means “make it a child”.  Outline the
            # complete target row above every cell widget; do not darken the row.
            if tree._drop_position == on_item and target is not None:
                rect = tree.visualItemRect(target)
                if rect.isValid() and rect.height() > 0:
                    target_rect = rect.adjusted(2, 2, -3, -2)
                    target_rect.setLeft(2)
                    target_rect.setRight(max(3, self.width() - 3))
                    target_pen = QPen(QColor("#00F5FF"))
                    target_pen.setWidth(3)
                    target_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    painter.setPen(target_pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRoundedRect(target_rect, 5, 5)

            geometry = tree._indicator_line()
            if geometry is not None:
                x1, x2, y, nested = geometry
                # A dark underlay and cyan line remain visible over every editor
                # because this overlay is raised above all setItemWidget children.
                shadow_pen = QPen(QColor(0, 0, 0, 220))
                shadow_pen.setWidth(7)
                shadow_pen.setStyle(Qt.PenStyle.SolidLine)
                shadow_pen.setCapStyle(Qt.PenCapStyle.SquareCap)
                painter.setPen(shadow_pen)
                painter.drawLine(x1, y, x2, y)
                marker_pen = QPen(QColor("#00F5FF"))
                marker_pen.setWidth(3)
                marker_pen.setStyle(Qt.PenStyle.SolidLine)
                marker_pen.setCapStyle(Qt.PenCapStyle.SquareCap)
                painter.setPen(marker_pen)
                painter.drawLine(x1, y, x2, y)
                if nested:
                    painter.drawLine(x1, max(2, y - 9), x1, y)
        painter.end()


class ActionTreeWidget(QTreeWidget):
    """Arbitrary-depth action tree with model-backed drag/drop and auto-scroll."""

    drop_requested = Signal(object, object, object)
    loop_point_clicked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 不再使用 QTreeWidget 原生拖拽。原生拖拽会进入 Qt/系统的 drag
        # 事件循环，按住卡片后滚轮事件可能不再按普通 wheel 路径送达，
        # dragMoveEvent 也会和定时器一起推动滚动，导致边缘滚动过快。
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.viewport().setAcceptDrops(False)
        # Qt's native indicator is subtle and can disagree with the model-backed
        # drop that is performed after the event unwinds. Draw one explicit,
        # high-contrast insertion line from the same target data used by dropEvent.
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setRootIsDecorated(True)
        self.setIndentation(24)
        self.setAnimated(False)
        self.setAutoScroll(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerItem)

        self._drag_capture_active = False
        self._manual_dragging = False
        self._drag_active = False
        self._last_drag_pos = None
        self._drag_start_pos = None
        self._drag_source_item = None
        self._drop_target = None
        self._drop_position = QAbstractItemView.DropIndicatorPosition.OnViewport
        self._loop_point_mode = False
        self._loop_start_item = None
        self._loop_end_item = None
        self._drag_scroll_timer = QTimer(self)
        # 边缘自动滚动按动作卡片计步：固定约一秒两格，不随 mouseMove
        # 频率叠加，也不使用距离/时间加速。
        self._drag_scroll_interval_s = 0.5
        self._drag_scroll_margin = 82
        self._drag_scroll_last_step_at = 0.0
        self._drag_scroll_timer.setInterval(500)
        self._drag_scroll_timer.timeout.connect(self._auto_scroll_once)
        self._drag_global_wheel_filter_installed = False
        self._drag_wheel_angle_remainder = 0.0
        self._drag_wheel_pixel_remainder = 0.0
        self._overlay = ActionTreeOverlay(self)
        self._item_widget_filter_targets = weakref.WeakSet()
        self._item_widget_owners = weakref.WeakKeyDictionary()
        self.viewport().installEventFilter(self)
        # setItemWidget 的永久编辑控件在部分 Windows/Qt 组合下会把单纯的
        # 鼠标悬停误判成“切换当前行”。保存一次完整悬停会话的选择快照，
        # 不允许后续子控件 Enter/MouseMove 以已经被污染的选择重新覆盖快照。
        self._item_widget_hover_snapshot = None
        self._item_widget_hover_generation = 0
        self._item_widget_restore_pending = False
        self._restoring_item_widget_selection = False
        self.itemSelectionChanged.connect(self._on_item_selection_changed)
        self.currentItemChanged.connect(self._on_current_item_changed)
        QTimer.singleShot(0, self._refresh_overlay)

    @staticmethod
    def _is_passive_item_widget_event(event):
        return event.type() in (
            QEvent.Enter,
            QEvent.HoverEnter,
            QEvent.MouseMove,
            QEvent.HoverMove,
        )

    def eventFilter(self, watched, event):
        event_type = event.type()

        if (self._drag_capture_active or self._drag_active) and event_type == QEvent.Wheel:
            if self._handle_drag_wheel_event(event):
                return True
        if self._drag_active and event_type in (QEvent.DragMove, QEvent.DragEnter):
            self._update_drop_indicator_from_global(QCursor.pos())
            if hasattr(event, "setDropAction"):
                event.setDropAction(Qt.MoveAction)
            event.accept()
            return True
        if self._drag_active and event_type == QEvent.MouseMove:
            self._update_drag_from_global_event(event)
            return True
        if (
            (self._drag_capture_active or self._drag_active)
            and event_type == QEvent.MouseButtonRelease
            and hasattr(event, "button")
            and event.button() == Qt.MouseButton.LeftButton
        ):
            if self._manual_dragging:
                self._finish_manual_drag_from_global(event)
                return True
            self._clear_drag_state()

        if watched not in self._item_widget_filter_targets:
            return super().eventFilter(watched, event)

        # QSpinBox、组合框和复合编辑器内部还包含 lineEdit、箭头按钮等
        # 子控件。只过滤 setItemWidget 的顶层控件会在鼠标进入这些子控件时
        # 留下遗漏，因此对子控件及运行时新增的子控件一并安装过滤器。
        if event_type == QEvent.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget):
                self._install_item_widget_event_filters(
                    child, self._item_widget_owners.get(watched)
                )

        if event_type == QEvent.MouseButtonPress:
            # 行内编辑器属于其所在动作。QComboBox 弹出列表时，
            # Windows/Qt 有时会让 ExtendedSelection 保留甚至扩大旧选择。
            # 在控件处理按下前明确单选归属行，不影响层级列的
            # Ctrl/Shift 多选和拖拽。
            self._clear_item_widget_hover_guard()
            self._select_item_widget_owner(watched)
        elif (
            self._is_passive_item_widget_event(event)
            and QApplication.mouseButtons() == Qt.MouseButton.NoButton
        ):
            self._begin_item_widget_hover_guard()
            # 某些平台在子控件处理事件后才切换选择；保留一次队列尾检查。
            # 若选择信号已经触发，下面的同步回滚会更早完成，这里只作兜底。
            self._queue_item_widget_selection_restore()
        elif event_type in (QEvent.Leave, QEvent.HoverLeave):
            self._schedule_item_widget_hover_guard_clear()

        return super().eventFilter(watched, event)

    def _install_item_widget_event_filters(self, widget, owner=None):
        self._item_widget_filter_targets.add(widget)
        if owner is not None:
            self._item_widget_owners[widget] = owner
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            self._item_widget_filter_targets.add(child)
            if owner is not None:
                self._item_widget_owners[child] = owner
            child.installEventFilter(self)

    def _select_item_widget_owner(self, watched):
        owner = self._item_widget_owners.get(watched)
        if owner is None or not any(
            item is owner for item in self.iter_items()
        ):
            return
        if self.selectedItems() == [owner] and self.currentItem() is owner:
            return
        self.setCurrentItem(
            owner, 0,
            QItemSelectionModel.SelectionFlag.ClearAndSelect,
        )

    def _begin_item_widget_hover_guard(self):
        self._item_widget_hover_generation += 1
        if self._item_widget_hover_snapshot is None:
            self._item_widget_hover_snapshot = (
                tuple(self.selectedItems()),
                self.currentItem(),
            )

    def _queue_item_widget_selection_restore(self):
        if self._item_widget_restore_pending:
            return
        snapshot = self._item_widget_hover_snapshot
        if snapshot is None:
            return
        self._item_widget_restore_pending = True
        QTimer.singleShot(
            0,
            lambda saved=snapshot: self._run_queued_item_widget_restore(saved),
        )

    def _run_queued_item_widget_restore(self, snapshot):
        self._item_widget_restore_pending = False
        self._restore_item_widget_selection(snapshot)

    def _schedule_item_widget_hover_guard_clear(self):
        # 从组合框主体移动到箭头、从一行移动到下一行时，Qt会先发Leave再发
        # Enter。延迟到本轮事件结束后再清除，避免在控件之间移动时重新抓取到
        # 已经被误选后的单行状态。
        self._item_widget_hover_generation += 1
        generation = self._item_widget_hover_generation
        QTimer.singleShot(
            0,
            lambda token=generation: self._clear_item_widget_hover_guard(token),
        )

    def _clear_item_widget_hover_guard(self, generation=None):
        if (
            generation is not None
            and generation != self._item_widget_hover_generation
        ):
            return
        self._item_widget_hover_generation += 1
        self._item_widget_hover_snapshot = None

    def _on_item_selection_changed(self):
        if (
            self._item_widget_hover_snapshot is not None
            and not self._restoring_item_widget_selection
            and not self._drag_active
            and QApplication.mouseButtons() == Qt.MouseButton.NoButton
        ):
            # 在 itemSelectionChanged 信号内同步恢复，避免先绘制一帧错误的
            # 单选紫色边框，再由 QTimer 恢复造成闪烁或短暂残留。
            self._restore_item_widget_selection(
                self._item_widget_hover_snapshot
            )
            return
        self._refresh_overlay()

    def _on_current_item_changed(self, _current, _previous):
        if (
            self._item_widget_hover_snapshot is not None
            and not self._restoring_item_widget_selection
            and not self._drag_active
            and QApplication.mouseButtons() == Qt.MouseButton.NoButton
        ):
            self._restore_item_widget_selection(
                self._item_widget_hover_snapshot
            )
            return
        self._refresh_overlay()

    def _restore_item_widget_selection(self, snapshot):
        if (
            snapshot is None
            or snapshot is not self._item_widget_hover_snapshot
            or self._restoring_item_widget_selection
            or self._drag_active
            or QApplication.mouseButtons() != Qt.MouseButton.NoButton
        ):
            return

        selected, current = snapshot
        live_items = list(self.iter_items())
        live_ids = {id(item) for item in live_items}
        selected_ids = {id(item) for item in selected if id(item) in live_ids}
        current_is_live = current is not None and id(current) in live_ids

        selection_changed = {id(item) for item in self.selectedItems()} != selected_ids
        current_changed = current_is_live and self.currentItem() is not current
        if selection_changed or current_changed:
            self._restoring_item_widget_selection = True
            tree_blocked = self.blockSignals(True)
            selection_model = self.selectionModel()
            model_blocked = (
                selection_model.blockSignals(True)
                if selection_model is not None else None
            )
            try:
                for item in live_items:
                    item.setSelected(id(item) in selected_ids)
                if current_is_live:
                    self.setCurrentItem(
                        current, 0, QItemSelectionModel.SelectionFlag.NoUpdate
                    )
            finally:
                if selection_model is not None:
                    selection_model.blockSignals(model_blocked)
                self.blockSignals(tree_blocked)
                self._restoring_item_widget_selection = False

        self._refresh_overlay()

    def set_loop_point_mode(self, enabled, start_item=None, end_item=None):
        self._loop_point_mode = bool(enabled)
        self._loop_start_item = start_item
        self._loop_end_item = end_item
        # 动作卡片拖拽由本类手动处理，不能在退出循环点模式时重新打开
        # QTreeWidget 原生拖拽，否则滚轮和边缘滚动又会回到不可控路径。
        self.setDragEnabled(False)
        self.viewport().setCursor(
            Qt.CursorShape.CrossCursor
            if self._loop_point_mode else Qt.CursorShape.ArrowCursor
        )
        self._refresh_overlay()

    def clear_loop_points(self):
        self.set_loop_point_mode(False, None, None)

    def mousePressEvent(self, event):
        # 在动作树空白区或层级列主动点击时，后续选择变化应由Qt正常处理。
        self._clear_item_widget_hover_guard()
        if (
            self._loop_point_mode
            and event.button() == Qt.MouseButton.LeftButton
        ):
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                self.loop_point_clicked.emit(item)
                event.accept()
                return
        if (
            not self._loop_point_mode
            and event.button() == Qt.MouseButton.LeftButton
        ):
            item = self.itemAt(event.position().toPoint())
            column = self.columnAt(event.position().toPoint().x())
            if (
                item is not None
                and column == 0
                and item.flags() & Qt.ItemFlag.ItemIsDragEnabled
            ):
                self._begin_drag_capture(item, event.position().toPoint())
                super().mousePressEvent(event)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_capture_active and event.buttons() & Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if (
                not self._manual_dragging
                and self._drag_start_pos is not None
                and (pos - self._drag_start_pos).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                self._begin_manual_drag(pos)
            if self._manual_dragging:
                self._update_drop_indicator(pos)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (
            (self._drag_capture_active or self._drag_active)
            and event.button() == Qt.MouseButton.LeftButton
        ):
            if self._manual_dragging:
                self._finish_manual_drag(event.position().toPoint())
                event.accept()
                return
            self._clear_drag_state()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        # 键盘扩展选择属于主动操作，不能被鼠标停留位置留下的悬停快照回滚。
        self._clear_item_widget_hover_guard()
        super().keyPressEvent(event)

    def _refresh_overlay(self):
        overlay = getattr(self, "_overlay", None)
        if overlay is None:
            return
        overlay.setGeometry(self.viewport().rect())
        overlay.raise_()
        overlay.update()

    def setItemWidget(self, item, column, widget):
        super().setItemWidget(item, column, widget)
        # 监听顶层单元格控件及其全部子控件。复合编辑器内部的箭头按钮、
        # spinbox lineEdit 等也必须受到同一选择保护。
        self._install_item_widget_event_filters(widget, item)
        # setItemWidget creates viewport children after the overlay.  Raise the
        # overlay again so row outlines and insertion lines remain uninterrupted.
        QTimer.singleShot(0, self._refresh_overlay)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_overlay()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._refresh_overlay()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._refresh_overlay)

    def _enclosing_scroll_area(self):
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    @staticmethod
    def _scroll_bar(bar, delta):
        before = bar.value()
        bar.setValue(max(bar.minimum(), min(bar.maximum(), before + delta)))
        return bar.value() != before

    @staticmethod
    def _can_scroll(bar, direction):
        if direction < 0:
            return bar.value() > bar.minimum()
        return bar.value() < bar.maximum()

    def _set_drag_global_wheel_filter_enabled(self, enabled):
        app = QApplication.instance()
        if app is None:
            return
        enabled = bool(enabled)
        if enabled and not self._drag_global_wheel_filter_installed:
            app.installEventFilter(self)
            self._drag_global_wheel_filter_installed = True
        elif not enabled and self._drag_global_wheel_filter_installed:
            app.removeEventFilter(self)
            self._drag_global_wheel_filter_installed = False

    def _begin_drag_capture(self, item, start_pos):
        self._drag_capture_active = True
        self._manual_dragging = False
        self._drag_active = False
        self._drag_source_item = item
        self._drag_start_pos = start_pos
        self._last_drag_pos = start_pos
        self._drag_wheel_angle_remainder = 0.0
        self._drag_wheel_pixel_remainder = 0.0
        self._set_drag_global_wheel_filter_enabled(True)

    def _begin_manual_drag(self, local_pos):
        if self._drag_source_item is None:
            return
        self._manual_dragging = True
        self._drag_active = True
        self._update_drop_indicator(local_pos)
        self._drag_scroll_last_step_at = 0.0
        if not self._drag_scroll_timer.isActive():
            self._drag_scroll_timer.start()
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)

    def _update_drag_from_global_event(self, event):
        if not self._manual_dragging:
            return
        if hasattr(event, "globalPosition"):
            global_pos = event.globalPosition().toPoint()
        else:
            global_pos = QCursor.pos()
        self._update_drop_indicator_from_global(global_pos)

    def _finish_manual_drag_from_global(self, event):
        if hasattr(event, "globalPosition"):
            global_pos = event.globalPosition().toPoint()
        else:
            global_pos = QCursor.pos()
        self._update_drop_indicator_from_global(global_pos)
        self._finish_manual_drag(self.viewport().mapFromGlobal(global_pos))

    def _finish_manual_drag(self, local_pos):
        if self._manual_dragging:
            self._update_drop_indicator(local_pos)
        source = self._drag_source_item
        target = self._drop_target
        position = self._drop_position
        self._clear_drag_state()
        if source is None:
            return
        QTimer.singleShot(
            0,
            lambda s=source, t=target, p=position:
            self.drop_requested.emit(s, t, p),
        )

    @staticmethod
    def _item_depth(item):
        depth = 0
        parent = item.parent() if item is not None else None
        while parent is not None:
            depth += 1
            parent = parent.parent()
        return depth

    @staticmethod
    def _item_is_effectively_visible(item):
        if item is None or item.isHidden():
            return False
        parent = item.parent()
        while parent is not None:
            if parent.isHidden() or not parent.isExpanded():
                return False
            parent = parent.parent()
        return True

    def _last_visible_descendant(self, item):
        current = item
        while current is not None and current.isExpanded():
            next_item = None
            for index in range(current.childCount() - 1, -1, -1):
                child = current.child(index)
                if not child.isHidden():
                    next_item = child
                    break
            if next_item is None:
                break
            current = next_item
        return current

    def _visible_action_bounds_in_outer(self, outer):
        top = None
        bottom = None
        for item in self.iter_items():
            if not self._item_is_effectively_visible(item):
                continue
            rect = self.visualItemRect(item)
            if not rect.isValid() or rect.height() <= 0:
                continue
            global_top = self.viewport().mapToGlobal(rect.topLeft())
            global_bottom = self.viewport().mapToGlobal(rect.bottomRight())
            outer_top = outer.viewport().mapFromGlobal(global_top).y()
            outer_bottom = outer.viewport().mapFromGlobal(global_bottom).y()
            top = outer_top if top is None else min(top, outer_top)
            bottom = outer_bottom if bottom is None else max(bottom, outer_bottom)
        return top, bottom

    def _outer_has_action_content_beyond(self, outer, direction):
        top, bottom = self._visible_action_bounds_in_outer(outer)
        if top is None or bottom is None:
            return False
        if direction < 0:
            return top < -2
        return bottom > outer.viewport().height() + 2

    def _drag_scroll_direction_for_pos(self, global_pos):
        local = self.viewport().mapFromGlobal(global_pos)
        direction = 0
        if local.y() < self._drag_scroll_margin:
            direction = -1
        elif local.y() > self.viewport().height() - self._drag_scroll_margin:
            direction = 1

        if direction and self._can_scroll(self.verticalScrollBar(), direction):
            return direction

        outer = self._enclosing_scroll_area()
        if outer is None:
            return 0
        outer_local = outer.viewport().mapFromGlobal(global_pos)
        near_outer_edge = (
            outer_local.y() < self._drag_scroll_margin if direction < 0
            else outer_local.y() > outer.viewport().height() - self._drag_scroll_margin
        )
        if (
            direction
            and near_outer_edge
            and self._can_scroll(outer.verticalScrollBar(), direction)
            and self._outer_has_action_content_beyond(outer, direction)
        ):
            return direction
        return 0

    def _drag_scroll_interval(self):
        # 固定 0.5 秒一步。第一次进入边缘也等待完整间隔，避免刚碰到
        # 顶/底边缘就立刻跳一格。
        import time
        now = time.monotonic()
        if self._drag_scroll_last_step_at <= 0:
            self._drag_scroll_last_step_at = now
            return self._drag_scroll_interval_s
        return self._drag_scroll_interval_s

    def _ordered_effective_action_items(self):
        return [
            item for item in self.iter_items()
            if self._item_is_effectively_visible(item)
        ]

    def _drop_slot_from_target(self, items, target, position):
        if not items:
            return 0
        try:
            index = items.index(target)
        except ValueError:
            return 0 if self.verticalScrollBar().value() <= self.verticalScrollBar().minimum() else len(items)
        above_item = QAbstractItemView.DropIndicatorPosition.AboveItem
        return index if position == above_item else index + 1

    def _drop_target_from_slot(self, items, slot):
        above_item = QAbstractItemView.DropIndicatorPosition.AboveItem
        below_item = QAbstractItemView.DropIndicatorPosition.BelowItem
        if not items:
            return None, QAbstractItemView.DropIndicatorPosition.OnViewport
        if slot <= 0:
            return items[0], above_item
        if slot >= len(items):
            return items[-1], below_item
        return items[slot], above_item

    def _action_card_step_pixels(self, items=None):
        items = items if items is not None else self._ordered_effective_action_items()
        heights = []
        for item in items:
            rect = self.visualItemRect(item)
            if rect.isValid() and rect.height() > 0:
                heights.append(rect.height())
        if heights:
            return max(24, int(sum(heights) / len(heights)))
        return 44

    def _tree_action_scroll_step(self, items=None):
        if self.verticalScrollMode() == QAbstractItemView.ScrollPerItem:
            return 1
        return self._action_card_step_pixels(items)

    def _scroll_item_one_card_into_view(self, items, direction):
        tree_step = self._tree_action_scroll_step(items)
        outer_step = self._action_card_step_pixels(items)
        moved = False
        if self._can_scroll(self.verticalScrollBar(), direction):
            moved = self._scroll_bar(
                self.verticalScrollBar(), direction * tree_step
            )
        if not moved:
            outer = self._enclosing_scroll_area()
            if (
                outer is not None
                and self._can_scroll(outer.verticalScrollBar(), direction)
                and self._outer_has_action_content_beyond(outer, direction)
            ):
                moved = self._scroll_bar(
                    outer.verticalScrollBar(), direction * outer_step
                )
        return moved

    def _scroll_one_action_card(self, direction):
        # 以“动作卡片”为单位滚动：每次只推进一个可见动作槽位。
        items = self._ordered_effective_action_items()
        if not items or not direction:
            return False
        current_slot = self._drop_slot_from_target(
            items, self._drop_target, self._drop_position
        )
        next_slot = max(0, min(len(items), current_slot + direction))
        self._drop_target, self._drop_position = self._drop_target_from_slot(
            items, next_slot
        )
        moved = self._scroll_item_one_card_into_view(items, direction)
        self._refresh_overlay()
        return moved

    def _nearest_visible_drop_target(self, local_y):
        best_item = None
        best_rect = None
        best_distance = None
        viewport_height = self.viewport().height()
        for item in self.iter_items():
            if not self._item_is_effectively_visible(item):
                continue
            rect = self.visualItemRect(item)
            if not rect.isValid() or rect.height() <= 0:
                continue
            if rect.bottom() < 0 or rect.top() > viewport_height:
                continue
            center_y = rect.center().y()
            distance = abs(local_y - center_y)
            if best_distance is None or distance < best_distance:
                best_item = item
                best_rect = rect
                best_distance = distance
        if best_item is None or best_rect is None:
            return None, QAbstractItemView.DropIndicatorPosition.OnViewport
        position = (
            QAbstractItemView.DropIndicatorPosition.AboveItem
            if local_y <= best_rect.center().y()
            else QAbstractItemView.DropIndicatorPosition.BelowItem
        )
        return best_item, position

    def _calculate_drop_target(self, local_pos):
        target = self.itemAt(local_pos)
        on_item = QAbstractItemView.DropIndicatorPosition.OnItem
        above_item = QAbstractItemView.DropIndicatorPosition.AboveItem
        below_item = QAbstractItemView.DropIndicatorPosition.BelowItem
        on_viewport = QAbstractItemView.DropIndicatorPosition.OnViewport
        if target is None:
            # 鼠标拖出动作树可视范围时，不能把目标退化成整张视图。
            # 否则松手会直接跳到预设顶部/底部。使用当前可见边缘行作为
            # 插入锚点，配合自动滚动逐步移动。
            edge_target, edge_position = self._nearest_visible_drop_target(
                local_pos.y()
            )
            return edge_target, edge_position

        rect = self.visualItemRect(target)
        if not rect.isValid() or rect.height() <= 0:
            return target, on_item
        edge = max(7, min(14, rect.height() // 3))
        if local_pos.y() <= rect.top() + edge:
            return target, above_item
        if local_pos.y() >= rect.bottom() - edge:
            return target, below_item
        return target, on_item

    def _update_drop_indicator(self, local_pos):
        self._last_drag_pos = local_pos
        if self._manual_dragging:
            global_pos = self.viewport().mapToGlobal(local_pos)
            direction = self._drag_scroll_direction_for_pos(global_pos)
            if direction:
                # 进入边缘步进区后，不再按每个 mouseMove 重新计算目标。
                # 目标槽位只能由 _drag_scroll_timer 每 0.5 秒推进一格，
                # 避免鼠标贴边时插入线和页面滚动被高频移动事件甩飞。
                return
        target, position = self._calculate_drop_target(local_pos)
        changed = target is not self._drop_target or position != self._drop_position
        self._drop_target = target
        self._drop_position = position
        if changed:
            self._refresh_overlay()

    def _update_drop_indicator_from_global(self, global_pos):
        self._update_drop_indicator(self.viewport().mapFromGlobal(global_pos))

    def _indicator_line(self):
        if not self._drag_active:
            return None
        above_item = QAbstractItemView.DropIndicatorPosition.AboveItem
        below_item = QAbstractItemView.DropIndicatorPosition.BelowItem
        on_item = QAbstractItemView.DropIndicatorPosition.OnItem
        on_viewport = QAbstractItemView.DropIndicatorPosition.OnViewport
        target = self._drop_target
        position = self._drop_position

        if position == on_viewport or target is None:
            last = None
            for index in range(self.topLevelItemCount() - 1, -1, -1):
                candidate = self.topLevelItem(index)
                if not candidate.isHidden():
                    last = self._last_visible_descendant(candidate)
                    break
            if last is None:
                y = 5
            else:
                rect = self.visualItemRect(last)
                y = rect.bottom() + 1 if rect.isValid() else self.viewport().height() - 5
            depth = 0
        else:
            rect = self.visualItemRect(target)
            if not rect.isValid():
                return None
            depth = self._item_depth(target)
            if position == above_item:
                y = rect.top()
            elif position == below_item:
                last = self._last_visible_descendant(target)
                last_rect = self.visualItemRect(last)
                y = last_rect.bottom() + 1 if last_rect.isValid() else rect.bottom() + 1
            elif position == on_item:
                last = self._last_visible_descendant(target)
                last_rect = self.visualItemRect(last)
                y = last_rect.bottom() + 1 if last_rect.isValid() else rect.bottom() + 1
                depth += 1
            else:
                return None

        y = max(3, min(self.viewport().height() - 4, y))
        x1 = max(7, depth * self.indentation() + 9)
        x2 = max(x1 + 24, self.viewport().width() - 8)
        return x1, x2, y, position == on_item
    def paintEvent(self, event):
        super().paintEvent(event)
        self._refresh_overlay()

    def _scroll_normal_wheel_delta(self, delta):
        if not delta:
            return False
        direction = -1 if delta < 0 else 1
        moved = False
        if self._can_scroll(self.verticalScrollBar(), direction):
            moved = self._scroll_bar(self.verticalScrollBar(), int(delta))
        if not moved:
            outer = self._enclosing_scroll_area()
            if outer is not None and self._can_scroll(outer.verticalScrollBar(), direction):
                items = self._ordered_effective_action_items()
                outer_delta = direction * self._action_card_step_pixels(items) * abs(int(delta))
                moved = self._scroll_bar(outer.verticalScrollBar(), outer_delta)
        return moved

    def _scroll_drag_wheel_like_normal(self, event):
        moved = False
        pixel_delta = event.pixelDelta().y() if hasattr(event, "pixelDelta") else 0
        if pixel_delta:
            card_pixels = max(1, self._action_card_step_pixels())
            self._drag_wheel_pixel_remainder += -pixel_delta / card_pixels
            whole_pixel_rows = int(self._drag_wheel_pixel_remainder)
            if whole_pixel_rows:
                self._drag_wheel_pixel_remainder -= whole_pixel_rows
                moved = self._scroll_normal_wheel_delta(whole_pixel_rows) or moved

        angle_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else 0
        if angle_delta:
            lines = max(1, QApplication.wheelScrollLines())
            # 普通滚动：一格滚轮按系统设置的 wheelScrollLines 推进；
            # 零散高精度 delta 累积后再滚，避免丢格。
            self._drag_wheel_angle_remainder += -(angle_delta / 120.0) * lines
            whole_line_delta = int(self._drag_wheel_angle_remainder)
            if whole_line_delta:
                self._drag_wheel_angle_remainder -= whole_line_delta
                moved = self._scroll_normal_wheel_delta(whole_line_delta) or moved
        return moved

    def _handle_drag_wheel_event(self, event):
        moved = self._scroll_drag_wheel_like_normal(event)
        if moved:
            self._update_drop_indicator_from_global(QCursor.pos())
            self._refresh_overlay()
        # 拖拽期间滚轮只服务于动作树，不再把事件漏给外层空白区域。
        event.accept()
        return True

    def _auto_scroll_once(self):
        if not self._drag_active:
            return
        if not (QApplication.mouseButtons() & Qt.MouseButton.LeftButton):
            self._clear_drag_state()
            return

        global_pos = QCursor.pos()
        direction = self._drag_scroll_direction_for_pos(global_pos)
        if not direction:
            self._drag_scroll_last_step_at = 0.0
            return

        import time
        interval = self._drag_scroll_interval()
        now = time.monotonic()
        if now - self._drag_scroll_last_step_at < interval:
            return
        if self._scroll_one_action_card(direction):
            self._drag_scroll_last_step_at = now

    def _clear_drag_state(self):
        self._set_drag_global_wheel_filter_enabled(False)
        self._drag_capture_active = False
        self._manual_dragging = False
        self._drag_active = False
        self._last_drag_pos = None
        self._drag_start_pos = None
        self._drag_source_item = None
        self._drop_target = None
        self._drop_position = QAbstractItemView.DropIndicatorPosition.OnViewport
        self._drag_scroll_last_step_at = 0.0
        self._drag_wheel_angle_remainder = 0.0
        self._drag_wheel_pixel_remainder = 0.0
        self._drag_scroll_timer.stop()
        self.viewport().unsetCursor()
        self._refresh_overlay()

    def dragEnterEvent(self, event):
        # 不再调用 QTreeWidget 的原生 dragEnterEvent。这里仅兜底吞掉可能
        # 残留的 Qt 拖拽事件，实际动作排序由手动拖拽状态机完成。
        self._drag_active = True
        self._update_drop_indicator(event.position().toPoint())
        self._set_drag_global_wheel_filter_enabled(True)
        if not self._drag_scroll_timer.isActive():
            self._drag_scroll_timer.start()
        event.setDropAction(Qt.MoveAction)
        event.accept()

    def dragMoveEvent(self, event):
        # 不调用 QTreeWidget.dragMoveEvent，滚动只能由 _drag_scroll_timer
        # 按动作卡片槽位推进，避免 dragMove 高频事件造成加速。
        self._drag_active = True
        self._update_drop_indicator(event.position().toPoint())
        self._set_drag_global_wheel_filter_enabled(True)
        if not self._drag_scroll_timer.isActive():
            self._drag_scroll_timer.start()
        event.setDropAction(Qt.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event):
        # Keep the timer and the last insertion line alive while the cursor is
        # just outside the tree, so scrolling can expose a real off-screen row.
        event.accept()

    def wheelEvent(self, event):
        # During a drag, wheel scrolling is constrained to directions containing
        # another action row. Outside a drag, preserve normal QTreeWidget behavior.
        if self._drag_active and self._handle_drag_wheel_event(event):
            return
        super().wheelEvent(event)

    def dropEvent(self, event):
        # Use the same explicit target that was drawn to the user. Reject the
        # native InternalMove so Qt cannot delete the rebuilt row a second time.
        source = self.currentItem()
        target = self._drop_target
        position = self._drop_position
        self._clear_drag_state()
        if source is None:
            event.ignore()
            return
        event.setDropAction(Qt.IgnoreAction)
        event.ignore()
        QTimer.singleShot(
            0,
            lambda s=source, t=target, p=position:
            self.drop_requested.emit(s, t, p),
        )

    def iter_items(self):
        def walk(item):
            yield item
            for child_index in range(item.childCount()):
                yield from walk(item.child(child_index))

        for root_index in range(self.topLevelItemCount()):
            yield from walk(self.topLevelItem(root_index))

    def total_item_count(self):
        return sum(1 for _ in self.iter_items())
