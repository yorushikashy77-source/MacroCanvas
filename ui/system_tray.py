"""System-tray lifecycle that always reuses coordinated shutdown."""

from __future__ import annotations

import json

from PySide6.QtCore import Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QMenu, QMessageBox, QStyle, QSystemTrayIcon,
)

from config.storage import atomic_write_text
from core.constants import APP_NAME, UI_SETTINGS_PATH
from ui.operation_state import operation_blocks, operation_state_snapshot


class SystemTrayMixin:
    def tray_visibility_requires_visible_safety_flow(self):
        macro_state = getattr(self, "macro_state", None)
        macro_state_name = str(
            getattr(macro_state, "name", macro_state or "")
        ).upper()
        return bool(
            getattr(self, "recording_session_active", False)
            or (
                macro_state_name == "COUNTDOWN"
                and str(getattr(self, "_test_countdown_preset_id", "") or "")
            )
        )

    def _initialize_system_tray_state(self):
        self.system_tray = None
        self.close_to_tray_enabled = True
        self._tray_exit_requested = False
        self._tray_notice_shown = False
        self._tray_settings_load_warning = ""
        if not UI_SETTINGS_PATH.exists():
            return
        try:
            if UI_SETTINGS_PATH.stat().st_size > 64 * 1024:
                raise ValueError("界面设置文件超过 64 KB 安全上限")
            payload = json.loads(UI_SETTINGS_PATH.read_text("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("界面设置文件不是 JSON 对象")
            close_to_tray = payload.get("close_to_tray", True)
            if not isinstance(close_to_tray, bool):
                raise ValueError("close_to_tray 必须是布尔值")
            self.close_to_tray_enabled = close_to_tray
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            # A damaged preference must not silently keep a low-level input tool
            # alive after the user closes its main window. Fail closed and make
            # the recovery visible once the tray icon exists.
            self.close_to_tray_enabled = False
            self._tray_settings_load_warning = str(error) or error.__class__.__name__

    def add_system_tray_settings(self, settings_menu):
        self.close_to_tray_action = settings_menu.addAction(
            "关闭主窗口时最小化到托盘"
        )
        self.close_to_tray_action.setCheckable(True)
        self.close_to_tray_action.setChecked(self.close_to_tray_enabled)
        if self._tray_settings_load_warning:
            self.close_to_tray_action.setToolTip(
                "界面设置无法读取；为安全起见，关闭到托盘已暂时停用"
            )
        self.close_to_tray_action.toggled.connect(self.set_close_to_tray_enabled)

    @Slot(bool)
    def set_close_to_tray_enabled(self, enabled):
        requested = bool(enabled)
        previous = bool(getattr(self, "close_to_tray_enabled", True))
        try:
            payload = {}
            try:
                if (
                    UI_SETTINGS_PATH.exists()
                    and UI_SETTINGS_PATH.stat().st_size <= 64 * 1024
                ):
                    loaded = json.loads(UI_SETTINGS_PATH.read_text("utf-8"))
                    if isinstance(loaded, dict):
                        payload = loaded
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # The explicit user choice is allowed to repair a damaged small
                # settings file. Unknown valid fields are preserved when possible.
                payload = {}
            payload["close_to_tray"] = requested
            UI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                UI_SETTINGS_PATH,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except OSError as error:
            action = getattr(self, "close_to_tray_action", None)
            if action is not None:
                action.blockSignals(True)
                action.setChecked(previous)
                action.blockSignals(False)
            self.close_to_tray_enabled = previous
            QMessageBox.warning(
                self,
                "托盘设置未保存",
                "无法保存“关闭主窗口时最小化到托盘”设置，界面已恢复为"
                f"原来的状态。\n\n{error}",
            )
            return False
        self.close_to_tray_enabled = requested
        self._tray_settings_load_warning = ""
        action = getattr(self, "close_to_tray_action", None)
        if action is not None:
            action.setToolTip("")
        return True

    def setup_system_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            if hasattr(self, "close_to_tray_action"):
                self.close_to_tray_action.blockSignals(True)
                self.close_to_tray_action.setChecked(False)
                self.close_to_tray_action.blockSignals(False)
                self.close_to_tray_action.setEnabled(False)
                self.close_to_tray_action.setToolTip("当前桌面环境没有可用的系统托盘")
            self.close_to_tray_enabled = False
            return False

        icon = self.windowIcon()
        if icon.isNull():
            app = QApplication.instance()
            style = app.style() if app is not None else self.style()
            icon = style.standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)

        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip(APP_NAME)
        menu = QMenu(self)
        self.tray_show_action = QAction("显示主窗口", menu)
        self.tray_show_action.triggered.connect(self.show_from_system_tray)
        menu.addAction(self.tray_show_action)

        self.tray_mapping_action = QAction("暂停全部映射", menu)
        self.tray_mapping_action.triggered.connect(self.toggle_mappings_from_tray)
        menu.addAction(self.tray_mapping_action)

        self.tray_stop_action = QAction("停止全部宏", menu)
        self.tray_stop_action.triggered.connect(
            lambda: self.stop_all_macros(play_sound=False)
        )
        menu.addAction(self.tray_stop_action)
        menu.addSeparator()

        self.tray_exit_action = QAction("安全退出", menu)
        self.tray_exit_action.triggered.connect(self.request_exit_from_system_tray)
        menu.addAction(self.tray_exit_action)
        menu.aboutToShow.connect(self.refresh_system_tray_actions)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_system_tray_activated)
        tray.show()
        self.system_tray = tray
        if self._tray_settings_load_warning:
            tray.showMessage(
                APP_NAME,
                "界面设置无法读取；为安全起见，本次启动已关闭“关闭到托盘”。\n"
                f"{self._tray_settings_load_warning}",
                QSystemTrayIcon.MessageIcon.Warning,
                6000,
            )
        return True

    @Slot()
    def refresh_system_tray_actions(self):
        if not hasattr(self, "tray_mapping_action"):
            return
        must_stay_visible = self.tray_visibility_requires_visible_safety_flow()
        visible = self.isVisible()
        self.tray_show_action.setText(
            "录制或测试期间保持显示"
            if visible and must_stay_visible
            else ("隐藏主窗口" if visible else "显示主窗口")
        )
        self.tray_show_action.setEnabled(not (visible and must_stay_visible))
        self.tray_mapping_action.setText(
            "暂停全部映射"
            if getattr(self, "mappings_enabled", True)
            else "恢复全部映射"
        )
        shutting_down = bool(getattr(self, "_shutdown_started", False))
        runtime_blocked, _snapshot = operation_blocks(self, "runtime_entry")
        self.tray_mapping_action.setEnabled(
            not shutting_down
            and not runtime_blocked
            and bool(getattr(self, "running", False))
        )
        self.tray_stop_action.setEnabled(not shutting_down)

    @Slot()
    def show_from_system_tray(self):
        if self.isVisible():
            if self.tray_visibility_requires_visible_safety_flow():
                self.raise_()
                self.activateWindow()
                return False
            self.hide()
            return True
        self.showNormal()
        self.raise_()
        self.activateWindow()
        return True

    @Slot()
    def toggle_mappings_from_tray(self):
        blocked, _snapshot = operation_blocks(self, "runtime_entry")
        if getattr(self, "_shutdown_started", False) or blocked:
            return
        self.set_mappings_enabled(
            not bool(getattr(self, "mappings_enabled", True)), sound=True
        )
        self.refresh_system_tray_actions()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_system_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_from_system_tray()
        elif (
            reason == QSystemTrayIcon.ActivationReason.DoubleClick
            and not self.isVisible()
        ):
            self.show_from_system_tray()

    @Slot()
    def request_exit_from_system_tray(self):
        if getattr(self, "_shutdown_complete", False):
            return
        self._tray_exit_requested = True
        if not self.isVisible():
            self.showNormal()
            self.raise_()
            self.activateWindow()
        self.close()
        if not getattr(self, "_shutdown_complete", False):
            self._tray_exit_requested = False

    def should_hide_close_to_tray(self):
        requires_visible_safety_flow = (
            self.tray_visibility_requires_visible_safety_flow()
        )
        return bool(
            self.close_to_tray_enabled
            and self.system_tray is not None
            and self.system_tray.isVisible()
            and not self._tray_exit_requested
            and not getattr(self, "_shutdown_started", False)
            and not requires_visible_safety_flow
        )

    def hide_close_to_tray(self, event):
        event.ignore()
        self.hide()
        if not self._tray_notice_shown and self.system_tray is not None:
            self.system_tray.showMessage(
                APP_NAME,
                "程序仍在托盘中运行；右键托盘图标可停止宏或安全退出。",
                QSystemTrayIcon.MessageIcon.Information,
                3500,
            )
            self._tray_notice_shown = True

    def dispose_system_tray(self):
        tray = getattr(self, "system_tray", None)
        if tray is not None:
            tray.hide()
