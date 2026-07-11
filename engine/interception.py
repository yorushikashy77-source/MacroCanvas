import ctypes
import threading
import time
from ctypes import wintypes

from core.constants import *
from engine.interception_types import (
    INTERCEPTION_KEY_STROKE, INTERCEPTION_MOUSE_STROKE,
)
from engine.kanata import KanataConfigBuilder
from engine.window_context import (
    foreground_window_context,
    foreground_window_identity,
    foreground_window_identity_matches,
)
from engine.trigger_resolver import modifier_names
from engine.win_input import POINT, WinInput

if not hasattr(ctypes, "WINFUNCTYPE"):
    # 非 Windows 测试环境没有 WINFUNCTYPE；用 CFUNCTYPE 只保证模块可导入，
    # 实际 Windows 运行仍使用系统提供的 WINFUNCTYPE。
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE


# The Interception DLL opens one shared kernel device per context.  Serialize
# creation/destruction in this process so a hot-plug callback cannot race the
# input and output owners while the driver is rebuilding its device stack.
_INTERCEPTION_CONTEXT_LOCK = threading.RLock()


def _win32_error_text():
    error = int(ctypes.get_last_error() or 0)
    return f" (Windows error {error})" if error else ""


class InterceptionOutput:
    """Send keyboard and mouse-button output through the Interception driver."""

    BUTTON_STATES = {
        "鼠标左键": (0x001, 0x002),
        "鼠标右键": (0x004, 0x008),
        "鼠标中键": (0x010, 0x020),
        "鼠标侧键 1": (0x040, 0x080),
        "鼠标侧键 2": (0x100, 0x200),
    }
    MOUSE_WHEEL = 0x400
    MOUSE_MOVE_ABSOLUTE = 0x001
    MOUSE_VIRTUAL_DESKTOP = 0x002

    def __init__(self):
        self.dll = ctypes.WinDLL(
            str(kanata_dir() / "interception.dll"), use_last_error=True
        )
        self.context = None
        self.mouse_device = 0
        self.keyboard_device = 0
        self.lock = threading.RLock()
        self.mouse_pressed = set()
        self.key_pressed = []
        # Logical ownership counts prevent one concurrent macro from releasing
        # an output that another macro is still holding.
        self.mouse_press_counts = {}
        self.mouse_press_contexts = {}
        self.mouse_release_quarantined = set()
        # 鼠标按下时附带的修饰键需要跟随同一次 MouseUp 完成。
        # 若 MouseUp 因跨窗口被隔离，修饰键也先保留在本输出账本中，
        # 等安全释放鼠标后再按原释放顺序松开，避免 Ctrl/Shift+MouseUp
        # 被拆成“无修饰键 MouseUp”。
        self.mouse_press_modifiers = {}
        self.mouse_release_modifier_quarantine = {}
        self.key_press_counts = {}
        self.last_start_warning = ""
        # Targeted SendInput fallback used only after the driver rejects a
        # release repeatedly.  It never starts hooks and never emits new Down.
        self.recovery_output = WinInput(lambda *_args: False)
        self.user32 = ctypes.windll.user32
        self._configure()

    def _ensure_release_state(self):
        """补齐释放路径依赖的状态字段，避免异常初始化后清理流程中断。

        正常运行时这些字段都由 __init__ 创建；这里主要覆盖测试桩、
        初始化中途失败后的恢复路径，以及显式“强制释放键鼠”兜底。
        """
        if not hasattr(self, "lock") or self.lock is None:
            self.lock = threading.RLock()
        if not hasattr(self, "context"):
            self.context = None
        if not hasattr(self, "mouse_device"):
            self.mouse_device = 0
        if not hasattr(self, "keyboard_device"):
            self.keyboard_device = 0
        if not hasattr(self, "mouse_pressed") or self.mouse_pressed is None:
            self.mouse_pressed = set()
        if not hasattr(self, "key_pressed") or self.key_pressed is None:
            self.key_pressed = []
        if not hasattr(self, "mouse_press_counts") or self.mouse_press_counts is None:
            self.mouse_press_counts = {}
        if not hasattr(self, "mouse_press_contexts") or self.mouse_press_contexts is None:
            self.mouse_press_contexts = {}
        if not hasattr(self, "mouse_release_quarantined") or self.mouse_release_quarantined is None:
            self.mouse_release_quarantined = set()
        if not hasattr(self, "mouse_press_modifiers") or self.mouse_press_modifiers is None:
            self.mouse_press_modifiers = {}
        if (
            not hasattr(self, "mouse_release_modifier_quarantine")
            or self.mouse_release_modifier_quarantine is None
        ):
            self.mouse_release_modifier_quarantine = {}
        if not hasattr(self, "key_press_counts") or self.key_press_counts is None:
            self.key_press_counts = {}
        if not hasattr(self, "recovery_output") or self.recovery_output is None:
            class _NoRecoveryOutput:
                @staticmethod
                def send(*_args, **_kwargs):
                    return False
            self.recovery_output = _NoRecoveryOutput()

    def _configure(self):
        self.dll.interception_create_context.restype = ctypes.c_void_p
        self.dll.interception_destroy_context.argtypes = [ctypes.c_void_p]
        self.dll.interception_get_hardware_id.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
        ]
        self.dll.interception_get_hardware_id.restype = ctypes.c_uint
        self.dll.interception_send.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
        ]
        self.dll.interception_send.restype = ctypes.c_int
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT)
        ]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetClientRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT)
        ]
        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.ClientToScreen.argtypes = [
            wintypes.HWND, ctypes.POINTER(POINT)
        ]
        self.user32.ClientToScreen.restype = wintypes.BOOL
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int

    def _find_device(self, start, end, required, blocked):
        for device in range(start, end + 1):
            buffer = ctypes.create_unicode_buffer(500)
            length = int(self.dll.interception_get_hardware_id(
                self.context, device, buffer, 500
            ))
            if not length:
                continue
            raw = ctypes.string_at(ctypes.addressof(buffer), length)
            description = raw.decode(
                "utf-16-le", errors="ignore"
            ).replace("\0", " ").upper()
            if blocked in description or required not in description:
                continue
            return device
        return 0

    def start(self):
        with self.lock:
            if self.context:
                return True
            self.context = None
            with _INTERCEPTION_CONTEXT_LOCK:
                for attempt in range(8):
                    ctypes.set_last_error(0)
                    self.context = self.dll.interception_create_context()
                    if self.context:
                        break
                    time.sleep(0.05 * (attempt + 1))
            if not self.context:
                self.last_start_warning = (
                    "interception_create_context returned an empty output context"
                    + _win32_error_text()
                )
                return False
            self.mouse_device = self._find_device(
                11, 20, "VID_", "LDVMOUSE"
            )
            self.keyboard_device = self._find_device(
                1, 10, "VID_", "LDVKEYBOARD"
            )
            if not self.mouse_device and not self.keyboard_device:
                context = self.context
                try:
                    with _INTERCEPTION_CONTEXT_LOCK:
                        self.dll.interception_destroy_context(context)
                except (OSError, ValueError):
                    # Preserve ownership so stop() can retry destruction.
                    self.last_start_warning = (
                        "Interception output context has no usable devices and "
                        "could not be destroyed"
                    )
                    return False
                self.context = None
                self.last_start_warning = (
                    "Interception output context found no usable keyboard or mouse"
                )
                return False
            self.last_start_warning = ""
            return True

    def stop(self):
        self._ensure_release_state()
        with self.lock:
            success = bool(self.release_all())
            if not success:
                # Keep the context and every ownership record alive after any
                # failed release.  A later stop/force-release can then retry;
                # destroying now would strand a synthetic Down in the OS.
                return False
            context = self.context
            if context:
                try:
                    with _INTERCEPTION_CONTEXT_LOCK:
                        self.dll.interception_destroy_context(context)
                except (OSError, ValueError):
                    # Do not lose the native handle on failure.  Callers retain
                    # this output object and can retry stop safely.
                    return False
            self.context = None
            self.mouse_device = 0
            self.keyboard_device = 0
            return success

    def _remember_mouse_press_modifiers(self, name, modifiers):
        self._ensure_release_state()
        ordered = [modifier for modifier in (modifiers or []) if modifier]
        if not ordered:
            return
        existing = self.mouse_press_modifiers.setdefault(name, [])
        for modifier in ordered:
            if modifier not in existing:
                existing.append(modifier)

    def _remember_quarantined_mouse_modifiers(self, name, modifiers):
        self._ensure_release_state()
        ordered = [modifier for modifier in (modifiers or []) if modifier]
        if not ordered:
            return
        existing = self.mouse_release_modifier_quarantine.setdefault(name, [])
        for modifier in ordered:
            if modifier not in existing:
                existing.append(modifier)

    def _release_quarantined_mouse_modifiers(self, name):
        self._ensure_release_state()
        modifiers = self.mouse_release_modifier_quarantine.pop(name, [])
        ok = True
        for modifier in reversed(modifiers):
            ok = self.send_key(modifier, False) and ok
        return ok

    def _quarantined_modifier_names(self):
        self._ensure_release_state()
        names = set()
        for modifiers in self.mouse_release_modifier_quarantine.values():
            names.update(modifiers or [])
        return names

    def send_mouse_button(self, name, down):
        states = self.BUTTON_STATES.get(name)
        if not states:
            return False
        with self.lock:
            if (not self.context and not self.start()) or not self.mouse_device:
                return False
            count = int(self.mouse_press_counts.get(name, 0))
            # A quarantined button is still physically owned by this output
            # context. Do not stack another Down while waiting to return to
            # the original foreground window.
            if down and name in self.mouse_release_quarantined:
                return False
            if not down:
                if count <= 0:
                    return True
                if count > 1:
                    self.mouse_press_counts[name] = count - 1
                    return True
                if not foreground_window_identity_matches(
                    self.mouse_press_contexts.get(name)
                ):
                    self.mouse_release_quarantined.add(name)
                    return True
            state = states[0] if down else states[1]
            press_window_before = foreground_window_identity() if down else None
            stroke = INTERCEPTION_MOUSE_STROKE(
                state, 0, 0, 0, 0, 0x4D434E56
            )
            ok = self.dll.interception_send(
                self.context, self.mouse_device, ctypes.byref(stroke), 1
            ) == 1
            if ok:
                if down:
                    press_window_after = foreground_window_identity()
                    before_hwnd = int(press_window_before.get("hwnd") or 0)
                    after_hwnd = int(press_window_after.get("hwnd") or 0)
                    if (
                        (before_hwnd or after_hwnd)
                        and before_hwnd != after_hwnd
                    ):
                        press_window = {
                            "_unstable": True,
                            "before": press_window_before,
                            "after": press_window_after,
                        }
                    else:
                        press_window = press_window_after
                    press_window["_mouse_origin"] = True
                    self.mouse_press_counts[name] = count + 1
                    self.mouse_press_contexts.setdefault(
                        name, press_window
                    )
                    self.mouse_pressed.add(name)
                else:
                    self.mouse_press_counts.pop(name, None)
                    self.mouse_press_contexts.pop(name, None)
                    self.mouse_press_modifiers.pop(name, None)
                    self.mouse_pressed.discard(name)
                    self.mouse_release_quarantined.discard(name)
            return ok

    def retry_quarantined_mouse_releases(self, force=False):
        self._ensure_release_state()
        """Release quarantined owned buttons only in their original window.

        ``force`` is reserved for the user's explicit “强制释放键鼠” action.
        Automatic cleanup never injects MouseUp into another foreground window.
        """
        success = True
        with self.lock:
            for name in list(self.mouse_release_quarantined):
                context = self.mouse_press_contexts.get(name)
                if not force and not foreground_window_identity_matches(context):
                    success = False
                    continue
                states = self.BUTTON_STATES.get(name)
                if not states or not self.context or not self.mouse_device:
                    success = False
                    continue
                stroke = INTERCEPTION_MOUSE_STROKE(
                    states[1], 0, 0, 0, 0, 0x4D434E56
                )
                try:
                    ok = self.dll.interception_send(
                        self.context, self.mouse_device,
                        ctypes.byref(stroke), 1,
                    ) == 1
                except (OSError, ValueError):
                    ok = False
                if ok:
                    self.mouse_release_quarantined.discard(name)
                    self.mouse_press_counts.pop(name, None)
                    self.mouse_press_contexts.pop(name, None)
                    self.mouse_press_modifiers.pop(name, None)
                    ok = self._release_quarantined_mouse_modifiers(name) and ok
                    self.mouse_pressed.discard(name)
                    success = bool(ok and success)
                else:
                    success = False
        return success

    def send_wheel(self, direction):
        with self.lock:
            if (not self.context and not self.start()) or not self.mouse_device:
                return False
            rolling = 120 if direction == "向上" else -120
            stroke = INTERCEPTION_MOUSE_STROKE(
                self.MOUSE_WHEEL, 0, rolling, 0, 0, 0x4D434E56
            )
            return self.dll.interception_send(
                self.context, self.mouse_device, ctypes.byref(stroke), 1
            ) == 1

    @staticmethod
    def _recorded_context_matches(recording_context):
        if not isinstance(recording_context, dict):
            return True
        expected_process = str(recording_context.get("process") or "").strip()
        if not expected_process:
            return True
        current_process, _current_title = foreground_window_context()
        return current_process.casefold() == expected_process.casefold()

    def _virtual_screen_geometry(self):
        left = int(self.user32.GetSystemMetrics(76))
        top = int(self.user32.GetSystemMetrics(77))
        width = int(self.user32.GetSystemMetrics(78))
        height = int(self.user32.GetSystemMetrics(79))
        if width <= 0 or height <= 0:
            left = top = 0
            width = max(1, int(self.user32.GetSystemMetrics(0)))
            height = max(1, int(self.user32.GetSystemMetrics(1)))
        return left, top, width, height

    @staticmethod
    def _scaled_coordinate(value, recorded_size, current_size):
        try:
            recorded_size = int(recorded_size)
            current_size = int(current_size)
        except (TypeError, ValueError, OverflowError):
            return float(value)
        if recorded_size <= 1 or current_size <= 1:
            return float(value)
        return float(value) * (current_size - 1) / (recorded_size - 1)

    def move_absolute(self, target, recording_context=None):
        with self.lock:
            if (not self.context and not self.start()) or not self.mouse_device:
                return False
            text = str(target or "0,0")
            mode = "screen"
            for prefix in ("rel:", "pct:", "window:", "client:"):
                if text.startswith(prefix):
                    mode, text = prefix[:-1], text[len(prefix):]
                    break
            try:
                raw_x, raw_y = (float(value.strip()) for value in text.split(",", 1))
            except (TypeError, ValueError, OverflowError):
                return False
            context = recording_context if isinstance(recording_context, dict) else {}
            if mode == "rel":
                stroke = INTERCEPTION_MOUSE_STROKE(
                    0, 0, 0, int(round(raw_x)), int(round(raw_y)), 0x4D434E56
                )
                return self.dll.interception_send(
                    self.context, self.mouse_device, ctypes.byref(stroke), 1
                ) == 1
            if mode == "pct":
                expected_monitors = context.get("monitor_count")
                if expected_monitors not in (None, ""):
                    try:
                        current_monitors = int(self.user32.GetSystemMetrics(80))
                        if current_monitors != int(expected_monitors):
                            return False
                    except (TypeError, ValueError, OverflowError):
                        return False
                x = round(max(0.0, min(100.0, raw_x)) * 65535 / 100)
                y = round(max(0.0, min(100.0, raw_y)) * 65535 / 100)
            else:
                screen_x, screen_y = raw_x, raw_y
                if mode == "screen" and context.get("virtual_screen"):
                    try:
                        recorded_geometry = tuple(
                            int(value) for value in context["virtual_screen"]
                        )
                    except (TypeError, ValueError, OverflowError):
                        return False
                    if recorded_geometry != self._virtual_screen_geometry():
                        # Exact screen coordinates are unsafe after the desktop
                        # geometry changes. Percentage mode remains available for
                        # intentional resolution adaptation.
                        return False
                if mode in ("window", "client"):
                    if not self._recorded_context_matches(context):
                        return False
                    hwnd = self.user32.GetForegroundWindow()
                    if not hwnd:
                        return False
                    if mode == "window":
                        rect = wintypes.RECT()
                        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                            return False
                        width = max(1, int(rect.right - rect.left))
                        height = max(1, int(rect.bottom - rect.top))
                        local_x = self._scaled_coordinate(
                            raw_x, context.get("width"), width
                        )
                        local_y = self._scaled_coordinate(
                            raw_y, context.get("height"), height
                        )
                        local_x = max(0.0, min(width - 1, local_x))
                        local_y = max(0.0, min(height - 1, local_y))
                        screen_x = rect.left + local_x
                        screen_y = rect.top + local_y
                    else:
                        client_rect = wintypes.RECT()
                        if not self.user32.GetClientRect(
                            hwnd, ctypes.byref(client_rect)
                        ):
                            return False
                        width = max(1, int(client_rect.right - client_rect.left))
                        height = max(1, int(client_rect.bottom - client_rect.top))
                        origin = POINT(0, 0)
                        if not self.user32.ClientToScreen(
                            hwnd, ctypes.byref(origin)
                        ):
                            return False
                        local_x = self._scaled_coordinate(
                            raw_x, context.get("width"), width
                        )
                        local_y = self._scaled_coordinate(
                            raw_y, context.get("height"), height
                        )
                        local_x = max(0.0, min(width - 1, local_x))
                        local_y = max(0.0, min(height - 1, local_y))
                        screen_x = origin.x + local_x
                        screen_y = origin.y + local_y
                x, y = KanataConfigBuilder._normalized_mouse_position(
                    f"{round(screen_x)},{round(screen_y)}"
                )
            stroke = INTERCEPTION_MOUSE_STROKE(
                0,
                self.MOUSE_MOVE_ABSOLUTE | self.MOUSE_VIRTUAL_DESKTOP,
                0,
                int(x),
                int(y),
                0x4D434E56,
            )
            return self.dll.interception_send(
                self.context, self.mouse_device, ctypes.byref(stroke), 1
            ) == 1

    @staticmethod
    def _is_extended_key(name):
        return name in (
            "方向左", "方向右", "方向上", "方向下",
            "Delete", "Insert", "Home", "End", "Page Up", "Page Down",
            "Print Screen", "Menu",
        )

    def send_key(self, name, down):
        key = WinInput.key_code(name)
        if not key:
            return False
        with self.lock:
            if (
                (not self.context and not self.start())
                or not self.keyboard_device
            ):
                return False
            scan_code = int(self.user32.MapVirtualKeyW(
                key, WinInput.MAPVK_VK_TO_VSC
            ))
            if not scan_code:
                return False
            extended = self._is_extended_key(name)
            key_id = (name, scan_code, extended)
            count = int(self.key_press_counts.get(key_id, 0))
            if not down:
                if count <= 0:
                    return True
                if count > 1:
                    self.key_press_counts[key_id] = count - 1
                    return True
            state = 0
            if not down:
                state |= 0x01
            if extended:
                state |= 0x02
            stroke = INTERCEPTION_KEY_STROKE(
                scan_code, state, 0x4D434E56
            )
            ok = self.dll.interception_send(
                self.context, self.keyboard_device, ctypes.byref(stroke), 1
            ) == 1
            if ok:
                if down:
                    self.key_press_counts[key_id] = count + 1
                    if key_id not in self.key_pressed:
                        self.key_pressed.append(key_id)
                else:
                    self.key_press_counts.pop(key_id, None)
                    try:
                        self.key_pressed.remove(key_id)
                    except ValueError:
                        pass
            return ok

    def send_combo_action(self, action, phase):
        kind = action.get("type")
        target = action.get("target")
        modifiers = modifier_names(action.get("modifiers", "无"))
        if phase not in ("Press", "Release", "Tap"):
            return None
        if kind == "鼠标滚轮" and phase == "Tap":
            return self.send_wheel(action.get("target", "向上"))
        if kind == "鼠标移动" and phase == "Tap":
            return self.move_absolute(
                action.get("target", "0,0"),
                action.get("recording_context"),
            )
        if phase == "Tap" and kind in ("鼠标点击", "键盘点击"):
            if not self.send_combo_action(action, "Press"):
                return False
            return self.send_combo_action(action, "Release")
        if kind == "鼠标点击" and target in MOUSE_NAMES:
            if phase == "Press":
                pressed_modifiers = []
                for modifier in modifiers:
                    if not self.send_key(modifier, True):
                        for pressed in reversed(pressed_modifiers):
                            self.send_key(pressed, False)
                        return False
                    pressed_modifiers.append(modifier)
                if self.send_mouse_button(target, True):
                    self._remember_mouse_press_modifiers(target, modifiers)
                    return True
                for pressed in reversed(pressed_modifiers):
                    self.send_key(pressed, False)
                return False
            ok = self.send_mouse_button(target, False)
            if ok and target in self.mouse_release_quarantined:
                self._remember_quarantined_mouse_modifiers(
                    target, self.mouse_press_modifiers.get(target, modifiers)
                )
                return True
            for modifier in reversed(modifiers):
                ok = self.send_key(modifier, False) and ok
            return ok
        if kind == "键盘点击" and target in KEY_NAMES:
            if phase == "Press":
                pressed_modifiers = []
                for modifier in modifiers:
                    if not self.send_key(modifier, True):
                        for pressed in reversed(pressed_modifiers):
                            self.send_key(pressed, False)
                        return False
                    pressed_modifiers.append(modifier)
                if self.send_key(target, True):
                    return True
                for pressed in reversed(pressed_modifiers):
                    self.send_key(pressed, False)
                return False
            ok = self.send_key(target, False)
            for modifier in reversed(modifiers):
                ok = self.send_key(modifier, False) and ok
            return ok
        return None

    def release_all(self, force=False):
        self._ensure_release_state()
        success = True
        with self.lock:
            # Cleanup must not synthesize mouse-button Up events that the
            # program never pressed.  Some applications open a context menu on
            # right-button Up, so an unconditional "safe" release can become a
            # visible click while switching foreground programs.  Even the force
            # path is limited to program-owned buttons.
            if self.context and self.mouse_device:
                names = list(
                    dict.fromkeys(
                        list(self.mouse_pressed) + list(self.mouse_press_counts)
                    )
                )
                for name in reversed(names):
                    states = self.BUTTON_STATES.get(name)
                    if not states:
                        continue
                    if (
                        not force
                        and not foreground_window_identity_matches(
                            self.mouse_press_contexts.get(name)
                        )
                    ):
                        self.mouse_release_quarantined.add(name)
                        self._remember_quarantined_mouse_modifiers(
                            name, self.mouse_press_modifiers.get(name, [])
                        )
                        success = False
                        continue
                    stroke = INTERCEPTION_MOUSE_STROKE(
                        states[1], 0, 0, 0, 0, 0x4D434E56
                    )
                    ok = False
                    for attempt in range(3):
                        try:
                            ok = self.dll.interception_send(
                                self.context, self.mouse_device,
                                ctypes.byref(stroke), 1,
                            ) == 1
                        except (OSError, ValueError):
                            ok = False
                        if ok:
                            break
                        if attempt < 2:
                            time.sleep(0.01)
                    if not ok:
                        if (
                            not force
                            and not foreground_window_identity_matches(
                                self.mouse_press_contexts.get(name)
                            )
                        ):
                            self.mouse_release_quarantined.add(name)
                            self._remember_quarantined_mouse_modifiers(
                                name, self.mouse_press_modifiers.get(name, [])
                            )
                        else:
                            try:
                                ok = bool(self.recovery_output.send(name, False))
                            except (OSError, ValueError, AttributeError):
                                ok = False
                    if ok:
                        self.mouse_pressed.discard(name)
                        self.mouse_press_counts.pop(name, None)
                        self.mouse_press_contexts.pop(name, None)
                        self.mouse_press_modifiers.pop(name, None)
                        self.mouse_release_quarantined.discard(name)
                        ok = self._release_quarantined_mouse_modifiers(name) and ok
                    success = bool(ok and success)

            if self.context and self.keyboard_device:
                deferred_modifier_names = set() if force else self._quarantined_modifier_names()
                if deferred_modifier_names:
                    success = False
                key_ids = list(dict.fromkeys(
                    list(self.key_pressed) + list(self.key_press_counts)
                ))
                for name, scan_code, extended in reversed(key_ids):
                    if name in deferred_modifier_names:
                        continue
                    state = 0x01 | (0x02 if extended else 0)
                    stroke = INTERCEPTION_KEY_STROKE(
                        scan_code, state, 0x4D434E56
                    )
                    ok = False
                    for attempt in range(3):
                        try:
                            ok = self.dll.interception_send(
                                self.context, self.keyboard_device,
                                ctypes.byref(stroke), 1,
                            ) == 1
                        except (OSError, ValueError):
                            ok = False
                        if ok:
                            break
                        if attempt < 2:
                            time.sleep(0.01)
                    if not ok:
                        try:
                            ok = bool(self.recovery_output.send(name, False))
                        except (OSError, ValueError, AttributeError):
                            ok = False
                    success = bool(ok and success)
                    if ok:
                        self.key_press_counts.pop((name, scan_code, extended), None)
                        try:
                            self.key_pressed.remove((name, scan_code, extended))
                        except ValueError:
                            pass
        return success

    def pending_release_summary(self):
        """Describe retained owned outputs after a failed cleanup attempt."""
        self._ensure_release_state()
        with self.lock:
            key_ids = list(dict.fromkeys(
                list(self.key_pressed) + list(self.key_press_counts)
            ))
            mouse_names = list(dict.fromkeys(
                list(self.mouse_pressed) + list(self.mouse_press_counts)
            ))
            return {
                "keys": [item[0] for item in key_ids],
                "mouse": sorted(mouse_names),
                "quarantined_mouse": sorted(self.mouse_release_quarantined),
            }

    def force_release_names_untracked(self, names, attempts=3):
        self._ensure_release_state()
        """Emit driver-level Up for configured targets without ownership state.

        This is the last-resort path for a Down that reached the driver but whose
        Python bookkeeping was lost.  It is used only by explicit user recovery.
        """
        requested = list(dict.fromkeys(
            str(name) for name in (names or []) if str(name) in INPUT_NAMES
        ))
        if not requested:
            return True
        with self.lock:
            if not self.context and not self.start():
                return False
            success = True
            for name in requested:
                sent = False
                states = self.BUTTON_STATES.get(name)
                if states and self.mouse_device:
                    stroke = INTERCEPTION_MOUSE_STROKE(
                        states[1], 0, 0, 0, 0, WinInput.INJECTION_TAG
                    )
                    for attempt in range(max(1, int(attempts))):
                        try:
                            sent = self.dll.interception_send(
                                self.context, self.mouse_device,
                                ctypes.byref(stroke), 1,
                            ) == 1
                        except (OSError, ValueError):
                            sent = False
                        if sent:
                            break
                        if attempt + 1 < max(1, int(attempts)):
                            time.sleep(0.01)
                    if sent:
                        self.mouse_pressed.discard(name)
                        self.mouse_press_counts.pop(name, None)
                        self.mouse_press_contexts.pop(name, None)
                        self.mouse_press_modifiers.pop(name, None)
                        self.mouse_release_quarantined.discard(name)
                        self._release_quarantined_mouse_modifiers(name)
                elif name in KEY_NAMES and self.keyboard_device:
                    key = WinInput.key_code(name)
                    scan_code = int(self.user32.MapVirtualKeyW(
                        key, WinInput.MAPVK_VK_TO_VSC
                    )) if key else 0
                    if scan_code:
                        extended = self._is_extended_key(name)
                        stroke = INTERCEPTION_KEY_STROKE(
                            scan_code,
                            0x01 | (0x02 if extended else 0),
                            WinInput.INJECTION_TAG,
                        )
                        for attempt in range(max(1, int(attempts))):
                            try:
                                sent = self.dll.interception_send(
                                    self.context, self.keyboard_device,
                                    ctypes.byref(stroke), 1,
                                ) == 1
                            except (OSError, ValueError):
                                sent = False
                            if sent:
                                break
                            if attempt + 1 < max(1, int(attempts)):
                                time.sleep(0.01)
                        if sent:
                            for key_id in list(self.key_press_counts):
                                if key_id[0] == name:
                                    self.key_press_counts.pop(key_id, None)
                            self.key_pressed = [
                                key_id for key_id in self.key_pressed
                                if key_id[0] != name
                            ]
                if not sent:
                    try:
                        sent = bool(self.recovery_output.send(name, False))
                    except (OSError, ValueError, AttributeError):
                        sent = False
                if sent and states:
                    self.mouse_pressed.discard(name)
                    self.mouse_press_counts.pop(name, None)
                    self.mouse_press_contexts.pop(name, None)
                    self.mouse_release_quarantined.discard(name)
                elif sent and name in KEY_NAMES:
                    for key_id in list(self.key_press_counts):
                        if key_id[0] == name:
                            self.key_press_counts.pop(key_id, None)
                    self.key_pressed = [
                        key_id for key_id in self.key_pressed
                        if key_id[0] != name
                    ]
                success = bool(sent and success)
            return success



