"""UI-facing macro activity and stop/pause controls."""

import inspect
import time
from datetime import datetime

from PySide6.QtCore import QTimer, Slot
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QMessageBox, QPushButton,
    QSystemTrayIcon, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from core.constants import MacroState
from ui.runtime_guards import (
    explain_runtime_cleanup_block, macro_control_transaction_busy,
    runtime_cleanup_blocks_new_output,
)


class MacroControlsMixin:
    MACRO_STOP_TIMEOUT_SECONDS = 2.0
    MACRO_RUN_HISTORY_LIMIT = 120

    @staticmethod
    def _macro_finish_summary(task):
        reason = str(getattr(task, "finish_reason", "") or "completed")
        labels = {
            "completed": ("完成", "正常完成"),
            "stopped": ("已停止", "由用户或运行状态停止"),
            "empty": ("失败", "没有可执行动作"),
            "runtime_limit": ("失败", "达到最长运行时间"),
            "backend_inactive": ("失败", "输入后端不可用"),
            "trigger_release_cancelled": ("已停止", "等待触发键释放时结束"),
            "condition_timeout": ("失败", "等待条件超时"),
            "submacro_unavailable": ("失败", "子宏不可用或形成循环"),
            "action_failed": ("失败", "动作执行失败"),
            "release_failed": ("失败", "结束时按键释放失败"),
        }
        return labels.get(reason, ("失败", reason))

    def _record_macro_run_history(self, task, preset_id=""):
        """Keep a short in-memory history without recording raw input events."""
        if task is None:
            return None
        preset = dict(getattr(task, "preset", {}) or {})
        task_id = str(preset.get("id") or preset_id or "")
        if task_id.startswith("mapping:"):
            return None
        finished_at = time.time()
        started_at = float(getattr(task, "history_started_at", 0.0) or finished_at)
        status, detail = self._macro_finish_summary(task)
        origin_id = str(preset.get("_origin_preset_id") or task_id)
        action_context = dict(getattr(task, "last_action_context", {}) or {})
        failure_action = (
            str(action_context.get("action") or "")
            if status == "失败" else ""
        )
        entry = {
            "finished_at": finished_at,
            "preset_id": origin_id,
            "preset_name": str(preset.get("name") or "宏任务"),
            "source": str(
                preset.get("_history_source") or "快捷键触发"
            ),
            "status": status,
            "detail": detail,
            "duration_ms": max(0, round((finished_at - started_at) * 1000)),
            "failure_action": failure_action,
            "action_preset_id": str(
                action_context.get("source_preset_id") or origin_id
            ),
            "action_id": str(action_context.get("action_id") or ""),
        }
        history = list(getattr(self, "macro_run_history", []) or [])
        history.insert(0, entry)
        del history[self.MACRO_RUN_HISTORY_LIMIT:]
        self.macro_run_history = history
        return entry

    def clear_macro_run_history(self):
        self.macro_run_history = []
        dialog = getattr(self, "macro_run_history_dialog", None)
        if dialog is not None:
            dialog.close()

    def open_macro_run_history(self):
        dialog = QDialog(self)
        self.macro_run_history_dialog = dialog
        dialog.setWindowTitle("宏运行历史")
        dialog.resize(920, 520)
        layout = QVBoxLayout(dialog)
        hint = QTreeWidget()
        hint.setObjectName("macroRunHistory")
        hint.setColumnCount(7)
        hint.setHeaderLabels([
            "结束时间", "宏", "触发来源", "结果", "耗时", "问题动作", "说明",
        ])
        hint.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        hint.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hint.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hint.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hint.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hint.header().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hint.header().setSectionResizeMode(5, QHeaderView.Stretch)
        hint.header().setSectionResizeMode(6, QHeaderView.Stretch)
        history = list(getattr(self, "macro_run_history", []) or [])
        for entry in history:
            timestamp = datetime.fromtimestamp(entry["finished_at"]).strftime(
                "%H:%M:%S"
            )
            row = QTreeWidgetItem([
                timestamp, entry["preset_name"], entry["source"],
                entry["status"], f"{entry['duration_ms']} ms",
                entry.get("failure_action", "—") or "—", entry["detail"],
            ])
            row.setData(0, 32, entry.get("action_preset_id") or entry["preset_id"])
            row.setData(1, 32, entry.get("action_id", ""))
            hint.addTopLevelItem(row)
        if not history:
            hint.addTopLevelItem(QTreeWidgetItem([
                "—", "—", "—", "暂无记录", "—", "—",
                "本次启动后完成的宏会显示在这里。",
            ]))
        layout.addWidget(hint, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        clear = QPushButton("清空")
        clear.setObjectName("dangerGhost")
        buttons.addButton(clear, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(dialog.reject)
        clear.clicked.connect(self.clear_macro_run_history)

        def locate_selected(*_args):
            row = hint.currentItem()
            preset_id = str(row.data(0, 32) or "") if row is not None else ""
            action_id = str(row.data(1, 32) or "") if row is not None else ""
            card = next(
                (item for item in getattr(self, "preset_cards", [])
                 if str(item.preset_id) == preset_id),
                None,
            )
            if card is not None:
                dialog.accept()
                self.open_preset_actions_dialog(card)
                focus = getattr(self, "_focus_submacro_overview_action", None)
                if callable(focus) and action_id:
                    focus(card, action_id)

        hint.itemDoubleClicked.connect(locate_selected)
        layout.addWidget(buttons)
        dialog.exec()
        self.macro_run_history_dialog = None

    def _macro_callback_blocked_by_shutdown(self):
        """Keep delayed task callbacks from reopening output during shutdown."""
        if not getattr(self, "_shutdown_started", False):
            return False
        self.output_shutdown_in_progress = True
        self._macro_stop_gate_restore = None
        self._deferred_profile_input_restore = None
        return True

    def _mark_macro_stop_started(self):
        self._macro_stop_started_at = time.perf_counter()

    def _clear_macro_stop_started(self):
        self._macro_stop_started_at = 0.0

    def _macro_stop_timed_out(self):
        started = float(getattr(self, "_macro_stop_started_at", 0.0) or 0.0)
        if not started:
            return False
        return (time.perf_counter() - started) >= self.MACRO_STOP_TIMEOUT_SECONDS

    def _set_macro_stop_waiting_display(self, remaining_count, cleanup_failures=None):
        remaining_count = max(1, int(remaining_count or 1))
        timed_out = self._macro_stop_timed_out()
        self.macro_state = MacroState.STOP_TIMEOUT if timed_out else MacroState.STOPPING
        self.macro_status_detail = (
            f"仍有 {remaining_count} 个任务退出超时"
            if timed_out else f"仍有 {remaining_count} 个任务正在退出"
        )
        if hasattr(self, "execution_info"):
            self.execution_info.setText(
                "停止等待超时；后台任务已禁止新输出，正在继续退出"
                if timed_out else "正在等待宏任务退出；已暂时禁止新的输出"
            )
        if hasattr(self, "engine_hint"):
            self.engine_hint.setStyleSheet("color: #fbbf24;")
            text = (
                "宏停止等待超时；任务已收到停止信号，正在继续清理"
                if timed_out else "宏任务正在退出；已禁止新输出并等待按键释放"
            )
            if cleanup_failures:
                text += "；且部分按键释放失败"
            self.engine_hint.setText(text)

    def _show_macro_cleanup_failure(self, title, failures=None):
        failures = [str(item) for item in (failures or []) if str(item)]
        detail = title
        if failures:
            detail = f"{title}：{'、'.join(failures)}"
        self.macro_state = MacroState.STOP_TIMEOUT
        self.macro_status_detail = title
        if hasattr(self, "engine_hint"):
            self.engine_hint.setStyleSheet("color: #ff8496;")
            self.engine_hint.setText(detail)
        if hasattr(self, "execution_info"):
            self.execution_info.setText(
                f"{detail}。请再次急停，或停止输入引擎以继续释放。"
            )
        if hasattr(self, "activity_overlay") and not getattr(
            self, "recording_session_active", False
        ):
            self.activity_overlay.show_message(
                "按键释放未完成", detail, "#fb7185"
            )
        if hasattr(self, "write_diagnostic"):
            self.write_diagnostic(
                "macro_cleanup_failed",
                force=True,
                title=title,
                failures=failures,
            )

    def _remember_macro_cleanup_failure(self, title, failures=None):
        """Latch a failed Release so no later state path silently reopens output."""
        remembered = list(getattr(self, "last_macro_release_failures", []) or [])
        for item in (failures or []):
            text = str(item or "").strip()
            if text and text not in remembered:
                remembered.append(text)
        self.last_macro_release_failures = remembered
        self.output_shutdown_in_progress = True
        if getattr(self, "_macro_stop_gate_restore", None) is not None:
            self._macro_stop_gate_restore = None
        if title:
            self.macro_state = MacroState.STOP_TIMEOUT
            self.macro_status_detail = str(title)
        return remembered

    def _runtime_cleanup_blocks_new_output(self):
        return runtime_cleanup_blocks_new_output(self)

    @staticmethod
    def _format_virtual_screen_geometry(value):
        try:
            left, top, width, height = (int(item) for item in value)
        except (TypeError, ValueError, OverflowError):
            return "无法读取"
        return f"左上角 ({left}, {top})，{width} × {height}"

    def _recorded_mouse_context_message(self, issue):
        issue = issue or {}
        preset_name = str(issue.get("preset_name") or "该宏")
        kind = str(issue.get("kind") or "screen")
        if kind == "monitor_count":
            expected = issue.get("expected")
            current = issue.get("current")
            return (
                f"“{preset_name}”包含按显示器数量录制的鼠标坐标。\n\n"
                f"录制时：{expected} 台显示器\n"
                f"当前：{current if current is not None else '无法读取'} 台显示器\n\n"
                "为避免鼠标落到错误位置，本次没有开始执行。请恢复录制时的"
                "显示器布局，或在当前布局下重新录制该宏。"
            )
        expected = self._format_virtual_screen_geometry(issue.get("expected"))
        current = self._format_virtual_screen_geometry(issue.get("current"))
        return (
            f"“{preset_name}”包含按绝对屏幕坐标录制的鼠标移动。\n\n"
            f"录制时的屏幕布局：{expected}\n"
            f"当前屏幕布局：{current}\n\n"
            "为避免鼠标落到错误位置，本次没有开始执行。请恢复录制时的"
            "显示布局，或在当前布局下重新录制该宏。"
        )

    def _show_recorded_mouse_context_issue(self, preset, issue, modal=True):
        payload = dict(issue or {})
        payload.setdefault("preset_name", str((preset or {}).get("name") or "宏任务"))
        message = self._recorded_mouse_context_message(payload)
        if hasattr(self, "write_diagnostic"):
            self.write_diagnostic(
                "recorded_mouse_context_start_blocked",
                force=True,
                preset_id=str((preset or {}).get("id") or ""),
                preset_name=payload["preset_name"],
                source=payload.get("source") or "menu",
                issue=payload,
            )
        if hasattr(self, "engine_hint"):
            self.engine_hint.setStyleSheet("color: #fbbf24;")
            self.engine_hint.setText("宏未执行：录制时的屏幕布局与当前不一致")
        if hasattr(self, "execution_info"):
            self.execution_info.setText(
                "已阻止执行：请恢复录制时的显示布局，或重新录制该宏"
            )
        if hasattr(self, "activity_overlay") and not getattr(
            self, "recording_session_active", False
        ):
            self.activity_overlay.show_message(
                "宏未执行 · 屏幕布局不一致", "请查看提示并重新录制或恢复显示布局", "#fbbf24"
            )
        if modal:
            QMessageBox.warning(self, "屏幕布局不一致，未开始执行", message)
        return message

    @Slot(dict)
    def on_recorded_mouse_context_mismatch(self, issue):
        """Show hotkey rejection feedback without moving focus away from its target."""
        if self._macro_callback_blocked_by_shutdown():
            return
        message = self._show_recorded_mouse_context_issue(issue, issue, modal=False)
        tray = getattr(self, "system_tray", None)
        if tray is not None and tray.isVisible():
            tray.showMessage(
                "MacroCanvas：宏未执行",
                message,
                QSystemTrayIcon.MessageIcon.Warning,
                7000,
            )
        self.refresh_status_ui()
        self.refresh_macro_controls()

    def _explain_runtime_cleanup_block(self, context="runtime_trigger"):
        return explain_runtime_cleanup_block(self, context)

    def _macro_control_operation_busy(self, context="macro_control"):
        if not macro_control_transaction_busy(self):
            return False
        if hasattr(self, "write_diagnostic"):
            self.write_diagnostic(
                "macro_control_rejected",
                context=context,
                reason="transaction_busy",
                macro_state=str(getattr(
                    getattr(self, "macro_state", None),
                    "name",
                    getattr(self, "macro_state", ""),
                )),
                output_shutdown=bool(getattr(self, "output_shutdown_in_progress", False)),
            )
        return True


    def _runtime_cleanup_failure_items(self):
        """Return concrete release failures, excluding a plain in-progress stop gate."""
        failures = [str(item) for item in (getattr(self, "last_macro_release_failures", []) or []) if str(item)]
        controller = getattr(self, "macro_controller", None)
        for item in (getattr(controller, "last_release_failures", []) or []):
            text = str(item or "").strip()
            if text and text not in failures:
                failures.append(text)
        lock = getattr(controller, "lock", None)
        tasks = getattr(controller, "tasks", {}) if controller is not None else {}
        if lock is not None:
            with lock:
                items = list(tasks.items())
        else:
            items = list(getattr(tasks, "items", lambda: [])())
        for preset_id, task in items:
            if getattr(task, "release_cleanup_failed", False):
                text = str(
                    preset_id
                    or getattr(task, "preset", {}).get("id")
                    or "宏任务"
                )
                if text and text not in failures:
                    failures.append(text)
        return failures

    def _macro_activity_details(self):
        info = (
            self.last_action_activity
            if isinstance(self.last_action_activity, dict) else {}
        )
        preset_name = str(info.get("name") or "宏任务")
        action_text = str(info.get("action") or "等待继续")
        return preset_name, action_text

    def _sync_macro_pause_display(self, paused):
        """同步暂停状态到状态栏、主界面通知和右上角浮窗。"""
        preset_name, action_text = self._macro_activity_details()
        if paused:
            self.macro_status_detail = f"{preset_name} · {action_text}"
            hint_text = f"宏任务已暂停：{preset_name} · {action_text}"
            execution_text = f"已暂停：{preset_name}　当前动作：{action_text}"
            overlay_title = f"已暂停 · {preset_name}"
            overlay_accent = "#71efa0"
        else:
            self.macro_status_detail = ""
            hint_text = f"正在执行：{preset_name} · {action_text}"
            execution_text = f"正在执行：{preset_name}　当前动作：{action_text}"
            overlay_title = f"正在执行 · {preset_name}"
            overlay_accent = "#38bdf8"

        if hasattr(self, "engine_hint"):
            self.engine_hint.setStyleSheet("")
            self.engine_hint.setText(hint_text)
        if hasattr(self, "execution_info"):
            self.execution_info.setText(execution_text)
        if hasattr(self, "activity_overlay") and not getattr(
            self, "recording_session_active", False
        ):
            self.activity_overlay.show_message(
                overlay_title, action_text, overlay_accent
            )

    @Slot(dict)
    def update_action_activity(self, info):
        if getattr(self, "recording_session_active", False):
            return
        if self._runtime_cleanup_blocks_new_output():
            failures = self._runtime_cleanup_failure_items()
            if failures or getattr(self, "macro_state", None) == MacroState.STOP_TIMEOUT:
                self._show_macro_cleanup_failure(
                    "按键释放未完成，已暂停新的动作状态刷新",
                    failures or self._explain_runtime_cleanup_block("action_activity"),
                )
            elif hasattr(self, "write_diagnostic"):
                self.write_diagnostic(
                    "macro_action_activity_suppressed",
                    context="cleanup_in_progress",
                    output_shutdown=bool(getattr(self, "output_shutdown_in_progress", False)),
                )
            self.refresh_status_ui()
            self.refresh_macro_controls()
            return
        preset_name = str(info.get("name") or "预设")
        action_text = str(info.get("action") or "动作")
        self.active_macro_id = info.get("id") or self.active_macro_id
        self.last_action_activity = dict(info)
        self.last_action_activity.update({
            "id": self.active_macro_id, "name": preset_name,
            "action": action_text,
        })
        if hasattr(self, "_set_runtime_debug_current_action"):
            self._set_runtime_debug_current_action(self.last_action_activity)
        phase = str(info.get("phase") or "start")
        if hasattr(self, "write_diagnostic"):
            self.write_diagnostic(
                "macro_action_activity",
                preset_id=self.active_macro_id,
                preset=preset_name,
                action=action_text,
                phase=phase,
                action_id=str(info.get("action_id") or ""),
                action_type=str(info.get("action_type") or ""),
                source_preset_id=str(info.get("source_preset_id") or ""),
                path=list(info.get("path", []) or []),
                parameters=dict(info.get("parameters", {}) or {}),
                reason=str(info.get("debug_reason") or ""),
            )
        if phase == "debug_pause":
            self.macro_state = MacroState.PAUSED
            self._sync_macro_pause_display(True)
            self.refresh_status_ui()
            self.refresh_macro_controls()
            return
        if phase == "finished":
            return
        if phase == "error":
            if hasattr(self, "write_diagnostic"):
                failure_kind = str(
                    info.get("debug_reason") or info.get("error_type") or ""
                )
                self.write_diagnostic(
                    (
                        "macro_parallel_action_failed"
                        if failure_kind == "parallel_exception"
                        else "macro_condition_timeout"
                        if failure_kind == "condition_timeout"
                        else "macro_submacro_failed"
                        if failure_kind == "submacro_unavailable"
                        else "macro_action_failed"
                    ),
                    force=True,
                    preset_id=self.active_macro_id,
                    preset=preset_name,
                    action=action_text,
                )
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #ff8496;")
                self.engine_hint.setText(
                    f"宏动作失败：{preset_name} · {action_text}"
                )
            if hasattr(self, "execution_info"):
                self.execution_info.setText(
                    f"执行失败：{preset_name}　{action_text}"
                )
            if hasattr(self, "activity_overlay"):
                self.activity_overlay.show_message(
                    f"执行失败 · {preset_name}", action_text, "#fb7185"
                )
            return
        task = self.macro_controller.tasks.get(self.active_macro_id)
        if task is not None and not task.run_event.is_set():
            self._sync_macro_pause_display(True)
            return
        if hasattr(self, "engine_hint"):
            self.engine_hint.setStyleSheet("")
            self.engine_hint.setText(
                f"正在执行：{preset_name} · {action_text}"
            )
        if hasattr(self, "activity_overlay"):
            self.activity_overlay.show_message(
                f"正在执行 · {preset_name}", action_text, "#38bdf8"
            )

    @Slot(dict)
    def update_macro_progress(self, info):
        if self._runtime_cleanup_blocks_new_output():
            failures = self._runtime_cleanup_failure_items()
            if failures or getattr(self, "macro_state", None) == MacroState.STOP_TIMEOUT:
                self._show_macro_cleanup_failure(
                    "按键释放未完成，已暂停宏进度刷新",
                    failures or self._explain_runtime_cleanup_block("macro_progress"),
                )
            elif hasattr(self, "write_diagnostic"):
                self.write_diagnostic(
                    "macro_progress_suppressed",
                    context="cleanup_in_progress",
                    output_shutdown=bool(getattr(self, "output_shutdown_in_progress", False)),
                )
            self.refresh_status_ui()
            self.refresh_macro_controls()
            return
        self.active_macro_id = info["id"]
        self.last_action_activity = {
            "id": info.get("id"),
            "name": str(info.get("name") or "预设"),
            "action": str(info.get("action") or "动作"),
        }
        loop_total = info.get("loop_total") or "∞"
        self.execution_info.setText(
            f"正在执行：{info['name']}　循环：{info['loop']} / {loop_total}　"
            f"步骤：{info['step']} / {info['step_total']}　当前动作：{info['action']}"
        )
        self.macro_state = MacroState.PAUSED if info.get("paused") else MacroState.RUNNING
        self.macro_status_detail = ""
        if info.get("paused"):
            self._sync_macro_pause_display(True)
        self.refresh_status_ui()
        self.refresh_macro_controls()

    @Slot(str)
    def on_macro_finished(self, preset_id):
        finished_task = self.macro_controller.finish(preset_id)
        if self._macro_callback_blocked_by_shutdown():
            return
        remaining_task = self.macro_controller.tasks.get(preset_id)
        if remaining_task is not None and remaining_task.has_live_threads():
            self.active_macro_id = preset_id
            self.macro_state = MacroState.STOPPING
            self.macro_status_detail = "主任务已结束，正在等待并行工作线程退出"
            if hasattr(self, "execution_info"):
                self.execution_info.setText(
                    "主任务已结束，正在等待并行工作线程完成清理"
                )
            self.refresh_status_ui()
            self.refresh_macro_controls()
            QTimer.singleShot(250, self._poll_stopping_macros)
            return
        controller_failures = set(getattr(
            self.macro_controller, "last_release_failures", []
        ) or [])
        cleanup_failed = bool(
            (
                finished_task is not None
                and getattr(finished_task, "release_cleanup_failed", False)
            )
            or str(preset_id or "") in controller_failures
        )
        failed_preset_name = str(
            getattr(finished_task, "preset", {}).get("name")
            if finished_task is not None else ""
        ) or "宏任务"
        if cleanup_failed:
            self._remember_macro_cleanup_failure(
                "宏自然结束，但最终按键释放未完成",
                [failed_preset_name],
            )
        self._record_macro_run_history(finished_task, preset_id)
        if self.active_macro_id == preset_id:
            with self.macro_controller.lock:
                fallback = next(
                    (
                        (other_id, task)
                        for other_id, task in self.macro_controller.tasks.items()
                        if task.has_live_threads()
                    ),
                    None,
                )
            if fallback is None:
                self.active_macro_id = None
                self.execution_info.setText("当前没有正在执行的宏")
            else:
                fallback_id, fallback_task = fallback
                self.active_macro_id = fallback_id
                fallback_name = str(
                    fallback_task.preset.get("name") or "宏任务"
                )
                paused = not fallback_task.run_event.is_set()
                self.macro_state = (
                    MacroState.PAUSED if paused else MacroState.RUNNING
                )
                self.last_action_activity = {
                    "id": fallback_id,
                    "name": fallback_name,
                    "action": "等待状态更新",
                }
                self.execution_info.setText(
                    f"{'已暂停' if paused else '正在执行'}：{fallback_name}　"
                    "等待下一条动作状态"
                )
                if paused:
                    self._sync_macro_pause_display(True)
                else:
                    if hasattr(self, "engine_hint"):
                        self.engine_hint.setStyleSheet("")
                        self.engine_hint.setText(
                            f"正在执行：{fallback_name} · 等待状态更新"
                        )
                    if hasattr(self, "activity_overlay") and not getattr(
                        self, "recording_session_active", False
                    ):
                        self.activity_overlay.show_message(
                            f"正在执行 · {fallback_name}",
                            "等待下一条动作状态",
                            "#38bdf8",
                        )
        if not self.macro_controller.tasks:
            cleanup_failures = list(getattr(self, "last_macro_release_failures", []))
            if cleanup_failures:
                self._show_macro_cleanup_failure(
                    "宏自然结束，但最终按键释放未完成",
                    cleanup_failures,
                )
            else:
                restore_gate = getattr(self, "_macro_stop_gate_restore", None)
                if restore_gate is not None:
                    self.output_shutdown_in_progress = bool(restore_gate)
                    self._macro_stop_gate_restore = None
                restore_ok = True
                if hasattr(self, "_apply_deferred_profile_input_restore"):
                    restore_ok = (
                        self._apply_deferred_profile_input_restore() is not False
                    )
                if not restore_ok:
                    cleanup_failures = list(
                        getattr(self, "last_macro_release_failures", []) or []
                    )
                    if not cleanup_failures:
                        cleanup_failures = ["延迟恢复运行档案输入失败"]
                        self._remember_macro_cleanup_failure(
                            "宏自然结束，但运行档案输入恢复失败",
                            cleanup_failures,
                        )
                    self.output_shutdown_in_progress = True
                    self._macro_stop_gate_restore = None
                    self._show_macro_cleanup_failure(
                        "宏自然结束，但运行档案输入恢复失败",
                        cleanup_failures,
                    )
                else:
                    if hasattr(self, "_discard_profile_suspended_macros"):
                        self._discard_profile_suspended_macros(
                            reason="macro_finished_all_threads_exited"
                        )
                    self.macro_state = MacroState.IDLE
                    self.last_action_activity = {}
                    if hasattr(self, "_set_runtime_debug_current_action"):
                        self._set_runtime_debug_current_action({})
                    if hasattr(self, "activity_overlay") and not getattr(
                        self, "recording_session_active", False
                    ):
                        self.activity_overlay.hide_message()
                    if hasattr(self, "engine_hint") and self.running:
                        self.engine_hint.setStyleSheet("")
                        self.engine_hint.setText("输入引擎运行中，当前没有正在执行的动作")
                    self.macro_status_detail = ""
        elif cleanup_failed:
            self._show_macro_cleanup_failure(
                "宏自然结束，但最终按键释放未完成",
                [failed_preset_name],
            )
        self.refresh_status_ui()
        self.refresh_macro_controls()
        if (
            not self.macro_controller.tasks
            and not cleanup_failed
            and not getattr(self, "last_macro_release_failures", [])
            and getattr(self, "auto_apply_checkbox", None) is not None
            and self.auto_apply_checkbox.isChecked()
            and getattr(self.config_state, "name", "") == "DIRTY"
        ):
            self.auto_apply_timer.start(300)

    def refresh_macro_controls(self):
        task = self.macro_controller.tasks.get(self.active_macro_id)
        active = task is not None
        temporarily_suspended = bool(getattr(
            self, "profile_input_temporarily_suspended", False
        ))
        control_busy = macro_control_transaction_busy(self)
        self.pause_button.setEnabled(
            active and not temporarily_suspended and not control_busy
        )
        self.stop_current_button.setEnabled(active and not control_busy)
        if control_busy and active:
            self.pause_button.setText("清理中")
        elif temporarily_suspended and active:
            self.pause_button.setText("暂时隔离")
        elif active and not task.run_event.is_set():
            self.pause_button.setText("继续")
        else:
            self.pause_button.setText("暂停")

    @Slot()
    def toggle_all_macro_pause(self):
        """Pause or resume the tasks owned by the global pause control.

        The first press pauses only tasks that are running at that moment and
        records those ids.  The next resume press only resumes that recorded set,
        so a task the user had already paused individually stays paused.
        """
        if self._macro_control_operation_busy("toggle_all_macro_pause"):
            return False
        if getattr(self, "profile_input_temporarily_suspended", False):
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "当前输入因前台窗口或设置操作暂时隔离，返回目标窗口后自动恢复"
                )
            return
        with self.macro_controller.lock:
            task_items = list(self.macro_controller.tasks.items())
        if not task_items:
            if hasattr(self, "_global_pause_macro_ids"):
                self._global_pause_macro_ids.clear()
            return

        def task_stopping(task):
            stop_event = getattr(task, "stop_event", None)
            return bool(stop_event is not None and stop_event.is_set())

        running_ids = {
            str(preset_id)
            for preset_id, task in task_items
            if task.run_event.is_set() and not task_stopping(task)
        }
        owned_ids = set(getattr(self, "_global_pause_macro_ids", set()) or set())
        live_ids = {str(preset_id) for preset_id, _task in task_items}
        owned_ids &= live_ids
        should_pause = bool(running_ids)
        target_ids = running_ids if should_pause else owned_ids
        if not target_ids:
            self._global_pause_macro_ids = owned_ids
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText("没有由“暂停全部”暂停的宏需要继续")
            self.refresh_macro_controls()
            return

        failed = []
        succeeded = []
        for preset_id, task in task_items:
            preset_id = str(preset_id)
            if preset_id not in target_ids:
                continue
            if should_pause:
                success = task.pause()
            else:
                if task.run_event.is_set() or task_stopping(task):
                    owned_ids.discard(preset_id)
                    continue
                success = task.resume()
            if success:
                succeeded.append(preset_id)
            else:
                failed.append(str(task.preset.get("name") or task.preset.get("id") or "宏任务"))

        if should_pause:
            owned_ids.update(succeeded)
        else:
            owned_ids.difference_update(succeeded)
        self._global_pause_macro_ids = owned_ids

        self.macro_controller.signals.state_changed.emit()
        if failed:
            self._play_feedback("error")
            title = (
                "部分宏暂停失败，按键未完全释放"
                if should_pause else "部分宏恢复失败，任务已停止"
            )
            self._remember_macro_cleanup_failure(title, failed)
            self._show_macro_cleanup_failure(
                title,
                failed,
            )
            self.refresh_status_ui()
            self.refresh_macro_controls()
            return
        self._play_feedback("paused" if should_pause else "resumed")
        with self.macro_controller.lock:
            remaining_tasks = list(self.macro_controller.tasks.values())
        any_running = any(task.run_event.is_set() for task in remaining_tasks)
        any_paused = any(not task.run_event.is_set() for task in remaining_tasks)
        if any_running:
            self.macro_state = MacroState.RUNNING
            self._sync_macro_pause_display(False)
        elif any_paused:
            self.macro_state = MacroState.PAUSED
            self._sync_macro_pause_display(True)
        else:
            self.macro_state = MacroState.IDLE
            self.macro_status_detail = ""
        self.refresh_status_ui()
        self.refresh_macro_controls()

    def pause_or_resume_current(self):
        if self._macro_control_operation_busy("pause_or_resume_current"):
            return False
        if getattr(self, "profile_input_temporarily_suspended", False):
            return False
        active_macro_id = self.active_macro_id
        with self.macro_controller.lock:
            task = self.macro_controller.tasks.get(active_macro_id)
        if not task:
            return False
        if task.run_event.is_set():
            if self.macro_controller.pause(active_macro_id):
                self._play_feedback("paused")
                self.macro_state = MacroState.PAUSED
                self._sync_macro_pause_display(True)
            else:
                self._play_feedback("error")
                failed = [str(task.preset.get("name") or "当前宏")]
                self._remember_macro_cleanup_failure(
                    "宏暂停失败，按键未完全释放", failed
                )
                self._show_macro_cleanup_failure(
                    "宏暂停失败，按键未完全释放",
                    failed,
                )
        else:
            if self.macro_controller.resume(active_macro_id):
                self._play_feedback("resumed")
                self.macro_state = MacroState.RUNNING
                self._sync_macro_pause_display(False)
            else:
                self._play_feedback("error")
                failed = [str(task.preset.get("name") or "当前宏")]
                self._remember_macro_cleanup_failure(
                    "宏恢复失败，任务已停止", failed
                )
                self._show_macro_cleanup_failure(
                    "宏恢复失败，任务已停止",
                    failed,
                )
        self.refresh_status_ui()
        self.refresh_macro_controls()

    def _stop_macro_task_with_release(self, preset_id):
        """兼容停止接口，同时确保关闭映射时主动释放该任务持有的输出。"""
        controller = getattr(self, "macro_controller", None)
        stop = getattr(controller, "stop", None)
        if stop is None:
            return False
        use_release_argument = True
        try:
            signature = inspect.signature(stop)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            parameters = signature.parameters
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            use_release_argument = accepts_kwargs or "release_held" in parameters
        if use_release_argument:
            return bool(stop(preset_id, release_held=True))

        stopped = bool(stop(preset_id))
        if stopped:
            task = None
            tasks = getattr(controller, "tasks", None)
            lock = getattr(controller, "lock", None)
            if tasks is not None:
                if lock is not None:
                    with lock:
                        task = tasks.get(preset_id)
                else:
                    task = tasks.get(preset_id)
            force_release = getattr(task, "force_release", None)
            if force_release is not None:
                force_release()
        return stopped

    def _request_stop_macro_task(self, preset_id, reason=""):
        """Stop one macro task while keeping the output gate closed until cleanup ends."""
        if not preset_id:
            return False
        previous_gate = bool(getattr(self, "output_shutdown_in_progress", False))
        self.output_shutdown_in_progress = True
        self._mark_macro_stop_started()
        if getattr(self, "_macro_stop_gate_restore", None) is None:
            self._macro_stop_gate_restore = previous_gate
        # A toggle/hold mapping stop means “close this mapping now”.  Close the
        # output gate, wait for any in-flight Press/Tap bookkeeping, then actively
        # release this task's held Press actions instead of relying only on the
        # worker thread's eventual finally cleanup.
        dispatch_lock = getattr(self, "output_dispatch_lock", None)
        if dispatch_lock is not None:
            with dispatch_lock:
                pass
        stopped = bool(self._stop_macro_task_with_release(preset_id))
        if not stopped:
            with self.macro_controller.lock:
                still_known = preset_id in self.macro_controller.tasks
            if not still_known and not getattr(self, "last_macro_release_failures", []):
                self.output_shutdown_in_progress = previous_gate
                self._macro_stop_gate_restore = None
                self._clear_macro_stop_started()
            return False
        self.active_macro_id = preset_id
        self.macro_state = MacroState.STOPPING
        self.macro_status_detail = reason or "正在停止当前宏"
        if hasattr(self, "execution_info"):
            self.execution_info.setText(reason or "正在停止当前宏并释放按键")
        self.refresh_status_ui()
        self.refresh_macro_controls()
        QTimer.singleShot(250, self._poll_stopping_macros)
        return True

    def stop_current_macro(self):
        if self._macro_control_operation_busy("stop_current_macro"):
            return False
        active_macro_id = self.active_macro_id
        if not active_macro_id:
            return False
        with self.macro_controller.lock:
            if active_macro_id not in self.macro_controller.tasks:
                return False
        return self._request_stop_macro_task(
            active_macro_id, "正在停止当前宏并释放按键"
        )

    def _poll_stopping_macros(self):
        if self._macro_callback_blocked_by_shutdown():
            return
        stale_cleanup_failures = []
        with self.macro_controller.lock:
            remaining = [
                preset_id for preset_id, task in self.macro_controller.tasks.items()
                if task.has_live_threads()
            ]
            stale_items = [
                (preset_id, task)
                for preset_id, task in self.macro_controller.tasks.items()
                if not task.has_live_threads()
            ]
            for preset_id, task in stale_items:
                if getattr(task, "release_cleanup_failed", False):
                    stale_cleanup_failures.append(
                        str(task.preset.get("name") or preset_id or "宏任务")
                    )
                self.macro_controller.tasks.pop(preset_id, None)
        if stale_cleanup_failures:
            self._remember_macro_cleanup_failure(
                "宏线程已停止，但部分按键释放失败",
                stale_cleanup_failures,
            )
        if remaining:
            self.active_macro_id = remaining[0]
            self._set_macro_stop_waiting_display(len(remaining))
            self.refresh_status_ui()
            self.refresh_macro_controls()
            QTimer.singleShot(250, self._poll_stopping_macros)
            return
        self._clear_macro_stop_started()
        cleanup_failures = list(getattr(self, "last_macro_release_failures", []))
        if cleanup_failures:
            self.output_shutdown_in_progress = True
            self._macro_stop_gate_restore = None
            self._deferred_profile_input_restore = None
            if hasattr(self, "_discard_profile_suspended_macros"):
                self._discard_profile_suspended_macros(
                    reason="macro_cleanup_failed"
                )
        else:
            restore_gate = getattr(self, "_macro_stop_gate_restore", None)
            if restore_gate is not None:
                self.output_shutdown_in_progress = bool(restore_gate)
                self._macro_stop_gate_restore = None
            restore_ok = True
            if hasattr(self, "_apply_deferred_profile_input_restore"):
                restore_ok = self._apply_deferred_profile_input_restore() is not False
            if not restore_ok:
                cleanup_failures = list(getattr(self, "last_macro_release_failures", []) or [])
                if not cleanup_failures:
                    cleanup_failures = ["延迟恢复运行档案输入失败"]
                    self._remember_macro_cleanup_failure(
                        "宏线程已停止，但运行档案输入恢复失败",
                        cleanup_failures,
                    )
                self.output_shutdown_in_progress = True
                self._macro_stop_gate_restore = None
            elif hasattr(self, "_discard_profile_suspended_macros"):
                self._discard_profile_suspended_macros(
                    reason="all_macro_threads_exited"
                )
        self.active_macro_id = None
        self.last_action_activity = {}
        if cleanup_failures:
            self._show_macro_cleanup_failure(
                "宏线程已停止，但部分按键释放失败", cleanup_failures
            )
        else:
            self.macro_state = MacroState.IDLE
            self.macro_status_detail = ""
            if hasattr(self, "execution_info"):
                self.execution_info.setText("已停止全部宏并释放程序持有的按键")
            if hasattr(self, "activity_overlay") and not getattr(
                self, "recording_session_active", False
            ):
                self.activity_overlay.hide_message()
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("")
                self.engine_hint.setText(
                    "输入引擎运行中，当前没有正在执行的动作"
                    if self.running else "输入引擎已停止"
                )
        self.refresh_status_ui()
        self.refresh_macro_controls()

    @Slot()
    def stop_all_macros(self, _checked=False, play_sound=False, keep_output_gate=False):
        previous_gate = self.output_shutdown_in_progress
        self.output_shutdown_in_progress = True
        self._mark_macro_stop_started()
        # Wait for a Press/Tap that passed the gate immediately before this stop
        # request. Its owner bookkeeping is completed before release tables are
        # inspected, so cleanup cannot run first and leave a late synthetic Down.
        dispatch_lock = getattr(self, "output_dispatch_lock", None)
        if dispatch_lock is not None:
            with dispatch_lock:
                pass
        _targets, virtual_keys = self._remember_runtime_release_state()
        remaining = []
        cleanup_failures = []
        previous_failures = list(getattr(self, "last_macro_release_failures", []) or [])
        try:
            # First block all new Press/Tap packets, then stop and drain tasks
            # while Release is still allowed to use the live output backend.
            remaining = self.macro_controller.stop_all()
            failed_task_ids = set(getattr(
                self.macro_controller, "last_release_failures", []
            ))
            with self.macro_controller.lock:
                retryable_task_ids = set(self.macro_controller.tasks)
            retry_failed = set(self.macro_controller.force_release_all())
            failed_tasks = sorted(
                (failed_task_ids - retryable_task_ids) | retry_failed
            )
            if failed_tasks:
                cleanup_failures.append(
                    f"宏任务释放失败({len(failed_tasks)})"
                )
            # 同步模式会保持目标按键状态，停止时必须统一发送 Release。
            if not self._release_all_sync_mappings():
                cleanup_failures.append("同步映射输出")
            if not self._release_runtime_virtual_keys(
                names=virtual_keys,
                include_history=True,
                timeout=0.8,
            ):
                cleanup_failures.append("Kanata 虚拟键")
            if not self._release_interception_output():
                cleanup_failures.append("Interception 输出")
            if not self._runtime_is_game_mode():
                if self._kanata_engine_has_runtime(self.engine):
                    if not self.engine.release_all_virtual_keys(timeout=0.8):
                        cleanup_failures.append("Kanata 主后端")
                if self._kanata_engine_has_runtime(self.keyboard_engine):
                    if not self.keyboard_engine.release_all_virtual_keys(timeout=0.8):
                        cleanup_failures.append("Kanata 键盘后端")
        finally:
            # The final gate decision depends on cleanup_failures as well as
            # remaining threads, so it is applied after cleanup_failures is known.
            pass
        if cleanup_failures:
            cleanup_failures = list(dict.fromkeys(previous_failures + cleanup_failures))
            self.last_macro_release_failures = list(cleanup_failures)
            self._remember_macro_cleanup_failure(
                "宏停止清理失败", cleanup_failures
            )
        else:
            self.last_macro_release_failures = []
        if remaining or keep_output_gate or cleanup_failures:
            if remaining and not cleanup_failures and not keep_output_gate:
                if getattr(self, "_macro_stop_gate_restore", None) is None:
                    self._macro_stop_gate_restore = bool(previous_gate)
            elif cleanup_failures:
                self._macro_stop_gate_restore = None
            self.output_shutdown_in_progress = True
        elif not keep_output_gate:
            self.output_shutdown_in_progress = previous_gate
        with self.input_state_lock:
            self.held_trigger_ids.clear()
            self.kanata_trigger_down.clear()
        if hasattr(self, "_discard_profile_suspended_macros"):
            self._discard_profile_suspended_macros(reason="stop_all_macros")
        self._test_countdown_generation = (
            int(getattr(self, "_test_countdown_generation", 0)) + 1
        )
        self._test_countdown_preset_id = None
        if remaining:
            self.active_macro_id = remaining[0]
            self._set_macro_stop_waiting_display(
                len(remaining), cleanup_failures=cleanup_failures
            )
            QTimer.singleShot(250, self._poll_stopping_macros)
        elif cleanup_failures:
            self._clear_macro_stop_started()
            self.active_macro_id = None
            self.last_action_activity = {}
            self._show_macro_cleanup_failure(
                "宏已停止，但部分按键释放失败", cleanup_failures
            )
        else:
            self._clear_macro_stop_started()
            self.active_macro_id = None
            self.last_action_activity = {}
            self.macro_state = MacroState.IDLE
            self.macro_status_detail = ""
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("")
                self.engine_hint.setText(
                    "输入引擎运行中，当前没有正在执行的动作"
                    if self.running else "输入引擎已停止"
                )
            if hasattr(self, "execution_info"):
                self.execution_info.setText("已停止全部宏并释放程序持有的按键")
            if hasattr(self, "activity_overlay") and not getattr(
                self, "recording_session_active", False
            ):
                self.activity_overlay.hide_message()
        self.refresh_status_ui()
        self.refresh_macro_controls()
        if play_sound:
            self._play_feedback("emergency")
        return list(remaining)
