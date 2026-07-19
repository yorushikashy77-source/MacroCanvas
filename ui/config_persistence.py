"""Configuration serialization, backups, and persistence for the main window.

This mixin deliberately owns no widgets.  The main window remains responsible
for collecting editor state and for applying a saved configuration to the
runtime backends.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QDialog, QMessageBox

from config.profiles import profile_payload
from config.diff import merge_config_sections, selected_section_labels
from config.schema import (
    MAX_CONFIG_FILE_BYTES, repair_duplicate_action_tree_ids,
    repair_duplicate_runtime_ids, repair_overlapping_loop_controls,
    validate_config_payload,
)
from config.storage import (
    atomic_write_text,
    load_valid_snapshot,
    write_deduplicated_snapshot,
)
from core.constants import (
    APP_DIR,
    CONFIG_BACKUP_DIR,
    CONFIG_BACKUP_LIMIT,
    CONFIG_PATH,
)
from ui.backup_manager import BackupManagerDialog


class ConfigPersistenceMixin:
    """Persist and restore configuration owned by a ``MainWindow`` instance."""

    def current_config_payload(self):
        if not hasattr(self, "mapping_cards") or not hasattr(self, "preset_cards"):
            return {}
        self._store_editor_payload()
        base_payload = profile_payload({"payload": self.base_profile_payload})
        return {
            "version": 24,
            "profiles": json.loads(json.dumps(self.profiles)),
            "profile_auto_switch_enabled": self.profile_auto_switch_enabled,
            "active_profile_id": self.active_profile_id,
            "editor_profile_id": self.editor_profile_id,
            "engine_backend": self.backend_combo.currentText(),
            "auto_apply": self.auto_apply_checkbox.isChecked(),
            "global_toggle_enabled": self.global_toggle_enabled,
            "global_toggle_modifiers": self.global_toggle_modifiers,
            "global_toggle_key": self.global_toggle_key,
            "macro_pause_enabled": self.macro_pause_enabled,
            "macro_pause_modifiers": self.macro_pause_modifiers,
            "macro_pause_key": self.macro_pause_key,
            "emergency_modifiers": self.emergency_modifiers,
            "emergency_key": self.emergency_key,
            "recording_cancel_modifiers": self.recording_cancel_modifiers,
            "recording_cancel_key": self.recording_cancel_key,
            "recording_finish_modifiers": self.recording_finish_modifiers,
            "recording_finish_key": self.recording_finish_key,
            "diagnostic_enabled": self.diagnostic_enabled,
            "mappings": base_payload["mappings"],
            "presets": base_payload["presets"],
        }

    def current_config_signature(self):
        return self.config_payload_signature(self.current_config_payload())

    @staticmethod
    def config_payload_signature(payload):
        payload = json.loads(json.dumps(payload or {}))
        payload.pop("auto_apply", None)
        # Runtime/editor selection changes are not unapplied content changes.
        payload.pop("active_profile_id", None)
        payload.pop("editor_profile_id", None)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def save_auto_apply_preference(self):
        if getattr(self, "startup_recovery_pending_save", False):
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "自动应用偏好将随恢复后的配置一起保存；原配置文件尚未覆盖"
                )
            return False
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            data = validate_config_payload(
                json.loads(CONFIG_PATH.read_text("utf-8"))
            )
            data["auto_apply"] = self.auto_apply_checkbox.isChecked()
            self._save_config_payload(data, create_backup=False)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(
                self,
                "自动应用偏好未保存",
                "主配置文件当前无法安全读取。为避免用内存配置覆盖原文件，"
                "本次只在界面中保留该选择；成功应用配置后会一并保存。\n\n"
                f"{error}",
            )
            return False
        return True

    def _load_latest_config_backup(self):
        """Load the newest valid saved snapshot for startup recovery."""
        def validate_startup_snapshot(payload):
            repaired, _removed = repair_overlapping_loop_controls(payload)
            repaired, _action_ids = repair_duplicate_action_tree_ids(repaired)
            repaired, _ids = repair_duplicate_runtime_ids(repaired)
            return validate_config_payload(repaired)

        data, _path = load_valid_snapshot(
            CONFIG_BACKUP_DIR,
            "saved",
            validate_startup_snapshot,
            legacy_prefixes=("config",),
            max_bytes=MAX_CONFIG_FILE_BYTES,
        )
        return data

    @Slot()
    def open_backup_config_table(self):
        if self._configuration_change_blocked_by_recording():
            return
        cancel_countdown = getattr(self, "_cancel_manual_test_countdown", None)
        if callable(cancel_countdown):
            cancel_countdown("已打开备份配置，原测试倒计时已取消")
        current_payload = validate_config_payload(
            json.loads(json.dumps(self.current_config_payload(), ensure_ascii=False))
        )
        dialog = BackupManagerDialog(
            CONFIG_BACKUP_DIR,
            self,
            current_payload=current_payload,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        snapshot = dialog.selected_snapshot()
        if not snapshot or snapshot.get("payload") is None:
            return
        selected_sections = set(snapshot.get("selected_sections") or [])
        if not selected_sections:
            return

        state_name = getattr(self.config_state, "name", "")
        has_unapplied_changes = state_name == "DIRTY"
        if state_name == "FAILED":
            try:
                has_unapplied_changes = (
                    self.current_config_signature()
                    != self.applied_config_signature
                )
            except Exception:
                has_unapplied_changes = True
        if has_unapplied_changes:
            answer = QMessageBox.question(
                self,
                "先应用当前修改",
                "选择性恢复会保留未选择区块的当前内容。为了确保恢复失败时"
                "能够完整回退，需要先成功应用当前修改，再与备份合并。\n\n"
                "是否先应用当前修改并继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.apply_changes()
            if getattr(self.config_state, "name", "") in ("DIRTY", "FAILED"):
                QMessageBox.warning(
                    self,
                    "当前修改未能应用",
                    "程序没有开始恢复备份；请先修正当前配置的应用错误。",
                )
                return
            current_payload = validate_config_payload(
                json.loads(json.dumps(
                    self.current_config_payload(), ensure_ascii=False
                ))
            )
        try:
            restored_payload = validate_config_payload(
                merge_config_sections(
                    current_payload,
                    snapshot["payload"],
                    selected_sections,
                )
            )
        except (ValueError, TypeError, RecursionError, MemoryError) as error:
            QMessageBox.warning(
                self,
                "无法组合所选恢复范围",
                "所选区块与当前配置组合后未能通过完整性校验，程序没有修改"
                f"任何配置。\n\n{error}",
            )
            return
        selected_labels = selected_section_labels(selected_sections)

        running_note = (
            "当前输入引擎会暂时停止，恢复完成后自动重新启动。"
            if self.running else
            "当前输入引擎未启动，恢复完成后仍保持停止。"
        )
        answer = QMessageBox.question(
            self,
            "恢复备份配置",
            f"将从“{snapshot.get('type_label', '配置备份')}”恢复以下区块：\n"
            + "\n".join(f"• {label}" for label in selected_labels)
            + "\n\n"
            f"备份时间：{snapshot.get('time_label', '时间未知')}\n\n"
            "只有所选区块会被备份内容替换，其他当前配置保持不变。恢复前的"
            "当前完整配置会自动保存为一份可回退快照。\n"
            f"{running_note}\n\n"
            "是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._begin_loading(
            "正在恢复备份配置",
            "正在校验备份并重新载入当前窗口……",
            host=self,
        )
        try:
            restored = self._overwrite_full_configuration_in_place(
                restored_payload
            )
        finally:
            self._end_loading()

        # Avoid importing the main window's state enum and creating a cycle.
        state_name = getattr(self.config_state, "name", "")
        if restored and state_name != "FAILED":
            QMessageBox.information(
                self,
                "备份配置已恢复",
                "所选配置区块已在当前窗口中载入并保存，不需要重新启动程序。",
            )

    def _save_config_payload(self, data, create_backup=True):
        """Persist config and optionally record a de-duplicated saved snapshot."""
        validate_config_payload(data)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        validate_config_payload(json.loads(text))
        atomic_write_text(CONFIG_PATH, text)
        if create_backup:
            try:
                write_deduplicated_snapshot(
                    CONFIG_BACKUP_DIR,
                    "saved",
                    text,
                    limit=CONFIG_BACKUP_LIMIT,
                    legacy_prefixes=("config",),
                )
            except OSError as error:
                # The live configuration has already been replaced atomically.
                # A backup-folder failure must not be reported as if saving the
                # actual configuration failed and trigger a false runtime split.
                if hasattr(self, "write_diagnostic"):
                    self.write_diagnostic(
                        "saved_snapshot_failed", error=str(error), force=True
                    )

    def _record_applied_config_snapshot(self, data, show_warning=True):
        """Record a snapshot after the selected backend accepted the config."""
        try:
            validate_config_payload(data)
            text = json.dumps(data, ensure_ascii=False, indent=2)
            validate_config_payload(json.loads(text))
            write_deduplicated_snapshot(
                CONFIG_BACKUP_DIR,
                "applied",
                text,
                limit=CONFIG_BACKUP_LIMIT,
            )
            return True
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self.write_diagnostic(
                "applied_snapshot_failed",
                error=str(error),
                force=True,
            )
            if show_warning:
                QMessageBox.warning(
                    self,
                    "成功应用快照保存失败",
                    "配置已经保存并成功应用，但无法记录成功应用快照。"
                    "上一份成功应用快照不会被覆盖。\n\n"
                    f"{error}",
                )
            return False

    def save_config(self, mark_applied=False):
        if not hasattr(self, "mapping_cards") or not hasattr(self, "preset_cards"):
            return False
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            data = self.current_config_payload()
            self._save_config_payload(data, create_backup=True)
            self.startup_recovery_pending_save = False
            self.applied_config_payload = json.loads(json.dumps(data))
            self.editor_loaded_profile_id = str(self.editor_profile_id or "")
            self.editor_loaded_payload = profile_payload({
                "payload": self._current_profile_snapshot()
            })
        except (OSError, ValueError) as error:
            QMessageBox.warning(
                self,
                "配置保存失败",
                f"配置没有保存，当前编辑内容仍保留在界面中：\n{error}",
            )
            return False

        if mark_applied:
            self._record_applied_config_snapshot(data, show_warning=True)
        return True