class InterceptionInputHook:
    """Direct Interception source owner for keyboard and mouse triggers.

    Mapping sources and preset triggers receive the exact same canonical event
    from this class.  The listener owns all physical keyboard/mouse devices at a
    high Interception precedence, forwards unmatched strokes unchanged, and
    suppresses only a source event that the shared trigger dispatcher accepted.
    """

    PREDICATE = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int)
    KEY_FILTER_ALL = 0xFFFF
    # Capture every button/wheel state but leave high-rate movement outside the
    # Python loop.  Button 4/5 use the same driver-level state mask as the basic
    # mapping path, without adding aim/movement latency in games.
    MOUSE_FILTER_BUTTONS_WHEEL = 0x0FFF
    MOUSE_FILTER_WITH_MOVE = 0xFFFF
    INPUT_PRECEDENCE = 1000
    MOUSE_MOVE_ABSOLUTE = 0x001
    MOUSE_VIRTUAL_DESKTOP = 0x002

    KEYBOARD_SIDE_BUTTON_SCANCODES = {
        (0x6A, True): "鼠标侧键 1",   # E0 6A: Browser Back
        (0x69, True): "鼠标侧键 2",   # E0 69: Browser Forward
        # A few vendor virtual-keyboard devices lose the E0 state while retaining
        # the browser scan code.  Accept these forms as the same physical source.
        (0x6A, False): "鼠标侧键 1",
        (0x69, False): "鼠标侧键 2",
    }
    KEYBOARD_SIDE_BUTTON_VKS = {
        0x05: "鼠标侧键 1",  # VK_XBUTTON1
        0x06: "鼠标侧键 2",  # VK_XBUTTON2
        0xA6: "鼠标侧键 1",  # VK_BROWSER_BACK
        0xA7: "鼠标侧键 2",  # VK_BROWSER_FORWARD
    }
    MOUSE_EVENTS = (
        (0x001, "鼠标左键", True),
        (0x002, "鼠标左键", False),
        (0x004, "鼠标右键", True),
        (0x008, "鼠标右键", False),
        (0x010, "鼠标中键", True),
        (0x020, "鼠标中键", False),
        (0x040, "鼠标侧键 1", True),
        (0x080, "鼠标侧键 1", False),
        (0x100, "鼠标侧键 2", True),
        (0x200, "鼠标侧键 2", False),
    )
    MOUSE_BUTTON_MASK = sum(item[0] for item in MOUSE_EVENTS)
    MOUSE_WHEEL_STATE = 0x400

    def __init__(
        self, callback, raw_event_callback=None,
        capture_mouse=True, map_keyboard_side_buttons=True,
        capture_mouse_move=False, mouse_move_emit_interval_ms=4,
        source_callback=None,
    ):
        self.callback = callback
        self.source_callback = source_callback
        self.raw_event_callback = raw_event_callback
        self.capture_mouse = bool(capture_mouse)
        self.map_keyboard_side_buttons = bool(map_keyboard_side_buttons)
        self.capture_mouse_move = bool(capture_mouse_move)
        self.mouse_move_emit_interval = max(
            0.001, float(mouse_move_emit_interval_ms or 4) / 1000
        )
        self.mouse_move_lock = threading.RLock()
        self.pending_mouse_move = None
        self.last_mouse_move_emit = 0.0
        self.user32 = ctypes.windll.user32
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.dll = ctypes.WinDLL(
            str(kanata_dir() / "interception.dll"), use_last_error=True
        )
        self.context = None
        self.thread = None
        self.stop_event = threading.Event()
        self.state_lock = threading.RLock()
        self.pressed_sources_lock = threading.RLock()
        self.pressed_sources = {}
        self.last_stop_warning = ""
        self.active_keyboard_devices = set()
        self.active_mouse_devices = set()
        self._device_predicates = {}
        self._last_device_refresh = 0.0
        self.keyboard_predicate = self.PREDICATE(
            lambda device: 1 if 1 <= int(device) <= 10 else 0
        )
        self.mouse_predicate = self.PREDICATE(
            lambda device: 1 if 11 <= int(device) <= 20 else 0
        )
        self._configure()

    def is_alive(self):
        with self.state_lock:
            return bool(
                self.context
                and self.thread
                and self.thread.is_alive()
                and not self.stop_event.is_set()
            )

    def pressed_input_snapshot(self):
        with self.pressed_sources_lock:
            return [
                (name, source_id)
                for source_id, name in self.pressed_sources.items()
            ]

    def _dispatch_source(self, name, down, source_id):
        source_id = str(source_id)
        with self.pressed_sources_lock:
            if down:
                self.pressed_sources[source_id] = str(name)
            else:
                self.pressed_sources.pop(source_id, None)
        if self.source_callback is not None:
            return bool(self.source_callback(name, down, source_id))
        return bool(self.callback(name, down))

    def _configure(self):
        self.dll.interception_create_context.restype = ctypes.c_void_p
        self.dll.interception_destroy_context.argtypes = [ctypes.c_void_p]
        self.dll.interception_set_filter.argtypes = [
            ctypes.c_void_p, self.PREDICATE, ctypes.c_ushort,
        ]
        self.dll.interception_wait_with_timeout.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
        ]
        self.dll.interception_wait_with_timeout.restype = ctypes.c_int
        self.dll.interception_receive.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
        ]
        self.dll.interception_receive.restype = ctypes.c_int
        self.dll.interception_send.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
        ]
        self.dll.interception_send.restype = ctypes.c_int
        self.dll.interception_get_hardware_id.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
        ]
        self.dll.interception_get_hardware_id.restype = ctypes.c_uint
        try:
            self.dll.interception_set_precedence.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ]
            self.dll.interception_set_precedence.restype = None
            self.has_precedence_api = True
        except AttributeError:
            self.has_precedence_api = False

    def _device_description(self, context, device):
        buffer = ctypes.create_string_buffer(4096)
        try:
            length = int(self.dll.interception_get_hardware_id(
                context, int(device), buffer, ctypes.sizeof(buffer)
            ))
        except (OSError, ValueError, AttributeError):
            return ""
        if length <= 0:
            return ""
        return bytes(buffer.raw[:length]).decode(
            "utf-16-le", errors="ignore"
        ).replace("\0", " ").upper()

    def _present_physical_devices(self, context, start, end, blocked):
        devices = set()
        for device in range(start, end + 1):
            description = self._device_description(context, device)
            if "VID_" not in description or blocked in description:
                continue
            devices.add(int(device))
        return devices

    def _set_device_filter(self, context, device, filter_mask):
        device = int(device)
        predicate = self._device_predicates.get(device)
        if predicate is None:
            predicate = self.PREDICATE(
                lambda current, target=device: int(current) == target
            )
            self._device_predicates[device] = predicate
        self.dll.interception_set_filter(context, predicate, filter_mask)

    def _apply_physical_device_filters(self, context):
        """Bind only present physical devices, leaving virtual/stale slots alone."""
        for device in range(1, 21):
            self._set_device_filter(context, device, 0)
        keyboards = self._present_physical_devices(
            context, 1, 10, "LDVKEYBOARD"
        )
        mice = self._present_physical_devices(
            context, 11, 20, "LDVMOUSE"
        ) if self.capture_mouse else set()
        if not keyboards:
            raise RuntimeError(
                "Interception 未枚举到可用的物理键盘设备"
            )
        for device in keyboards:
            self._set_device_filter(context, device, self.KEY_FILTER_ALL)
        for device in mice:
            mouse_filter = (
                self.MOUSE_FILTER_WITH_MOVE
                if self.capture_mouse_move
                else self.MOUSE_FILTER_BUTTONS_WHEEL
            )
            self._set_device_filter(context, device, mouse_filter)
        self.active_keyboard_devices = keyboards
        self.active_mouse_devices = mice

    def _refresh_physical_device_filters(self, context):
        """Rebind newly enumerated devices without cycling the native context."""
        keyboards = self._present_physical_devices(
            context, 1, 10, "LDVKEYBOARD"
        )
        if not keyboards:
            return False
        mice = self._present_physical_devices(
            context, 11, 20, "LDVMOUSE"
        ) if self.capture_mouse else set()
        keyboard_mask = self.KEY_FILTER_ALL
        mouse_mask = (
            self.MOUSE_FILTER_WITH_MOVE
            if self.capture_mouse_move
            else self.MOUSE_FILTER_BUTTONS_WHEEL
        )
        for device in self.active_keyboard_devices - keyboards:
            self._set_device_filter(context, device, 0)
        for device in keyboards - self.active_keyboard_devices:
            self._set_device_filter(context, device, keyboard_mask)
        for device in self.active_mouse_devices - mice:
            self._set_device_filter(context, device, 0)
        for device in mice - self.active_mouse_devices:
            self._set_device_filter(context, device, mouse_mask)
        self.active_keyboard_devices = keyboards
        self.active_mouse_devices = mice
        return True

    def _emit_raw(self, **payload):
        if not self.raw_event_callback:
            return
        try:
            self.raw_event_callback(payload)
        except Exception:
            pass

    def _emit_mouse_move_payload(self, payload):
        enriched = dict(payload)
        try:
            point = POINT()
            if self.user32.GetCursorPos(ctypes.byref(point)):
                enriched["cursor_x"] = int(point.x)
                enriched["cursor_y"] = int(point.y)
        except Exception:
            pass
        self._emit_raw(**enriched)

    def _queue_mouse_move(self, device, stroke, state=0):
        if not self.capture_mouse_move:
            return
        flags = int(stroke.flags)
        x = int(stroke.x)
        y = int(stroke.y)
        if not (x or y or flags & (self.MOUSE_MOVE_ABSOLUTE | self.MOUSE_VIRTUAL_DESKTOP)):
            return
        now = time.perf_counter()
        absolute = bool(flags & self.MOUSE_MOVE_ABSOLUTE)
        ready = []
        with self.mouse_move_lock:
            pending = self.pending_mouse_move
            if pending is not None and bool(
                int(pending.get("flags", 0)) & self.MOUSE_MOVE_ABSOLUTE
            ) != absolute:
                ready.append(pending)
                pending = None
            if pending is None:
                pending = {
                    "kind": "mouse_move",
                    "device": int(device),
                    "state": int(state),
                    "flags": flags,
                    "x": x,
                    "y": y,
                    "rolling": int(stroke.rolling),
                    "time": now,
                }
            elif absolute:
                pending.update({
                    "device": int(device),
                    "state": int(state),
                    "flags": flags,
                    "x": x,
                    "y": y,
                    "rolling": int(stroke.rolling),
                    "time": now,
                })
            else:
                pending["x"] = int(pending.get("x", 0)) + x
                pending["y"] = int(pending.get("y", 0)) + y
                pending["state"] = int(state)
                pending["flags"] = flags
                pending["rolling"] = int(stroke.rolling)
                pending["time"] = now
            self.pending_mouse_move = pending
            if now - self.last_mouse_move_emit >= self.mouse_move_emit_interval:
                ready.append(self.pending_mouse_move)
                self.pending_mouse_move = None
                self.last_mouse_move_emit = now
        for payload in ready:
            self._emit_mouse_move_payload(payload)

    def flush_mouse_move_events(self):
        payload = None
        with self.mouse_move_lock:
            if self.pending_mouse_move is not None:
                payload = self.pending_mouse_move
                self.pending_mouse_move = None
                self.last_mouse_move_emit = time.perf_counter()
        if payload is not None:
            self._emit_mouse_move_payload(payload)

    def start(self):
        with self.state_lock:
            if self.thread and self.thread.is_alive():
                return True
            # A previous listener that has not finished its callback still owns
            # its context.  Starting a second owner would make both contexts
            # compete for the same physical strokes.
            if self.context:
                self.last_stop_warning = (
                    "Interception listener context is still owned by a previous start"
                )
                return False
            context = None
            with _INTERCEPTION_CONTEXT_LOCK:
                for attempt in range(8):
                    ctypes.set_last_error(0)
                    context = self.dll.interception_create_context()
                    if context:
                        break
                    time.sleep(0.05 * (attempt + 1))
            if not context:
                self.last_stop_warning = (
                    "interception_create_context returned an empty input context"
                    + _win32_error_text()
                )
                return False
            self.context = context
            try:
                # Give this one source owner precedence over stale or third-party
                # Interception contexts.  This is especially relevant for gaming-mouse
                # utilities which may otherwise consume Button 4/5 before this program.
                if self.has_precedence_api:
                    device_end = 20 if self.capture_mouse else 10
                    for device in range(1, device_end + 1):
                        try:
                            self.dll.interception_set_precedence(
                                context, device, self.INPUT_PRECEDENCE
                            )
                        except (OSError, ValueError):
                            pass

                self._apply_physical_device_filters(context)
                self.stop_event.clear()
                self.last_stop_warning = ""
                thread = threading.Thread(
                    target=self._loop,
                    name="MacroCanvas-InterceptionInput",
                    daemon=False,
                )
                self.thread = thread
                thread.start()
                return True
            except (OSError, RuntimeError, ValueError) as error:
                self.thread = None
                try:
                    with _INTERCEPTION_CONTEXT_LOCK:
                        self.dll.interception_destroy_context(context)
                except (OSError, ValueError) as destroy_error:
                    # Preserve the native handle so a later stop/retry can
                    # attempt destruction again instead of losing ownership.
                    self.context = context
                    self.last_stop_warning = (
                        f"Interception 启动失败且 context 销毁失败：{destroy_error}"
                    )
                else:
                    self.context = None
                    self.last_stop_warning = str(error)
                return False

    def update_capture_mouse_move(self, capture_mouse_move):
        """Update the mouse filter without cycling the Interception context."""
        capture_mouse_move = bool(capture_mouse_move)
        with self.state_lock:
            context = self.context
            alive = bool(
                context
                and self.thread
                and self.thread.is_alive()
                and not self.stop_event.is_set()
            )
            if not alive:
                return False
            if self.capture_mouse_move == capture_mouse_move:
                return True
            if not self.capture_mouse:
                self.capture_mouse_move = False
                return not capture_mouse_move
            mouse_filter = (
                self.MOUSE_FILTER_WITH_MOVE
                if capture_mouse_move
                else self.MOUSE_FILTER_BUTTONS_WHEEL
            )
            try:
                for device in self.active_mouse_devices:
                    self._set_device_filter(context, device, mouse_filter)
            except (OSError, ValueError) as error:
                self.last_stop_warning = (
                    f"Interception mouse filter update failed: {error}"
                )
                return False
            if not capture_mouse_move:
                self.flush_mouse_move_events()
            self.capture_mouse_move = capture_mouse_move
            self.last_stop_warning = ""
            return True

    def stop(self, timeout=1.0):
        """Stop receiving without destroying a context still used by its thread.

        The Interception wait call has a 50 ms timeout, so a normal listener exits
        quickly.  If a UI callback is temporarily blocked, the listener thread
        keeps ownership of the native context and destroys it itself on exit.
        """
        self.stop_event.set()
        with self.state_lock:
            thread = self.thread
            context = self.context

        # Disable future captures before waiting.  This also prevents new input
        # from accumulating while an in-flight callback is completing.
        if context:
            try:
                self.dll.interception_set_filter(
                    context, self.keyboard_predicate, 0
                )
                if self.capture_mouse:
                    self.dll.interception_set_filter(
                        context, self.mouse_predicate, 0
                    )
            except (OSError, ValueError):
                pass

        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout)))

        alive = bool(thread and thread.is_alive())
        if alive:
            self.last_stop_warning = (
                "Interception 输入线程仍在结束回调；context 将由监听线程退出时释放"
            )
            return False

        # The loop normally performs this cleanup in its finally block.  Keep a
        # retry path for a context whose first destruction attempt failed.
        with self.state_lock:
            if self.thread is thread:
                self.thread = None
            orphan = self.context if self.context == context else None
        if orphan:
            try:
                with _INTERCEPTION_CONTEXT_LOCK:
                    self.dll.interception_destroy_context(orphan)
            except (OSError, ValueError) as error:
                self.last_stop_warning = (
                    f"Interception 输入线程已退出，但 context 销毁失败：{error}"
                )
                return False
            with self.state_lock:
                if self.context == orphan:
                    self.context = None
        self.last_stop_warning = ""
        return True

    def _send_stroke(self, device, stroke):
        context = self.context
        if not context:
            return False
        return self.dll.interception_send(
            context, int(device), ctypes.byref(stroke), 1
        ) == 1

    @staticmethod
    def _normalize_interception_scan(code, extended):
        """Accept both normal Interception scan encoding and embedded E0/E1."""
        raw = int(code) & 0xFFFF
        prefix = raw & 0xFF00
        if prefix == 0xE000:
            return raw & 0xFF, True
        if prefix == 0xE100:
            return raw & 0xFF, True
        return raw, bool(extended)

    def _handle_keyboard(self, device):
        stroke = INTERCEPTION_KEY_STROKE()
        if self.dll.interception_receive(
            self.context, device, ctypes.byref(stroke), 1
        ) != 1:
            return
        if int(stroke.information) == WinInput.INJECTION_TAG:
            self._send_stroke(device, stroke)
            return

        down = not bool(int(stroke.state) & 0x01)
        code, extended = self._normalize_interception_scan(
            int(stroke.code), bool(int(stroke.state) & 0x02)
        )
        scan = code | (0xE000 if extended else 0)
        vk = int(ctypes.windll.user32.MapVirtualKeyW(scan, 3))
        side_name = None
        if self.map_keyboard_side_buttons:
            side_name = (
                self.KEYBOARD_SIDE_BUTTON_SCANCODES.get((code, extended))
                or self.KEYBOARD_SIDE_BUTTON_VKS.get(vk)
            )
        name = side_name or WinInput.key_name(vk)
        if self.capture_mouse_move:
            self.flush_mouse_move_events()
        self._emit_raw(
            kind="keyboard", device=int(device), code=int(code),
            extended=bool(extended), vk=int(vk), name=name, down=bool(down),
        )
        suppress = False
        source_id = f"interception:kbd:{int(device)}:{int(code):02X}:{int(bool(extended))}"
        try:
            suppress = self._dispatch_source(name, down, source_id)
        except Exception:
            suppress = False
        if not suppress:
            self._send_stroke(device, stroke)

    def _handle_mouse(self, device):
        stroke = INTERCEPTION_MOUSE_STROKE()
        if self.dll.interception_receive(
            self.context, device, ctypes.byref(stroke), 1
        ) != 1:
            return
        if int(stroke.information) == WinInput.INJECTION_TAG:
            self._send_stroke(device, stroke)
            return

        original_state = int(stroke.state) & 0xFFFF
        has_move_payload = bool(
            int(stroke.x)
            or int(stroke.y)
            or int(stroke.flags) & (
                self.MOUSE_MOVE_ABSOLUTE | self.MOUSE_VIRTUAL_DESKTOP
            )
        )

        # Pure movement is forwarded before any Python recording callback. This
        # keeps the game input path independent from recording/UI processing.
        if original_state == 0:
            self._send_stroke(device, stroke)
            if has_move_payload:
                self._queue_mouse_move(device, stroke, original_state)
            return

        if self.capture_mouse_move:
            self.flush_mouse_move_events()
        remaining_state = original_state
        recognized_bits = original_state & self.MOUSE_BUTTON_MASK
        accepted_any = False
        if original_state & self.MOUSE_WHEEL_STATE:
            self._emit_raw(
                kind="mouse_wheel",
                device=int(device),
                state=original_state,
                flags=int(stroke.flags),
                rolling=int(stroke.rolling),
                time=time.perf_counter(),
            )
        for bit, raw_name, down in self.MOUSE_EVENTS:
            if not (recognized_bits & bit):
                continue
            name = raw_name
            self._emit_raw(
                kind="mouse", device=int(device), state=original_state,
                bit=int(bit), name=name, down=bool(down),
                flags=int(stroke.flags), rolling=int(stroke.rolling),
                time=time.perf_counter(),
            )
            suppress = False
            source_id = f"interception:mouse:{int(device)}:{name}"
            try:
                suppress = self._dispatch_source(name, down, source_id)
            except Exception:
                suppress = False
            if suppress:
                accepted_any = True
                remaining_state &= ~bit

        # Preserve wheels, horizontal wheels, attributes and every unrecognized
        # state bit. Only accepted source-transition bits are removed.
        has_other_payload = bool(
            remaining_state
            or int(stroke.flags)
            or int(stroke.rolling)
            or int(stroke.x)
            or int(stroke.y)
        )
        if has_other_payload or not recognized_bits or not accepted_any:
            forwarded = INTERCEPTION_MOUSE_STROKE(
                remaining_state,
                int(stroke.flags),
                int(stroke.rolling),
                int(stroke.x),
                int(stroke.y),
                int(stroke.information),
            )
            self._send_stroke(device, forwarded)
        if has_move_payload:
            self._queue_mouse_move(device, stroke, original_state)

    def _loop(self):
        with self.state_lock:
            context = self.context
        try:
            while not self.stop_event.is_set() and context:
                now = time.monotonic()
                if now - self._last_device_refresh >= 0.5:
                    try:
                        self._refresh_physical_device_filters(context)
                    except (OSError, RuntimeError, ValueError):
                        # Keep the last known-good filters during a transient
                        # USB descriptor failure; do not tear down the context.
                        pass
                    self._last_device_refresh = now
                device = int(self.dll.interception_wait_with_timeout(
                    context, 50
                ))
                if self.stop_event.is_set():
                    break
                if 1 <= device <= 10:
                    self._handle_keyboard(device)
                elif self.capture_mouse and 11 <= device <= 20:
                    self._handle_mouse(device)
        finally:
            self.flush_mouse_move_events()
            with self.pressed_sources_lock:
                self.pressed_sources.clear()
            # The listener thread is the native context owner.  Never let the UI
            # thread destroy the context while wait/receive/send may still be on
            # this stack.  Only clear the handle after destruction succeeds;
            # otherwise stop() retains a retryable native handle.
            with self.state_lock:
                owned = self.context if self.context == context else None
                if self.thread is threading.current_thread():
                    self.thread = None
            if owned:
                try:
                    with _INTERCEPTION_CONTEXT_LOCK:
                        self.dll.interception_destroy_context(owned)
                except (OSError, ValueError) as error:
                    self.last_stop_warning = (
                        f"Interception 监听已退出，但 context 销毁失败：{error}"
                    )
                else:
                    with self.state_lock:
                        if self.context == owned:
                            self.context = None
                    self.last_stop_warning = ""
