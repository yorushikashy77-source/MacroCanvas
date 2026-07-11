"""Configuration and preset import/export workflows."""

import json
import re
import time
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from config.schema import (
    MAX_CONFIG_FILE_BYTES, repair_duplicate_action_tree_ids,
    repair_duplicate_runtime_ids, repair_overlapping_loop_controls,
    validate_config_payload, validate_preset_payload,
)
from config.storage import atomic_write_text
from config.transfer import (
    clone_mapping_for_import,
    clone_preset_for_import,
    merge_full_configurations,
)
from core.constants import ConfigState


class ConfigurationTransferMixin:
    def _capture_editor_import_transaction(self):
        """Capture enough UI/model state to roll back a partially built import."""
        hint = getattr(self, "engine_hint", None)
        reload_button = getattr(self, "reload_button", None)
        return {
            "mapping_cards": list(getattr(self, "mapping_cards", [])),
            "preset_cards": list(getattr(self, "preset_cards", [])),
            "selected_preset_card": getattr(self, "selected_preset_card", None),
            "config_state": getattr(self, "config_state", None),
            "reload_enabled": reload_button.isEnabled() if reload_button else None,
            "hint_text": hint.text() if hint else "",
            "hint_style": hint.styleSheet() if hint else "",
        }

    def _rollback_editor_import_transaction(self, transaction):
        """Remove every card created after a failed in-editor import."""
        original_mappings = set(transaction.get("mapping_cards", []))
        original_presets = set(transaction.get("preset_cards", []))
        for card in list(getattr(self, "preset_cards", [])):
            if card in original_presets:
                continue
            if hasattr(card, "action_dialog"):
                card.action_dialog.close()
                card.action_dialog.deleteLater()
            self.preset_cards.remove(card)
            self.preset_layout.removeWidget(card)
            card.hide()
            card.deleteLater()
        for card in list(getattr(self, "mapping_cards", [])):
            if card in original_mappings:
                continue
            self.mapping_cards.remove(card)
            self.mapping_layout.removeWidget(card)
            card.hide()
            card.deleteLater()

        selected = transaction.get("selected_preset_card")
        if selected in getattr(self, "preset_cards", []):
            self.select_preset_card(selected)
        elif getattr(self, "preset_cards", []):
            self.select_preset_card(self.preset_cards[0])
        else:
            self.selected_preset_card = None
            self.action_table = None
            self.action_title = None
        self.refresh_mapping_priority_labels()
        self.refresh_cache()
        self._store_editor_payload()
        self.config_state = transaction.get("config_state", self.config_state)
        if transaction.get("reload_enabled") is not None:
            self.reload_button.setEnabled(transaction["reload_enabled"])
        self.engine_hint.setStyleSheet(transaction.get("hint_style", ""))
        self.engine_hint.setText(transaction.get("hint_text", ""))
        self.refresh_status_ui()

    def _validate_editor_import_capacity(self, mappings=None, presets=None):
        """Validate the complete post-import config before creating Qt widgets."""
        candidate = json.loads(json.dumps(self.current_config_payload()))
        target_mappings = candidate.setdefault("mappings", [])
        target_presets = candidate.setdefault("presets", [])
        editor_profile_id = str(getattr(self, "editor_profile_id", "") or "")
        if editor_profile_id:
            for profile in candidate.get("profiles", []):
                if str(profile.get("id") or "") != editor_profile_id:
                    continue
                payload = profile.setdefault("payload", {})
                target_mappings = payload.setdefault("mappings", [])
                target_presets = payload.setdefault("presets", [])
                break
        target_mappings.extend(json.loads(json.dumps(mappings or [])))
        target_presets.extend(json.loads(json.dumps(presets or [])))
        validate_config_payload(candidate)

    @staticmethod
    def _export_envelope(kind, payload):
        return {
            "format": "MacroCanvas",
            "kind": kind,
            "format_version": 1,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "payload": payload,
        }

    def _write_export_file(self, title, suggested_name, envelope):
        cancel_countdown = getattr(self, "_cancel_manual_test_countdown", None)
        if callable(cancel_countdown):
            cancel_countdown("已打开配置文件操作，原测试倒计时已取消")
        path, _ = QFileDialog.getSaveFileName(
            self, title, str(Path.home() / suggested_name), "JSON 配置 (*.json)"
        )
        if not path:
            return False
        self._begin_loading(
            "正在导出文件", "正在序列化并写入配置数据……", host=self
        )
        try:
            atomic_write_text(
                Path(path), json.dumps(envelope, ensure_ascii=False, indent=2)
            )
        except OSError as error:
            QMessageBox.warning(self, "导出失败", str(error))
            return False
        finally:
            self._end_loading()
        return True

    def export_full_configuration(self):
        if self._write_export_file(
            "导出完整配置", "MacroCanvas-config.json",
            self._export_envelope("configuration", self.current_config_payload()),
        ):
            self.engine_hint.setText("完整配置已导出")

    def export_selected_preset(self):
        card = self.selected_preset_card
        if card is None:
            QMessageBox.information(self, "没有选中预设", "请先选择一个预设方案。")
            return
        preset = next(
            (item for item in self.collect_presets()
             if item.get("id") == card.preset_id), None
        )
        if preset is None:
            return
        safe_name = re.sub(
            r"[^\w\-\u4e00-\u9fff]+", "_", preset.get("name", "preset")
        )
        if self._write_export_file(
            "导出当前预设", f"{safe_name}.json",
            self._export_envelope("preset", preset),
        ):
            self.engine_hint.setText(f"预设“{preset.get('name')}”已导出")

    def import_configuration_file(self):
        if self._configuration_change_blocked_by_recording():
            return
        cancel_countdown = getattr(self, "_cancel_manual_test_countdown", None)
        if callable(cancel_countdown):
            cancel_countdown("已打开配置导入，原测试倒计时已取消")
        path, _ = QFileDialog.getOpenFileName(
            self, "导入配置或预设", str(Path.home()), "JSON 配置 (*.json)"
        )
        if not path:
            return
        try:
            source_path = Path(path)
            if source_path.stat().st_size > MAX_CONFIG_FILE_BYTES:
                raise ValueError(
                    f"导入文件超过 {MAX_CONFIG_FILE_BYTES // (1024 * 1024)} MB 上限"
                )
            raw = json.loads(source_path.read_text("utf-8-sig"))
            if raw.get("format") == "MacroCanvas":
                kind = raw.get("kind")
                payload = raw.get("payload")
            else:
                kind = "configuration"
                payload = raw
        except (
            OSError, ValueError, json.JSONDecodeError, AttributeError,
            RecursionError, MemoryError,
        ) as error:
            QMessageBox.warning(self, "导入失败", f"文件无法读取：\n{error}")
            return

        if kind == "preset":
            try:
                preset_wrapper = {"mappings": [], "presets": [payload], "profiles": []}
                preset_wrapper, repaired_loops = repair_overlapping_loop_controls(
                    preset_wrapper
                )
                preset_wrapper, repaired_action_ids = repair_duplicate_action_tree_ids(
                    preset_wrapper
                )
                payload = (preset_wrapper.get("presets") or [None])[0]
                validate_preset_payload(payload)
            except (ValueError, RecursionError, MemoryError) as error:
                QMessageBox.warning(self, "导入失败", str(error))
                return
            if repaired_loops:
                self.write_diagnostic(
                    "import_preset_overlapping_loop_controls_repaired",
                    count=len(repaired_loops),
                )
            if repaired_action_ids:
                self.write_diagnostic(
                    "import_preset_duplicate_action_ids_repaired",
                    count=len(repaired_action_ids),
                )
            imported = clone_preset_for_import(payload)
            try:
                self._validate_editor_import_capacity(presets=[imported])
            except (ValueError, RecursionError, MemoryError) as error:
                QMessageBox.warning(self, "导入失败", str(error))
                return
            transaction = self._capture_editor_import_transaction()
            self._begin_loading(
                "正在导入预设",
                f"正在创建“{imported.get('name', '导入预设')}”的编辑表单……",
                host=self,
            )
            import_error = None
            rollback_complete = True
            try:
                before_count = len(self.preset_cards)
                self.add_preset(imported)
                if len(self.preset_cards) != before_count + 1:
                    raise RuntimeError("预设编辑卡未能创建")
            except Exception as error:
                import_error = error
                try:
                    self._rollback_editor_import_transaction(transaction)
                except Exception as rollback_error:
                    rollback_complete = False
                    import_error = RuntimeError(
                        f"{error}；回滚编辑表单时又发生：{rollback_error}"
                    )
            finally:
                self._end_loading()
            if import_error is not None:
                recovery_text = (
                    "已回滚本次导入，原编辑内容未改变。"
                    if rollback_complete else
                    "自动回滚未能完整结束，请检查当前编辑表单后再继续。"
                )
                QMessageBox.warning(
                    self, "导入失败",
                    f"预设表单创建失败，{recovery_text}\n\n"
                    f"{import_error}",
                )
                return
            self.config_state = ConfigState.DIRTY
            self.reload_button.setEnabled(True)
            self.engine_hint.setText(f"已导入预设“{imported['name']}”；应用更改后生效")
            return

        try:
            payload, repaired_loops = repair_overlapping_loop_controls(payload)
            payload, repaired_action_ids = repair_duplicate_action_tree_ids(payload)
            payload, repaired_ids = repair_duplicate_runtime_ids(payload)
            validate_config_payload(payload)
        except (ValueError, RecursionError, MemoryError) as error:
            QMessageBox.warning(self, "导入失败", str(error))
            return
        if repaired_loops:
            self.write_diagnostic(
                "import_overlapping_loop_controls_repaired",
                count=len(repaired_loops),
            )
        if repaired_action_ids:
            self.write_diagnostic(
                "import_duplicate_action_ids_repaired",
                count=len(repaired_action_ids),
            )
        if repaired_ids:
            self.write_diagnostic(
                "import_duplicate_runtime_ids_repaired",
                count=len(repaired_ids),
            )
        mode, accepted = QInputDialog.getItem(
            self, "导入方式", "请选择处理方式：",
            ["导入到当前编辑方案", "合并完整配置", "覆盖完整配置"],
            0, False,
        )
        if not accepted:
            return

        if mode == "合并完整配置":
            current = self.current_config_payload()
            try:
                merged = merge_full_configurations(current, payload)
                validate_config_payload(merged)
            except (ValueError, RecursionError, MemoryError) as error:
                QMessageBox.warning(self, "合并失败", str(error))
                return
            counts = (
                len(payload.get("mappings", []) or []),
                len(payload.get("presets", []) or []),
                len(payload.get("profiles", []) or []),
            )
            answer = QMessageBox.question(
                self, "合并完整配置",
                "将把导入文件中的基础映射、基础预设和全部配置档案追加到"
                "当前完整配置。\n\n"
                f"基础映射：{counts[0]} 个\n基础预设：{counts[1]} 个\n"
                f"配置档案：{counts[2]} 个\n\n"
                "当前输入模式、全局快捷键、录制快捷键和诊断设置会保留，"
                "导入内容会重新生成 ID，不会覆盖现有项目。合并后需点击"
                "“应用更改”保存并生效。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._begin_loading(
                "正在合并完整配置", "正在重建基础配置和配置档案表单……", host=self
            )
            try:
                self._reload_full_configuration_into_window(merged)
            except (ValueError, RuntimeError, RecursionError, MemoryError) as error:
                QMessageBox.warning(self, "合并失败", str(error))
                return
            finally:
                self._end_loading()
            self.config_state = ConfigState.DIRTY
            self.reload_button.setEnabled(True)
            self.engine_hint.setStyleSheet("")
            self.engine_hint.setText("完整配置已合并；当前全局设置已保留，请检查后应用更改")
            self.refresh_status_ui()
            return

        if mode == "覆盖完整配置":
            running_note = (
                "当前输入引擎会暂时停止，载入完成后自动恢复。"
                if self.running else "当前输入引擎未启动，载入后仍保持停止。"
            )
            answer = QMessageBox.question(
                self, "覆盖完整配置",
                "这会用导入文件替换当前全部映射、预设、配置档案和全局设置。"
                "当前未保存的编辑将被丢弃。\n\n"
                f"{running_note}\n"
                "程序本身不会重启，并会在失败时尝试恢复覆盖前的配置。"
                "是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._begin_loading(
                "正在覆盖完整配置", "正在停止旧配置并载入导入内容……", host=self
            )
            try:
                imported_ok = self._overwrite_full_configuration_in_place(payload)
            finally:
                self._end_loading()
            if imported_ok and self.config_state != ConfigState.FAILED:
                QMessageBox.information(
                    self, "配置已导入",
                    "完整配置已在当前窗口中载入并保存，不需要重新启动程序。",
                )
            return

        suffix = "（导入）"
        imported_mappings = [
            clone_mapping_for_import(mapping, suffix)
            for mapping in payload.get("mappings", []) or []
        ]
        imported_presets = [
            clone_preset_for_import(preset, suffix)
            for preset in payload.get("presets", []) or []
        ]
        try:
            self._validate_editor_import_capacity(
                mappings=imported_mappings, presets=imported_presets
            )
        except (ValueError, RecursionError, MemoryError) as error:
            QMessageBox.warning(self, "导入失败", str(error))
            return
        mapping_count = len(imported_mappings)
        preset_count = len(imported_presets)
        transaction = self._capture_editor_import_transaction()
        self._begin_loading(
            "正在导入配置",
            f"正在载入 {mapping_count} 个映射和 {preset_count} 个预设……",
            host=self,
        )
        import_error = None
        rollback_complete = True
        try:
            for mapping in imported_mappings:
                before_count = len(self.mapping_cards)
                self.add_mapping(mapping)
                if len(self.mapping_cards) != before_count + 1:
                    raise RuntimeError("映射编辑卡未能创建")
            for preset in imported_presets:
                before_count = len(self.preset_cards)
                self.add_preset(preset)
                if len(self.preset_cards) != before_count + 1:
                    raise RuntimeError("预设编辑卡未能创建")
            self._loading_checkpoint(force=True)
        except Exception as error:
            import_error = error
            try:
                self._rollback_editor_import_transaction(transaction)
            except Exception as rollback_error:
                rollback_complete = False
                import_error = RuntimeError(
                    f"{error}；回滚编辑表单时又发生：{rollback_error}"
                )
        finally:
            self._end_loading()
        if import_error is not None:
            recovery_text = (
                "已回滚本次导入，原编辑内容未改变。"
                if rollback_complete else
                "自动回滚未能完整结束，请检查当前编辑表单后再继续。"
            )
            QMessageBox.warning(
                self, "导入失败",
                f"配置表单未能完整创建，{recovery_text}\n\n"
                f"{import_error}",
            )
            return
        self.config_state = ConfigState.DIRTY
        self.reload_button.setEnabled(True)
        self.engine_hint.setText("配置内容已合并；请检查快捷键冲突后应用更改")
