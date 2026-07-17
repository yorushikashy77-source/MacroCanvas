"""录制会话、倒计时、结果整理与动作转换流程。"""

from __future__ import annotations

import copy
import ctypes
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
from ctypes import wintypes

from PySide6.QtCore import QEvent, QTimer, Qt, Slot
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QLabel, QMessageBox, QSpinBox, QWidget,
)

from config.schema import MAX_ACTION_COUNT, validate_config_payload
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


class RecordingWorkflowMixin:
    def _set_recording_configuration_actions_enabled(self, enabled):
        """Freeze every control that can invalidate an active recording."""
        names = (
            "import_config_action",
            "profile_settings_action",
            "global_hotkey_action",
            "kanata_directory_action",
            "restore_config_action",
            "toggle_button",
            "backend_combo",
            "tabs",
            "auto_apply_checkbox",
            "profile_selector_combo",
        )
        if not enabled:
            states = []
            seen = set()
            targets = [getattr(self, name, None) for name in names]
            targets.extend(
                getattr(card, "action_dialog", None)
                for card in getattr(self, "preset_cards", [])
            )
            for target in targets:
                if target is None or id(target) in seen:
                    continue
                seen.add(id(target))
                try:
                    states.append((target, bool(target.isEnabled())))
                    target.setEnabled(False)
                except (AttributeError, RuntimeError):
                    continue
            self._recording_control_enabled_states = states
        else:
            states = list(getattr(
                self, "_recording_control_enabled_states", []
            ))
            self._recording_control_enabled_states = []
            for target, was_enabled in states:
                try:
                    target.setEnabled(bool(was_enabled))
                except (AttributeError, RuntimeError):
                    continue
        # “应用更改”会重建输入后端，不能与倒计时或正式录制并行。
        # 恢复时按当前配置状态决定是否可用，避免把原本没有修改的配置
        # 错误地显示成可应用。
        reload_button = getattr(self, "reload_button", None)
        if reload_button is not None:
            reload_button.setEnabled(
                bool(enabled)
                and getattr(self, "config_state", None)
                in (ConfigState.DIRTY, ConfigState.FAILED)
            )
        if enabled and hasattr(self, "refresh_profile_selector_state"):
            self.refresh_profile_selector_state()

    def _configuration_change_blocked_by_recording(self):
        if not getattr(self, "recording_session_active", False):
            return False
        QMessageBox.information(
            self,
            "正在录制",
            "录制倒计时或正式录制期间不能载入、覆盖或修改全局配置。"
            "请先完成或取消录制。",
        )
        return True

    def _enter_recording_input_mode(self):
        """Disable live triggers before recording observes physical input."""
        self.recording_restore_layer = (
            self.active_profile_layer
            if self.mappings_enabled else DISABLED_LAYER_NAME
        )
        previous_gate = self.output_shutdown_in_progress
        previous_allowed = self.profile_trigger_allowed
        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        dispatch_lock = getattr(self, "output_dispatch_lock", None)
        if dispatch_lock is not None:
            with dispatch_lock:
                pass

        if (
            self.running
            and not self._runtime_is_game_mode()
            and self.mappings_enabled
            and not self._change_runtime_profile_layer(
                DISABLED_LAYER_NAME, wait=True
            )
        ):
            self.output_shutdown_in_progress = previous_gate
            self.profile_trigger_allowed = previous_allowed
            self.recording_restore_layer = None
            return False

        remaining = self.stop_all_macros(
            play_sound=False, keep_output_gate=True
        )
        release_failures = list(getattr(
            self, "last_macro_release_failures", []
        ))
        if remaining or release_failures:
            if remaining:
                if getattr(self, "_macro_stop_gate_restore", None) is None:
                    self._macro_stop_gate_restore = bool(previous_gate)
                self._defer_profile_input_restore(
                    layer=self.recording_restore_layer,
                    profile_trigger_allowed=previous_allowed,
                    reason="recording_prepare_timeout",
                )
            self.engine_hint.setStyleSheet("color: #fbbf24;")
            self.engine_hint.setText(
                "仍有宏任务正在退出，已取消录制准备；映射将在任务退出后恢复"
                if remaining else
                "录制前仍有输入未能释放，已保持映射禁用；请先强制释放键鼠"
            )
            if release_failures:
                self.write_diagnostic(
                    "recording_prepare_release_failed",
                    force=True,
                    failures=release_failures,
                )
                self.recording_restore_layer = None
            return False

        self.output_shutdown_in_progress = previous_gate
        if self._runtime_is_game_mode():
            self.interception_record_mouse_move = bool(
                self.recording_options.get("record_move")
            )
            if not self.start_interception_input_hook():
                self.profile_trigger_allowed = previous_allowed
                self.recording_restore_layer = None
                return False
            return True
        return True

    def _leave_recording_input_mode(self):
        layer = self.recording_restore_layer
        self.recording_restore_layer = None
        if not layer:
            return True
        if self._runtime_is_game_mode():
            self.interception_record_mouse_move = False
        if not self.running:
            self.profile_trigger_allowed = True
            return True
        if foreground_window_belongs_to_current_process():
            # 录制预览或主窗口仍在前台时继续保持 disabled 层和 Python
            # 触发门关闭。等待用户回到外部窗口后，再由前台档案检测恢复。
            self.profile_trigger_allowed = False
            self.macrocanvas_foreground_suspended = True
            return True
        if self.runtime_profile_auto_switch_enabled:
            target_id, _process_name, _title = self._foreground_profile_id()
            if self._activate_profile_by_id(
                target_id, reason="recording_finished_foreground", immediate=True
            ):
                return True
        return self._restore_active_profile_input(
            reason="recording_finished"
        ) is not False

    def _recording_control_still_held(self):
        return bool(
            self.recording_control_modifiers
            or {
                "record_cancel", "record_finish"
            } & self.system_hotkey_latched
        )

    def _request_recording_restore_check(self):
        """Queue recording cleanup on the GUI thread.

        Physical KeyUp callbacks may arrive on a Windows or Interception input
        thread.  A QTimer created in that thread has no Qt event loop and may
        never fire, so use the MainWindow signal whose connection is explicitly
        queued to the GUI thread.  The direct fallback is only for lightweight
        non-Qt test harnesses.
        """
        signal = getattr(self, "recording_restore_signal", None)
        if signal is not None:
            signal.emit()
            return True
        return self._complete_recording_restore_if_ready()

    def _complete_recording_restore_if_ready(self):
        """Restore mappings only after workflow completion and control KeyUp."""
        if not getattr(self, "recording_restore_pending", False):
            return False
        if not getattr(self, "recording_workflow_complete", False):
            return False
        if self._recording_control_still_held():
            return False
        self.recording_restore_pending = False
        self.recording_workflow_complete = False
        self.recording_session_active = False
        self._set_recording_configuration_actions_enabled(True)
        self.recording_control_modifiers.clear()
        getattr(self, "recording_control_sources", {}).clear()
        self._unlatch_system_hotkey("record_cancel", force=True)
        self._unlatch_system_hotkey("record_finish", force=True)
        restore_ok = self._leave_recording_input_mode() is not False
        if self.update_global_hook_for_backend():
            self._reseed_physical_input_state(
                seed_control=bool(
                    not self.running
                    and self._runtime_is_game_mode()
                    and self.runtime_global_toggle_enabled
                )
            )
        if restore_ok and not self._runtime_cleanup_blocks_new_output():
            self.macro_state = MacroState.IDLE
            self.macro_status_detail = ""
        elif not restore_ok:
            self._show_macro_cleanup_failure(
                "录制结束后输入层恢复失败",
                self._explain_runtime_cleanup_block("recording_restore_failed"),
            )
        self.refresh_status_ui()
        if (
            getattr(self, "auto_apply_checkbox", None) is not None
            and self.auto_apply_checkbox.isChecked()
            and getattr(self, "config_state", None) == ConfigState.DIRTY
        ):
            self.auto_apply_timer.start(300)
        self._auto_apply_deferred_for_recording = False
        return True

    def _mark_recording_workflow_complete(self):
        self.recording_workflow_complete = True
        return self._complete_recording_restore_if_ready()

    def open_recording_dialog(self, card=None, insert_context=None):
        if getattr(self, "recording_session_active", False):
            QMessageBox.information(
                self,
                "录制已在进行",
                "录制倒计时、正式录制或结果整理尚未结束，不能重复开始录制。",
            )
            return
        self.recording_insert_context = copy.deepcopy(insert_context) if insert_context else None
        if card is not None:
            self.select_preset_card(card)
        if self.selected_preset_row() < 0:
            QMessageBox.information(self, "请选择方案", "请先选择要接收录制结果的预设。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("录制设置")
        form = QFormLayout(dialog)
        countdown = QSpinBox()
        countdown.setRange(1, 10)
        countdown.setValue(3)
        countdown.setSuffix(" 秒")
        record_move = QCheckBox("记录鼠标移动")
        move_mode = QComboBox()
        move_mode.addItems([
            "屏幕坐标", "相对移动", "屏幕比例", "前台窗口", "前台客户区"
        ])
        move_mode.setEnabled(False)
        move_interval = QSpinBox()
        move_interval.setRange(10, 1000)
        move_interval.setValue(80)
        move_interval.setSuffix(" ms")
        move_interval.setToolTip(
            "每隔多长时间汇总并保存一次移动；相对模式会累计间隔内的全部原始位移"
        )
        move_interval.setEnabled(False)
        move_info = QLabel()
        move_info.setWordWrap(True)
        move_info.setObjectName("muted")
        min_wait = QSpinBox()
        min_wait.setRange(0, 500)
        min_wait.setValue(40)
        min_wait.setSuffix(" ms")
        form.addRow("开始倒计时", countdown)
        form.addRow("", record_move)
        form.addRow("移动记录方式", move_mode)
        form.addRow("移动采样间隔", move_interval)
        form.addRow("模式说明", move_info)
        form.addRow("忽略短等待", min_wait)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("开始录制")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        def refresh_move_settings(*_args):
            enabled = record_move.isChecked()
            move_mode.setEnabled(enabled)
            move_interval.setEnabled(enabled)
            selected_mode = move_mode.currentText()
            game_only = selected_mode in ("相对移动", "前台窗口", "前台客户区")
            unsupported = enabled and game_only and not self._is_game_mode()
            buttons.button(QDialogButtonBox.Ok).setEnabled(not unsupported)
            descriptions = {
                "屏幕坐标": (
                    "记录可视鼠标在虚拟桌面中的绝对像素坐标。回放时会校验桌面几何，"
                    "避免分辨率或显示器布局变化后误点。"
                ),
                "相对移动": (
                    "直接累计 Interception 原始相对位移，适合游戏隐藏鼠标后的视角转动；"
                    "仅游戏模式可执行。"
                ),
                "屏幕比例": (
                    "将可视鼠标位置换算为虚拟桌面百分比，可适配分辨率变化；"
                    "显示器数量变化时会停止执行该移动。"
                ),
                "前台窗口": (
                    "记录可视鼠标相对于前台窗口外框的位置，并保存进程和尺寸信息；"
                    "回放时校验前台进程并按当前窗口尺寸换算。"
                ),
                "前台客户区": (
                    "记录可视鼠标相对于前台窗口客户区的位置，并保存进程和尺寸信息；"
                    "回放时校验前台进程并按当前客户区尺寸换算。"
                ),
            }
            text = descriptions.get(selected_mode, "")
            if unsupported:
                text += " 当前为普通模式，切换到游戏模式后才能使用该记录方式。"
            if not enabled:
                text = "未启用鼠标移动录制。键盘、鼠标按键和滚轮仍会正常记录。"
            move_info.setText(text)

        record_move.toggled.connect(refresh_move_settings)
        move_mode.currentTextChanged.connect(refresh_move_settings)
        refresh_move_settings()
        if dialog.exec() != QDialog.Accepted:
            self.recording_insert_context = None
            return
        self.recording_options = {
            "record_move": record_move.isChecked(),
            # 保留旧字段，兼容仍读取 coordinates 的旧配置或辅助代码。
            "coordinates": record_move.isChecked(),
            "move_mode": move_mode.currentText(),
            "move_interval_ms": move_interval.value(),
            "min_wait_ms": min_wait.value(),
        }
        self.begin_recording_countdown(countdown.value())

    def begin_recording_countdown(self, seconds):
        cancel_countdown = getattr(self, "_cancel_manual_test_countdown", None)
        if callable(cancel_countdown):
            cancel_countdown("已开始录制，原测试倒计时已取消")
        self.recording_guard_profile_id = str(self.editor_profile_id or "")
        # 游戏模式直接复用 Interception 输入源录制；普通模式才需要
        # Windows 低级钩子观察键鼠事件。
        if not self._runtime_is_game_mode():
            if self.global_hook is None:
                self.start_global_hook()
            if self.global_hook is None:
                QMessageBox.warning(
                    self, "录制监听失败",
                    "无法安装键盘和鼠标录制监听，请尝试以管理员身份运行。",
                )
                return
        if not self._enter_recording_input_mode():
            QMessageBox.warning(
                self, "录制准备失败",
                self.engine.last_command_error
                or self.keyboard_engine.last_command_error
                or "无法临时暂停映射层或启用录制监听。",
            )
            return
        with self.recording_lock:
            self.recorded_events = []
            self.recording_recent_events = {}
        self.recording_cancelled = False
        self.recording_restore_pending = False
        self.recording_workflow_complete = False
        self.recording_target_card = self.selected_preset_card
        self.recording_session_active = True
        if hasattr(self, "auto_apply_timer"):
            self._auto_apply_deferred_for_recording = bool(
                getattr(self, "auto_apply_checkbox", None) is not None
                and self.auto_apply_checkbox.isChecked()
                and getattr(self, "config_state", None) == ConfigState.DIRTY
            )
            self.auto_apply_timer.stop()
        self._set_recording_configuration_actions_enabled(False)
        with self.input_state_lock:
            self.physical_down.clear()
            self.physical_modifiers.clear()
            getattr(self, "physical_input_sources", {}).clear()
            self.suppressed_trigger_names.clear()
            self.interception_forwarded_down.clear()
        self.global_toggle_latched = False
        self.global_toggle_latched_source = None
        self.recording_control_modifiers.clear()
        getattr(self, "recording_control_sources", {}).clear()
        self._unlatch_system_hotkey("record_cancel", force=True)
        self._unlatch_system_hotkey("record_finish", force=True)
        cancel_text = combo_text(
            self.runtime_recording_cancel_modifiers,
            self.runtime_recording_cancel_key,
        ).replace("+", " + ")
        finish_text = combo_text(
            self.runtime_recording_finish_modifiers,
            self.runtime_recording_finish_key,
        ).replace("+", " + ")
        self.activity_overlay.show_message(
            "录制准备",
            f"即将开始 · {cancel_text} 取消；{finish_text} 将在正式录制后生效",
            "#f59e0b",
        )
        self._record_countdown_value = seconds
        self.recording_countdown_tick()

    def recording_countdown_tick(self):
        if not getattr(self, "recording_session_active", False):
            return
        if self._record_countdown_value > 0:
            self.macro_state = MacroState.COUNTDOWN
            self.macro_status_detail = (
                f"{self._record_countdown_value} 秒后开始录制"
            )
            cancel_text = combo_text(
                self.runtime_recording_cancel_modifiers,
                self.runtime_recording_cancel_key,
            ).replace("+", " + ")
            finish_text = combo_text(
                self.runtime_recording_finish_modifiers,
                self.runtime_recording_finish_key,
            ).replace("+", " + ")
            self.activity_overlay.show_message(
                f"{self._record_countdown_value} 秒后开始录制",
                f"{cancel_text} 取消；{finish_text} 将在正式录制后生效",
                "#f59e0b",
            )
            self.refresh_status_ui()
            self._record_countdown_value -= 1
            QTimer.singleShot(1000, self.recording_countdown_tick)
            return
        required_profile_id = str(self.recording_guard_profile_id or "")
        if required_profile_id and not self._foreground_matches_profile(required_profile_id):
            profile_name = self._profile_name(required_profile_id)
            self.recording_guard_profile_id = None
            self.cancel_recording()
            QMessageBox.warning(
                self,
                "当前进程不匹配",
                f"倒计时结束时，前台窗口并不属于“{profile_name}”绑定的进程，"
                "因此没有开始录制。请重新开始并在倒计时内切换到目标程序。",
            )
            return
        self.recording_started_at = time.perf_counter()
        self.recording_finished_at = 0.0
        self.recording_move_origin = self._cursor_position_fields()
        with self.recording_lock:
            self.recorded_events = []
            self.last_recorded_move = self.recording_started_at
            self.recording_pending_move = None
            self.recording_recent_events = {}
            self.recording_limit_reason = ""
            self.recording_limit_stop_requested = False
            self.recording_generation = int(getattr(self, "recording_generation", 0)) + 1
            recording_generation = self.recording_generation
        self.recording = True
        QTimer.singleShot(
            MAX_RECORDING_DURATION_MS,
            lambda generation=recording_generation:
            self._finish_recording_at_duration_limit(generation),
        )
        self.macro_state = MacroState.RECORDING
        self.macro_status_detail = ""
        cancel_text = combo_text(
            self.runtime_recording_cancel_modifiers,
            self.runtime_recording_cancel_key,
        ).replace("+", " + ")
        finish_text = combo_text(
            self.runtime_recording_finish_modifiers,
            self.runtime_recording_finish_key,
        ).replace("+", " + ")
        self.activity_overlay.show_message(
            "● 正在录制",
            f"{cancel_text} 取消录制 · {finish_text} 完成录制",
            "#fb7185",
        )
        self.refresh_status_ui()

    def _finish_recording_at_duration_limit(self, generation):
        with self.recording_lock:
            if (
                generation != int(getattr(self, "recording_generation", 0))
                or not self.recording
                or self.recording_restore_pending
            ):
                return
            self.recording_limit_reason = (
                f"录制时长已达到 {MAX_RECORDING_DURATION_MS // 60000} 分钟上限"
            )
        self.finish_recording()

    @Slot(dict)
    def handle_recorded_event(self, event):
        # 兼容旧的信号入口；当前底层钩子会直接写入带锁事件队列。
        self._store_recorded_event(event)

    def cancel_recording(self):
        if getattr(self, "recording_restore_pending", False):
            return
        self.recording_guard_profile_id = None
        self.recording_restore_pending = True
        self.recording_workflow_complete = False
        self.recording_cancelled = True
        with self.recording_lock:
            self.recording = False
            self.recording_generation = int(getattr(self, "recording_generation", 0)) + 1
            self.recorded_events = []
            self.recording_pending_move = None
            self.recording_recent_events = {}
            self.recording_limit_reason = ""
            self.recording_limit_stop_requested = False
        self.recording_target_card = None
        self.recording_insert_context = None
        self.recording_move_origin = {}
        with self.input_state_lock:
            self.physical_down.clear()
            self.physical_modifiers.clear()
            getattr(self, "physical_input_sources", {}).clear()
            self.suppressed_trigger_names.clear()
            self.interception_forwarded_down.clear()
        self.global_toggle_latched = False
        self.global_toggle_latched_source = None
        if hasattr(self, "activity_overlay"):
            self.activity_overlay.hide_message()
        self._mark_recording_workflow_complete()

    @Slot()
    def finish_recording(self):
        if getattr(self, "recording_restore_pending", False):
            return
        if (
            not self.recording
            and getattr(self, "recording_session_active", False)
        ):
            # 倒计时阶段尚无可整理的录制内容。“完成”只在正式录制开始后
            # 生效，避免与“取消录制”产生相同结果。
            return
        finished_at = time.perf_counter()
        hook = self.interception_input_hook
        if hook is not None and getattr(hook, "capture_mouse_move", False):
            try:
                hook.flush_mouse_move_events()
            except Exception as error:
                self.write_diagnostic(
                    "recording_mouse_flush_error", error=str(error)
                )
        with self.recording_lock:
            if not self.recording:
                return
            self.recording_finished_at = finished_at
            self._flush_pending_recorded_move_locked()
            self.recording = False
            self.recording_generation = int(getattr(self, "recording_generation", 0)) + 1
            limit_reason = str(getattr(self, "recording_limit_reason", "") or "")
            events = sorted(
                (dict(event) for event in self.recorded_events),
                key=lambda event: float(event.get("time", 0)),
            )
            # 后续流程只使用局部快照。立即释放原始共享队列，避免结果预览
            # 和动作控件构建期间同时保留两份大列表。
            self.recorded_events = []
            self.recording_recent_events = {}
            self.recording_limit_reason = ""
            self.recording_limit_stop_requested = False
        self.recording_restore_pending = True
        self.recording_workflow_complete = False
        self.recording_guard_profile_id = None
        self._begin_loading(
            "正在整理录制结果",
            (
                f"{limit_reason}，已自动停止；正在把 {len(events)} 条原始输入转换为动作……"
                if limit_reason else
                f"正在把 {len(events)} 条原始输入转换为动作……"
            ),
            host=self,
        )
        conversion_error = ""
        try:
            actions = self.convert_recording_to_actions(events)
        except Exception as error:
            actions = []
            conversion_error = str(error)
        finally:
            self._end_loading()
        self.recording_move_origin = {}
        self.recording_pending_move = None
        target_card = self.recording_target_card
        insert_context = copy.deepcopy(getattr(self, "recording_insert_context", None))
        self.recording_target_card = None
        self.recording_insert_context = None
        with self.input_state_lock:
            self.physical_down.clear()
            self.physical_modifiers.clear()
            getattr(self, "physical_input_sources", {}).clear()
            self.suppressed_trigger_names.clear()
            self.interception_forwarded_down.clear()
        self.global_toggle_latched = False
        self.global_toggle_latched_source = None
        if hasattr(self, "activity_overlay"):
            self.activity_overlay.hide_message()
        if conversion_error:
            QMessageBox.warning(
                self,
                "录制结果整理失败",
                f"无法把本次录制转换为动作：\n{conversion_error}",
            )
            self._mark_recording_workflow_complete()
            return
        if not actions:
            QMessageBox.information(
                self,
                "录制为空",
                "没有捕获到可用的键鼠动作。请确认倒计时结束后再操作，"
                "并检查录制监听是否启动成功。",
            )
            self._mark_recording_workflow_complete()
            return
        converted_count = sum(1 for _ in iter_action_tree(actions))
        if converted_count > MAX_ACTION_COUNT:
            QMessageBox.warning(
                self,
                "录制动作数量超限",
                f"本次录制转换后包含 {converted_count} 个动作，超过单个预设 "
                f"{MAX_ACTION_COUNT} 个动作的上限。请缩短录制时间或提高鼠标采样间隔。",
            )
            self._mark_recording_workflow_complete()
            return
        if target_card is None or target_card not in self.preset_cards:
            QMessageBox.warning(self, "录制目标失效", "原预设已不存在，录制结果未写入。")
            self._mark_recording_workflow_complete()
            return
        try:
            prepared = self.preview_recording_import(
                events, actions, insert_context=insert_context
            )
        except Exception as error:
            QMessageBox.warning(
                self, "录制结果预览失败", f"无法打开录制结果预览：\n{error}"
            )
            self._mark_recording_workflow_complete()
            return
        if prepared is None:
            self._mark_recording_workflow_complete()
            return
        actions, import_mode = prepared
        imported_count = sum(1 for _ in iter_action_tree(actions))
        action_dialog = getattr(target_card, "action_dialog", None)
        loading_host = (
            action_dialog
            if isinstance(action_dialog, QWidget) and action_dialog.isVisible()
            else self._loading_host_widget()
        )
        self._begin_loading(
            "正在写入录制结果",
            f"正在准备向动作菜单写入 {imported_count} 个录制动作……",
            host=loading_host,
        )
        write_error = ""
        write_rejected = False
        try:
            self.select_preset_card(target_card)
            if insert_context:
                self._set_loading_message(
                    "正在准备动作菜单",
                    "正在定位当前动作并计算录制结果的写入位置……",
                )
                existing = self.collect_visible_actions(target_card)
                actions = self._merge_recording_at_action(
                    existing, actions, insert_context, import_mode
                )
            elif import_mode == "追加到末尾":
                self._set_loading_message(
                    "正在准备动作菜单",
                    "正在读取目标预设中的原有动作……",
                )
                actions = self.collect_visible_actions(target_card) + actions
            total_to_build = sum(1 for _ in iter_action_tree(actions))
            self._set_loading_message(
                "正在写入动作菜单",
                f"正在重建目标预设的 {total_to_build} 个动作项……",
            )
            if self.load_actions(actions, target_card) is not True:
                write_rejected = True
            else:
                self._set_loading_message(
                    "正在保存动作变更",
                    "正在同步循环引用、撤销记录和预设状态……",
                )
                self.action_changed(target_card)
                self._loading_checkpoint(force=True)
        except Exception as error:
            write_error = str(error)
        finally:
            self._end_loading()
        if write_error:
            QMessageBox.warning(self, "录制写入失败", write_error)
            self._mark_recording_workflow_complete()
            return
        if write_rejected:
            self._mark_recording_workflow_complete()
            return
        if hasattr(target_card, "action_dialog"):
            self.open_preset_actions_dialog(target_card)
        self.tabs.setCurrentIndex(1)
        if self.action_table is not None and self.action_table.topLevelItemCount():
            self.action_table.setCurrentItem(self.action_table.topLevelItem(0))
        total_actions = sum(1 for _ in iter_action_tree(actions))
        self.engine_hint.setText(
            (
                f"录制达到上限后已自动停止：已载入 {len(actions)} 个动作组、"
                f"{total_actions} 个动作"
                if limit_reason else
                f"录制完成：已载入 {len(actions)} 个动作组、{total_actions} 个动作"
            )
        )
        self._mark_recording_workflow_complete()

    def preview_recording_import(self, events, actions, insert_context=None):
        dialog = QDialog(self)
        dialog.setWindowTitle("整理并导入录制结果")
        form = QFormLayout(dialog)
        mode = QComboBox()
        if insert_context:
            mode.addItems(["添加在当前位置下方", "覆盖下方所有动作"])
        else:
            mode.addItems(["替换原动作", "追加到末尾"])
        speed = QSpinBox()
        speed.setRange(10, 500)
        speed.setValue(100)
        speed.setSuffix(" %")
        min_hold = QSpinBox()
        min_hold.setRange(1, 500)
        min_hold.setValue(30)
        min_hold.setSuffix(" ms")
        simplify_moves = QCheckBox("简化密集鼠标移动轨迹")
        simplify_moves.setChecked(False)
        if self.recording_options.get("move_mode") == "相对移动":
            simplify_moves.setEnabled(False)
            simplify_moves.setToolTip(
                "游戏视角的原始相对位移默认逐段保留，避免合并输入包后改变转动结果。"
            )
        else:
            simplify_moves.setToolTip(
                "默认保留完整录制轨迹；需要减少动作数量时可手动启用。"
            )
        merge_wheel = QCheckBox("合并短间隔内的同方向滚轮")
        merge_wheel.setChecked(True)
        merge_gap = QSpinBox()
        merge_gap.setRange(0, 2000)
        merge_gap.setValue(max(120, int(
            self.recording_options.get("move_interval_ms", 80) or 80
        ) + 20))
        merge_gap.setSuffix(" ms")
        merge_gap.setToolTip("不超过该时长的等待可被视为同一段移动或滚轮序列")
        move_tolerance = QSpinBox()
        move_tolerance.setRange(0, 100)
        move_tolerance.setValue(6)
        move_tolerance.setSuffix(" px")
        move_tolerance.setToolTip("数值越大，轨迹点越少；0 表示保留全部轨迹点")
        summary = QLabel()
        summary.setWordWrap(True)
        _left, _top, virtual_width, virtual_height = self._virtual_screen_geometry()
        raw_count = len(events)
        before = sum(1 for _ in iter_action_tree(actions))
        before_moves = sum(
            action.get("type") == "鼠标移动"
            for action in iter_action_tree(actions)
        )
        duration = 0.0
        if events:
            duration = max(
                0.0,
                float(events[-1].get("time", 0))
                - float(events[0].get("time", 0)),
            )
        organized_cache = {"key": None, "actions": None, "count": 0, "moves": 0}

        def organized_actions():
            key = (
                speed.value(),
                min_hold.value(),
                simplify_moves.isChecked(),
                merge_wheel.isChecked(),
                merge_gap.value(),
                move_tolerance.value(),
                virtual_width,
                virtual_height,
            )
            if organized_cache["key"] == key:
                return organized_cache["actions"]
            cleaned = simplify_recorded_actions(
                actions,
                speed_percent=speed.value(),
                min_hold_ms=min_hold.value(),
                simplify_moves=simplify_moves.isChecked(),
                merge_wheel=merge_wheel.isChecked(),
                merge_gap_ms=merge_gap.value(),
                move_tolerance=move_tolerance.value(),
                percentage_size=(virtual_width, virtual_height),
            )
            organized_cache["key"] = key
            organized_cache["actions"] = cleaned
            organized_cache["count"] = sum(1 for _ in iter_action_tree(cleaned))
            organized_cache["moves"] = sum(
                action.get("type") == "鼠标移动"
                for action in iter_action_tree(cleaned)
            )
            return cleaned

        def refresh_summary(*_args):
            organized_actions()
            summary.setText(
                f"原始事件：{raw_count} · 动作：{before} → {organized_cache['count']} · "
                f"鼠标移动：{before_moves} → {organized_cache['moves']} · "
                f"录制时长：{duration:.2f} 秒"
            )

        for control in (speed, min_hold, merge_gap, move_tolerance):
            control.valueChanged.connect(refresh_summary)
        simplify_moves.toggled.connect(refresh_summary)
        merge_wheel.toggled.connect(refresh_summary)
        form.addRow("导入方式", mode)
        form.addRow("整体速度", speed)
        form.addRow("最短按住", min_hold)
        form.addRow("", simplify_moves)
        form.addRow("", merge_wheel)
        form.addRow("合并间隔上限", merge_gap)
        form.addRow("轨迹容差", move_tolerance)
        form.addRow("预览", summary)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导入")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        refresh_summary()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return organized_actions(), mode.currentText()

    def convert_recording_to_actions(self, events):
        """Convert raw input into root groups and concurrent child timelines."""
        pressed = {}
        completed = []
        origin = self.recording_move_origin or {}
        try:
            previous_move_position = (int(origin["x"]), int(origin["y"]))
        except (KeyError, TypeError, ValueError):
            previous_move_position = None
        for event in events:
            kind = event.get("kind")
            timestamp = float(event.get("time", 0))
            if kind == "key":
                name = event.get("name")
                if name not in KEY_NAMES:
                    continue
                key = ("key", name)
                if event.get("down"):
                    pressed.setdefault(key, timestamp)
                elif key in pressed:
                    started = pressed.pop(key)
                    completed.append((
                        started, timestamp,
                        {
                            "type": "键盘点击",
                            "target": name,
                            "hold_ms": max(20, int((timestamp - started) * 1000)),
                            "children": [],
                        },
                    ))
            elif kind == "button":
                name = event.get("name")
                key = ("button", name)
                if event.get("down"):
                    pressed.setdefault(key, timestamp)
                elif key in pressed:
                    started = pressed.pop(key)
                    completed.append((
                        started, timestamp,
                        {
                            "type": "鼠标点击",
                            "target": name,
                            "hold_ms": max(20, int((timestamp - started) * 1000)),
                            "children": [],
                        },
                    ))
            elif kind == "wheel":
                completed.append((
                    timestamp, timestamp,
                    {
                        "type": "鼠标滚轮",
                        "target": "向上" if event.get("delta", 0) > 0 else "向下",
                        "steps": max(1, abs(int(event.get("delta", 120))) // 120),
                        "children": [],
                    },
                ))
            elif kind == "move" and self.recording_options.get("coordinates"):
                target, previous_move_position, recording_context = (
                    self._recorded_move_target(event, previous_move_position)
                )
                if target is None:
                    continue
                action = {
                    "type": "鼠标移动",
                    "target": target,
                    "children": [],
                }
                if isinstance(recording_context, dict):
                    action["recording_context"] = recording_context
                completed.append((timestamp, timestamp, action))

        recording_end = self.recording_finished_at or time.perf_counter()
        for (kind, name), started in list(pressed.items()):
            action = {
                "type": "键盘点击" if kind == "key" else "鼠标点击",
                "target": name,
                "hold_ms": max(20, int((recording_end - started) * 1000)),
                "children": [],
            }
            completed.append((started, recording_end, action))

        completed.sort(
            key=lambda item: (item[0], -(item[1] - item[0]))
        )
        if not completed:
            return []

        # Events whose active intervals overlap belong to one concurrent group.
        # A tiny tolerance also groups physically simultaneous key-down events.
        groups = []
        tolerance = 0.006
        for started, ended, action in completed:
            action_is_move = action.get("type") == "鼠标移动"
            if not groups or started > groups[-1]["end"] + tolerance:
                groups.append({
                    "start": started,
                    "end": max(started, ended),
                    "events": [(started, ended, action)],
                    "has_move": action_is_move,
                })
            else:
                groups[-1]["events"].append((started, ended, action))
                groups[-1]["end"] = max(groups[-1]["end"], ended)
                groups[-1]["has_move"] = (
                    groups[-1]["has_move"] or action_is_move
                )

        result = []
        previous_group_end = self.recording_started_at
        previous_group_had_move = False
        minimum_wait = int(self.recording_options.get("min_wait_ms", 40))
        for group in groups:
            gap_ms = max(
                0, round((group["start"] - previous_group_end) * 1000)
            )
            preserve_move_timing = (
                bool(group.get("has_move")) or previous_group_had_move
            )
            if gap_ms > 0 and (
                preserve_move_timing or gap_ms >= minimum_wait
            ):
                result.append({
                    "type": "等待", "wait_ms": gap_ms, "children": []
                })

            events_in_group = sorted(
                group["events"],
                key=lambda item: (item[0], -(item[1] - item[0])),
            )
            root_start, _root_end, root = events_in_group[0]
            root = dict(root)
            root["children"] = []
            timeline_time = root_start
            previous_action_was_move = root.get("type") == "鼠标移动"
            for started, _ended, action in events_in_group[1:]:
                offset_ms = max(0, round((started - timeline_time) * 1000))
                action_is_move = action.get("type") == "鼠标移动"
                if offset_ms > 0 and (
                    previous_action_was_move
                    or action_is_move
                    or offset_ms >= minimum_wait
                ):
                    root["children"].append({
                        "type": "等待",
                        "wait_ms": offset_ms,
                        "children": [],
                    })
                    timeline_time = started
                child = dict(action)
                child["children"] = []
                root["children"].append(child)
                previous_action_was_move = action_is_move
            result.append(root)
            previous_group_end = group["end"]
            previous_group_had_move = bool(group.get("has_move"))
        return result
