"""输入监听、触发分发、输出后端与运行时输入状态处理。"""

from __future__ import annotations

from contextlib import nullcontext
import copy
import ctypes
import io
import json
import math
import os
import struct
import threading
import time
import wave
from ctypes import wintypes

from PySide6.QtCore import QEvent, QTimer, Qt, QUrl, Slot
from PySide6.QtMultimedia import QMediaDevices, QSoundEffect
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QLabel, QMessageBox, QSpinBox, QWidget,
)

from config.schema import validate_config_payload
from config.storage import write_deduplicated_snapshot
from config.profiles import (
    BASE_LAYER_NAME, DISABLED_LAYER_NAME, normalize_profile,
    profile_payload, select_profile,
)
from core.constants import *
from engine.input_backend import POINT, InterceptionInputHook, InterceptionOutput, WinInput
from engine.kanata import KANATA_KEYS
from engine.window_context import (
    foreground_window_belongs_to_current_process,
    foreground_window_context,
    foreground_window_identity,
    foreground_window_identity_matches,
)
from engine.trigger_resolver import (
    MODIFIER_ORDER, combo_text, mapping_condition_satisfied, modifier_names,
    source_modifier_specificity, source_modifiers_match,
)
from macro.actions import clone_action_tree, iter_action_tree
from macro.recording import simplify_recorded_actions
from ui.profile_manager import ProfileManagerDialog
from ui.runtime_guards import (
    explain_runtime_cleanup_block, runtime_cleanup_blocks_new_output,
)


