"""Global control-hotkey settings used by the main window."""

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
)

from config.profiles import profile_payload
from core.constants import SYSTEM_HOTKEY_KEYS
from engine.trigger_resolver import MODIFIER_ORDER, combo_text
from ui.editors import HotkeyEdit


class HotkeySettingsMixin:
    """Edit global controls and detect collisions with enabled rules."""

    @staticmethod
    def _payload_hotkey_conflict(payload, trigger, scope_label):
        payload = profile_payload({"payload": payload or {}})
        for index, mapping in enumerate(payload.get("mappings", []), 1):
            if not mapping.get("enabled"):
                continue
            current = combo_text(
                mapping.get("source_modifiers", "无"), mapping.get("source", "")
            )
            if current == trigger:
                label = mapping.get("name") or f"基础映射 {index}"
                return f"{scope_label}{label}"
        for index, preset in enumerate(payload.get("presets", []), 1):
            if not preset.get("enabled"):
                continue
            current = combo_text(
                preset.get("trigger_modifiers", "无"), preset.get("trigger", "")
            )
            if current == trigger:
                label = preset.get("name") or f"预设 {index}"
                return f"{scope_label}{label}"
        return ""

    def global_hotkey_conflict(self, modifiers, key):
        """Check the editable base payload and every enabled profile."""
        trigger = combo_text(modifiers, key)
        self._store_editor_payload()

        conflict = self._payload_hotkey_conflict(
            self.base_profile_payload, trigger, "基础配置 · "
        )
        if conflict:
            return conflict

        for profile in self.profiles:
            if not profile.get("enabled", False):
                continue
            profile_name = str(profile.get("name") or "未命名档案")
            conflict = self._payload_hotkey_conflict(
                profile_payload(profile), trigger, f"档案“{profile_name}” · "
            )
            if conflict:
                return conflict
        return ""

    def _hotkey_candidate_error(
        self, *, toggle_enabled, toggle, pause_enabled, pause,
        emergency, cancel, finish,
    ):
        """Return a validation error without closing the settings dialog."""
        toggle_modifiers, toggle_key = toggle
        pause_modifiers, pause_key = pause
        emergency_modifiers, emergency_key = emergency
        cancel_modifiers, cancel_key = cancel
        finish_modifiers, finish_key = finish

        active_control_values = []
        if toggle_enabled:
            active_control_values.append(("引擎开关", *toggle))
        if pause_enabled:
            active_control_values.append(("暂停 / 继续宏", *pause))
        active_control_values.append(("停止全部", *emergency))

        for label, modifiers, key in (
            active_control_values
            + [("取消录制", *cancel), ("完成录制", *finish)]
        ):
            if key in MODIFIER_ORDER:
                return (
                    "快捷键无效",
                    f"{label} 使用了 {combo_text(modifiers, key)}。"
                    "Ctrl / Shift / Alt 只能作为组合修饰键，不能作为触发主键。",
                )

        seen_controls = {}
        for label, modifiers, key in active_control_values:
            shortcut = combo_text(modifiers, key)
            if shortcut in seen_controls:
                return (
                    "快捷键冲突",
                    f"{label}与{seen_controls[shortcut]}不能使用同一个快捷键。",
                )
            seen_controls[shortcut] = label

        cancel_text = combo_text(cancel_modifiers, cancel_key)
        finish_text = combo_text(finish_modifiers, finish_key)
        if cancel_text == finish_text:
            return (
                "快捷键冲突", "取消录制与完成录制不能使用同一个快捷键。"
            )

        recording_controls = (
            ("取消录制", cancel_text, cancel_key),
            ("完成录制", finish_text, finish_key),
        )
        active_control_keys = [
            (label, combo_text(modifiers, key), key)
            for label, modifiers, key in active_control_values
        ]
        for recording_label, recording_text, recording_key in recording_controls:
            for control_label, control_text, control_key in active_control_keys:
                if recording_key != control_key:
                    continue
                if (
                    recording_label == "完成录制"
                    and control_label == "停止全部"
                    and recording_text == control_text
                ):
                    continue
                return (
                    "快捷键冲突",
                    f"{recording_label}（{recording_text}）不能与{control_label}"
                    f"（{control_text}）共用主键 {recording_key}。请更换其中一个主键。",
                )

        for label, modifiers, key in active_control_values:
            conflict = self.global_hotkey_conflict(modifiers, key)
            if conflict:
                return (
                    "快捷键冲突",
                    f"{combo_text(modifiers, key)} 已被“{conflict}”使用。"
                    f"请先更换{label}或对应映射的快捷键。",
                )
        return None

    @Slot()
    def open_global_hotkey_settings(self):
        if getattr(self, "recording_session_active", False):
            QMessageBox.information(
                self,
                "正在录制",
                "录制倒计时或正式录制期间不能修改全局快捷键。请先完成或取消录制。",
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("快捷键设置")
        dialog.setMinimumWidth(560)
        form = QFormLayout(dialog)

        enabled = QCheckBox("启用全局输入引擎开关")
        enabled.setChecked(self.global_toggle_enabled)
        toggle_hotkey = HotkeyEdit(
            self.global_toggle_modifiers,
            self.global_toggle_key,
            SYSTEM_HOTKEY_KEYS,
            dialog,
            reserved_keys=set(),
        )
        toggle_hotkey.setEnabled(enabled.isChecked())
        enabled.toggled.connect(toggle_hotkey.setEnabled)

        pause_enabled = QCheckBox("启用宏任务暂停 / 继续快捷键")
        pause_enabled.setChecked(self.macro_pause_enabled)
        pause_hotkey = HotkeyEdit(
            self.macro_pause_modifiers,
            self.macro_pause_key,
            SYSTEM_HOTKEY_KEYS,
            dialog,
            reserved_keys=set(),
        )
        pause_hotkey.setEnabled(pause_enabled.isChecked())
        pause_enabled.toggled.connect(pause_hotkey.setEnabled)

        emergency_hotkey = HotkeyEdit(
            self.emergency_modifiers,
            self.emergency_key,
            SYSTEM_HOTKEY_KEYS,
            dialog,
            reserved_keys=set(),
        )
        cancel_hotkey = HotkeyEdit(
            self.recording_cancel_modifiers,
            self.recording_cancel_key,
            SYSTEM_HOTKEY_KEYS,
            dialog,
            reserved_keys=set(),
        )
        finish_hotkey = HotkeyEdit(
            self.recording_finish_modifiers,
            self.recording_finish_key,
            SYSTEM_HOTKEY_KEYS,
            dialog,
            reserved_keys=set(),
        )

        note = QLabel(
            "全局开关会完整启动或关闭输入引擎；关闭后仍保留一个仅识别该开关的 "
            "Interception 控制监听，以便在游戏中再次启动。宏任务暂停键会统一暂停"
            "当前全部宏任务，输入引擎和基础映射保持运行，再次触发后从暂停处继续。"
            "急停仍会停止全部动作并释放按键。完成录制可以与急停使用完全相同的"
            "快捷键；录制期间该快捷键优先用于完成录制。"
        )
        note.setWordWrap(True)
        note.setObjectName("muted")

        form.addRow("功能", enabled)
        form.addRow("引擎开关", toggle_hotkey)
        form.addRow("宏暂停功能", pause_enabled)
        form.addRow("暂停 / 继续宏", pause_hotkey)
        form.addRow("停止全部 / 急停", emergency_hotkey)
        form.addRow("取消录制", cancel_hotkey)
        form.addRow("完成录制", finish_hotkey)
        form.addRow("", note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")

        def validate_and_accept():
            issue = self._hotkey_candidate_error(
                toggle_enabled=enabled.isChecked(),
                toggle=toggle_hotkey.value(),
                pause_enabled=pause_enabled.isChecked(),
                pause=pause_hotkey.value(),
                emergency=emergency_hotkey.value(),
                cancel=cancel_hotkey.value(),
                finish=finish_hotkey.value(),
            )
            if issue is not None:
                QMessageBox.warning(dialog, issue[0], issue[1])
                return
            dialog.accept()

        buttons.accepted.connect(validate_and_accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if not self._enter_settings_input_mode():
            dialog.deleteLater()
            return
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._leave_settings_input_mode()

        if not accepted:
            return

        toggle_modifiers, toggle_key = toggle_hotkey.value()
        pause_modifiers, pause_key = pause_hotkey.value()
        emergency_modifiers, emergency_key = emergency_hotkey.value()
        cancel_modifiers, cancel_key = cancel_hotkey.value()
        finish_modifiers, finish_key = finish_hotkey.value()

        self.global_toggle_enabled = enabled.isChecked()
        self.global_toggle_modifiers = toggle_modifiers
        self.global_toggle_key = toggle_key
        self.macro_pause_enabled = pause_enabled.isChecked()
        self.macro_pause_modifiers = pause_modifiers
        self.macro_pause_key = pause_key
        self.emergency_modifiers = emergency_modifiers
        self.emergency_key = emergency_key
        self.recording_cancel_modifiers = cancel_modifiers
        self.recording_cancel_key = cancel_key
        self.recording_finish_modifiers = finish_modifiers
        self.recording_finish_key = finish_key
        self.update_global_hotkey_action_text()
        self.data_changed()
        self.engine_hint.setStyleSheet("")
        self.engine_hint.setText("快捷键设置已修改；点击“应用更改”后生效")
