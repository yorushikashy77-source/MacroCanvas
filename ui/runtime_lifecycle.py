"""Configuration-apply transactions and input-engine runtime lifecycle."""

from __future__ import annotations

from contextlib import nullcontext
import json
import uuid

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QMessageBox

from config.profiles import DISABLED_LAYER_NAME, profile_payload
from config.schema import validate_config_payload
from config.storage import atomic_write_text, write_deduplicated_snapshot
from core.constants import (
    APP_DIR, CONFIG_BACKUP_DIR, CONFIG_BACKUP_LIMIT, CONFIG_PATH,
    DIAGNOSTIC_LOG_PATH, KANATA_CONFIG_PATH, KANATA_KEYBOARD_CONFIG_PATH,
    ConfigState, EngineState, MacroState,
)
from engine.window_context import foreground_window_belongs_to_current_process


class RuntimeLifecycleMixin:
    def _runtime_control_transaction_busy(self):
        """Return True while ordinary runtime controls must not re-enter state changes."""
        return bool(
            getattr(self, "_shutdown_started", False)
            or getattr(self, "_runtime_operation_active", False)
            or getattr(self, "_config_apply_transaction_active", False)
            or getattr(self, "loading_task_stack", [])
        )

    @Slot()
    def toggle_running(self):
        if self._runtime_control_transaction_busy():
            self.write_diagnostic(
                "runtime_control_rejected",
                command="toggle_button",
                reason="transaction_busy",
            )
            return False
        target_enabled = not self.running
        result = self.set_running(
            target_enabled,
            allow_owned_mouse_force_release=not target_enabled,
        )
        if result is not False and self.running == target_enabled:
            self._play_feedback("enabled" if target_enabled else "disabled")
        else:
            self._play_feedback("error")

    @Slot(bool)
    def handle_global_toggle_fallback(self, enabled):
        """Start or completely stop the selected input engine.

        A control-only Interception input context remains available while the
        business engine is stopped, so the same shortcut also works in games.
        """
        enabled = bool(enabled)
        if self._runtime_control_transaction_busy():
            self.write_diagnostic(
                "runtime_control_rejected",
                command="global_toggle",
                reason="transaction_busy",
                requested=enabled,
            )
            return False
        if enabled == self.running:
            return
        result = self.set_running(
            enabled,
            allow_owned_mouse_force_release=not enabled,
        )
        if result is not False and self.running == enabled:
            self._play_feedback("enabled" if enabled else "disabled")
        else:
            self._play_feedback("error")

    @Slot()
    def reload_engine(self):
        return self.apply_changes()

    def _capture_apply_transaction_state(self):
        try:
            if self.applied_config_payload:
                payload = validate_config_payload(
                    json.loads(json.dumps(self.applied_config_payload))
                )
            else:
                payload = validate_config_payload(
                    json.loads(CONFIG_PATH.read_text("utf-8"))
                )
        except (OSError, ValueError, json.JSONDecodeError):
            payload = validate_config_payload(
                json.loads(json.dumps(self.current_config_payload()))
            )
        generated = {}
        for path in (KANATA_CONFIG_PATH, KANATA_KEYBOARD_CONFIG_PATH):
            try:
                generated[path] = path.read_text("utf-8")
            except OSError:
                generated[path] = None
        return {
            "payload": json.loads(json.dumps(payload)),
            "generated": generated,
            "was_running": bool(
                self.running or getattr(self, "restart_engine_after_apply", False)
            ),
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
            "applied_text": str(self.applied_config_text or ""),
            "applied_signature": str(self.applied_config_signature or ""),
            "runtime_diagnostic_enabled": bool(self.runtime_diagnostic_enabled),
            "diagnostic_session_id": str(self.diagnostic_session_id or ""),
            "diagnostic_write_count": int(self.diagnostic_write_count or 0),
        }

    @staticmethod
    def _restore_generated_apply_files(generated):
        for path, previous_text in (generated or {}).items():
            if previous_text is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, previous_text)

    def _stop_runtime_backends_for_transaction(self):
        """Stop every possible backend using the same safe order as power-off."""
        previous_gate = self.output_shutdown_in_progress
        previous_allowed = self.profile_trigger_allowed
        restore_layer = (
            self.active_profile_layer
            if self.mappings_enabled else DISABLED_LAYER_NAME
        )
        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        cleanup_complete = False
        source_disabled = False
        tasks_timed_out = False
        cleanup_failed = False
        retirement_started = False
        try:
            dispatch_lock = getattr(self, "output_dispatch_lock", None)
            if dispatch_lock is not None:
                with dispatch_lock:
                    pass

            if (
                self.running
                and not self._runtime_is_game_mode()
                and self.mappings_enabled
            ):
                if not self._change_runtime_profile_layer(
                    DISABLED_LAYER_NAME, wait=True
                ):
                    raise RuntimeError(
                        self.engine.last_command_error
                        or self.keyboard_engine.last_command_error
                        or "无法在应用配置前禁用旧 Kanata 映射层"
                    )
                source_disabled = True

            # Preserve any existing cleanup failure until this transaction proves
            # that every output was released successfully.  Only failures newly
            # reported by stop_all_macros() abort here; older latched failures are
            # verified by the full release sweep below and cleared only on success.
            previous_stop_failures = list(getattr(self, "last_macro_release_failures", []) or [])
            remaining = self.stop_all_macros(
                play_sound=False, keep_output_gate=True
            )
            stop_failures = list(getattr(self, "last_macro_release_failures", []) or [])
            new_stop_failures = [
                item for item in stop_failures if item not in previous_stop_failures
            ]
            if new_stop_failures:
                cleanup_failed = True
                self.engine_state = EngineState.FAILED
                self.output_shutdown_in_progress = True
                self.profile_trigger_allowed = False
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(
                    "旧配置的宏输出未能完全释放，已取消配置切换"
                )
                raise RuntimeError(
                    "停止旧配置宏任务时仍有输入未能确认释放，已保持映射层禁用并取消配置切换："
                    + "、".join(new_stop_failures)
                )
            if remaining:
                tasks_timed_out = True
                if getattr(self, "_macro_stop_gate_restore", None) is None:
                    self._macro_stop_gate_restore = bool(previous_gate)
                self._defer_profile_input_restore(
                    layer=restore_layer,
                    profile_trigger_allowed=previous_allowed,
                    reason="config_apply_stop_timeout",
                )
                raise RuntimeError(
                    "仍有宏任务未能在安全期限内退出，已取消配置切换。"
                )

            release_failures = []
            if not self._release_all_sync_mappings():
                release_failures.append("同步映射输出")
            if not self._release_runtime_virtual_keys(
                include_history=True, timeout=1.0
            ):
                release_failures.append("Kanata 虚拟键")
            if not self._release_interception_output():
                release_failures.append("Interception 输出")
            if not self._failsafe_release_runtime_targets(force_all=True):
                release_failures.append("系统级兜底释放")
            if release_failures:
                cleanup_failed = True
                self.last_macro_release_failures = list(dict.fromkeys(
                    list(getattr(self, "last_macro_release_failures", []))
                    + release_failures
                ))
                self.engine_state = EngineState.FAILED
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(
                    "旧配置的输入状态未能完全释放，已取消配置切换"
                )
                raise RuntimeError(
                    "旧配置仍有输入未能确认释放，已保持映射层禁用并取消配置切换："
                    + "、".join(release_failures)
                )
            self.last_macro_release_failures = []
            self.active_macro_id = None
            self.last_action_activity = {}
            self.macro_state = MacroState.IDLE
            self.macro_status_detail = ""
            retirement_started = True
            self.running = False
            self.direct_interception_active = False
            if self.global_hook:
                hook = self.global_hook
                if not hook.stop(timeout=1.5):
                    raise RuntimeError(
                        getattr(hook, "last_stop_warning", "Windows 输入监听线程未能安全退出")
                    )
                if self.global_hook is hook:
                    self.global_hook = None
            if self.interception_input_hook and not self._stop_interception_input_hook(
                timeout=1.5
            ):
                raise RuntimeError("Interception 输入线程未能安全退出")
            if self.interception_output:
                output = self.interception_output
                if not output.stop():
                    raise RuntimeError("Interception 输出上下文未能安全销毁")
                if self.interception_output is output:
                    self.interception_output = None
                with getattr(self, "input_state_lock", nullcontext()):
                    self.active_sync_by_source.clear()
                with self.sync_output_lock:
                    self.sync_output_counts.clear()
            if self._kanata_engine_has_runtime(self.keyboard_engine):
                if not self.keyboard_engine.stop(timeout=3.0):
                    raise RuntimeError("辅助 Kanata 未能安全停止")
            if self._kanata_engine_has_runtime(self.engine):
                if not self.engine.stop(timeout=3.0):
                    raise RuntimeError("主 Kanata 未能安全停止")
            with getattr(self, "input_state_lock", nullcontext()):
                self.active_sync_by_source.clear()
            with self.sync_output_lock:
                self.sync_output_counts.clear()
            self.engine_state = EngineState.STOPPED
            cleanup_complete = True
        finally:
            if cleanup_complete:
                self.output_shutdown_in_progress = previous_gate
            elif tasks_timed_out or cleanup_failed or retirement_started:
                self.output_shutdown_in_progress = True
                self.profile_trigger_allowed = False
            else:
                layer_restored = True
                if source_disabled and self.running:
                    layer_restored = self._change_runtime_profile_layer(
                        restore_layer, wait=True
                    )
                self.output_shutdown_in_progress = (
                    previous_gate if layer_restored else True
                )
                self.profile_trigger_allowed = bool(
                    layer_restored and previous_allowed
                )

    def _restore_apply_transaction(
        self, state, stop_current=True, restart_runtime=True
    ):
        rollback_errors = []
        if stop_current:
            try:
                self._stop_runtime_backends_for_transaction()
            except Exception as error:
                rollback_errors.append(f"停止失败配置：{error}")
        try:
            self._save_config_payload(state["payload"], create_backup=False)
        except Exception as error:
            rollback_errors.append(f"恢复配置文件：{error}")
        try:
            self._restore_generated_apply_files(state["generated"])
        except Exception as error:
            rollback_errors.append(f"恢复引擎文件：{error}")
        try:
            restored = self._reload_full_configuration_into_window(
                state["payload"]
            )
            self.applied_config_payload = json.loads(json.dumps(restored))
            self.applied_config_signature = self.current_config_signature()
            self.applied_config_text = (
                state["generated"].get(KANATA_CONFIG_PATH)
                or state.get("applied_text", "")
            )
            self._snapshot_runtime_config()
            self.runtime_diagnostic_enabled = bool(
                state.get("runtime_diagnostic_enabled", False)
            )
            self.diagnostic_session_id = str(
                state.get("diagnostic_session_id", "") or ""
            )
            self.diagnostic_write_count = int(
                state.get("diagnostic_write_count", 0) or 0
            )
            self.suspended_mapping_ids.clear()
            self.suspended_preset_ids.clear()
            self.pending_mapping_deletions.clear()
            self.pending_preset_deletions.clear()
            self.mappings_enabled = bool(state.get("mappings_enabled", True))
            self.profile_trigger_allowed = bool(
                state.get("profile_trigger_allowed", True)
            )
            self.profile_input_temporarily_suspended = bool(
                state.get("profile_input_temporarily_suspended", False)
            )
            self.profile_input_suspend_reason = str(
                state.get("profile_input_suspend_reason", "") or ""
            )
            self.macrocanvas_foreground_suspended = bool(
                state.get("macrocanvas_foreground_suspended", False)
            )
            self.foreground_candidate_input_suspended = bool(
                state.get("foreground_candidate_input_suspended", False)
            )
            self.reload_button.setEnabled(False)
            self.config_state = ConfigState.SAVED
        except Exception as error:
            rollback_errors.append(f"恢复界面和运行快照：{error}")

        if state.get("was_running") and restart_runtime and not rollback_errors:
            try:
                # A failure can occur before the old backend is touched (for
                # example a macro refuses to stop).  In that case keep the live
                # old engine and only restore its matching runtime tables.
                if not (self.running and not stop_current):
                    started = self._set_running_impl(True)
                    if started is False or not self.running:
                        raise RuntimeError("原输入引擎未能重新启动")
                    self._restore_runtime_mapping_gate_after_apply(state)
                self.config_state = ConfigState.APPLIED
            except Exception as error:
                rollback_errors.append(f"恢复原输入引擎：{error}")
        elif not state.get("was_running"):
            try:
                if not self.update_global_hook_for_backend():
                    raise RuntimeError("全局输入监听未能恢复")
            except Exception as error:
                rollback_errors.append(f"恢复全局监听：{error}")

        if rollback_errors:
            self.config_state = ConfigState.FAILED
        else:
            self.restart_engine_after_apply = False
        self.refresh_status_ui()
        return rollback_errors

    def _restore_runtime_mapping_gate_after_apply(self, state):
        """Restore the pre-apply mapping gate after rebuilding runtime backends."""
        if not self.running:
            return True
        if bool(state.get("mappings_enabled", True)):
            return True

        self.profile_trigger_allowed = bool(
            state.get("profile_trigger_allowed", True)
        )
        self.profile_input_temporarily_suspended = False
        self.profile_input_suspend_reason = ""
        self.macrocanvas_foreground_suspended = False
        self.foreground_candidate_input_suspended = False
        if self._runtime_is_game_mode():
            self.mappings_enabled = False
            self.write_diagnostic(
                "apply_preserved_mapping_pause",
                backend="interception",
                active_profile_id=self.active_profile_id,
            )
            return True

        if not self._change_runtime_profile_layer(DISABLED_LAYER_NAME, wait=True):
            self.output_shutdown_in_progress = True
            self.profile_trigger_allowed = False
            raise RuntimeError(
                "新配置已启动，但未能恢复应用前的映射暂停状态："
                + (
                    self.engine.last_command_error
                    or self.keyboard_engine.last_command_error
                    or "Kanata 未确认禁用映射层"
                )
            )
        self.mappings_enabled = False
        self.write_diagnostic(
            "apply_preserved_mapping_pause",
            backend="kanata",
            active_profile_id=self.active_profile_id,
        )
        return True

    def _isolate_macrocanvas_foreground_after_runtime_start(self):
        """Close the source layer immediately if MacroCanvas itself is foreground."""
        if not foreground_window_belongs_to_current_process():
            self.macrocanvas_foreground_suspended = False
            self.macrocanvas_foreground_suspend_failed = False
            return True

        self.foreground_profile_candidate = None
        self.foreground_profile_candidate_hits = 0
        self.foreground_candidate_input_suspended = False
        suspended = self._suspend_active_profile_input(
            layer=DISABLED_LAYER_NAME,
            reason="macrocanvas_foreground",
        )
        if suspended:
            self.macrocanvas_foreground_suspended = True
            self.macrocanvas_foreground_suspend_failed = False
            self.write_diagnostic(
                "runtime_start_macrocanvas_foreground_isolated",
                active_profile_id=self.active_profile_id,
            )
            return True

        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        self.write_diagnostic(
            "runtime_start_macrocanvas_foreground_isolation_failed",
            force=True,
            active_profile_id=self.active_profile_id,
        )
        return False

    def _commit_applied_candidate(self, candidate, *, mappings_enabled=None):
        self.startup_recovery_pending_save = False
        self.applied_config_payload = json.loads(json.dumps(candidate))
        self.editor_loaded_profile_id = str(self.editor_profile_id or "")
        self.editor_loaded_payload = profile_payload({
            "payload": self._current_profile_snapshot()
        })
        self.applied_config_signature = self.current_config_signature()
        self.applied_config_text = (
            KANATA_CONFIG_PATH.read_text("utf-8", errors="replace")
            if KANATA_CONFIG_PATH.exists() else ""
        )
        self.suspended_mapping_ids.clear()
        self.suspended_preset_ids.clear()
        self.pending_mapping_deletions.clear()
        self.pending_preset_deletions.clear()
        self.restart_engine_after_apply = False
        if mappings_enabled is not None:
            self.mappings_enabled = bool(mappings_enabled)
        elif not self.running:
            self.mappings_enabled = True
        self.config_state = (
            ConfigState.APPLIED if self.running else ConfigState.SAVED
        )
        self.reload_button.setEnabled(False)
        if self.running:
            self._record_applied_config_snapshot(candidate, show_warning=True)

    def apply_changes(self):
        nested_runtime_start = bool(getattr(
            self, "_runtime_nested_apply_token", False
        ))
        if nested_runtime_start:
            # Consume the one-shot token before any Qt event pumping. A queued
            # public apply command therefore cannot inherit this permission.
            self._runtime_nested_apply_token = False
        if getattr(self, "_shutdown_started", False):
            return False
        if (
            self.loading_task_stack
            or getattr(self, "_runtime_operation_active", False)
            or getattr(self, "_config_apply_transaction_active", False)
        ) and not nested_runtime_start:
            self.write_diagnostic(
                "runtime_control_rejected",
                command="apply_changes",
                reason="transaction_busy",
            )
            return False
        if getattr(self, "recording_session_active", False):
            QMessageBox.information(
                self,
                "正在录制",
                "录制倒计时或正式录制期间不能应用配置。请先完成或取消录制。",
            )
            return False
        if nested_runtime_start:
            return self._apply_changes_impl()

        self._runtime_operation_active = True
        try:
            self._begin_loading(
                "正在应用更改",
                "正在校验当前配置方案并准备运行配置……",
                host=self,
            )
            try:
                return self._apply_changes_impl()
            finally:
                self._end_loading()
        finally:
            self._runtime_operation_active = False

    def _apply_changes_impl(self):
        self._set_loading_message(
            "正在应用更改", "正在收集并校验当前配置方案……"
        )
        self._store_editor_payload()
        target_editor_id = str(self.editor_profile_id or "")
        target_profile = self._profile_record(target_editor_id)
        if target_editor_id and target_profile is None:
            self.editor_profile_id = ""
        health_check = getattr(self, "current_preset_health_issues", None)
        health_issues = health_check() if callable(health_check) else []
        if health_issues:
            self.config_state = ConfigState.FAILED
            if getattr(self, "_auto_apply_in_progress", False):
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "自动应用已暂停：当前方案检查发现问题，请修正后再应用"
                )
            else:
                self.open_preset_health_check(self.selected_preset_card)
            self.refresh_status_ui()
            return False
        if not self.confirm_trigger_conflict_report():
            return False

        with self.macro_controller.lock:
            active_tasks = [
                task for task in self.macro_controller.tasks.values()
                if task.has_live_threads()
            ]
        if active_tasks:
            if getattr(self, "_auto_apply_in_progress", False):
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "配置已修改；检测到新的宏任务，等待任务结束后再自动应用"
                )
                self.auto_apply_timer.start(500)
                self.refresh_status_ui()
                return False
            answer = QMessageBox.question(
                self,
                "应用配置会停止宏",
                f"当前仍有 {len(active_tasks)} 个宏任务正在运行。\n"
                "继续应用会停止这些任务并释放其按键。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False

        try:
            candidate = validate_config_payload(
                json.loads(json.dumps(self.current_config_payload()))
            )
        except (TypeError, ValueError) as error:
            self.config_state = ConfigState.FAILED
            QMessageBox.warning(self, "配置校验失败", str(error))
            self.refresh_status_ui()
            return False

        transaction = self._capture_apply_transaction_state()
        self._config_apply_transaction_active = True
        candidate_saved = False
        runtime_reconfigured = False
        listener_warning = ""
        previous_runtime_diagnostic = self.runtime_diagnostic_enabled
        try:
            self._set_loading_message(
                "正在生成配置", "正在构建档案图层和虚拟按键……"
            )
            if not self.generate_kanata_config():
                raise RuntimeError("无法生成有效的输入引擎配置")

            if transaction["was_running"]:
                self._set_loading_message(
                    "正在检查运行环境", "正在检查当前输入后端和所需组件……"
                )
                ok, message = self._validate_selected_backend()
                if not ok:
                    raise RuntimeError(message)

            # Save the validated candidate before replacing any live runtime.
            # If this write fails, the old engine and runtime tables remain
            # untouched, so game mode cannot enter a new-runtime/old-disk split.
            self._set_loading_message(
                "正在保存候选配置", "正在原子写入配置文件……"
            )
            self._save_config_payload(candidate, create_backup=False)
            candidate_saved = True

            if transaction["was_running"]:
                self._set_loading_message(
                    "正在切换运行配置",
                    "正在停止旧输入通道并准备载入候选配置……",
                )
                self._stop_runtime_backends_for_transaction()
                runtime_reconfigured = True

            self.last_foreground_profile_context = None
            self.applied_config_payload = json.loads(json.dumps(candidate))
            self.config_state = ConfigState.SAVED
            self._snapshot_runtime_config()
            self.suspended_mapping_ids.clear()
            self.suspended_preset_ids.clear()
            self.pending_mapping_deletions.clear()
            self.pending_preset_deletions.clear()
            # Runtime rebuild has to open the fresh backend first.  The user-visible
            # pause/isolation gate captured at transaction start is restored after
            # the new channels are confirmed, before the candidate is committed.
            self.mappings_enabled = True
            self.profile_trigger_allowed = True

            if self.runtime_diagnostic_enabled != previous_runtime_diagnostic:
                if self.runtime_diagnostic_enabled:
                    self.diagnostic_session_id = uuid.uuid4().hex[:8]
                    self.reset_diagnostic_log()
                    self.write_diagnostic(
                        "diagnostic_enabled",
                        path=str(DIAGNOSTIC_LOG_PATH),
                        session=self.diagnostic_session_id,
                        force=True,
                    )
                else:
                    self.write_diagnostic("diagnostic_disabled", force=True)
                    self.diagnostic_session_id = ""

            if transaction["was_running"]:
                self._set_loading_message(
                    "正在启动候选配置", "正在建立新的输入通道……"
                )
                started = self._set_running_impl(True)
                if started is False or not self.running:
                    raise RuntimeError("新输入引擎未能启动或建立完整监听通道")
            else:
                if not self.update_global_hook_for_backend():
                    listener_warning = (
                        "配置已保存，但当前系统环境未能建立全局控制监听。"
                        "这不会撤销已保存的配置；修复驱动或权限后，可直接启动"
                        "输入引擎再次检查。"
                    )
                    self.write_diagnostic(
                        "saved_config_listener_unavailable",
                        force=True,
                        backend=self.backend_combo.currentText(),
                    )

            self._set_loading_message(
                "正在提交配置", "正在记录已验证的保存快照……"
            )
            self._restore_runtime_mapping_gate_after_apply(transaction)
            self._save_config_payload(candidate, create_backup=True)
            self._commit_applied_candidate(
                candidate, mappings_enabled=self.mappings_enabled
            )
            if self.running:
                if not self.mappings_enabled:
                    self.engine_hint.setStyleSheet("color: #fbbf24;")
                    self.engine_hint.setText("配置已保存；映射保持应用前的暂停状态")
                elif getattr(self, "profile_input_temporarily_suspended", False):
                    # Keep the foreground/isolation message produced by the
                    # runtime gate restoration instead of masking it as ordinary
                    # success while the source layer is still disabled.
                    self.engine_hint.setStyleSheet("color: #fbbf24;")
                    if not self.engine_hint.text():
                        self.engine_hint.setText("配置已保存；映射仍处于临时隔离状态")
                else:
                    self.engine_hint.setStyleSheet("")
                    self.engine_hint.setText(
                        "配置已保存并成功应用到当前输入引擎"
                    )
            elif listener_warning:
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "配置已保存；全局控制监听暂未建立，启动引擎时将再次检查"
                )
                QMessageBox.warning(self, "配置已保存", listener_warning)
            else:
                self.engine_hint.setStyleSheet("")
                self.engine_hint.setText("配置已保存；启动输入引擎后确认运行")
            self._config_apply_transaction_active = False
            self.refresh_status_ui()
            return True
        except Exception as error:
            rollback_errors = []
            rejected_snapshot_saved = False
            if candidate_saved:
                try:
                    write_deduplicated_snapshot(
                        CONFIG_BACKUP_DIR,
                        "rejected",
                        json.dumps(candidate, ensure_ascii=False, indent=2),
                        limit=CONFIG_BACKUP_LIMIT,
                    )
                    rejected_snapshot_saved = True
                except (OSError, ValueError, TypeError):
                    pass
                rollback_errors = self._restore_apply_transaction(
                    transaction,
                    stop_current=(
                        runtime_reconfigured
                        or (transaction["was_running"] and not self.running)
                    ),
                    # Keep the editor candidate available for correction.  The
                    # previous runtime is left stopped so UI candidate settings
                    # cannot be mistaken for the restored applied backend.
                    restart_runtime=False,
                )
                try:
                    self._reload_full_configuration_into_window(candidate)
                    self.applied_config_payload = json.loads(json.dumps(
                        transaction["payload"]
                    ))
                    self.applied_config_signature = self.config_payload_signature(
                        transaction["payload"]
                    )
                    self.applied_config_text = str(
                        transaction.get("applied_text", "") or ""
                    )
                    self.restart_engine_after_apply = bool(
                        transaction.get("was_running")
                    )
                    self.config_state = ConfigState.FAILED
                    self.reload_button.setEnabled(True)
                    if not self.update_global_hook_for_backend():
                        rollback_errors.append(
                            "失败候选已保留，但全局控制监听未能切换"
                        )
                except Exception as candidate_error:
                    rollback_errors.append(
                        f"恢复失败候选到编辑器：{candidate_error}"
                    )
            else:
                try:
                    self._restore_generated_apply_files(transaction["generated"])
                except Exception as restore_error:
                    rollback_errors.append(f"恢复引擎文件：{restore_error}")
                self.config_state = ConfigState.FAILED
                self.refresh_status_ui()

            detail = str(error)
            if rollback_errors:
                detail += "\n\n自动恢复过程中还发生：\n" + "\n".join(
                    f"- {item}" for item in rollback_errors
                )
            elif candidate_saved:
                detail += (
                    "\n\n磁盘与运行快照已恢复到应用前版本；失败候选仍保留在"
                    "当前编辑器中，输入引擎保持停止，修正后可直接再次应用。"
                )
            if rejected_snapshot_saved:
                detail += (
                    "\n\n本次未能应用的候选配置已保存到备份配置表中的"
                    "“应用失败候选”，可在修正运行环境后手动恢复。"
                )
            self._config_apply_transaction_active = False
            QMessageBox.warning(self, "配置应用失败", detail)
            return False

    def set_running(self, value, allow_owned_mouse_force_release=False):
        if getattr(self, "_shutdown_started", False):
            return False
        if (
            self.loading_task_stack
            or getattr(self, "_runtime_operation_active", False)
            or getattr(self, "_config_apply_transaction_active", False)
        ):
            self.write_diagnostic(
                "runtime_control_rejected",
                command="set_running",
                reason="transaction_busy",
                requested=bool(value),
            )
            return False
        if getattr(self, "recording_session_active", False):
            QMessageBox.information(
                self,
                "正在录制",
                "录制倒计时、正式录制或结果整理期间不能启动或停止输入引擎。"
                "请先完成或取消录制。",
            )
            return False
        title = "正在启动输入引擎" if value else "正在停止输入引擎"
        detail = (
            "正在检查设备并建立输入通道……"
            if value else "正在停止监听并释放程序持有的输入……"
        )
        self._runtime_operation_active = True
        try:
            self._begin_loading(title, detail, host=self)
            try:
                return self._set_running_impl(
                    value,
                    allow_owned_mouse_force_release=allow_owned_mouse_force_release,
                )
            finally:
                self._end_loading()
        finally:
            self._runtime_operation_active = False

    def _set_running_impl(self, value, allow_owned_mouse_force_release=False):
        if value:
            # Cancel delayed Up sweeps from a previous stop before accepting any
            # new output from the freshly started runtime.
            self._stop_release_guard_generation = int(getattr(
                self, "_stop_release_guard_generation", 0
            )) + 1
        if value and self.runtime_diagnostic_enabled:
            self.diagnostic_session_id = uuid.uuid4().hex[:8]
            self.reset_diagnostic_log()
        self.write_diagnostic(
            "set_running_requested",
            value=bool(value),
            current=self.running,
            backend=self.backend_combo.currentText(),
        )

        if value:
            start_previous_trigger_allowed = bool(getattr(
                self, "profile_trigger_allowed", False
            ))

            def _abort_start_without_runtime(result=False):
                # 启动早期失败时不要把运行中的旧门控状态误清成 False。
                # 如果当前并没有成功运行，则新触发门保持关闭。
                self.profile_trigger_allowed = (
                    start_previous_trigger_allowed
                    if bool(getattr(self, "running", False))
                    else False
                )
                return result

            pending_cleanup = list(getattr(
                self, "last_macro_release_failures", []
            ))
            # 旧逻辑只拦截 output_shutdown_in_progress and pending_cleanup:
            # 现在只要输出清理门仍关闭，就拒绝重新启动。
            if bool(getattr(self, "output_shutdown_in_progress", False)):
                if not pending_cleanup:
                    pending_cleanup = ["输出清理仍在进行"]
                self.engine_state = EngineState.FAILED
                self.profile_trigger_allowed = False
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(
                    "上一次输入清理尚未完成，请先执行“强制释放键鼠”"
                )
                QMessageBox.warning(
                    self,
                    "暂不能启动输入引擎",
                    "上一次停止或异常清理仍未确认完成：\n"
                    + "\n".join(f"- {item}" for item in pending_cleanup)
                    + "\n\n请先点击“强制释放键鼠”，确认清理成功后再启动。",
                )
                self.refresh_status_ui()
                return False
            self._set_loading_message(
                "正在启动输入引擎", "正在清理旧状态并准备运行配置……"
            )
            clear_physical_state = getattr(
                self, "_clear_physical_input_state", None
            )
            if callable(clear_physical_state):
                clear_physical_state()
            else:
                with getattr(self, "input_state_lock", nullcontext()):
                    getattr(self, "physical_down", set()).clear()
                    getattr(self, "physical_modifiers", set()).clear()
                    getattr(self, "physical_input_sources", {}).clear()
            with getattr(self, "input_state_lock", nullcontext()):
                self.held_trigger_ids.clear()
                self.kanata_trigger_down.clear()
                self.suppressed_trigger_names.clear()
                getattr(self, "interception_forwarded_down", set()).clear()
            with self.expected_kanata_event_lock:
                self.expected_kanata_events.clear()
            clear_hotkey_latches = getattr(
                self, "_clear_system_hotkey_latches", None
            )
            if callable(clear_hotkey_latches):
                clear_hotkey_latches()
            else:
                self.system_hotkey_latched.clear()
                getattr(self, "system_hotkey_latched_sources", {}).clear()
            self.recording_control_modifiers.clear()
            getattr(self, "recording_control_sources", {}).clear()
            # Preserve the global-toggle latch owner until its matching KeyUp.
            # A shortcut-triggered start rebuilds listeners between Down and Up.
            self.profile_trigger_allowed = False

            if self.config_state in (ConfigState.DIRTY, ConfigState.FAILED):
                if not self.auto_apply_checkbox.isChecked():
                    QMessageBox.information(
                        self,
                        "存在未应用更改",
                        "当前配置尚未保存或应用。请先点击“应用更改”，"
                        "再启动输入引擎。",
                    )
                    return _abort_start_without_runtime(False)
                # This is the one intentional nested apply path. The one-shot
                # token is consumed at apply_changes() entry before any event
                # pumping, so unrelated queued commands remain blocked.
                self._runtime_nested_apply_token = True
                try:
                    applied = self.apply_changes()
                finally:
                    self._runtime_nested_apply_token = False
                # Applying can be rejected without changing the state to FAILED
                # (for example when the user declines a trigger-risk warning).
                # Never continue into _snapshot_runtime_config with that pending
                # editor candidate: game mode would otherwise run it directly,
                # while normal mode would pair it with the previous Kanata file.
                if (
                    applied is not True
                    or self.config_state in (ConfigState.DIRTY, ConfigState.FAILED)
                ):
                    return _abort_start_without_runtime(False)
                if self.running:
                    return _abort_start_without_runtime(False)
            elif (
                not self._generated_kanata_configs_current()
                and not self.generate_kanata_config()
            ):
                return _abort_start_without_runtime(False)

            self._set_loading_message(
                "正在检查输入后端", "正在确认驱动、设备和配置文件状态……"
            )
            if self._config_apply_transaction_active:
                ok, message = True, "候选配置已在应用事务中完成校验"
            else:
                ok, message = self._validate_selected_backend()
            if not ok:
                self.engine_state = EngineState.FAILED
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(message)
                QMessageBox.warning(self, "输入引擎启动失败", message)
                self.refresh_status_ui()
                return _abort_start_without_runtime(False)

            self._snapshot_runtime_config()
            selected_backend = self.backend_combo.currentText()
            game_mode = self._runtime_is_game_mode()

            if not game_mode and self.interception_input_hook:
                if not self._stop_interception_input_hook():
                    self.engine_state = EngineState.FAILED
                    self.engine_hint.setStyleSheet("color: #ff8496;")
                    self.engine_hint.setText(
                        "旧的 Interception 输入线程仍在退出，请再次启动输入引擎"
                    )
                    self.refresh_status_ui()
                    return _abort_start_without_runtime(False)
            stale_backend_errors = []
            if not game_mode and self.interception_output:
                output = self.interception_output
                try:
                    stopped = bool(output.stop())
                except Exception as error:
                    stopped = False
                    stale_backend_errors.append(
                        f"停止旧 Interception 输出：{error}"
                    )
                if stopped and self.interception_output is output:
                    self.interception_output = None
                elif not stopped and not stale_backend_errors:
                    stale_backend_errors.append("旧 Interception 输出上下文停止超时")
            if self._kanata_engine_has_runtime(self.keyboard_engine):
                try:
                    if not self.keyboard_engine.stop(timeout=3.0):
                        stale_backend_errors.append("辅助 Kanata 停止超时")
                except Exception as error:
                    stale_backend_errors.append(f"停止辅助 Kanata：{error}")
            if self._kanata_engine_has_runtime(self.engine):
                try:
                    if not self.engine.stop(timeout=3.0):
                        stale_backend_errors.append("主 Kanata 停止超时")
                except Exception as error:
                    stale_backend_errors.append(f"停止主 Kanata：{error}")
            if stale_backend_errors:
                self.running = False
                self.profile_trigger_allowed = False
                self.output_shutdown_in_progress = True
                self.engine_state = EngineState.FAILED
                detail = "旧输入后端未能安全停止，已取消本次启动：\n" + "\n".join(
                    f"- {item}" for item in stale_backend_errors
                )
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText("旧输入后端仍在退出，未启动新的输入引擎")
                self.write_diagnostic(
                    "set_running_stale_backend_stop_failed",
                    errors=stale_backend_errors,
                )
                QMessageBox.warning(self, "输入引擎启动失败", detail)
                self.refresh_status_ui()
                return False

            self._set_loading_message(
                "正在建立输入通道",
                "正在连接 Interception 或 Kanata 输入后端……",
            )
            if game_mode:
                # 输入 context 从程序启动起常驻。启动业务引擎时只创建输出
                # context，并将同一个监听器从控制态切换为完整分发模式。
                self.direct_interception_active = True
                self.mappings_enabled = True
                ok = self.start_interception_input_hook(control_only=False)
                self.running = bool(ok)
                message = (
                    "Interception 键盘/鼠标来源与输出通道运行中"
                    if ok else "Interception 来源或输出通道启动失败"
                )
                if not ok:
                    self.direct_interception_active = False
                    self.start_interception_input_hook(control_only=True)
            else:
                self.direct_interception_active = False
                ok, message = self.engine.start(
                    selected_backend, validate_config=False
                )
                self.running = ok
                if ok:
                    self.mappings_enabled = True
                    if not self.engine.change_layer(
                        self.active_profile_layer, wait=True, timeout=1.0
                    ):
                        ok = False
                        message = "Kanata 档案层切换失败"
                    elif not self.update_global_hook_for_backend():
                        ok = False
                        message = "Windows 全局输入监听启动失败"
                    if not ok:
                        self.running = False
                        self.mappings_enabled = False
                        self.profile_trigger_allowed = False
                        try:
                            stopped = bool(self.engine.stop(timeout=3.0))
                        except Exception as error:
                            stopped = False
                            message += f"；停止不完整引擎时发生：{error}"
                        if not stopped:
                            self.output_shutdown_in_progress = True
                            message += "；不完整的 Kanata 进程仍在退出"

            if ok:
                # A key can stay physically held while the listener/backend is
                # rebuilt. Reconstruct that state before opening the trigger gate
                # so auto-repeat cannot be mistaken for a fresh first press.
                self._reseed_physical_input_state(seed_control=False)
                self.output_shutdown_in_progress = False
                self.profile_trigger_allowed = True
                self.macrocanvas_foreground_suspend_failed = False
                clear_process_guard = getattr(
                    self, "_clear_process_guard_input_state", None
                )
                if callable(clear_process_guard):
                    clear_process_guard()
                if not self._isolate_macrocanvas_foreground_after_runtime_start():
                    self.engine_state = EngineState.FAILED
                    self.engine_hint.setStyleSheet("color: #ff8496;")
                    self.engine_hint.setText(
                        "输入引擎已启动，但 MacroCanvas 前台隔离失败；"
                        "已禁止新的 Python 侧触发"
                    )
                    QMessageBox.warning(
                        self,
                        "前台隔离失败",
                        "输入引擎已启动，但未能确认切换到禁用层。"
                        "为避免在 MacroCanvas 主窗口前台误触发映射，"
                        "当前已禁止新的 Python 侧触发。请停止输入引擎或强制释放键鼠后再继续编辑。",
                    )
                    self.refresh_status_ui()
                    return False
                self.engine_state = EngineState.RUNNING
                self.write_diagnostic(
                    "set_running_started",
                    backend=selected_backend,
                    kanata_engine=self.engine.is_running(),
                    keyboard_kanata_engine=self.keyboard_engine.is_running(),
                    interception_input_hook=self._interception_source_ready(),
                    interception_keyboard_device=(
                        getattr(self.interception_output, "keyboard_device", 0)
                    ),
                    interception_mouse_device=(
                        getattr(self.interception_output, "mouse_device", 0)
                    ),
                )
                self.write_diagnostic(
                    "runtime_trigger_rules",
                    rules=[
                        {
                            "id": rule.get("id"),
                            "kind": rule.get("_runtime_kind", "mapping"),
                            "enabled": bool(rule.get("enabled")),
                            "source_modifiers": rule.get(
                                "source_modifiers", "无"
                            ),
                            "source": rule.get("source"),
                            "mode": rule.get("mode"),
                            "condition_enabled": bool(
                                rule.get("condition_enabled", False)
                            ),
                            "condition_input": rule.get("condition_input"),
                            "condition_state": rule.get("condition_state"),
                            "actions": len(rule.get("actions", []) or []),
                        }
                        for rule in self._runtime_mapping_rules()
                    ],
                )
                if getattr(self, "profile_input_temporarily_suspended", False):
                    self.engine_hint.setStyleSheet("color: #fbbf24;")
                    if not self.engine_hint.text():
                        self.engine_hint.setText(
                            "MacroCanvas 位于前台，映射已临时隔离；切换到目标程序后自动恢复"
                        )
                else:
                    self.engine_hint.setStyleSheet("")
                    if game_mode:
                        self.engine_hint.setText(
                            "游戏模式运行中 · 键盘、鼠标按键、滚轮与动作输出"
                            "均由 Interception 处理"
                        )
                    else:
                        self.engine_hint.setText(f"{message} · {selected_backend}")
                self.toggle_button.setText("停止输入引擎")
                self.toggle_button.setObjectName("stop")
                self.config_state = ConfigState.APPLIED
                if (
                    self.applied_config_payload
                    and not getattr(self, "_config_apply_transaction_active", False)
                ):
                    self._record_applied_config_snapshot(
                        self.applied_config_payload,
                        show_warning=False,
                    )
            else:
                residual_backend = bool(
                    game_mode
                    and (
                        self.interception_output is not None
                        or getattr(self, "_interception_output_stop_failed", False)
                    )
                )
                self.running = residual_backend
                self.engine_state = EngineState.FAILED
                self.profile_trigger_allowed = False
                self.engine_hint.setStyleSheet("color: #ff8496;")
                if residual_backend:
                    self.output_shutdown_in_progress = True
                    self.direct_interception_active = False
                    self.mappings_enabled = False
                    message += "；Interception 输出上下文仍在退出，请重试停止输入引擎"
                    if hasattr(self, "toggle_button"):
                        self.toggle_button.setText("重试停止输入引擎")
                        self.toggle_button.setObjectName("stop")
                    if hasattr(self, "write_diagnostic"):
                        self.write_diagnostic(
                            "set_running_game_backend_start_residual",
                            force=True,
                            interception_output_present=bool(
                                self.interception_output is not None
                            ),
                        )
                else:
                    self.running = False
                self.engine_hint.setText(message)
                QMessageBox.warning(self, "输入引擎启动失败", message)
        else:
            self._set_loading_message(
                "正在停止输入引擎", "正在停止宏、监听器并释放持有按键……"
            )
            game_mode = self._runtime_is_game_mode()
            was_running = bool(self.running)
            previous_gate = bool(self.output_shutdown_in_progress)
            previous_allowed = bool(self.profile_trigger_allowed)
            # Keep all output resources alive while task-owned Release packets
            # are still draining. Press/Tap are blocked by this gate.
            self.output_shutdown_in_progress = True
            self.profile_trigger_allowed = False

            # Stop the physical source layer before releasing held outputs.
            # Otherwise a still-held source key can race with cleanup and
            # immediately reactivate the target while the engine is stopping.
            if not game_mode and self.mappings_enabled:
                layer_disabled = self._change_runtime_profile_layer(
                    DISABLED_LAYER_NAME, wait=True
                )
                self.write_diagnostic(
                    "stop_layer_disabled_before_release",
                    force=True,
                    success=bool(layer_disabled),
                    error=str(getattr(self.engine, "last_command_error", "")),
                )
                if not layer_disabled:
                    # 旧来源层仍然有效时不能开始释放。否则物理按住中的来源
                    # 会在清理过程中再次生成目标 Down，形成释放/重按竞争。
                    self.output_shutdown_in_progress = previous_gate
                    self.profile_trigger_allowed = previous_allowed
                    self.engine_state = (
                        EngineState.RUNNING if was_running else self.engine_state
                    )
                    detail = (
                        self.engine.last_command_error
                        or self.keyboard_engine.last_command_error
                        or "Kanata 未确认禁用旧映射层"
                    )
                    self.engine_hint.setStyleSheet("color: #ff8496;")
                    self.engine_hint.setText(
                        "无法先禁用旧映射层，已取消本次停止以避免卡键"
                    )
                    QMessageBox.warning(
                        self,
                        "暂时无法安全停止",
                        "停止输入引擎前未能禁用当前映射层。程序没有继续释放或"
                        "销毁输入后端，以避免按住来源键在清理期间重新按下目标。\n\n"
                        + detail,
                    )
                    self.refresh_status_ui()
                    return False
            if allow_owned_mouse_force_release:
                self._start_stop_release_guard()
            # Preserve existing Release failures until the stop transaction
            # confirms that every owned output has been released.  Only failures
            # newly reported by stop_all_macros() abort before the final sweep.
            previous_stop_failures = list(getattr(self, "last_macro_release_failures", []) or [])
            remaining = self.stop_all_macros(keep_output_gate=True)
            stop_failures = list(getattr(self, "last_macro_release_failures", []) or [])
            new_stop_failures = [
                item for item in stop_failures if item not in previous_stop_failures
            ]
            if new_stop_failures:
                self.engine_state = EngineState.FAILED
                self.running = was_running
                self.output_shutdown_in_progress = True
                self.profile_trigger_allowed = False
                detail = (
                    "宏任务已停止，但部分输入未能确认释放。输入后端尚未销毁，"
                    "请先执行“强制释放键鼠”，然后再次停止输入引擎。"
                )
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText("宏输出释放失败；输入后端保持存活且新输出已禁止")
                self.write_diagnostic(
                    "set_running_stop_release_failed",
                    force=True,
                    failures=new_stop_failures,
                )
                QMessageBox.warning(
                    self,
                    "输入引擎尚未安全停止",
                    detail + "\n\n" + "、".join(new_stop_failures),
                )
                self.refresh_status_ui()
                return False
            if remaining:
                if allow_owned_mouse_force_release:
                    # Even if a worker cannot exit yet, the user's explicit stop
                    # must still clear any OS-level state whose bookkeeping was
                    # lost. New Press/Tap output is already gated above.
                    self._failsafe_release_runtime_targets(force_all=True)
                self.engine_state = EngineState.FAILED
                self.running = was_running
                detail = (
                    "仍有宏线程未能在安全期限内退出。输入后端尚未销毁，"
                    "以避免后台线程访问已释放资源。请等待任务退出后再次停止引擎。"
                )
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText("宏线程仍在退出；输入后端保持存活且新输出已禁止")
                self.write_diagnostic(
                    "set_running_stop_deferred",
                    force=True,
                    remaining_tasks=list(remaining),
                    task_details=self.macro_controller.remaining_task_details(),
                )
                QMessageBox.warning(self, "输入引擎尚未完全停止", detail)
                self.refresh_status_ui()
                return False

            self.running = False
            self.direct_interception_active = False
            with getattr(self, "input_state_lock", nullcontext()):
                self.held_trigger_ids.clear()
                self.kanata_trigger_down.clear()
                self.suppressed_trigger_names.clear()
                getattr(self, "interception_forwarded_down", set()).clear()
            with self.expected_kanata_event_lock:
                self.expected_kanata_events.clear()
            clear_hotkey_latches = getattr(
                self, "_clear_system_hotkey_latches", None
            )
            if callable(clear_hotkey_latches):
                clear_hotkey_latches()
            else:
                self.system_hotkey_latched.clear()
                getattr(self, "system_hotkey_latched_sources", {}).clear()
            self.recording_control_modifiers.clear()
            getattr(self, "recording_control_sources", {}).clear()
            # Keep the toggle owner across the stop transaction so the physical
            # release is consumed by the same source that initiated the stop.
            if hasattr(self, "activity_overlay"):
                self.activity_overlay.hide_message()

            stop_errors = []
            if allow_owned_mouse_force_release:
                # Independent final safety net: release only currently owned concrete
                # outputs after the virtual-key/backend release paths have run.
                self._failsafe_release_runtime_targets(force_all=True)

            if game_mode:
                # Keep the persistent source listener, but demote it to the
                # control-only mode after the business output context is gone.
                self.interception_input_control_only = True
                self.interception_control_modifiers.clear()
                getattr(self, "interception_control_sources", {}).clear()
                if self.interception_output:
                    output = self.interception_output
                    try:
                        stopped = bool(output.stop())
                    except Exception as error:
                        stopped = False
                        stop_errors.append(f"销毁 Interception 输出上下文：{error}")
                    if (
                        not stopped
                        and allow_owned_mouse_force_release
                        and output.pending_release_summary()["quarantined_mouse"]
                    ):
                        self.write_diagnostic(
                            "explicit_stop_forcing_owned_mouse_release",
                            force=True,
                            pending=output.pending_release_summary(),
                        )
                        try:
                            stopped = bool(
                                output.release_all(force=True) and output.stop()
                            )
                        except Exception as error:
                            stopped = False
                            stop_errors.append(
                                f"显式停止时强制释放程序持有的鼠标状态：{error}"
                            )
                    if stopped and self.interception_output is output:
                        self.interception_output = None
                    elif not stopped and not any(
                        item.startswith("销毁 Interception 输出上下文")
                        for item in stop_errors
                    ):
                        pending = output.pending_release_summary()
                        if pending["quarantined_mouse"]:
                            stop_errors.append(
                                "Interception 鼠标输出正在等待回到按下时窗口后安全释放；"
                                "也可使用“强制释放键鼠”后重试停止"
                            )
                        elif pending["keys"] or pending["mouse"]:
                            stop_errors.append(
                                "Interception 持有输出未能释放，状态已保留供再次重试"
                            )
                        else:
                            stop_errors.append("Interception 输出上下文销毁失败")
            else:
                if self.interception_output:
                    output = self.interception_output
                    try:
                        stopped = bool(output.stop())
                    except Exception as error:
                        stopped = False
                        stop_errors.append(f"销毁 Interception 输出上下文：{error}")
                    if (
                        not stopped
                        and allow_owned_mouse_force_release
                        and output.pending_release_summary()["quarantined_mouse"]
                    ):
                        self.write_diagnostic(
                            "explicit_stop_forcing_owned_mouse_release",
                            force=True,
                            pending=output.pending_release_summary(),
                        )
                        try:
                            stopped = bool(
                                output.release_all(force=True) and output.stop()
                            )
                        except Exception as error:
                            stopped = False
                            stop_errors.append(
                                f"显式停止时强制释放程序持有的鼠标状态：{error}"
                            )
                    if stopped and self.interception_output is output:
                        self.interception_output = None
                    elif not stopped and not any(
                        item.startswith("销毁 Interception 输出上下文")
                        for item in stop_errors
                    ):
                        pending = output.pending_release_summary()
                        if pending["quarantined_mouse"]:
                            stop_errors.append(
                                "Interception 鼠标输出正在等待回到按下时窗口后安全释放"
                            )
                        elif pending["keys"] or pending["mouse"]:
                            stop_errors.append(
                                "Interception 持有输出未能释放，状态已保留供再次重试"
                            )
                        else:
                            stop_errors.append("Interception 输出上下文销毁失败")
                if self.interception_input_hook and not self._stop_interception_input_hook(
                    timeout=1.5
                ):
                    stop_errors.append("Interception 输入监听线程停止超时")

            if not game_mode and self._kanata_engine_has_runtime(
                self.keyboard_engine
            ):
                try:
                    if not self.keyboard_engine.stop(timeout=3.0):
                        stop_errors.append("辅助 Kanata 停止超时")
                except Exception as error:
                    stop_errors.append(f"停止辅助 Kanata：{error}")
            if not game_mode and self._kanata_engine_has_runtime(self.engine):
                try:
                    if not self.engine.stop(timeout=3.0):
                        stop_errors.append("主 Kanata 停止超时")
                except Exception as error:
                    stop_errors.append(f"停止主 Kanata：{error}")

            if allow_owned_mouse_force_release:
                # Repeat once after backend shutdown to cover a late Release
                # racing with the first pass. Press/Tap have been gated since
                # the beginning of this transaction, so no new owned Down can
                # legitimately appear here.
                if not self._failsafe_release_runtime_targets(force_all=True):
                    stop_errors.append("程序持有输入的系统级兜底释放未完全成功")

            if not self.update_global_hook_for_backend():
                stop_errors.append("全局控制监听未能恢复")
            else:
                self._reseed_physical_input_state(
                    seed_control=bool(
                        game_mode and self.runtime_global_toggle_enabled
                    )
                )

            backend_alive = bool(
                self.interception_output is not None
                or (
                    not game_mode
                    and self.interception_input_hook is not None
                )
                or self._kanata_engine_has_runtime(self.keyboard_engine)
                or self._kanata_engine_has_runtime(self.engine)
            )
            if stop_errors:
                self.running = backend_alive
                self.engine_state = EngineState.FAILED
                self.output_shutdown_in_progress = bool(backend_alive)
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(
                    "输入引擎停止不完整；残留后端已禁止新输出，请再次停止"
                    if backend_alive else "输入引擎已停止，但控制监听恢复失败"
                )
                self.toggle_button.setText(
                    "重试停止输入引擎" if backend_alive else "启动输入引擎"
                )
                self.toggle_button.setObjectName(
                    "stop" if backend_alive else "primary"
                )
                self.write_diagnostic(
                    "set_running_stop_failed",
                    force=True,
                    errors=stop_errors,
                    backend_alive=backend_alive,
                    pending_interception=(
                        self.interception_output.pending_release_summary()
                        if self.interception_output is not None else {}
                    ),
                )
                QMessageBox.warning(
                    self,
                    "输入引擎停止不完整",
                    "关闭部分输入资源时发生问题：\n"
                    + "\n".join(f"- {item}" for item in stop_errors),
                )
                self.refresh_status_ui()
                return False

            self.engine_state = EngineState.STOPPED
            clear_process_guard = getattr(
                self, "_clear_process_guard_input_state", None
            )
            if callable(clear_process_guard):
                clear_process_guard()
            with getattr(self, "input_state_lock", nullcontext()):
                self.active_sync_by_source.clear()
            with self.sync_output_lock:
                self.sync_output_counts.clear()
            self.runtime_release_target_history.clear()
            self.runtime_release_vkey_history.clear()
            self.last_macro_release_failures = []
            self.active_macro_id = None
            self.last_action_activity = {}
            self.macro_state = MacroState.IDLE
            self.macro_status_detail = ""
            self.write_diagnostic(
                "set_running_stopped",
                kanata_engine=self.engine.is_running(),
                keyboard_kanata_engine=self.keyboard_engine.is_running(),
                interception_input_hook=self.interception_input_hook is not None,
                global_hook=self.global_hook is not None,
            )
            self.toggle_button.setText("启动输入引擎")
            self.toggle_button.setObjectName("primary")
            self.output_shutdown_in_progress = False
            self.macrocanvas_foreground_suspend_failed = False

        for widget in (self.toggle_button,):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.refresh_status_ui()
