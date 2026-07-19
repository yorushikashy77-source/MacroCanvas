import random
import threading
import time

from PySide6.QtCore import QObject, Signal

from core.constants import (
    CONDITION_ACTION_TYPE, CONDITION_BRANCH_TYPES,
    CONDITION_ELSE_BRANCH_TYPE, CONDITION_TRUE_BRANCH_TYPE,
    CONTROL_ACTION_TYPES, MOUSE_NAMES, SUBMACRO_ACTION_TYPE,
    WAIT_CONDITION_ACTION_TYPE,
)
from macro.actions import clone_action_tree
from macro.parameters import merged_parameter_values, resolve_action_parameters
from engine.window_context import (
    foreground_window_identity,
    foreground_window_identity_matches,
)


LOOP_ACTION_TYPE = "循环动作"


class MacroSignals(QObject):
    progress = Signal(dict)
    action_activity = Signal(dict)
    task_finished = Signal(str)
    state_changed = Signal()


class MacroTask:
    MAX_PARALLEL_WORKERS = 16

    def __init__(
        self, preset, engine, signals,
        expect_output=None, send_output=None, is_active=None,
        profile_active=None, quarantine_release=None, condition_state=None,
        debug_state=None,
    ):
        # Resolve root defaults before action IDs and loop references are
        # indexed. Presets without named parameters retain the legacy action
        # path and avoid an unnecessary deep copy on every trigger.
        self.preset = dict(preset)
        self.preset["actions"] = (
            resolve_action_parameters(preset.get("actions", []), preset)
            if preset.get("parameters")
            else preset.get("actions", [])
        )
        self.engine = engine
        self.signals = signals
        self.expect_output = expect_output
        self.send_output = send_output
        # Game mode deliberately does not start Kanata.  Macro timing therefore
        # follows the selected backend's own liveness callback instead of being
        # hard-wired to KanataEngine.is_running().
        self.is_active = is_active or self.engine.is_running
        self.profile_active = profile_active
        self.quarantine_release = quarantine_release
        self.condition_state = condition_state
        self.debug_state = debug_state
        self.debug_lock = threading.RLock()
        self.debug_pause_info = None
        self.debug_pause_next = False
        self.finish_reason = ""
        self.last_failure_reason = ""
        self.root_debug_parameters = (
            merged_parameter_values(preset) if preset.get("parameters") else {}
        )
        self.stop_event = threading.Event()
        self.run_event = threading.Event()
        self.run_event.set()
        # press_key -> {"action": <dict>, "count": <logical holders>}
        #
        # Parallel timelines may overlap on the same output.  Sending Release
        # when the first branch finishes would prematurely cancel the longer
        # holder, so keep one physical press and a logical reference count.
        self.pressed = {}
        self.pressed_lock = threading.RLock()
        # Serialize every new Press/Tap with pause/stop barriers.  A worker may
        # already have been created when the user pauses or stops the task; this
        # lock guarantees that the barrier observes any in-flight output and
        # that no later synthetic Down can slip through afterwards.
        self.output_state_lock = threading.RLock()
        task_name = str(self.preset.get("id") or "task")
        self.thread = threading.Thread(
            target=self.run,
            name=f"MacroCanvas-Macro-{task_name}",
            daemon=False,
        )
        self.worker_threads = set()
        self.worker_lock = threading.RLock()
        self.started_at = 0.0
        self.deadline = 0.0
        self.clock_lock = threading.RLock()
        self.pause_started_at = 0.0
        self.paused_seconds = 0.0
        self.paused_actions = []
        self.release_cleanup_failed = False
        self.parallel_errors = []
        self.parallel_error_lock = threading.RLock()
        self.parallel_slots = threading.BoundedSemaphore(
            self.MAX_PARALLEL_WORKERS
        )
        self._finish_signal_lock = threading.RLock()
        self._finish_signal_emitted = False
        self.action_index = {}
        self._index_preset_actions(self.preset.get("actions", []))
        self.loop_controls = [
            action for action in self.preset.get("actions", [])
            if action.get("type") == LOOP_ACTION_TYPE
        ]
        self.loop_controls_by_start = {}
        for loop_action in self.loop_controls:
            target_ids = [
                str(value)
                for value in loop_action.get("target_action_ids", []) or []
                if value
            ]
            if target_ids:
                self.loop_controls_by_start.setdefault(target_ids[0], []).append(
                    loop_action
                )

    def _index_preset_actions(self, actions):
        for action in actions or []:
            if action.get("type") != LOOP_ACTION_TYPE:
                action_id = str(action.get("action_id") or "")
                if action_id:
                    self.action_index[action_id] = action
            self._index_preset_actions(action.get("children", []))

    def _debug_snapshot(self):
        if not callable(self.debug_state):
            return {"enabled": False, "breakpoints": set()}
        try:
            raw = dict(self.debug_state() or {})
        except Exception:
            return {"enabled": False, "breakpoints": set()}
        breakpoints = {
            (str(item[0]), str(item[1]))
            for item in raw.get("breakpoints", set()) or set()
            if isinstance(item, (tuple, list)) and len(item) == 2
        }
        return {
            "enabled": bool(raw.get("enabled", False)),
            "breakpoints": breakpoints,
        }

    def _debug_action_context(self, action):
        path = list(action.get("_debug_path", []) or [])
        if not path:
            path = [str(self.preset.get("name") or "预设")]
        return {
            "source_preset_id": str(
                action.get("_debug_preset_id") or self.preset.get("id") or ""
            ),
            "source_preset_name": str(
                action.get("_debug_preset_name")
                or self.preset.get("name") or "预设"
            ),
            "action_id": str(action.get("action_id") or ""),
            "action_type": str(action.get("type") or "动作"),
            "path": path,
            "parameters": dict(
                action.get("_debug_parameters", self.root_debug_parameters) or {}
            ),
        }

    def _debug_before_action(self, action):
        snapshot = self._debug_snapshot()
        if not snapshot["enabled"]:
            return True
        context = self._debug_action_context(action)
        key = (context["source_preset_id"], context["action_id"])
        wait_for_existing_pause = False
        with self.debug_lock:
            if not self.run_event.is_set():
                wait_for_existing_pause = True
            else:
                reason = ""
                if self.debug_pause_next:
                    reason = "step"
                elif context["action_id"] and key in snapshot["breakpoints"]:
                    reason = "breakpoint"
                if not reason:
                    return True
                self.debug_pause_next = False
                if not self.pause():
                    return False
                self.debug_pause_info = {
                    **context,
                    "reason": reason,
                    "action": self.describe(action),
                }
                self._emit_action_activity(
                    action,
                    phase="debug_pause",
                    debug_reason=reason,
                )
        if wait_for_existing_pause:
            if not self.wait_ready():
                return False
            # Another parallel branch owned the previous pause. Re-evaluate
            # this action after it resumes so single-step and its own
            # breakpoint cannot be skipped by the earlier branch.
            return self._debug_before_action(action)
        return self.wait_ready()

    def debug_pause_next_action(self):
        if self.stop_event.is_set() or not self.run_event.is_set():
            return False
        with self.debug_lock:
            self.debug_pause_next = True
        return True

    def cancel_pending_debug_pause(self):
        with self.debug_lock:
            self.debug_pause_next = False

    def debug_step(self):
        with self.debug_lock:
            if self.run_event.is_set() or not self.debug_pause_info:
                return False
            self.debug_pause_info = None
            self.debug_pause_next = True
        return self.resume(preserve_debug=True)

    def debug_continue(self):
        with self.debug_lock:
            if self.run_event.is_set() or not self.debug_pause_info:
                return False
            self.debug_pause_info = None
            self.debug_pause_next = False
        return self.resume(preserve_debug=True)

    def _resolve_loop_targets(self, action):
        targets = []
        seen = set()
        for action_id in action.get("target_action_ids", []) or []:
            action_id = str(action_id)
            target = self.action_index.get(action_id)
            if (
                target is not None
                and target.get("type") != LOOP_ACTION_TYPE
                and action_id not in seen
            ):
                seen.add(action_id)
                targets.append(target)
        return targets

    def _matching_loop(self, actions, index, controls_by_start=None):
        if not 0 <= index < len(actions):
            return None, []
        first_id = str(actions[index].get("action_id") or "")
        if not first_id:
            return None, []
        controls_by_start = controls_by_start or self.loop_controls_by_start
        for loop_action in controls_by_start.get(first_id, []):
            target_ids = [
                str(value)
                for value in loop_action.get("target_action_ids", []) or []
            ]
            if not target_ids or index + len(target_ids) > len(actions):
                continue
            segment = actions[index:index + len(target_ids)]
            segment_ids = [str(item.get("action_id") or "") for item in segment]
            if segment_ids == target_ids:
                return loop_action, segment
        return None, []

    def _run_action_sequence(
        self, actions, speed, timeline_mode="sequential",
        local_stop=None, progress_callback=None,
    ):
        local_stop = local_stop or self.stop_event
        raw_actions = list(actions or [])
        local_controls = {}
        for control in raw_actions:
            if control.get("type") != LOOP_ACTION_TYPE:
                continue
            target_ids = [
                str(value) for value in control.get("target_action_ids", []) or []
                if value
            ]
            if target_ids:
                local_controls.setdefault(target_ids[0], []).append(control)
        actions = [
            action for action in raw_actions
            if action.get("type") != LOOP_ACTION_TYPE
        ]
        if timeline_mode == "parallel":
            workers = []
            results = []
            result_lock = threading.Lock()
            launch_count = [0]

            def launch(function, name):
                launch_count[0] += 1
                self._launch_parallel(
                    function, name, workers, results, result_lock
                )

            index = 0
            while index < len(actions):
                if local_stop.is_set() or self.stop_event.is_set() or not self.wait_ready():
                    return False
                loop_action, segment = self._matching_loop(
                    actions, index, local_controls
                )
                if loop_action is not None:
                    if progress_callback:
                        progress_callback(index, loop_action, len(actions), {"loop_segment_total": len(segment)})
                    launch(
                        lambda a=loop_action, s=list(segment), i=index:
                        self._run_loop_sequence(
                            a, speed, local_stop, targets=s,
                            timeline_mode_override="parallel",
                            progress_callback=progress_callback,
                            outer_index=i,
                            outer_step_total=len(actions),
                        ),
                        "MacroCanvas-ReferencedLoop",
                    )
                    index += len(segment)
                    continue
                action = actions[index]
                if progress_callback:
                    progress_callback(index, action, len(actions))
                if action.get("type") == "等待":
                    if not self.run_action_group(action, speed):
                        return False
                else:
                    launch(
                        lambda a=action: self.run_action_group(a, speed),
                        "MacroCanvas-ParallelActionGroup",
                    )
                index += 1

            while any(thread.is_alive() for thread in workers):
                if local_stop.wait(0.02) or self.stop_event.is_set():
                    return False
                if not self.is_active():
                    self.stop_event.set()
                    return False
            for thread in workers:
                thread.join(timeout=0.25)
            return len(results) == launch_count[0] and all(results)

        index = 0
        while index < len(actions):
            if local_stop.is_set() or self.stop_event.is_set() or not self.wait_ready():
                return False
            loop_action, segment = self._matching_loop(
                actions, index, local_controls
            )
            if loop_action is not None:
                if progress_callback:
                    progress_callback(index, loop_action, len(actions), {"loop_segment_total": len(segment)})
                if not self._run_loop_sequence(
                    loop_action, speed, local_stop, targets=segment,
                    timeline_mode_override="sequential",
                    progress_callback=progress_callback,
                    outer_index=index,
                    outer_step_total=len(actions),
                ):
                    return False
                index += len(segment)
                continue
            action = actions[index]
            if progress_callback:
                progress_callback(index, action, len(actions))
            if not self.run_action_group(action, speed):
                return False
            index += 1
        return True

    def start(self):
        self.thread.start()

    def stop(self):
        if not self.finish_reason:
            self.finish_reason = "stopped"
        self.stop_event.set()
        self.run_event.set()
        with self.debug_lock:
            self.debug_pause_info = None
            self.debug_pause_next = False
        # Wait until an in-flight Press/Tap has either completed its ownership
        # bookkeeping or been rejected by the new stop state.
        with self.output_state_lock:
            pass
        with self.clock_lock:
            self.paused_actions.clear()
        return True

    def pause(self):
        with self.debug_lock:
            self.debug_pause_info = None
            self.debug_pause_next = False
        with self.clock_lock:
            if self.stop_event.is_set():
                return False
            if not self.run_event.is_set():
                return True
            self.run_event.clear()
            self.pause_started_at = time.perf_counter()
        with self.output_state_lock:
            with self.pressed_lock:
                held = [
                    {
                        "action": dict(entry.get("action") or {}),
                        "count": max(1, int(entry.get("count", 1))),
                    }
                    for entry in self.pressed.values()
                ]
            released = self.release_all()
            with self.clock_lock:
                self.paused_actions = held if released else []
            if not released:
                self.stop_event.set()
                return False
            return True

    def resume(self, preserve_debug=False):
        if not preserve_debug:
            with self.debug_lock:
                self.debug_pause_info = None
                self.debug_pause_next = False
        with self.output_state_lock:
            with self.clock_lock:
                if self.run_event.is_set() or self.stop_event.is_set():
                    return False
                actions = list(self.paused_actions)
            restored = True
            for entry in actions:
                action = dict(entry.get("action") or {})
                count = max(1, int(entry.get("count", 1)))
                if not self._restore_press_count(action, count):
                    restored = False
                    break
            if not restored:
                # A partial restore is more dangerous than leaving the task
                # paused. Release anything already restored and terminate the
                # task so the UI cannot report a successful resume.
                self.release_all()
                self.stop_event.set()
                with self.clock_lock:
                    self.paused_actions = []
                return False
            with self.clock_lock:
                if self.pause_started_at:
                    self.paused_seconds += max(
                        0.0, time.perf_counter() - self.pause_started_at
                    )
                    self.pause_started_at = 0.0
                self.paused_actions = []
            self.run_event.set()
            return True

    def active_elapsed(self):
        with self.clock_lock:
            paused = self.paused_seconds
            if self.pause_started_at:
                paused += max(0.0, time.perf_counter() - self.pause_started_at)
            started = self.started_at
        return max(0.0, time.perf_counter() - started - paused) if started else 0.0

    def wait_ready(self):
        while not self.stop_event.is_set():
            deadline = float(getattr(self, "deadline", 0.0) or 0.0)
            if deadline and self.active_elapsed() >= deadline:
                self.finish_reason = "runtime_limit"
                self.stop_event.set()
                return False
            if not self.is_active():
                self.finish_reason = "backend_inactive"
                self.stop_event.set()
                return False
            if self.run_event.wait(0.05):
                if self.stop_event.is_set():
                    return False
                if not self.is_active():
                    self.finish_reason = "backend_inactive"
                    self.stop_event.set()
                    return False
                return True
        return False

    @staticmethod
    def jittered_milliseconds(base_ms, jitter_ms=0, minimum=0):
        base = int(base_ms or 0)
        jitter = max(0, int(jitter_ms or 0))
        if jitter:
            base += random.randint(-jitter, jitter)
        return max(int(minimum), base)

    def sleep(self, milliseconds):
        remaining = max(0, milliseconds) / 1000
        while remaining > 0:
            if not self.wait_ready():
                return False
            started = time.perf_counter()
            if self.stop_event.wait(min(0.03, remaining)):
                return False
            if self.run_event.is_set():
                remaining -= max(0.0, time.perf_counter() - started)
        return not self.stop_event.is_set()

    def _launch_parallel(self, function, name, workers, results, result_lock):
        """Bound thread growth; run inline when all safe worker slots are busy."""
        if not self.parallel_slots.acquire(blocking=False):
            try:
                ok = function()
            except Exception as error:
                self._record_parallel_exception(name, error)
                ok = False
            with result_lock:
                results.append(bool(ok))
            return

        def worker():
            current = threading.current_thread()
            try:
                try:
                    ok = function()
                except Exception as error:
                    self._record_parallel_exception(name, error)
                    ok = False
                with result_lock:
                    results.append(bool(ok))
            finally:
                self.parallel_slots.release()
                with self.worker_lock:
                    self.worker_threads.discard(current)
                self.signals.state_changed.emit()
                if not self.has_live_threads(exclude_current=True):
                    self._emit_task_finished_once()

        thread = threading.Thread(target=worker, name=name, daemon=False)
        with self.worker_lock:
            self.worker_threads.add(thread)
        workers.append(thread)
        thread.start()

    def _emit_task_finished_once(self):
        with self._finish_signal_lock:
            if self._finish_signal_emitted:
                return False
            self._finish_signal_emitted = True
        self.signals.task_finished.emit(self.preset["id"])
        return True

    def _record_parallel_exception(self, worker_name, error):
        error_type = type(error).__name__
        error_text = str(error).strip() or error_type
        detail = f"并行动作异常（{worker_name}）：{error_type}: {error_text}"
        with self.parallel_error_lock:
            self.parallel_errors.append({
                "worker": str(worker_name),
                "type": error_type,
                "message": error_text,
            })
            if len(self.parallel_errors) > 20:
                del self.parallel_errors[:-20]
        self.signals.action_activity.emit({
            "id": self.preset.get("id", ""),
            "name": self.preset.get("name", "预设"),
            "action": detail[:500],
            "phase": "error",
            "error_type": "parallel_exception",
        })

    @staticmethod
    def _is_mouse_button_action(action):
        return (
            isinstance(action, dict)
            and action.get("type") == "鼠标点击"
            and action.get("target") in MOUSE_NAMES
        )

    @staticmethod
    def _press_key(action):
        return (
            action.get("_vkey")
            or f"{action.get('type')}|{action.get('modifiers', '无')}|{action.get('target')}"
        )

    @staticmethod
    def _release_context_still_safe(entry):
        if not isinstance(entry, dict):
            return True
        action = entry.get("action") or {}
        if not MacroTask._is_mouse_button_action(action):
            return True
        return foreground_window_identity_matches(entry.get("press_window"))


    def live_threads(self, exclude_current=False):
        """Return every task-owned thread that is still alive."""
        current = threading.current_thread() if exclude_current else None
        threads = []
        main_thread = self.thread
        if main_thread.is_alive() and main_thread is not current:
            threads.append(main_thread)
        with self.worker_lock:
            workers = list(self.worker_threads)
        for thread in workers:
            if thread.is_alive() and thread is not current:
                threads.append(thread)
        return threads

    def has_live_threads(self, exclude_current=False):
        return bool(self.live_threads(exclude_current=exclude_current))

    def wait_for_exit(self, timeout=2.5):
        """Wait for the main task and all parallel workers within one deadline."""
        deadline = time.perf_counter() + max(0.0, float(timeout))
        current = threading.current_thread()
        while True:
            threads = [
                thread for thread in self.live_threads()
                if thread is not current
            ]
            if not threads:
                return True
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return False
            # Join in short slices so workers that spawn/finish while the main
            # thread unwinds are also observed before the shared deadline.
            threads[0].join(timeout=min(0.1, remaining))

    def thread_details(self):
        return [
            {"name": thread.name, "ident": thread.ident}
            for thread in self.live_threads()
        ]

    def _profile_output_matches(self):
        required_profile = str(self.preset.get("_required_profile_id") or "")
        return bool(
            not required_profile
            or self.profile_active is None
            or self.profile_active(required_profile)
        )

    def _wait_new_output_ready(self):
        """Wait without holding the output barrier until a new Down is allowed."""
        required_profile = str(self.preset.get("_required_profile_id") or "")
        while not self.stop_event.is_set():
            if not self.wait_ready():
                return False
            if (
                not required_profile
                or self.profile_active is None
                or self.profile_active(required_profile)
            ):
                return True
            # A foreground overlay or Alt-Tab can briefly make the bound process
            # stop matching before the UI-side stability window has decided
            # whether this is a real profile change.  Block the pending output
            # instead of terminating the task immediately.  The profile watcher
            # will pause the task within one polling interval; a stable mismatch
            # then stops it through the normal guarded transition path.
            if self.stop_event.wait(0.02):
                return False
        return False

    def _dispatch_output(self, action, phase):
        # Cleanup Release must still be attempted after the business engine has
        # begun stopping. New Press/Tap calls are checked by the task-local
        # barrier before reaching this low-level dispatch method.
        if phase != "Release" and not self.is_active():
            return False
        if self.send_output:
            return bool(self.send_output(
                action, phase, wait=True, timeout=1.0
            ))
        virtual_key = action.get("_vkey")
        if not virtual_key:
            return False
        if self.expect_output:
            self.expect_output(action, phase)
        return self.engine.queue_virtual_key_action(
            virtual_key, phase, wait=True, timeout=1.0
        )

    def _send(self, action, phase):
        if phase == "Release":
            return self._dispatch_output(action, phase)
        if not self._wait_new_output_ready():
            return False
        output_state_lock = getattr(self, "output_state_lock", None)
        if output_state_lock is None:
            output_state_lock = threading.RLock()
            self.output_state_lock = output_state_lock
        with output_state_lock:
            if (
                self.stop_event.is_set()
                or not self.run_event.is_set()
                or not self.is_active()
                or not self._profile_output_matches()
            ):
                return False
            return self._dispatch_output(action, phase)

    def press(self, action):
        press_key = self._press_key(action)
        if not self._wait_new_output_ready():
            return False
        # Keep the check, output and state update in one lock scope.  Without
        # this, two parallel branches can both observe an empty slot and emit
        # duplicate physical Press events.
        with self.output_state_lock:
            if (
                self.stop_event.is_set()
                or not self.run_event.is_set()
                or not self.is_active()
                or not self._profile_output_matches()
            ):
                return False
            with self.pressed_lock:
                entry = self.pressed.get(press_key)
                if entry is not None:
                    entry["count"] = max(1, int(entry.get("count", 1))) + 1
                    return True
                press_window_before = (
                    foreground_window_identity()
                    if self._is_mouse_button_action(action)
                    else None
                )
                if not self._dispatch_output(action, "Press"):
                    return False
                press_window = press_window_before
                if press_window_before is not None:
                    press_window_after = foreground_window_identity()
                    before_hwnd = int(press_window_before.get("hwnd") or 0)
                    after_hwnd = int(press_window_after.get("hwnd") or 0)
                    before_pid = int(press_window_before.get("pid") or 0)
                    after_pid = int(press_window_after.get("pid") or 0)
                    if (
                        (
                            (before_hwnd or after_hwnd)
                            and before_hwnd != after_hwnd
                        )
                        or (
                            not before_hwnd and not after_hwnd
                            and before_pid and after_pid and before_pid != after_pid
                        )
                    ):
                        press_window = {
                            "_unstable": True,
                            "before": press_window_before,
                            "after": press_window_after,
                        }
                    else:
                        press_window = press_window_after
                    press_window["_mouse_origin"] = True
                self.pressed[press_key] = {
                    "action": dict(action),
                    "count": 1,
                    "press_window": press_window,
                }
                return True

    def _restore_press_count(self, action, count):
        """Restore one physical press with its pre-pause logical holder count."""
        press_key = self._press_key(action)
        with self.pressed_lock:
            if press_key in self.pressed:
                return False
            if (
                self.stop_event.is_set()
                or not self.is_active()
                or not self._profile_output_matches()
                or not self._dispatch_output(action, "Press")
            ):
                return False
            self.pressed[press_key] = {
                "action": dict(action),
                "count": max(1, int(count or 1)),
                "press_window": (
                    foreground_window_identity()
                    if self._is_mouse_button_action(action)
                    else None
                ),
            }
            return True

    def release(self, action, attempts=3):
        press_key = self._press_key(action)
        release_failed_action = None
        with self.output_state_lock:
            with self.pressed_lock:
                entry = self.pressed.get(press_key)
                if entry is None:
                    # Pause physically releases every held output but preserves
                    # logical holder counts for resume. If an action reaches its
                    # own Release while paused, consume that saved holder here so
                    # resume cannot re-press an action that has already ended.
                    with self.clock_lock:
                        for index, paused_entry in enumerate(self.paused_actions):
                            paused_action = dict(paused_entry.get("action") or {})
                            if self._press_key(paused_action) != press_key:
                                continue
                            count = max(1, int(paused_entry.get("count", 1)))
                            if count > 1:
                                paused_entry["count"] = count - 1
                            else:
                                self.paused_actions.pop(index)
                            return True
                    return True
                count = max(1, int(entry.get("count", 1)))
                if count > 1:
                    entry["count"] = count - 1
                    return True
                stored_action = dict(entry.get("action") or action)
                if not self._release_context_still_safe(entry):
                    quarantined = bool(
                        self.quarantine_release
                        and self.quarantine_release(
                            stored_action, entry.get("press_window")
                        )
                    )
                    if quarantined:
                        self.pressed.pop(press_key, None)
                    return quarantined
                attempt_count = max(1, int(attempts))
                # Keep the logical-holder lock across the physical Release.
                # Otherwise a parallel branch can acquire the same output between
                # the send and the bookkeeping update, causing a premature Release.
                for attempt in range(attempt_count):
                    if self._send(stored_action, "Release"):
                        self.pressed.pop(press_key, None)
                        if not self.pressed:
                            self.release_cleanup_failed = False
                        return True
                    if attempt + 1 < attempt_count:
                        time.sleep(0.02)
                release_failed_action = stored_action

        self._emit_action_activity(
            release_failed_action, phase="error", extra="松开失败，任务已停止"
        )
        self.release_cleanup_failed = True
        self.stop_event.set()
        return False

    def release_all(self):
        with self.output_state_lock:
            for _attempt in range(3):
                with self.pressed_lock:
                    pending = list(self.pressed.items())
                if not pending:
                    break
                for virtual_key, _snapshot in reversed(pending):
                    with self.pressed_lock:
                        entry = self.pressed.get(virtual_key)
                        if entry is None:
                            continue
                        action = dict(entry.get("action") or {})
                        if not self._release_context_still_safe(entry):
                            quarantined = bool(
                                self.quarantine_release
                                and self.quarantine_release(
                                    action, entry.get("press_window")
                                )
                            )
                            if quarantined:
                                self.pressed.pop(virtual_key, None)
                            continue
                        if self._send(action, "Release"):
                            self.pressed.pop(virtual_key, None)
                with self.pressed_lock:
                    still_pressed = bool(self.pressed)
                if still_pressed:
                    time.sleep(0.03)
            with self.pressed_lock:
                released = not bool(self.pressed)
            self.release_cleanup_failed = not released
            return released

    def force_release(self):
        with self.output_state_lock:
            with self.clock_lock:
                self.paused_actions.clear()
            return self.release_all()

    def _emit_action_activity(
        self, action, phase="start", extra="", debug_reason="",
    ):
        try:
            description = self.describe(action)
            if extra:
                description = f"{description} · {extra}"
            context = self._debug_action_context(action)
            self.signals.action_activity.emit({
                "id": self.preset.get("id", ""),
                "name": self.preset.get("name", "预设"),
                "action": description,
                "phase": phase,
                "debug_reason": str(debug_reason or ""),
                **context,
            })
        except Exception:
            pass

    def run_action(self, action, speed):
        self._emit_action_activity(action)
        kind = action.get("type")
        if kind in CONTROL_ACTION_TYPES:
            return False
        scale = 100 / max(10, speed)
        if kind == "等待":
            duration = self.jittered_milliseconds(
                action.get("wait_ms", 1), action.get("jitter_ms", 0), 1
            )
            return self.sleep(int(duration * scale))
        if kind == "鼠标移动":
            return self._send(action, "Tap")
        if kind == "鼠标滚轮":
            steps = max(1, int(action.get("steps", 1)))
            for step in range(steps):
                if not self._send(action, "Tap"):
                    return False
                if step + 1 < steps and not self.sleep(2):
                    return False
            return True

        if not self.press(action):
            return False
        duration = self.jittered_milliseconds(
            action.get("hold_ms", 100), action.get("jitter_ms", 0), 1
        )
        ok = self.sleep(int(duration * scale))
        released = self.release(action)
        return bool(ok and released)

    def _sleep_with_local_stop(self, milliseconds, local_stop):
        remaining = max(0, milliseconds) / 1000
        while remaining > 0:
            if local_stop.is_set() or not self.wait_ready():
                return False
            started = time.perf_counter()
            if local_stop.wait(min(0.03, remaining)):
                return False
            if self.run_event.is_set():
                remaining -= max(0.0, time.perf_counter() - started)
        return not local_stop.is_set() and not self.stop_event.is_set()

    def _run_loop_child_timeline(self, children, speed, local_stop):
        workers = []
        results = []
        result_lock = threading.Lock()
        launch_count = [0]

        def launch(child):
            launch_count[0] += 1
            self._launch_parallel(
                lambda: self.run_action_group(child, speed),
                "MacroCanvas-LoopParallelAction",
                workers, results, result_lock,
            )

        for child in children:
            if local_stop.is_set() or self.stop_event.is_set() or not self.wait_ready():
                return False
            if child.get("type") == "等待":
                if not self.run_action_group(child, speed):
                    return False
            else:
                launch(child)

        while any(thread.is_alive() for thread in workers):
            if local_stop.wait(0.02) or self.stop_event.is_set():
                return False
            if not self.is_active():
                self.stop_event.set()
                return False
        for thread in workers:
            thread.join(timeout=0.25)
        return len(results) == launch_count[0] and all(results)

    def _run_loop_sequence(
        self, action, parent_speed, local_stop, mode_override=None,
        targets=None, timeline_mode_override=None, progress_callback=None,
        outer_index=0, outer_step_total=0,
    ):
        # Always resolve loop contents through current action IDs. Loop cards do
        # not own parameter snapshots, so edits to the original actions are the
        # single source of truth for every subsequent loop execution.
        dynamic_targets = targets is None
        resolved_targets = self._resolve_loop_targets(action)
        targets = (
            list(targets) if targets is not None else resolved_targets
        )
        if not targets:
            return True
        if not self._debug_before_action(action):
            return False
        self._emit_action_activity(action)
        mode = mode_override or action.get("execution_mode", "执行次数")
        cycles = (
            max(1, int(action.get("loop_count", 2)))
            if mode in ("执行次数", "固定次数", "执行一次")
            else 2_147_483_647
        )

        local_speed_setting = max(10, min(500, int(action.get("speed_percent", 100))))
        effective_speed = max(10, min(500, round(parent_speed * local_speed_setting / 100)))
        interval = max(0, int(action.get("loop_interval_ms", 0)))
        interval_jitter = max(0, int(action.get("loop_interval_jitter_ms", 0)))
        max_seconds = (
            max(0, int(action.get("max_runtime_s", 0)))
            if mode == "无限循环" else 0
        )
        started = self.active_elapsed()

        for cycle_index in range(cycles):
            if local_stop.is_set() or self.stop_event.is_set() or not self.wait_ready():
                return False
            latest_targets = self._resolve_loop_targets(action)
            if dynamic_targets and latest_targets:
                targets = latest_targets
            if max_seconds and self.active_elapsed() - started >= max_seconds:
                break
            timeline_mode = (
                timeline_mode_override
                or action.get("timeline_mode", "sequential")
            )
            if timeline_mode == "parallel":
                if progress_callback:
                    progress_callback(
                        outer_index, action, outer_step_total or len(targets),
                        {
                            "loop_control": self.describe(action),
                            "loop_cycle": cycle_index + 1,
                            "loop_cycle_total": cycles if cycles < 2_000_000_000 else 0,
                            "loop_inner_step": 1,
                            "loop_inner_total": len(targets),
                            "loop_inner_parallel": True,
                        },
                    )
                if not self._run_loop_child_timeline(
                    targets, effective_speed, local_stop
                ):
                    return False
            else:
                for target_index, target in enumerate(targets):
                    if local_stop.is_set() or self.stop_event.is_set():
                        return False
                    if progress_callback:
                        progress_callback(
                            outer_index, target, outer_step_total or len(targets),
                            {
                                "loop_control": self.describe(action),
                                "loop_cycle": cycle_index + 1,
                                "loop_cycle_total": cycles if cycles < 2_000_000_000 else 0,
                                "loop_inner_step": target_index + 1,
                                "loop_inner_total": len(targets),
                            },
                        )
                    if not self.run_action_group(target, effective_speed):
                        return False
            if cycle_index + 1 >= cycles:
                break
            current_interval = self.jittered_milliseconds(
                interval, interval_jitter, 0
            )
            scaled_interval = current_interval * 100 / max(10, effective_speed)
            if current_interval:
                if not self._sleep_with_local_stop(scaled_interval, local_stop):
                    return False
            elif not self._sleep_with_local_stop(1, local_stop):
                return False
        return not self.stop_event.is_set() and not local_stop.is_set()

    def run_loop_action(self, action, parent_speed):
        # Loop cards are references to existing actions. They never own, move, or
        # delete the selected actions; they only replay the referenced range when
        # the card is reached at the end of the preset timeline.
        return self._run_loop_sequence(
            action, parent_speed, threading.Event()
        )

    def _condition_satisfied(self, action):
        callback = self.condition_state
        if not callable(callback):
            return False
        try:
            return bool(callback(
                action.get("condition_input", ""),
                action.get("condition_state", "按住时"),
            ))
        except Exception:
            return False

    def _wait_for_trigger_release(self):
        """Fence one-shot output until its physical trigger chord is released."""
        inputs = tuple(dict.fromkeys(
            str(value) for value in (
                self.preset.get("_trigger_release_inputs", []) or []
            ) if str(value)
        ))
        if not inputs or not callable(self.condition_state):
            return True
        while not self.stop_event.is_set():
            if not self.wait_ready():
                return False
            if not any(self._condition_satisfied({
                "condition_input": input_name,
                "condition_state": "按住时",
            }) for input_name in inputs):
                return True
            if self.stop_event.wait(0.005):
                return False
        return False

    @staticmethod
    def condition_branch_actions(action):
        """Return true/false branch actions, accepting legacy true-only data."""
        children = list(action.get("children", []) or [])
        branches = {
            child.get("type"): child.get("children", []) or []
            for child in children
            if child.get("type") in CONDITION_BRANCH_TYPES
        }
        if branches:
            return (
                list(branches.get(CONDITION_TRUE_BRANCH_TYPE, [])),
                list(branches.get(CONDITION_ELSE_BRANCH_TYPE, [])),
            )
        return children, []

    def _wait_for_condition(self, action):
        timeout_ms = max(0, int(action.get("timeout_ms", 0)))
        poll_ms = max(10, min(1_000, int(action.get("poll_ms", 20))))
        started = self.active_elapsed()
        while not self.stop_event.is_set():
            if not self.wait_ready() or not self.is_active():
                return False
            if self._condition_satisfied(action):
                return True
            if timeout_ms and (self.active_elapsed() - started) * 1000 >= timeout_ms:
                self.last_failure_reason = "condition_timeout"
                self.finish_reason = "condition_timeout"
                return False
            if self.stop_event.wait(poll_ms / 1000):
                return False
        return False

    @staticmethod
    def _mark_submacro_call_stack(
        actions, stack, preset_id="", preset_name="", path=None,
        parameters=None,
    ):
        copied = clone_action_tree(actions)
        pending = list(copied)
        while pending:
            action = pending.pop()
            action["_debug_preset_id"] = str(preset_id or "")
            action["_debug_preset_name"] = str(preset_name or "预设")
            action["_debug_path"] = list(path or [])
            action["_debug_parameters"] = dict(parameters or {})
            if action.get("type") == SUBMACRO_ACTION_TYPE:
                action["_call_stack"] = tuple(stack)
            pending.extend(action.get("children", []) or [])
        return copied

    def _run_submacro(self, action, parent_speed):
        library = self.preset.get("_preset_library", {})
        target_id = str(action.get("preset_id") or "")
        target = library.get(target_id) if isinstance(library, dict) else None
        stack = tuple(action.get("_call_stack") or (self.preset.get("id"),))
        if not target or not target_id or target_id in stack or len(stack) >= 16:
            self.last_failure_reason = "submacro_unavailable"
            self.finish_reason = "submacro_unavailable"
            return False
        repeat_count = max(1, min(100_000, int(action.get("repeat_count", 1))))
        local_speed = max(10, min(500, int(action.get("speed_percent", 100))))
        effective_speed = max(
            10, min(500, round(parent_speed * local_speed / 100))
        )
        parameter_values = (
            merged_parameter_values(target, action.get("parameter_values", {}))
            if target.get("parameters") else {}
        )
        resolved_actions = (
            resolve_action_parameters(
                target.get("actions", []), target,
                action.get("parameter_values", {}),
            )
            if target.get("parameters")
            else target.get("actions", [])
        )
        called_actions = self._mark_submacro_call_stack(
            resolved_actions,
            stack + (target_id,),
            preset_id=target_id,
            preset_name=str(target.get("name") or target_id),
            path=(
                list(action.get("_debug_path", []) or [])
                or [str(self.preset.get("name") or "预设")]
            ) + [str(target.get("name") or target_id)],
            parameters=parameter_values,
        )
        self._emit_action_activity(action, extra=target.get("name", target_id))
        for _index in range(repeat_count):
            if not self._run_action_sequence(
                called_actions, effective_speed, timeline_mode="sequential",
                local_stop=self.stop_event,
            ):
                return False
        return True

    def run_action_group(self, root_action, speed):
        """Run one action and its child timeline, applying referenced loops in place."""
        if root_action.get("type") == LOOP_ACTION_TYPE:
            # Loop cards are control metadata and are never normal action nodes.
            return True
        if not self._debug_before_action(root_action):
            return False
        kind = root_action.get("type")
        if kind == CONDITION_ACTION_TYPE:
            self._emit_action_activity(root_action)
            true_actions, else_actions = self.condition_branch_actions(
                root_action
            )
            selected_actions = (
                true_actions if self._condition_satisfied(root_action)
                else else_actions
            )
            return self._run_action_sequence(
                selected_actions, speed,
                timeline_mode="sequential", local_stop=self.stop_event,
            )
        if kind in CONDITION_BRANCH_TYPES:
            return self._run_action_sequence(
                root_action.get("children", []) or [], speed,
                timeline_mode="sequential", local_stop=self.stop_event,
            )
        if kind == WAIT_CONDITION_ACTION_TYPE:
            self._emit_action_activity(root_action)
            if not self._wait_for_condition(root_action):
                if self.last_failure_reason and not self.stop_event.is_set():
                    self._emit_action_activity(
                        root_action,
                        phase="error",
                        extra="条件等待超时，当前宏结束",
                        debug_reason=self.last_failure_reason,
                    )
                return False
            return self._run_action_sequence(
                root_action.get("children", []) or [], speed,
                timeline_mode="sequential", local_stop=self.stop_event,
            )
        if kind == SUBMACRO_ACTION_TYPE:
            if not self._run_submacro(root_action, speed):
                if (
                    self.last_failure_reason == "submacro_unavailable"
                    and not self.stop_event.is_set()
                ):
                    self._emit_action_activity(
                        root_action,
                        phase="error",
                        extra="子宏无法继续执行",
                        debug_reason=self.last_failure_reason,
                    )
                return False
            return self._run_action_sequence(
                root_action.get("children", []) or [], speed,
                timeline_mode="sequential", local_stop=self.stop_event,
            )

        workers = []
        results = []
        result_lock = threading.Lock()
        launch_count = [0]

        def launch(function, name):
            launch_count[0] += 1
            self._launch_parallel(
                function, name, workers, results, result_lock
            )

        if root_action.get("type") == "等待":
            if not self.run_action(root_action, speed):
                return False
        else:
            launch(
                lambda a=root_action: self.run_action(a, speed),
                "MacroCanvas-ParallelAction",
            )

        children_ok = self._run_action_sequence(
            root_action.get("children", []) or [],
            speed, timeline_mode="parallel", local_stop=self.stop_event,
        )

        while any(thread.is_alive() for thread in workers):
            if self.stop_event.wait(0.02):
                break
            if not self.is_active():
                self.stop_event.set()
                break
        for thread in workers:
            thread.join(timeout=0.25)
        return (
            children_ok
            and not self.stop_event.is_set()
            and len(results) == launch_count[0]
            and all(results)
        )

    @staticmethod
    def describe(action):
        kind = action.get("type", "动作")
        if kind == LOOP_ACTION_TYPE:
            mode = action.get("execution_mode", "执行次数")
            if mode == "执行次数":
                detail = f"{max(1, int(action.get('loop_count', 2)))} 次"
            else:
                detail = "无限循环"
            return f"{action.get('name', '循环项目')}（{detail}）"
        if kind == CONDITION_ACTION_TYPE:
            return (
                f"条件分支：{action.get('condition_input', '')} "
                f"{action.get('condition_state', '按住时')}"
            )
        if kind == CONDITION_TRUE_BRANCH_TYPE:
            return "条件成立分支"
        if kind == CONDITION_ELSE_BRANCH_TYPE:
            return "否则分支"
        if kind == WAIT_CONDITION_ACTION_TYPE:
            timeout = max(0, int(action.get("timeout_ms", 0)))
            suffix = f"，超时 {timeout}ms" if timeout else "，一直等待"
            return (
                f"等待 {action.get('condition_input', '')} "
                f"{action.get('condition_state', '按住时')}{suffix}"
            )
        if kind == SUBMACRO_ACTION_TYPE:
            return (
                f"调用子宏 {action.get('preset_id', '')} ×"
                f"{max(1, int(action.get('repeat_count', 1)))}"
            )
        if kind == "等待":
            jitter = max(0, int(action.get("jitter_ms", 0)))
            suffix = f" ±{jitter}ms" if jitter else ""
            return f"等待 {action.get('wait_ms', 0)}ms{suffix}"
        if kind == "鼠标移动":
            return f"鼠标移动到 {action.get('target', '0,0')}"
        if kind == "鼠标滚轮":
            return (
                f"鼠标滚轮 {action.get('target', '向上')} "
                f"{max(1, int(action.get('steps', 1)))} 格"
            )
        hold = max(1, int(action.get("hold_ms", 100)))
        jitter = max(0, int(action.get("jitter_ms", 0)))
        suffix = f" ±{jitter}ms" if jitter else ""
        return f"{kind} {action.get('target', '')}（按住 {hold}ms{suffix}）"

    def run(self):
        actions = self.preset.get("actions", [])
        mode = self.preset.get("execution_mode", "执行一次")
        loops = int(self.preset.get("loop_count", 1)) if mode == "固定次数" else (
            1 if mode == "执行一次" else 2_147_483_647
        )
        speed = int(self.preset.get("speed_percent", 100))
        interval = int(self.preset.get("loop_interval_ms", 0))
        interval_jitter = int(self.preset.get("loop_interval_jitter_ms", 0))
        max_seconds = int(self.preset.get("max_runtime_s", 0))
        try:
            if not actions:
                self.finish_reason = "empty"
                return
            if not self.is_active():
                self.finish_reason = "backend_inactive"
                return
            if not self._wait_for_trigger_release():
                if not self.finish_reason:
                    self.finish_reason = "trigger_release_cancelled"
                return
            # Trigger-release waiting is input isolation, not macro runtime. Do
            # not charge it against finite runtime or action timing.
            self.started_at = time.perf_counter()
            self.deadline = float(max_seconds) if max_seconds else 0.0
            for loop_index in range(1, max(1, loops) + 1):
                if self.stop_event.is_set() or not self.is_active():
                    break
                if max_seconds and self.active_elapsed() >= max_seconds:
                    break
                ordinary_actions = [
                    action for action in actions
                    if action.get("type") != LOOP_ACTION_TYPE
                ]

                def emit_progress(step_index, action, step_total, meta=None):
                    meta = dict(meta or {})
                    action_text = self.describe(action)
                    if meta.get("loop_control"):
                        cycle_total = meta.get("loop_cycle_total") or "∞"
                        if meta.get("loop_inner_parallel"):
                            action_text = (
                                f"{meta['loop_control']} · 第 {meta.get('loop_cycle', 1)} / "
                                f"{cycle_total} 轮 · 并行引用动作 "
                                f"{meta.get('loop_inner_total', 0)} 个"
                            )
                        else:
                            action_text = (
                                f"{meta['loop_control']} · 第 {meta.get('loop_cycle', 1)} / "
                                f"{cycle_total} 轮 · 引用动作 "
                                f"{meta.get('loop_inner_step', 1)} / "
                                f"{meta.get('loop_inner_total', 1)}：{action_text}"
                            )
                    self.signals.progress.emit({
                        "id": self.preset["id"], "name": self.preset["name"],
                        "loop": loop_index,
                        "loop_total": loops if loops < 2_000_000_000 else 0,
                        "step": step_index + 1, "step_total": step_total,
                        "action": action_text,
                        "paused": not self.run_event.is_set(),
                        **meta,
                    })

                if not self._run_action_sequence(
                    ordinary_actions, speed, timeline_mode="sequential",
                    local_stop=self.stop_event, progress_callback=emit_progress,
                ):
                    if not self.finish_reason:
                        self.finish_reason = (
                            "stopped" if self.stop_event.is_set()
                            else self.last_failure_reason or "action_failed"
                        )
                    break
                if (
                    self.stop_event.is_set()
                    or not self.is_active()
                    or mode == "执行一次"
                ):
                    break
                current_interval = self.jittered_milliseconds(
                    interval, interval_jitter, 0
                )
                if current_interval and not self.sleep(
                    current_interval * 100 / max(10, speed)
                ):
                    break
                if not current_interval and not self.sleep(1):
                    break
        finally:
            if not self.finish_reason:
                self.finish_reason = (
                    "stopped" if self.stop_event.is_set() else "completed"
                )
            # Stop any branch that outlived the main sequence and give it a
            # bounded chance to leave backend calls before final release.
            self.stop_event.set()
            self.wait_for_exit(timeout=2.0)
            if not self.release_all():
                self.finish_reason = "release_failed"
            reason_labels = {
                "completed": "正常完成",
                "stopped": "已停止",
                "empty": "没有动作",
                "runtime_limit": "达到最长运行时间",
                "backend_inactive": "输入后端不可用",
                "trigger_release_cancelled": "等待触发键释放时结束",
                "condition_timeout": "等待条件超时",
                "submacro_unavailable": "子宏不可用或形成循环",
                "action_failed": "动作执行失败",
                "release_failed": "结束时按键释放失败",
            }
            self.signals.action_activity.emit({
                "id": self.preset.get("id", ""),
                "name": self.preset.get("name", "预设"),
                "action": f"宏结束：{reason_labels.get(self.finish_reason, self.finish_reason)}",
                "phase": "finished",
                "finish_reason": self.finish_reason,
                "source_preset_id": str(self.preset.get("id") or ""),
                "source_preset_name": str(self.preset.get("name") or "预设"),
                "action_id": "",
                "action_type": "宏结束",
                "path": [str(self.preset.get("name") or "预设")],
                "parameters": dict(self.root_debug_parameters),
            })
            self._emit_task_finished_once()


