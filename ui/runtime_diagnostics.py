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
    APP_DIR,
    DIAGNOSTIC_LOG_PATH,
    DIAGNOSTIC_MAX_LINES,
    DIAGNOSTIC_TRIM_INTERVAL,
    KANATA_KEYBOARD_LOG_PATH,
    KANATA_LOG_PATH,
)
from macro.actions import iter_action_tree
from ui.diagnostic_bundle import write_diagnostic_bundle
from ui.operation_state import operation_blocks, operation_state_snapshot


class RuntimeDiagnosticsMixin:
    """Provide diagnostic controls without owning application runtime state."""

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

    def open_runtime_debugger(self):
        if self.runtime_debug_dialog is not None:
            self.runtime_debug_dialog.show()
            self.runtime_debug_dialog.raise_()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("运行调试器")
        dialog.resize(980, 620)
        layout = QVBoxLayout(dialog)
        status = QLabel()
        status.setWordWrap(True)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["时间", "事件", "内容"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(status)
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
        rendered_ids = []

        def append_event(item):
            row = table.rowCount()
            table.insertRow(row)
            details = {
                key: value for key, value in item.items()
                if key not in ("time", "event", "_seq")
            }
            table.setItem(row, 0, QTableWidgetItem(item.get("time", "")))
            table.setItem(row, 1, QTableWidgetItem(item.get("event", "")))
            table.setItem(row, 2, QTableWidgetItem(
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
            active = list(self.macro_controller.tasks)
            held = self.held_input_snapshot()
            backend = "Interception" if self._runtime_is_game_mode() else "Kanata"
            status.setText(
                f"输出后端：{backend}　活跃宏：{len(active)}　"
                f"程序按住：{held or '无'}"
            )

        def clear_events():
            with self.runtime_debug_lock:
                self.runtime_debug_events.clear()
            refresh()

        def finished(_result=0):
            timer.stop()
            self.runtime_debug_enabled = False
            self.runtime_debug_dialog = None

        clear.clicked.connect(clear_events)
        close.clicked.connect(dialog.close)
        dialog.finished.connect(finished)
        timer.timeout.connect(refresh)
        self.runtime_debug_enabled = True
        self.runtime_debug_dialog = dialog
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
