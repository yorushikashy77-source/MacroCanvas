import ctypes
import threading
import time
from ctypes import wintypes

from core.constants import *
from engine.trigger_resolver import modifier_names

if not hasattr(ctypes, "WINFUNCTYPE"):
    # 非 Windows 测试环境没有 WINFUNCTYPE；用 CFUNCTYPE 只保证模块可导入，
    # 实际 Windows 运行仍使用系统提供的 WINFUNCTYPE。
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT), ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


class WinInput:
    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
    WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
    WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
    WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
    WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
    WM_MOUSEMOVE, WM_MOUSEWHEEL = 0x0200, 0x020A
    WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
    LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01
    INJECTION_TAG = 0x4D434E56
    WM_QUIT = 0x0012
    MAPVK_VK_TO_VSC = 0
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )

    def __init__(
        self, callback, status_callback=None, event_callback=None,
        source_callback=None,
    ):
        self.callback = callback
        self.source_callback = source_callback
        self.status_callback = status_callback
        self.event_callback = event_callback
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.keyboard_hook = None
        self.mouse_hook = None
        self.thread = None
        self.thread_id = 0
        self.ready = threading.Event()
        self.output_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.last_stop_warning = ""
        self._keyboard_proc = self.HOOKPROC(self._keyboard_callback)
        self._mouse_proc = self.HOOKPROC(self._mouse_callback)
        self._configure_api()

    def _configure_api(self):
        self.user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, self.HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
        ]
        self.user32.SetWindowsHookExW.restype = wintypes.HHOOK
        self.user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self.user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self.user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        ]
        self.user32.CallNextHookEx.restype = ctypes.c_ssize_t
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
        ]
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        self.user32.PostThreadMessageW.restype = wintypes.BOOL
        self.user32.SendInput.argtypes = [
            wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int
        ]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = wintypes.SHORT
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def start(self):
        if self.thread and self.thread.is_alive():
            return bool(self.keyboard_hook and self.mouse_hook)
        self.ready.clear()
        self.thread = threading.Thread(
            target=self._message_loop,
            name="MacroCanvas-WinInputHook",
            daemon=False,
        )
        self.thread.start()
        self.ready.wait(timeout=2)
        return bool(self.keyboard_hook and self.mouse_hook)

    def is_alive(self):
        with self.state_lock:
            thread = self.thread
            return bool(
                thread
                and thread.is_alive()
                and self.keyboard_hook
                and self.mouse_hook
            )

    def _dispatch_source(self, name, down, source_id):
        if self.source_callback is not None:
            return bool(self.source_callback(name, down, source_id))
        return bool(self.callback(name, down))

    def stop(self, timeout=1.5):
        with self.state_lock:
            thread = self.thread
            thread_id = self.thread_id
        if thread_id:
            try:
                self.user32.PostThreadMessageW(
                    thread_id, self.WM_QUIT, 0, 0
                )
            except (OSError, ValueError):
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout)))
        if thread and thread.is_alive():
            self.last_stop_warning = "Windows 低级钩子线程仍在退出"
            return False
        with self.state_lock:
            if self.thread is thread:
                self.thread = None
            self.keyboard_hook = None
            self.mouse_hook = None
            self.thread_id = 0
        self.last_stop_warning = ""
        return True

    def _message_loop(self):
        current = threading.current_thread()
        keyboard_hook = None
        mouse_hook = None
        try:
            self.thread_id = self.kernel32.GetCurrentThreadId()
            module = self.kernel32.GetModuleHandleW(None)
            keyboard_hook = self.user32.SetWindowsHookExW(
                self.WH_KEYBOARD_LL, self._keyboard_proc, module, 0
            )
            mouse_hook = self.user32.SetWindowsHookExW(
                self.WH_MOUSE_LL, self._mouse_proc, module, 0
            )
            self.keyboard_hook = keyboard_hook
            self.mouse_hook = mouse_hook
            success = bool(keyboard_hook and mouse_hook)
            if self.status_callback:
                if success:
                    self.status_callback(True, "键盘和鼠标监听已就绪")
                else:
                    error = ctypes.get_last_error()
                    self.status_callback(
                        False, f"输入监听安装失败（Windows 错误 {error}）"
                    )
            self.ready.set()
            if not success:
                return
            message = wintypes.MSG()
            while self.user32.GetMessageW(
                ctypes.byref(message), None, 0, 0
            ) > 0:
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if keyboard_hook:
                try:
                    self.user32.UnhookWindowsHookEx(keyboard_hook)
                except (OSError, ValueError):
                    pass
            if mouse_hook:
                try:
                    self.user32.UnhookWindowsHookEx(mouse_hook)
                except (OSError, ValueError):
                    pass
            with self.state_lock:
                self.keyboard_hook = None
                self.mouse_hook = None
                self.thread_id = 0
                if self.thread is current:
                    self.thread = None
            self.ready.set()

    def _keyboard_callback(self, code, wparam, lparam):
        if code >= 0:
            data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if data.dwExtraInfo != self.INJECTION_TAG:
                relevant = wparam in (
                    self.WM_KEYDOWN, self.WM_SYSKEYDOWN,
                    self.WM_KEYUP, self.WM_SYSKEYUP,
                )
                down = wparam in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
                name = self.key_name(data.vkCode)
                # Recording may need to observe Kanata pass-through events that
                # Windows marks as injected. Mapping callbacks still receive only
                # physical events, so synthetic output cannot retrigger macros.
                if self.event_callback and relevant:
                    self.event_callback({
                        "kind": "key", "name": name, "down": down,
                        "injected": bool(data.flags & self.LLKHF_INJECTED),
                        "time": time.perf_counter(),
                    })
                if (
                    relevant
                    and not (data.flags & self.LLKHF_INJECTED)
                    and self._dispatch_source(
                        name, down, f"win:kbd:{int(data.vkCode):02X}"
                    )
                ):
                    return 1
        return self.user32.CallNextHookEx(None, code, wparam, lparam)

    def _mouse_callback(self, code, wparam, lparam):
        if code >= 0:
            data = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if data.dwExtraInfo != self.INJECTION_TAG:
                known = {
                    self.WM_LBUTTONDOWN: ("鼠标左键", True),
                    self.WM_LBUTTONUP: ("鼠标左键", False),
                    self.WM_RBUTTONDOWN: ("鼠标右键", True),
                    self.WM_RBUTTONUP: ("鼠标右键", False),
                    self.WM_MBUTTONDOWN: ("鼠标中键", True),
                    self.WM_MBUTTONUP: ("鼠标中键", False),
                }
                result = known.get(wparam)
                if wparam in (self.WM_XBUTTONDOWN, self.WM_XBUTTONUP):
                    result = (
                        "鼠标侧键 1" if (data.mouseData >> 16) == 1 else "鼠标侧键 2",
                        wparam == self.WM_XBUTTONDOWN,
                    )
                if self.event_callback:
                    if result:
                        self.event_callback({
                            "kind": "button", "name": result[0],
                            "down": result[1], "x": data.pt.x, "y": data.pt.y,
                            "injected": bool(data.flags & self.LLMHF_INJECTED),
                            "time": time.perf_counter(),
                        })
                    elif wparam == self.WM_MOUSEWHEEL:
                        delta = ctypes.c_short(data.mouseData >> 16).value
                        self.event_callback({
                            "kind": "wheel", "delta": delta,
                            "x": data.pt.x, "y": data.pt.y,
                            "time": time.perf_counter(),
                        })
                    elif wparam == self.WM_MOUSEMOVE:
                        self.event_callback({
                            "kind": "move", "x": data.pt.x, "y": data.pt.y,
                            "time": time.perf_counter(),
                        })
                if (
                    result
                    and not (data.flags & self.LLMHF_INJECTED)
                    and self._dispatch_source(
                        result[0], result[1], f"win:mouse:{result[0]}"
                    )
                ):
                    return 1
        return self.user32.CallNextHookEx(None, code, wparam, lparam)

    @staticmethod
    def key_name(vk):
        if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
            return chr(vk)
        if 0x70 <= vk <= 0x87:
            return f"F{vk - 0x6F}"
        return {
            0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter",
            0x14: "Caps Lock", 0x1B: "Esc", 0x20: "Space",
            0x21: "Page Up", 0x22: "Page Down", 0x23: "End",
            0x24: "Home",
            0x25: "方向左", 0x26: "方向上", 0x27: "方向右",
            0x28: "方向下", 0x2C: "Print Screen", 0x2D: "Insert",
            0x2E: "Delete", 0x13: "Pause", 0x5D: "Menu",
            # Some gaming-mouse software exposes the two thumb buttons as
            # Browser Back/Forward keyboard-class HID events instead of raw
            # Interception mouse BUTTON_4/BUTTON_5 states. Normalize both forms
            # to the same source names used by the mapping/preset model.
            0x05: "鼠标侧键 1", 0x06: "鼠标侧键 2",
            0xA6: "鼠标侧键 1", 0xA7: "鼠标侧键 2",
            0xAD: "静音", 0xAE: "音量减", 0xAF: "音量加",
            0xB0: "下一曲", 0xB1: "上一曲", 0xB3: "播放/暂停",
            0xA0: "Shift", 0xA1: "Shift", 0xA2: "Ctrl",
            0xA3: "Ctrl", 0xA4: "Alt", 0xA5: "Alt",
            0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-",
            0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[",
            0xDC: "\\", 0xDD: "]", 0xDE: "'",
        }.get(vk, f"VK-{vk:02X}")

    @staticmethod
    def key_code(name):
        if len(name) == 1 and name.isalnum():
            return ord(name.upper())
        if name.startswith("F") and name[1:].isdigit():
            return 0x6F + int(name[1:])
        return {
            "Backspace": 0x08, "Tab": 0x09, "Enter": 0x0D,
            "Caps Lock": 0x14, "Esc": 0x1B, "Space": 0x20,
            "Page Up": 0x21, "Page Down": 0x22, "End": 0x23,
            "Home": 0x24,
            "方向左": 0x25, "方向上": 0x26, "方向右": 0x27,
            "方向下": 0x28, "Print Screen": 0x2C, "Insert": 0x2D,
            "Delete": 0x2E, "Pause": 0x13, "Menu": 0x5D,
            "静音": 0xAD, "音量减": 0xAE, "音量加": 0xAF,
            "下一曲": 0xB0, "上一曲": 0xB1, "播放/暂停": 0xB3,
            "Shift": 0xA0, "Ctrl": 0xA2, "Alt": 0xA4,
            ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD,
            ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB,
            "\\": 0xDC, "]": 0xDD, "'": 0xDE,
        }.get(name, 0)

    def pressed_input_snapshot(self):
        """Return current logical inputs with stable left/right source IDs.

        WH_KEYBOARD_LL does not expose a physical device number, but querying
        left/right virtual keys still prevents one modifier side from clearing
        the other side's logical state during engine/listener transitions.
        """
        candidates = [
            (0xA0, "Shift"), (0xA1, "Shift"),
            (0xA2, "Ctrl"), (0xA3, "Ctrl"),
            (0xA4, "Alt"), (0xA5, "Alt"),
            (0x01, "鼠标左键"), (0x02, "鼠标右键"),
            (0x04, "鼠标中键"), (0x05, "鼠标侧键 1"),
            (0x06, "鼠标侧键 2"),
        ]
        seen_codes = {code for code, _name in candidates}
        for name in KEY_NAMES:
            if name in ("Ctrl", "Shift", "Alt"):
                continue
            code = self.key_code(name)
            if code and code not in seen_codes:
                candidates.append((code, name))
                seen_codes.add(code)

        pressed = []
        for code, name in candidates:
            try:
                if self.user32.GetAsyncKeyState(code) & 0x8000:
                    prefix = "win:mouse" if name in MOUSE_NAMES else "win:kbd"
                    pressed.append((name, f"{prefix}:{code:02X}"))
            except (OSError, ValueError):
                continue
        return pressed

    def send(self, name, down):
        with self.output_lock:
            if not name:
                return False
            mouse = {
                "鼠标左键": (0x0002, 0x0004, 0),
                "鼠标右键": (0x0008, 0x0010, 0),
                "鼠标中键": (0x0020, 0x0040, 0),
                "鼠标侧键 1": (0x0080, 0x0100, 1 << 16),
                "鼠标侧键 2": (0x0080, 0x0100, 2 << 16),
            }.get(name)
            if mouse:
                flags = mouse[0] if down else mouse[1]
                item = INPUT(
                    type=0,
                    mi=MOUSEINPUT(
                        0, 0, mouse[2], flags, 0, self.INJECTION_TAG
                    ),
                )
            else:
                key = self.key_code(name)
                if not key:
                    return False
                scan_code = self.user32.MapVirtualKeyW(key, self.MAPVK_VK_TO_VSC)
                if not scan_code:
                    return False
                flags = self.KEYEVENTF_SCANCODE
                if not down:
                    flags |= self.KEYEVENTF_KEYUP
                if name in ("方向左", "方向右", "方向上", "方向下", "Delete"):
                    flags |= self.KEYEVENTF_EXTENDEDKEY
                item = INPUT(
                    type=1,
                    ki=KEYBDINPUT(
                        0, scan_code, flags, 0, self.INJECTION_TAG
                    ),
                )
            sent = self.user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(item))
            return sent == 1

    def click(self, name, hold_ms=35):
        with self.output_lock:
            self.send(name, True)
            time.sleep(max(1, hold_ms) / 1000)
            self.send(name, False)

    def send_combo(self, modifiers, key, down):
        with self.output_lock:
            names = modifier_names(modifiers)
            if down:
                for modifier in names:
                    self.send(modifier, True)
                self.send(key, True)
            else:
                self.send(key, False)
                for modifier in reversed(names):
                    self.send(modifier, False)

    def click_combo(self, modifiers, key, hold_ms=35):
        with self.output_lock:
            self.send_combo(modifiers, key, True)
            time.sleep(max(1, hold_ms) / 1000)
            self.send_combo(modifiers, key, False)

    def wheel(self, delta):
        item = INPUT(
            type=0,
            mi=MOUSEINPUT(
                0, 0, ctypes.c_uint32(delta).value, 0x0800, 0,
                self.INJECTION_TAG,
            ),
        )
        return self.user32.SendInput(
            1, ctypes.byref(item), ctypes.sizeof(item)
        ) == 1

    def move(self, x, y):
        width = max(1, self.user32.GetSystemMetrics(0) - 1)
        height = max(1, self.user32.GetSystemMetrics(1) - 1)
        item = INPUT(
            type=0,
            mi=MOUSEINPUT(
                int(x * 65535 / width), int(y * 65535 / height), 0,
                0x0001 | 0x8000, 0, self.INJECTION_TAG,
            ),
        )
        return self.user32.SendInput(
            1, ctypes.byref(item), ctypes.sizeof(item)
        ) == 1

    def force_release_all(self, include_mouse=False):
        """Best-effort OS-level recovery for keys left down by a stopped backend."""
        # Release both left and right variants. The normal name mapping only uses
        # left modifiers, which was insufficient for recovering a stuck Alt state.
        virtual_keys = [
            0xA0, 0xA1,  # L/R Shift
            0xA2, 0xA3,  # L/R Ctrl
            0xA4, 0xA5,  # L/R Alt
            0x5B, 0x5C,  # L/R Windows
        ]
        virtual_keys.extend(
            code for code in (self.key_code(name) for name in KEY_NAMES)
            if code and code not in virtual_keys
        )
        for key in virtual_keys:
            scan_code = self.user32.MapVirtualKeyW(
                key, self.MAPVK_VK_TO_VSC
            )
            if not scan_code:
                continue
            flags = self.KEYEVENTF_SCANCODE | self.KEYEVENTF_KEYUP
            if key in (
                0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                0x2D, 0x2E, 0x5B, 0x5C, 0xA3, 0xA5,
            ):
                flags |= self.KEYEVENTF_EXTENDEDKEY
            item = INPUT(
                type=1,
                ki=KEYBDINPUT(
                    0, scan_code, flags, 0, self.INJECTION_TAG
                ),
            )
            self.user32.SendInput(
                1, ctypes.byref(item), ctypes.sizeof(item)
            )
        if include_mouse:
            for name in MOUSE_NAMES:
                self.send(name, False)

    def force_release_names(self, names, attempts=2, only_if_down=True):
        """Release a bounded set of configured output targets.

        This recovery path does not depend on task/backend ownership tables, so
        it can repair a Down whose bookkeeping was lost.  It never emits Down
        and never touches mouse buttons absent from ``names``.
        """
        candidates = list(dict.fromkeys(
            str(name) for name in (names or [])
            if str(name) in INPUT_NAMES
        ))
        mouse_vks = {
            "鼠标左键": 0x01,
            "鼠标右键": 0x02,
            "鼠标中键": 0x04,
            "鼠标侧键 1": 0x05,
            "鼠标侧键 2": 0x06,
        }
        # A bare MouseUp can itself be visible (for example opening a context
        # menu). Only repair targets Windows currently considers logically down.
        pending = []
        for name in candidates:
            if not only_if_down:
                pending.append(name)
                continue
            vk = mouse_vks.get(name) or self.key_code(name)
            try:
                is_down = bool(vk and self.user32.GetAsyncKeyState(vk) & 0x8000)
            except (OSError, ValueError, AttributeError):
                is_down = True
            if is_down:
                pending.append(name)
        if not pending:
            return True
        for attempt in range(max(1, int(attempts))):
            failed = []
            for name in pending:
                if not self.send(name, False):
                    failed.append(name)
            pending = failed
            if not pending:
                return True
            if attempt + 1 < max(1, int(attempts)):
                time.sleep(0.015)
        return False