class MacroController:
    def __init__(
        self, engine, expect_output=None, send_output=None, is_active=None,
        quarantine_release=None, condition_state=None,
    ):
        self.engine = engine
        self.expect_output = expect_output
        self.send_output = send_output
        self.is_active = is_active or self.engine.is_running
        self.profile_active = None
        self.quarantine_release = quarantine_release
        self.condition_state = condition_state
        self.signals = MacroSignals()
        self.tasks = {}
        self.lock = threading.RLock()
        self.last_release_failures = []
        self.debug_enabled = False
        self.debug_breakpoints = set()
        self.debug_lock = threading.RLock()

    def _debug_snapshot(self):
        with self.debug_lock:
            return {
                "enabled": bool(self.debug_enabled),
                "breakpoints": set(self.debug_breakpoints),
            }

    def set_debug_enabled(self, enabled):
        with self.debug_lock:
            self.debug_enabled = bool(enabled)
        if not enabled:
            with self.lock:
                tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel_pending_debug_pause()
        self.signals.state_changed.emit()

    def set_debug_breakpoints(self, breakpoints):
        normalized = {
            (str(item[0]), str(item[1]))
            for item in breakpoints or set()
            if isinstance(item, (tuple, list)) and len(item) == 2
            and str(item[0]) and str(item[1])
        }
        with self.debug_lock:
            self.debug_breakpoints = normalized
        self.signals.state_changed.emit()

    def debug_pause_next_action(self, preset_id):
        with self.lock:
            task = self.tasks.get(preset_id)
        return bool(task and task.debug_pause_next_action())

    def debug_step(self, preset_id):
        with self.lock:
            task = self.tasks.get(preset_id)
        success = bool(task and task.debug_step())
        if success:
            self.signals.state_changed.emit()
        return success

    def debug_continue(self, preset_id):
        with self.lock:
            task = self.tasks.get(preset_id)
        success = bool(task and task.debug_continue())
        if success:
            self.signals.state_changed.emit()
        return success

    def _remember_release_failure_locked(self, preset_id):
        preset_id = str(preset_id or "")
        if not preset_id:
            return
        failures = list(getattr(self, "last_release_failures", []) or [])
        if preset_id not in failures:
            failures.append(preset_id)
        self.last_release_failures = failures

    def start(self, preset):
        if not self.is_active():
            return False
        with self.lock:
            preset_id = preset["id"]
            if preset_id in self.tasks:
                existing = self.tasks[preset_id]
                if existing.has_live_threads():
                    return False
                if getattr(existing, "release_cleanup_failed", False):
                    self._remember_release_failure_locked(preset_id)
                    self.signals.state_changed.emit()
                    return False
                self.tasks.pop(preset_id, None)
            if getattr(self, "last_release_failures", []):
                return False
            task = MacroTask(
                dict(preset), self.engine, self.signals,
                self.expect_output, self.send_output, self.is_active,
                self.profile_active, self.quarantine_release,
                self.condition_state,
                self._debug_snapshot,
            )
            self.tasks[preset_id] = task
            task.start()
        self.signals.state_changed.emit()
        return True

    def restart(self, preset, timeout=1.0):
        """Safely replace one live task with a fresh run of the same preset."""
        if not self.is_active():
            return False
        preset_id = str(preset.get("id") or "")
        if not preset_id:
            return False
        with self.lock:
            existing = self.tasks.get(preset_id)
        if existing is None or not existing.has_live_threads():
            return self.start(preset)

        existing.stop()
        # A condition branch can race with the retrigger edge. Block any new
        # output first, then immediately release output it may already own.
        released = bool(existing.force_release())
        exited = bool(existing.wait_for_exit(timeout=max(0.05, float(timeout))))
        if not released or not exited or existing.has_live_threads():
            if not released:
                with self.lock:
                    self._remember_release_failure_locked(preset_id)
            self.signals.state_changed.emit()
            return False
        if getattr(existing, "release_cleanup_failed", False):
            with self.lock:
                self._remember_release_failure_locked(preset_id)
            self.signals.state_changed.emit()
            return False

        with self.lock:
            if self.tasks.get(preset_id) is existing:
                self.tasks.pop(preset_id, None)
        return self.start(preset)

    def finish(self, preset_id):
        finished_task = None
        with self.lock:
            task = self.tasks.get(preset_id)
            if task is not None and not task.has_live_threads():
                if getattr(task, "release_cleanup_failed", False):
                    self._remember_release_failure_locked(preset_id)
                finished_task = self.tasks.pop(preset_id, None)
        self.signals.state_changed.emit()
        return finished_task or task

    def stop(self, preset_id, release_held=False):
        with self.lock:
            task = self.tasks.get(preset_id)
        if not task:
            return False
        stopped = bool(task.stop())
        if stopped and release_held:
            # Toggle/hold macro shutdown is user-visible as “close this mapping”.
            # Do not wait only for the worker thread's finally block to release
            # held Press actions: release the task-owned outputs immediately, then
            # let the normal thread-exit cleanup run as a second no-op/backup pass.
            task.force_release()
        return stopped

    def pause(self, preset_id):
        with self.lock:
            task = self.tasks.get(preset_id)
        if task:
            success = bool(task.pause())
            self.signals.state_changed.emit()
            return success
        return False

    def resume(self, preset_id):
        with self.lock:
            task = self.tasks.get(preset_id)
        if task:
            success = bool(task.resume())
            self.signals.state_changed.emit()
            return success
        return False

    def stop_all(self, timeout=2.5):
        """Stop every task and every task-owned worker within one deadline."""
        with self.lock:
            tasks = list(self.tasks.values())
        for task in tasks:
            task.stop()

        deadline = time.perf_counter() + max(0.1, float(timeout))
        for task in tasks:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            task.wait_for_exit(timeout=remaining)

        release_failures = []
        for task in tasks:
            preset_id = str(task.preset.get("id") or "")
            try:
                if task.force_release() is False:
                    release_failures.append(preset_id)
            except Exception:
                release_failures.append(preset_id)
        self.last_release_failures = list(dict.fromkeys(release_failures))

        with self.lock:
            stale_ids = [
                preset_id for preset_id, task in self.tasks.items()
                if not task.has_live_threads()
            ]
            for preset_id in stale_ids:
                self.tasks.pop(preset_id, None)
            remaining_ids = [
                preset_id for preset_id, task in self.tasks.items()
                if task.has_live_threads()
            ]
        if stale_ids or remaining_ids:
            self.signals.state_changed.emit()
        return remaining_ids

    def wait_for_all(self, timeout=5.0):
        """Continue waiting for previously stopped tasks without resetting them."""
        deadline = time.perf_counter() + max(0.0, float(timeout))
        with self.lock:
            tasks = list(self.tasks.values())
        for task in tasks:
            task.stop()
        for task in tasks:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            task.wait_for_exit(timeout=remaining)
        with self.lock:
            finished_items = [
                (preset_id, task) for preset_id, task in self.tasks.items()
                if not task.has_live_threads()
            ]
            finished = [preset_id for preset_id, _task in finished_items]
            for preset_id, task in finished_items:
                if getattr(task, "release_cleanup_failed", False):
                    self._remember_release_failure_locked(preset_id)
                self.tasks.pop(preset_id, None)
            remaining_ids = [
                preset_id for preset_id, task in self.tasks.items()
                if task.has_live_threads()
            ]
        if finished or remaining_ids:
            self.signals.state_changed.emit()
        return remaining_ids

    def remaining_task_details(self):
        with self.lock:
            items = list(self.tasks.items())
        return {
            preset_id: task.thread_details()
            for preset_id, task in items
            if task.has_live_threads()
        }


    def force_release_all(self):
        """Release every task-held output and return IDs that failed cleanup."""
        with self.lock:
            items = list(self.tasks.items())
        failed = []
        for preset_id, task in items:
            try:
                if task.force_release() is False:
                    failed.append(preset_id)
            except Exception:
                failed.append(preset_id)
        return failed

    def is_running(self, preset_id):
        with self.lock:
            task = self.tasks.get(preset_id)
            if not task:
                return False
            if task.has_live_threads():
                return True
            if getattr(task, "release_cleanup_failed", False):
                self._remember_release_failure_locked(preset_id)
            self.tasks.pop(preset_id, None)
            return False
