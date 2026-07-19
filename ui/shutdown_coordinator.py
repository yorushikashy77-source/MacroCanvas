"""Coordinated, observable shutdown for MacroCanvas runtime resources."""

from __future__ import annotations

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QMessageBox

from core.constants import ConfigState, EngineState


class ShutdownCoordinatorMixin:
    """Stop workers before destroying the input resources they may still use."""

    def _shutdown_quarantined_mouse_names(self):
        names = []
        output = getattr(self, "interception_output", None)
        if output is not None:
            try:
                summary = output.pending_release_summary()
                names.extend(summary.get("quarantined_mouse", []) or [])
            except (AttributeError, RuntimeError):
                pass
        lock = getattr(self, "quarantined_mouse_release_lock", None)
        entries = getattr(self, "quarantined_mouse_releases", [])
        try:
            if lock is None:
                snapshot = list(entries)
            else:
                with lock:
                    snapshot = list(entries)
            names.extend(
                str(item.get("action", {}).get("target") or "鼠标按键")
                for item in snapshot
            )
        except (AttributeError, RuntimeError, TypeError):
            pass
        return sorted(set(str(name) for name in names if name))

    def _offer_forced_mouse_release_for_shutdown(self):
        names = self._shutdown_quarantined_mouse_names()
        if not names:
            return True
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("鼠标按键等待跨窗口释放")
        confirm.setText("程序仍持有在其他窗口中按下的鼠标按键。")
        confirm.setInformativeText(
            "为避免当前窗口意外收到鼠标弹起，自动退出已暂停。可以强制释放"
            "这些按键并继续退出；也可以暂不退出，返回原目标窗口后再重试。\n\n"
            "等待释放：" + "、".join(names)
        )
        force_button = confirm.addButton(
            "强制释放并继续退出", QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_button = confirm.addButton(
            "暂不退出", QMessageBox.ButtonRole.RejectRole
        )
        confirm.setDefaultButton(cancel_button)
        confirm.exec()
        if confirm.clickedButton() is not force_button:
            return False
        return bool(self.force_release_held_inputs(
            show_feedback=False,
            _cross_window_release_confirmed=True,
        ))

    def _shutdown_issue(self, issues, step, error=None, detail="", critical=False):
        message = detail or (str(error) if error is not None else "未知错误")
        item = {
            "step": str(step),
            "message": str(message),
            "critical": bool(critical),
        }
        issues.append(item)
        try:
            self.write_diagnostic(
                "shutdown_step_failed",
                force=True,
                step=item["step"],
                error=item["message"],
                critical=item["critical"],
            )
        except Exception:
            # Diagnostics must never replace the original shutdown failure.
            pass
        return item

    def _shutdown_call(self, issues, step, function, *, critical=False):
        try:
            result = function()
        except Exception as error:
            self._shutdown_issue(
                issues, step, error=error, critical=critical
            )
            return False, None
        if result is False:
            self._shutdown_issue(
                issues,
                step,
                detail="操作返回失败",
                critical=critical,
            )
            return False, result
        return True, result

    def _stop_macro_runtime_for_shutdown(self, issues):
        """Drain main macro threads and every tracked parallel worker."""
        try:
            remaining = self.macro_controller.stop_all(timeout=2.5)
            release_failures = list(getattr(
                self.macro_controller, "last_release_failures", []
            ) or [])
            for item in self.macro_controller.force_release_all():
                if item not in release_failures:
                    release_failures.append(item)
            if release_failures:
                self._shutdown_issue(
                    issues,
                    "释放宏任务持有输入",
                    detail=f"释放失败任务：{release_failures}",
                    critical=True,
                )
        except Exception as error:
            self._shutdown_issue(
                issues, "停止宏任务", error=error, critical=True
            )
            return False

        if remaining:
            # Backend calls use their own bounded waits. Give in-flight cleanup
            # a longer final window while Release can still reach the backend.
            remaining = self.macro_controller.wait_for_all(timeout=6.0)
            late_release_failures = list(getattr(
                self.macro_controller, "last_release_failures", []
            ) or [])
            late_only = [
                item for item in late_release_failures
                if item not in release_failures
            ]
            if late_only:
                release_failures.extend(late_only)
                self._shutdown_issue(
                    issues,
                    "释放宏任务持有输入",
                    detail=f"释放失败任务：{late_release_failures}",
                    critical=True,
                )
        if remaining:
            details = self.macro_controller.remaining_task_details()
            self._shutdown_issue(
                issues,
                "等待宏线程退出",
                detail=f"残留任务：{remaining}；线程：{details}",
                critical=True,
            )
            return False
        return True

    def _stop_global_hook_for_shutdown(self, issues):
        hook = getattr(self, "global_hook", None)
        if hook is None:
            return True
        try:
            stopped = bool(hook.stop(timeout=1.5))
            if not stopped:
                stopped = bool(hook.stop(timeout=3.0))
        except Exception as error:
            self._shutdown_issue(
                issues, "停止Windows输入监听", error=error, critical=True
            )
            return False
        if not stopped:
            self._shutdown_issue(
                issues,
                "停止Windows输入监听",
                detail=getattr(hook, "last_stop_warning", "监听线程停止超时"),
                critical=True,
            )
            return False
        if self.global_hook is hook:
            self.global_hook = None
        return True

    def _stop_interception_hook_for_shutdown(self, issues):
        if getattr(self, "interception_input_hook", None) is None:
            return True
        try:
            stopped = bool(self._stop_interception_input_hook(timeout=1.5))
            if not stopped:
                stopped = bool(self._stop_interception_input_hook(timeout=4.0))
        except Exception as error:
            self._shutdown_issue(
                issues, "停止Interception输入监听", error=error, critical=True
            )
            return False
        if not stopped:
            hook = getattr(self, "interception_input_hook", None)
            self._shutdown_issue(
                issues,
                "停止Interception输入监听",
                detail=(
                    getattr(hook, "last_stop_warning", "")
                    or "Interception输入线程停止超时"
                ),
                critical=True,
            )
            return False
        return True

    def _enter_shutdown_failed_state(self, issues):
        """Latch a non-interactive state after partial shutdown cannot roll back safely."""
        self._shutdown_started = True
        self._shutdown_in_progress = False
        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        if hasattr(self, "profile_timer"):
            self.profile_timer.stop()
        if hasattr(self, "auto_apply_timer"):
            self.auto_apply_timer.stop()

        try:
            central = self.centralWidget()
        except Exception:
            central = None
        if central is not None:
            central.setEnabled(False)
        try:
            menu_bar = self.menuBar()
        except Exception:
            menu_bar = None
        if menu_bar is not None:
            menu_bar.setEnabled(False)
        try:
            self.refresh_status_ui()
        except Exception:
            pass
        if hasattr(self, "engine_hint"):
            self.engine_hint.setStyleSheet("color: #ff8496;")
            self.engine_hint.setText(
                "安全退出未完成；主界面已锁定。再次关闭窗口可重试，"
                "并可选择强制释放跨窗口鼠标按键"
            )
        try:
            self.write_diagnostic(
                "shutdown_failed_state_latched",
                force=True,
                issues=issues,
            )
        except Exception:
            pass

    def _fail_shutdown(self, issues):
        self._finalize_shutdown_diagnostics(issues, complete=False)
        self._enter_shutdown_failed_state(issues)
        return False

    def _finalize_shutdown_diagnostics(self, issues, complete):
        event = "shutdown_complete"
        if issues and complete:
            event = "shutdown_completed_with_warnings"
        elif not complete:
            event = "shutdown_failed"
        try:
            self.write_diagnostic(
                event,
                force=True,
                issues=issues,
                held_inputs=self.held_input_snapshot(),
            )
            self._flush_diagnostic_queue(timeout=1.5)
            with self.diagnostic_lock:
                self._trim_diagnostic_log_locked()
            if complete:
                self._stop_diagnostic_writer(timeout=1.5)
        except Exception:
            pass

    @Slot()
    def shutdown(self):
        """Return True only after every critical worker and backend has stopped."""
        if getattr(self, "_shutdown_complete", False):
            return True
        if getattr(self, "_shutdown_in_progress", False):
            return False

        self._shutdown_in_progress = True
        self._shutdown_started = True
        self._shutdown_errors = []
        issues = self._shutdown_errors
        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        # Capture concrete ownership before task/backend cleanup erases its tables.
        # The final SendInput fallback is restricted to these program-owned keys;
        # mouse buttons remain on their context-aware backend release path.
        shutdown_owned_keys = self._owned_output_names_snapshot(
            include_mouse=False
        )
        self._shutdown_owned_release_names = list(shutdown_owned_keys)

        if hasattr(self, "profile_timer"):
            self.profile_timer.stop()
        if hasattr(self, "auto_apply_timer"):
            self.auto_apply_timer.stop()
        self.recording_session_active = False
        self.recording = False
        self.recording_restore_pending = False
        self.recording_workflow_complete = False
        self.recording_restore_layer = None
        self.recording_generation = int(
            getattr(self, "recording_generation", 0)
        ) + 1
        self._macro_stop_gate_restore = None
        self._deferred_profile_input_restore = None

        # Keep the live backend ownership flags intact until every macro worker
        # has completed its final Release calls.  Worker cleanup consults
        # _runtime_is_game_mode(); clearing these flags early would make it read
        # the editable backend combo from a non-GUI thread and could route the
        # final release through the wrong backend.
        macros_stopped = self._stop_macro_runtime_for_shutdown(issues)
        if not macros_stopped:
            return self._fail_shutdown(issues)
        if self._shutdown_quarantined_mouse_names():
            if not self._offer_forced_mouse_release_for_shutdown():
                if not any(
                    item.get("step") == "等待跨窗口鼠标释放"
                    for item in issues
                ):
                    self._shutdown_issue(
                        issues,
                        "等待跨窗口鼠标释放",
                        detail="用户暂未确认跨窗口强制释放",
                        critical=True,
                    )
                return self._fail_shutdown(issues)
            # The confirmed force-release verifies all program-owned output.
            issues[:] = [
                item for item in issues
                if item.get("step") != "释放宏任务持有输入"
            ]

        self.running = False
        self.direct_interception_active = False

        self._shutdown_call(
            issues,
            "释放同步映射",
            self._release_all_sync_mappings,
            critical=True,
        )
        self._shutdown_call(
            issues,
            "释放Interception输出",
            self._release_interception_output,
            critical=True,
        )
        self._shutdown_call(
            issues,
            "释放主Kanata虚拟键",
            lambda: self.engine.release_all_virtual_keys(timeout=1.2)
            if self._kanata_engine_has_runtime(self.engine) else True,
            critical=True,
        )
        self._shutdown_call(
            issues,
            "释放辅助Kanata虚拟键",
            lambda: self.keyboard_engine.release_all_virtual_keys(timeout=1.2)
            if self._kanata_engine_has_runtime(self.keyboard_engine) else True,
            critical=True,
        )

        # Input callbacks retain references to the window and runtime state. Do
        # not destroy their dependencies until both listener threads are gone.
        hooks_stopped = self._stop_global_hook_for_shutdown(issues)
        hooks_stopped = (
            self._stop_interception_hook_for_shutdown(issues)
            and hooks_stopped
        )
        if not hooks_stopped:
            return self._fail_shutdown(issues)

        # No task or listener is now allowed to create/use another output context.
        self.output_backend_retired = True

        output = getattr(self, "interception_output", None)
        if output is not None:
            ok, _ = self._shutdown_call(
                issues,
                "销毁Interception输出上下文",
                output.stop,
                critical=True,
            )
            if ok and self.interception_output is output:
                self.interception_output = None

        keyboard_ok, _ = self._shutdown_call(
            issues,
            "停止辅助Kanata",
            lambda: self.keyboard_engine.stop(timeout=3.0),
            critical=True,
        )
        main_ok, _ = self._shutdown_call(
            issues,
            "停止主Kanata",
            lambda: self.engine.stop(timeout=3.0),
            critical=True,
        )

        # Final SendInput recovery is bounded to keys that MacroCanvas actually
        # owned before cleanup. Do not emit unrelated keyboard or mouse Up events.
        self._shutdown_call(
            issues,
            "系统级最终持有键释放",
            lambda: self._force_release_system_inputs(
                names=shutdown_owned_keys
            ),
            critical=True,
        )

        try:
            if hasattr(self, "activity_overlay"):
                self.activity_overlay.hide_message()
                self.activity_overlay.close()
        except Exception as error:
            self._shutdown_issue(issues, "关闭状态浮窗", error=error)

        critical_failed = any(item.get("critical") for item in issues)
        complete = bool(keyboard_ok and main_ok and not critical_failed)
        self._shutdown_complete = complete
        if complete:
            if hasattr(self, "dispose_system_tray"):
                self.dispose_system_tray()
            self._shutdown_in_progress = False
            self.engine_state = EngineState.STOPPED
            self._finalize_shutdown_diagnostics(issues, complete=True)
            return True
        return self._fail_shutdown(issues)

    def shutdown_error_summary(self):
        issues = getattr(self, "_shutdown_errors", []) or []
        if not issues:
            return "关闭流程尚未完成，请稍后重试。"
        return "\n".join(
            f"• {item.get('step')}：{item.get('message')}"
            for item in issues
        )

    def emergency_shutdown_fallback(self):
        """Atexit fallback that never creates new runtime resources."""
        if getattr(self, "_shutdown_complete", False):
            return
        self.output_shutdown_in_progress = True
        self.output_backend_retired = True
        try:
            self._release_interception_output()
        except Exception:
            pass
        try:
            self._force_release_system_inputs(
                names=getattr(self, "_shutdown_owned_release_names", None)
            )
        except Exception:
            pass
        for engine_name in ("keyboard_engine", "engine"):
            engine = getattr(self, engine_name, None)
            if engine is None:
                continue
            try:
                engine._stop_stale_app_instances()
            except Exception:
                pass

    def _has_unapplied_changes_for_shutdown(self):
        if self.config_state == ConfigState.DIRTY:
            return True
        if self.config_state != ConfigState.FAILED:
            return False
        try:
            return self.current_config_signature() != self.applied_config_signature
        except Exception:
            return True

    def _cancel_silent_tray_exit(self, event, reason):
        self._tray_exit_requested = False
        notice = getattr(self, "show_tray_exit_blocked_notice", None)
        if callable(notice):
            notice(reason)
        event.ignore()

    def _close_from_system_tray_silently(self, event):
        """Exit without foregrounding the main window or opening destructive dialogs."""
        if getattr(self, "recording_session_active", False):
            self._cancel_silent_tray_exit(
                event, "录制尚未结束，未自动退出以避免丢失本次录制。"
            )
            return
        if self._has_unapplied_changes_for_shutdown():
            self._cancel_silent_tray_exit(
                event, "存在未应用的修改，未自动退出以避免丢失配置。"
            )
            return
        if self._shutdown_quarantined_mouse_names():
            self._cancel_silent_tray_exit(
                event, "有鼠标按键正等待回到原窗口释放，未自动退出。"
            )
            return
        if not self.shutdown():
            self._cancel_silent_tray_exit(
                event, "关键线程或输入资源尚未安全停止，程序仍在托盘中运行。"
            )
            return
        event.accept()

    def closeEvent(self, event):
        if (
            hasattr(self, "should_hide_close_to_tray")
            and self.should_hide_close_to_tray()
        ):
            self.hide_close_to_tray(event)
            return
        if getattr(self, "_tray_exit_requested", False):
            self._close_from_system_tray_silently(event)
            return
        if (
            getattr(self, "_shutdown_started", False)
            and not getattr(self, "_shutdown_complete", False)
        ):
            if not self.shutdown():
                QMessageBox.critical(
                    self,
                    "暂时无法安全退出",
                    "关键线程或输入资源仍未全部停止。\n\n"
                    + self.shutdown_error_summary()
                    + "\n\n主界面保持锁定，请通过窗口关闭按钮继续重试退出。",
                )
                event.ignore()
                return
            event.accept()
            return

        if getattr(self, "recording_session_active", False):
            confirm = QMessageBox(self)
            confirm.setIcon(QMessageBox.Icon.Warning)
            confirm.setWindowTitle("录制尚未结束")
            if getattr(self, "recording", False):
                confirm.setText("当前仍在录制键鼠动作。")
                confirm.setInformativeText(
                    "可以先完成并整理录制结果，再继续退出；也可以放弃本次录制。"
                )
                finish_button = confirm.addButton(
                    "完成录制并继续退出", QMessageBox.ButtonRole.AcceptRole
                )
            else:
                confirm.setText("录制仍处于开始倒计时。")
                confirm.setInformativeText(
                    "倒计时阶段尚无可保存的录制结果。可以放弃本次录制，或取消退出。"
                )
                finish_button = None
            discard_recording_button = confirm.addButton(
                "放弃录制并退出", QMessageBox.ButtonRole.DestructiveRole
            )
            cancel_close_button = confirm.addButton(
                "取消退出", QMessageBox.ButtonRole.RejectRole
            )
            confirm.setDefaultButton(cancel_close_button)
            confirm.exec()
            clicked = confirm.clickedButton()
            if clicked is cancel_close_button or clicked is None:
                event.ignore()
                return
            if finish_button is not None and clicked is finish_button:
                self.finish_recording()
                if getattr(self, "recording_session_active", False):
                    QMessageBox.information(
                        self,
                        "录制尚未完成",
                        "录制结果仍在处理，或录制控制键尚未松开。请稍后再次关闭程序。",
                    )
                    event.ignore()
                    return
            elif clicked is discard_recording_button:
                self.cancel_recording()
            else:
                event.ignore()
                return

        has_unapplied_changes = self._has_unapplied_changes_for_shutdown()
        if has_unapplied_changes:
            confirm = QMessageBox(self)
            confirm.setIcon(QMessageBox.Icon.Warning)
            confirm.setWindowTitle("存在未应用修改")
            confirm.setText("当前配置仍有未保存或未应用的修改。")
            confirm.setInformativeText(
                "请选择应用并退出、放弃全部尚未保存的修改，或取消本次退出。"
            )
            apply_button = confirm.addButton(
                "应用并退出", QMessageBox.ButtonRole.AcceptRole
            )
            discard_button = confirm.addButton(
                "放弃修改", QMessageBox.ButtonRole.DestructiveRole
            )
            cancel_button = confirm.addButton(
                "取消退出", QMessageBox.ButtonRole.RejectRole
            )
            confirm.setDefaultButton(cancel_button)
            confirm.exec()
            clicked = confirm.clickedButton()
            if clicked is cancel_button or clicked is None:
                event.ignore()
                return
            if clicked is apply_button:
                self.apply_changes()
                if self.config_state in (ConfigState.DIRTY, ConfigState.FAILED):
                    QMessageBox.warning(
                        self,
                        "无法退出",
                        "配置未能成功应用，程序将保持打开以避免静默丢失修改。",
                    )
                    event.ignore()
                    return
            elif clicked is not discard_button:
                event.ignore()
                return

        for card in getattr(self, "preset_cards", []):
            if hasattr(card, "action_dialog"):
                card.action_dialog.close()
        if not self.shutdown():
            QMessageBox.critical(
                self,
                "暂时无法安全退出",
                "仍有关键线程或输入资源未停止，程序没有销毁输入后端。\n\n"
                + self.shutdown_error_summary()
                + "\n\n主界面已锁定以避免继续操作不完整的运行资源。"
                "请通过窗口关闭按钮重试退出。",
            )
            event.ignore()
            return
        event.accept()