class InputRuntimeMixin:

    def _macro_action_condition_satisfied(self, input_name, state="按住时"):
        """Read only physical input state for worker-thread control actions."""
        with getattr(self, "input_state_lock", nullcontext()):
            held = str(input_name or "") in set(
                getattr(self, "physical_down", set())
            )
        return held if state == "按住时" else not held

    def _runtime_cleanup_blocks_new_output(self):
        return runtime_cleanup_blocks_new_output(self)

    def _explain_runtime_cleanup_block(self, context="runtime_trigger"):
        return explain_runtime_cleanup_block(self, context)

    @staticmethod
    def _input_source_token(name, source_id=None):
        if source_id is not None:
            return str(source_id)
        return str(name or "")

    def _ensure_system_hotkey_latch_sources(self):
        if not hasattr(self, "system_hotkey_latched_sources"):
            self.system_hotkey_latched_sources = {}

    def _latch_system_hotkey(self, control_id, source_id):
        self._ensure_system_hotkey_latch_sources()
        self.system_hotkey_latched.add(control_id)
        self.system_hotkey_latched_sources[control_id] = str(
            source_id if source_id is not None else ""
        )

    def _system_hotkey_latched_by(self, control_id, source_id):
        if control_id not in self.system_hotkey_latched:
            return False
        self._ensure_system_hotkey_latch_sources()
        owner = self.system_hotkey_latched_sources.get(control_id)
        # Existing in-memory state created before this field was introduced has
        # no owner. Treat it as releasable once, preserving the old behavior.
        source = str(source_id if source_id is not None else "")
        return owner in (None, source)

    def _unlatch_system_hotkey(self, control_id, source_id=None, *, force=False):
        if not force and not self._system_hotkey_latched_by(control_id, source_id):
            return False
        self.system_hotkey_latched.discard(control_id)
        self._ensure_system_hotkey_latch_sources()
        self.system_hotkey_latched_sources.pop(control_id, None)
        return True

    def _clear_system_hotkey_latches(self):
        self.system_hotkey_latched.clear()
        self._ensure_system_hotkey_latch_sources()
        self.system_hotkey_latched_sources.clear()

    def _ensure_physical_source_tables_locked(self):
        if not hasattr(self, "physical_input_sources"):
            self.physical_input_sources = {}
        if not hasattr(self, "interception_control_sources"):
            self.interception_control_sources = {}

    def _refresh_logical_physical_sets_locked(self):
        self._ensure_physical_source_tables_locked()
        if not hasattr(self, "physical_down"):
            self.physical_down = set()
        if not hasattr(self, "physical_modifiers"):
            self.physical_modifiers = set()
        names = set(self.physical_input_sources.values())
        self.physical_down.clear()
        self.physical_down.update(names)
        self.physical_modifiers.clear()
        self.physical_modifiers.update(
            name for name in names if name in MODIFIER_ORDER
        )

    def _update_physical_input_state_locked(
        self, name, down, source_id=None,
    ):
        """Update logical state without collapsing independent physical sources."""
        self._ensure_physical_source_tables_locked()
        name = str(name or "")
        token = self._input_source_token(name, source_id)
        repeated = bool(down and token in self.physical_input_sources)
        if down:
            self.physical_input_sources[token] = name
        else:
            self.physical_input_sources.pop(token, None)
        self._refresh_logical_physical_sets_locked()
        return repeated, token

    def _clear_physical_input_state(self):
        with getattr(self, "input_state_lock", nullcontext()):
            self._ensure_physical_source_tables_locked()
            self.physical_input_sources.clear()
            self.physical_down.clear()
            self.physical_modifiers.clear()

    def _reseed_physical_input_state(self, seed_control=False):
        """Rebuild held-input state after listener/backend mode transitions."""
        snapshot = []
        hook = getattr(self, "interception_input_hook", None)
        global_hook = getattr(self, "global_hook", None)
        try:
            if hook is not None and hook.is_alive():
                snapshot = hook.pressed_input_snapshot()
            elif global_hook is not None and global_hook.is_alive():
                snapshot = global_hook.pressed_input_snapshot()
        except Exception as error:
            self.write_diagnostic(
                "physical_input_reseed_failed", force=True, error=str(error)
            )
            snapshot = []

        with getattr(self, "input_state_lock", nullcontext()):
            self._ensure_physical_source_tables_locked()
            self.physical_input_sources.clear()
            for name, source_id in snapshot:
                if name:
                    self.physical_input_sources[str(source_id)] = str(name)
            self._refresh_logical_physical_sets_locked()
            if seed_control:
                self.interception_control_sources = {
                    source_id: name
                    for source_id, name in self.physical_input_sources.items()
                    if name in MODIFIER_ORDER
                }
                self.interception_control_modifiers.clear()
                self.interception_control_modifiers.update(
                    self.interception_control_sources.values()
                )
        self.write_diagnostic(
            "physical_input_reseeded",
            sources=len(snapshot),
            logical_inputs=sorted(self.physical_down),
            control_seeded=bool(seed_control),
        )
        return bool(snapshot)

    def _quarantine_mouse_release(self, action, press_window):
        """Retain an unsafe MouseUp until its original foreground returns."""
        if action.get("type") != "鼠标点击" or action.get("target") not in MOUSE_NAMES:
            return False
        entry = {
            "action": dict(action),
            "press_window": dict(press_window or {}),
        }
        signature = (
            str(action.get("_vkey") or ""),
            str(action.get("target") or ""),
            str(action.get("modifiers") or "无"),
        )
        entry["signature"] = signature
        with self.quarantined_mouse_release_lock:
            if not any(
                item.get("signature") == signature
                for item in self.quarantined_mouse_releases
            ):
                self.quarantined_mouse_releases.append(entry)
        virtual_key = str(action.get("_vkey") or "")
        if virtual_key:
            with self.engine.active_virtual_keys_lock:
                self.engine.quarantined_virtual_keys.add(virtual_key)
        self.write_diagnostic(
            "mouse_release_quarantined",
            target=action.get("target"),
            virtual_key=virtual_key,
            press_window=press_window,
        )
        return True

    def _retry_quarantined_mouse_releases(self, force=False):
        with self.quarantined_mouse_release_lock:
            pending = list(self.quarantined_mouse_releases)
        retained = []
        for entry in pending:
            if not force and not foreground_window_identity_matches(
                entry.get("press_window")
            ):
                retained.append(entry)
                continue
            action = dict(entry.get("action") or {})
            released = bool(
                self._send_output_action(
                    action, "Release", wait=True, timeout=0.8
                )
            )
            if released and not self._runtime_is_game_mode():
                # A completed queue item only proves that Python wrote the
                # command. Keep the quarantine record until Kanata replies to a
                # subsequent protocol probe, which proves the ordered Release
                # batch was actually consumed.
                released = bool(self.engine.flush_commands(timeout=0.8))
            if not released:
                retained.append(entry)
        with self.quarantined_mouse_release_lock:
            current_signatures = {
                item.get("signature") for item in pending
            }
            newer = [
                item for item in self.quarantined_mouse_releases
                if item.get("signature") not in current_signatures
            ]
            self.quarantined_mouse_releases = retained + newer
        macro_released = not retained
        output_released = True
        output = self.interception_output
        if output is not None:
            output_released = bool(
                output.retry_quarantined_mouse_releases(force=force)
            )
        return bool(macro_released and output_released)

    def _receive_kanata_message(self, payload):
        """Forward Kanata push-msg notifications onto Qt's UI thread."""
        self.write_diagnostic("kanata_message_raw", payload=payload)
        if not isinstance(payload, list) or not payload:
            return
        marker = str(payload[0])
        if marker == "mc-state":
            if len(payload) < 3:
                return
            token = str(payload[1])
            phase = str(payload[2])
            source = next(
                (name for name, key in KANATA_KEYS.items() if key == token),
                "",
            )
            if source and phase in ("down", "up"):
                self.kanata_state_signal.emit(source, phase == "down")
            return

        if marker == "mc-trigger":
            if len(payload) >= 5:
                scope, kind, item_id, phase = (
                    str(value) for value in payload[1:5]
                )
            elif len(payload) >= 4:
                # Compatibility with single-layer configurations generated by
                # previous releases.
                scope = BASE_LAYER_NAME
                kind, item_id, phase = (
                    str(value) for value in payload[1:4]
                )
            else:
                return
            self.write_diagnostic(
                "kanata_message",
                marker=marker,
                scope=scope,
                kind=kind,
                item_id=item_id,
                phase=phase,
                active_layer=self.active_profile_layer,
                running=self.running,
            )
            if kind in ("mapping", "preset") and phase in ("down", "up"):
                self.kanata_trigger_signal.emit(scope, kind, item_id, phase)
            return

        if len(payload) < 4:
            return
        _marker, kind, item_id, phase = (
            str(value) for value in payload[:4]
        )
        self.write_diagnostic(
            "kanata_message", marker=marker, kind=kind,
            item_id=item_id, phase=phase, running=self.running,
        )
        if marker == "mc-control" and kind in ("emergency", "toggle", "pause"):
            self.kanata_control_signal.emit(kind)
        elif marker == "mc-diagnostic":
            self.write_diagnostic(
                "kanata_source_seen", kind=kind, item_id=item_id,
                phase=phase, running=self.running,
                mappings_enabled=self.mappings_enabled,
            )

    @staticmethod
    def _feedback_tones():
        return {
            "enabled": [(1047, 90), (1319, 110)],
            "disabled": [(659, 90), (440, 130)],
            "paused": [(784, 95), (622, 125)],
            "resumed": [(622, 95), (784, 95), (988, 115)],
            "emergency": [(330, 110), (247, 160)],
            "error": [(220, 90), (220, 90), (196, 140)],
        }

    @classmethod
    def _feedback_wave_bytes(cls, kind):
        """Build one complete PCM cue for Qt's normal audio output device."""
        sample_rate = 44100
        frames = bytearray()
        tones = cls._feedback_tones().get(kind, [(700, 100)])
        for tone_index, (frequency, duration_ms) in enumerate(tones):
            sample_count = max(1, int(sample_rate * duration_ms / 1000))
            fade_count = min(sample_count // 2, int(sample_rate * 0.006))
            for index in range(sample_count):
                envelope = 1.0
                if fade_count:
                    envelope = min(
                        1.0,
                        index / fade_count,
                        (sample_count - index - 1) / fade_count,
                    )
                value = int(
                    32767 * 0.32 * envelope
                    * math.sin(2.0 * math.pi * frequency * index / sample_rate)
                )
                frames.extend(struct.pack("<h", value))
            if tone_index + 1 < len(tones):
                frames.extend(b"\x00\x00" * int(sample_rate * 0.025))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(frames)
        return buffer.getvalue()

    def _initialize_feedback_audio(self):
        self.feedback_effects = {}
        self.feedback_current_effect = None
        feedback_directory = APP_DIR / "feedback_audio"
        try:
            feedback_directory.mkdir(parents=True, exist_ok=True)
            for kind in self._feedback_tones():
                path = feedback_directory / f"{kind}.wav"
                payload = self._feedback_wave_bytes(kind)
                if not path.exists() or path.read_bytes() != payload:
                    path.write_bytes(payload)
                effect = QSoundEffect(self)
                effect.setLoopCount(1)
                effect.setVolume(0.9)
                effect.setSource(QUrl.fromLocalFile(str(path)))
                self.feedback_effects[kind] = effect
        except (OSError, RuntimeError) as error:
            self.feedback_effects = {}
            self.write_diagnostic(
                "feedback_audio_initialize_failed", force=True, error=str(error)
            )
        self.feedback_signal.connect(self._play_feedback_qt)

    def _play_feedback(self, kind):
        # Emitting the Qt signal is thread-safe and guarantees that QSoundEffect
        # is controlled on the GUI thread even when a hotkey originated in an
        # Interception callback.
        self.feedback_signal.emit(str(kind or ""))

    @Slot(str)
    def _play_feedback_qt(self, kind):
        effect = self.feedback_effects.get(kind)
        device = QMediaDevices.defaultAudioOutput().description()
        if effect is None or effect.status() == QSoundEffect.Status.Error:
            QApplication.beep()
            self.write_diagnostic(
                "feedback_audio_failed", force=True, kind=kind,
                device=device, status=(str(effect.status()) if effect else "missing"),
            )
            return
        current = self.feedback_current_effect
        if current is not None and current.isPlaying():
            current.stop()
        self.feedback_current_effect = effect
        effect.stop()
        effect.play()
        self.write_diagnostic(
            "feedback_audio_play", kind=kind, device=device,
            status=str(effect.status()),
        )


    def _handle_kanata_owned_control_input(
        self, name, down, route_token, repeated=False
    ):
        """Handle runtime control shortcuts before passing input to Kanata.

        Normal desktop mappings still stay in Kanata for low latency, but
        emergency stop / engine switch / macro pause must not depend on the
        Kanata push-message path being healthy after the process has started.
        Only the configured control shortcut edge is consumed here; all other
        physical input continues to Kanata unchanged.
        """
        controls = [
            (
                "emergency", True,
                self.runtime_emergency_modifiers, self.runtime_emergency_key,
                self.emergency_signal,
            ),
            (
                "global_toggle", self.runtime_global_toggle_enabled,
                self.runtime_global_toggle_modifiers, self.runtime_global_toggle_key,
                self.global_toggle_signal,
            ),
            (
                "macro_pause", self.runtime_macro_pause_enabled,
                self.runtime_macro_pause_modifiers, self.runtime_macro_pause_key,
                self.macro_pause_signal,
            ),
        ]
        with getattr(self, "input_state_lock", nullcontext()):
            effective_modifiers = set(getattr(self, "physical_modifiers", set()))
        if name in MODIFIER_ORDER:
            effective_modifiers.discard(name)
        modifiers = self._modifier_text(effective_modifiers)

        for control_id, enabled, expected_modifiers, expected_key, signal in controls:
            if not enabled or name != expected_key:
                continue
            if not down and self._unlatch_system_hotkey(control_id, route_token):
                self.write_diagnostic(
                    "kanata_owned_control_release",
                    control=control_id, key=name, source_id=route_token,
                )
                return True
            if down and control_id in self.system_hotkey_latched:
                return self._system_hotkey_latched_by(control_id, route_token)
            if not down or repeated:
                continue
            if not self._shortcut_matches(
                name, modifiers, expected_modifiers, expected_key
            ):
                continue
            if control_id == "emergency" and not (
                self.running or self.macro_controller.tasks
            ):
                return False

            self._latch_system_hotkey(control_id, route_token)
            self.write_diagnostic(
                "kanata_owned_control_input",
                control=control_id, key=name, modifiers=modifiers,
                source_id=route_token, running=self.running,
            )
            if control_id == "global_toggle":
                signal.emit(not self.running)
            else:
                signal.emit()
            return True
        return False

    @Slot(str)
    def handle_kanata_control(self, command):
        if command in {"toggle", "pause"} and self._runtime_control_transaction_busy():
            self.write_diagnostic(
                "kanata_control_ignored",
                command=command,
                reason="transaction_busy",
            )
            return
        if self._runtime_is_game_mode():
            self.write_diagnostic(
                "kanata_control_ignored",
                command=command,
                reason="game_mode_uses_interception_only",
            )
            return
        control_id = {
            "emergency": "emergency",
            "toggle": "global_toggle",
            "pause": "macro_pause",
        }.get(str(command or ""))
        if control_id in getattr(self, "system_hotkey_latched", set()):
            self.write_diagnostic(
                "kanata_control_ignored",
                command=command,
                reason="python_control_already_consumed",
            )
            return
        self.write_diagnostic(
            "kanata_control",
            command=command,
            running=self.running,
            mappings_enabled=self.mappings_enabled,
        )
        if command == "emergency":
            if getattr(self, "recording_session_active", False):
                emergency_text = combo_text(
                    self.runtime_emergency_modifiers, self.runtime_emergency_key
                )
                finish_text = combo_text(
                    self.runtime_recording_finish_modifiers,
                    self.runtime_recording_finish_key,
                )
                if emergency_text == finish_text:
                    self.recording_stop_signal.emit()
                else:
                    self.recording_cancel_signal.emit()
            else:
                # Configurable one-shot emergency stop: release all actions while
                # preserving the mapping enabled/paused layer.
                self.emergency_stop(disable_mappings=False, sound=True)
        elif command == "toggle":
            if getattr(self, "recording_session_active", False):
                return
            # The global shortcut is a real engine power switch.  Stopping the
            # business engine also stops macros and releases all held outputs.
            # A control-only Interception listener remains active afterwards so
            # the same shortcut can start the engine again inside games.
            target_enabled = not self.running
            result = self.set_running(
                target_enabled,
                allow_owned_mouse_force_release=not target_enabled,
            )
            if result is not False and self.running == target_enabled:
                self._play_feedback("enabled" if target_enabled else "disabled")
            else:
                self._play_feedback("error")
        elif command == "pause":
            if not getattr(self, "recording_session_active", False):
                self.toggle_all_macro_pause()

    def set_mappings_enabled(self, enabled, sound=False):
        enabled = bool(enabled)
        if not self.running:
            return False

        game_mode = self._runtime_is_game_mode()
        if not enabled:
            # Close the trigger gate before cleanup.  A source key may still be
            # physically held, so releasing outputs first leaves a window in
            # which the same source can press the target again.
            if game_mode:
                if not self._interception_source_ready():
                    return False
                self.mappings_enabled = False
            else:
                if not self.engine.is_running():
                    return False
                if not self.engine.change_layer(
                    DISABLED_LAYER_NAME, wait=True, timeout=1.0
                ):
                    if sound:
                        self._play_feedback("error")
                    self.engine_hint.setText(
                        self.engine.last_command_error or "Kanata 映射层切换失败"
                    )
                    return False
                self.mappings_enabled = False

            remaining = self.stop_all_macros(play_sound=False)
            # Final bounded Up sweep for an output whose direct Kanata state was
            # not represented in Python ownership tables.  only-if-down avoids
            # synthesizing unrelated mouse-button releases.
            failsafe_ok = self._failsafe_release_runtime_targets(
                force_all=False, allow_mouse_targets=False
            )
            cleanup_failures = list(getattr(
                self, "last_macro_release_failures", []
            ))
            if not failsafe_ok:
                cleanup_failures.append("系统兜底释放")

            if cleanup_failures:
                if sound:
                    self._play_feedback("error")
                display_failures = cleanup_failures + (
                    [f"仍有 {len(remaining)} 个宏线程正在退出"]
                    if remaining else []
                )
                self._remember_macro_cleanup_failure(
                    "映射已暂停，但部分输出未能确认释放",
                    cleanup_failures,
                )
                self._show_macro_cleanup_failure(
                    "映射已暂停，但部分输出未能确认释放",
                    display_failures,
                )
                self.refresh_status_ui()
                return False

            if remaining:
                if sound:
                    self._play_feedback("disabled")
                self.macro_state = MacroState.STOPPING
                self.macro_status_detail = (
                    f"映射已暂停，仍有 {len(remaining)} 个宏线程正在退出"
                )
                if hasattr(self, "engine_hint"):
                    self.engine_hint.setStyleSheet("color: #fbbf24;")
                    self.engine_hint.setText(
                        "映射已暂停；正在自动等待宏线程退出并释放按键"
                    )
                if hasattr(self, "execution_info"):
                    self.execution_info.setText(
                        "映射已暂停；宏线程正在退出，完成后会自动恢复下次触发"
                    )
                self.refresh_status_ui()
                return True

            if sound:
                self._play_feedback("disabled")
            self.engine_hint.setText(
                "Interception 映射已暂停"
                if game_mode else "Kanata 映射已暂停"
            )
            self.refresh_status_ui()
            return True

        if self._runtime_cleanup_blocks_new_output():
            failures = self._explain_runtime_cleanup_block("enable_mappings")
            if sound:
                self._play_feedback("error")
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #ff8496;")
                if any("鼠标按键等待安全释放" in str(item) for item in failures):
                    self.engine_hint.setText(
                        "有鼠标按键等待回到原窗口后释放；请回到原窗口或使用“强制释放键鼠”"
                    )
                else:
                    self.engine_hint.setText(
                        "按键释放尚未完成，请先执行“强制释放键鼠”后再恢复映射"
                    )
            self.refresh_status_ui()
            return False

        if game_mode:
            if not self._interception_source_ready():
                return False
            self.mappings_enabled = True
            if sound:
                self._play_feedback("enabled")
            self.engine_hint.setText("Interception 映射已启用")
            self.refresh_status_ui()
            return True

        if not self.engine.is_running():
            return False
        if not self.engine.change_layer(
            self.active_profile_layer, wait=True, timeout=1.0
        ):
            if sound:
                self._play_feedback("error")
            self.engine_hint.setText(
                self.engine.last_command_error or "Kanata 映射层切换失败"
            )
            return False
        self.mappings_enabled = True
        if sound:
            self._play_feedback("enabled")
        self.engine_hint.setText("Kanata 映射已启用")
        self.refresh_status_ui()
        return True

    def emergency_stop(self, disable_mappings=True, sound=True):
        if sound:
            # Acknowledge the emergency command before any bounded backend
            # waits; cleanup can legitimately take several seconds.
            self._play_feedback("emergency")
        previous_gate = bool(self.output_shutdown_in_progress)
        self.output_shutdown_in_progress = True
        remaining = self.stop_all_macros(
            play_sound=False, keep_output_gate=True
        )
        cleanup_failures = list(getattr(
            self, "last_macro_release_failures", []
        ))
        # Keep emergency recovery conservative for mouse buttons: unconditional
        # mouse-button Up can be interpreted as a click by the foreground app.
        # Release program-owned mouse state via backend ownership records, and
        # still do a broad keyboard-only OS recovery for stuck modifiers.
        if not self._release_interception_output():
            cleanup_failures.append("Interception 输出")
        if not self._force_release_system_inputs():
            cleanup_failures.append("系统按键兜底释放")
        if disable_mappings:
            if self._runtime_is_game_mode():
                self.mappings_enabled = False
            elif self.engine.is_running():
                if self.engine.change_layer("disabled", wait=True, timeout=1.0):
                    self.mappings_enabled = False
                else:
                    cleanup_failures.append("Kanata 禁用层切换")
        cleanup_failures = list(dict.fromkeys(cleanup_failures))
        if remaining or cleanup_failures:
            self.last_macro_release_failures = list(cleanup_failures)
            # Timed-out tasks may still finish later. Reopen the previous gate
            # only after the poller confirms every task exited, and only when
            # no Release/backend failure remains.
            if remaining and not cleanup_failures:
                if getattr(self, "_macro_stop_gate_restore", None) is None:
                    self._macro_stop_gate_restore = previous_gate
            else:
                self._macro_stop_gate_restore = None
            self.output_shutdown_in_progress = True
            self._show_macro_cleanup_failure(
                "急停已执行，但部分清理未完成",
                cleanup_failures
                + ([f"仍有 {len(remaining)} 个宏线程"] if remaining else []),
            )
            self.refresh_status_ui()
            return False
        self.output_shutdown_in_progress = previous_gate
        return True

    @Slot(str, bool)
    def handle_kanata_state(self, name, down):
        """Track real condition inputs that Kanata intercepts before WinInput."""
        with getattr(self, "input_state_lock", nullcontext()):
            self._update_physical_input_state_locked(
                name, down, f"kanata:{name}"
            )
        self._release_invalid_conditional_holds()
        self.write_diagnostic(
            "kanata_condition_state", name=name, down=down
        )

    @Slot(str, str, str, str)
    def handle_kanata_trigger(self, scope, kind, item_id, phase):
        """Dispatch only messages from the currently active profile layer."""
        if self._runtime_is_game_mode():
            self.write_diagnostic(
                "kanata_trigger_ignored", scope=scope, kind=kind,
                item_id=item_id, phase=phase,
                reason="game_mode_uses_interception_only",
            )
            return
        if not self.running or not self.engine.is_running():
            return
        if phase == "down" and self._runtime_cleanup_blocks_new_output():
            self._explain_runtime_cleanup_block("kanata_trigger")
            return
        if self.profile_switch_in_progress or not self.profile_trigger_allowed:
            return
        if str(scope) != str(self.active_profile_layer):
            self.write_diagnostic(
                "kanata_trigger_ignored", scope=scope, kind=kind,
                item_id=item_id, phase=phase,
                reason="inactive_profile_layer",
                active_layer=self.active_profile_layer,
            )
            return

        normalized_id = "".join(
            character.lower() for character in str(item_id)
            if character.isalnum()
        )
        rule = None
        with self.data_lock:
            for item in self.runtime_trigger_rules:
                expected_kind = item.get("_runtime_kind", "mapping")
                if expected_kind != kind:
                    continue
                current_id = "".join(
                    character.lower() for character in str(item.get("id", ""))
                    if character.isalnum()
                )
                if current_id == normalized_id:
                    rule = dict(item)
                    if rule.get("_runtime_kind") == "preset":
                        rule["actions"] = clone_action_tree(
                            item.get("actions", [])
                        )
                    break
        if not rule or not rule.get("enabled"):
            return

        token = f"kanata:{scope}:{kind}:{normalized_id}"
        down = phase == "down"
        if down and rule.get("condition_enabled", False):
            # The generated Kanata switch already checked the real input state.
            # Trust that decision here to avoid a race with the Windows hook's
            # physical_down snapshot arriving a few milliseconds later.
            rule["_condition_prevalidated"] = True
        repeated = down and token in self.kanata_trigger_down
        if down:
            self.kanata_trigger_down.add(token)
        else:
            self.kanata_trigger_down.discard(token)
        self._dispatch_runtime_mapping_rule(
            rule, token, down, repeated
        )

    def _event_targets_recording_overlay(self, event):
        """Exclude only actionable overlay input, never pointer movement."""
        if event.get("kind") == "move":
            return False
        overlay = getattr(self, "recording_overlay", None)
        if overlay is None or not overlay.isVisible():
            return False
        try:
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            overlay_root = user32.GetAncestor(
                wintypes.HWND(int(overlay.winId())), 2
            )
            if event.get("kind") in ("button", "wheel"):
                point = POINT(int(event.get("x", 0)), int(event.get("y", 0)))
                user32.WindowFromPoint.argtypes = [POINT]
                user32.WindowFromPoint.restype = wintypes.HWND
                hwnd = user32.WindowFromPoint(point)
            else:
                hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            return user32.GetAncestor(hwnd, 2) == overlay_root
        except Exception:
            return False

    @staticmethod
    def _cursor_position_fields():
        try:
            point = POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return {"x": int(point.x), "y": int(point.y)}
        except Exception:
            pass
        return {}

    @staticmethod
    def _virtual_screen_geometry():
        try:
            user32 = ctypes.windll.user32
            left = int(user32.GetSystemMetrics(76))
            top = int(user32.GetSystemMetrics(77))
            width = int(user32.GetSystemMetrics(78))
            height = int(user32.GetSystemMetrics(79))
            if width > 0 and height > 0:
                return left, top, width, height
        except Exception:
            pass
        screen = QApplication.primaryScreen()
        if screen is not None:
            geometry = screen.virtualGeometry()
            return geometry.left(), geometry.top(), geometry.width(), geometry.height()
        return 0, 0, 1920, 1080

    def _recording_move_context_fields(self):
        mode = self.recording_options.get("move_mode", "屏幕坐标")
        if mode not in ("前台窗口", "前台客户区"):
            return {}
        try:
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowRect.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.RECT)
            ]
            user32.GetWindowRect.restype = wintypes.BOOL
            user32.GetClientRect.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.RECT)
            ]
            user32.GetClientRect.restype = wintypes.BOOL
            user32.ClientToScreen.argtypes = [
                wintypes.HWND, ctypes.POINTER(POINT)
            ]
            user32.ClientToScreen.restype = wintypes.BOOL
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return {}
            process_name, window_title = foreground_window_context()
            fields = {
                "window_handle": int(hwnd),
                "window_process": process_name,
                "window_title": window_title,
            }
            rect = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                fields.update({
                    "window_left": int(rect.left),
                    "window_top": int(rect.top),
                    "window_width": max(1, int(rect.right - rect.left)),
                    "window_height": max(1, int(rect.bottom - rect.top)),
                })
            client_rect = wintypes.RECT()
            origin = POINT(0, 0)
            if (
                user32.GetClientRect(hwnd, ctypes.byref(client_rect))
                and user32.ClientToScreen(hwnd, ctypes.byref(origin))
            ):
                fields.update({
                    "client_left": int(origin.x),
                    "client_top": int(origin.y),
                    "client_width": max(
                        1, int(client_rect.right - client_rect.left)
                    ),
                    "client_height": max(
                        1, int(client_rect.bottom - client_rect.top)
                    ),
                })
            return fields
        except Exception:
            return {}

    @staticmethod
    def _coordinate_number(value, decimals=4):
        number = round(float(value), decimals)
        if abs(number - round(number)) < 10 ** (-decimals):
            return str(int(round(number)))
        return f"{number:.{decimals}f}".rstrip("0").rstrip(".")

    def _recorded_move_target(self, event, previous_position):
        mode = self.recording_options.get("move_mode", "屏幕坐标")
        if mode == "相对移动" and event.get("relative_raw"):
            try:
                dx = int(event.get("dx", 0))
                dy = int(event.get("dy", 0))
            except (TypeError, ValueError, OverflowError):
                return None, previous_position, None
            if dx == 0 and dy == 0:
                return None, previous_position, None
            return f"rel:{dx},{dy}", previous_position, None

        try:
            x = int(event.get("x"))
            y = int(event.get("y"))
        except (TypeError, ValueError, OverflowError):
            return None, previous_position, None
        current = (x, y)
        if mode == "相对移动":
            if previous_position is None:
                return None, current, None
            dx = x - int(previous_position[0])
            dy = y - int(previous_position[1])
            if dx == 0 and dy == 0:
                return None, current, None
            return f"rel:{dx},{dy}", current, None
        if mode == "屏幕比例":
            left, top, width, height = self._virtual_screen_geometry()
            percent_x = max(0.0, min(100.0, (x - left) * 100 / max(1, width - 1)))
            percent_y = max(0.0, min(100.0, (y - top) * 100 / max(1, height - 1)))
            try:
                monitor_count = int(ctypes.windll.user32.GetSystemMetrics(80))
            except Exception:
                monitor_count = 1
            return (
                "pct:"
                f"{self._coordinate_number(percent_x)},"
                f"{self._coordinate_number(percent_y)}"
            ), current, {
                "mode": "pct",
                "monitor_count": max(1, monitor_count),
            }
        if mode == "前台窗口":
            required = (
                "window_left", "window_top", "window_width", "window_height"
            )
            if any(field not in event for field in required):
                return None, current, None
            local_x = x - int(event["window_left"])
            local_y = y - int(event["window_top"])
            width = max(1, int(event["window_width"]))
            height = max(1, int(event["window_height"]))
            if not (0 <= local_x < width and 0 <= local_y < height):
                return None, current, None
            return f"window:{local_x},{local_y}", current, {
                "mode": "window",
                "process": str(event.get("window_process") or ""),
                "title": str(event.get("window_title") or ""),
                "width": width,
                "height": height,
            }
        if mode == "前台客户区":
            required = (
                "client_left", "client_top", "client_width", "client_height"
            )
            if any(field not in event for field in required):
                return None, current, None
            local_x = x - int(event["client_left"])
            local_y = y - int(event["client_top"])
            width = max(1, int(event["client_width"]))
            height = max(1, int(event["client_height"]))
            if not (0 <= local_x < width and 0 <= local_y < height):
                return None, current, None
            return f"client:{local_x},{local_y}", current, {
                "mode": "client",
                "process": str(event.get("window_process") or ""),
                "title": str(event.get("window_title") or ""),
                "width": width,
                "height": height,
            }
        left, top, width, height = self._virtual_screen_geometry()
        return f"{x},{y}", current, {
            "mode": "screen",
            "virtual_screen": [left, top, width, height],
        }

    def _handle_interception_raw_input(self, payload):
        if payload.get("kind") != "mouse_move":
            self.write_diagnostic("interception_raw_input", **payload)
        if not self.recording:
            return
        kind = payload.get("kind")
        timestamp = float(payload.get("time", time.perf_counter()))
        event = None
        if kind == "keyboard":
            event = {
                "kind": "key",
                "name": payload.get("name"),
                "down": bool(payload.get("down")),
                "time": timestamp,
            }
        elif kind == "mouse":
            event = {
                "kind": "button",
                "name": payload.get("name"),
                "down": bool(payload.get("down")),
                "time": timestamp,
                **self._cursor_position_fields(),
            }
        elif kind == "mouse_wheel":
            rolling = int(payload.get("rolling") or 0)
            if rolling:
                event = {
                    "kind": "wheel",
                    "delta": rolling,
                    "time": timestamp,
                    **self._cursor_position_fields(),
                }
        elif kind == "mouse_move" and self.recording_options.get("record_move"):
            flags = int(payload.get("flags") or 0)
            raw_relative = not bool(
                flags & InterceptionInputHook.MOUSE_MOVE_ABSOLUTE
            )
            event = {
                "kind": "move",
                "time": timestamp,
                "source": "interception",
                "move_flags": flags,
            }
            if (
                self.recording_options.get("move_mode") == "相对移动"
                and raw_relative
            ):
                event.update({
                    "relative_raw": True,
                    "dx": int(payload.get("x") or 0),
                    "dy": int(payload.get("y") or 0),
                })
            else:
                try:
                    event.update({
                        "x": int(payload["cursor_x"]),
                        "y": int(payload["cursor_y"]),
                    })
                except (KeyError, TypeError, ValueError, OverflowError):
                    event.update(self._cursor_position_fields())
        if not event:
            return
        if self._event_targets_recording_overlay(event):
            return
        self._store_recorded_event(event)

    def _request_recording_limit_stop_locked(self, reason):
        reason = str(reason or "")
        if reason and not getattr(self, "recording_limit_reason", ""):
            self.recording_limit_reason = reason
        if not reason or getattr(self, "recording_limit_stop_requested", False):
            return False
        self.recording_limit_stop_requested = True
        return True

    def _append_recorded_event_locked(self, event):
        """Append one raw event and request a bounded automatic finish if needed."""
        now = float(event.get("time", time.perf_counter()))
        started_at = float(getattr(self, "recording_started_at", 0.0) or 0.0)
        if (
            started_at > 0
            and (now - started_at) * 1000 >= MAX_RECORDING_DURATION_MS
        ):
            requested = self._request_recording_limit_stop_locked(
                f"录制时长已达到 {MAX_RECORDING_DURATION_MS // 60000} 分钟上限"
            )
            return False, requested
        if len(self.recorded_events) >= MAX_RECORDING_RAW_EVENTS:
            requested = self._request_recording_limit_stop_locked(
                f"原始输入已达到 {MAX_RECORDING_RAW_EVENTS} 条上限"
            )
            return False, requested

        self.recorded_events.append(dict(event))
        requested = False
        if len(self.recorded_events) >= MAX_RECORDING_RAW_EVENTS:
            requested = self._request_recording_limit_stop_locked(
                f"原始输入已达到 {MAX_RECORDING_RAW_EVENTS} 条上限"
            )
        return True, requested

    def _flush_pending_recorded_move_locked(self):
        pending = self.recording_pending_move
        self.recording_pending_move = None
        if not pending:
            return False
        event = dict(pending)
        if event.get("relative_raw"):
            try:
                if int(event.get("dx", 0)) == 0 and int(event.get("dy", 0)) == 0:
                    return False
            except (TypeError, ValueError, OverflowError):
                return False
        appended, request_finish = self._append_recorded_event_locked(event)
        if appended:
            self.last_recorded_move = float(
                event.get("time", self.last_recorded_move or time.perf_counter())
            )
        return request_finish

    def _flush_pending_recorded_move(self):
        request_finish = False
        with self.recording_lock:
            request_finish = self._flush_pending_recorded_move_locked()
        if request_finish:
            signal = getattr(self, "recording_stop_signal", None)
            if signal is not None:
                signal.emit()

    def _store_recorded_event(self, event):
        # Store normal uses of the configured control keys and modifiers. When
        # an exact cancel/finish shortcut is recognized, only that final control
        # sequence is removed by _discard_recording_control_events.
        request_finish = False
        with self.recording_lock:
            if (
                not self.recording
                or getattr(self, "recording_limit_stop_requested", False)
            ):
                return
            kind = event.get("kind")
            now = float(event.get("time", time.perf_counter()))
            started_at = float(getattr(self, "recording_started_at", 0.0) or 0.0)
            if (
                started_at > 0
                and (now - started_at) * 1000 >= MAX_RECORDING_DURATION_MS
            ):
                request_finish = self._request_recording_limit_stop_locked(
                    f"录制时长已达到 {MAX_RECORDING_DURATION_MS // 60000} 分钟上限"
                )
            elif kind == "move":
                if not self.recording_options.get("record_move"):
                    return
                move_interval = max(10, int(
                    self.recording_options.get("move_interval_ms", 80) or 80
                )) / 1000
                prepared = dict(event)
                if not prepared.get("relative_raw"):
                    prepared.update(self._recording_move_context_fields())
                pending = self.recording_pending_move
                if prepared.get("relative_raw"):
                    if pending and pending.get("relative_raw"):
                        pending["dx"] = int(pending.get("dx", 0)) + int(
                            prepared.get("dx", 0)
                        )
                        pending["dy"] = int(pending.get("dy", 0)) + int(
                            prepared.get("dy", 0)
                        )
                        pending["time"] = now
                        pending["move_flags"] = prepared.get("move_flags", 0)
                    else:
                        request_finish = (
                            self._flush_pending_recorded_move_locked()
                            or request_finish
                        )
                        if not request_finish:
                            self.recording_pending_move = prepared
                else:
                    if pending and pending.get("relative_raw"):
                        request_finish = (
                            self._flush_pending_recorded_move_locked()
                            or request_finish
                        )
                    # Absolute-coordinate modes keep the most recent position in
                    # the sampling window instead of the first position.
                    if not request_finish:
                        self.recording_pending_move = prepared
                baseline = self.last_recorded_move or self.recording_started_at
                if not request_finish and now - baseline >= move_interval:
                    request_finish = self._flush_pending_recorded_move_locked()
            else:
                # Preserve the order between a final movement sample and a following
                # click/key/wheel event even when the movement interval has not elapsed.
                request_finish = self._flush_pending_recorded_move_locked()
                if not request_finish:
                    # A physical event can be followed immediately by Kanata's injected
                    # pass-through copy. Collapse only near-identical pairs.
                    signature = (
                        kind,
                        event.get("name"),
                        event.get("down"),
                        event.get("delta"),
                    )
                    previous = self.recording_recent_events.get(signature, -1.0)
                    if now - previous < 0.004:
                        return
                    self.recording_recent_events[signature] = now
                    _appended, request_finish = self._append_recorded_event_locked(
                        event
                    )

        if request_finish:
            signal = getattr(self, "recording_stop_signal", None)
            if signal is not None:
                signal.emit()

    def _raw_recording_event(self, event):
        if (
            getattr(self, "_shutdown_in_progress", False)
            or getattr(self, "_shutdown_started", False)
        ):
            return
        if (
            getattr(self, "recording_session_active", False)
            and event.get("injected")
            and event.get("kind") in ("key", "button")
        ):
            if self._recording_control_event(
                event.get("name"), bool(event.get("down"))
            ):
                return
        if not self.recording:
            return
        # 主窗口内的操作也应被录制；录制状态使用不可点击的屏幕浮窗，
        # 避免结束录制的那次点击被写入动作序列。
        if self._event_targets_recording_overlay(event):
            return
        # 直接在钩子线程写入带锁队列，避免“停止录制”信号先于
        # 尚未处理的事件信号抵达 UI 线程，导致错误显示录制为空。
        self._store_recorded_event(event)

    def mapping_to_task(self, mapping):
        # Presets enter this function only after being converted to the same
        # runtime mapping structure. From here onward both features use the same
        # mode fields, trigger-task handler and MacroController execution path.
        if mapping.get("_runtime_kind") == "preset":
            return {
                "id": mapping.get("id"),
                "name": mapping.get("name", "预设"),
                "execution_mode": mapping.get("mode", "执行一次"),
                "loop_count": int(mapping.get("loop_count", 1)),
                "loop_interval_ms": int(mapping.get("loop_interval_ms", 0)),
                "loop_interval_jitter_ms": int(
                    mapping.get("loop_interval_jitter_ms", 0)
                ),
                "speed_percent": int(mapping.get("speed_percent", 100)),
                "max_runtime_s": int(mapping.get("max_runtime_s", 0)),
                "parameters": copy.deepcopy(mapping.get("parameters", [])),
                "actions": clone_action_tree(mapping.get("actions", [])),
                "_preset_library": mapping.get("_preset_library", {}),
            }

        target = mapping.get("target", "A")
        action_type = "鼠标点击" if target in MOUSE_NAMES else "键盘点击"
        return {
            "id": f"mapping:{mapping['id']}",
            "name": (
                mapping.get("name")
                or (
                    f"基础映射：{combo_text(mapping.get('source_modifiers', '无'), mapping.get('source', ''))}"
                    f" → {combo_text(mapping.get('target_modifiers', '无'), target)}"
                )
            ),
            "execution_mode": mapping.get("mode", "执行一次"),
            "loop_count": int(mapping.get("loop_count", 1)),
            "loop_interval_ms": int(mapping.get("loop_interval_ms", 0)),
            "loop_interval_jitter_ms": int(
                mapping.get("loop_interval_jitter_ms", 0)
            ),
            "speed_percent": int(mapping.get("speed_percent", 100)),
            "max_runtime_s": int(mapping.get("max_runtime_s", 0)),
            "condition_enabled": bool(mapping.get("condition_enabled", False)),
            "condition_input": mapping.get("condition_input", "鼠标左键"),
            "condition_state": mapping.get("condition_state", "按住时"),
            "actions": [{
                "type": action_type,
                "modifiers": mapping.get("target_modifiers", "无"),
                "target": target,
                "hold_ms": int(mapping.get("hold_ms", 100)),
                "jitter_ms": int(mapping.get("hold_jitter_ms", 0)),
                "_vkey": mapping.get("_vkey"),
            }],
        }

    def _dispatch_runtime_mapping_rule(
        self, rule, trigger_token, down, repeated, trigger_name=None,
    ):
        """Execute one applied rule with the original basic-mapping dispatcher."""
        if (
            rule.get("_runtime_kind") == "preset"
            and rule.get("id") in self.suspended_preset_ids
        ):
            return False
        if (
            rule.get("_runtime_kind", "mapping") == "mapping"
            and rule.get("id") in self.suspended_mapping_ids
        ):
            return False
        if down and self._runtime_cleanup_blocks_new_output():
            self._explain_runtime_cleanup_block("runtime_mapping_rule")
            return False
        if (
            down
            and rule.get("condition_enabled", False)
            and not rule.get("_condition_prevalidated", False)
        ):
            with getattr(self, "input_state_lock", nullcontext()):
                held_inputs = set(self.physical_down)
            if not mapping_condition_satisfied(rule, held_inputs):
                self.write_diagnostic(
                    "runtime_trigger_condition_rejected",
                    rule_id=rule.get("id"),
                    condition_input=rule.get("condition_input"),
                    condition_state=rule.get("condition_state"),
                    trigger=trigger_token,
                )
                return False
        if (
            rule.get("_runtime_kind", "mapping") == "mapping"
            and rule.get("mode", "同步按住") == "同步按住"
        ):
            mapping_id = rule.get("id") or str(id(rule))
            logical_trigger = trigger_name or trigger_token
            with getattr(self, "input_state_lock", nullcontext()):
                active = self.active_sync_by_source.setdefault(trigger_token, {})
                if down and not repeated:
                    # Interception mouse packets are edges. A new Down while this
                    # source is still active means its previous Up was lost. Keep
                    # the state decision and output ownership update atomic against
                    # GUI-side profile/delete cleanup.
                    if mapping_id in active and logical_trigger in MOUSE_NAMES:
                        if self._release_sync_mapping(active[mapping_id]):
                            active.pop(mapping_id, None)
                        else:
                            self._latch_sync_release_failure([mapping_id])
                            return True
                    if mapping_id not in active and self._press_sync_mapping(rule):
                        active[mapping_id] = dict(rule)
                elif not down:
                    active_mapping = active.get(mapping_id)
                    if active_mapping is not None:
                        if self._release_sync_mapping(active_mapping):
                            active.pop(mapping_id, None)
                            if not active:
                                self.active_sync_by_source.pop(trigger_token, None)
                        else:
                            self._latch_sync_release_failure([mapping_id])
            return True

        task = self.mapping_to_task(rule)
        if down and task.get("execution_mode") != "按住循环":
            source = str(rule.get("source") or trigger_name or "")
            configured_modifiers = list(modifier_names(
                rule.get("source_modifiers", "无")
            ))
            with getattr(self, "input_state_lock", nullcontext()):
                held_modifiers = list(
                    getattr(self, "physical_modifiers", set()) or []
                )
            task["_trigger_release_inputs"] = list(dict.fromkeys(
                [source, *configured_modifiers, *held_modifiers]
            ))
        return bool(self.handle_trigger_task(
            task, trigger_token, down, repeated
        ))

    def handle_trigger_task(self, task, name, down, repeated):
        task_id = task["id"]
        mode = task.get("execution_mode", "执行一次")
        backend_active = self._macro_backend_active()
        already_running = self.macro_controller.is_running(task_id)
        self.write_diagnostic(
            "trigger_task",
            task_id=task_id,
            task=task.get("name", ""),
            trigger=name,
            down=down,
            repeated=repeated,
            mode=mode,
            backend_active=backend_active,
            already_running=already_running,
        )
        if down and self._runtime_cleanup_blocks_new_output():
            self._explain_runtime_cleanup_block("trigger_task")
            return False
        # Only the preset's own execution mode controls how its trigger key is
        # interpreted. Nested loop cards govern their local action subtree and
        # must not silently turn an otherwise one-shot preset into a hold/toggle
        # trigger at the top level.
        toggle_controlled = mode == "开关循环"
        hold_controlled = mode == "按住循环"
        if down and not repeated:
            if toggle_controlled and already_running:
                stop_task = getattr(self, "_request_stop_macro_task", None)
                if callable(stop_task):
                    stop_result = stop_task(task_id, "正在停止开关循环宏并释放按键")
                else:
                    stop_result = self.macro_controller.stop(task_id)
                stopped = True if stop_result is None else bool(stop_result)
                self.write_diagnostic(
                    "trigger_task_stop_toggle", task_id=task_id, stopped=stopped
                )
                return stopped
            else:
                issue = self._recorded_mouse_context_issue(task.get("actions", []))
                if issue is not None:
                    self._report_recorded_mouse_context_issue(
                        task, issue, source="trigger"
                    )
                    return False
                task = dict(task)
                task["_required_profile_id"] = str(self.active_profile_id or "")
                restartable = mode in ("执行一次", "固定次数", "单次触发")
                restart = getattr(self.macro_controller, "restart", None)
                if already_running and restartable and callable(restart):
                    started = restart(task)
                    self.write_diagnostic(
                        "trigger_task_restart",
                        task_id=task_id,
                        started=started,
                    )
                else:
                    started = self.macro_controller.start(task)
                self.write_diagnostic(
                    "trigger_task_start",
                    task_id=task_id,
                    started=started,
                )
                if hold_controlled and started:
                    with getattr(self, "input_state_lock", nullcontext()):
                        self.held_trigger_ids.setdefault(name, set()).add(task_id)
                if not started:
                    self.write_diagnostic(
                        "trigger_task_start_rejected",
                        task_id=task_id,
                        mode=mode,
                        backend_active=backend_active,
                        already_running=already_running,
                    )
                return bool(started)
        elif not down and hold_controlled:
            with getattr(self, "input_state_lock", nullcontext()):
                task_ids = self.held_trigger_ids.get(name)
                owned = bool(task_ids is not None and task_id in task_ids)
                if owned:
                    task_ids.discard(task_id)
                    if not task_ids:
                        self.held_trigger_ids.pop(name, None)
            if owned:
                stop_task = getattr(self, "_request_stop_macro_task", None)
                if callable(stop_task):
                    stop_result = stop_task(task_id, "正在停止按住循环宏并释放按键")
                else:
                    stop_result = self.macro_controller.stop(task_id)
                stopped = True if stop_result is None else bool(stop_result)
                self.write_diagnostic(
                    "trigger_task_stop_hold", task_id=task_id, stopped=stopped
                )
                return stopped
            else:
                self.write_diagnostic(
                    "trigger_task_release_ignored",
                    task_id=task_id,
                    reason="trigger_did_not_start_task",
                )
                return False
        return False

    def _dispatch_preset_trigger(
        self, preset, trigger_token, down, repeated=False, source="interception"
    ):
        """Compatibility entry point using the basic-mapping runtime dispatcher."""
        if not preset:
            return False
        if not preset.get("enabled"):
            return False
        if not self._macro_backend_active() or not self.mappings_enabled:
            return False
        rule = self._preset_as_mapping_rule(preset)
        return self._dispatch_runtime_mapping_rule(
            rule, trigger_token, down, repeated
        )

    @staticmethod
    def _mapping_output_action(mapping):
        target = mapping.get("target", "A")
        return {
            "type": "鼠标点击" if target in MOUSE_NAMES else "键盘点击",
            "modifiers": mapping.get("target_modifiers", "无"),
            "target": target,
            "_vkey": mapping.get("_vkey"),
        }

    @staticmethod
    def _sync_signature(mapping):
        return (
            mapping.get("target_modifiers", "无"),
            mapping.get("target", "A"),
        )

    def _prune_expected_kanata_events(self, now=None, *, force=False):
        """Drop expired expected output events without growing the hot path list."""
        now = time.perf_counter() if now is None else float(now)
        last_prune = float(getattr(self, "expected_kanata_event_last_prune", 0.0))
        if not force and now - last_prune < 0.20:
            return 0
        limit = int(getattr(self, "expected_kanata_event_limit", 128))
        with self.expected_kanata_event_lock:
            self.expected_kanata_event_last_prune = now
            before = len(self.expected_kanata_events)
            kept = [
                item for item in self.expected_kanata_events
                if item[2] >= now
            ]
            if len(kept) > limit:
                kept = kept[-limit:]
            self.expected_kanata_events = kept
            return before - len(kept)

    def _expect_kanata_action_events(self, action, phase):
        """Register output events that may look physical under wintercept."""
        if action.get("type") not in ("键盘点击", "鼠标点击"):
            return
        target = action.get("target")
        if not target:
            return
        names = modifier_names(action.get("modifiers", "无")) + [target]
        if phase == "Press":
            events = [(name, True) for name in names]
        elif phase == "Release":
            events = [(name, False) for name in reversed(names)]
        elif phase == "Tap":
            events = (
                [(name, True) for name in names]
                + [(name, False) for name in reversed(names)]
            )
        else:
            return
        now = time.perf_counter()
        expires = now + 0.20
        limit = int(getattr(self, "expected_kanata_event_limit", 128))
        with self.expected_kanata_event_lock:
            self.expected_kanata_event_last_prune = now
            self.expected_kanata_events = [
                item for item in self.expected_kanata_events
                if item[2] >= now
            ]
            self.expected_kanata_events.extend(
                (name, down, expires) for name, down in events
            )
            if len(self.expected_kanata_events) > limit:
                self.expected_kanata_events = self.expected_kanata_events[-limit:]
        self.write_diagnostic(
            "expect_output_events",
            phase=phase,
            action_type=action.get("type"),
            target=action.get("target"),
            modifiers=action.get("modifiers", "无"),
            events=events,
        )

    def _consume_expected_kanata_event(self, name, down):
        now = time.perf_counter()
        with self.expected_kanata_event_lock:
            self.expected_kanata_event_last_prune = now
            self.expected_kanata_events = [
                item for item in self.expected_kanata_events
                if item[2] >= now
            ]
            for index, (expected_name, expected_down, _expires) in enumerate(
                self.expected_kanata_events
            ):
                if expected_name == name and expected_down == down:
                    self.expected_kanata_events.pop(index)
                    return True
        return False

    def _recorded_mouse_context_valid(self, action):
        if action.get("type") != "鼠标移动":
            return True
        context = action.get("recording_context")
        if not isinstance(context, dict):
            return True
        mode = str(context.get("mode") or "")
        if mode == "screen" and context.get("virtual_screen"):
            try:
                expected = tuple(int(value) for value in context["virtual_screen"])
            except (TypeError, ValueError, OverflowError):
                return False
            return expected == tuple(self._virtual_screen_geometry())
        if mode == "pct" and context.get("monitor_count") not in (None, ""):
            try:
                current = int(ctypes.windll.user32.GetSystemMetrics(80))
                return current == int(context["monitor_count"])
            except (TypeError, ValueError, OverflowError, OSError):
                return False
        if mode in ("window", "client"):
            expected_process = str(context.get("process") or "").strip()
            if not expected_process:
                return True
            current_process, _current_title = foreground_window_context()
            return current_process.casefold() == expected_process.casefold()
        return True

    def _recorded_mouse_context_issue(self, actions):
        """Return the first display-layout mismatch that is unsafe to start.

        Window/client actions intentionally are not checked here: a menu test
        temporarily makes MacroCanvas the foreground window during its countdown.
        They remain checked at send time after focus has returned to the target.
        """
        for action in iter_action_tree(actions):
            if action.get("type") != "鼠标移动":
                continue
            context = action.get("recording_context")
            if not isinstance(context, dict):
                continue
            mode = str(context.get("mode") or "")
            if mode == "screen" and context.get("virtual_screen"):
                try:
                    expected = tuple(
                        int(value) for value in context["virtual_screen"]
                    )
                except (TypeError, ValueError, OverflowError):
                    return {
                        "kind": "screen",
                        "expected": (),
                        "current": tuple(self._virtual_screen_geometry()),
                        "target": str(action.get("target") or ""),
                        "invalid_context": True,
                    }
                current = tuple(self._virtual_screen_geometry())
                if expected != current:
                    return {
                        "kind": "screen",
                        "expected": expected,
                        "current": current,
                        "target": str(action.get("target") or ""),
                    }
            elif mode == "pct" and context.get("monitor_count") not in (None, ""):
                try:
                    expected_count = int(context["monitor_count"])
                    current_count = int(ctypes.windll.user32.GetSystemMetrics(80))
                except (TypeError, ValueError, OverflowError, OSError):
                    return {
                        "kind": "monitor_count",
                        "expected": context.get("monitor_count"),
                        "current": None,
                        "target": str(action.get("target") or ""),
                    }
                if expected_count != current_count:
                    return {
                        "kind": "monitor_count",
                        "expected": expected_count,
                        "current": current_count,
                        "target": str(action.get("target") or ""),
                    }
        return None

    def _report_recorded_mouse_context_issue(self, task, issue, source):
        """Report a rejected hotkey start on the GUI thread without stealing focus."""
        payload = dict(issue or {})
        payload.update({
            "preset_id": str((task or {}).get("id") or ""),
            "preset_name": str((task or {}).get("name") or "宏任务"),
            "source": str(source or "runtime"),
        })
        self.write_diagnostic(
            "recorded_mouse_context_start_blocked",
            force=True,
            preset_id=payload["preset_id"],
            preset_name=payload["preset_name"],
            source=payload["source"],
            issue=payload,
        )
        signal = getattr(self, "recorded_mouse_context_mismatch_signal", None)
        if signal is not None:
            signal.emit(payload)
        if hasattr(self, "_play_feedback"):
            self._play_feedback("error")

    def _send_interception_action(self, action, phase):
        if getattr(self, "output_backend_retired", False):
            return False
        if (
            not bool(self.direct_interception_active)
            or phase not in ("Press", "Release", "Tap")
        ):
            return None
        if action.get("type") not in (
            "鼠标点击", "键盘点击", "鼠标滚轮", "鼠标移动"
        ):
            return None
        try:
            if self.interception_output is None:
                self.interception_output = InterceptionOutput()
            ok = self.interception_output.send_combo_action(
                action, phase
            )
        except OSError as error:
            self.write_diagnostic(
                "interception_output_error",
                phase=phase,
                target=action.get("target"),
                action_type=action.get("type"),
                error=str(error),
            )
            return False
        if ok is None:
            return None
        self.write_diagnostic(
            "interception_output",
            phase=phase,
            action_type=action.get("type"),
            target=action.get("target"),
            modifiers=action.get("modifiers", "无"),
            ok=ok,
        )
        return ok

    def _send_output_action(self, action, phase, wait=False, timeout=1.0):
        # Stop/profile-switch code raises output_shutdown_in_progress first and
        # then takes this same lock as a barrier.  Any Down already in flight
        # therefore finishes before cleanup; any later Down observes the gate
        # and is rejected before reaching Kanata or Interception.
        lock = getattr(self, "output_dispatch_lock", None)
        if lock is None:
            return self._send_output_action_locked(
                action, phase, wait=wait, timeout=timeout
            )
        with lock:
            return self._send_output_action_locked(
                action, phase, wait=wait, timeout=timeout
            )

    def _send_output_action_locked(
        self, action, phase, wait=False, timeout=1.0
    ):
        if getattr(self, "output_backend_retired", False):
            self.write_diagnostic(
                "output_rejected_after_backend_retired",
                phase=phase, action_type=action.get("type"),
                target=action.get("target"),
            )
            return False
        if self.output_shutdown_in_progress and phase in ("Press", "Tap"):
            self.write_diagnostic(
                "output_rejected_during_shutdown",
                phase=phase, action_type=action.get("type"),
                target=action.get("target"),
            )
            return False
        if not self._recorded_mouse_context_valid(action):
            self.write_diagnostic(
                "recorded_mouse_context_mismatch",
                phase=phase,
                target=action.get("target"),
                context=action.get("recording_context"),
            )
            return False
        direct_output = self._send_interception_action(action, phase)
        if direct_output is not None:
            return direct_output
        if self._runtime_is_game_mode():
            self.write_diagnostic(
                "interception_action_not_sent",
                reason="unsupported_interception_action",
                phase=phase,
                action_type=action.get("type"),
                target=action.get("target"),
            )
            return False
        virtual_key = action.get("_vkey")
        if not virtual_key or not self.engine.is_running():
            self.write_diagnostic(
                "kanata_action_not_sent",
                reason="missing_vkey_or_engine_stopped",
                phase=phase,
                virtual_key=virtual_key,
                engine_running=self.engine.is_running(),
                action_type=action.get("type"),
                target=action.get("target"),
            )
            return False
        self._expect_kanata_action_events(action, phase)
        queued = self.engine.queue_virtual_key_action(
            virtual_key, phase, wait=wait, timeout=timeout
        )
        self.write_diagnostic(
            "kanata_action_queued",
            phase=phase,
            virtual_key=virtual_key,
            queued=queued,
            action_type=action.get("type"),
            target=action.get("target"),
            modifiers=action.get("modifiers", "无"),
            engine_running=self.engine.is_running(),
        )
        return queued

    def _send_kanata_action(self, action, phase):
        # This method is called from the Windows low-level hook for sync
        # mappings. Queue the command and return immediately; blocking here can
        # deadlock Kanata output and cause Windows to remove the hook.
        return self._send_output_action(action, phase, wait=False)

    def _press_sync_mapping(self, mapping):
        signature = self._sync_signature(mapping)
        with self.sync_output_lock:
            existing = self.sync_output_counts.get(signature)
            if existing:
                existing["count"] += 1
                return True

            action = self._mapping_output_action(mapping)
            press_window = (
                foreground_window_identity()
                if action.get("type") == "鼠标点击"
                and action.get("target") in MOUSE_NAMES
                else None
            )
            if not self._send_kanata_action(action, "Press"):
                return False
            self.sync_output_counts[signature] = {
                "count": 1,
                "action": action,
                "press_window": press_window,
            }
            return True

    def _sync_mouse_release_must_wait(self, action, press_window):
        if (
            action.get("type") != "鼠标点击"
            or action.get("target") not in MOUSE_NAMES
            or not isinstance(press_window, dict)
        ):
            return False
        return not foreground_window_identity_matches(press_window)

    def _release_sync_mapping(self, mapping):
        signature = self._sync_signature(mapping)
        with self.sync_output_lock:
            existing = self.sync_output_counts.get(signature)
            if not existing:
                return True
            existing["count"] -= 1
            if existing["count"] > 0:
                return True
            action = existing["action"]
            press_window = existing.get("press_window")
            if self._sync_mouse_release_must_wait(action, press_window):
                if self._quarantine_mouse_release(action, press_window):
                    self.sync_output_counts.pop(signature, None)
                    return True
                existing["count"] = 1
                return False
            # Do not forget the output until Release actually succeeds.  This
            # lets emergency/engine-stop cleanup retry a transient driver error.
            for attempt in range(3):
                if self._send_kanata_action(action, "Release"):
                    self.sync_output_counts.pop(signature, None)
                    return True
                if attempt < 2:
                    time.sleep(0.02)
            existing["count"] = 1
            return False

    def _latch_sync_release_failure(self, failed):
        """Record sync Release failure in the same cleanup latch used by macros."""
        failed = [str(item) for item in (failed or []) if str(item)]
        if not failed:
            return
        failures = [f"同步映射释放失败({len(failed)})"]
        remember = getattr(self, "_remember_macro_cleanup_failure", None)
        if callable(remember):
            remember("同步映射释放失败", failures)
        else:
            remembered = list(getattr(self, "last_macro_release_failures", []) or [])
            for item in failures:
                if item not in remembered:
                    remembered.append(item)
            self.last_macro_release_failures = remembered
            self.output_shutdown_in_progress = True
            self.macro_state = MacroState.STOP_TIMEOUT
            self.macro_status_detail = "同步映射释放失败"
        self.profile_trigger_allowed = False

    def _release_detached_sync_mappings(self, ownership_entries):
        """Release detached sync holds and restore ownership after failure.

        Source-Up, condition changes and mapping deletion all detach ownership
        before touching the potentially slow output backend.  A transient
        Release failure must put the exact source/mapping ownership back so a
        later source edge, emergency stop or engine-stop cleanup can retry it.
        """
        released = []
        failed = []
        for trigger_token, mapping_id, mapping in ownership_entries:
            if self._release_sync_mapping(mapping):
                released.append(str(mapping_id))
                continue
            failed.append(str(mapping_id))
            with getattr(self, "input_state_lock", nullcontext()):
                self.active_sync_by_source.setdefault(
                    trigger_token, {}
                ).setdefault(mapping_id, mapping)
        if failed:
            self._latch_sync_release_failure(failed)
            self.write_diagnostic(
                "sync_release_ownership_restored",
                force=True,
                mapping_ids=failed,
            )
        return released, failed

    def _release_all_sync_mappings(self):
        with self.sync_output_lock:
            pending = list(self.sync_output_counts.items())
            for signature, item in reversed(pending):
                action = item["action"]
                press_window = item.get("press_window")
                released = False
                if self._sync_mouse_release_must_wait(action, press_window):
                    released = bool(
                        self._quarantine_mouse_release(action, press_window)
                    )
                else:
                    for attempt in range(3):
                        if self._send_kanata_action(action, "Release"):
                            released = True
                            break
                        if attempt < 2:
                            time.sleep(0.02)
                if released:
                    self.sync_output_counts.pop(signature, None)
            remaining_signatures = set(self.sync_output_counts)
        with getattr(self, "input_state_lock", nullcontext()):
            if not remaining_signatures:
                self.active_sync_by_source.clear()
            else:
                for source, mappings in list(self.active_sync_by_source.items()):
                    retained = {
                        mapping_id: mapping
                        for mapping_id, mapping in mappings.items()
                        if self._sync_signature(mapping) in remaining_signatures
                    }
                    if retained:
                        self.active_sync_by_source[source] = retained
                    else:
                        self.active_sync_by_source.pop(source, None)
        return not remaining_signatures

    def _release_invalid_conditional_holds(self):
        """Release hold-bound outputs whose optional condition became false."""
        with getattr(self, "input_state_lock", nullcontext()):
            has_conditional_sync = any(
                mapping.get("condition_enabled", False)
                for active in self.active_sync_by_source.values()
                for mapping in active.values()
            )
            has_held_tasks = any(self.held_trigger_ids.values())
        if not has_conditional_sync and not has_held_tasks:
            return False

        rules = self._runtime_mapping_rules() if has_held_tasks else []
        held_rule_by_task = {
            (
                str(rule.get("id"))
                if rule.get("_runtime_kind") == "preset"
                else f"mapping:{rule.get('id')}"
            ): rule
            for rule in rules
            if (
                rule.get("condition_enabled", False)
                and rule.get("mode", "同步按住") == "按住循环"
            )
        }
        sync_releases = []
        stopped_task_ids = []
        with getattr(self, "input_state_lock", nullcontext()):
            held_inputs = set(self.physical_down)
            for trigger_token in list(self.active_sync_by_source):
                active = self.active_sync_by_source.get(trigger_token, {})
                for mapping_id, mapping in list(active.items()):
                    if (
                        mapping.get("condition_enabled", False)
                        and not mapping_condition_satisfied(mapping, held_inputs)
                    ):
                        active.pop(mapping_id, None)
                        sync_releases.append((
                            trigger_token, mapping_id, mapping
                        ))
                if not active:
                    self.active_sync_by_source.pop(trigger_token, None)

            for trigger_token in list(self.held_trigger_ids):
                task_ids = self.held_trigger_ids.get(trigger_token, set())
                for task_id in list(task_ids):
                    rule = held_rule_by_task.get(task_id)
                    if rule is None:
                        continue
                    if not mapping_condition_satisfied(rule, held_inputs):
                        task_ids.discard(task_id)
                        stopped_task_ids.append(task_id)
                if not task_ids:
                    self.held_trigger_ids.pop(trigger_token, None)

        released_sync, failed_sync = self._release_detached_sync_mappings(
            sync_releases
        )
        for task_id in stopped_task_ids:
            stop_task = getattr(self, "_request_stop_macro_task", None)
            if callable(stop_task):
                stop_task(task_id, "条件失效，正在停止按住循环宏")
            else:
                self.macro_controller.stop(task_id)
        if released_sync or failed_sync or stopped_task_ids:
            self.write_diagnostic(
                "conditional_hold_released",
                sync_count=len(released_sync),
                sync_release_failed=failed_sync,
                task_ids=stopped_task_ids,
            )
        return bool(released_sync or failed_sync or stopped_task_ids)

    def _runtime_mapping_rules(self):
        """Return the one applied table used by the basic-mapping matcher."""
        with self.data_lock:
            rules = []
            for item in self.runtime_trigger_rules:
                copied = dict(item)
                if copied.get("_runtime_kind") == "preset":
                    copied["actions"] = clone_action_tree(
                        item.get("actions", [])
                    )
                rules.append(copied)
            return rules

    @staticmethod
    def _modifier_text(values):
        return "+".join(
            item for item in MODIFIER_ORDER if item in set(values)
        ) or "无"

    @staticmethod
    def _shortcut_matches(name, modifiers, expected_modifiers, expected_key):
        return (
            name == expected_key
            and modifiers == (expected_modifiers or "无")
        )

    def _discard_recording_control_events(self, modifiers, key):
        """Remove only the exact trailing shortcut used to end recording.

        Modifier keys remain recordable everywhere else. A modifier press is
        removed only when no non-control event occurred after it, which avoids
        deleting a modifier that was still being used by the last real action.
        """
        required = set(modifier_names(modifiers))
        allowed_names = required | {key}
        with self.recording_lock:
            events = self.recorded_events
            key_index = None
            for index in range(len(events) - 1, -1, -1):
                event = events[index]
                if (
                    event.get("kind") in ("key", "button")
                    and event.get("name") == key
                    and bool(event.get("down"))
                ):
                    key_index = index
                    break
            if key_index is None:
                return

            remove_indexes = {key_index}
            for modifier in required:
                depth = 0
                modifier_index = None
                for index in range(key_index - 1, -1, -1):
                    event = events[index]
                    if (
                        event.get("kind") != "key"
                        or event.get("name") != modifier
                    ):
                        continue
                    if not event.get("down"):
                        depth += 1
                    elif depth:
                        depth -= 1
                    else:
                        modifier_index = index
                        break
                if modifier_index is None:
                    continue
                trailing_real_event = any(
                    event.get("kind") in ("key", "button", "wheel", "move")
                    and event.get("name") not in allowed_names
                    for event in events[modifier_index + 1:key_index]
                )
                if not trailing_real_event:
                    remove_indexes.add(modifier_index)

            self.recorded_events = [
                event for index, event in enumerate(events)
                if index not in remove_indexes
            ]
            for signature, timestamp in list(
                self.recording_recent_events.items()
            ):
                if signature[1] in allowed_names:
                    self.recording_recent_events.pop(signature, None)

    def _recording_control_event(self, name, down, source_id=None):
        if not hasattr(self, "recording_control_sources"):
            self.recording_control_sources = {}
        token = self._input_source_token(name, source_id)
        with getattr(self, "input_state_lock", nullcontext()):
            repeated, _token = self._update_physical_input_state_locked(
                name, down, token
            )
        if name in MODIFIER_ORDER:
            if down:
                self.recording_control_sources[token] = name
            else:
                self.recording_control_sources.pop(token, None)
            self.recording_control_modifiers.clear()
            self.recording_control_modifiers.update(
                self.recording_control_sources.values()
            )
        effective = set(self.recording_control_modifiers)
        if name in MODIFIER_ORDER:
            effective.discard(name)
        modifiers = self._modifier_text(effective)

        controls = (
            (
                "record_cancel",
                self.runtime_recording_cancel_modifiers,
                self.runtime_recording_cancel_key,
                self.recording_cancel_signal,
            ),
            (
                "record_finish",
                self.runtime_recording_finish_modifiers,
                self.runtime_recording_finish_key,
                self.recording_stop_signal,
            ),
        )
        if getattr(self, "recording_restore_pending", False) and down:
            if any(name == expected_key for _cid, _mods, expected_key, _sig in controls):
                return True
        for control_id, expected_modifiers, expected_key, signal in controls:
            if name != expected_key:
                continue
            if not down and self._unlatch_system_hotkey(control_id, token):
                self._request_recording_restore_check()
                return True
            if down and control_id in self.system_hotkey_latched:
                return self._system_hotkey_latched_by(control_id, token)
            if (
                down
                and not repeated
                and self._shortcut_matches(
                    name, modifiers, expected_modifiers, expected_key
                )
            ):
                self._latch_system_hotkey(control_id, token)
                self._discard_recording_control_events(
                    expected_modifiers, expected_key
                )
                self.write_diagnostic(
                    "system_hotkey_input",
                    control=control_id, key=name, modifiers=modifiers,
                    recording=True,
                )
                if control_id == "record_finish" and not self.recording:
                    self.write_diagnostic(
                        "record_finish_ignored_during_countdown",
                        key=name,
                        modifiers=modifiers,
                    )
                    return True
                signal.emit()
                return True
        if not down:
            self._request_recording_restore_check()
        return False

    def _handle_interception_control_event(self, name, down, source_id=None):
        """Decode only the engine-toggle shortcut while the engine is closed."""
        if not self.runtime_global_toggle_enabled:
            return False

        token = self._input_source_token(name, source_id)
        with getattr(self, "input_state_lock", nullcontext()):
            repeated, _token = self._update_physical_input_state_locked(
                name, down, token
            )
            self._ensure_physical_source_tables_locked()
            if name in MODIFIER_ORDER:
                if down:
                    self.interception_control_sources[token] = name
                else:
                    self.interception_control_sources.pop(token, None)
                self.interception_control_modifiers.clear()
                self.interception_control_modifiers.update(
                    self.interception_control_sources.values()
                )

        effective_modifiers = set(self.interception_control_modifiers)
        if name in MODIFIER_ORDER:
            effective_modifiers.discard(name)
        current_modifiers = "+".join(
            item for item in MODIFIER_ORDER if item in effective_modifiers
        ) or "无"

        if (
            name == self.runtime_global_toggle_key
            and not down
            and self.global_toggle_latched
            and getattr(self, "global_toggle_latched_source", token) == token
        ):
            self.global_toggle_latched = False
            self.global_toggle_latched_source = None
            self.write_diagnostic(
                "interception_control_toggle_release",
                key=name,
                modifiers=current_modifiers,
                source_id=token,
            )
            return True

        matched = (
            name == self.runtime_global_toggle_key
            and current_modifiers == self.runtime_global_toggle_modifiers
        )
        if matched and down:
            if self.global_toggle_latched:
                return bool(
                    getattr(self, "global_toggle_latched_source", None)
                    == token
                )
            if repeated:
                return False
            self.global_toggle_latched = True
            self.global_toggle_latched_source = token
            self.write_diagnostic(
                "interception_control_toggle_input",
                key=name,
                modifiers=current_modifiers,
                listener_alive=True,
                source_id=token,
            )
            self.global_toggle_signal.emit(True)
            return True
        return False

    def _interception_source_callback(self, name, down, source_id=None):
        """Keep one physical Interception edge pair on one routing path.

        A foreground-profile switch can happen after a physical Down has already
        been forwarded but before its Up arrives.  The Up must follow the Down's
        original routing decision; otherwise a rule in the newly active profile
        can swallow only the Up and leave Windows or the foreground application
        believing that the physical key/button is still held.
        """
        name = str(name or "")
        down = bool(down)
        route_token = self._input_source_token(name, source_id)
        with getattr(self, "input_state_lock", nullcontext()):
            forwarded_before = route_token in self.interception_forwarded_down
        suppressed = bool(
            self._global_hook_callback(
                name, down, interception=True, source_id=route_token
            )
        )
        with getattr(self, "input_state_lock", nullcontext()):
            if down:
                if suppressed:
                    self.interception_forwarded_down.discard(route_token)
                else:
                    self.interception_forwarded_down.add(route_token)
            else:
                # Defense in depth: even if a future branch accidentally tries to
                # consume this Up, a Down already delivered to Windows must receive
                # its matching Up.
                if forwarded_before and suppressed:
                    self.write_diagnostic(
                        "interception_release_route_corrected",
                        force=True,
                        name=name,
                        source_id=route_token,
                    )
                    suppressed = False
                self.interception_forwarded_down.discard(route_token)
        return suppressed

    def _handle_settings_input_event(self, name, down, source_id=None):
        """Track physical edges while settings are modal without triggering rules.

        A Down consumed before the dialog still owns its matching Up. Inputs first
        pressed inside the dialog remain forwarded, and a key still held when the
        dialog closes remains in ``physical_down`` so auto-repeat cannot become a
        fresh trigger.
        """
        consume_release = False
        route_token = self._input_source_token(name, source_id)
        with getattr(self, "input_state_lock", nullcontext()):
            self._update_physical_input_state_locked(
                name, down, route_token
            )
            if not down:
                if route_token in self.suppressed_trigger_names:
                    self.suppressed_trigger_names.discard(route_token)
                    consume_release = True

                if (
                    name == getattr(self, "runtime_global_toggle_key", "")
                    and getattr(self, "global_toggle_latched", False)
                    and getattr(
                        self, "global_toggle_latched_source", route_token
                    ) == route_token
                ):
                    self.global_toggle_latched = False
                    self.global_toggle_latched_source = None
                    consume_release = True

                latched_controls = (
                    ("emergency", getattr(self, "runtime_emergency_key", "")),
                    ("global_toggle", getattr(self, "runtime_global_toggle_key", "")),
                    ("macro_pause", getattr(self, "runtime_macro_pause_key", "")),
                    (
                        "record_cancel",
                        getattr(self, "runtime_recording_cancel_key", ""),
                    ),
                    (
                        "record_finish",
                        getattr(self, "runtime_recording_finish_key", ""),
                    ),
                )
                for control_id, expected_key in latched_controls:
                    if (
                        name == expected_key
                        and self._unlatch_system_hotkey(
                            control_id, route_token
                        )
                    ):
                        consume_release = True

        self._release_invalid_conditional_holds()
        self.write_diagnostic(
            "settings_input_tracked",
            name=name,
            down=down,
            consumed_release=consume_release,
            source_id=route_token,
        )
        return consume_release

    def _global_hook_callback(
        self, name, down, interception=False, source_id=None,
    ):
        if (
            getattr(self, "_shutdown_in_progress", False)
            or getattr(self, "_shutdown_started", False)
        ):
            return False
        route_token = self._input_source_token(name, source_id)
        with getattr(self, "input_state_lock", nullcontext()):
            source_was_down = bool(
                down
                and route_token in getattr(self, "physical_input_sources", {})
            )
            forwarded_down = bool(
                interception
                and not down
                and route_token in self.interception_forwarded_down
            )
        self.write_diagnostic(
            "physical_hook",
            name=name,
            down=down,
            interception=interception,
            running=self.running,
            engine_running=self.engine.is_running(),
            recording=getattr(self, "recording_session_active", False),
            settings=self.settings_dialog_active,
            source_id=route_token,
        )
        if interception and self.interception_input_control_only:
            return self._handle_interception_control_event(
                name, down, route_token
            )
        # Kanata 运行且当前未录制/设置快捷键时，普通触发由 Kanata 处理。
        # Python 只截获急停、全局开关、暂停宏这三类控制键作为兜底，
        # 其他输入立即放行，避免低级钩子扩大普通按键延迟。
        if (
            self.engine.is_running()
            and not interception
            and not getattr(self, "recording_session_active", False)
            and not self.settings_dialog_active
        ):
            # Kanata/wintercept 输出偶尔可能未被 Windows 标成 injected。
            # 先消费已登记的宏输出事件，再维护物理输入快照，避免宏输出
            # 反向污染条件键状态或被当作急停/暂停等控制键处理。
            if self._consume_expected_kanata_event(name, down):
                self.write_diagnostic(
                    "physical_hook_expected_output",
                    name=name,
                    down=down,
                    source_id=route_token,
                    phase="kanata_passthrough",
                )
                return False
            with getattr(self, "input_state_lock", nullcontext()):
                source_repeated, _source_token = (
                    self._update_physical_input_state_locked(
                        name, down, route_token
                    )
                )
            self._release_invalid_conditional_holds()
            control_consumed = self._handle_kanata_owned_control_input(
                name, down, route_token, repeated=bool(source_repeated)
            )
            self.write_diagnostic(
                "physical_hook_passthrough",
                reason=(
                    "kanata_control_consumed"
                    if control_consumed else "kanata_owns_input"
                ),
                name=name,
                down=down,
                source_id=route_token,
            )
            return bool(control_consumed)

        # 在录制/设置等确实需要观察输出事件的阶段做合成事件匹配。
        if self._consume_expected_kanata_event(name, down):
            self.write_diagnostic(
                "physical_hook_expected_output",
                name=name,
                down=down,
                source_id=route_token,
                phase="observer",
            )
            return False

        if self.settings_dialog_active:
            return self._handle_settings_input_event(
                name, down, route_token
            )

        # While Kanata is running, it owns the configured emergency and engine
        # switch shortcuts. Avoid processing the same physical event twice on
        # the desktop. Recording is excluded because its controls are handled by
        # the dedicated recording state below.
        if self.engine.is_running() and not interception:
            if name == self.runtime_emergency_key:
                return False
            if (
                self.runtime_global_toggle_enabled
                and name == self.runtime_global_toggle_key
            ):
                return False
            if (
                self.runtime_macro_pause_enabled
                and name == self.runtime_macro_pause_key
            ):
                return False

        # Recording controls have priority over every mapping and preset. They use
        # the same Interception/global input stream, but never become recorded
        # actions themselves.
        if getattr(self, "recording_session_active", False):
            return self._recording_control_event(
                name, down, route_token
            )

        # Configurable emergency fallback for direct Interception and for desktop
        # mode while Kanata is stopped.
        with getattr(self, "input_state_lock", nullcontext()):
            emergency_effective = set(self.physical_modifiers)
        if name in MODIFIER_ORDER:
            if down:
                emergency_effective.add(name)
            else:
                emergency_effective.discard(name)
            emergency_effective.discard(name)
        emergency_modifiers = self._modifier_text(emergency_effective)
        if name == self.runtime_emergency_key:
            if not down and self._unlatch_system_hotkey(
                "emergency", route_token
            ):
                return True
            if down and "emergency" in self.system_hotkey_latched:
                return self._system_hotkey_latched_by(
                    "emergency", route_token
                )
            if (
                down
                and not source_was_down
                and self._shortcut_matches(
                    name, emergency_modifiers,
                    self.runtime_emergency_modifiers,
                    self.runtime_emergency_key,
                )
            ):
                # Lock the release only when the corresponding Down is actually
                # consumed.  When the engine is already stopped and no macro is
                # alive, both edges must continue to the foreground application.
                if self.running or self.macro_controller.tasks:
                    self._latch_system_hotkey("emergency", route_token)
                    self.write_diagnostic(
                        "system_hotkey_input", control="emergency",
                        key=name, modifiers=emergency_modifiers, recording=False,
                    )
                    self.emergency_signal.emit()
                    return True
                return False

        sync_released = False
        held_released = False
        with getattr(self, "input_state_lock", nullcontext()):
            was_suppressed = route_token in self.suppressed_trigger_names
            active_sync = {}
            held_task_ids = set()
            if not down:
                self.suppressed_trigger_names.discard(route_token)
                active_sync = self.active_sync_by_source.pop(route_token, {})
                held_task_ids = self.held_trigger_ids.pop(route_token, set())

            # Interception mouse packets are edges, not keyboard auto-repeat.
            mouse_interception_edge = interception and name in MOUSE_NAMES
            source_repeated, _source_token = self._update_physical_input_state_locked(
                name, down, route_token
            )
            repeated = bool(
                down and source_repeated and not mouse_interception_edge
            )

            effective_modifiers = set(self.physical_modifiers)

        # Slow backend/task cleanup runs outside the state lock. The corresponding
        # ownership entries were detached atomically above, so GUI cleanup cannot
        # process the same source a second time.
        sync_ownership = [
            (route_token, mapping_id, mapping)
            for mapping_id, mapping in active_sync.items()
        ]
        if sync_ownership:
            self._release_detached_sync_mappings(sync_ownership)
            sync_released = True
        for task_id in list(held_task_ids):
            stop_task = getattr(self, "_request_stop_macro_task", None)
            if callable(stop_task):
                stop_task(task_id, "来源松开，正在停止按住循环宏")
            else:
                self.macro_controller.stop(task_id)
            held_released = True
        self._release_invalid_conditional_holds()
        if name in MODIFIER_ORDER:
            effective_modifiers.discard(name)
        current_modifiers = "+".join(
            item for item in MODIFIER_ORDER if item in effective_modifiers
        ) or "无"

        # F8 and the global switch are decoded from this same physical input
        # stream. In game mode that stream is owned entirely by Interception.
        # Apply the new gate immediately in the Interception thread so no mapping
        # can slip through while the queued UI-thread cleanup is still pending.
        if (
            self.runtime_global_toggle_enabled
            and name == self.runtime_global_toggle_key
            and not down
            and self.global_toggle_latched
            and getattr(self, "global_toggle_latched_source", route_token)
            == route_token
        ):
            self.global_toggle_latched = False
            self.global_toggle_latched_source = None
            return True
        toggle_match = (
            self.runtime_global_toggle_enabled
            and name == self.runtime_global_toggle_key
            and current_modifiers == self.runtime_global_toggle_modifiers
        )
        if toggle_match and down:
            if self.global_toggle_latched:
                return bool(
                    getattr(self, "global_toggle_latched_source", None)
                    == route_token
                )
            if repeated:
                return False
            self.global_toggle_latched = True
            self.global_toggle_latched_source = route_token
            desired_enabled = not self.running
            self.write_diagnostic(
                "global_toggle_input",
                interception=interception,
                desired_enabled=desired_enabled,
                modifiers=current_modifiers,
                key=name,
                source_id=route_token,
            )
            self.global_toggle_signal.emit(desired_enabled)
            return True

        pause_latch = "macro_pause"
        if (
            self.runtime_macro_pause_enabled
            and name == self.runtime_macro_pause_key
            and not down
            and self._unlatch_system_hotkey(pause_latch, route_token)
        ):
            return True
        pause_match = (
            self.runtime_macro_pause_enabled
            and name == self.runtime_macro_pause_key
            and current_modifiers == self.runtime_macro_pause_modifiers
        )
        if pause_match and down:
            if pause_latch in self.system_hotkey_latched:
                return self._system_hotkey_latched_by(
                    pause_latch, route_token
                )
            if repeated:
                return False
            self._latch_system_hotkey(pause_latch, route_token)
            self.write_diagnostic(
                "system_hotkey_input", control="macro_pause",
                key=name, modifiers=current_modifiers, recording=False,
            )
            self.macro_pause_signal.emit()
            return True

        # 即使刚才的快捷键已经把引擎关闭，也要吞掉对应的松开事件。
        if was_suppressed and not down and not self.running:
            return True
        if not self.running:
            return False

        if not interception:
            return was_suppressed or sync_released or held_released

        # Never decide how to route an Up from the newly active profile.  Its
        # Down already decided whether the physical stroke was forwarded or
        # suppressed.  Cleanup above still releases any old-profile outputs.
        if not down:
            if forwarded_down:
                self.write_diagnostic(
                    "interception_release_followed_forwarded_down",
                    name=name,
                    source_id=route_token,
                )
                return False
            return was_suppressed or sync_released or held_released

        if not self.profile_trigger_allowed:
            return was_suppressed or sync_released or held_released

        if foreground_window_belongs_to_current_process():
            self.write_diagnostic(
                "runtime_trigger_ignored",
                reason="macrocanvas_foreground",
                name=name,
                down=down,
                interception=interception,
            )
            return was_suppressed or sync_released

        # In game mode the disabled state is enforced here because Kanata no
        # longer owns any physical source. Release cleanup above still runs, but
        # new keyboard and mouse triggers pass through unchanged while disabled.
        if not self.mappings_enabled:
            return was_suppressed or sync_released

        # Both features are already represented as mapping-shaped rules in the
        # applied runtime snapshot. This is the same loop, same exact source name,
        # same modifier field and same dispatcher for basic mappings and presets.
        rules = self._runtime_mapping_rules()
        matched = sync_released
        checked_rules = 0
        candidates = []
        with getattr(self, "input_state_lock", nullcontext()):
            held_inputs = set(self.physical_down)
        for index, rule in enumerate(rules):
            checked_rules += 1
            if not rule.get("enabled") or rule.get("source") != name:
                continue
            configured_modifiers = rule.get("source_modifiers", "无")
            if not source_modifiers_match(configured_modifiers, current_modifiers):
                continue
            if not mapping_condition_satisfied(rule, held_inputs):
                continue
            candidates.append((
                -source_modifier_specificity(configured_modifiers),
                0 if rule.get("condition_enabled", False) else 1,
                0 if rule.get("_runtime_kind", "mapping") == "mapping" else 1,
                index,
                rule,
            ))

        if candidates:
            candidates.sort(key=lambda item: item[:4])
            rule = candidates[0][4]
            if len(candidates) > 1:
                shadowed_rule_ids = [
                    item[4].get("id") for item in candidates[1:]
                ]
                self.write_diagnostic(
                    "runtime_trigger_multiple_candidates",
                    name=name,
                    down=down,
                    repeated=repeated,
                    modifiers=current_modifiers,
                    selected_rule_id=rule.get("id"),
                    shadowed_rule_ids=shadowed_rule_ids,
                    source_id=route_token,
                )
                shadow_key = (str(name), str(current_modifiers))
                shadow_cache = getattr(self, "runtime_shadow_warning_last", {})
                now = time.monotonic()
                if now - float(shadow_cache.get(shadow_key, 0.0) or 0.0) > 2.0:
                    shadow_cache[shadow_key] = now
                    self.runtime_shadow_warning_last = shadow_cache
                    if hasattr(self, "engine_hint"):
                        self.engine_hint.setStyleSheet("color: #fbbf24;")
                        self.engine_hint.setText(
                            "同一快捷键有多条规则同时成立，本次只执行优先级最高的一条"
                        )
            dispatched = self._dispatch_runtime_mapping_rule(
                rule, route_token, down, repeated, name
            )
            matched = bool(matched or dispatched)
            self.write_diagnostic(
                "runtime_trigger_match" if dispatched
                else "runtime_trigger_dispatch_rejected",
                name=name,
                down=down,
                repeated=repeated,
                modifiers=current_modifiers,
                rule_id=rule.get("id"),
                runtime_kind=rule.get("_runtime_kind", "mapping"),
                mode=rule.get("mode", ""),
                condition_input=rule.get("condition_input"),
                condition_state=rule.get("condition_state"),
                source_id=route_token,
            )

        if not matched and name in MOUSE_NAMES:
            self.write_diagnostic(
                "runtime_trigger_no_match",
                name=name,
                down=down,
                repeated=repeated,
                modifiers=current_modifiers,
                checked_rules=checked_rules,
                source_id=route_token,
            )

        if down and matched:
            with getattr(self, "input_state_lock", nullcontext()):
                self.suppressed_trigger_names.add(route_token)
        return matched or was_suppressed

    @Slot(str, bool)
    def handle_global_input(self, name, down):
        self._global_hook_callback(
            str(name or ""), bool(down), interception=False,
            source_id=f"signal:{name}",
        )
