import ctypes
import os
from ctypes import wintypes


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsKillOnCloseJob:
    """Tie a child process to the lifetime of the owning GUI process."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    def __init__(self):
        self.handle = None
        self.last_error = ""
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            self.last_error = self._format_last_error("创建 Job Object 失败")
            return
        info = _JobExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle, self.JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            self.last_error = self._format_last_error(
                "设置 Job Object 自动终止规则失败"
            )
            kernel32.CloseHandle(handle)
            return
        self.handle = handle

    @staticmethod
    def _format_last_error(prefix):
        code = int(ctypes.get_last_error() or ctypes.windll.kernel32.GetLastError())
        if not code:
            return prefix
        try:
            detail = ctypes.FormatError(code).strip()
        except (OSError, ValueError):
            detail = ""
        return f"{prefix}（Windows 错误 {code}{'：' + detail if detail else ''}）"

    def assign(self, process):
        if not self.handle:
            if not self.last_error:
                self.last_error = "Job Object 未创建"
            return False
        if not process or not getattr(process, "_handle", None):
            self.last_error = "Kanata 进程句柄无效"
            return False
        try:
            ok = bool(ctypes.windll.kernel32.AssignProcessToJobObject(
                self.handle, wintypes.HANDLE(int(process._handle))
            ))
        except (AttributeError, OSError, TypeError, ValueError) as error:
            self.last_error = f"绑定 Kanata 进程到 Job Object 失败：{error}"
            return False
        if not ok:
            self.last_error = self._format_last_error(
                "绑定 Kanata 进程到 Job Object 失败"
            )
            return False
        self.last_error = ""
        return True

    def close(self):
        if self.handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except (OSError, ValueError):
                pass
            self.handle = None
