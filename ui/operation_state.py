"""Single read model for mutually exclusive UI/runtime operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from core.constants import MacroState


@dataclass(frozen=True)
class OperationSnapshot:
    key: str
    label: str
    blocks_runtime_entry: bool = False
    blocks_bulk_edit: bool = False

    def to_dict(self):
        return asdict(self)


def operation_state_snapshot(owner):
    """Return the highest-priority operation currently owning the UI.

    This is deliberately a read model. Existing workflows remain responsible
    for their own transitions, while every new entry point can make the same
    decision about re-entry and conflicting edits.
    """
    macro_state = getattr(owner, "macro_state", None)
    if getattr(owner, "_shutdown_started", False):
        return OperationSnapshot("shutdown", "正在退出", True, True)
    if getattr(owner, "loading_task_stack", []):
        return OperationSnapshot("loading", "正在载入或整理数据", True, True)
    if getattr(owner, "_config_apply_transaction_active", False):
        return OperationSnapshot("applying", "正在应用配置", True, True)
    if getattr(owner, "_runtime_operation_active", False):
        return OperationSnapshot("runtime_change", "正在切换输入引擎", True, True)
    if getattr(owner, "recording_session_active", False):
        if getattr(owner, "recording", False) or macro_state == MacroState.RECORDING:
            return OperationSnapshot("recording", "正在录制动作", True, True)
        return OperationSnapshot("recording_countdown", "录制会话准备中", True, True)
    if (
        macro_state == MacroState.COUNTDOWN
        and str(getattr(owner, "_test_countdown_preset_id", "") or "")
    ):
        return OperationSnapshot("test_countdown", "测试倒计时中", True, False)
    if macro_state in (MacroState.STOPPING, MacroState.STOP_TIMEOUT):
        return OperationSnapshot("stopping", "正在停止并释放输入", True, True)
    if getattr(owner, "profile_switch_confirmation_active", False):
        return OperationSnapshot("profile_switch", "正在切换配置档案", True, True)
    if getattr(owner, "settings_dialog_active", False):
        return OperationSnapshot("settings", "设置窗口正在修改输入状态", True, True)
    if macro_state == MacroState.PAUSED:
        return OperationSnapshot("paused", "宏任务已暂停")
    if macro_state == MacroState.RUNNING:
        return OperationSnapshot("running", "宏任务执行中")
    return OperationSnapshot("idle", "空闲")


def operation_blocks(owner, command):
    snapshot = operation_state_snapshot(owner)
    if command == "runtime_entry":
        return snapshot.blocks_runtime_entry, snapshot
    if command == "bulk_edit":
        return snapshot.blocks_bulk_edit, snapshot
    if command == "diagnostic_export":
        return snapshot.key in {"shutdown", "loading", "applying"}, snapshot
    return False, snapshot
