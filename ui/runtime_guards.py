"""Shared guards for runtime output cleanup and UI operation re-entry."""

from core.constants import MacroState


def runtime_transaction_busy(owner):
    """Return True while runtime/apply/loading/shutdown transactions are active."""
    return bool(
        getattr(owner, "_shutdown_started", False)
        or getattr(owner, "_runtime_operation_active", False)
        or getattr(owner, "_config_apply_transaction_active", False)
        or getattr(owner, "loading_task_stack", [])
    )


def macro_control_transaction_busy(owner):
    """Return True when current-macro UI controls must not enter task state changes."""
    return bool(
        runtime_transaction_busy(owner)
        or getattr(owner, "macro_state", None) in (
            MacroState.STOPPING,
            MacroState.STOP_TIMEOUT,
        )
    )


def pending_quarantined_mouse_release_names(owner):
    """Return mouse buttons that are still waiting for a safe MouseUp."""
    names = []
    output = getattr(owner, "interception_output", None)
    if output is not None:
        try:
            summary = output.pending_release_summary()
            names.extend(summary.get("quarantined_mouse", []) or [])
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass

    lock = getattr(owner, "quarantined_mouse_release_lock", None)
    entries = getattr(owner, "quarantined_mouse_releases", [])
    try:
        if lock is None:
            snapshot = list(entries)
        else:
            with lock:
                snapshot = list(entries)
        for item in snapshot:
            action = item.get("action", {}) if isinstance(item, dict) else {}
            names.append(str(action.get("target") or "鼠标按键"))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return sorted(set(str(name) for name in names if str(name)))


def runtime_cleanup_blocks_new_output(owner):
    """Return True while an unfinished cleanup must block new triggers/tasks."""
    controller = getattr(owner, "macro_controller", None)
    controller_failures = list(getattr(
        controller, "last_release_failures", []
    ) or [])
    lock = getattr(controller, "lock", None)
    tasks = getattr(controller, "tasks", {}) if controller is not None else {}
    if lock is not None:
        with lock:
            items = list(tasks.items())
    else:
        items = list(getattr(tasks, "items", lambda: [])())
    for preset_id, task in items:
        if getattr(task, "release_cleanup_failed", False):
            text = str(
                preset_id
                or getattr(task, "preset", {}).get("id")
                or "宏任务"
            )
            if text not in controller_failures:
                controller_failures.append(text)
    if controller_failures:
        remember = getattr(owner, "_remember_macro_cleanup_failure", None)
        if callable(remember):
            remember(
                "宏任务释放失败",
                [f"宏任务释放失败({len(controller_failures)})"],
            )
        else:
            owner.last_macro_release_failures = list(controller_failures)
            owner.output_shutdown_in_progress = True
        return True
    if getattr(owner, "last_macro_release_failures", None):
        return True
    if pending_quarantined_mouse_release_names(owner):
        return True
    if bool(getattr(owner, "output_shutdown_in_progress", False)):
        return True
    return getattr(owner, "macro_state", None) == MacroState.STOP_TIMEOUT


def explain_runtime_cleanup_block(owner, context="runtime_trigger"):
    failures = list(getattr(owner, "last_macro_release_failures", []) or [])
    quarantined_mouse = pending_quarantined_mouse_release_names(owner)
    for name in quarantined_mouse:
        text = f"鼠标按键等待安全释放：{name}"
        if text not in failures:
            failures.append(text)
    if not failures:
        failures = ["输出清理仍在进行"]
    if hasattr(owner, "write_diagnostic"):
        owner.write_diagnostic(
            "runtime_output_blocked",
            context=context,
            failures=failures,
            quarantined_mouse=quarantined_mouse,
            output_shutdown=bool(getattr(owner, "output_shutdown_in_progress", False)),
            macro_state=str(getattr(
                getattr(owner, "macro_state", None),
                "name",
                getattr(owner, "macro_state", ""),
            )),
        )
    return failures
