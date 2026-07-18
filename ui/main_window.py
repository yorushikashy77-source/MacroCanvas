from __future__ import annotations

import atexit
import copy
from collections import deque
import ctypes
import json
import os
import queue
import random
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import weakref
try:
    import winsound
except ImportError:
    class _WinSoundFallback:
        SND_ALIAS = 0
        SND_ASYNC = 0

        @staticmethod
        def PlaySound(*_args, **_kwargs):
            return None

    winsound = _WinSoundFallback()
from ctypes import wintypes
from enum import Enum

from PySide6.QtCore import QEvent, QSize, QTimer, Qt, Signal, Slot, QUrl
from PySide6.QtGui import (
    QColor, QCursor, QDesktopServices, QFont, QPainter, QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox,
    QComboBox, QFrame, QHBoxLayout, QHeaderView,
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QScrollArea, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from config.schema import (
    MAX_CONFIG_FILE_BYTES, repair_duplicate_action_tree_ids,
    repair_duplicate_runtime_ids, repair_overlapping_loop_controls,
    validate_config_payload,
)
from config.storage import (
    atomic_write_text, write_deduplicated_snapshot,
)
from config.transfer import remap_action_ids
from config.profiles import (
    BASE_LAYER_NAME, DISABLED_LAYER_NAME,
    normalize_profile, profile_layer_name, profile_namespace, profile_payload,
    select_profile,
)
from core.constants import *
from engine.process_guard import WindowsKillOnCloseJob
from engine.interception_types import (
    INTERCEPTION_KEY_STROKE, INTERCEPTION_MOUSE_STROKE,
)
from engine.kanata import (
    KanataConfigBuilder, KanataEngine,
    interception_keyboard_hwids, interception_mouse_hwids,
)
from engine.input_backend import (
    POINT, InterceptionInputHook, InterceptionOutput, WinInput,
)
from engine.window_context import (
    foreground_window_belongs_to_current_process,
    foreground_window_context,
)
from engine.trigger_resolver import (
    MODIFIER_ORDER, combo_text, modifier_names, normalize_input_name,
)
from macro.actions import clone_action_tree, iter_action_tree
from macro.scheduler import MacroController, MacroSignals, MacroTask
from macro.recording import simplify_recorded_actions
from ui.overlays import ActivityOverlay
from ui.profile_manager import ProfileManagerDialog
from ui.editors import (
    ActionDurationEditor, ActionTargetEditor, ActionTreeWidget, HotkeyEdit,
)
from ui.styles import STYLESHEET
from ui.config_persistence import ConfigPersistenceMixin
from ui.configuration_transfer import ConfigurationTransferMixin
from ui.action_execution import ActionExecutionMixin
from ui.engine_configuration import EngineConfigurationMixin
from ui.hotkey_settings import HotkeySettingsMixin
from ui.loading_coordinator import LoadingCoordinatorMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin
from ui.shutdown_coordinator import ShutdownCoordinatorMixin
from ui.macro_controls import MacroControlsMixin
from ui.profile_workflow import ProfileWorkflowMixin
from ui.input_listener_lifecycle import InputListenerLifecycleMixin
from ui.input_runtime import InputRuntimeMixin
from ui.recording_workflow import RecordingWorkflowMixin
from ui.mapping_editor import MappingEditorMixin
from ui.preset_editor import PresetEditorMixin
from ui.editor_workflow import EditorWorkflowMixin
from ui.runtime_diagnostics import RuntimeDiagnosticsMixin
from ui.trigger_conflicts import TriggerConflictMixin
from ui.widget_behaviors import WheelEditBlocker
from ui.catalog_tools import CatalogToolsMixin
from ui.system_tray import SystemTrayMixin


class MainWindow(
    ConfigPersistenceMixin,
    ConfigurationTransferMixin,
    ActionExecutionMixin,
    EngineConfigurationMixin,
    RuntimeDiagnosticsMixin,
    TriggerConflictMixin,
    HotkeySettingsMixin,
    LoadingCoordinatorMixin,
    RuntimeLifecycleMixin,
    ShutdownCoordinatorMixin,
    SystemTrayMixin,
    MacroControlsMixin,
    ProfileWorkflowMixin,
    InputListenerLifecycleMixin,
    InputRuntimeMixin,
    RecordingWorkflowMixin,
    CatalogToolsMixin,
    MappingEditorMixin,
    PresetEditorMixin,
    EditorWorkflowMixin,
    QMainWindow,
):
    global_input_signal = Signal(str, bool)
    recorded_event_signal = Signal(dict)
    emergency_signal = Signal()
    recording_stop_signal = Signal()
    recording_cancel_signal = Signal()
    recording_restore_signal = Signal()
    global_toggle_signal = Signal(bool)
    macro_pause_signal = Signal()
    feedback_signal = Signal(str)
    recorded_mouse_context_mismatch_signal = Signal(dict)
    kanata_trigger_signal = Signal(str, str, str, str)
    kanata_state_signal = Signal(str, bool)
    kanata_control_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self._initialize_system_tray_state()
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 800)
        self.setMinimumSize(760, 500)

        # 全程序统一禁用滚轮直接修改 QComboBox / QSpinBox。
        # 事件过滤器安装在 QApplication 上，因此后续创建的设置窗口、
        # 动作编辑器和配置档案窗口也会自动遵守这一规则。
        self.wheel_edit_blocker = WheelEditBlocker(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self.wheel_edit_blocker)
        self.running = False
        # Startup repair may produce a valid in-memory configuration without
        # replacing the source file.  Keep that state explicitly dirty until
        # the user applies it; otherwise the repaired IDs are regenerated on
        # every launch while the UI incorrectly says that everything is saved.
        self.startup_recovery_pending_save = False
        # During stop/cleanup, reject any new Press/Tap output while still
        # allowing Release packets to pass through the live backend.
        self.output_shutdown_in_progress = False
        # Serialize every output send against stop/profile-transition cleanup.
        # The gate alone is insufficient when a callback passed the check just
        # before a foreground switch and sends its Down after cleanup completed.
        self.output_dispatch_lock = threading.RLock()
        self._stop_release_guard_generation = 0
        self._macro_stop_gate_restore = None
        self._deferred_profile_input_restore = None
        # 前台切换、设置窗口等临时隔离输入时，只暂停原本正在运行的宏。
        # 单独记录这些任务，恢复输入时不会误把用户手动暂停的任务继续运行。
        self._profile_input_paused_macro_ids = set()
        # “暂停/继续全部宏”只恢复自己曾经暂停的宏，不接管用户单独暂停的任务。
        self._global_pause_macro_ids = set()
        self.profile_input_temporarily_suspended = False
        self.profile_input_suspend_reason = ""
        # 进程保护的输入隔离要独立于“暂停宏任务”账本。确认中断后，
        # stop_all_macros 会清空暂停账本，但输入层仍需等回到目标进程再恢复。
        self._process_guard_input_suspended = False
        self._process_guard_suspended_profile_ids = set()
        self.data_lock = threading.Lock()
        self.cached_mappings = []
        self.cached_presets = []
        self.runtime_mappings = []
        self.runtime_presets = []
        # One applied trigger table. Presets are converted to the exact same
        # source/source_modifiers/mode structure as basic mappings when the
        # runtime snapshot is created. The Interception callback therefore has
        # no separate preset matching branch.
        self.runtime_trigger_rules = []
        self.runtime_shadow_warning_last = {}
        # Presets being deleted are blocked before the confirmation dialog opens.
        # Keep this separate gate so a rule copied by the Interception thread just
        # before runtime-table removal still cannot start after deletion begins.
        self.suspended_preset_ids = set()
        self.suspended_mapping_ids = set()
        self.pending_mapping_deletions = {}
        self.pending_preset_deletions = {}
        # Direct Kanata mappings cannot be removed from a live layer. If one
        # is deleted, the engine is stopped safely and should resume only after
        # the edited configuration has been validated and committed.
        self.restart_engine_after_apply = False
        self._config_apply_transaction_active = False
        self.action_clipboard = []
        self.action_history_limit = 100
        self.profiles = []
        self.profile_auto_switch_enabled = True
        # 主界面的映射和预设编辑区始终对应 editor_profile_id。
        # 基础配置单独保存在 base_profile_payload 中，避免切换到档案后
        # 把档案内容误写回顶层基础配置。
        self.base_profile_payload = {"mappings": [], "presets": []}
        self.editor_profile_id = ""
        # None 只表示启动阶段尚未载入任何表单；空字符串是“基础配置”的有效 ID。
        # 这样下拉框显示状态不会把基础配置误当成“缺失值”。
        self.editor_loaded_profile_id = None
        self.editor_loaded_payload = {"mappings": [], "presets": []}
        # Invalidates queued combo-box commits when a newer selection arrives.
        self._profile_selector_change_generation = 0
        self._last_profile_switch_error = ""
        self.active_profile_id = ""
        self.active_profile_layer = BASE_LAYER_NAME
        self.runtime_profile_catalog = {}
        self.runtime_profiles = []
        self.runtime_profile_auto_switch_enabled = True
        # Keep release targets from profiles that have already been active.
        # A foreground switch replaces runtime_trigger_rules immediately, so a
        # later engine stop must not depend only on the newly selected profile.
        self.runtime_release_target_history = set()
        self.runtime_release_vkey_history = set()
        self.profile_trigger_allowed = True
        self.profile_switch_in_progress = False
        self.macrocanvas_foreground_suspended = False
        self.macrocanvas_foreground_suspend_failed = False
        # 运行档案切换先进入 disabled 层并释放旧输出；前台匹配还会经过
        # 一个很短的稳定窗口，避免临时覆盖层或焦点抖动反复切换档案。
        self.settings_input_mode_active = False
        # 前台档案始终以实际前台窗口为准。编辑器中的手动选择只影响表单，
        # 不再把某个运行档案无限保持到其他进程中。
        self.last_foreground_profile_context = None
        self.foreground_profile_candidate = None
        self.foreground_profile_candidate_since = 0.0
        self.foreground_profile_candidate_hits = 0
        # 候选前台尚未稳定时，旧档案输入层已经被暂停。该标记用于在
        # 候选取消、回到原档案或切换失败时恢复实际 Kanata 层。
        self.foreground_candidate_input_suspended = False
        self.foreground_profile_stable_seconds = 0.24
        self.runtime_debug_enabled = False
        self.runtime_debug_events = deque(maxlen=500)
        self.runtime_debug_lock = threading.RLock()
        self.runtime_debug_sequence = 0
        self.runtime_debug_dialog = None
        self.engine = KanataEngine()
        self.engine.set_message_callback(self._receive_kanata_message)
        self.keyboard_engine = KanataEngine(
            KANATA_KEYBOARD_CONFIG_PATH,
            KANATA_KEYBOARD_LOG_PATH,
            "keyboard",
        )
        self.keyboard_engine.set_message_callback(self._receive_kanata_message)
        self.interception_output = None
        self.quarantined_mouse_releases = []
        self.quarantined_mouse_release_lock = threading.RLock()
        # MacroTask always calls _send_output_action. In game mode both basic
        # mappings and presets are sourced directly by Interception and every
        # final key/mouse action is emitted by InterceptionOutput.
        self.macro_controller = MacroController(
            self.engine,
            self._expect_kanata_action_events,
            self._send_output_action,
            self._macro_backend_active,
            self._quarantine_mouse_release,
        )
        self.macro_controller.profile_active = self._foreground_matches_profile
        self.active_macro_id = None
        # 物理输入状态必须在任何输入后端启动前就存在。游戏模式首次启动时
        # 不会经过 start_global_hook()，而启动按钮、Interception 回调和
        # 前台档案定时器都会访问该集合。
        # GUI 清理流程和输入监听线程会同时访问以下路由状态。所有成组的
        # 读取/修改都通过同一把锁完成，避免档案切换恰好夹在 Down/Up 之间。
        self.input_state_lock = threading.RLock()
        self.physical_down = set()
        self.physical_modifiers = set()
        # source_id -> logical input name. Interception supplies device-aware
        # IDs and WinInput supplies left/right modifier IDs. The public config
        # model still uses the existing logical names.
        self.physical_input_sources = {}
        self.macro_controller.condition_state = (
            self._macro_action_condition_satisfied
        )
        self.held_trigger_ids = {}
        self.kanata_trigger_down = set()
        # 同步映射由 Kanata 虚拟键 Press/Release 保持。相同输出使用引用
        # 计数，避免多个来源同时按住时其中一个松开就提前释放目标。
        self.active_sync_by_source = {}
        self.sync_output_counts = {}
        self.sync_output_lock = threading.RLock()
        # wintercept 输出有时不会携带 Windows 的 INJECTED 标志。这里登记
        # 即将由 Kanata 返回的事件，使它们通过钩子而不再次触发映射。
        self.expected_kanata_events = []
        self.expected_kanata_event_lock = threading.RLock()
        self.expected_kanata_event_last_prune = 0.0
        self.expected_kanata_event_limit = 128
        # Interception 必须保证一次物理输入的 Down/Up 使用同一条吞放路径。
        # 前台档案可能在两者之间切换，因此另行记录已经放行给系统的 Down；
        # 它对应的 Up 绝不能再按新档案规则被吞掉。
        self.suppressed_trigger_names = set()
        self.interception_forwarded_down = set()
        self.mapping_cards = []
        self.preset_cards = []
        self.selected_preset_card = None
        self.action_table = None
        self.action_title = None
        self.recording = False
        self.recording_cancelled = False
        self.recorded_events = []
        self.recording_options = {}
        self.recording_started_at = 0.0
        self.recording_finished_at = 0.0
        self.last_recorded_move = 0.0
        self.recording_pending_move = None
        self.recording_move_origin = {}
        self.recording_recent_events = {}
        self.recording_limit_reason = ""
        self.recording_limit_stop_requested = False
        self.recording_generation = 0
        self.recording_lock = threading.RLock()
        self.recording_target_card = None
        self.recording_insert_context = None
        self.recording_restore_layer = None
        self.recording_restore_pending = False
        self.recording_workflow_complete = False
        self.global_hook = None
        self.interception_input_hook = None
        self.interception_input_control_only = False
        # 关闭业务引擎后仍由同一个 Interception 输入 context 监听开关。
        # 控制态使用独立修饰键集合，避免档案切换清理运行态物理状态时
        # 破坏“重新开启引擎”组合键的识别。
        self.interception_control_modifiers = set()
        self.interception_control_sources = {}
        self.interception_record_mouse_move = False
        # Plain Python state used by macro worker threads and runtime listeners;
        # do not read Qt widgets from those paths merely to determine the active
        # backend.  The editable backend combo can already show an unapplied
        # candidate while control listeners must still follow the last applied
        # snapshot.
        self.runtime_engine_backend = ""
        self.direct_interception_active = False
        self.global_toggle_enabled = True
        self.global_toggle_modifiers = "Ctrl+Shift"
        self.global_toggle_key = "F10"
        self.macro_pause_enabled = True
        self.macro_pause_modifiers = "Ctrl"
        self.macro_pause_key = "F9"
        self.emergency_modifiers = "无"
        self.emergency_key = "F8"
        self.recording_cancel_modifiers = "无"
        self.recording_cancel_key = "F7"
        self.recording_finish_modifiers = "无"
        self.recording_finish_key = "F8"
        # Editable settings stay pending until “应用更改”.
        self.runtime_global_toggle_enabled = True
        self.runtime_global_toggle_modifiers = "Ctrl+Shift"
        self.runtime_global_toggle_key = "F10"
        self.runtime_macro_pause_enabled = True
        self.runtime_macro_pause_modifiers = "Ctrl"
        self.runtime_macro_pause_key = "F9"
        self.runtime_emergency_modifiers = "无"
        self.runtime_emergency_key = "F8"
        self.runtime_recording_cancel_modifiers = "无"
        self.runtime_recording_cancel_key = "F7"
        self.runtime_recording_finish_modifiers = "无"
        self.runtime_recording_finish_key = "F8"
        self.global_toggle_latched = False
        self.system_hotkey_latched = set()
        self.system_hotkey_latched_sources = {}
        self.recording_control_modifiers = set()
        self.recording_control_sources = {}
        self.global_toggle_latched_source = None
        self.mappings_enabled = True
        self.diagnostic_enabled = False
        self.runtime_diagnostic_enabled = False
        self.diagnostic_lock = threading.RLock()
        self.diagnostic_queue = queue.Queue(maxsize=4096)
        self.diagnostic_writer_stop = threading.Event()
        self.diagnostic_writer_thread = None
        self.diagnostic_generation = 0
        self.diagnostic_dropped_count = 0
        self.diagnostic_session_id = ""
        self.diagnostic_write_count = 0
        self.settings_dialog_active = False
        self.profile_switch_confirmation_active = False
        self._shutdown_started = False
        self._shutdown_in_progress = False
        self._shutdown_complete = False
        self._shutdown_errors = []
        self._runtime_operation_active = False
        self.output_backend_retired = False
        # 统一的程序内加载遮罩。使用主窗口/动作窗口内部的卡片式动画，
        # 不创建独立的 Windows 进度窗口。支持嵌套任务和阶段文字更新。
        self.profile_form_loading = False
        self.loading_task_stack = []
        self.loading_event_counter = 0
        self.loading_overlay = None
        self.initializing = True
        self.engine_state = EngineState.STOPPED
        self.config_state = ConfigState.SAVED
        self.macro_state = MacroState.IDLE
        self.macro_status_detail = ""
        # 缓存最近一次宏动作，用于暂停时同步主界面通知栏和右上角浮窗。
        self.last_action_activity = {}
        self._test_countdown_generation = 0
        self._test_countdown_preset_id = None
        self.recording_guard_profile_id = None
        self._process_guard_warning_active = False
        self._process_guard_candidate = None
        self._process_guard_candidate_since = 0.0
        self._process_guard_candidate_hits = 0
        self._recording_guard_candidate = None
        self._recording_guard_candidate_since = 0.0
        self._recording_guard_candidate_hits = 0
        self._backend_failure_handling = False
        self.input_listener_degraded_reason = ""
        self.applied_config_text = ""
        self.applied_config_signature = ""
        self.applied_config_payload = None
        self.build_ui()
        self.setup_system_tray()
        self._initialize_feedback_audio()
        self.activity_overlay = ActivityOverlay()
        self.global_input_signal.connect(self.handle_global_input)
        self.recorded_event_signal.connect(self.handle_recorded_event)
        self.emergency_signal.connect(
            lambda: self.emergency_stop(disable_mappings=False, sound=True)
        )
        self.recording_stop_signal.connect(self.finish_recording)
        self.recording_cancel_signal.connect(self.cancel_recording)
        self.recording_restore_signal.connect(
            self._complete_recording_restore_if_ready,
            Qt.ConnectionType.QueuedConnection,
        )
        self.global_toggle_signal.connect(self.handle_global_toggle_fallback)
        self.macro_pause_signal.connect(self.toggle_all_macro_pause)
        self.recorded_mouse_context_mismatch_signal.connect(
            self.on_recorded_mouse_context_mismatch,
            Qt.ConnectionType.QueuedConnection,
        )
        self.kanata_trigger_signal.connect(self.handle_kanata_trigger)
        self.kanata_state_signal.connect(self.handle_kanata_state)
        self.kanata_control_signal.connect(self.handle_kanata_control)
        self.macro_controller.signals.progress.connect(self.update_macro_progress)
        self.macro_controller.signals.action_activity.connect(
            self.update_action_activity
        )
        self.macro_controller.signals.task_finished.connect(self.on_macro_finished)
        self.macro_controller.signals.state_changed.connect(self.refresh_macro_controls)
        self.auto_apply_timer = QTimer(self)
        self.auto_apply_timer.setSingleShot(True)
        self.auto_apply_timer.setInterval(1500)
        self.auto_apply_timer.timeout.connect(self.auto_apply_config)
        self.load_config()
        self.refresh_cache()
        generated = self.generate_kanata_config()
        self.applied_config_text = (
            KANATA_CONFIG_PATH.read_text("utf-8", errors="replace")
            if generated and KANATA_CONFIG_PATH.exists() else ""
        )
        # Generating backend files normally establishes the saved baseline, but
        # a startup repair still needs an explicit Apply before its normalized
        # IDs replace the source file.  Keep "已应用" reserved for a live engine
        # that has successfully started with this configuration.
        self.config_state = (
            ConfigState.DIRTY
            if generated and self.startup_recovery_pending_save
            else (ConfigState.SAVED if generated else ConfigState.FAILED)
        )
        if generated:
            self._snapshot_runtime_config()
            self.applied_config_signature = self.current_config_signature()
            self.applied_config_payload = self.current_config_payload()
        self.reload_button.setEnabled(self.config_state == ConfigState.DIRTY)
        self.profile_timer = QTimer(self)
        # Keep detection responsive while the short candidate window prevents
        # transient overlays and title changes from repeatedly switching profiles.
        self.profile_timer.setInterval(120)
        self.profile_timer.timeout.connect(self.check_foreground_profile)
        self.profile_timer.start()
        self.process_guard_timer = QTimer(self)
        self.process_guard_timer.setInterval(120)
        self.process_guard_timer.timeout.connect(self.check_active_process_guards)
        self.process_guard_timer.start()
        self.backend_health_timer = QTimer(self)
        self.backend_health_timer.setInterval(750)
        self.backend_health_timer.timeout.connect(self.check_input_backend_health)
        self.backend_health_timer.start()
        self.initializing = False
        self.refresh_mapping_filters()
        self.refresh_preset_filters()
        self.refresh_status_ui()
        # 启动阶段不能固定安装 Windows 全局钩子。游戏模式且引擎处于
        # 关闭状态时，必须立即建立 control-only Interception context，
        # 否则独占输入游戏位于前台时，第一次全局开关快捷键可能无法收到。
        self._startup_listener_retry_count = 0
        QTimer.singleShot(0, self.initialize_startup_input_listener)

    def initialize_startup_input_listener(self):
        """按当前后端初始化启动监听，并为 Interception 提供有限重试。"""
        if (
            getattr(self, "_shutdown_started", False)
            or getattr(self, "output_backend_retired", False)
        ):
            return False
        ok = self.update_global_hook_for_backend()
        if ok:
            self.write_diagnostic(
                "startup_input_listener_ready",
                backend=self.backend_combo.currentText(),
                running=self.running,
                interception_control_only=(
                    not self.running
                    and self.runtime_global_toggle_enabled
                    and self.backend_combo.currentText()
                    == "游戏模式（Interception）"
                ),
            )
            return

        # Windows 窗口和驱动刚初始化时，Interception context 偶尔可能尚未
        # 就绪。只在启动阶段有限重试，避免形成永久重试循环或重复 context。
        game_control_expected = (
            os.name == "nt"
            and not self.running
            and self.runtime_global_toggle_enabled
            and self.backend_combo.currentText()
            == "游戏模式（Interception）"
        )
        if game_control_expected and self._startup_listener_retry_count < 3:
            self._startup_listener_retry_count += 1
            self.write_diagnostic(
                "startup_interception_listener_retry",
                attempt=self._startup_listener_retry_count,
            )
            QTimer.singleShot(400, self.initialize_startup_input_listener)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        overlay = getattr(self, "loading_overlay", None)
        if overlay is not None and overlay.isVisible():
            overlay.sync_geometry()

    def build_settings_menu(self):
        menu_bar = self.menuBar()
        menu_bar.setNativeMenuBar(False)
        file_menu = menu_bar.addMenu("文件")
        self.export_full_config_action = file_menu.addAction(
            "导出完整配置…", self.export_full_configuration
        )
        self.export_preset_action = file_menu.addAction(
            "导出当前预设…", self.export_selected_preset
        )
        file_menu.addSeparator()
        self.import_config_action = file_menu.addAction(
            "导入配置或预设…", self.import_configuration_file
        )
        file_menu.addSeparator()
        self.profile_settings_action = file_menu.addAction(
            "配置档案与自动切换…", self.open_profile_settings
        )
        settings_menu = menu_bar.addMenu("设置")
        self.global_hotkey_action = settings_menu.addAction("")
        self.global_hotkey_action.triggered.connect(
            self.open_global_hotkey_settings
        )
        self.add_system_tray_settings(settings_menu)
        self.kanata_directory_action = settings_menu.addAction(
            "设置 Kanata 组件目录…"
        )
        self.kanata_directory_action.setToolTip(
            f"当前目录：{kanata_dir()}"
        )
        self.kanata_directory_action.triggered.connect(
            self.choose_kanata_directory
        )
        settings_menu.addSeparator()
        official_components_menu = settings_menu.addMenu("官方组件下载")
        self.kanata_releases_action = official_components_menu.addAction(
            "下载 Kanata（GitHub Releases）"
        )
        self.kanata_releases_action.triggered.connect(
            lambda: self.open_external_url(KANATA_RELEASES_URL)
        )
        self.interception_releases_action = official_components_menu.addAction(
            "下载 Interception（GitHub Releases）"
        )
        self.interception_releases_action.triggered.connect(
            lambda: self.open_external_url(INTERCEPTION_RELEASES_URL)
        )
        official_components_menu.addSeparator()
        self.kanata_source_action = official_components_menu.addAction(
            "查看 Kanata 源码仓库"
        )
        self.kanata_source_action.triggered.connect(
            lambda: self.open_external_url(KANATA_GITHUB_URL)
        )
        self.interception_source_action = official_components_menu.addAction(
            "查看 Interception 源码仓库"
        )
        self.interception_source_action.triggered.connect(
            lambda: self.open_external_url(INTERCEPTION_GITHUB_URL)
        )
        settings_menu.addSeparator()
        self.diagnostic_action = settings_menu.addAction("本地诊断日志")
        self.diagnostic_action.setCheckable(True)
        self.diagnostic_action.toggled.connect(
            self.set_diagnostic_enabled
        )
        self.open_diagnostic_action = settings_menu.addAction(
            "打开诊断日志"
        )
        self.open_diagnostic_action.triggered.connect(
            self.open_diagnostic_log
        )
        self.export_diagnostic_bundle_action = settings_menu.addAction(
            "导出脱敏诊断包…"
        )
        self.export_diagnostic_bundle_action.triggered.connect(
            self.export_diagnostic_bundle
        )
        self.runtime_debug_action = settings_menu.addAction("运行调试器")
        self.runtime_debug_action.triggered.connect(self.open_runtime_debugger)
        settings_menu.addSeparator()
        self.restore_config_action = settings_menu.addAction(
            "打开备份配置表"
        )
        self.restore_config_action.triggered.connect(
            self.open_backup_config_table
        )
        self.update_global_hotkey_action_text()
        self.update_diagnostic_action_text()

    def open_external_url(self, url):
        """Open an official component page in the user's default browser."""
        if QDesktopServices.openUrl(QUrl(str(url))):
            return True
        QMessageBox.warning(
            self,
            "无法打开网页",
            f"无法调用系统浏览器打开以下地址：\n{url}",
        )
        return False
















































    def update_global_hotkey_action_text(self):
        if not hasattr(self, "global_hotkey_action"):
            return
        toggle = combo_text(
            self.global_toggle_modifiers, self.global_toggle_key
        ).replace("+", " + ")
        emergency = combo_text(
            self.emergency_modifiers, self.emergency_key
        ).replace("+", " + ")
        pause = combo_text(
            self.macro_pause_modifiers, self.macro_pause_key
        ).replace("+", " + ")
        cancel = combo_text(
            self.recording_cancel_modifiers, self.recording_cancel_key
        ).replace("+", " + ")
        finish = combo_text(
            self.recording_finish_modifiers, self.recording_finish_key
        ).replace("+", " + ")
        state = "已启用" if self.global_toggle_enabled else "已关闭"
        self.global_hotkey_action.setText(
            f"快捷键设置"
        )
        self.update_hotkey_ui_texts()

    def update_hotkey_ui_texts(self):
        emergency = combo_text(
            self.emergency_modifiers, self.emergency_key
        ).replace("+", " + ")
        if hasattr(self, "stop_all_button"):
            self.stop_all_button.setText(f"停止全部（{emergency}）")
        if hasattr(self, "recording_hint_label"):
            cancel = combo_text(
                self.recording_cancel_modifiers, self.recording_cancel_key
            ).replace("+", " + ")
            finish = combo_text(
                self.recording_finish_modifiers, self.recording_finish_key
            ).replace("+", " + ")
            self.recording_hint_label.setText(
                f"{cancel} 取消录制　·　{finish} 完成录制"
            )

    def build_ui(self):
        self.build_settings_menu()
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(32, 26, 32, 22)
        outer.setSpacing(16)

        # 顶部左侧直接显示运行状态；不再保留仅作装饰的品牌区。
        top = QHBoxLayout()
        status_box = QHBoxLayout()
        status_box.setSpacing(10)
        self.runtime_profile_status = QLabel()
        self.engine_status = QLabel()
        self.config_status = QLabel()
        self.macro_status = QLabel()
        for label in (
            self.runtime_profile_status,
            self.engine_status,
            self.config_status,
            self.macro_status,
        ):
            label.setObjectName("statusOff")
            status_box.addWidget(label)
        top.addLayout(status_box)
        top.addStretch()

        # 引擎控制移至原状态区的右侧，作为一组保持在同一行。
        engine_controls = QHBoxLayout()
        engine_controls.setSpacing(10)
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(KanataEngine.EXECUTABLES.keys())
        self.backend_combo.setToolTip(
            "普通模式无需驱动；游戏模式需要已安装 Interception 驱动"
        )
        self.backend_combo.currentTextChanged.connect(self.data_changed)
        engine_controls.addWidget(self.backend_combo)

        self.force_release_button = QPushButton("强制释放键鼠")
        self.force_release_button.setObjectName("dangerGhost")
        self.force_release_button.setToolTip(
            "停止当前宏任务并释放本程序持有的键盘和鼠标状态；输入引擎保持运行"
        )
        self.force_release_button.clicked.connect(self.force_release_held_inputs)
        engine_controls.addWidget(self.force_release_button)
        self.toggle_button = QPushButton("启动 Kanata")
        self.toggle_button.setObjectName("primary")
        self.toggle_button.clicked.connect(self.toggle_running)
        engine_controls.addWidget(self.toggle_button)
        top.addLayout(engine_controls)
        outer.addLayout(top)

        engine = QFrame()
        engine.setObjectName("card")
        row = QHBoxLayout(engine)
        row.setContentsMargins(20, 15, 20, 15)
        row.setSpacing(8)
        heading = QLabel("映射引擎：")
        heading.setObjectName("heading")
        self.engine_hint = QLabel("Kanata 引擎尚未启动")
        self.engine_hint.setObjectName("muted")
        self.engine_hint.setWordWrap(False)
        row.addWidget(heading)
        row.addWidget(self.engine_hint, 1)
        outer.addWidget(engine)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_mapping_tab(), "基础映射")
        self.tabs.addTab(self.build_preset_tab(), "预设方案")
        outer.addWidget(self.tabs, 1)

        # “自动应用 / 应用更改”属于映射配置管理，不再占用引擎控制栏。
        # 使用同一组控件，在当前打开的管理页标题栏之间移动，确保两页
        # 都能直接应用配置，同时继续复用原有保存和自动应用状态。
        self.apply_controls_widget = self.build_apply_controls_widget()
        self.tabs.currentChanged.connect(self.move_apply_controls_to_tab)
        self.move_apply_controls_to_tab(self.tabs.currentIndex())

        footer = QHBoxLayout()
        save_text = QLabel("点击“应用更改”后保存并生效")
        save_text.setObjectName("muted")
        footer.addWidget(save_text)
        footer.addStretch()
        legal = QLabel("仅用于合法的个人效率与无障碍用途")
        legal.setObjectName("muted")
        footer.addWidget(legal)
        outer.addLayout(footer)
        self.setStyleSheet(STYLESHEET)
        self.refresh_status_ui()

    def build_apply_controls_widget(self):
        holder = QWidget()
        holder.setObjectName("applyControls")
        controls = QHBoxLayout(holder)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)

        # 档案选择和“自动应用 / 应用更改”分别放入页头的两个位置，
        # 让筛选与批量操作可以位于两者之间。
        self.profile_selector_frame = QFrame()
        self.profile_selector_frame.setObjectName("parameterArea")
        profile_selector_layout = QHBoxLayout(self.profile_selector_frame)
        profile_selector_layout.setContentsMargins(8, 3, 8, 3)
        profile_selector_layout.setSpacing(6)
        profile_selector_label = QLabel("编辑配置方案")
        profile_selector_label.setObjectName("muted")
        profile_selector_layout.addWidget(profile_selector_label)
        self.profile_selector_combo = QComboBox()
        self.profile_selector_combo.setMinimumWidth(150)
        self.profile_selector_combo.setMaximumWidth(220)
        self.profile_selector_combo.setToolTip("只切换主界面正在编辑的配置档案")
        # currentIndexChanged is the authoritative selector signal.  On the
        # native Windows popup, some close/commit paths update the combo index
        # without emitting activated; listening only to activated can therefore
        # change the displayed name while leaving the editor on the old profile.
        self.profile_selector_combo.currentIndexChanged.connect(
            self.on_main_profile_index_changed
        )
        # ``activated`` also fires when the user explicitly chooses the item
        # that the combo already displays.  This repairs a pre-existing
        # name/form mismatch that currentIndexChanged alone cannot observe.
        self.profile_selector_combo.activated.connect(
            self.on_main_profile_activated
        )
        # Mouse selection is sourced from the popup row itself.  On Windows the
        # combo can repaint a clicked row without reliably delivering either of
        # its high-level commit signals; the view's clicked QModelIndex is the
        # earliest unambiguous record of what the user chose.
        self.profile_selector_combo.view().clicked.connect(
            self.on_main_profile_view_clicked
        )
        profile_selector_layout.addWidget(self.profile_selector_combo)

        self.auto_apply_checkbox = QCheckBox("自动应用")
        self.auto_apply_checkbox.setChecked(False)
        self.auto_apply_checkbox.setToolTip(
            "默认关闭；开启后停止编辑 1.5 秒再自动保存并应用"
        )
        self.auto_apply_checkbox.stateChanged.connect(
            self.on_auto_apply_changed
        )
        controls.addWidget(self.auto_apply_checkbox)

        self.reload_button = QPushButton("应用更改")
        self.reload_button.setObjectName("secondary")
        self.reload_button.setToolTip("保存当前映射和预设参数并立即生效")
        self.reload_button.clicked.connect(self.reload_engine)
        self.reload_button.setEnabled(False)
        controls.addWidget(self.reload_button)
        return holder

    @Slot(int)
    def move_apply_controls_to_tab(self, index):
        targets = (
            (
                self.profile_selector_frame,
                self.mapping_profile_host if index == 0 else self.preset_profile_host,
                "_profile_selector_host",
            ),
            (
                self.apply_controls_widget,
                self.mapping_apply_host if index == 0 else self.preset_apply_host,
                "_apply_controls_host",
            ),
        )
        for widget, target_host, host_attribute in targets:
            if widget is None:
                continue
            current_host = getattr(self, host_attribute, None)
            if current_host is target_host:
                continue
            if current_host is not None and current_host.layout() is not None:
                current_host.layout().removeWidget(widget)
            widget.setParent(target_host)
            target_host.layout().addWidget(widget)
            widget.show()
            setattr(self, host_attribute, target_host)

    @staticmethod
    def build_apply_controls_host():
        host = QWidget()
        host.setObjectName("applyControlsHost")
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        return host

    def build_mapping_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # 映射页的档案管理与筛选操作共用一行，避免重复的标题说明区
        # 挤占映射卡片的可视空间。
        header = QHBoxLayout()
        header.setSpacing(8)
        self.mapping_profile_host = self.build_apply_controls_host()
        header.addWidget(self.mapping_profile_host)
        self.mapping_search = QLineEdit()
        self.mapping_search.setPlaceholderText("搜索名称、来源、目标或执行模式…")
        self.mapping_search.setClearButtonEnabled(True)
        self.mapping_search.textChanged.connect(self.refresh_mapping_filters)
        self.mapping_search.setMinimumWidth(220)
        header.addWidget(self.mapping_search, 1)
        self.mapping_enabled_filter = QComboBox()
        self.mapping_enabled_filter.addItems(["全部状态", "已启用", "已停用"])
        self.mapping_enabled_filter.currentTextChanged.connect(
            self.refresh_mapping_filters
        )
        header.addWidget(self.mapping_enabled_filter)
        self.mapping_filter_result = QLabel("显示 0 / 0")
        self.mapping_filter_result.setObjectName("muted")
        header.addWidget(self.mapping_filter_result)
        enable_filtered = QPushButton("启用筛选项")
        enable_filtered.clicked.connect(self.enable_filtered_mappings)
        header.addWidget(enable_filtered)
        disable_filtered = QPushButton("停用筛选项")
        disable_filtered.clicked.connect(self.disable_filtered_mappings)
        header.addWidget(disable_filtered)
        self.mapping_apply_host = self.build_apply_controls_host()
        header.addWidget(self.mapping_apply_host)
        add = QPushButton("＋ 添加映射")
        add.setObjectName("primary")
        add.clicked.connect(self.add_mapping)
        header.addWidget(add)
        layout.addLayout(header)

        self.mapping_scroll = QScrollArea()
        self.mapping_scroll.setWidgetResizable(True)
        self.mapping_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.mapping_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.mapping_container = QWidget()
        self.mapping_container.setObjectName("mappingContainer")
        self.mapping_layout = QVBoxLayout(self.mapping_container)
        self.mapping_layout.setContentsMargins(2, 2, 8, 2)
        self.mapping_layout.setSpacing(7)
        self.mapping_layout.addStretch(1)
        self.mapping_scroll.setWidget(self.mapping_container)
        layout.addWidget(self.mapping_scroll, 1)
        return page

    def build_preset_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # 与基础映射页保持一致：档案管理与筛选操作共用一行。
        header = QHBoxLayout()
        header.setSpacing(8)
        self.preset_profile_host = self.build_apply_controls_host()
        header.addWidget(self.preset_profile_host)
        self.preset_search = QLineEdit()
        self.preset_search.setPlaceholderText("搜索名称、触发键或执行模式…")
        self.preset_search.setClearButtonEnabled(True)
        self.preset_search.textChanged.connect(self.refresh_preset_filters)
        self.preset_search.setMinimumWidth(220)
        header.addWidget(self.preset_search, 1)
        self.preset_enabled_filter = QComboBox()
        self.preset_enabled_filter.addItems(["全部状态", "已启用", "已停用"])
        self.preset_enabled_filter.currentTextChanged.connect(
            self.refresh_preset_filters
        )
        header.addWidget(self.preset_enabled_filter)
        self.preset_filter_result = QLabel("显示 0 / 0")
        self.preset_filter_result.setObjectName("muted")
        header.addWidget(self.preset_filter_result)
        enable_filtered = QPushButton("启用筛选项")
        enable_filtered.clicked.connect(self.enable_filtered_presets)
        header.addWidget(enable_filtered)
        disable_filtered = QPushButton("停用筛选项")
        disable_filtered.clicked.connect(self.disable_filtered_presets)
        header.addWidget(disable_filtered)
        self.preset_apply_host = self.build_apply_controls_host()
        header.addWidget(self.preset_apply_host)
        add = QPushButton("＋ 添加预设")
        add.setObjectName("primary")
        add.clicked.connect(self.add_preset)
        header.addWidget(add)
        layout.addLayout(header)

        self.preset_scroll = QScrollArea()
        self.preset_scroll.setWidgetResizable(True)
        self.preset_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.preset_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.preset_container = QWidget()
        self.preset_container.setObjectName("presetContainer")
        self.preset_layout = QVBoxLayout(self.preset_container)
        self.preset_layout.setContentsMargins(2, 2, 8, 2)
        self.preset_layout.setSpacing(7)
        self.preset_layout.addStretch(1)
        self.preset_scroll.setWidget(self.preset_container)
        layout.addWidget(self.preset_scroll, 1)

        control = QFrame()
        control.setObjectName("card")
        control_layout = QHBoxLayout(control)
        self.execution_info = QLabel("当前没有正在执行的宏")
        self.execution_info.setObjectName("muted")
        control_layout.addWidget(self.execution_info, 1)
        self.pause_button = QPushButton("暂停")
        self.pause_button.setObjectName("secondary")
        self.pause_button.clicked.connect(self.pause_or_resume_current)
        self.stop_current_button = QPushButton("停止")
        self.stop_current_button.setObjectName("dangerGhost")
        self.stop_current_button.clicked.connect(self.stop_current_macro)
        self.stop_all_button = QPushButton("停止全部（F8）")
        self.stop_all_button.setObjectName("stop")
        self.stop_all_button.clicked.connect(self.stop_all_macros)
        control_layout.addWidget(self.pause_button)
        control_layout.addWidget(self.stop_current_button)
        control_layout.addWidget(self.stop_all_button)
        layout.addWidget(control)
        return page






















































































    def refresh_status_ui(self):
        listener_degraded = bool(
            self.running and getattr(self, "input_listener_degraded_reason", "")
        )
        mapping_paused = (
            self.engine_state == EngineState.RUNNING
            and self.running
            and not self.mappings_enabled
        )
        input_isolated = bool(
            self.running
            and getattr(self, "profile_input_temporarily_suspended", False)
        )
        suspend_reason = str(getattr(self, "profile_input_suspend_reason", "") or "")
        if input_isolated and suspend_reason == "macrocanvas_foreground":
            isolate_text = "前台隔离"
        elif input_isolated and suspend_reason == "foreground_candidate_detected":
            isolate_text = "切换隔离"
        elif input_isolated:
            isolate_text = "临时隔离"
        else:
            isolate_text = ""
        runtime_name = self._profile_name(self.active_profile_id)
        runtime_suffix = f"（{isolate_text}）" if isolate_text else ""
        self.runtime_profile_status.setText(f"运行档案：{runtime_name}{runtime_suffix}")
        if input_isolated:
            self.engine_status.setText(f"映射：{isolate_text}")
        elif mapping_paused:
            self.engine_status.setText("映射：已暂停")
        elif listener_degraded:
            self.engine_status.setText("引擎：运行中（监听异常）")
        else:
            self.engine_status.setText(f"引擎：{self.engine_state.value}")
        self.config_status.setText(f"配置：{self.config_state.value}")
        if (
            self.macro_state in (
                MacroState.COUNTDOWN, MacroState.STOPPING, MacroState.STOP_TIMEOUT
            )
            and self.macro_status_detail
        ):
            self.macro_status.setText(f"宏任务：{self.macro_status_detail}")
        else:
            self.macro_status.setText(f"宏任务：{self.macro_state.value}")
        self.engine_status.setObjectName(
            "statusPaused" if (mapping_paused or input_isolated)
            else (
                "statusOff" if listener_degraded
                else ("statusOn" if self.engine_state == EngineState.RUNNING else "statusOff")
            )
        )
        self.runtime_profile_status.setObjectName(
            "statusPaused" if input_isolated
            else ("statusOn" if self.running else "statusOff")
        )
        self.config_status.setObjectName(
            "statusOn" if self.config_state in (ConfigState.SAVED, ConfigState.APPLIED)
            else "statusOff"
        )
        self.macro_status.setObjectName(
            "statusPaused"
            if self.macro_state in (
                MacroState.PAUSED, MacroState.STOPPING, MacroState.STOP_TIMEOUT
            )
            else ("statusOn" if self.macro_state != MacroState.IDLE else "statusOff")
        )
        for label in (
            self.runtime_profile_status,
            self.engine_status,
            self.config_status,
            self.macro_status,
        ):
            label.style().unpolish(label)
            label.style().polish(label)
        if hasattr(self, "refresh_profile_selector_state"):
            self.refresh_profile_selector_state()

    def update_engine_release_inputs(self):
        # All output and release operations are performed by Kanata virtual keys.
        return

    def _owned_output_names_snapshot(self, include_mouse=False):
        """Collect concrete key/button names currently owned by MacroCanvas."""
        names = []
        output = self.interception_output
        if output is not None:
            try:
                pending = output.pending_release_summary()
                names.extend(pending.get("keys", []))
                names.extend(pending.get("mouse", []))
            except (AttributeError, OSError, ValueError):
                pass

        with self.sync_output_lock:
            sync_items = list(self.sync_output_counts.values())
        for item in sync_items:
            action = dict(item.get("action") or {})
            names.extend(modifier_names(action.get("modifiers", "无")))
            names.append(str(action.get("target") or ""))

        with self.macro_controller.lock:
            tasks = list(self.macro_controller.tasks.values())
        for task in tasks:
            with task.pressed_lock:
                entries = list(task.pressed.values())
            for entry in entries:
                action = dict(entry.get("action") or {})
                names.extend(modifier_names(action.get("modifiers", "无")))
                names.append(str(action.get("target") or ""))

        with self.quarantined_mouse_release_lock:
            quarantined = list(self.quarantined_mouse_releases)
        for entry in quarantined:
            action = dict(entry.get("action") or {})
            names.extend(modifier_names(action.get("modifiers", "无")))
            names.append(str(action.get("target") or ""))

        names = [name for name in dict.fromkeys(names) if name in INPUT_NAMES]
        if not include_mouse:
            names = [name for name in names if name not in MOUSE_NAMES]
        return names

    def _force_release_system_inputs(self, names=None, include_mouse=False):
        """Last-resort release limited to inputs actually owned by this process."""
        owned_names = list(
            names
            if names is not None
            else self._owned_output_names_snapshot(include_mouse=include_mouse)
        )
        if not include_mouse:
            owned_names = [name for name in owned_names if name not in MOUSE_NAMES]
        owned_names = [
            name for name in dict.fromkeys(owned_names) if name in INPUT_NAMES
        ]
        if not owned_names:
            return True
        try:
            return bool(
                WinInput(lambda *_: False).force_release_names(
                    owned_names, attempts=2, only_if_down=False
                )
            )
        except (AttributeError, OSError, ValueError):
            return False

    def _release_interception_output(self):
        if not self.interception_output:
            return True
        try:
            return bool(self.interception_output.release_all())
        except Exception:
            return False

    def _runtime_output_release_targets(
        self, rules=None, extra_names=None, include_history=True,
        include_configured=True, include_owned=True, include_mouse_history=True,
    ):
        """Collect key/button names that a bounded cleanup pass may release."""
        targets = []
        modifiers = []
        source_rules = (
            self._runtime_mapping_rules() if rules is None else list(rules)
        )
        if include_configured:
            for rule in source_rules:
                if rule.get("_runtime_kind") == "preset":
                    actions = rule.get("actions", []) or []
                else:
                    target = rule.get("target")
                    if target in INPUT_NAMES:
                        targets.append(target)
                    modifiers.extend(modifier_names(
                        rule.get("target_modifiers", "无")
                    ))
                    actions = rule.get("actions", []) or []
                for action in iter_action_tree(actions):
                    if action.get("type") not in ("键盘点击", "鼠标点击"):
                        continue
                    target = action.get("target")
                    if target in INPUT_NAMES:
                        targets.append(target)
                    modifiers.extend(modifier_names(action.get("modifiers", "无")))

        if include_owned:
            for _label, names in self.held_input_snapshot():
                targets.extend(name for name in names if name in INPUT_NAMES)
        if include_history:
            targets.extend(
                name for name in self.runtime_release_target_history
                if name in INPUT_NAMES
                and (include_mouse_history or name not in MOUSE_NAMES)
            )
        targets.extend(
            str(name) for name in (extra_names or [])
            if str(name) in INPUT_NAMES
        )
        return list(dict.fromkeys(targets + list(reversed(modifiers))))

    def _runtime_virtual_key_release_names(
        self, rules=None, extra_names=None, include_history=True
    ):
        source_rules = (
            self._runtime_mapping_rules() if rules is None else list(rules)
        )
        names = []
        for rule in source_rules:
            for field in ("_vkey", "_loop_vkey"):
                name = str(rule.get(field) or "")
                if name:
                    names.append(name)
            for action in iter_action_tree(rule.get("actions", []) or []):
                name = str(action.get("_vkey") or "")
                if name:
                    names.append(name)
        if include_history:
            names.extend(self.runtime_release_vkey_history)
        names.extend(str(name) for name in (extra_names or []) if str(name))
        return list(dict.fromkeys(names))

    def _remember_runtime_release_state(self, rules=None):
        source_rules = (
            self._runtime_mapping_rules() if rules is None else list(rules)
        )
        targets = self._runtime_output_release_targets(
            rules=source_rules, include_history=False
        )
        virtual_keys = self._runtime_virtual_key_release_names(
            rules=source_rules, include_history=False
        )
        self.runtime_release_target_history.update(targets)
        self.runtime_release_vkey_history.update(virtual_keys)
        return targets, virtual_keys

    def _release_runtime_virtual_keys(
        self, names=None, rules=None, include_history=True, timeout=1.0,
        force=False,
    ):
        release_names = self._runtime_virtual_key_release_names(
            rules=rules,
            extra_names=names,
            include_history=include_history,
        )
        if not release_names:
            return True

        success = True
        attempted = False
        engine_results = []
        for label, engine in (
            ("Kanata", self.engine),
            ("键盘 Kanata", self.keyboard_engine),
        ):
            if not self._kanata_engine_has_runtime(engine) or not engine.is_running():
                continue
            available = set(getattr(engine, "available_fake_keys", set()))
            names_known = bool(getattr(engine, "fake_key_names_received", False))
            quarantined = set(getattr(engine, "quarantined_virtual_keys", set()))
            candidates = [
                name for name in release_names
                if (not names_known or name in available)
                and (force or name not in quarantined)
            ]
            if not candidates:
                continue
            attempted = True
            queued = True
            per_key_timeout = min(0.35, max(0.08, float(timeout)))
            for name in reversed(candidates):
                queued = bool(
                    engine.queue_virtual_key_action(
                        name, "Release", wait=True, timeout=per_key_timeout
                    )
                    and queued
                )
            flushed = bool(engine.flush_commands(timeout=max(0.1, timeout)))
            if flushed:
                # The protocol acknowledgement proves Kanata consumed the
                # Release batch; leave a short bounded window for the output
                # worker to emit the corresponding KeyUp packets.
                time.sleep(0.03)
            engine_ok = bool(queued and flushed)
            engine_results.append({
                "engine": label,
                "count": len(candidates),
                "queued": queued,
                "flushed": flushed,
                "success": engine_ok,
            })
            success = bool(engine_ok and success)

        self.write_diagnostic(
            "runtime_virtual_keys_released",
            force=True,
            targets=release_names,
            attempted=attempted,
            engines=engine_results,
            success=success,
        )
        return success

    def _failsafe_release_runtime_targets(
        self, force_all=False, names=None, include_history=True,
        allow_mouse_targets=True,
    ):
        names = self._runtime_output_release_targets(
            extra_names=names,
            include_history=bool(include_history and not force_all),
            include_configured=not force_all,
            include_owned=True,
            include_mouse_history=not force_all,
        )
        if not allow_mouse_targets:
            names = [name for name in names if name not in MOUSE_NAMES]
        if not names:
            return True
        driver_released = False
        if force_all and self.interception_output is not None:
            try:
                driver_released = bool(
                    self.interception_output.force_release_names_untracked(names)
                )
            except (AttributeError, OSError, ValueError):
                driver_released = False
        try:
            system_released = bool(
                WinInput(lambda *_args: False).force_release_names(
                    names, attempts=2, only_if_down=not force_all
                )
            )
        except (AttributeError, OSError, ValueError):
            system_released = False
        released = bool(driver_released or system_released)
        self.write_diagnostic(
            "runtime_target_release_failsafe",
            force=True,
            targets=names,
            driver_released=driver_released,
            system_released=system_released,
            success=released,
        )
        return released

    def _start_stop_release_guard(self):
        """Repeat post-stop Up sweeps to catch late or orphaned driver output."""
        self._stop_release_guard_generation += 1
        generation = self._stop_release_guard_generation

        def release_pass():
            if generation != self._stop_release_guard_generation:
                return
            if self.running and not self.output_shutdown_in_progress:
                return
            self._failsafe_release_runtime_targets(
                force_all=False, allow_mouse_targets=False
            )

        for delay in (0, 80, 200, 450, 900):
            QTimer.singleShot(delay, release_pass)

    def held_input_snapshot(self):
        """Collect all program-owned held states without changing them."""
        groups = []
        output = self.interception_output
        if output:
            with output.lock:
                pending = output.pending_release_summary()
                names = list(pending["keys"])
                names.extend(pending["mouse"])
            if names:
                groups.append(("Interception", names))
        for label, engine in (("Kanata", self.engine),
                              ("键盘 Kanata", self.keyboard_engine)):
            with engine.active_virtual_keys_lock:
                names = sorted(engine.active_virtual_keys)
            if names:
                groups.append((label, names))
        macro_names = []
        with self.macro_controller.lock:
            tasks = list(self.macro_controller.tasks.values())
        for task in tasks:
            with task.pressed_lock:
                macro_names.extend(str(name) for name in task.pressed)
        if macro_names:
            groups.append(("宏任务", sorted(set(macro_names))))
        with self.quarantined_mouse_release_lock:
            quarantined = [
                str(item.get("action", {}).get("target") or "鼠标按键")
                for item in self.quarantined_mouse_releases
            ]
        if quarantined:
            groups.append(("待安全释放", sorted(set(quarantined))))
        return groups

    @Slot()
    def force_release_held_inputs(
        self, _checked=False, show_feedback=True,
        _cross_window_release_confirmed=False,
    ):
        """Stop active macros, then release owned input without stopping the engine."""
        before = self.held_input_snapshot()
        cross_window_release_confirmed = bool(
            _cross_window_release_confirmed
        )

        def confirm_quarantined_mouse_release():
            nonlocal cross_window_release_confirmed
            output_quarantined = bool(
                self.interception_output
                and self.interception_output.mouse_release_quarantined
            )
            with self.quarantined_mouse_release_lock:
                macro_quarantined = bool(self.quarantined_mouse_releases)
            if not output_quarantined and not macro_quarantined:
                return True
            if cross_window_release_confirmed:
                return True
            # This is the sole cross-window MouseUp path. Keep it behind an
            # explicit confirmation so automatic cleanup cannot inject MouseUp
            # into an unrelated foreground window.
            if not show_feedback:
                return False
            decision = QMessageBox.question(
                self,
                "确认跨窗口释放",
                "有鼠标按键正等待回到原窗口后安全释放。\n"
                "现在强制释放可能会让当前窗口收到一次鼠标弹起，是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            cross_window_release_confirmed = decision == QMessageBox.Yes
            return cross_window_release_confirmed

        if not confirm_quarantined_mouse_release():
            return False

        previous_gate = self.output_shutdown_in_progress
        previous_allowed = self.profile_trigger_allowed
        self.output_shutdown_in_progress = True
        self.profile_trigger_allowed = False
        dispatch_lock = getattr(self, "output_dispatch_lock", None)
        if dispatch_lock is not None:
            with dispatch_lock:
                pass

        restore_layer = (
            self.active_profile_layer
            if self.mappings_enabled else DISABLED_LAYER_NAME
        )
        if (
            self.running
            and not getattr(self, "_shutdown_started", False)
            and not self._runtime_is_game_mode()
            and self.mappings_enabled
            and not self._change_runtime_profile_layer(
                DISABLED_LAYER_NAME, wait=True
            )
        ):
            self.output_shutdown_in_progress = previous_gate
            self.profile_trigger_allowed = previous_allowed
            if show_feedback:
                QMessageBox.warning(
                    self,
                    "无法暂停映射层",
                    self.engine.last_command_error
                    or self.keyboard_engine.last_command_error
                    or "无法在释放前禁用当前 Kanata 映射层。",
                )
            return False

        remaining = self.stop_all_macros(
            play_sound=False, keep_output_gate=True
        )
        if remaining:
            if getattr(self, "_macro_stop_gate_restore", None) is None:
                self._macro_stop_gate_restore = bool(previous_gate)
            self._defer_profile_input_restore(
                layer=restore_layer,
                profile_trigger_allowed=previous_allowed,
                reason="force_release_waiting_tasks",
            )
            self.engine_hint.setStyleSheet("color: #fbbf24;")
            self.engine_hint.setText(
                "仍有宏任务正在退出；已禁止新输出，请稍后再次强制释放"
            )
            if show_feedback:
                QMessageBox.warning(
                    self,
                    "宏任务尚未退出",
                    "部分宏线程仍在退出。为避免释放后再次按下，程序已暂时禁止"
                    "新的输出；请等待状态恢复后再次点击“强制释放键鼠”。",
                )
            return False

        if not confirm_quarantined_mouse_release():
            self.output_shutdown_in_progress = True
            self.profile_trigger_allowed = False
            if hasattr(self, "engine_hint"):
                self.engine_hint.setStyleSheet("color: #fbbf24;")
                self.engine_hint.setText(
                    "有鼠标按键等待安全释放；请回到原窗口后再次重试"
                )
            return False

        success = True
        failed_release_steps = []
        try:
            if not self._retry_quarantined_mouse_releases(force=True):
                success = False
                failed_release_steps.append("隔离鼠标释放")
            if not self._release_all_sync_mappings():
                success = False
                failed_release_steps.append("同步映射输出")
            if self.interception_output:
                if not self.interception_output.release_all(force=True):
                    success = False
                    failed_release_steps.append("Interception 输出")
            for engine in (self.engine, self.keyboard_engine):
                if self._kanata_engine_has_runtime(engine):
                    if not engine.release_all_virtual_keys(timeout=0.8, force=True):
                        success = False
                        failed_release_steps.append("Kanata 虚拟键")
            if not self._failsafe_release_runtime_targets(force_all=True):
                success = False
                failed_release_steps.append("系统级兜底释放")
            if success:
                self.runtime_release_target_history.clear()
                self.runtime_release_vkey_history.clear()
        finally:
            can_restore_runtime = bool(
                self.running
                and not self.settings_input_mode_active
                and not getattr(self, "recording_session_active", False)
                and not getattr(self, "_shutdown_started", False)
            )
            if success and can_restore_runtime:
                self.output_shutdown_in_progress = False
                if foreground_window_belongs_to_current_process():
                    # The force-release button is normally clicked while
                    # MacroCanvas is foreground. Keep the disabled layer until
                    # the user returns to an external window.
                    self.profile_trigger_allowed = False
                    self.profile_input_temporarily_suspended = True
                    self.profile_input_suspend_reason = "macrocanvas_foreground"
                    self.macrocanvas_foreground_suspended = True
                    self.foreground_profile_candidate = None
                    self.foreground_profile_candidate_hits = 0
                    self.foreground_candidate_input_suspended = False
                    self.write_diagnostic(
                        "force_release_macrocanvas_foreground_isolated",
                        active_profile_id=self.active_profile_id,
                    )
                else:
                    layer_restored = self._change_runtime_profile_layer(
                        restore_layer, wait=True
                    )
                    self.profile_trigger_allowed = bool(layer_restored)
                    if layer_restored:
                        self.profile_input_temporarily_suspended = False
                        self.profile_input_suspend_reason = ""
                        self.macrocanvas_foreground_suspended = False
                    if not layer_restored:
                        self.output_shutdown_in_progress = True
                        success = False
            elif success:
                self.output_shutdown_in_progress = False
                self.profile_trigger_allowed = bool(
                    can_restore_runtime and previous_allowed
                )
            else:
                self.output_shutdown_in_progress = True
                self.profile_trigger_allowed = False

        if success:
            self.macrocanvas_foreground_suspend_failed = False
            self.last_macro_release_failures = []
            if hasattr(self, "macro_controller"):
                self.macro_controller.last_release_failures = []
            self.active_macro_id = None
            self.last_action_activity = {}
            self.macro_state = MacroState.IDLE
            self.macro_status_detail = ""
            if hasattr(self, "activity_overlay"):
                self.activity_overlay.hide_message()
        else:
            failures = failed_release_steps or ["强制释放键鼠"]
            remember = getattr(self, "_remember_macro_cleanup_failure", None)
            if callable(remember):
                remember("强制释放键鼠失败", failures)
            else:
                self.last_macro_release_failures = list(dict.fromkeys(
                    list(getattr(self, "last_macro_release_failures", []) or [])
                    + failures
                ))
                self.macro_state = MacroState.STOP_TIMEOUT
                self.macro_status_detail = "强制释放键鼠失败"
                self.output_shutdown_in_progress = True

        self._start_stop_release_guard()
        self.write_diagnostic(
            "force_release_held_inputs",
            had_held_state=bool(before),
            macros_stopped=True,
            success=success,
            remaining=self.held_input_snapshot(),
        )
        if show_feedback and hasattr(self, "execution_info"):
            self.execution_info.setText(
                "已停止当前宏并强制释放程序持有的全部键鼠状态"
                if success else "仍有键鼠状态未能释放；请回到原窗口后再次重试"
            )
        if show_feedback and not success:
            QMessageBox.warning(
                self,
                "仍有输入未释放",
                "部分程序持有的键鼠状态仍未能释放。请回到触发映射时的窗口，"
                "再点击“强制释放键鼠”；必要时随后重试停止输入引擎。",
            )
        self.refresh_status_ui()
        self.refresh_macro_controls()
        return success



    def _build_runtime_entry(self, profile_id, name, mappings, presets):
        namespace = profile_namespace(profile_id)
        cached_mappings = []
        for mapping in mappings or []:
            copied = dict(mapping)
            copied["_vkey"] = KanataConfigBuilder.mapping_key(
                copied.get("id"), namespace
            )
            if (
                not copied.get("condition_enabled", False)
                and copied.get("source") in MOUSE_NAMES
                and copied.get("mode") in ("按住循环", "无限循环", "开关循环")
            ):
                copied["_loop_vkey"] = KanataConfigBuilder.mapping_loop_key(
                    copied.get("id"), namespace
                )
            cached_mappings.append(copied)

        cached_presets = []
        for preset in presets or []:
            copied_preset = dict(preset)
            action_index = 0

            def assign_virtual_keys(actions):
                nonlocal action_index
                copied_actions = []
                for action in actions or []:
                    copied_action = {
                        key: value for key, value in dict(action).items()
                        if key != "children"
                    }
                    current_index = action_index
                    action_index += 1
                    if copied_action.get("type") not in (
                        "等待", LOOP_ACTION_TYPE, *CONTROL_ACTION_TYPES,
                    ):
                        copied_action["_vkey"] = (
                            KanataConfigBuilder.preset_action_key(
                                copied_preset.get("id"), current_index, namespace
                            )
                        )
                    copied_action["children"] = assign_virtual_keys(
                        action.get("children", [])
                    )
                    copied_actions.append(copied_action)
                return copied_actions

            copied_preset["actions"] = assign_virtual_keys(
                preset.get("actions", [])
            )
            cached_presets.append(copied_preset)

        preset_library = {
            str(preset.get("id")): preset
            for preset in cached_presets if preset.get("id")
        }
        for preset in cached_presets:
            preset["_preset_library"] = preset_library

        return {
            "id": str(profile_id or ""),
            "name": str(name or "基础配置"),
            "layer": profile_layer_name(profile_id),
            "namespace": namespace,
            "mappings": cached_mappings,
            "presets": cached_presets,
        }

    def refresh_cache(self):
        if not hasattr(self, "mapping_cards") or not hasattr(self, "preset_cards"):
            return
        entry = self._build_runtime_entry(
            "", "基础配置", self.collect_mappings(), self.collect_presets()
        )
        with self.data_lock:
            self.cached_mappings = entry["mappings"]
            self.cached_presets = entry["presets"]

    @staticmethod
    def _preset_as_mapping_rule(preset):
        """Represent a preset with the exact runtime fields of a basic mapping."""
        copied = dict(preset)
        return {
            "id": copied.get("id"),
            "enabled": bool(copied.get("enabled")),
            "source_modifiers": copied.get("trigger_modifiers", "无"),
            "source": copied.get("trigger", "F1"),
            "mode": copied.get("execution_mode", "执行一次"),
            "loop_count": int(copied.get("loop_count", 1)),
            "loop_interval_ms": int(copied.get("loop_interval_ms", 0)),
            "loop_interval_jitter_ms": int(
                copied.get("loop_interval_jitter_ms", 0)
            ),
            "speed_percent": int(copied.get("speed_percent", 100)),
            "max_runtime_s": int(copied.get("max_runtime_s", 0)),
            "condition_enabled": bool(copied.get("condition_enabled", False)),
            "condition_input": copied.get("condition_input", "鼠标左键"),
            "condition_state": copied.get("condition_state", "按住时"),
            "name": copied.get("name", "预设"),
            "actions": clone_action_tree(copied.get("actions", [])),
            "_preset_library": copied.get("_preset_library", {}),
            "_runtime_kind": "preset",
        }

    def _snapshot_runtime_config(self):
        """Freeze base and all enabled profile tables for one applied config."""
        self.runtime_engine_backend = self.backend_combo.currentText()
        self.runtime_global_toggle_enabled = bool(self.global_toggle_enabled)
        self.runtime_global_toggle_modifiers = self.global_toggle_modifiers
        self.runtime_global_toggle_key = self.global_toggle_key
        self.runtime_macro_pause_enabled = bool(self.macro_pause_enabled)
        self.runtime_macro_pause_modifiers = self.macro_pause_modifiers
        self.runtime_macro_pause_key = self.macro_pause_key
        self.runtime_emergency_modifiers = self.emergency_modifiers
        self.runtime_emergency_key = self.emergency_key
        self.runtime_recording_cancel_modifiers = self.recording_cancel_modifiers
        self.runtime_recording_cancel_key = self.recording_cancel_key
        self.runtime_recording_finish_modifiers = self.recording_finish_modifiers
        self.runtime_recording_finish_key = self.recording_finish_key
        self.runtime_diagnostic_enabled = bool(self.diagnostic_enabled)

        self.runtime_profile_auto_switch_enabled = bool(
            self.profile_auto_switch_enabled
        )
        self.runtime_profiles = [
            normalize_profile(profile) for profile in self.profiles
            if profile.get("enabled", False)
        ]

        self._store_editor_payload()
        base_payload = profile_payload({"payload": self.base_profile_payload})
        base_entry = self._build_runtime_entry(
            "", "基础配置",
            base_payload.get("mappings", []),
            base_payload.get("presets", []),
        )
        catalog = {"": base_entry}
        for profile in self.runtime_profiles:
            profile_id = str(profile.get("id") or "")
            if not profile_id:
                continue
            payload = profile_payload(profile)
            catalog[profile_id] = self._build_runtime_entry(
                profile_id,
                profile.get("name", "未命名档案"),
                payload.get("mappings", []),
                payload.get("presets", []),
            )
        self.runtime_profile_catalog = catalog
        if self.active_profile_id not in catalog:
            self.active_profile_id = ""
        entry = catalog[self.active_profile_id]
        self.active_profile_layer = entry["layer"]
        self._install_runtime_profile_entry(entry)

    def load_config(self, data_override=None):
        default = {
            "engine_backend": "普通模式（winIOv2）",
            "auto_apply": False,
            "global_toggle_enabled": True,
            "global_toggle_modifiers": "Ctrl+Shift",
            "global_toggle_key": "F10",
            "macro_pause_enabled": True,
            "macro_pause_modifiers": "Ctrl",
            "macro_pause_key": "F9",
            "emergency_modifiers": "无",
            "emergency_key": "F8",
            "recording_cancel_modifiers": "无",
            "recording_cancel_key": "F7",
            "recording_finish_modifiers": "无",
            "recording_finish_key": "F8",
            "diagnostic_enabled": False,
            "mappings": [{
                "id": uuid.uuid4().hex,
                "enabled": False,
                "name": "未配置映射 1",
                "source_modifiers": "无", "source": "F6",
                "target_modifiers": "无", "target": "鼠标左键",
                "condition_enabled": False,
                "condition_input": "鼠标左键",
                "condition_state": "按住时",
                "mode": "同步按住", "hold_ms": 100,
                "hold_jitter_ms": 0,
                "loop_count": 1, "loop_interval_ms": 0,
                "loop_interval_jitter_ms": 0,
                "speed_percent": 100, "max_runtime_s": 0,
            }],
            "presets": [{
                "id": uuid.uuid4().hex,
                "enabled": False,
                "name": "未配置预设",
                "trigger_modifiers": "无",
                "trigger": "F1",
                "execution_mode": "执行一次",
                "loop_count": 1,
                "loop_interval_ms": 0,
                "loop_interval_jitter_ms": 0,
                "speed_percent": 100,
                "max_runtime_s": 0,
                "actions": [],
            }],
        }
        recovery_message = ""
        recovery_requires_save = False
        if data_override is not None:
            repaired_override, repaired_action_ids = repair_duplicate_action_tree_ids(
                json.loads(json.dumps(data_override, ensure_ascii=False))
            )
            repaired_override, repaired_ids = repair_duplicate_runtime_ids(
                repaired_override
            )
            data = validate_config_payload(
                repaired_override
            )
            messages = []
            if repaired_ids:
                messages.append(
                    f"检测到 {len(repaired_ids)} 个重复或缺失的映射/预设 ID，"
                    "已在本次载入中重新生成。原始来源不会被直接覆盖。"
                )
            if repaired_action_ids:
                messages.append(
                    f"检测到 {len(repaired_action_ids)} 个重复或缺失的动作/循环 ID，"
                    "已在本次载入中重新生成；循环引用保留首次出现的动作 ID。"
                )
            recovery_message = "\n\n".join(messages)
        else:
            if not CONFIG_PATH.exists():
                data = validate_config_payload(json.loads(json.dumps(default)))
                try:
                    self._save_config_payload(data, create_backup=True)
                except (OSError, ValueError) as error:
                    recovery_requires_save = True
                    recovery_message = (
                        "首次启动已载入安全的默认配置，但无法创建主配置文件。"
                        "请检查配置目录权限后点击“应用更改”。\n\n"
                        f"保存错误：{error}"
                    )
            else:
                try:
                    if CONFIG_PATH.stat().st_size > MAX_CONFIG_FILE_BYTES:
                        raise ValueError(
                            f"主配置文件超过 {MAX_CONFIG_FILE_BYTES // (1024 * 1024)} MB 上限"
                        )
                    raw_data = json.loads(CONFIG_PATH.read_text("utf-8"))
                    raw_data, repaired_loops = repair_overlapping_loop_controls(raw_data)
                    raw_data, repaired_action_ids = repair_duplicate_action_tree_ids(raw_data)
                    raw_data, repaired_ids = repair_duplicate_runtime_ids(raw_data)
                    data = validate_config_payload(raw_data)
                    messages = []
                    if repaired_loops:
                        repaired_names = "、".join(item["loop"] for item in repaired_loops[:5])
                        suffix = f"等 {len(repaired_loops)} 项" if len(repaired_loops) > 5 else ""
                        messages.append(
                            "检测到旧版本产生的重叠循环项目，已在本次载入中移除"
                            f"后出现的冲突项目：{repaired_names}{suffix}。"
                        )
                    if repaired_action_ids:
                        messages.append(
                            f"检测到 {len(repaired_action_ids)} 个重复或缺失的动作/循环 ID，"
                            "已重新生成；循环引用保留首次出现的动作 ID。"
                        )
                    if repaired_ids:
                        messages.append(
                            f"检测到 {len(repaired_ids)} 个重复或缺失的映射/预设 ID，"
                            "已在本次载入中重新生成。"
                        )
                    if messages:
                        recovery_requires_save = True
                        recovery_message = "\n\n".join(messages) + (
                            "\n\n原配置文件尚未覆盖；点击“应用更改”后才会写入修复结果。"
                        )
                except (OSError, ValueError, json.JSONDecodeError, RecursionError) as error:
                    data = default
                    recovery_requires_save = True
                    preserved_path = None
                    preserve_error = None
                    preserve_note = ""
                    try:
                        source_size = CONFIG_PATH.stat().st_size
                        if source_size > MAX_CONFIG_FILE_BYTES:
                            preserve_note = (
                                "原文件仍保留在原位；由于超过配置大小上限，"
                                "未再创建额外副本。"
                            )
                        else:
                            CONFIG_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                            preserved_path = CONFIG_BACKUP_DIR / (
                                f"corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
                            )
                            shutil.copy2(CONFIG_PATH, preserved_path)
                            corrupt_copies = sorted(
                                CONFIG_BACKUP_DIR.glob("corrupt-*.json"),
                                key=lambda path: path.stat().st_mtime,
                                reverse=True,
                            )
                            for stale in corrupt_copies[5:]:
                                try:
                                    stale.unlink()
                                except OSError:
                                    pass
                    except OSError as copy_error:
                        preserve_error = copy_error
                    recovered = self._load_latest_config_backup()
                    if recovered is not None:
                        data = recovered
                        recovery_message = (
                            "主配置无法读取，已在内存中载入最近一份有效保存快照。"
                            "主配置不会自动被覆盖；请检查后手动应用。\n\n"
                            f"原错误：{error}"
                        )
                    else:
                        recovery_message = (
                            "主配置无法读取，且没有找到有效备份；当前仅载入安全的"
                            "默认配置。原文件尚未覆盖，请先导出或检查原文件后再保存。\n\n"
                            f"原错误：{error}"
                        )
                    if preserved_path is not None:
                        recovery_message += f"\n\n损坏原件的额外副本已保留到：\n{preserved_path}"
                    elif preserve_error is not None:
                        recovery_message += f"\n\n原文件仍保留在原位，但创建额外副本失败：{preserve_error}"
                    elif preserve_note:
                        recovery_message += f"\n\n{preserve_note}"
                    try:
                        legacy = json.loads(LEGACY_CONFIG_PATH.read_text("utf-8"))
                        if recovered is None and isinstance(legacy, list):
                            legacy_candidate = json.loads(json.dumps(data))
                            legacy_candidate["mappings"] = legacy
                            legacy_candidate, legacy_loops = repair_overlapping_loop_controls(legacy_candidate)
                            legacy_candidate, legacy_action_ids = repair_duplicate_action_tree_ids(legacy_candidate)
                            legacy_candidate, legacy_runtime_ids = repair_duplicate_runtime_ids(legacy_candidate)
                            data = validate_config_payload(legacy_candidate)
                            legacy_repairs = len(legacy_loops) + len(legacy_action_ids) + len(legacy_runtime_ids)
                            recovery_message += (
                                "\n\n检测到旧版映射文件，已在通过完整校验后载入；"
                                "手动应用后才会写入当前配置格式。"
                            )
                            if legacy_repairs:
                                recovery_message += f"本次同时修复了 {legacy_repairs} 处结构问题。"
                    except (OSError, json.JSONDecodeError):
                        pass
                    except (TypeError, ValueError, RecursionError) as legacy_error:
                        recovery_message += (
                            "\n\n检测到旧版映射文件，但内容未通过当前配置校验，"
                            f"已忽略该旧文件。\n旧版文件错误：{legacy_error}"
                        )
        self.startup_recovery_pending_save = bool(
            data_override is None and recovery_requires_save
        )
        if recovery_message:
            QTimer.singleShot(
                0, lambda message=recovery_message: QMessageBox.warning(
                    self, "配置已自动恢复", message
                )
            )
        self.profiles = []
        for raw_profile in data.get("profiles", []):
            if not isinstance(raw_profile, dict):
                continue
            profile = normalize_profile(raw_profile)
            if not profile.get("id"):
                profile["id"] = uuid.uuid4().hex
            self.profiles.append(profile)
        self.profile_auto_switch_enabled = bool(
            data.get("profile_auto_switch_enabled", True)
        )
        self.active_profile_id = str(data.get("active_profile_id", ""))
        valid_profile_ids = {
            str(profile.get("id") or "") for profile in self.profiles
            if profile.get("enabled", False)
        }
        editable_profile_ids = {
            str(profile.get("id") or "") for profile in self.profiles
            if profile.get("id")
        }
        if self.active_profile_id not in valid_profile_ids:
            self.active_profile_id = ""
        requested_editor_id = str(
            data.get("editor_profile_id", self.active_profile_id) or ""
        )
        self.editor_profile_id = (
            requested_editor_id
            if requested_editor_id in editable_profile_ids else ""
        )
        self.active_profile_layer = profile_layer_name(self.active_profile_id)
        backend = data.get("engine_backend", "普通模式（winIOv2）")
        if backend in KanataEngine.EXECUTABLES:
            self.backend_combo.blockSignals(True)
            self.backend_combo.setCurrentText(backend)
            self.backend_combo.blockSignals(False)
        self.auto_apply_checkbox.blockSignals(True)
        self.auto_apply_checkbox.setChecked(bool(data.get("auto_apply", False)))
        self.auto_apply_checkbox.blockSignals(False)
        self.global_toggle_enabled = bool(
            data.get("global_toggle_enabled", True)
        )
        loaded_modifiers = data.get("global_toggle_modifiers", "Ctrl+Shift")
        self.global_toggle_modifiers = (
            loaded_modifiers if loaded_modifiers in MODIFIER_OPTIONS else "Ctrl+Shift"
        )
        loaded_key = data.get("global_toggle_key", "F10")
        self.global_toggle_key = (
            loaded_key if loaded_key in SYSTEM_HOTKEY_KEYS else "F10"
        )
        self.macro_pause_enabled = bool(data.get("macro_pause_enabled", True))
        loaded_pause_modifiers = data.get("macro_pause_modifiers", "Ctrl")
        self.macro_pause_modifiers = (
            loaded_pause_modifiers
            if loaded_pause_modifiers in MODIFIER_OPTIONS else "Ctrl"
        )
        loaded_pause_key = data.get("macro_pause_key", "F9")
        self.macro_pause_key = (
            loaded_pause_key if loaded_pause_key in SYSTEM_HOTKEY_KEYS else "F9"
        )
        self.emergency_modifiers = (
            data.get("emergency_modifiers", "无")
            if data.get("emergency_modifiers", "无") in MODIFIER_OPTIONS else "无"
        )
        loaded_emergency_key = data.get("emergency_key", "F8")
        self.emergency_key = (
            loaded_emergency_key
            if loaded_emergency_key in SYSTEM_HOTKEY_KEYS else "F8"
        )
        self.recording_cancel_modifiers = (
            data.get("recording_cancel_modifiers", "无")
            if data.get("recording_cancel_modifiers", "无") in MODIFIER_OPTIONS else "无"
        )
        loaded_cancel_key = data.get("recording_cancel_key", "F7")
        self.recording_cancel_key = (
            loaded_cancel_key if loaded_cancel_key in SYSTEM_HOTKEY_KEYS else "F7"
        )
        self.recording_finish_modifiers = (
            data.get("recording_finish_modifiers", "无")
            if data.get("recording_finish_modifiers", "无") in MODIFIER_OPTIONS else "无"
        )
        loaded_finish_key = data.get("recording_finish_key", "F8")
        self.recording_finish_key = (
            loaded_finish_key if loaded_finish_key in SYSTEM_HOTKEY_KEYS else "F8"
        )
        self.update_global_hotkey_action_text()
        self.diagnostic_enabled = bool(
            data.get("diagnostic_enabled", False)
        )
        self.update_diagnostic_action_text()

        def migrate_actions(actions):
            deferred_loops = []

            def migrate_level(raw_actions):
                migrated = []
                for raw_action in raw_actions or []:
                    copied = dict(raw_action)
                    children = migrate_level(copied.pop("children", []))
                    legacy_delay = max(0, int(copied.pop("delay_ms", 0)))
                    action_type = copied.get("type", "键盘点击")
                    copied["jitter_ms"] = max(0, int(copied.get("jitter_ms", 0)))
                    if action_type == LOOP_ACTION_TYPE:
                        loop_action = self.normalize_loop_action_data(copied)
                        # Previous versions physically owned the selected actions.
                        # Restore those actions at their original position, then
                        # append a reference-only loop card at the preset bottom.
                        if children:
                            for child in children:
                                if child.get("type") != LOOP_ACTION_TYPE:
                                    child["action_id"] = str(
                                        child.get("action_id") or uuid.uuid4().hex
                                    )
                            migrated.extend(children)
                            loop_action["target_action_ids"] = [
                                str(child.get("action_id"))
                                for child in children
                                if child.get("type") != LOOP_ACTION_TYPE
                                and child.get("action_id")
                            ]
                        loop_action["children"] = []
                        deferred_loops.append(loop_action)
                        continue
                    copied["action_id"] = str(
                        copied.get("action_id") or uuid.uuid4().hex
                    )
                    if action_type == "等待":
                        copied.pop("modifiers", None)
                        copied["wait_ms"] = int(
                            copied.get("wait_ms", legacy_delay or 500)
                        )
                        copied["children"] = children
                        migrated.append(copied)
                        continue

                    legacy_modifiers = modifier_names(
                        copied.get("modifiers", "无")
                    )
                    if legacy_modifiers:
                        copied["modifiers"] = "+".join(legacy_modifiers)
                    else:
                        copied.pop("modifiers", None)
                    copied["children"] = children
                    # Keyboard/mouse output backends already implement an atomic
                    # combo Press/Release: modifiers go down in order, the target
                    # follows, then Release reverses that order. Keep the legacy
                    # field on the action instead of expanding it into parallel
                    # action-tree branches whose thread start order is undefined.
                    migrated.append(copied)
                    if legacy_delay:
                        migrated.append({
                            "action_id": uuid.uuid4().hex,
                            "type": "等待",
                            "wait_ms": legacy_delay,
                            "children": [],
                        })
                return migrated

            ordinary = migrate_level(actions)
            valid_ids = {
                str(action.get("action_id"))
                for action in iter_action_tree(ordinary)
                if action.get("type") != LOOP_ACTION_TYPE and action.get("action_id")
            }
            for loop in deferred_loops:
                loop["target_action_ids"] = [
                    action_id for action_id in loop.get("target_action_ids", [])
                    if str(action_id) in valid_ids
                ]
                loop["children"] = []
            return ordinary + deferred_loops

        def migrate_payload(raw_payload):
            raw_payload = raw_payload if isinstance(raw_payload, dict) else {}
            mappings = []
            for mapping_index, mapping in enumerate(
                raw_payload.get("mappings", []), 1
            ):
                migrated = dict(mapping)
                migrated.setdefault("name", f"基础映射 {mapping_index}")
                if migrated.get("mode") == "单次触发":
                    migrated["mode"] = "执行一次"
                migrated.setdefault("condition_enabled", False)
                migrated.setdefault("condition_input", "鼠标左键")
                migrated.setdefault("condition_state", "按住时")
                migrated.setdefault("hold_jitter_ms", 0)
                migrated.setdefault("loop_count", 1)
                migrated.setdefault("loop_interval_ms", 0)
                migrated.setdefault("loop_interval_jitter_ms", 0)
                migrated.setdefault("speed_percent", 100)
                migrated.setdefault("max_runtime_s", 0)
                mappings.append(migrated)

            presets = []
            for raw_preset in raw_payload.get("presets", []):
                preset = dict(raw_preset)
                preset["actions"] = migrate_actions(
                    preset.get("actions", [])
                )
                preset.setdefault("condition_enabled", False)
                preset.setdefault("condition_input", "鼠标左键")
                preset.setdefault("condition_state", "按住时")
                preset.setdefault("loop_interval_jitter_ms", 0)
                presets.append(preset)
            return {"mappings": mappings, "presets": presets}

        self.base_profile_payload = migrate_payload({
            "mappings": data.get("mappings", []),
            "presets": data.get("presets", []),
        })
        for profile in self.profiles:
            profile["payload"] = migrate_payload(profile_payload(profile))

        editor_payload = self._payload_for_profile_id(self.editor_profile_id)
        if editor_payload is None:
            self.editor_profile_id = ""
            editor_payload = profile_payload({
                "payload": self.base_profile_payload
            })
        for mapping in editor_payload.get("mappings", []):
            self.add_mapping(json.loads(json.dumps(mapping)))
        previous_defer = getattr(self, "defer_preset_action_rows", False)
        self.defer_preset_action_rows = True
        try:
            for preset in editor_payload.get("presets", []):
                copied = dict(preset)
                copied["actions"] = preset.get("actions", []) or []
                self.add_preset(copied)
        finally:
            self.defer_preset_action_rows = previous_defer
        # 配置文件中的空方案必须按空方案恢复，不能在启动时重新
        # 注入“未配置映射 / 未配置预设”占位项目。首次运行所需的
        # 示例项目仍由上方 default 配置提供。
        if self.preset_cards and self.selected_preset_card is None:
            self.select_preset_card(self.preset_cards[0])
        self.editor_loaded_profile_id = str(self.editor_profile_id or "")
        # Keep startup/full reload light: action rows are loaded lazily, so do
        # not immediately collect the whole editor just to capture a baseline.
        self.editor_loaded_payload = {
            "mappings": editor_payload.get("mappings", []) or [],
            "presets": editor_payload.get("presets", []) or [],
        }
        self.refresh_profile_selector()


























































    def start_preset(self, preset):
        """Compatibility entry point; use the same dispatcher as shortcuts."""
        token = f"manual:preset:{preset.get('id', '')}"
        triggered = self._dispatch_preset_trigger(
            preset, token, True, repeated=False, source="manual"
        )
        if not triggered:
            self.engine_hint.setText(
                f"“{preset.get('name', '预设')}”未执行：预设未启用、映射已暂停或引擎已停止"
            )
            return False
        # This legacy manual entry does not receive a physical KeyUp.  Always
        # pair the synthetic Down with a synthetic Up so hold-loop presets cannot
        # leave a task registered as held forever if this compatibility path is
        # reused by an old button or plugin.
        if not self._dispatch_preset_trigger(
            preset, token, False, repeated=False, source="manual"
        ):
            self.write_diagnostic(
                "manual_preset_release_ignored",
                preset_id=preset.get("id"),
                name=preset.get("name", "预设"),
            )
        return True
