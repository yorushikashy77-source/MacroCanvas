"""Local diagnostic logging and the live runtime debugger."""

from __future__ import annotations

import json
import os
import queue as queue_module
import threading
import time
import platform
import sys
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QTimer, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.constants import (
    ACTION_ID_ROLE,
    APP_DIR,
    DIAGNOSTIC_LOG_PATH,
    DIAGNOSTIC_MAX_LINES,
    DIAGNOSTIC_TRIM_INTERVAL,
    KANATA_KEYBOARD_LOG_PATH,
    KANATA_LOG_PATH,
)
from engine.window_context import foreground_window_belongs_to_current_process
from macro.actions import iter_action_tree
from ui.diagnostic_bundle import write_diagnostic_bundle
from ui.operation_state import operation_blocks, operation_state_snapshot


class RuntimeDiagnosticsMixin:
    """Provide diagnostic controls without owning application runtime state."""

    def _show_runtime_debug_notice(self, title, detail="", accent="#38bdf8"):
        """Show debugger reminders in the non-activating status overlay."""
        overlay = getattr(self, "activity_overlay", None)
        if overlay is None or getattr(self, "recording_session_active", False):
            return None
        return overlay.show_message(title, detail, accent)

    def _runtime_debug_resume_target_ready(self, task):
        """Do not resume debugger output while MacroCanvas owns the foreground."""
        if foreground_window_belongs_to_current_process():
            return False, "请先切回需要测试的程序"
        required_profile = str(task.preset.get("_required_profile_id") or "")
        profile_active = getattr(task, "profile_active", None)
        if required_profile and callable(profile_active) and not profile_active(
            required_profile
        ):
            return False, "目标程序尚未恢复为当前配置档案"
        return True, ""

    def update_diagnostic_action_text(self):
        if not hasattr(self, "diagnostic_action"):
            return
        self.diagnostic_action.blockSignals(True)
        self.diagnostic_action.setChecked(self.diagnostic_enabled)
        self.diagnostic_action.blockSignals(False)
        state = "已开启" if self.diagnostic_enabled else "已关闭"
        self.diagnostic_action.setText(f"本地诊断日志（{state}）")

    @Slot(bool)
    def set_diagnostic_enabled(self, enabled):
        self.diagnostic_enabled = bool(enabled)
        self.update_diagnostic_action_text()
        if not self.initializing:
            self.data_changed()
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("")
                self.engine_hint.setText(
                    "诊断日志设置已修改，点击“应用更改”后生效"
                )

    def _ensure_diagnostic_writer_state(self):
        if not hasattr(self, "diagnostic_queue"):
            self.diagnostic_queue = queue_module.Queue(maxsize=4096)
        if not hasattr(self, "diagnostic_writer_stop"):
            self.diagnostic_writer_stop = threading.Event()
        if not hasattr(self, "diagnostic_writer_thread"):
            self.diagnostic_writer_thread = None
        if not hasattr(self, "diagnostic_generation"):
            self.diagnostic_generation = 0
        if not hasattr(self, "diagnostic_dropped_count"):
            self.diagnostic_dropped_count = 0

    def _ensure_diagnostic_writer(self):
        self._ensure_diagnostic_writer_state()
        thread = self.diagnostic_writer_thread
        if thread is not None and thread.is_alive():
            return
        with self.diagnostic_lock:
            thread = self.diagnostic_writer_thread
            if thread is not None and thread.is_alive():
                return
            self.diagnostic_writer_stop.clear()
            thread = threading.Thread(
                target=self._diagnostic_writer_loop,
                name="MacroCanvasDiagnosticWriter",
                daemon=True,
            )
            self.diagnostic_writer_thread = thread
            thread.start()

    def _diagnostic_writer_loop(self):
        pending = self.diagnostic_queue
        stop_event = self.diagnostic_writer_stop
        while not stop_event.is_set() or pending.unfinished_tasks:
            try:
                first = pending.get(timeout=0.1)
            except queue_module.Empty:
                continue

            batch = [first]
            while len(batch) < 64:
                try:
                    batch.append(pending.get_nowait())
                except queue_module.Empty:
                    break

            try:
                with self.diagnostic_lock:
                    generation = self.diagnostic_generation
                    lines = [
                        item[1] for item in batch
                        if item[0] == generation
                    ]
                    if lines:
                        APP_DIR.mkdir(parents=True, exist_ok=True)
                        with DIAGNOSTIC_LOG_PATH.open(
                            "a", encoding="utf-8"
                        ) as file:
                            file.write("\n".join(lines) + "\n")
                        self.diagnostic_write_count += len(lines)
                        if (
                            self.diagnostic_write_count
                            >= DIAGNOSTIC_TRIM_INTERVAL
                        ):
                            self._trim_diagnostic_log_locked()
                            self.diagnostic_write_count = 0
            except OSError:
                pass
            finally:
                for _item in batch:
                    pending.task_done()

    def _flush_diagnostic_queue(self, timeout=1.5):
        self._ensure_diagnostic_writer_state()
        if not self.diagnostic_queue.unfinished_tasks:
            return True
        self._ensure_diagnostic_writer()
        deadline = time.perf_counter() + max(0.0, float(timeout))
        while self.diagnostic_queue.unfinished_tasks:
            if time.perf_counter() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _stop_diagnostic_writer(self, timeout=1.5):
        self._ensure_diagnostic_writer_state()
        thread = self.diagnostic_writer_thread
        if (
            self.diagnostic_queue.unfinished_tasks
            and (thread is None or not thread.is_alive())
        ):
            self._ensure_diagnostic_writer()
        self.diagnostic_writer_stop.set()
        flushed = self._flush_diagnostic_queue(timeout=timeout)
        thread = self.diagnostic_writer_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self.diagnostic_writer_thread = None
        return bool(flushed and stopped)

    def reset_diagnostic_log(self):
        """Atomically start a new log generation without waiting on input threads."""
        self._ensure_diagnostic_writer_state()
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            with self.diagnostic_lock:
                self.diagnostic_generation += 1
                DIAGNOSTIC_LOG_PATH.write_text("", "utf-8")
                self.diagnostic_write_count = 0
                self.diagnostic_dropped_count = 0
            return True
        except OSError:
            return False

    @Slot()
    def open_diagnostic_log(self):
        try:
            self._flush_diagnostic_queue(timeout=0.5)
            APP_DIR.mkdir(parents=True, exist_ok=True)
            if not DIAGNOSTIC_LOG_PATH.exists():
                DIAGNOSTIC_LOG_PATH.write_text(
                    "MacroCanvas diagnostic log\n", "utf-8"
                )
            os.startfile(str(DIAGNOSTIC_LOG_PATH))
        except OSError as error:
            QMessageBox.warning(
                self, "无法打开诊断日志", f"{DIAGNOSTIC_LOG_PATH}\n\n{error}"
            )

    def _trim_diagnostic_log_locked(self, max_lines=DIAGNOSTIC_MAX_LINES):
        try:
            if not DIAGNOSTIC_LOG_PATH.exists():
                return
            lines = DIAGNOSTIC_LOG_PATH.read_text(
                "utf-8", errors="replace"
            ).splitlines()
            if len(lines) <= max_lines:
                return
            DIAGNOSTIC_LOG_PATH.write_text(
                "\n".join(lines[-max_lines:]) + "\n", "utf-8"
            )
        except OSError:
            pass

    def clear_diagnostic_log(self):
        self.reset_diagnostic_log()

    def _diagnostic_configuration_summary(self):
        mappings = self.collect_mappings()
        presets = self.collect_presets()
        action_types = Counter()
        action_count = 0
        for preset in presets:
            for action in iter_action_tree(preset.get("actions", []) or []):
                action_count += 1
                action_types[str(action.get("type") or "动作")] += 1
        return {
            "mapping_count": len(mappings),
            "enabled_mapping_count": sum(bool(item.get("enabled")) for item in mappings),
            "preset_count": len(presets),
            "enabled_preset_count": sum(bool(item.get("enabled")) for item in presets),
            "action_count": action_count,
            "action_type_counts": dict(sorted(action_types.items())),
            "profile_count": len(getattr(self, "profiles", []) or []) + 1,
            "diagnostic_enabled": bool(getattr(self, "runtime_diagnostic_enabled", False)),
        }

    @Slot()
    def export_diagnostic_bundle(self):
        blocked, snapshot = operation_blocks(self, "diagnostic_export")
        if blocked:
            QMessageBox.information(
                self, "无法导出诊断包", f"{snapshot.label}，无法开始新的导出。"
            )
            return False
        suggested = str(Path.home() / "Desktop" / "MacroCanvas-诊断包.zip")
        destination, _filter = QFileDialog.getSaveFileName(
            self, "导出脱敏诊断包", suggested, "ZIP 压缩包 (*.zip)"
        )
        if not destination:
            return False
        if not destination.casefold().endswith(".zip"):
            destination += ".zip"
        try:
            self._flush_diagnostic_queue(timeout=0.5)
            operation = operation_state_snapshot(self)
            with self.macro_controller.lock:
                active_tasks = len(self.macro_controller.tasks)
            summary = {
                "application": "MacroCanvas",
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "operation": operation.to_dict(),
                "engine_running": bool(getattr(self, "running", False)),
                "engine_state": str(getattr(
                    getattr(self, "engine_state", None), "value", "unknown"
                )),
                "config_state": str(getattr(
                    getattr(self, "config_state", None), "value", "unknown"
                )),
                "macro_state": str(getattr(
                    getattr(self, "macro_state", None), "value", "unknown"
                )),
                "active_macro_count": active_tasks,
                "dropped_diagnostic_lines": int(getattr(
                    self, "diagnostic_dropped_count", 0
                )),
            }
            included = write_diagnostic_bundle(
                destination,
                summary,
                self._diagnostic_configuration_summary(),
                (
                    ("diagnostic.log", DIAGNOSTIC_LOG_PATH),
                    ("kanata.log", KANATA_LOG_PATH),
                    ("kanata-keyboard.log", KANATA_KEYBOARD_LOG_PATH),
                ),
                home=Path.home(),
            )
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, "导出失败", f"诊断包未能生成：\n{error}")
            return False
        QMessageBox.information(
            self,
            "导出完成",
            f"脱敏诊断包已保存：\n{destination}\n\n"
            f"包含 {len(included)} 份可用日志；原始配置未写入压缩包。",
        )
        return True

    def _runtime_debug_action_item(self, info):
        preset_id = str((info or {}).get("source_preset_id") or "")
        action_id = str((info or {}).get("action_id") or "")
        if not preset_id or not action_id:
            return None, None
        card = next(
            (
                item for item in getattr(self, "preset_cards", [])
                if str(getattr(item, "preset_id", "") or "") == preset_id
            ),
            None,
        )
        if card is None or not getattr(card, "_actions_loaded", False):
            return card, None
        for item in card.action_table.iter_items():
            if str(item.data(0, ACTION_ID_ROLE) or "") == action_id:
                return card, item
        return card, None

    def _set_runtime_debug_current_action(self, info):
        previous = dict(getattr(self, "runtime_debug_current_action", {}) or {})
        current = dict(info or {})
        self.runtime_debug_current_action = current
        for candidate in (previous, current):
            card, item = self._runtime_debug_action_item(candidate)
            if card is not None and item is not None:
                self._update_action_variable_marker(item, card)

    def _locate_runtime_debug_action(self, info=None):
        info = dict(info or getattr(self, "runtime_debug_current_action", {}) or {})
        card, item = self._runtime_debug_action_item(info)
        if card is None:
            return False
        if not getattr(card, "_actions_loaded", False):
            self.ensure_card_actions_loaded(card)
            card, item = self._runtime_debug_action_item(info)
        if item is None:
            return False
        self.select_preset_card(card)
        card.action_table.clearSelection()
        item.setSelected(True)
        card.action_table.setCurrentItem(item)
        card.action_table.scrollToItem(item)
        card.action_dialog.show()
        card.action_dialog.raise_()
        card.action_dialog.activateWindow()
        return True

    def open_runtime_debugger(self):
        if self.runtime_debug_dialog is not None:
            self.macro_controller.set_debug_enabled(True)
            self.macro_controller.set_debug_breakpoints(
                getattr(self, "runtime_debug_breakpoints", set())
            )
            self.runtime_debug_dialog.show()
            self.runtime_debug_dialog.raise_()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("运行调试器")
        dialog.resize(980, 620)
        layout = QVBoxLayout(dialog)
        status = QLabel()
        status.setWordWrap(True)
        current_detail = QLabel("当前尚无动作事件")
        current_detail.setWordWrap(True)
        current_detail.setObjectName("muted")
        task_controls = QHBoxLayout()
        task_picker = QComboBox()
        task_picker.setMinimumWidth(220)
        pause_next = QPushButton("下一动作暂停")
        pause_next.setObjectName("secondary")
        step = QPushButton("单步")
        step.setObjectName("secondary")
        resume = QPushButton("继续")
        resume.setObjectName("testAction")
        locate = QPushButton("定位动作")
        locate.setObjectName("secondary")
        task_controls.addWidget(QLabel("调试任务"))
        task_controls.addWidget(task_picker, 1)
        task_controls.addWidget(pause_next)
        task_controls.addWidget(step)
        task_controls.addWidget(resume)
        task_controls.addWidget(locate)
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["时间", "事件", "调用路径", "内容"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(status)
        layout.addWidget(current_detail)
        layout.addLayout(task_controls)
        layout.addWidget(table, 1)
        controls = QHBoxLayout()
        clear = QPushButton("清空")
        close = QPushButton("关闭")
        controls.addWidget(clear)
        controls.addStretch()
        controls.addWidget(close)
        layout.addLayout(controls)
        timer = QTimer(dialog)
        timer.setInterval(200)
        command_timer = QTimer(dialog)
        command_timer.setInterval(100)
        rendered_ids = []
        pending_command = {}

        def show_notice(title, detail="", accent="#38bdf8"):
            return self._show_runtime_debug_notice(title, detail, accent)

        def cancel_pending_command(notice=""):
            if not pending_command:
                return False
            command_timer.stop()
            generation = pending_command.get("overlay_generation")
            pending_command.clear()
            if notice:
                show_notice("调试倒计时已取消", notice, "#fbbf24")
            elif generation is not None:
                overlay = getattr(self, "activity_overlay", None)
                if overlay is not None:
                    overlay.hide_message(generation)
            return True

        def append_event(item):
            row = table.rowCount()
            table.insertRow(row)
            details = {
                key: value for key, value in item.items()
                if key not in ("time", "event", "_seq", "path")
            }
            table.setItem(row, 0, QTableWidgetItem(item.get("time", "")))
            table.setItem(row, 1, QTableWidgetItem(item.get("event", "")))
            path = item.get("path", []) or []
            table.setItem(row, 2, QTableWidgetItem(" → ".join(map(str, path))))
            table.setItem(row, 3, QTableWidgetItem(
                json.dumps(details, ensure_ascii=False, default=str)
            ))

        def rebuild(events, event_ids):
            table.setRowCount(0)
            for item in events:
                append_event(item)
            rendered_ids[:] = event_ids

        def sync_events(events):
            event_ids = [item.get("_seq") for item in events]
            if rendered_ids == event_ids:
                return False
            if not events:
                table.setRowCount(0)
                rendered_ids.clear()
                return True
            if not rendered_ids:
                rebuild(events, event_ids)
                return True

            try:
                overlap_start = rendered_ids.index(event_ids[0])
            except ValueError:
                rebuild(events, event_ids)
                return True

            overlap = rendered_ids[overlap_start:]
            if overlap != event_ids[:len(overlap)]:
                rebuild(events, event_ids)
                return True

            for _index in range(overlap_start):
                table.removeRow(0)
            rendered_ids[:] = overlap
            for item in events[len(overlap):]:
                append_event(item)
            rendered_ids[:] = event_ids
            return True

        def refresh():
            with self.runtime_debug_lock:
                events = list(self.runtime_debug_events)
            changed = sync_events(events)
            if changed and events:
                table.scrollToBottom()
            with self.macro_controller.lock:
                active_items = list(self.macro_controller.tasks.items())
            active = [str(preset_id) for preset_id, _task in active_items]
            selected_id = str(task_picker.currentData() or "")
            picker_ids = [
                str(task_picker.itemData(index) or "")
                for index in range(task_picker.count())
            ]
            if picker_ids != active:
                task_picker.blockSignals(True)
                task_picker.clear()
                for preset_id, task in active_items:
                    task_picker.addItem(
                        str(task.preset.get("name") or preset_id), str(preset_id)
                    )
                preferred = (
                    selected_id if selected_id in active
                    else str(getattr(self, "active_macro_id", "") or "")
                )
                index = task_picker.findData(preferred)
                task_picker.setCurrentIndex(max(0, index))
                task_picker.blockSignals(False)
            selected_id = str(task_picker.currentData() or "")
            task = next(
                (item for preset_id, item in active_items if str(preset_id) == selected_id),
                None,
            )
            debug_pause = dict(getattr(task, "debug_pause_info", {}) or {})
            debug_paused = bool(
                task is not None and debug_pause and not task.run_event.is_set()
            )
            command_pending = bool(pending_command)
            pause_next.setEnabled(
                bool(task and task.run_event.is_set() and not command_pending)
            )
            step.setEnabled(debug_paused and not command_pending)
            resume.setEnabled(debug_paused and not command_pending)
            locate.setEnabled(bool(
                (debug_pause or getattr(self, "runtime_debug_current_action", {}))
            ) and not command_pending)
            held = self.held_input_snapshot()
            backend = "Interception" if self._runtime_is_game_mode() else "Kanata"
            status.setText(
                f"输出后端：{backend}　活跃宏：{len(active)}　"
                f"断点：{len(getattr(self, 'runtime_debug_breakpoints', set()))}　"
                f"程序按住：{held or '无'}"
            )
            detail = debug_pause or dict(
                getattr(self, "runtime_debug_current_action", {}) or {}
            )
            path = " → ".join(map(str, detail.get("path", []) or [])) or "—"
            parameters = detail.get("parameters", {}) or {}
            reason = {
                "breakpoint": "命中断点",
                "step": "单步暂停",
            }.get(str(detail.get("reason") or ""), "当前动作")
            current_detail.setText(
                f"{reason}：{detail.get('action', detail.get('action_type', '—'))}　"
                f"路径：{path}　参数："
                f"{json.dumps(parameters, ensure_ascii=False, default=str) if parameters else '无'}"
            )

        def clear_events():
            with self.runtime_debug_lock:
                self.runtime_debug_events.clear()
            refresh()

        def finished(_result=0):
            timer.stop()
            cancel_pending_command()
            clear_breakpoints = getattr(
                self, "clear_runtime_debug_breakpoints", None
            )
            if callable(clear_breakpoints):
                clear_breakpoints()
            else:
                self.runtime_debug_breakpoints = set()
                self.macro_controller.set_debug_breakpoints(set())
            self.runtime_debug_enabled = False
            self.runtime_debug_dialog = None
            self.macro_controller.set_debug_enabled(False)

        def selected_task_id():
            return str(task_picker.currentData() or "")

        def pause_at_next_action():
            task_id = selected_task_id()
            if self.macro_controller.debug_pause_next_action(task_id):
                show_notice(
                    "下一动作将暂停",
                    "请切回需要测试的程序；宏会在下一动作前暂停。",
                    "#fbbf24",
                )
            else:
                show_notice(
                    "未设置暂停", "当前没有可运行的调试任务。", "#fb7185"
                )
            refresh()

        def schedule_resume(command):
            if pending_command:
                return False
            task_id = selected_task_id()
            with self.macro_controller.lock:
                task = self.macro_controller.tasks.get(task_id)
            if task is None or task.run_event.is_set() or not task.debug_pause_info:
                show_notice(
                    "调试命令未执行", "请先选择一个命中断点且已暂停的宏。", "#fb7185"
                )
                refresh()
                return False
            delay_seconds = max(
                0.0, float(getattr(self, "runtime_debug_resume_delay_seconds", 5.0))
            )
            command_name = "单步" if command == "step" else "继续"
            pending_command.update({
                "command": command,
                "task_id": task_id,
                "deadline": time.monotonic() + delay_seconds,
                "command_name": command_name,
                "overlay_generation": show_notice(
                    f"调试{command_name} · 请切回目标程序",
                    f"将在 {max(1, int(delay_seconds + 0.999))} 秒后执行。",
                    "#fbbf24",
                ),
            })
            command_timer.start()
            refresh()
            return True

        def run_pending_command():
            if not pending_command:
                command_timer.stop()
                return
            remaining = float(pending_command["deadline"]) - time.monotonic()
            if remaining > 0:
                show_notice(
                    f"调试{pending_command['command_name']} · 请切回目标程序",
                    f"将在 {max(1, int(remaining + 0.999))} 秒后执行。",
                    "#fbbf24",
                )
                return
            command_timer.stop()
            task_id = str(pending_command["task_id"])
            command = str(pending_command["command"])
            command_name = str(pending_command["command_name"])
            with self.macro_controller.lock:
                task = self.macro_controller.tasks.get(task_id)
            if task is None or task.run_event.is_set() or not task.debug_pause_info:
                pending_command.clear()
                show_notice(
                    "调试命令未执行", "宏已结束或不再处于调试暂停状态。", "#fb7185"
                )
                refresh()
                return
            target_ready, reason = self._runtime_debug_resume_target_ready(task)
            if not target_ready:
                pending_command.clear()
                show_notice("调试命令未执行", reason, "#fb7185")
                refresh()
                return
            pending_command.clear()
            success = (
                self.macro_controller.debug_step(task_id)
                if command == "step"
                else self.macro_controller.debug_continue(task_id)
            )
            if success:
                show_notice(
                    f"调试{command_name}已执行",
                    "已向目标程序恢复宏动作。",
                    "#71efa0",
                )
            else:
                show_notice(
                    "调试命令未执行", "宏状态已变化，请重新确认断点。", "#fb7185"
                )
            refresh()

        def step_once():
            schedule_resume("step")

        def continue_run():
            schedule_resume("continue")

        def locate_action():
            with self.macro_controller.lock:
                task = self.macro_controller.tasks.get(selected_task_id())
            info = dict(getattr(task, "debug_pause_info", {}) or {}) if task else {}
            self._locate_runtime_debug_action(
                info or getattr(self, "runtime_debug_current_action", {})
            )

        clear.clicked.connect(clear_events)
        pause_next.clicked.connect(pause_at_next_action)
        step.clicked.connect(step_once)
        resume.clicked.connect(continue_run)
        locate.clicked.connect(locate_action)
        close.clicked.connect(dialog.close)
        dialog.finished.connect(finished)
        timer.timeout.connect(refresh)
        command_timer.timeout.connect(run_pending_command)
        self.runtime_debug_enabled = True
        self.runtime_debug_dialog = dialog
        self.macro_controller.set_debug_breakpoints(
            getattr(self, "runtime_debug_breakpoints", set())
        )
        self.macro_controller.set_debug_enabled(True)
        refresh()
        timer.start()
        dialog.show()

    def write_diagnostic(self, event, force=False, **fields):
        if self.runtime_debug_enabled:
            with self.runtime_debug_lock:
                self.runtime_debug_sequence = int(getattr(
                    self, "runtime_debug_sequence", 0
                )) + 1
                self.runtime_debug_events.append({
                    "_seq": self.runtime_debug_sequence,
                    "time": time.strftime("%H:%M:%S"),
                    "event": str(event),
                    **fields,
                })
        if not force and not self.runtime_diagnostic_enabled:
            return
        important = {
            "diagnostic_enabled", "diagnostic_disabled",
            "set_running_requested", "mouse_engine_start_result",
            "keyboard_engine_start_result", "set_running_started",
            "set_running_stopped", "kanata_message", "kanata_source_seen",
            "kanata_control", "kanata_trigger_enter", "kanata_trigger_ignored",
            "kanata_trigger_dispatch", "kanata_sync_mapping_press",
            "kanata_sync_mapping_release", "trigger_task", "trigger_task_start",
            "trigger_task_stop_toggle", "trigger_task_stop_hold",
            "kanata_action_not_sent", "kanata_action_queued",
            "interception_output", "interception_output_error",
            "interception_raw_input", "runtime_trigger_match",
            "runtime_trigger_dispatch_rejected", "runtime_trigger_no_match",
            "runtime_trigger_rules", "global_toggle_input",
            "system_hotkey_input",
        }
        if not force and event not in important:
            return
        if not force and not self.running and not str(event).startswith(
            ("set_running", "mouse_engine", "keyboard_engine", "diagnostic")
        ):
            return
        payload = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "perf": round(time.perf_counter(), 6),
            "thread": threading.current_thread().name,
            "event": str(event),
            **fields,
        }
        if self.diagnostic_session_id:
            payload["session"] = self.diagnostic_session_id
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        self._ensure_diagnostic_writer()
        item = (self.diagnostic_generation, line)
        try:
            self.diagnostic_queue.put_nowait(item)
        except queue_module.Full:
            try:
                self.diagnostic_queue.get_nowait()
                self.diagnostic_queue.task_done()
                self.diagnostic_dropped_count += 1
                self.diagnostic_queue.put_nowait(item)
            except (queue_module.Empty, queue_module.Full):
                self.diagnostic_dropped_count += 1
