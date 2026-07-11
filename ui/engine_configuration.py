"""Input-backend validation and Kanata configuration generation."""

import ctypes
import os
import json
import threading

from PySide6.QtCore import QEventLoop
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from config.profiles import normalize_profile, profile_payload
from config.storage import atomic_write_text
from config.schema import MOUSE_NAMES
from core.constants import (
    APP_DIR,
    KANATA_CONFIG_PATH,
    KANATA_SETTINGS_PATH,
    kanata_dir,
    KANATA_KEYBOARD_CONFIG_PATH,
)
from engine.kanata import (
    KanataConfigBuilder,
    interception_keyboard_hwids,
    interception_mouse_hwids,
)


class EngineConfigurationMixin:
    """Build backend files and expose backend liveness predicates."""

    @staticmethod
    def _generated_kanata_configs_current():
        for path in (KANATA_CONFIG_PATH, KANATA_KEYBOARD_CONFIG_PATH):
            try:
                text = path.read_text("utf-8")
            except OSError:
                return False
            if not KanataConfigBuilder.generated_config_is_current(text):
                return False
        return True

    def generate_kanata_config(self):
        if not hasattr(self, "mapping_cards") or not hasattr(self, "preset_cards"):
            return False
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            text = self.build_kanata_config_text()
            atomic_write_text(KANATA_CONFIG_PATH, text)
            keyboard_text = self.build_keyboard_kanata_config_text()
            atomic_write_text(KANATA_KEYBOARD_CONFIG_PATH, keyboard_text)
            if hasattr(self, "engine_hint") and not self.running:
                self.engine_hint.setText(f"配置已生成：{KANATA_CONFIG_PATH}")
            return True
        except (OSError, ValueError) as error:
            if hasattr(self, "engine_hint"):
                self.engine_hint.setText(f"配置生成失败：{error}")
                self.engine_hint.setStyleSheet("color: #ff8496;")
            return False

    def _compiled_profiles(self, keyboard_only=False):
        result = []
        for profile in self.profiles:
            if not profile.get("enabled", False):
                continue
            copied = normalize_profile(profile)
            payload = profile_payload(copied)
            if keyboard_only:
                payload["mappings"] = [
                    item for item in payload.get("mappings", [])
                    if item.get("source") not in MOUSE_NAMES
                ]
                payload["presets"] = [
                    item for item in payload.get("presets", [])
                    if item.get("trigger") not in MOUSE_NAMES
                ]
            copied["payload"] = payload
            result.append(copied)
        return result

    def build_kanata_config_text(self):
        self._store_editor_payload()
        conflicts = self.detect_trigger_conflicts()
        if conflicts:
            raise ValueError("；".join(conflicts))
        if self._is_game_mode():
            return KanataConfigBuilder(
                [], [], global_toggle_enabled=False
            ).build()
        base_payload = profile_payload({"payload": self.base_profile_payload})
        return KanataConfigBuilder(
            base_payload.get("mappings", []),
            base_payload.get("presets", []),
            global_toggle_enabled=self.global_toggle_enabled,
            global_toggle_modifiers=self.global_toggle_modifiers,
            global_toggle_key=self.global_toggle_key,
            macro_pause_enabled=self.macro_pause_enabled,
            macro_pause_modifiers=self.macro_pause_modifiers,
            macro_pause_key=self.macro_pause_key,
            emergency_modifiers=self.emergency_modifiers,
            emergency_key=self.emergency_key,
            mouse_hwids=interception_mouse_hwids(),
            emit_diagnostics=self.diagnostic_enabled,
            profiles=self._compiled_profiles(keyboard_only=False),
        ).build()

    def _runtime_engine_backend(self):
        backend = str(getattr(self, "runtime_engine_backend", "") or "")
        if backend:
            return backend
        combo = getattr(self, "backend_combo", None)
        if combo is not None:
            return combo.currentText()
        return "普通模式（winIOv2）"

    def _is_game_mode(self):
        return self.backend_combo.currentText() == "游戏模式（Interception）"

    def _runtime_is_game_mode(self):
        """Return the backend that currently owns live physical input.

        The backend combo is an editable candidate and may already show a new
        value while the previously applied engine is still running or while a
        failed candidate remains visible for correction.  Runtime cleanup and
        control listeners must follow the last applied backend snapshot instead
        of that unapplied selection.
        """
        if self.running:
            return bool(self.direct_interception_active)
        return self._runtime_engine_backend() == "游戏模式（Interception）"

    def _interception_source_ready(self):
        hook = self.interception_input_hook
        output = self.interception_output
        return bool(
            hook and hook.context and hook.thread and hook.thread.is_alive()
            and output and output.context
            and (output.keyboard_device or output.mouse_device)
        )

    def _macro_backend_active(self):
        if not self.running:
            return False
        if self.direct_interception_active:
            return self._interception_source_ready()
        return self.engine.is_running()

    @staticmethod
    def _kanata_engine_has_runtime(engine):
        return bool(
            engine.is_running()
            or getattr(engine, "command_thread", None)
            or getattr(engine, "process", None)
        )

    def _validate_selected_backend(self):
        selected = self.backend_combo.currentText()
        if selected == "游戏模式（Interception）":
            if os.name != "nt":
                return False, "Interception 游戏模式仅支持 Windows"
            dll_path = kanata_dir() / "interception.dll"
            if not dll_path.exists():
                return False, f"找不到 Interception：{dll_path}"
            try:
                ctypes.WinDLL(str(dll_path))
            except OSError as error:
                return False, f"Interception DLL 无法加载：{error}"
            return True, "Interception 键盘、鼠标来源与输出通道可用"
        result = {}
        done = threading.Event()

        def validate_worker():
            try:
                result["value"] = self.engine.validate(selected)
            except Exception as error:
                result["value"] = (False, f"Kanata 校验异常：{error}")
            finally:
                done.set()

        thread = threading.Thread(
            target=validate_worker,
            name="MacroCanvas-KanataValidation",
            daemon=False,
        )
        thread.start()
        while not done.wait(0.04):
            # Keep the in-window progress indicator painting, but do not admit
            # user input or start a second configuration transaction.
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 10
            )
        thread.join(timeout=0.2)
        return result.get("value", (False, "Kanata 校验未返回结果"))

    def build_keyboard_kanata_config_text(self):
        self._store_editor_payload()
        if self._is_game_mode():
            return KanataConfigBuilder(
                [], [], global_toggle_enabled=False
            ).build()
        base_payload = profile_payload({"payload": self.base_profile_payload})
        mappings = [
            item for item in base_payload.get("mappings", [])
            if item.get("source") not in MOUSE_NAMES
        ]
        presets = [
            item for item in base_payload.get("presets", [])
            if item.get("trigger") not in MOUSE_NAMES
        ]
        return KanataConfigBuilder(
            mappings,
            presets,
            global_toggle_enabled=self.global_toggle_enabled,
            global_toggle_modifiers=self.global_toggle_modifiers,
            global_toggle_key=self.global_toggle_key,
            macro_pause_enabled=self.macro_pause_enabled,
            macro_pause_modifiers=self.macro_pause_modifiers,
            macro_pause_key=self.macro_pause_key,
            emergency_modifiers=self.emergency_modifiers,
            emergency_key=self.emergency_key,
            keyboard_hwids=interception_keyboard_hwids(),
            emit_diagnostics=self.diagnostic_enabled,
            profiles=self._compiled_profiles(keyboard_only=True),
        ).build()

    def choose_kanata_directory(self):
        environment_override = str(
            os.getenv("MACROCANVAS_KANATA_DIR", "") or ""
        ).strip()
        if environment_override:
            QMessageBox.information(
                self,
                "组件目录由环境变量管理",
                "当前 MACROCANVAS_KANATA_DIR 环境变量优先于界面设置：\n"
                f"{environment_override}",
            )
            return False
        if getattr(self, "recording_session_active", False):
            QMessageBox.information(
                self, "正在录制", "请先完成或取消录制，再修改 Kanata 组件目录。"
            )
            return False
        if getattr(self, "running", False):
            QMessageBox.information(
                self, "输入引擎正在运行", "请先停止输入引擎，再修改 Kanata 组件目录。"
            )
            return False
        selected = QFileDialog.getExistingDirectory(
            self, "选择 Kanata 组件目录", str(kanata_dir())
        )
        if not selected:
            return False
        directory = os.path.abspath(selected)
        backend = self.backend_combo.currentText()
        executable = os.path.join(
            directory, self.engine.EXECUTABLES.get(backend, "")
        )
        missing = []
        if not os.path.isfile(executable):
            missing.append(os.path.basename(executable) or "Kanata 可执行文件")
        if backend == "游戏模式（Interception）" and not os.path.isfile(
            os.path.join(directory, "interception.dll")
        ):
            missing.append("interception.dll")
        if missing:
            QMessageBox.warning(
                self,
                "组件目录无效",
                "所选目录缺少当前输入模式需要的文件：\n"
                + "\n".join(f"- {name}" for name in missing),
            )
            return False
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                KANATA_SETTINGS_PATH,
                json.dumps({"kanata_dir": directory}, ensure_ascii=False, indent=2),
            )
        except OSError as error:
            QMessageBox.warning(self, "组件目录保存失败", str(error))
            return False
        self.engine_hint.setStyleSheet("")
        self.engine_hint.setText(f"Kanata 组件目录已更新：{directory}")
        action = getattr(self, "kanata_directory_action", None)
        if action is not None:
            action.setToolTip(f"当前目录：{directory}")
        return True
