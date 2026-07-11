import ctypes
import os
import time
from ctypes import wintypes


TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _foreground_hwnd(user32):
    hwnd = user32.GetForegroundWindow()
    if hwnd:
        return hwnd
    # GetForegroundWindow can transiently return NULL during activation. Query
    # the active GUI thread as a second source before giving up on ownership.
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    try:
        if user32.GetGUIThreadInfo(0, ctypes.byref(info)):
            return info.hwndActive or info.hwndFocus
    except (AttributeError, OSError):
        pass
    return 0


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _process_name_from_snapshot(pid):
    """Read an executable name without opening the target process.

    This remains usable for many elevated or protected game windows where
    OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) is denied.
    """
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (0, INVALID_HANDLE_VALUE):
        return ""
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return ""
        while True:
            if int(entry.th32ProcessID) == int(pid):
                return str(entry.szExeFile or "")
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return ""


def foreground_window_context():
    """Return (process executable name, window title) for the foreground window."""
    identity = foreground_window_identity()
    return identity.get("process", ""), identity.get("title", "")


def foreground_window_identity():
    """Return stable foreground-window identity for safe output cleanup."""
    if os.name != "nt":
        return {
            "hwnd": 0, "pid": 0, "process": "", "title": "",
            "captured_at": time.monotonic(),
        }

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = _foreground_hwnd(user32)
    if not hwnd:
        return {
            "hwnd": 0, "pid": 0, "process": "", "title": "",
            "captured_at": time.monotonic(),
        }

    length = user32.GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return {
            "hwnd": int(hwnd), "pid": 0,
            "process": "", "title": title_buffer.value,
            "captured_at": time.monotonic(),
        }

    process_name = ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if handle:
        try:
            size = wintypes.DWORD(32768)
            path_buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, path_buffer, ctypes.byref(size)
            ):
                process_name = os.path.basename(path_buffer.value)
        finally:
            kernel32.CloseHandle(handle)

    if not process_name:
        process_name = _process_name_from_snapshot(pid.value)

    return {
        "hwnd": int(hwnd),
        "pid": int(pid.value),
        "process": process_name,
        "title": title_buffer.value,
        "captured_at": time.monotonic(),
    }


def foreground_window_identity_matches(identity):
    """Return True when the current foreground window is the recorded window."""
    if not isinstance(identity, dict):
        return True
    if identity.get("_unstable"):
        # MouseDown itself can activate a window, so sampling immediately before
        # and after the send may legitimately produce two HWNDs.  Keeping that
        # state permanently quarantined leaves the OS button down forever.  A
        # release is safe when the foreground is exactly either sampled window;
        # unrelated windows remain protected by the same exact-HWND rule below.
        candidates = [
            candidate for candidate in (
                identity.get("before"), identity.get("after")
            )
            if isinstance(candidate, dict)
            and (candidate.get("hwnd") or candidate.get("pid"))
        ]
        return bool(
            candidates
            and any(foreground_window_identity_matches(item) for item in candidates)
        )
    expected_hwnd = int(identity.get("hwnd") or 0)
    expected_pid = int(identity.get("pid") or 0)
    if not expected_hwnd and not expected_pid:
        if not identity.get("_mouse_origin"):
            return True
        current = foreground_window_identity()
        age = time.monotonic() - float(
            identity.get("captured_at") or time.monotonic()
        )
        # Exact ownership is unavailable. Permit only the matching short-lived
        # input edge while the observable context is still unchanged; otherwise
        # retain quarantine for explicit recovery rather than forever losing the
        # corresponding MouseUp.
        return bool(
            age <= 0.75
            and not current.get("hwnd")
            and not current.get("pid")
            and str(current.get("process") or "").casefold()
            == str(identity.get("process") or "").casefold()
            and str(current.get("title") or "")
            == str(identity.get("title") or "")
        )
    current = foreground_window_identity()
    # HWND is the authoritative identity. Falling back to PID after an HWND
    # mismatch would allow a dialog/new window in the same process to receive
    # the old window's MouseUp.
    if expected_hwnd:
        return int(current.get("hwnd") or 0) == expected_hwnd
    return bool(
        expected_pid and int(current.get("pid") or 0) == expected_pid
    )


def foreground_window_belongs_to_current_process():
    """Return True when the foreground window is owned by this process."""
    if os.name != "nt":
        return False

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False
    return int(pid.value) == int(kernel32.GetCurrentProcessId())
