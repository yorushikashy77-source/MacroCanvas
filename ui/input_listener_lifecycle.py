"""Lifecycle management for Windows and Interception input listeners."""

from __future__ import annotations

import os
import time

from core.constants import EngineState
from engine.input_backend import InterceptionInputHook, InterceptionOutput, WinInput


class InputListenerLifecycleMixin:
    @staticmethod
    def _listener_alive(listener):
        try:
            return bool(listener and listener.is_alive())
        except Exception:
            return False

    @staticmethod
    def _kanata_runtime_healthy(engine):
        try:
            thread = getattr(engine, "command_thread", None)
            stop_event = getattr(engine, "command_stop", None)
            return bool(
                engine.is_running()
                and thread
                and thread.is_alive()
                and not (stop_event and stop_event.is_set())
            )
        except Exception:
            return False

    def _set_listener_degraded(self, reason=""):
        reason = str(reason or "")
        previous = str(
            getattr(self, "input_listener_degraded_reason", "") or ""
        )
        if previous == reason:
            return
        self.input_listener_degraded_reason = reason
        hint = getattr(self, "engine_hint", None)
        if reason and hint is not None:
            hint.setStyleSheet("color: #fbbf24;")
            hint.setText(reason)
        elif previous and hint is not None:
            # Only replace the warning if no newer runtime/macro message has
            # already taken its place. This prevents a recovered listener from
            # erasing a more important error while also avoiding a stale yellow
            # warning after automatic recovery.
            try:
                current_text = str(hint.text())
            except (AttributeError, TypeError):
                current_text = str(getattr(hint, "text", ""))
            if current_text == previous:
                hint.setStyleSheet("")
                hint.setText(
                    "输入引擎运行中，当前没有正在执行的动作"
                    if getattr(self, "running", False)
                    else "全局输入监听已恢复"
                )
        if hasattr(self, "refresh_status_ui"):
            self.refresh_status_ui()

    def _handle_runtime_backend_failure(self, reason):
        """Close the runtime gate and retain only resources safe to retry."""
        if getattr(self, "_backend_failure_handling", False):
            return
        self._backend_failure_handling = True
        try:
            self.write_diagnostic(
                "runtime_backend_health_failed", force=True, reason=str(reason)
            )
            self.profile_trigger_allowed = False
            self.output_shutdown_in_progress = True
            remaining = []
            cleanup_failures = []
            try:
                remaining = self.stop_all_macros(keep_output_gate=True)
                cleanup_failures.extend(list(getattr(
                    self, "last_macro_release_failures", []
                )))
            except Exception as error:
                cleanup_failures.append(f"停止宏任务：{error}")
            try:
                if not self._release_all_sync_mappings():
                    cleanup_failures.append("同步映射输出")
            except Exception as error:
                cleanup_failures.append(f"同步映射输出：{error}")
            try:
                if not self._release_runtime_virtual_keys(
                    include_history=True, timeout=0.8
                ):
                    cleanup_failures.append("Kanata 虚拟键")
            except Exception as error:
                cleanup_failures.append(f"Kanata 虚拟键：{error}")
            try:
                if not self._release_interception_output():
                    cleanup_failures.append("Interception 输出")
            except Exception as error:
                cleanup_failures.append(f"Interception 输出：{error}")
            try:
                if not self._failsafe_release_runtime_targets(force_all=True):
                    cleanup_failures.append("系统级兜底释放")
            except Exception as error:
                cleanup_failures.append(f"系统级兜底释放：{error}")

            game_mode = self._runtime_is_game_mode()
            # A still-live worker may still be unwinding a backend call. Keep
            # its output object alive, but no new Press/Tap can pass the gate.
            if not remaining:
                if self.interception_output is not None:
                    output = self.interception_output
                    try:
                        if output.stop() and self.interception_output is output:
                            self.interception_output = None
                        else:
                            cleanup_failures.append("Interception 输出上下文停止")
                    except Exception as error:
                        cleanup_failures.append(
                            f"Interception 输出上下文停止：{error}"
                        )
                for label, engine in (
                    ("主 Kanata", self.engine),
                    ("辅助 Kanata", self.keyboard_engine),
                ):
                    if not self._kanata_engine_has_runtime(engine):
                        continue
                    try:
                        if not engine.stop(timeout=2.0):
                            cleanup_failures.append(f"{label}停止超时")
                    except Exception as error:
                        cleanup_failures.append(f"{label}停止：{error}")

            backend_alive = bool(
                self.interception_output is not None
                or self._kanata_engine_has_runtime(self.engine)
                or self._kanata_engine_has_runtime(self.keyboard_engine)
            )
            self.running = backend_alive
            self.direct_interception_active = False
            self.mappings_enabled = False
            self.engine_state = EngineState.FAILED
            cleanup_failures = list(dict.fromkeys(cleanup_failures))
            cleanup_incomplete = bool(
                remaining or cleanup_failures or backend_alive
            )
            self.last_macro_release_failures = list(cleanup_failures)
            if hasattr(self, "toggle_button"):
                if backend_alive:
                    self.toggle_button.setText("重试停止输入引擎")
                    self.toggle_button.setObjectName("stop")
                elif cleanup_incomplete:
                    self.toggle_button.setText("强制释放后再启动")
                    self.toggle_button.setObjectName("primary")
                else:
                    self.toggle_button.setText("启动输入引擎")
                    self.toggle_button.setObjectName("primary")

            listener_restored = False
            if game_mode and self.runtime_global_toggle_enabled:
                old_hook = self.interception_input_hook
                if old_hook is not None and not self._listener_alive(old_hook):
                    try:
                        old_hook.stop(timeout=0.5)
                    except Exception:
                        pass
                    if self.interception_input_hook is old_hook:
                        self.interception_input_hook = None
                listener_restored = bool(
                    self.start_interception_input_hook(control_only=True)
                )
            elif not game_mode:
                listener_restored = bool(self.restart_global_hook())

            if listener_restored:
                self._reseed_physical_input_state(
                    seed_control=bool(game_mode)
                )

            detail = str(reason)
            if remaining:
                detail += f"；仍有 {len(remaining)} 个宏线程正在退出"
            if cleanup_failures:
                detail += "；" + "、".join(cleanup_failures)
            if not listener_restored:
                detail += "；全局控制监听恢复失败"
            self._set_listener_degraded(detail)
            # Do not reopen Press/Tap while any output channel or backend has
            # not confirmed cleanup. A later explicit start first retries stale
            # backend shutdown before it can create a replacement backend.
            self.output_shutdown_in_progress = cleanup_incomplete
            if hasattr(self, "refresh_macro_controls"):
                self.refresh_macro_controls()
        finally:
            self._backend_failure_handling = False

    def check_input_backend_health(self):
        """Detect dead listener/process state that no longer matches the UI."""
        if os.name != "nt":
            return
        if (
            getattr(self, "initializing", False)
            or getattr(self, "_shutdown_in_progress", False)
            or getattr(self, "_shutdown_started", False)
            or getattr(self, "_backend_failure_handling", False)
            or getattr(self, "_config_apply_transaction_active", False)
            or bool(getattr(self, "loading_task_stack", []))
        ):
            return

        game_mode = self._runtime_is_game_mode()
        if self.running:
            if game_mode:
                hook_ok = self._listener_alive(self.interception_input_hook)
                output = self.interception_output
                output_ok = bool(
                    output
                    and getattr(output, "context", None)
                    and getattr(output, "keyboard_device", 0)
                    and getattr(output, "mouse_device", 0)
                )
                if not hook_ok or not output_ok:
                    missing = []
                    if not hook_ok:
                        missing.append("Interception 输入监听已退出")
                    if not output_ok:
                        missing.append("Interception 输出上下文不可用")
                    self._handle_runtime_backend_failure("；".join(missing))
                    return
            elif not self._kanata_runtime_healthy(self.engine):
                self._handle_runtime_backend_failure(
                    "Kanata 进程或命令线程已意外退出"
                )
                return

        if game_mode:
            listener_expected = bool(
                self.running or self.runtime_global_toggle_enabled
            )
            listener_ok = self._listener_alive(self.interception_input_hook)
            if listener_expected and not listener_ok:
                if self.running:
                    self._handle_runtime_backend_failure(
                        "Interception 输入监听已意外退出"
                    )
                    return
                old_hook = self.interception_input_hook
                if old_hook is not None:
                    try:
                        old_hook.stop(timeout=0.5)
                    except Exception:
                        pass
                    if self.interception_input_hook is old_hook:
                        self.interception_input_hook = None
                if self.start_interception_input_hook(control_only=True):
                    self._reseed_physical_input_state(seed_control=True)
                    self._set_listener_degraded("")
                else:
                    self._set_listener_degraded(
                        "全局开关监听不可用；请使用界面按钮启动输入引擎"
                    )
            elif listener_ok:
                self._set_listener_degraded("")
            return

        if not self._listener_alive(self.global_hook):
            if self.restart_global_hook():
                self._reseed_physical_input_state(seed_control=False)
                self._set_listener_degraded("")
            else:
                self._set_listener_degraded(
                    "Windows 全局输入监听不可用；录制与备用急停快捷键失效"
                )
        else:
            self._set_listener_degraded("")

    def start_global_hook(self):
        if self.global_hook and self.global_hook.is_alive():
            return True
        if self.global_hook:
            old_hook = self.global_hook
            try:
                stopped = bool(old_hook.stop(timeout=0.5))
            except Exception as error:
                stopped = False
                if hasattr(self, "write_diagnostic"):
                    self.write_diagnostic(
                        "global_hook_stop_error", force=True, error=str(error)
                    )
            if not stopped:
                if hasattr(self, "write_diagnostic"):
                    self.write_diagnostic(
                        "global_hook_start_blocked",
                        force=True,
                        warning=getattr(old_hook, "last_stop_warning", ""),
                    )
                if hasattr(self, "engine_hint"):
                    self.engine_hint.setText(
                        "旧 Windows 输入监听仍在退出，暂不能启动新的监听"
                    )
                    self.engine_hint.setStyleSheet("color: #ff8496;")
                return False
            if self.global_hook is old_hook:
                self.global_hook = None
        self._clear_physical_input_state()
        hook = WinInput(
            self._global_hook_callback,
            event_callback=self._raw_recording_event,
            source_callback=lambda name, down, source_id: self._global_hook_callback(
                name, down, False, source_id
            ),
        )
        self.global_hook = hook
        if not hook.start():
            if self.global_hook is hook:
                self.global_hook = None
            self.engine_hint.setText("全局快捷键监听启动失败；录制与急停不可用")
            self.engine_hint.setStyleSheet("color: #ff8496;")
            return False
        return True

    def _stop_interception_input_hook(self, timeout=1.0):
        hook = self.interception_input_hook
        if hook is None:
            self.interception_input_control_only = False
            self.interception_control_modifiers.clear()
            getattr(self, "interception_control_sources", {}).clear()
            return True
        try:
            stopped = bool(hook.stop(timeout=timeout))
        except Exception as error:
            self.write_diagnostic(
                "interception_input_stop_error", force=True,
                error=str(error),
            )
            return False
        if stopped:
            if self.interception_input_hook is hook:
                self.interception_input_hook = None
            self.interception_input_control_only = False
            self.interception_control_modifiers.clear()
            getattr(self, "interception_control_sources", {}).clear()
            return True
        self.write_diagnostic(
            "interception_input_stop_pending", force=True,
            warning=getattr(hook, "last_stop_warning", ""),
        )
        return False

    def start_interception_input_hook(self, control_only=False):
        """Ensure the persistent Interception source listener is available."""
        game_mode = self._runtime_is_game_mode()
        if not game_mode and not control_only:
            return self._stop_interception_input_hook()
        if os.name != "nt":
            self.engine_hint.setText("Interception 游戏模式仅支持 Windows")
            self.engine_hint.setStyleSheet("color: #ff8496;")
            return False

        requested_control_only = bool(control_only)
        requested_capture_mouse_move = bool(
            self.interception_record_mouse_move and not requested_control_only
        )
        existing_hook = self.interception_input_hook
        existing_alive = bool(
            existing_hook
            and existing_hook.context
            and existing_hook.thread
            and existing_hook.thread.is_alive()
            and not existing_hook.stop_event.is_set()
        )

        def ensure_interception_output():
            if self.interception_output is None:
                self.interception_output = InterceptionOutput()
            if not self.interception_output.start():
                raise OSError("Interception 输出上下文创建失败")
            if not self.interception_output.keyboard_device:
                raise OSError("未找到可用的物理键盘设备")
            if not self.interception_output.mouse_device:
                raise OSError("未找到可用的物理鼠标设备")

        try:
            if (
                existing_alive
            ):
                capture_ready = (
                    existing_hook.capture_mouse_move
                    == requested_capture_mouse_move
                )
                if not capture_ready:
                    update_filter = getattr(
                        existing_hook, "update_capture_mouse_move", None
                    )
                    capture_ready = bool(
                        update_filter
                        and update_filter(requested_capture_mouse_move)
                )
                previous_mode = self.interception_input_control_only
                if capture_ready:
                    if not requested_control_only:
                        ensure_interception_output()
                    self.interception_input_control_only = requested_control_only
                    if previous_mode != requested_control_only:
                        self.interception_control_modifiers.clear()
                        getattr(self, "interception_control_sources", {}).clear()
                    self.write_diagnostic(
                        "interception_input_listener_mode_changed",
                        control_only=requested_control_only,
                        reused_context=True,
                        capture_mouse_move=requested_capture_mouse_move,
                    )
                    return True
                self.write_diagnostic(
                    "interception_input_filter_reconfigure_failed",
                    force=True,
                    requested_capture_mouse_move=requested_capture_mouse_move,
                    warning=getattr(existing_hook, "last_stop_warning", ""),
                )
                if previous_mode != requested_control_only:
                    self.write_diagnostic(
                        "interception_input_listener_mode_change_deferred",
                        control_only=requested_control_only,
                        capture_mouse_move=requested_capture_mouse_move,
                    )

            if existing_hook:
                stop_deadline = time.monotonic() + 3.0
                stopped = False
                while time.monotonic() < stop_deadline:
                    stopped = self._stop_interception_input_hook(timeout=0.35)
                    if stopped:
                        break
                    time.sleep(0.05)
                if not stopped:
                    raise OSError(
                        "旧的 Interception 输入线程仍在结束，暂不能重建监听上下文"
                    )

            if not requested_control_only and self.interception_output is not None:
                output = self.interception_output
                try:
                    output_stopped = bool(output.stop())
                except Exception as stop_error:
                    output_stopped = False
                    self.write_diagnostic(
                        "interception_stale_output_stop_error",
                        force=True,
                        error=str(stop_error),
                    )
                if output_stopped and self.interception_output is output:
                    self.interception_output = None
                    self._interception_output_stop_failed = False
                elif not output_stopped:
                    self.write_diagnostic(
                        "interception_stale_output_stop_pending",
                        force=True,
                        warning=getattr(output, "last_start_warning", ""),
                    )

            hook = InterceptionInputHook(
                self._interception_source_callback,
                raw_event_callback=self._handle_interception_raw_input,
                capture_mouse=True,
                map_keyboard_side_buttons=True,
                capture_mouse_move=requested_capture_mouse_move,
                source_callback=self._interception_source_callback,
            )
            previous_mode = self.interception_input_control_only
            if not requested_control_only:
                self.interception_input_control_only = True
            if not hook.start():
                warning = getattr(hook, "last_stop_warning", "")
                detail = "Interception 键盘/鼠标来源监听上下文创建失败"
                if warning:
                    detail = f"{detail}：{warning}"
                self.interception_input_control_only = previous_mode
                raise OSError(detail)
            self.interception_input_hook = hook
            if not requested_control_only:
                ensure_interception_output()
            self.interception_input_control_only = requested_control_only
            self.interception_control_modifiers.clear()
            getattr(self, "interception_control_sources", {}).clear()
            self.write_diagnostic(
                "interception_input_listener_started",
                control_only=requested_control_only,
                capture_mouse_move=hook.capture_mouse_move,
                reused_context=False,
            )
            return True
        except OSError as error:
            output_stop_error = ""
            if self.interception_output and not requested_control_only:
                output = self.interception_output
                try:
                    output_stopped = bool(output.stop())
                except Exception as stop_error:
                    output_stopped = False
                    output_stop_error = str(stop_error)
                if output_stopped and self.interception_output is output:
                    self.interception_output = None
                    self._interception_output_stop_failed = False
                elif not output_stopped:
                    self._interception_output_stop_failed = True
                    self.write_diagnostic(
                        "interception_output_stop_error",
                        force=True,
                        error=(
                            output_stop_error
                            or "Interception 输出上下文停止超时"
                        ),
                    )
            live_hook = self.interception_input_hook
            live_hook_ready = bool(
                live_hook
                and live_hook.context
                and live_hook.thread
                and live_hook.thread.is_alive()
                and not live_hook.stop_event.is_set()
            )
            if live_hook_ready:
                self.interception_input_control_only = True
            self.engine_hint.setText(
                f"Interception 控制监听或输入/输出通道失败：{error}"
            )
            self.engine_hint.setStyleSheet("color: #ff8496;")
            self.write_diagnostic(
                "interception_input_listener_failed",
                force=True,
                requested_control_only=requested_control_only,
                control_listener_preserved=live_hook_ready,
                error=str(error),
                output_stop_error=output_stop_error,
            )
            return False

    def restart_global_hook(self):
        if self.global_hook:
            stopped = self.global_hook.stop(timeout=1.5)
            if not stopped:
                self.write_diagnostic(
                    "global_hook_restart_blocked",
                    force=True,
                    warning=getattr(self.global_hook, "last_stop_warning", ""),
                )
                return False
            self.global_hook = None
        if not self.start_global_hook():
            return False
        return bool(self.global_hook and self.global_hook.is_alive())

    def update_global_hook_for_backend(self):
        """Select one physical-source owner without cycling game input context."""
        game_mode = self._runtime_is_game_mode()
        if game_mode:
            if self.global_hook:
                if not self.global_hook.stop(timeout=1.5):
                    self.engine_hint.setText(
                        "Windows 输入监听仍在退出，暂不能切换到游戏模式"
                    )
                    self.engine_hint.setStyleSheet("color: #ff8496;")
                    return False
                self.global_hook = None
            if self.running:
                return self.start_interception_input_hook(control_only=False)
            if self.runtime_global_toggle_enabled:
                return self.start_interception_input_hook(control_only=True)
            if self.interception_input_hook:
                return self._stop_interception_input_hook()
            return True

        if self.interception_input_hook:
            if not self._stop_interception_input_hook():
                self.engine_hint.setText(
                    "Interception 输入线程仍在退出，未切换到普通输入监听"
                )
                self.engine_hint.setStyleSheet("color: #ff8496;")
                return False
        if self.interception_output:
            try:
                stopped = self.interception_output.stop()
            except Exception as error:
                stopped = False
                self.write_diagnostic(
                    "interception_output_stop_error",
                    force=True,
                    error=str(error),
                )
            if not stopped:
                self.engine_hint.setText(
                    "Interception 输出上下文未能安全停止"
                )
                self.engine_hint.setStyleSheet("color: #ff8496;")
                return False
            self.interception_output = None
        return self.restart_global_hook()
