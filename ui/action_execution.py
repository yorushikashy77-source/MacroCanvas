"""Manual preset execution and recording entry points."""

import copy

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from core.constants import ACTION_ID_ROLE, ConfigState, MacroState
from macro.actions import clone_action_tree
from macro.simulation import simulate_preset
from ui.runtime_guards import (
    explain_runtime_cleanup_block, runtime_cleanup_blocks_new_output,
    runtime_transaction_busy,
)
from ui.simulation_preview import SimulationPreviewDialog


class ActionExecutionMixin:

    def preview_selected_preset_simulation(self, card=None):
        """Preview current editor contents without touching an input backend."""
        if card is not None:
            self.select_preset_card(card)
        card = getattr(self, "selected_preset_card", None)
        if card not in getattr(self, "preset_cards", []):
            QMessageBox.information(self, "请选择方案", "请先选择一个预设方案。")
            return None
        preset = next(
            (
                item for item in self.collect_presets()
                if str(item.get("id") or "") == str(card.preset_id or "")
            ),
            None,
        )
        if preset is None:
            QMessageBox.warning(self, "无法预览", "当前预设内容无法读取。")
            return None
        report = simulate_preset(preset)
        dialog = SimulationPreviewDialog(report, self)
        dialog.setStyleSheet(self.styleSheet())
        dialog.exec()
        return report

    def _cancel_manual_test_countdown(self, reason=""):
        """Cancel a menu/debug countdown before editable state can diverge."""
        if (
            getattr(self, "macro_state", None) != MacroState.COUNTDOWN
            or not str(getattr(self, "_test_countdown_preset_id", "") or "")
        ):
            return False
        self._test_countdown_generation = int(getattr(
            self, "_test_countdown_generation", 0
        )) + 1
        self._test_countdown_preset_id = None
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        if hasattr(self, "activity_overlay"):
            self.activity_overlay.hide_message()
        if reason and hasattr(self, "engine_hint"):
            self.engine_hint.setStyleSheet("")
            self.engine_hint.setText(str(reason))
        if hasattr(self, "refresh_status_ui"):
            self.refresh_status_ui()
        return True

    def _runtime_cleanup_blocks_new_output(self):
        return runtime_cleanup_blocks_new_output(self)

    def _explain_runtime_cleanup_block(self, context="runtime_trigger"):
        return explain_runtime_cleanup_block(self, context)

    def _runtime_transaction_blocks_manual_test(self, context):
        """Cancel a pending manual test/debug countdown during unsafe states."""
        recording_active = bool(getattr(self, "recording_session_active", False))
        if not runtime_transaction_busy(self) and not recording_active:
            return False
        if hasattr(self, "write_diagnostic"):
            self.write_diagnostic(
                "manual_test_rejected",
                context=context,
                reason=("recording_active" if recording_active else "transaction_busy"),
                macro_state=str(getattr(
                    getattr(self, "macro_state", None),
                    "name",
                    getattr(self, "macro_state", ""),
                )),
            )
        self._test_countdown_generation = int(getattr(
            self, "_test_countdown_generation", 0
        )) + 1
        self._test_countdown_preset_id = None
        if getattr(self, "macro_state", None) == MacroState.COUNTDOWN:
            self.macro_state = MacroState.IDLE
            self.macro_status_detail = ""
            if hasattr(self, "activity_overlay") and not getattr(
                self, "recording_session_active", False
            ):
                self.activity_overlay.hide_message()
            self.refresh_status_ui()
        return True

    def _build_menu_test_task(self, preset):
        """Build an isolated task for the UI test button."""
        test_rule = self._preset_as_mapping_rule(preset)
        test_task = self.mapping_to_task(test_rule)
        origin_preset_id = str(preset.get("id") or "")
        test_task.update({
            "id": f"test:{origin_preset_id}",
            "_origin_preset_id": origin_preset_id,
            "name": f"{preset.get('name', '预设')} · 菜单测试",
            "_required_profile_id": str(self.active_profile_id or ""),
        })
        if test_task.get("execution_mode") in (
            "按住循环", "开关循环", "无限循环"
        ):
            test_task["execution_mode"] = "执行一次"
            test_task["loop_count"] = 1
            test_task["loop_interval_ms"] = 0
            test_task["loop_interval_jitter_ms"] = 0
            test_task["max_runtime_s"] = 0
        return test_task

    @staticmethod
    def _action_slice_from_id(actions, action_id):
        """Return the selected action and following siblings from its own level."""
        action_id = str(action_id or "")
        for index, action in enumerate(actions or []):
            if str(action.get("action_id") or "") == action_id:
                return clone_action_tree(actions[index:])
            nested = ActionExecutionMixin._action_slice_from_id(
                action.get("children", []) or [], action_id
            )
            if nested is not None:
                return nested
        return None

    @staticmethod
    def _merge_recording_at_action(actions, recorded, context, mode):
        """Insert/replace following siblings while preserving the surrounding tree."""
        target_id = str((context or {}).get("action_id") or "")
        payload = clone_action_tree(actions)
        recorded = clone_action_tree(recorded)

        def merge_level(level):
            for index, action in enumerate(level):
                if str(action.get("action_id") or "") == target_id:
                    if mode == "覆盖下方所有动作":
                        level[index + 1:] = recorded
                    else:
                        level[index + 1:index + 1] = recorded
                    return True
                if merge_level(action.get("children", []) or []):
                    return True
            return False

        if not merge_level(payload):
            raise ValueError("录制起始动作已不存在，无法确定写入位置")
        return payload

    def _current_action_context(self, card):
        if card is None:
            return None
        item = card.action_table.currentItem()
        if item is None:
            selected = card.action_table.selectedItems()
            item = selected[0] if selected else None
        if item is None or self.is_loop_action_item(item):
            return None
        return {
            "action_id": str(item.data(0, ACTION_ID_ROLE) or ""),
        }

    def run_from_current_action(self, card=None):
        card = card or self.selected_preset_card
        context = self._current_action_context(card)
        if context is None:
            QMessageBox.information(
                self,
                "请选择动作",
                "请先选择一个普通动作，再执行其所在层级中的后续动作。",
            )
            return
        if self.config_state in (ConfigState.DIRTY, ConfigState.FAILED):
            QMessageBox.information(
                self, "存在未应用更改",
                "当前配置尚未成功应用。请先修正问题并点击“应用更改”，再进行调试。",
            )
            return
        if not self._macro_backend_active():
            QMessageBox.information(
                self, "输入引擎未启动", "请先启动输入引擎，再执行当前动作。"
            )
            return
        if self._runtime_transaction_blocks_manual_test("debug_current_action"):
            return
        if self._runtime_cleanup_blocks_new_output():
            failures = self._explain_runtime_cleanup_block("debug_current_action")
            QMessageBox.warning(
                self,
                "按键释放未完成",
                "上一次停止或异常清理仍有未确认释放的输入：\n"
                + "\n".join(f"- {item}" for item in failures)
                + "\n\n请先执行“强制释放键鼠”，再启动新的调试任务。",
            )
            return

        preset_id = card.preset_id
        preset_name = card.name.text().strip() or "预设"
        expected_profile_id = str(self.editor_profile_id or "")
        action_id = context["action_id"]
        countdown_seconds = 5
        self._test_countdown_generation += 1
        countdown_generation = self._test_countdown_generation
        self._test_countdown_preset_id = str(preset_id or "")

        def finish_countdown():
            if countdown_generation != self._test_countdown_generation:
                return
            self._test_countdown_preset_id = None
            if self._runtime_transaction_blocks_manual_test(
                "debug_current_action_countdown"
            ):
                return
            if self._runtime_cleanup_blocks_new_output():
                failures = self._explain_runtime_cleanup_block("debug_current_action_countdown")
                self.refresh_status_ui()
                QMessageBox.warning(
                    self,
                    "按键释放未完成",
                    "倒计时期间检测到仍有未确认释放的输入：\n"
                    + "\n".join(f"- {item}" for item in failures)
                    + "\n\n请先执行“强制释放键鼠”，再启动新的调试任务。",
                )
                return

            # 倒计时结束后才读取当前前台进程所对应的运行配置，避免动作菜单
            # 仍占据焦点时提前用错误档案判定并拒绝执行。
            if str(self.active_profile_id or "") != expected_profile_id:
                self.macro_state = MacroState.IDLE
                self.macro_status_detail = ""
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
                QMessageBox.warning(
                    self,
                    "当前运行配置不匹配",
                    "倒计时结束后，当前运行档案与发起调试时正在编辑的档案不同。"
                    "为避免跨档案同名 ID 误执行，本次调试已取消。",
                )
                return
            with self.data_lock:
                runtime = next(
                    (item for item in self.runtime_presets
                     if item.get("id") == preset_id),
                    None,
                )
                preset = copy.deepcopy(runtime) if runtime else None

            self.macro_status_detail = ""
            if not preset:
                self.macro_state = MacroState.IDLE
                self.engine_hint.setText(
                    f"“{preset_name}”未执行：倒计时结束时不在当前运行配置中"
                )
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
                QMessageBox.warning(
                    self,
                    "当前运行配置不匹配",
                    "5 秒倒计时结束后，当前前台进程对应的运行配置中仍未找到"
                    f"“{preset_name}”。请确认已切换到绑定该配置的程序窗口。",
                )
                return
            if not self.mappings_enabled:
                self.macro_state = MacroState.IDLE
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
                QMessageBox.information(
                    self, "映射已暂停", "请先恢复全局映射，再执行当前动作。"
                )
                return

            actions = self._action_slice_from_id(
                preset.get("actions", []), action_id
            )
            if not actions:
                self.macro_state = MacroState.IDLE
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
                QMessageBox.warning(
                    self,
                    "动作已变化",
                    "倒计时结束后，无法在当前运行配置中找到所选动作。"
                    "请确认已进入正确程序，并重新应用更改后再试。",
                )
                return

            origin_preset_id = str(preset.get("id") or "")
            debug_task_id = f"debug:{origin_preset_id}"
            with self.macro_controller.lock:
                existing_debug_task = self.macro_controller.tasks.get(debug_task_id)
                debug_task_running = bool(
                    existing_debug_task is not None
                    and existing_debug_task.has_live_threads()
                )
            if debug_task_running:
                self.macro_state = (
                    MacroState.RUNNING
                    if existing_debug_task.run_event.is_set()
                    else MacroState.PAUSED
                )
                self.macro_status_detail = ""
                self.engine_hint.setText(
                    f"“{preset_name}”的当前动作调试仍在执行"
                )
                self.refresh_status_ui()
                QMessageBox.information(
                    self,
                    "调试任务正在运行",
                    "同一预设一次只能运行一个“当前层后续动作”调试任务。"
                    "请先停止当前调试任务，或等待它执行完成。",
                )
                return

            preset.update({
                # 同一预设只允许存在一个“当前层后续动作”调试任务。稳定 ID
                # 同时让删除预设、停止当前宏和任务状态显示都能追踪该任务。
                "id": debug_task_id,
                "_origin_preset_id": origin_preset_id,
                "name": f"{preset.get('name', '预设')} · 当前层后续动作",
                "enabled": True,
                "execution_mode": "执行一次",
                "loop_count": 1,
                "loop_interval_ms": 0,
                "loop_interval_jitter_ms": 0,
                "max_runtime_s": 0,
                "actions": actions,
            })
            preset["_required_profile_id"] = str(self.active_profile_id or "")
            if self.macro_controller.start(preset):
                self.macro_state = MacroState.RUNNING
                self.engine_hint.setText(
                    f"正在执行“{preset_name}”当前层的后续动作"
                )
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
            else:
                if self._runtime_cleanup_blocks_new_output():
                    self._show_macro_cleanup_failure(
                        "当前动作未能启动，按键释放未完成",
                        self._explain_runtime_cleanup_block("debug_start_failed"),
                    )
                else:
                    self.macro_state = MacroState.IDLE
                    if hasattr(self, "activity_overlay"):
                        self.activity_overlay.hide_message()
                    QMessageBox.warning(
                        self, "执行失败", "当前动作未能启动，请检查输入引擎状态。"
                    )
                self.refresh_status_ui()

        def countdown_tick(remaining):
            if countdown_generation != self._test_countdown_generation:
                return
            if self._runtime_transaction_blocks_manual_test(
                "debug_current_action_countdown_tick"
            ):
                return
            if remaining > 0:
                detail = (
                    f"请切换到目标程序，{remaining} 秒后检查运行配置并执行"
                    f"“{preset_name}”当前层的后续动作"
                )
                self.macro_state = MacroState.COUNTDOWN
                self.macro_status_detail = detail
                self.engine_hint.setText(detail)
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.show_message(
                        "当前动作调试准备中", detail, "#fbbf24"
                    )
                self.refresh_status_ui()
                QTimer.singleShot(
                    1000, lambda value=remaining - 1: countdown_tick(value)
                )
                return
            finish_countdown()

        countdown_tick(countdown_seconds)

    def record_from_current_action(self, card=None):
        card = card or self.selected_preset_card
        context = self._current_action_context(card)
        if context is None:
            QMessageBox.information(
                self, "请选择动作", "请先选择一个普通动作，再从当前位置开始录制。"
            )
            return
        self.open_recording_dialog(card, insert_context=context)

    def test_selected_preset(self, card=None):
        if card is not None:
            self.select_preset_card(card)
        row = self.selected_preset_row()
        if row < 0:
            QMessageBox.information(self, "请选择方案", "请先选择一个预设方案。")
            return
        if self.config_state in (ConfigState.DIRTY, ConfigState.FAILED):
            QMessageBox.information(
                self,
                "存在未应用更改",
                "当前配置尚未成功应用。请先修正问题并点击“应用更改”，再进行测试。",
            )
            return
        if not self._macro_backend_active():
            QMessageBox.information(
                self, "输入引擎未启动", "请先启动输入引擎，再测试预设方案。"
            )
            return
        if self._runtime_transaction_blocks_manual_test("test_preset"):
            return
        if self._runtime_cleanup_blocks_new_output():
            failures = self._explain_runtime_cleanup_block("test_preset")
            QMessageBox.warning(
                self,
                "按键释放未完成",
                "上一次停止或异常清理仍有未确认释放的输入：\n"
                + "\n".join(f"- {item}" for item in failures)
                + "\n\n请先执行“强制释放键鼠”，再测试预设方案。",
            )
            return

        selected_card = self.preset_cards[row]
        preset_id = selected_card.preset_id
        preset_name = selected_card.name.text().strip() or "预设"
        expected_profile_id = str(self.editor_profile_id or "")

        countdown_seconds = 5
        self._test_countdown_generation += 1
        countdown_generation = self._test_countdown_generation
        self._test_countdown_preset_id = str(preset_id or "")

        def finish_countdown():
            if countdown_generation != self._test_countdown_generation:
                return
            self._test_countdown_preset_id = None
            if self._runtime_transaction_blocks_manual_test(
                "test_preset_countdown"
            ):
                return
            if self._runtime_cleanup_blocks_new_output():
                failures = self._explain_runtime_cleanup_block("test_preset_countdown")
                self.refresh_status_ui()
                QMessageBox.warning(
                    self,
                    "按键释放未完成",
                    "倒计时期间检测到仍有未确认释放的输入：\n"
                    + "\n".join(f"- {item}" for item in failures)
                    + "\n\n请先执行“强制释放键鼠”，再测试预设方案。",
                )
                return

            # Only now inspect the active runtime profile. This gives the user
            # time to switch focus to the target process and lets automatic
            # profile matching install the corresponding runtime preset first.
            if str(self.active_profile_id or "") != expected_profile_id:
                self.macro_state = MacroState.IDLE
                self.macro_status_detail = ""
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
                QMessageBox.warning(
                    self,
                    "当前运行配置不匹配",
                    "倒计时结束后，当前运行档案与发起测试时正在编辑的档案不同。"
                    "为避免跨档案同名 ID 误执行，本次测试已取消。",
                )
                return
            with self.data_lock:
                preset = None
                for item in self.runtime_presets:
                    if item.get("id") == preset_id:
                        preset = dict(item)
                        preset["actions"] = clone_action_tree(
                            item.get("actions", [])
                        )
                        break

            self.macro_status_detail = ""
            if not preset:
                self.macro_state = MacroState.IDLE
                self.engine_hint.setText(
                    f"“{preset_name}”未执行：倒计时结束时不在当前运行配置中"
                )
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
                QMessageBox.warning(
                    self,
                    "当前运行配置不匹配",
                    "5 秒倒计时结束后，当前前台进程对应的运行配置中仍未找到"
                    f"“{preset_name}”。请确认已切换到绑定该配置的程序窗口。",
                )
                return
            if not preset.get("enabled"):
                self.macro_state = MacroState.IDLE
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
                QMessageBox.information(
                    self, "预设未启用",
                    "该预设左侧的“启用”尚未勾选。启用后才能测试或由绑定快捷键触发。"
                )
                return
            if not self.mappings_enabled:
                self.macro_state = MacroState.IDLE
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
                QMessageBox.information(
                    self, "映射已暂停",
                    "当前全局映射处于暂停状态。恢复映射后才能执行测试。"
                )
                return
            if not preset.get("actions"):
                self.macro_state = MacroState.IDLE
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.hide_message()
                self.refresh_status_ui()
                QMessageBox.information(
                    self, "方案为空", "请先在该方案下方添加至少一个动作。"
                )
                return

            # A menu test is not a physical trigger edge. Give it an independent
            # task identity and never create held/toggle ownership. Modes that
            # require a later KeyUp or a second Down are reduced to one complete
            # pass, while fixed-count and ordinary one-shot modes keep their
            # configured execution semantics.
            test_task = self._build_menu_test_task(preset)

            started = bool(self.macro_controller.start(test_task))
            if started:
                self.active_macro_id = test_task["id"]
                self.macro_state = MacroState.RUNNING
                self.engine_hint.setText(f"正在运行“{preset['name']}”")
            else:
                with self.macro_controller.lock:
                    existing = self.macro_controller.tasks.get(test_task["id"])
                    existing_running = bool(
                        existing is not None and existing.has_live_threads()
                    )
                if existing_running:
                    self.active_macro_id = test_task["id"]
                    self.macro_state = (
                        MacroState.RUNNING
                        if existing.run_event.is_set() else MacroState.PAUSED
                    )
                    self.engine_hint.setText(
                        f"“{preset.get('name', '预设')}”的菜单测试仍在执行"
                    )
                    QMessageBox.information(
                        self,
                        "测试任务正在运行",
                        "同一预设一次只能运行一个菜单测试任务。请先停止当前测试，"
                        "或等待它执行完成。",
                    )
                else:
                    if self._runtime_cleanup_blocks_new_output():
                        self._show_macro_cleanup_failure(
                            "测试任务未能启动，按键释放未完成",
                            self._explain_runtime_cleanup_block("menu_test_start_failed"),
                        )
                    else:
                        self.macro_state = MacroState.IDLE
                        self.engine_hint.setText(
                            f"“{preset.get('name', '预设')}”未执行：测试任务启动失败"
                        )
                        if hasattr(self, "activity_overlay"):
                            self.activity_overlay.hide_message()
            self.refresh_status_ui()

        def countdown_tick(remaining):
            if countdown_generation != self._test_countdown_generation:
                return
            if self._runtime_transaction_blocks_manual_test(
                "test_preset_countdown_tick"
            ):
                return
            if remaining > 0:
                detail = (
                    f"请切换到目标程序，{remaining} 秒后检查运行配置并执行“{preset_name}”"
                )
                self.macro_state = MacroState.COUNTDOWN
                self.macro_status_detail = detail
                self.engine_hint.setText(detail)
                if hasattr(self, "activity_overlay"):
                    self.activity_overlay.show_message(
                        "测试方案准备中", detail, "#fbbf24"
                    )
                self.refresh_status_ui()
                QTimer.singleShot(
                    1000, lambda value=remaining - 1: countdown_tick(value)
                )
                return
            finish_countdown()

        countdown_tick(countdown_seconds)
