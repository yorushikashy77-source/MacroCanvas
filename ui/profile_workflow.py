"""配置档案编辑、前台进程识别与运行档案切换流程。"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
try:
    import winsound
except ImportError:
    class _WinSoundFallback:
        SND_ALIAS = 0
        SND_ASYNC = 0

        @staticmethod
        def PlaySound(*_args, **_kwargs):
            return None

    winsound = _WinSoundFallback()

from PySide6.QtCore import QEvent, QTimer, Qt, Slot
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QLabel, QMessageBox, QSpinBox, QWidget,
)

from config.schema import (
    repair_duplicate_action_tree_ids, repair_duplicate_runtime_ids,
    repair_overlapping_loop_controls, validate_config_payload,
)
from config.storage import write_deduplicated_snapshot
from config.profiles import (
    BASE_LAYER_NAME, DISABLED_LAYER_NAME, normalize_profile,
    profile_payload, select_profile,
)
from core.constants import *
from engine.input_backend import POINT, InterceptionInputHook, InterceptionOutput, WinInput
from engine.window_context import (
    foreground_window_belongs_to_current_process,
    foreground_window_context,
)
from engine.trigger_resolver import MODIFIER_ORDER, combo_text, modifier_names
from macro.actions import clone_action_tree, iter_action_tree
from macro.recording import simplify_recorded_actions
from ui.profile_manager import ProfileManagerDialog
from ui.runtime_guards import (
    explain_runtime_cleanup_block, runtime_cleanup_blocks_new_output,
)


class ProfileWorkflowMixin:

    def _runtime_cleanup_blocks_new_output(self):
        return runtime_cleanup_blocks_new_output(self)

    def _explain_runtime_cleanup_block(self, context="runtime_trigger"):
        return explain_runtime_cleanup_block(self, context)

    def _begin_profile_form_loading(self, profile_name):
        if self.profile_form_loading:
            return
        self.profile_form_loading = True
        self._begin_loading(
            "正在切换配置方案",
            f"正在加载“{profile_name}”的映射、预设和动作……",
            host=self,
        )

    def _profile_loading_checkpoint(self, force=False):
        if self.profile_form_loading:
            self._loading_checkpoint(force=force)

    def _end_profile_form_loading(self):
        if not self.profile_form_loading:
            return
        self.profile_form_loading = False
        self._end_loading()

    def _current_profile_snapshot(self):
        """Return mappings and presets currently shown in the main editor."""
        return {
            "mappings": json.loads(json.dumps(self.collect_mappings())),
            "presets": json.loads(json.dumps(self.collect_presets())),
        }

    def _profile_record(self, profile_id, profiles=None):
        profile_id = str(profile_id or "")
        if not profile_id:
            return None
        for profile in self.profiles if profiles is None else profiles:
            if str(profile.get("id") or "") == profile_id:
                return profile
        return None

    def _visible_editor_profile_id(self):
        """Return the profile whose cards are currently shown in the editor.

        The empty string is the valid ID of the base scheme, so it must not be
        treated as a missing value and replaced with ``editor_profile_id``.
        ``None`` is reserved for the short startup window before any form has
        been loaded.
        """
        loaded_profile_id = getattr(self, "editor_loaded_profile_id", None)
        if loaded_profile_id is None:
            loaded_profile_id = getattr(self, "editor_profile_id", "")
        return str(loaded_profile_id or "")

    def _payload_for_profile_id(self, profile_id):
        """Return a detached editor payload for one scheme."""
        profile_id = str(profile_id or "")
        if not profile_id:
            return profile_payload({"payload": self.base_profile_payload})
        profile = self._profile_record(profile_id)
        if profile is None:
            return None
        return profile_payload(profile)

    def _store_editor_payload(self):
        """Write the visible cards back to the scheme currently being edited.

        This only updates the in-memory pending configuration. Persistence and
        runtime replacement still happen through “应用更改”.
        """
        if not hasattr(self, "mapping_cards") or not hasattr(self, "preset_cards"):
            return {"mappings": [], "presets": []}
        snapshot = self._current_profile_snapshot()
        # Store the form that is actually visible. The base scheme uses an
        # empty ID, so do not fall back to a possibly stale combo target.
        profile_id = self._visible_editor_profile_id()
        if profile_id:
            profile = self._profile_record(profile_id)
            if profile is None:
                # A profile may have been deleted in the manager while it was
                # open in the editor. Fall back to the base scheme instead of
                # silently discarding the visible cards.
                self.editor_profile_id = ""
                self.base_profile_payload = profile_payload({"payload": snapshot})
            else:
                profile["payload"] = profile_payload({"payload": snapshot})
        else:
            self.base_profile_payload = profile_payload({"payload": snapshot})
        return snapshot

    @staticmethod
    def _profile_payload_signature(payload):
        normalized = profile_payload({"payload": payload})
        return json.dumps(
            normalized, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )

    def _editor_has_pending_changes(self):
        if not hasattr(self, "mapping_cards") or not hasattr(self, "preset_cards"):
            return False
        current = self._current_profile_snapshot()
        return self._profile_payload_signature(current) != self._profile_payload_signature(
            self.editor_loaded_payload
        )

    def _restore_editor_baseline_to_model(self):
        """Discard current form edits and restore the payload loaded for this scheme."""
        baseline = profile_payload({"payload": self.editor_loaded_payload})
        profile_id = self._visible_editor_profile_id()
        if profile_id:
            profile = self._profile_record(profile_id)
            if profile is not None:
                profile["payload"] = baseline
        else:
            self.base_profile_payload = baseline

    def _clear_editor_cards(self):
        """Destroy the old editor widgets before another profile is rendered.

        ``deleteLater()`` alone can leave the removed cards visible until Qt
        returns from the combo-box popup event loop.  During that interval the
        selector already shows the new profile while the page still looks like
        the previous one.  Hide and detach every old widget immediately, then
        flush deferred deletes before constructing the next profile.
        """
        for card in list(self.mapping_cards):
            self.mapping_layout.removeWidget(card)
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.mapping_cards.clear()
        for card in list(self.preset_cards):
            dialog = getattr(card, "action_dialog", None)
            if dialog is not None:
                dialog.hide()
                dialog.setParent(None)
                dialog.deleteLater()
            self.preset_layout.removeWidget(card)
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.preset_cards.clear()
        self.selected_preset_card = None
        self.action_table = None
        self.action_title = None
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def _load_profile_payload_into_editor(self, payload, profile_id=None):
        """Replace visible mappings/presets only; never touch the live runtime."""
        payload = profile_payload({"payload": payload})
        mapping_count = len(payload.get("mappings", []) or [])
        preset_count = len(payload.get("presets", []) or [])
        action_count = sum(
            1
            for preset in payload.get("presets", []) or []
            for _ in iter_action_tree(preset.get("actions", []) or [])
        )
        self._set_loading_message(
            "正在切换配置方案",
            f"正在载入 {mapping_count} 个映射、{preset_count} 个预设和 "
            f"{action_count} 个动作项……",
        )
        previous_initializing = self.initializing
        self.initializing = True
        update_targets = [
            getattr(self, "mapping_scroll", None),
            getattr(self, "preset_scroll", None),
            getattr(self, "mapping_container", None),
            getattr(self, "preset_container", None),
        ]
        for target in update_targets:
            if target is not None:
                target.setUpdatesEnabled(False)
        try:
            self._clear_editor_cards()
            self._set_loading_message(
                "正在重建配置表单",
                f"正在创建 {mapping_count} 个映射和 {preset_count} 个预设……",
            )
            for mapping in payload.get("mappings", []):
                self.add_mapping(json.loads(json.dumps(mapping)))
            previous_defer = getattr(self, "defer_preset_action_rows", False)
            self.defer_preset_action_rows = True
            try:
                for preset in payload.get("presets", []):
                    copied = dict(preset)
                    copied["actions"] = preset.get("actions", []) or []
                    self.add_preset(copied)
            finally:
                self.defer_preset_action_rows = previous_defer
            # 空配置方案保持真正空白。添加映射和预设应由用户通过
            # 标题栏中的“添加映射 / 添加预设”按钮主动完成，不能在
            # 载入空档案时自动生成“未配置…”占位项目。
            if self.preset_cards:
                self.select_preset_card(self.preset_cards[0])
        finally:
            self.initializing = previous_initializing
            for target in update_targets:
                if target is not None:
                    target.setUpdatesEnabled(True)
                    target.update()
        self.refresh_mapping_filters()
        self.refresh_preset_filters()

        self.editor_loaded_profile_id = str(
            self.editor_profile_id if profile_id is None else profile_id or ""
        )
        # Keep the load path light.  Large recorded action trees are attached to
        # preset cards lazily, so do not immediately collect the whole editor
        # back into a fresh payload merely to establish a baseline.
        self.editor_loaded_payload = {
            "mappings": payload.get("mappings", []) or [],
            "presets": payload.get("presets", []) or [],
        }

    def _switch_editor_profile(self, profile_id, *, store_current=True):
        """Switch only the main-page form; runtime state is deliberately untouched.

        The switch is transactional at the editor level. If rebuilding the
        target form fails, restore the previously visible scheme instead of
        leaving only the combo-box text on the requested item.
        """
        profile_id = str(profile_id or "")
        previous_profile_id = self._visible_editor_profile_id()
        self._last_profile_switch_error = ""
        if store_current:
            self._store_editor_payload()

        payload = self._payload_for_profile_id(profile_id)
        if payload is None:
            return False
        previous_payload = self._payload_for_profile_id(previous_profile_id)
        try:
            self._load_profile_payload_into_editor(payload, profile_id)
        except Exception as error:
            restore_error = None
            if previous_payload is not None:
                try:
                    self._load_profile_payload_into_editor(
                        previous_payload, previous_profile_id
                    )
                except Exception as rollback_error:
                    restore_error = rollback_error
            self.editor_profile_id = previous_profile_id
            detail = str(error) or error.__class__.__name__
            if restore_error is not None:
                detail += (
                    "；恢复原配置表单时也发生错误："
                    f"{str(restore_error) or restore_error.__class__.__name__}"
                )
            self._last_profile_switch_error = detail
            return False

        self.editor_profile_id = profile_id
        return True

    def _reload_full_configuration_into_window(self, payload):
        """Reload a validated complete configuration without restarting the app."""
        repaired, _removed_loops = repair_overlapping_loop_controls(
            json.loads(json.dumps(payload, ensure_ascii=False))
        )
        repaired, _action_changes = repair_duplicate_action_tree_ids(repaired)
        repaired, _changes = repair_duplicate_runtime_ids(repaired)
        data = validate_config_payload(
            repaired
        )
        mapping_count = len(data.get("mappings", []) or [])
        preset_count = len(data.get("presets", []) or [])
        profile_count = len(data.get("profiles", []) or [])
        self._set_loading_message(
            "正在载入完整配置",
            f"正在重建 {mapping_count} 个基础映射、{preset_count} 个基础预设和 "
            f"{profile_count} 个配置档案……",
        )
        previous_initializing = self.initializing
        self.initializing = True
        update_targets = [
            getattr(self, "mapping_scroll", None),
            getattr(self, "preset_scroll", None),
            getattr(self, "mapping_container", None),
            getattr(self, "preset_container", None),
        ]
        for target in update_targets:
            if target is not None:
                target.setUpdatesEnabled(False)
        try:
            self._clear_editor_cards()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.load_config(data_override=data)
        finally:
            self.initializing = previous_initializing
            for target in update_targets:
                if target is not None:
                    target.setUpdatesEnabled(True)
                    target.update()
        self.refresh_mapping_filters()
        self.refresh_preset_filters()
        self.refresh_profile_selector()
        return self.current_config_payload()

    def _overwrite_full_configuration_in_place(self, payload):
        """Replace the complete config and reload the current process safely."""
        repaired, _removed_loops = repair_overlapping_loop_controls(
            json.loads(json.dumps(payload, ensure_ascii=False))
        )
        repaired, _action_changes = repair_duplicate_action_tree_ids(repaired)
        repaired, _changes = repair_duplicate_runtime_ids(repaired)
        imported = validate_config_payload(
            repaired
        )
        try:
            previous_raw = json.loads(CONFIG_PATH.read_text("utf-8"))
            previous_raw, _removed_loops = repair_overlapping_loop_controls(previous_raw)
            previous_raw, _action_changes = repair_duplicate_action_tree_ids(previous_raw)
            previous_raw, _changes = repair_duplicate_runtime_ids(previous_raw)
            previous_payload = validate_config_payload(previous_raw)
        except (OSError, ValueError, json.JSONDecodeError):
            previous_payload = validate_config_payload(
                json.loads(json.dumps(self.current_config_payload(), ensure_ascii=False))
            )

        was_running = bool(self.running)
        runtime_gate_state = {
            "mappings_enabled": bool(getattr(self, "mappings_enabled", True)),
            "profile_trigger_allowed": bool(
                getattr(self, "profile_trigger_allowed", True)
            ),
            "profile_input_temporarily_suspended": bool(
                getattr(self, "profile_input_temporarily_suspended", False)
            ),
            "profile_input_suspend_reason": str(
                getattr(self, "profile_input_suspend_reason", "") or ""
            ),
            "macrocanvas_foreground_suspended": bool(
                getattr(self, "macrocanvas_foreground_suspended", False)
            ),
            "foreground_candidate_input_suspended": bool(
                getattr(self, "foreground_candidate_input_suspended", False)
            ),
        }
        # From this point onward a failure may have already stopped the old
        # runtime, so restore both the previous payload and its running state.
        rollback_required = True
        try:
            if was_running:
                self._set_loading_message(
                    "正在停止旧配置",
                    "正在停止宏、释放输入并关闭旧的输入通道……",
                )
                stopped = self._set_running_impl(
                    False, allow_owned_mouse_force_release=True
                )
                if stopped is False or self.running:
                    raise RuntimeError(
                        "旧输入引擎未能安全停止，已取消覆盖完整配置。"
                    )
                if self.interception_input_hook is not None:
                    raise RuntimeError(
                        "旧的 Interception 输入线程仍在退出，暂不能覆盖完整配置。"
                    )
            else:
                remaining = self.stop_all_macros(play_sound=False)
                release_failures = list(getattr(
                    self, "last_macro_release_failures", []
                ))
                if remaining or release_failures:
                    detail = []
                    if remaining:
                        detail.append(f"仍有 {len(remaining)} 个宏任务正在退出")
                    detail.extend(release_failures)
                    raise RuntimeError(
                        "旧配置的运行状态未能安全清理，已取消覆盖完整配置："
                        + "、".join(detail)
                    )

            # Keep the configuration that existed before the overwrite as a
            # recoverable saved snapshot.  This does not rewrite the live file.
            previous_text = json.dumps(
                previous_payload, ensure_ascii=False, indent=2
            )
            write_deduplicated_snapshot(
                CONFIG_BACKUP_DIR,
                "saved",
                previous_text,
                limit=CONFIG_BACKUP_LIMIT,
                legacy_prefixes=("config",),
            )

            self._set_loading_message(
                "正在写入完整配置",
                "正在校验并原子替换配置文件……",
            )
            self._save_config_payload(imported, create_backup=True)
            normalized = self._reload_full_configuration_into_window(imported)

            self._set_loading_message(
                "正在生成运行配置",
                "正在重新构建配置档案、动作索引和输入后端配置……",
            )
            if not self.generate_kanata_config():
                raise ValueError("导入内容无法生成有效的运行配置")

            # Persist the migrated/normalized form rendered by the current
            # version so disk and UI use exactly the same payload.
            normalized = self.current_config_payload()
            self._save_config_payload(normalized, create_backup=True)
            self.applied_config_payload = json.loads(json.dumps(normalized))
            self.applied_config_signature = self.current_config_signature()
            self.applied_config_text = (
                KANATA_CONFIG_PATH.read_text("utf-8", errors="replace")
                if KANATA_CONFIG_PATH.exists() else ""
            )
            self._snapshot_runtime_config()
            self.reload_button.setEnabled(False)
            self.config_state = ConfigState.SAVED

            if was_running:
                self._set_loading_message(
                    "正在恢复输入引擎",
                    "正在使用导入后的配置重新建立输入通道……",
                )
                started = self._set_running_impl(True)
                if started is False or not self.running:
                    self.config_state = ConfigState.FAILED
                    self.refresh_status_ui()
                    QMessageBox.warning(
                        self,
                        "配置已导入，但输入引擎启动失败",
                        "完整配置已经在当前窗口中载入并保存，无需重启程序。"
                        "但输入引擎未能重新启动，请检查当前后端和驱动后手动启动。",
                    )
                    return True
                try:
                    self._restore_runtime_mapping_gate_after_apply(
                        runtime_gate_state
                    )
                except Exception as error:
                    self.config_state = ConfigState.FAILED
                    self.refresh_status_ui()
                    QMessageBox.warning(
                        self,
                        "配置已导入，但映射暂停状态恢复失败",
                        "完整配置已经在当前窗口中载入并保存，无需重启程序。"
                        f"但导入前的映射暂停状态未能恢复：{error}",
                    )
                    return True
            else:
                self.update_global_hook_for_backend()
                self.engine_hint.setStyleSheet("")
                self.engine_hint.setText(
                    "完整配置已导入并载入；启动输入引擎后确认运行"
                )

            rollback_required = False
            self.refresh_status_ui()
            return True
        except Exception as error:
            rollback_error = None
            if rollback_required:
                try:
                    self._set_loading_message(
                        "正在恢复原配置",
                        "导入失败，正在恢复覆盖前的配置和运行状态……",
                    )
                    self._save_config_payload(previous_payload, create_backup=False)
                    restored = self._reload_full_configuration_into_window(
                        previous_payload
                    )
                    if not self.generate_kanata_config():
                        raise RuntimeError("原配置恢复后无法重新生成运行配置")
                    self.applied_config_payload = json.loads(json.dumps(restored))
                    self.applied_config_signature = self.current_config_signature()
                    self.applied_config_text = (
                        KANATA_CONFIG_PATH.read_text("utf-8", errors="replace")
                        if KANATA_CONFIG_PATH.exists() else ""
                    )
                    self._snapshot_runtime_config()
                    self.config_state = ConfigState.SAVED
                    self.reload_button.setEnabled(False)
                    if was_running and not self.running:
                        restart_result = self._set_running_impl(True)
                        if restart_result is False or not self.running:
                            raise RuntimeError(
                                "原配置文件和界面已恢复，但输入引擎未能恢复运行"
                            )
                        self._restore_runtime_mapping_gate_after_apply(
                            runtime_gate_state
                        )
                    else:
                        self.update_global_hook_for_backend()
                except Exception as restore_error:
                    rollback_error = restore_error
                    self.config_state = ConfigState.FAILED
            detail = str(error)
            if rollback_error is not None:
                detail += f"\n\n原配置或原运行状态恢复失败：{rollback_error}"
            QMessageBox.warning(self, "完整配置导入失败", detail)
            self.refresh_status_ui()
            return False

    def _persist_profile_manager_settings(self):
        """Write the complete profile catalog and verify it can be read back."""
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            try:
                data = validate_config_payload(
                    json.loads(CONFIG_PATH.read_text("utf-8"))
                )
            except (OSError, ValueError, json.JSONDecodeError):
                data = json.loads(json.dumps(
                    self.applied_config_payload or self.current_config_payload()
                ))

            stored_profiles = [normalize_profile(item) for item in self.profiles]
            data["profiles"] = json.loads(json.dumps(stored_profiles))
            data["profile_auto_switch_enabled"] = bool(
                self.profile_auto_switch_enabled
            )
            valid_ids = {str(item.get("id") or "") for item in stored_profiles}
            data["active_profile_id"] = (
                str(data.get("active_profile_id") or "")
                if str(data.get("active_profile_id") or "") in valid_ids
                else ""
            )
            data["editor_profile_id"] = (
                str(self.editor_profile_id or "")
                if str(self.editor_profile_id or "") in valid_ids
                else ""
            )
            self._save_config_payload(data, create_backup=True)

            # Read-after-write verification prevents the dialog from closing on
            # a partial or stale save.
            verified = validate_config_payload(
                json.loads(CONFIG_PATH.read_text("utf-8"))
            )
            expected = json.dumps(
                stored_profiles, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            actual = json.dumps(
                [normalize_profile(item) for item in verified.get("profiles", [])],
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            if actual != expected:
                raise OSError("配置文件写入后校验不一致")
            return True
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(
                self,
                "档案设置保存失败",
                f"无法写入配置文件，窗口将保持打开：\n{error}",
            )
            return False

    def _commit_profile_manager_settings(self, profiles, auto_switch_enabled):
        """Stage manager contents; the main Apply action owns persistence."""
        self._store_editor_payload()
        latest_payloads = {
            str(item.get("id") or ""): profile_payload(item)
            for item in self.profiles
        }
        merged_profiles = []
        for item in profiles:
            merged = dict(item)
            profile_id = str(merged.get("id") or "")
            if profile_id in latest_payloads:
                merged["payload"] = latest_payloads[profile_id]
            merged_profiles.append(merged)
        self.profiles = [normalize_profile(item) for item in merged_profiles]
        self.profile_auto_switch_enabled = bool(auto_switch_enabled)
        profile_ids = {
            str(item.get("id") or "") for item in self.profiles
            if item.get("id")
        }
        editor_removed = bool(
            self.editor_profile_id and self.editor_profile_id not in profile_ids
        )
        if editor_removed:
            self.editor_profile_id = ""
        if editor_removed:
            self._load_profile_payload_into_editor(
                self.base_profile_payload, ""
            )
        self.refresh_profile_selector()
        self.data_changed()
        self.engine_hint.setStyleSheet("")
        self.engine_hint.setText(
            "档案修改已暂存到当前编辑内容，尚未写入配置；点击“应用更改”后保存并更新运行配置"
        )
        return True

    def open_profile_settings(self):
        if self.recording or getattr(self, "recording_session_active", False):
            QMessageBox.information(
                self, "正在录制", "请先完成或取消录制，再管理配置档案。"
            )
            return

        self._begin_loading(
            "正在打开档案管理",
            "正在读取档案列表、匹配规则和内容摘要……",
            host=self,
        )
        try:
            # Refresh the in-memory payload before the manager calculates content
            # summaries and virtual-key capacity.  This does not save or apply the
            # pending configuration.
            self._store_editor_payload()
            process_name, title = foreground_window_context()
            dialog = ProfileManagerDialog(
                self.profiles,
                self.base_profile_payload,
                self.profile_auto_switch_enabled,
                self.active_profile_id,
                (process_name, title),
                self,
                selected_profile_id=self.editor_profile_id,
                save_callback=self._commit_profile_manager_settings,
                status_overlay_callback=(
                    lambda title, detail, accent="#7dd3fc":
                    self.activity_overlay.show_message(title, detail, accent)
                ),
                status_overlay_hide_callback=self.activity_overlay.hide_message,
            )
        finally:
            self._end_loading()
        if not self._enter_settings_input_mode():
            dialog.deleteLater()
            return
        try:
            result = dialog.exec()
        finally:
            self._leave_settings_input_mode()
        if (
            result == QDialog.DialogCode.Accepted
            and dialog.requested_activate_id is not None
        ):
            self._switch_editor_profile(
                str(dialog.requested_activate_id or ""), store_current=True
            )

    def _profile_name(self, profile_id):
        if not profile_id:
            return "基础配置"
        for profile in self.profiles:
            if str(profile.get("id") or "") == str(profile_id):
                return str(profile.get("name") or "未命名档案")
        return "未知档案"

    def _manual_profile_switch_locked(self):
        """Only running macro tasks lock manual scheme selection.

        Loading indicators, configuration application and ordinary form work are
        visual progress states, not macro execution states.  They must not leave
        the scheme selector greyed out after the operation finishes.
        """
        return bool(
            self.macro_state != MacroState.IDLE
            or self.macro_controller.tasks
        )

    def _refresh_editor_profile_labels(self):
        """Keep page labels aligned with the form that is actually visible."""
        profile_id = self._visible_editor_profile_id()
        profile_name = self._profile_name(profile_id)
        is_base = not profile_id

        mapping_text = "基础映射" if is_base else f"{profile_name} · 映射"
        preset_text = "预设方案" if is_base else f"{profile_name} · 预设方案"
        mapping_title = getattr(self, "mapping_section_title", None)
        preset_title = getattr(self, "preset_section_title", None)
        if mapping_title is not None:
            mapping_title.setText(mapping_text)
        if preset_title is not None:
            preset_title.setText(preset_text)

        tabs = getattr(self, "tabs", None)
        if tabs is not None and tabs.count() >= 2:
            tabs.setTabText(0, "基础映射" if is_base else "映射")
            tabs.setTabText(1, "预设方案")
            tabs.setTabToolTip(0, f"正在编辑：{profile_name} · 映射")
            tabs.setTabToolTip(1, f"正在编辑：{profile_name} · 预设方案")

    def refresh_profile_selector(self):
        combo = getattr(self, "profile_selector_combo", None)
        if combo is None:
            self._refresh_editor_profile_labels()
            return
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("基础配置", "")
            for profile in self.profiles:
                profile_id = str(profile.get("id") or "")
                if not profile_id:
                    continue
                name = str(profile.get("name") or "未命名档案")
                if not profile.get("enabled", False):
                    name += "（停用，仅编辑）"
                combo.addItem(
                    name, profile_id
                )
            visible_profile_id = self._visible_editor_profile_id()
            target_index = combo.findData(visible_profile_id)
            self._set_profile_selector_index(
                combo, target_index if target_index >= 0 else 0
            )
        finally:
            combo.blockSignals(False)
        self._refresh_editor_profile_labels()
        self.refresh_profile_selector_state()

    @staticmethod
    def _set_profile_selector_index(combo, index):
        combo.setCurrentIndex(index)
        try:
            model_index = combo.model().index(index, combo.modelColumn())
            combo.view().setCurrentIndex(model_index)
        except Exception:
            pass

    def _sync_profile_selector_to_visible(self):
        """Align the existing combo index with the form that is actually shown.

        Ordinary profile selection must not clear/rebuild the combo model while
        its popup is closing.  Rebuilding at that point can let Qt repaint the
        clicked text after the code has rejected or rolled back the switch,
        producing a name-only switch.  The profile list itself is unchanged, so
        an in-place index sync is sufficient.
        """
        combo = getattr(self, "profile_selector_combo", None)
        if combo is None:
            self._refresh_editor_profile_labels()
            return
        visible_profile_id = self._visible_editor_profile_id()
        target_index = combo.findData(visible_profile_id)
        if target_index < 0:
            # The list really changed (for example a profile was removed or
            # disabled in the manager); this is one of the few cases that needs
            # a full model rebuild.
            self.refresh_profile_selector()
            return
        combo.blockSignals(True)
        try:
            self._set_profile_selector_index(combo, target_index)
        finally:
            combo.blockSignals(False)
        self._refresh_editor_profile_labels()
        self.refresh_profile_selector_state()

    def refresh_profile_selector_state(self):
        combo = getattr(self, "profile_selector_combo", None)
        if combo is None:
            return
        locked = self._manual_profile_switch_locked()
        combo.setEnabled(not locked)
        if locked:
            combo.setToolTip("宏任务或录制正在进行，停止后才能切换正在编辑的档案")
        elif self.config_state == ConfigState.DIRTY:
            combo.setToolTip(
                "切换前会确认未应用修改；各档案的编辑内容会分别保存"
            )
        else:
            combo.setToolTip("只切换主界面正在编辑的配置方案，不改变当前运行档案")

    def _confirm_pending_changes_before_profile_switch(self, target_id):
        """Return keep/discard/cancel without applying or touching the runtime."""
        pending = self._editor_has_pending_changes()
        if not pending:
            return "keep"

        # Keep the switch confirmation, Apply button and status text driven by
        # the same dirty calculation.  If a structural action edit reached
        # this fresh comparison before its queued UI update, reconcile the UI
        # first; a warning must never appear while Apply still looks disabled.
        if self.config_state != ConfigState.DIRTY:
            self.data_changed()
        if (
            self.config_state != ConfigState.DIRTY
            or not self._editor_has_pending_changes()
        ):
            return "keep"

        target_name = self._profile_name(target_id)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("当前表单尚未保存")
        box.setText("当前配置方案的表单内容已经修改。")
        box.setInformativeText(
            f"切换到“{target_name}”前，是否暂存当前修改？"
        )
        keep_button = box.addButton(
            "暂存修改并切换", QMessageBox.ButtonRole.AcceptRole
        )
        discard_button = box.addButton(
            "放弃修改并切换", QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_button = box.addButton(
            "取消", QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(keep_button)
        self.profile_switch_confirmation_active = True
        try:
            box.exec()
        finally:
            self.profile_switch_confirmation_active = False
        clicked = box.clickedButton()
        if clicked is keep_button:
            return "keep"
        if clicked is discard_button:
            return "discard"
        return "cancel"

    @Slot(int)
    def on_main_profile_index_changed(self, index):
        """Queue the profile represented by the combo's committed index.

        The native Windows combo popup does not guarantee that every visual
        index commit also emits ``activated``.  ``currentIndexChanged`` covers
        both the normal row-click path and the close/commit path seen in the
        recorded failure.  Programmatic selector synchronization blocks signals,
        so this slot still represents an external/user-visible index change.
        """
        combo = getattr(self, "profile_selector_combo", None)
        if combo is None or index < 0:
            return
        target_id = str(combo.itemData(index) or "")
        generation = int(
            getattr(self, "_profile_selector_change_generation", 0)
        ) + 1
        self._profile_selector_change_generation = generation
        QTimer.singleShot(
            0,
            lambda generation=generation, target_id=target_id:
            self._apply_requested_profile_selection(generation, target_id),
        )

    def _apply_requested_profile_selection(self, generation, target_id):
        if generation != getattr(
            self, "_profile_selector_change_generation", 0
        ):
            return
        combo = getattr(self, "profile_selector_combo", None)
        if combo is None:
            return
        target_id = str(target_id or "")
        index = combo.findData(target_id)
        if index < 0:
            self._sync_profile_selector_to_visible()
            return
        # Do not use combo.currentData() to validate this queued request.  The
        # native popup can transiently restore its previous index while it is
        # closing, then paint the committed row afterward.  The generation is
        # the authoritative ordering token; a genuinely newer user request has
        # already incremented it and was rejected above.
        try:
            self.on_main_profile_selected(index, target_id=target_id)
        finally:
            QTimer.singleShot(
                0,
                lambda generation=generation:
                self._finalize_main_profile_selection(generation),
            )
            # Windows can finish the native popup's close/repaint after the
            # first zero-delay callback.  A short second pass is harmless and
            # is invalidated automatically if another selection has arrived.
            QTimer.singleShot(
                50,
                lambda generation=generation:
                self._finalize_main_profile_selection(generation),
            )

    def _finalize_main_profile_selection(self, generation):
        if generation != getattr(
            self, "_profile_selector_change_generation", 0
        ):
            return
        combo = getattr(self, "profile_selector_combo", None)
        if combo is None:
            return
        try:
            combo.count()
        except RuntimeError:
            # A delayed native-popup repaint may outlive window teardown.
            return
        self._sync_profile_selector_to_visible()

    # Route keyboard activation and repeat-selection through the same transaction.
    @Slot(int)
    def on_main_profile_activated(self, index):
        self.on_main_profile_index_changed(index)

    def on_main_profile_view_clicked(self, model_index):
        """Capture the popup row directly for reliable native mouse selection."""
        if model_index is None or not model_index.isValid():
            return
        combo = getattr(self, "profile_selector_combo", None)
        if combo is None:
            return
        # Finish the popup transaction before form rebuilding starts.  Loading a
        # profile pumps the event loop; doing that while QComboBox is still in
        # its mouse-release/hide path can leave the popup visible intermittently.
        combo.hidePopup()
        self.on_main_profile_index_changed(model_index.row())

    def on_main_profile_selected(self, index, target_id=None):
        combo = getattr(self, "profile_selector_combo", None)
        if combo is None or index < 0:
            return
        requested_id = str(combo.itemData(index) or "")
        target_id = requested_id if target_id is None else str(target_id or "")
        if requested_id != target_id:
            self._sync_profile_selector_to_visible()
            return
        visible_profile_id = self._visible_editor_profile_id()
        if target_id == visible_profile_id:
            self.editor_profile_id = visible_profile_id
            self._sync_profile_selector_to_visible()
            return
        # Only another editor-form rebuild can make this selection re-entrant.
        # ``profile_switch_in_progress`` belongs to the independently applied
        # runtime/Kanata profile and must not cancel an editor-only selection.
        # Runtime foreground switching can overlap this UI event briefly; the
        # editor reads/writes its pending profile payloads, while the runtime uses
        # the applied snapshot in ``runtime_profile_catalog``.
        if self.profile_form_loading:
            self._sync_profile_selector_to_visible()
            return
        if self._manual_profile_switch_locked():
            self._sync_profile_selector_to_visible()
            return

        decision = self._confirm_pending_changes_before_profile_switch(target_id)
        if decision == "cancel":
            self._sync_profile_selector_to_visible()
            return
        if decision == "discard":
            discarded_profile_id = self._visible_editor_profile_id()
            self._restore_editor_baseline_to_model()
            restart_discarded_mapping = self._restore_discarded_mapping_deletions(
                discarded_profile_id
            )
            restored_discarded_preset = self._restore_discarded_preset_deletions(
                discarded_profile_id
            )
            store_current = False
        else:
            restart_discarded_mapping = False
            restored_discarded_preset = False
            self._store_editor_payload()
            store_current = False

        target_name = self._profile_name(target_id)
        self._begin_profile_form_loading(target_name)
        try:
            switched = self._switch_editor_profile(
                target_id, store_current=store_current
            )
        finally:
            self._end_profile_form_loading()
        if not switched:
            detail = str(getattr(self, "_last_profile_switch_error", "") or "")
            if detail:
                QMessageBox.warning(
                    self,
                    "配置方案切换失败",
                    "目标配置方案未能完成载入，已恢复切换前的表单。\n\n"
                    f"错误信息：{detail}",
                )
            else:
                QMessageBox.warning(
                    self, "配置方案不存在", "目标配置方案已被删除或无法读取。"
                )
            self._sync_profile_selector_to_visible()
            return

        # This selector is an editor-only operation. It never switches Kanata
        # layers, stops macros, releases inputs, or changes active_profile_id.
        self.engine_hint.setStyleSheet("")
        self.engine_hint.setText(
            f"已载入“{self._profile_name(target_id)}”的配置表单"
        )
        self._sync_profile_selector_to_visible()
        if restart_discarded_mapping:
            self.engine_hint.setStyleSheet("color: #fbbf24;")
            self.engine_hint.setText(
                "已恢复放弃删除的直接映射；请重新启动输入引擎"
            )
        elif restored_discarded_preset:
            self.engine_hint.setStyleSheet("")
            self.engine_hint.setText("已恢复放弃删除的预设及其运行触发")
        # Ensure the rebuilt page is painted after the combo popup closes.  The
        # data switch is already complete here; this only prevents stale card
        # pixels from the previous profile from surviving one event cycle.
        for target in (
            getattr(self, "mapping_container", None),
            getattr(self, "preset_container", None),
            getattr(self, "mapping_scroll", None),
            getattr(self, "preset_scroll", None),
        ):
            if target is not None:
                target.update()
        QTimer.singleShot(0, self._refresh_editor_profile_labels)

    def _runtime_profile_entry(self, profile_id):
        return self.runtime_profile_catalog.get(str(profile_id or ""))

    def _install_runtime_profile_entry(self, entry):
        if entry is None:
            return False
        with self.data_lock:
            self.runtime_mappings = [
                dict(item) for item in entry.get("mappings", [])
            ]
            self.runtime_presets = []
            self.runtime_trigger_rules = []
            for mapping in self.runtime_mappings:
                rule = dict(mapping)
                rule["_runtime_kind"] = "mapping"
                self.runtime_trigger_rules.append(rule)
            for preset in entry.get("presets", []):
                copied = dict(preset)
                copied["actions"] = clone_action_tree(
                    preset.get("actions", [])
                )
                self.runtime_presets.append(copied)
            runtime_library = {
                str(preset.get("id")): preset
                for preset in self.runtime_presets if preset.get("id")
            }
            for preset in self.runtime_presets:
                preset["_preset_library"] = runtime_library
                self.runtime_trigger_rules.append(
                    self._preset_as_mapping_rule(preset)
                )
        return True


    def _clear_profile_transition_state(self, release_outputs=True):
        """Reset runtime trigger bookkeeping before a direct profile change."""
        release_failures = []
        if release_outputs:
            rules = self._runtime_mapping_rules()
            targets, virtual_keys = self._remember_runtime_release_state(rules)
            sync_released = self._release_all_sync_mappings()
            if not sync_released:
                release_failures.append("同步映射输出")
            virtual_released = self._release_runtime_virtual_keys(
                names=virtual_keys,
                rules=rules,
                include_history=False,
            )
            if not virtual_released:
                release_failures.append("Kanata 虚拟键")
            # Compatibility fallback for a generated configuration from an older
            # program version, where synchronous outputs were not routed through a
            # releasable fake key.  Avoid untracked mouse Up packets during an
            # automatic foreground switch; owned mouse outputs are handled above by
            # the backend and its quarantine mechanism.
            fallback_released = self._failsafe_release_runtime_targets(
                force_all=False,
                names=targets,
                include_history=False,
                allow_mouse_targets=False,
            )
            if not fallback_released:
                release_failures.append("系统级兜底释放")
            if release_failures:
                remembered = list(getattr(self, "last_macro_release_failures", []) or [])
                for item in release_failures:
                    if item not in remembered:
                        remembered.append(item)
                self.last_macro_release_failures = remembered
                self.output_shutdown_in_progress = True
                self.profile_trigger_allowed = False
                if getattr(self, "_macro_stop_gate_restore", None) is not None:
                    self._macro_stop_gate_restore = None
            self.write_diagnostic(
                "profile_transition_outputs_released",
                force=True,
                targets=targets,
                virtual_keys=virtual_keys,
                sync_released=sync_released,
                virtual_released=virtual_released,
                fallback_released=fallback_released,
                release_failures=list(release_failures),
            )
        with self.input_state_lock:
            self.physical_modifiers.clear()
            if hasattr(self, "physical_input_sources"):
                self.physical_input_sources = {
                    source_id: name
                    for source_id, name in self.physical_input_sources.items()
                    if name not in MODIFIER_ORDER
                }
                self._refresh_logical_physical_sets_locked()
            self.held_trigger_ids.clear()
            self.kanata_trigger_down.clear()
        with self.expected_kanata_event_lock:
            self.expected_kanata_events.clear()
        # Do not clear suppressed_trigger_names here. A physical Down may have
        # been consumed by the old profile while its Up arrives after the
        # foreground switch. Keeping the edge ownership until Up prevents the
        # two halves of one click/key press from taking different routes.
        clear_hotkey_latches = getattr(
            self, "_clear_system_hotkey_latches", None
        )
        if callable(clear_hotkey_latches):
            clear_hotkey_latches()
        else:
            self.system_hotkey_latched.clear()
            getattr(self, "system_hotkey_latched_sources", {}).clear()
        return not release_failures

    def _change_runtime_profile_layer(self, layer, *, wait=True):
        """Change every active Kanata input source to one runtime layer."""
        if (
            not self.running
            or self._runtime_is_game_mode()
            or not self.mappings_enabled
        ):
            return True
        ok = True
        if self.engine.is_running():
            ok = bool(self.engine.change_layer(
                layer, wait=wait, timeout=1.0
            )) and ok
        if self.keyboard_engine.is_running():
            ok = bool(self.keyboard_engine.change_layer(
                layer, wait=wait, timeout=1.0
            )) and ok
        return ok

    def _defer_profile_input_restore(
        self, *, layer=None, profile_trigger_allowed=True, reason=""
    ):
        """Restore a disabled runtime layer only after timed-out tasks exit."""
        self._deferred_profile_input_restore = {
            "layer": str(
                layer
                or (
                    self.active_profile_layer
                    if self.mappings_enabled else DISABLED_LAYER_NAME
                )
            ),
            "profile_trigger_allowed": bool(profile_trigger_allowed),
            "reason": str(reason or "macro_stop_completed"),
        }

    def _apply_deferred_profile_input_restore(self):
        pending = getattr(self, "_deferred_profile_input_restore", None)
        if not pending:
            return True
        with self.macro_controller.lock:
            if any(
                task.has_live_threads() and task.stop_event.is_set()
                for task in self.macro_controller.tasks.values()
            ):
                return False
        if self._shutdown_started or not self.running:
            self._deferred_profile_input_restore = None
            self._discard_profile_suspended_macros(
                reason="deferred_restore_runtime_unavailable"
            )
            return False
        if (
            self.settings_input_mode_active
            or getattr(self, "recording_session_active", False)
        ):
            return False
        layer = str(pending.get("layer") or DISABLED_LAYER_NAME)
        if not self._change_runtime_profile_layer(layer, wait=True):
            self.profile_trigger_allowed = False
            self.engine_hint.setStyleSheet("color: #ff8496;")
            self.engine_hint.setText(
                self.engine.last_command_error
                or self.keyboard_engine.last_command_error
                or "宏任务退出后无法恢复映射层"
            )
            return False
        self.profile_trigger_allowed = bool(
            pending.get("profile_trigger_allowed", True)
        )
        self._deferred_profile_input_restore = None
        if self.profile_trigger_allowed:
            resumed = self._resume_profile_suspended_macros(
                reason=pending.get("reason", "deferred_restore")
            )
            if resumed is False:
                return False
        else:
            self._discard_profile_suspended_macros(
                reason=pending.get("reason", "deferred_restore_disabled")
            )
        self.write_diagnostic(
            "deferred_profile_input_restored",
            reason=pending.get("reason", ""),
            layer=layer,
            active_profile_id=self.active_profile_id,
        )
        return True

    def _suspend_active_profile_input(
        self, *, layer=DISABLED_LAYER_NAME, mark_transition=False, reason=""
    ):
        """Temporarily isolate physical triggers without terminating live macros.

        Only tasks that were running at the moment of suspension are remembered
        and resumed later.  Tasks already paused by the user stay paused.
        """
        del mark_transition
        previous_gate = self.output_shutdown_in_progress
        previous_allowed = self.profile_trigger_allowed
        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        dispatch_lock = getattr(self, "output_dispatch_lock", None)
        if dispatch_lock is not None:
            with dispatch_lock:
                pass

        layer_ok = self._change_runtime_profile_layer(layer, wait=True)
        if not layer_ok:
            error = (
                self.engine.last_command_error
                or self.keyboard_engine.last_command_error
                or f"无法切换到 {layer} 层"
            )
            if str(reason or "") == "macrocanvas_foreground":
                # MacroCanvas 自身窗口获得焦点时，旧映射层必须被视为不安全。
                # 如果 Kanata 没有确认进入 disabled layer，就不能恢复触发门；
                # 否则用户在主界面编辑时可能仍然处于旧运行层。
                self.output_shutdown_in_progress = True
                self.profile_trigger_allowed = False
                self.profile_input_temporarily_suspended = True
                self.profile_input_suspend_reason = "macrocanvas_foreground_failed"
                self.macrocanvas_foreground_suspended = False
                self.macrocanvas_foreground_suspend_failed = True
                if hasattr(self, "engine_state"):
                    self.engine_state = EngineState.FAILED
                if hasattr(self, "engine_hint"):
                    self.engine_hint.setStyleSheet("color: #ff8496;")
                    self.engine_hint.setText(
                        "MacroCanvas 前台隔离失败；已禁止新的 Python 侧触发，"
                        "请停止输入引擎或强制释放键鼠后再继续编辑"
                    )
                if hasattr(self, "toggle_button"):
                    self.toggle_button.setText("重试停止输入引擎")
                    self.toggle_button.setObjectName("stop")
                self.write_diagnostic(
                    "profile_input_suspend_failed",
                    force=True,
                    reason=reason,
                    layer=layer,
                    active_profile_id=self.active_profile_id,
                    fail_closed=True,
                    error=error,
                )
                if hasattr(self, "refresh_status_ui"):
                    self.refresh_status_ui()
                if hasattr(self, "refresh_macro_controls"):
                    self.refresh_macro_controls()
                return False
            self.output_shutdown_in_progress = previous_gate
            self.profile_trigger_allowed = previous_allowed
            self.write_diagnostic(
                "profile_input_suspend_failed",
                force=True,
                reason=reason,
                layer=layer,
                active_profile_id=self.active_profile_id,
                fail_closed=False,
                error=error,
            )
            return False

        paused_ids = []
        pause_failed_ids = []
        with self.macro_controller.lock:
            tasks = list(self.macro_controller.tasks.items())
        for preset_id, task in tasks:
            if not task.has_live_threads() or not task.run_event.is_set():
                continue
            if task.pause() is not False:
                paused_ids.append(str(preset_id))
            else:
                pause_failed_ids.append(str(preset_id))
        self._profile_input_paused_macro_ids.update(paused_ids)
        self.profile_input_temporarily_suspended = True
        self.profile_input_suspend_reason = str(reason or "temporary_input_suspend")
        transition_result = self._clear_profile_transition_state(
            release_outputs=True
        )
        transition_released = transition_result is not False
        if transition_released and not pause_failed_ids:
            self.output_shutdown_in_progress = previous_gate
        else:
            self.output_shutdown_in_progress = True
            self.profile_trigger_allowed = False
        if not transition_released:
            transition_failures = list(getattr(
                self, "last_macro_release_failures", []
            ))
            if hasattr(self, "_show_macro_cleanup_failure"):
                self._show_macro_cleanup_failure(
                    "输入已隔离，但旧输出释放失败", transition_failures
                )
            elif hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText("输入已隔离，但旧输出释放失败")
        if pause_failed_ids:
            if hasattr(self, "_show_macro_cleanup_failure"):
                self._show_macro_cleanup_failure(
                    "输入已隔离，但部分宏暂停释放失败",
                    pause_failed_ids,
                )
            elif hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText("输入已隔离，但部分宏暂停释放失败")
        elif paused_ids:
            self.macro_state = MacroState.PAUSED
            self.macro_status_detail = "输入暂时隔离，宏将在恢复后继续"
            if hasattr(self, "execution_info"):
                self.execution_info.setText(
                    "输入暂时隔离；当前宏已安全暂停并释放按住输出"
                )
        self.write_diagnostic(
            "profile_input_suspended",
            reason=reason,
            layer=layer,
            active_profile_id=self.active_profile_id,
            layer_ok=True,
            paused_macro_ids=paused_ids,
            pause_failed_macro_ids=pause_failed_ids,
        )
        if transition_released and not pause_failed_ids and hasattr(self, "engine_hint"):
            if str(reason or "") == "macrocanvas_foreground":
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "MacroCanvas 位于前台，映射已临时隔离；切换到目标程序后自动恢复"
                )
            elif str(reason or "") == "foreground_candidate_detected":
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "检测到前台程序变化，正在确认目标档案，映射已临时隔离"
                )
        if hasattr(self, "runtime_profile_status"):
            self.refresh_status_ui()
        if hasattr(self, "refresh_macro_controls"):
            self.refresh_macro_controls()
        return bool(transition_released and not pause_failed_ids)

    def _resume_profile_suspended_macros(self, *, reason=""):
        paused_ids = set(getattr(self, "_profile_input_paused_macro_ids", set()))
        resumed_ids = []
        failed_ids = []
        with self.macro_controller.lock:
            tasks = {
                str(preset_id): task
                for preset_id, task in self.macro_controller.tasks.items()
            }
        for preset_id in paused_ids:
            task = tasks.get(preset_id)
            if (
                task is None
                or not task.has_live_threads()
                or task.stop_event.is_set()
                or task.run_event.is_set()
            ):
                continue
            if task.resume() is not False:
                resumed_ids.append(preset_id)
            else:
                failed_ids.append(preset_id)

        if failed_ids:
            # Keep only the tasks that still need manual recovery under the
            # temporary-suspension owner set.  Do not re-enable the source layer
            # or clear the suspended flag until every resume has succeeded.
            self._profile_input_paused_macro_ids = set(failed_ids)
            self.profile_input_temporarily_suspended = True
            self.profile_input_suspend_reason = reason or "resume_failed"
            self.profile_trigger_allowed = False
            if hasattr(self, "_remember_macro_cleanup_failure"):
                self._remember_macro_cleanup_failure(
                    "输入层恢复失败，部分宏未能重新取得输出", failed_ids
                )
            else:
                remembered = list(getattr(self, "last_macro_release_failures", []) or [])
                for item in failed_ids:
                    if item not in remembered:
                        remembered.append(item)
                self.last_macro_release_failures = remembered
                self.output_shutdown_in_progress = True
                self.macro_state = MacroState.STOP_TIMEOUT
                self.macro_status_detail = "输入层恢复失败，部分宏未能重新取得输出"
            if hasattr(self, "_show_macro_cleanup_failure"):
                self._show_macro_cleanup_failure(
                    "输入层恢复失败，部分宏未能重新取得输出",
                    failed_ids,
                )
            elif hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText("输入层恢复失败，部分宏未能重新取得输出")
        else:
            self._profile_input_paused_macro_ids.clear()
            self.profile_input_temporarily_suspended = False
            self.profile_input_suspend_reason = ""
            self.macro_status_detail = ""
            if tasks:
                self.macro_state = (
                    MacroState.RUNNING
                    if any(task.run_event.is_set() for task in tasks.values())
                    else MacroState.PAUSED
                )
            elif self.macro_state == MacroState.PAUSED:
                self.macro_state = MacroState.IDLE
        self.write_diagnostic(
            "profile_suspended_macros_resumed",
            reason=reason,
            resumed_macro_ids=resumed_ids,
            failed_macro_ids=failed_ids,
            skipped_macro_ids=sorted(paused_ids - set(resumed_ids) - set(failed_ids)),
        )
        if hasattr(self, "refresh_status_ui"):
            self.refresh_status_ui()
        if hasattr(self, "refresh_macro_controls"):
            self.refresh_macro_controls()
        return not failed_ids

    def _discard_profile_suspended_macros(self, *, reason=""):
        paused_ids = sorted(getattr(self, "_profile_input_paused_macro_ids", set()))
        self._profile_input_paused_macro_ids.clear()
        self.profile_input_temporarily_suspended = False
        self.profile_input_suspend_reason = ""
        if paused_ids:
            self.write_diagnostic(
                "profile_suspended_macros_discarded",
                reason=reason,
                macro_ids=paused_ids,
            )

    def _restore_active_profile_input(self, *, reason=""):
        """Restore the selected runtime layer immediately, without a guard wait."""
        if self._shutdown_started or not self.running:
            return False
        if (
            self.settings_input_mode_active
            or getattr(self, "recording_session_active", False)
        ):
            return False
        if self._runtime_cleanup_blocks_new_output():
            self._explain_runtime_cleanup_block(f"restore_profile_input:{reason}")
            self.profile_trigger_allowed = False
            return False
        layer = (
            self.active_profile_layer
            if self.mappings_enabled else DISABLED_LAYER_NAME
        )
        with self.macro_controller.lock:
            tasks_still_exiting = any(
                task.has_live_threads() and task.stop_event.is_set()
                for task in self.macro_controller.tasks.values()
            )
        if tasks_still_exiting:
            self._defer_profile_input_restore(
                layer=layer,
                profile_trigger_allowed=True,
                reason=reason,
            )
            return False
        self._deferred_profile_input_restore = None
        if not self._change_runtime_profile_layer(layer, wait=True):
            self.profile_trigger_allowed = False
            self.engine_hint.setStyleSheet("color: #ff8496;")
            self.engine_hint.setText(
                self.engine.last_command_error
                or self.keyboard_engine.last_command_error
                or "恢复当前运行档案失败"
            )
            return False
        self.profile_trigger_allowed = True
        if self._resume_profile_suspended_macros(reason=reason) is False:
            self.profile_trigger_allowed = False
            self._change_runtime_profile_layer(DISABLED_LAYER_NAME, wait=False)
            return False
        self.write_diagnostic(
            "profile_input_restored",
            reason=reason,
            layer=layer,
            active_profile_id=self.active_profile_id,
        )
        if hasattr(self, "runtime_profile_status"):
            self.refresh_status_ui()
        return True

    def _enter_settings_input_mode(self):
        """Prevent mappings from firing while a modal settings editor has focus."""
        cancel_countdown = getattr(self, "_cancel_manual_test_countdown", None)
        if callable(cancel_countdown):
            cancel_countdown("已打开设置，原测试倒计时已取消")
        self.settings_dialog_active = True
        self.settings_input_mode_active = True
        suspended = self._suspend_active_profile_input(
            layer=DISABLED_LAYER_NAME,
            reason="settings_dialog_opened",
        )
        if suspended:
            return True
        if self.running:
            # A settings window must never open while the old direct Kanata
            # source layer is still active.  If the layer command itself fails,
            # fall back to the complete stop path instead of silently allowing
            # mappings behind the modal editor.
            stopped = self._set_running_impl(
                False, allow_owned_mouse_force_release=True
            )
            if stopped is not False and not self.running:
                return True

        # Isolation and the complete-stop fallback both failed.  Do not leave
        # settings mode latched and do not let the caller open a modal editor
        # over a still-active mapping layer.  Runtime stop code owns its safety
        # gates, so only roll back the settings-window flags here.
        self.settings_dialog_active = False
        self.settings_input_mode_active = False
        self.write_diagnostic(
            "settings_dialog_open_aborted",
            force=True,
            running=bool(self.running),
            error=(
                self.engine.last_command_error
                or self.keyboard_engine.last_command_error
            ),
        )
        return False

    def _leave_settings_input_mode(self):
        self.settings_dialog_active = False
        self.settings_input_mode_active = False
        if not self.running:
            self.profile_trigger_allowed = True
            self._discard_profile_suspended_macros(reason="settings_closed_engine_stopped")
            self.refresh_status_ui()
            return True
        if foreground_window_belongs_to_current_process():
            # 设置窗口关闭后主程序仍在前台，继续保持输入隔离。等用户回到
            # 外部窗口后，由前台档案检测统一恢复或切换运行档案。
            self.macrocanvas_foreground_suspended = True
            self.profile_input_temporarily_suspended = True
            self.profile_input_suspend_reason = "macrocanvas_foreground"
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "设置已关闭；MacroCanvas 仍在前台，映射保持隔离，切换到目标程序后自动恢复"
                )
            self.refresh_status_ui()
            return True
        if (
            self.runtime_profile_auto_switch_enabled
            and not foreground_window_belongs_to_current_process()
        ):
            target_id, _process_name, _title = self._foreground_profile_id()
            if self._activate_profile_by_id(
                target_id, reason="settings_closed_foreground", immediate=True
            ):
                return True
        # 关闭模态设置窗口后，前台通常仍是 MacroCanvas 本身。此时不能
        # 把“未匹配到外部程序”解释为切换到基础档案，只恢复原运行档案。
        restored = self._restore_active_profile_input(reason="settings_closed")
        if restored is False:
            self.profile_trigger_allowed = False
            self.output_shutdown_in_progress = True
            if hasattr(self, "_show_macro_cleanup_failure"):
                self._show_macro_cleanup_failure(
                    "设置窗口关闭后输入层恢复失败",
                    self._explain_runtime_cleanup_block("settings_closed_restore_failed"),
                )
            self.refresh_status_ui()
            return False
        return True

    def _activate_profile_by_id(self, profile_id, reason="auto", immediate=False):
        """Activate one applied profile after quiescing the old source layer."""
        del immediate
        profile_id = str(profile_id or "")
        if self.profile_switch_in_progress:
            return False

        entry = self._runtime_profile_entry(profile_id)
        if entry is None:
            if profile_id:
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(
                    f"档案“{self._profile_name(profile_id)}”尚未应用或未启用"
                )
            return False

        target_layer = str(entry.get("layer") or BASE_LAYER_NAME)
        if self._runtime_cleanup_blocks_new_output():
            self._explain_runtime_cleanup_block(f"activate_profile:{reason}")
            self.profile_trigger_allowed = False
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(
                    "按键释放尚未完成，请先执行“强制释放键鼠”后再切换或恢复档案"
                )
            self.refresh_status_ui()
            return False
        same_profile = profile_id == self.active_profile_id
        if same_profile:
            self.active_profile_layer = target_layer
            if self.running and not self.settings_input_mode_active and not getattr(
                self, "recording_session_active", False
            ):
                if not self._change_runtime_profile_layer(
                    target_layer if self.mappings_enabled else DISABLED_LAYER_NAME,
                    wait=True,
                ):
                    self.profile_trigger_allowed = False
                    self.engine_hint.setStyleSheet("color: #ff8496;")
                    self.engine_hint.setText(
                        self.engine.last_command_error
                        or self.keyboard_engine.last_command_error
                        or f"切换到档案层 {target_layer} 失败"
                    )
                    return False
                self.profile_trigger_allowed = True
                self._clear_process_guard_input_state()
                resume_ok = self._resume_profile_suspended_macros(
                    reason=f"profile_reactivated:{reason}"
                )
                if resume_ok is False:
                    self.profile_trigger_allowed = False
                    self.output_shutdown_in_progress = True
                    if not self._runtime_is_game_mode():
                        self._change_runtime_profile_layer(
                            DISABLED_LAYER_NAME, wait=False
                        )
                    self.refresh_status_ui()
                    return False
            self.refresh_status_ui()
            return True

        previous_profile_id = str(self.active_profile_id or "")
        previous_entry = self._runtime_profile_entry(previous_profile_id)
        previous_layer = self.active_profile_layer
        previous_allowed = self.profile_trigger_allowed
        previous_gate = self.output_shutdown_in_progress
        self.profile_switch_in_progress = True
        self.profile_trigger_allowed = False
        self.output_shutdown_in_progress = True
        success = False
        timed_out_tasks = []
        cleanup_failures = []
        try:
            dispatch_lock = getattr(self, "output_dispatch_lock", None)
            if dispatch_lock is not None:
                with dispatch_lock:
                    pass

            # Disable the old physical source layer first. Releasing while the
            # old layer is still active lets a held source immediately recreate
            # the output between cleanup and the layer change.
            if (
                self.running
                and not self._runtime_is_game_mode()
                and self.mappings_enabled
                and not self._change_runtime_profile_layer(
                    DISABLED_LAYER_NAME, wait=True
                )
            ):
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(
                    self.engine.last_command_error
                    or self.keyboard_engine.last_command_error
                    or "切换档案前无法禁用旧映射层"
                )
                return False

            timed_out_tasks = self.stop_all_macros(
                play_sound=False, keep_output_gate=True
            )
            cleanup_failures = list(getattr(
                self, "last_macro_release_failures", []
            ))
            self._clear_profile_transition_state(release_outputs=False)
            if timed_out_tasks or cleanup_failures:
                if timed_out_tasks:
                    if cleanup_failures:
                        self._macro_stop_gate_restore = None
                    elif getattr(self, "_macro_stop_gate_restore", None) is None:
                        self._macro_stop_gate_restore = bool(previous_gate)
                    self.engine_hint.setStyleSheet("color: #fbbf24;")
                    self.engine_hint.setText(
                        "旧档案宏任务仍在退出；档案切换已取消且映射层保持禁用"
                        + (
                            "，并且部分按键释放失败"
                            if cleanup_failures else ""
                        )
                    )
                    self.write_diagnostic(
                        "profile_transition_task_timeout",
                        force=True,
                        profile_id=profile_id,
                        remaining_tasks=list(timed_out_tasks),
                        cleanup_failures=list(cleanup_failures),
                    )
                    if not cleanup_failures:
                        self._defer_profile_input_restore(
                            layer=(
                                previous_layer
                                if self.mappings_enabled else DISABLED_LAYER_NAME
                            ),
                            profile_trigger_allowed=previous_allowed,
                            reason="profile_switch_timeout",
                        )
                    elif hasattr(self, "_show_macro_cleanup_failure"):
                        self._show_macro_cleanup_failure(
                            "档案切换已取消，旧档案任务与按键清理均未完成",
                            cleanup_failures
                            + [f"仍有 {len(timed_out_tasks)} 个宏线程"],
                        )
                else:
                    # Threads have exited, but at least one output channel did
                    # not confirm Release. Do not restore the old source layer
                    # or reopen Press/Tap; either action could recreate or add
                    # to the unconfirmed held state.
                    self._macro_stop_gate_restore = None
                    self.engine_hint.setStyleSheet("color: #ff8496;")
                    self.engine_hint.setText(
                        "旧档案按键释放未完成；档案切换已取消，请先强制释放键鼠"
                    )
                    self.write_diagnostic(
                        "profile_transition_release_failed",
                        force=True,
                        profile_id=profile_id,
                        cleanup_failures=list(cleanup_failures),
                    )
                    if hasattr(self, "_show_macro_cleanup_failure"):
                        self._show_macro_cleanup_failure(
                            "档案切换已取消，旧档案按键释放未完成",
                            cleanup_failures,
                        )
                return False

            if not self._install_runtime_profile_entry(entry):
                if previous_entry is not None:
                    self._install_runtime_profile_entry(previous_entry)
                return False

            self.active_profile_id = profile_id
            self.active_profile_layer = target_layer
            should_enable_layer = not (
                self.settings_input_mode_active
                or getattr(self, "recording_session_active", False)
            )
            desired_layer = (
                target_layer
                if self.mappings_enabled and should_enable_layer
                else DISABLED_LAYER_NAME
            )
            if (
                self.running
                and not self._runtime_is_game_mode()
                and not self._change_runtime_profile_layer(
                    desired_layer, wait=True
                )
            ):
                if previous_entry is not None:
                    self._install_runtime_profile_entry(previous_entry)
                self.active_profile_id = previous_profile_id
                self.active_profile_layer = previous_layer
                self._change_runtime_profile_layer(
                    previous_layer if self.mappings_enabled else DISABLED_LAYER_NAME,
                    wait=False,
                )
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(
                    self.engine.last_command_error
                    or self.keyboard_engine.last_command_error
                    or f"切换到档案层 {target_layer} 失败"
                )
                return False

            self.profile_trigger_allowed = should_enable_layer
            self._clear_process_guard_input_state()
            self._discard_profile_suspended_macros(
                reason=f"profile_switched:{previous_profile_id}->{profile_id}"
            )
            self.engine_hint.setStyleSheet("")
            mode_text = "热切换" if self.running else "已选择"
            self.engine_hint.setText(
                f"配置档案{mode_text}：{self._profile_name(profile_id)}"
            )
            self.write_diagnostic(
                "profile_activated",
                profile_id=profile_id,
                profile_name=self._profile_name(profile_id),
                layer=target_layer,
                previous_layer=previous_layer,
                reason=reason,
                backend=self.backend_combo.currentText(),
                running=self.running,
                transition="disabled_then_released",
            )
            self.refresh_status_ui()
            success = True
            return True
        finally:
            self.profile_switch_in_progress = False
            if not timed_out_tasks and not cleanup_failures:
                self.output_shutdown_in_progress = previous_gate
            if not success:
                self.profile_trigger_allowed = bool(
                    not timed_out_tasks
                    and not cleanup_failures
                    and previous_allowed
                    and not self.settings_input_mode_active
                    and not getattr(self, "recording_session_active", False)
                )
                if (
                    self.running
                    and not self._runtime_is_game_mode()
                    and not timed_out_tasks
                    and not cleanup_failures
                ):
                    layer_restored = self._change_runtime_profile_layer(
                        previous_layer
                        if self.mappings_enabled
                        and self.profile_trigger_allowed
                        else DISABLED_LAYER_NAME,
                        wait=False,
                    )
                    if layer_restored and self.profile_trigger_allowed:
                        resume_ok = self._resume_profile_suspended_macros(
                            reason=f"profile_switch_failed:{reason}"
                        )
                        if resume_ok is False:
                            self.profile_trigger_allowed = False
                            self.output_shutdown_in_progress = True
                            if not self._runtime_is_game_mode():
                                self._change_runtime_profile_layer(
                                    DISABLED_LAYER_NAME, wait=False
                                )
                            if hasattr(self, "engine_hint"):
                                self.engine_hint.setStyleSheet("color: #ff8496;")
                                self.engine_hint.setText(
                                    "档案切换失败后旧档案输入恢复不完整，请先强制释放键鼠"
                                )
                self.refresh_status_ui()
            self.refresh_profile_selector_state()

    def _foreground_profile_id(self):
        process_name, title = foreground_window_context()
        with self.data_lock:
            profiles = [dict(item) for item in self.runtime_profiles]
        profile = select_profile(profiles, process_name, title)
        return (
            str(profile.get("id") or "") if profile else "",
            str(process_name or ""),
            str(title or ""),
        )

    def _foreground_matches_profile(self, profile_id):
        profile_id = str(profile_id or "")
        if not profile_id:
            return True
        matched_id, _process_name, _title = self._foreground_profile_id()
        return matched_id == profile_id

    def _clear_process_guard_input_state(self):
        self._process_guard_input_suspended = False
        self._process_guard_suspended_profile_ids = set()

    def _restore_process_guard_input_if_ready(self, matched_profile_id=None):
        """Restore a layer isolated by process guard only in its bound process."""
        if not getattr(self, "_process_guard_input_suspended", False):
            return False
        if not self.running:
            self._clear_process_guard_input_state()
            return False
        if matched_profile_id is None:
            matched_profile_id, _process_name, _title = self._foreground_profile_id()
        active_id = str(self.active_profile_id or "")
        if active_id:
            allowed_ids = {active_id}
        else:
            allowed_ids = {
                str(value)
                for value in getattr(
                    self, "_process_guard_suspended_profile_ids", set()
                )
                if str(value)
            }
        if allowed_ids and str(matched_profile_id or "") not in allowed_ids:
            return False
        if not self._restore_active_profile_input(
            reason="process_guard_foreground_restored"
        ):
            return False
        self._clear_process_guard_input_state()
        self._process_guard_warning_active = False
        self.foreground_candidate_input_suspended = False
        return True

    def check_active_process_guards(self):
        if self._shutdown_started:
            return
        self._retry_quarantined_mouse_releases(force=False)

        required_recording_profile = str(self.recording_guard_profile_id or "")
        recording_guard_active = bool(required_recording_profile and self.recording)
        with self.macro_controller.lock:
            guarded = [
                (preset_id, str(task.preset.get("_required_profile_id") or ""),
                 str(task.preset.get("name") or "预设"))
                for preset_id, task in self.macro_controller.tasks.items()
                if task.has_live_threads()
                and str(task.preset.get("_required_profile_id") or "")
            ]

        matched_profile_id, _process_name, _title = self._foreground_profile_id()
        if not recording_guard_active:
            self._recording_guard_candidate = None
            self._recording_guard_candidate_hits = 0
        elif matched_profile_id == required_recording_profile:
            self._recording_guard_candidate = None
            self._recording_guard_candidate_hits = 0
        else:
            recording_candidate = (
                required_recording_profile,
                str(matched_profile_id or ""),
            )
            now = time.monotonic()
            if recording_candidate != self._recording_guard_candidate:
                self._recording_guard_candidate = recording_candidate
                self._recording_guard_candidate_since = now
                self._recording_guard_candidate_hits = 1
                return
            self._recording_guard_candidate_hits += 1
            if (
                self._recording_guard_candidate_hits < 2
                or now - self._recording_guard_candidate_since
                < float(self.foreground_profile_stable_seconds)
            ):
                return
            self._recording_guard_candidate = None
            self._recording_guard_candidate_hits = 0
            profile_name = self._profile_name(required_recording_profile)
            self.recording_guard_profile_id = None
            self.cancel_recording()
            self.engine_hint.setStyleSheet("color: #ff8496;")
            self.engine_hint.setText(
                f"录制已中断：前台窗口持续离开“{profile_name}”绑定的进程"
            )
            QMessageBox.warning(
                self,
                "录制已中断",
                f"录制过程中持续检测到前台窗口已离开“{profile_name}”绑定的进程，"
                "当前录制已取消，且不会导入未完成的录制结果。",
            )
            return

        if not guarded:
            self._process_guard_warning_active = False
            self._process_guard_candidate = None
            self._process_guard_candidate_hits = 0
            self._restore_process_guard_input_if_ready(matched_profile_id)
            return

        mismatched = [
            item for item in guarded
            if matched_profile_id != item[1]
        ]
        if not mismatched:
            self._process_guard_candidate = None
            self._process_guard_candidate_hits = 0
            self._restore_process_guard_input_if_ready(matched_profile_id)
            return
        if self._process_guard_warning_active:
            return

        candidate = (
            str(matched_profile_id or ""),
            tuple(sorted({item[1] for item in mismatched})),
        )
        now = time.monotonic()
        if candidate != self._process_guard_candidate:
            self._process_guard_candidate = candidate
            self._process_guard_candidate_since = now
            self._process_guard_candidate_hits = 1
            if not getattr(self, "profile_input_temporarily_suspended", False):
                suspended = self._suspend_active_profile_input(
                    layer=DISABLED_LAYER_NAME,
                    reason="process_guard_candidate",
                )
                if not suspended:
                    # If the source layer cannot be isolated, terminate now;
                    # continuing output into an unrelated foreground is unsafe.
                    self._process_guard_candidate_hits = 2
                    self._process_guard_candidate_since = 0.0
                else:
                    self._process_guard_input_suspended = True
                    self._process_guard_suspended_profile_ids = {
                        str(item[1]) for item in mismatched if str(item[1])
                    }
                    return
            else:
                if self.profile_input_suspend_reason == "process_guard_candidate":
                    self._process_guard_input_suspended = True
                    self._process_guard_suspended_profile_ids.update(
                        str(item[1]) for item in mismatched if str(item[1])
                    )
                return

        self._process_guard_candidate_hits += 1
        if (
            self._process_guard_candidate_hits < 2
            or now - self._process_guard_candidate_since
            < float(self.foreground_profile_stable_seconds)
        ):
            return

        self._process_guard_candidate = None
        self._process_guard_candidate_hits = 0

        self._process_guard_warning_active = True
        _preset_id, profile_id, preset_name = mismatched[0]
        profile_name = self._profile_name(profile_id)
        remaining = self.stop_all_macros(play_sound=False)
        cleanup_failures = list(getattr(
            self, "last_macro_release_failures", []
        ))
        # The normal mouse cleanup path quarantines MouseUp after the foreground
        # HWND changes, because an unrelated window must not receive an
        # unowned release.  A confirmed process-guard interruption is different:
        # leaving a program-owned button Down disables normal clicking system-wide.
        # Force only the concrete buttons retained by the two ownership ledgers;
        # never sweep configured or unrelated mouse targets here.
        mouse_released = bool(
            self._retry_quarantined_mouse_releases(force=True)
        )
        guarded_outputs_released = bool(
            not remaining and not cleanup_failures and mouse_released
        )
        self.engine_hint.setStyleSheet("color: #ff8496;")
        self.engine_hint.setText(
            (
                f"“{preset_name}”已中断：前台窗口已离开“{profile_name}”绑定的进程"
                if guarded_outputs_released else
                f"“{preset_name}”已中断，但仍有程序持有的输入未能释放"
            )
        )
        if hasattr(self, "activity_overlay"):
            overlay_generation = self.activity_overlay.show_message(
                "动作执行已中断",
                (
                    f"已离开“{profile_name}”绑定的进程，所有按下输入已释放"
                    if guarded_outputs_released else
                    "动作已停止，但输入释放失败；请使用“强制释放键鼠”"
                ),
                "#fb7185",
            )
            if guarded_outputs_released:
                QTimer.singleShot(
                    2200,
                    lambda generation=overlay_generation:
                    self.activity_overlay.hide_message(generation),
                )
        QMessageBox.warning(
            self,
            "动作执行已中断",
            f"执行过程中检测到前台窗口已离开“{profile_name}”绑定的进程，"
            + (
                "当前动作已立即停止，程序持有的虚拟按键和鼠标按钮均已释放。"
                if guarded_outputs_released else
                "当前动作已立即停止，但仍有程序持有的输入未能释放。"
                "请立即使用“强制释放键鼠”。"
            ),
        )

    def check_foreground_profile(self):
        """Follow a foreground profile after isolating the previous one safely."""
        if self._shutdown_started:
            return
        if not self.running:
            self.profile_trigger_allowed = True
            self._clear_process_guard_input_state()
            self.foreground_profile_candidate = None
            self.foreground_profile_candidate_hits = 0
            self.foreground_candidate_input_suspended = False
            return

        if foreground_window_belongs_to_current_process():
            self.foreground_profile_candidate = None
            self.foreground_profile_candidate_hits = 0
            self.foreground_candidate_input_suspended = False
            if getattr(self, "macrocanvas_foreground_suspend_failed", False):
                self.profile_trigger_allowed = False
                self.output_shutdown_in_progress = True
                return
            if self.profile_trigger_allowed:
                suspended = self._suspend_active_profile_input(
                    layer=DISABLED_LAYER_NAME,
                    reason="macrocanvas_foreground",
                )
                if not suspended:
                    return
            self.macrocanvas_foreground_suspended = True
            return

        if getattr(self, "macrocanvas_foreground_suspend_failed", False):
            # 前台隔离失败后不能因为焦点离开就自动恢复旧运行层。
            # 需要用户显式停止或强制释放，避免把未确认的旧 Kanata 层
            # 当作已经安全隔离过的状态继续使用。
            self.profile_trigger_allowed = False
            self.output_shutdown_in_progress = True
            return

        if getattr(self, "macrocanvas_foreground_suspended", False):
            self.macrocanvas_foreground_suspended = False
            transition_result = self._clear_profile_transition_state()
            transition_released = transition_result is not False
            if not transition_released:
                if hasattr(self, "_show_macro_cleanup_failure"):
                    self._show_macro_cleanup_failure(
                        "离开 MacroCanvas 前台时旧输出释放失败",
                        list(getattr(self, "last_macro_release_failures", [])),
                    )
                return
            if not self.runtime_profile_auto_switch_enabled:
                self._restore_active_profile_input(
                    reason="macrocanvas_foreground_left"
                )
                return
            # MacroCanvas 前台期间输入层已经禁用。离开窗口后仍保持隔离，
            # 但状态原因改为“切换隔离”，避免界面继续提示 MacroCanvas 位于前台。
            self.foreground_candidate_input_suspended = True
            if (
                getattr(self, "profile_input_temporarily_suspended", False)
                and str(getattr(self, "profile_input_suspend_reason", "") or "")
                == "macrocanvas_foreground"
            ):
                self.profile_input_suspend_reason = "foreground_candidate_detected"
                if hasattr(self, "engine_hint"):
                    self.engine_hint.setStyleSheet("color: #fbbf24;")
                    self.engine_hint.setText(
                        "已离开 MacroCanvas，正在确认目标档案，映射仍临时隔离"
                    )
                if hasattr(self, "runtime_profile_status"):
                    self.refresh_status_ui()

        if not self.runtime_profile_auto_switch_enabled:
            if self.foreground_candidate_input_suspended:
                if self._restore_active_profile_input(
                    reason="foreground_auto_switch_disabled"
                ):
                    self.foreground_candidate_input_suspended = False
            return
        if (
            self.settings_dialog_active
            or self.recording
            or getattr(self, "recording_session_active", False)
            or self.profile_switch_in_progress
            or self.loading_task_stack
        ):
            return

        process_name, title = foreground_window_context()
        profile = select_profile(self.runtime_profiles, process_name, title)
        target_id = str(profile.get("id") or "") if profile else ""

        context_key = (str(process_name or ""), str(title or ""), target_id)
        if context_key != self.last_foreground_profile_context:
            self.last_foreground_profile_context = context_key
            self.write_diagnostic(
                "foreground_profile_detected",
                process=process_name or "",
                title=title or "",
                matched_profile_id=target_id,
                matched_profile_name=(profile or {}).get("name", "") if profile else "",
                active_profile_id=self.active_profile_id,
            )

        if target_id == self.active_profile_id:
            self.foreground_profile_candidate = None
            self.foreground_profile_candidate_hits = 0
            if (
                not self.settings_input_mode_active
                and (
                    self.foreground_candidate_input_suspended
                    or not self.profile_trigger_allowed
                )
            ):
                if self._restore_active_profile_input(
                    reason="foreground_returned_to_active_profile"
                ):
                    self.foreground_candidate_input_suspended = False
            elif not self.settings_input_mode_active:
                self.profile_trigger_allowed = True
            return

        candidate = (
            str(process_name or "").casefold(),
            target_id,
        )
        now = time.monotonic()
        if candidate != self.foreground_profile_candidate:
            self.foreground_profile_candidate = candidate
            self.foreground_profile_candidate_since = now
            self.foreground_profile_candidate_hits = 1
            if not self.foreground_candidate_input_suspended:
                suspended = self._suspend_active_profile_input(
                    layer=DISABLED_LAYER_NAME,
                    reason="foreground_candidate_detected",
                )
                self.foreground_candidate_input_suspended = bool(suspended)
                if not suspended:
                    self.foreground_profile_candidate = None
                    self.foreground_profile_candidate_hits = 0
                    self.engine_hint.setStyleSheet("color: #ff8496;")
                    self.engine_hint.setText(
                        self.engine.last_command_error
                        or self.keyboard_engine.last_command_error
                        or "无法在档案切换前暂停旧映射层"
                    )
            return

        self.foreground_profile_candidate_hits += 1
        if (
            self.foreground_profile_candidate_hits < 2
            or now - self.foreground_profile_candidate_since
            < float(self.foreground_profile_stable_seconds)
        ):
            return

        # Guarded macros must be stopped by check_active_process_guards so the
        # user receives the explicit interruption message.  Keep the old layer
        # isolated for this tick instead of silently removing those tasks inside
        # the profile switch transaction.
        with self.macro_controller.lock:
            guarded_transition = any(
                task.has_live_threads()
                and str(task.preset.get("_required_profile_id") or "")
                and str(task.preset.get("_required_profile_id") or "") != target_id
                for task in self.macro_controller.tasks.values()
            )
        if guarded_transition:
            return

        self.foreground_profile_candidate = None
        self.foreground_profile_candidate_hits = 0
        activated = self._activate_profile_by_id(
            target_id, reason="foreground_direct"
        )
        if activated:
            self.foreground_candidate_input_suspended = False
        elif self.running and not self.settings_input_mode_active:
            # 切换失败时恢复旧档案的实际层，不能只恢复布尔标记。
            if self._restore_active_profile_input(
                reason="foreground_switch_failed"
            ):
                self.foreground_candidate_input_suspended = False
