from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from config.profiles import normalize_profile, profile_summary
from config.schema import (
    MAX_CONFIG_FILE_BYTES, repair_duplicate_action_tree_ids,
    repair_duplicate_runtime_ids, repair_overlapping_loop_controls,
    validate_config_payload,
)


_SNAPSHOT_LABELS = {
    "applied": "成功应用配置",
    "saved": "已保存配置",
    "rejected": "应用失败候选",
    "config": "旧版配置备份",
}


class _SnapshotLoadSignals(QObject):
    finished = Signal(object)


class _SnapshotLoadTask(QRunnable):
    """Read and validate one selected snapshot outside the GUI thread."""

    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        self.signals = _SnapshotLoadSignals()

    @Slot()
    def run(self):
        payload = None
        summary = None
        error = ""
        try:
            if self.path.stat().st_size > MAX_CONFIG_FILE_BYTES:
                raise ValueError(
                    f"备份文件超过 {MAX_CONFIG_FILE_BYTES // (1024 * 1024)} MB 上限"
                )
            raw = json.loads(self.path.read_text("utf-8-sig"))
            repaired, _removed_loops = repair_overlapping_loop_controls(raw)
            repaired, _action_changes = repair_duplicate_action_tree_ids(repaired)
            repaired, _changes = repair_duplicate_runtime_ids(repaired)
            payload = validate_config_payload(repaired)
            summary = BackupManagerDialog._build_summary(payload)
        except (
            OSError, ValueError, TypeError, json.JSONDecodeError,
            RecursionError, MemoryError,
        ) as load_error:
            error = str(load_error)
        self.signals.finished.emit({
            "path": str(self.path),
            "payload": payload,
            "summary": summary,
            "error": error,
        })


class BackupManagerDialog(QDialog):
    """Theme-matched browser for saved configuration snapshots."""

    def __init__(self, backup_directory, parent=None):
        super().__init__(parent)
        self.setWindowTitle("备份配置表")
        self.resize(930, 590)
        self.setMinimumSize(720, 460)
        self.backup_directory = Path(backup_directory)
        self._snapshots = []
        self._selected_snapshot = None
        self._snapshot_pool = QThreadPool.globalInstance()
        self._snapshot_load_active = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        title = QLabel("备份配置表")
        title.setObjectName("heading")
        root.addWidget(title)

        hint = QLabel(
            "备份按保存时间从新到旧排列。选择一项后可查看主要内容，"
            "确认恢复时会在当前窗口内重新载入配置。"
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        list_card = QFrame()
        list_card.setObjectName("card")
        list_card.setFixedWidth(390)
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(12, 12, 12, 12)

        list_title = QLabel("可用备份")
        list_title.setObjectName("sectionLabel")
        list_layout.addWidget(list_title)

        self.snapshot_tree = QTreeWidget()
        self.snapshot_tree.setColumnCount(2)
        self.snapshot_tree.setHeaderLabels(["备份类型", "保存时间"])
        self.snapshot_tree.setRootIsDecorated(False)
        self.snapshot_tree.setAlternatingRowColors(True)
        self.snapshot_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.snapshot_tree.setUniformRowHeights(True)
        self.snapshot_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.snapshot_tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.snapshot_tree.currentItemChanged.connect(
            self._on_selection_changed
        )
        list_layout.addWidget(self.snapshot_tree, 1)
        body.addWidget(list_card)

        detail_card = QFrame()
        detail_card.setObjectName("card")
        detail_layout = QVBoxLayout(detail_card)
        detail_layout.setContentsMargins(18, 16, 18, 16)
        detail_layout.setSpacing(10)

        detail_title = QLabel("备份详情")
        detail_title.setObjectName("sectionLabel")
        detail_layout.addWidget(detail_title)

        self.detail_name = QLabel("请选择一份备份")
        self.detail_name.setObjectName("heading")
        self.detail_name.setWordWrap(True)
        detail_layout.addWidget(self.detail_name)

        self.detail_time = self._make_detail_label()
        self.detail_mode = self._make_detail_label()
        self.detail_base = self._make_detail_label()
        self.detail_profiles = self._make_detail_label()
        self.detail_profile_contents = self._make_detail_label()
        self.detail_active = self._make_detail_label()
        self.detail_size = self._make_detail_label()
        self.detail_note = self._make_detail_label(word_wrap=True)

        for widget in (
            self.detail_time,
            self.detail_mode,
            self.detail_base,
            self.detail_profiles,
            self.detail_profile_contents,
            self.detail_active,
            self.detail_size,
            self.detail_note,
        ):
            detail_layout.addWidget(widget)

        detail_layout.addStretch(1)
        body.addWidget(detail_card, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.setObjectName("secondary")
        close_button.clicked.connect(self.reject)
        buttons.addWidget(close_button)

        self.restore_button = QPushButton("恢复所选备份")
        self.restore_button.setObjectName("primary")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self._accept_selected)
        buttons.addWidget(self.restore_button)
        root.addLayout(buttons)

        self._load_snapshots()

    @staticmethod
    def _make_detail_label(word_wrap=False):
        label = QLabel("—")
        label.setObjectName("muted")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(bool(word_wrap))
        return label

    @staticmethod
    def _snapshot_kind(path):
        name = path.name.lower()
        for prefix in ("applied", "saved", "rejected", "config"):
            if name.startswith(prefix + "-"):
                return prefix
        return "config"

    @staticmethod
    def _format_timestamp(timestamp):
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return "时间未知"

    @staticmethod
    def _profile_name(data, profile_id):
        profile_id = str(profile_id or "")
        if not profile_id:
            return "基础配置"
        for profile in data.get("profiles", []) or []:
            if str(profile.get("id") or "") == profile_id:
                return str(profile.get("name") or "未命名档案")
        return "未知档案"

    @staticmethod
    def _build_summary(data):
        base = profile_summary({
            "payload": {
                "mappings": data.get("mappings", []) or [],
                "presets": data.get("presets", []) or [],
            }
        })
        profiles = [
            normalize_profile(item)
            for item in data.get("profiles", []) or []
        ]
        enabled_count = sum(1 for item in profiles if item.get("enabled", False))
        profile_totals = {
            "mappings": 0,
            "presets": 0,
            "actions": 0,
        }
        for profile in profiles:
            summary = profile_summary(profile)
            for key in profile_totals:
                profile_totals[key] += int(summary.get(key, 0))

        names = [str(item.get("name") or "未命名档案") for item in profiles]
        if len(names) > 6:
            names_text = "、".join(names[:6]) + f" 等 {len(names)} 个"
        else:
            names_text = "、".join(names) if names else "无"

        return {
            "mode": str(data.get("engine_backend") or "未记录"),
            "base": base,
            "profiles": len(profiles),
            "enabled_profiles": enabled_count,
            "profile_totals": profile_totals,
            "profile_names": names_text,
            "active_name": BackupManagerDialog._profile_name(
                data, data.get("active_profile_id")
            ),
            "editor_name": BackupManagerDialog._profile_name(
                data, data.get("editor_profile_id")
            ),
        }

    def _load_snapshots(self):
        self.snapshot_tree.clear()
        self._snapshots.clear()

        paths = []
        for prefix in ("applied", "saved", "rejected", "config"):
            paths.extend(self.backup_directory.glob(f"{prefix}-*.json"))

        def modified_time(path):
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        paths = sorted(set(paths), key=modified_time, reverse=True)
        for path in paths:
            kind = self._snapshot_kind(path)
            timestamp = modified_time(path)
            snapshot = {
                "path": path,
                "kind": kind,
                "type_label": _SNAPSHOT_LABELS.get(kind, "配置备份"),
                "timestamp": timestamp,
                "time_label": self._format_timestamp(timestamp),
                "payload": None,
                "summary": None,
                "error": "",
                "size": 0,
                "loaded": False,
                "loading": False,
            }
            try:
                snapshot["size"] = path.stat().st_size
                if snapshot["size"] > MAX_CONFIG_FILE_BYTES:
                    raise ValueError(
                        f"备份文件超过 {MAX_CONFIG_FILE_BYTES // (1024 * 1024)} MB 上限"
                    )
            except (OSError, ValueError) as error:
                snapshot["error"] = str(error)
                snapshot["loaded"] = True

            self._snapshots.append(snapshot)
            display_type = snapshot["type_label"]
            if snapshot["error"]:
                display_type += "（无法读取）"
            item = QTreeWidgetItem([
                display_type,
                snapshot["time_label"],
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, len(self._snapshots) - 1)
            if snapshot["error"]:
                item.setForeground(0, QColor("#ff8496"))
                item.setForeground(1, QColor("#ff8496"))
            self.snapshot_tree.addTopLevelItem(item)

        if self.snapshot_tree.topLevelItemCount():
            self.snapshot_tree.setCurrentItem(self.snapshot_tree.topLevelItem(0))
        else:
            self._show_empty_state()

    def _show_empty_state(self):
        self._selected_snapshot = None
        self.restore_button.setEnabled(False)
        self.detail_name.setText("暂无可用备份")
        self.detail_time.setText("保存或成功应用配置后，备份会显示在这里。")
        for widget in (
            self.detail_mode,
            self.detail_base,
            self.detail_profiles,
            self.detail_profile_contents,
            self.detail_active,
            self.detail_size,
            self.detail_note,
        ):
            widget.setText("—")

    def _on_selection_changed(self, current, _previous):
        if current is None:
            self._show_empty_state()
            return
        index = current.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(index, int) or not (0 <= index < len(self._snapshots)):
            self._show_empty_state()
            return
        snapshot = self._snapshots[index]
        self._selected_snapshot = snapshot
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snapshot):
        self.detail_name.setText(snapshot["type_label"])
        self.detail_time.setText(f"保存时间：{snapshot['time_label']}")
        self.detail_size.setText(
            f"文件大小：{max(1, round(snapshot['size'] / 1024))} KB"
        )

        if not snapshot.get("loaded"):
            self.restore_button.setEnabled(False)
            self.detail_mode.setText("输入模式：正在后台校验……")
            self.detail_base.setText("基础配置：正在读取……")
            self.detail_profiles.setText("配置档案：—")
            self.detail_profile_contents.setText("档案内容：—")
            self.detail_active.setText("档案状态：—")
            self.detail_note.setText("只会读取当前选中的备份，其他备份不会阻塞窗口。")
            if not snapshot.get("loading"):
                self._request_snapshot_load(snapshot)
            return

        if snapshot["error"] or snapshot["payload"] is None:
            self.restore_button.setEnabled(False)
            self.detail_mode.setText("输入模式：无法解析")
            self.detail_base.setText("基础配置：—")
            self.detail_profiles.setText("配置档案：—")
            self.detail_profile_contents.setText("档案内容：—")
            self.detail_active.setText("档案状态：—")
            self.detail_note.setText(
                "该备份无法通过当前版本的配置校验，不能直接恢复。\n"
                f"错误：{snapshot['error'] or '未知错误'}"
            )
            return

        summary = snapshot["summary"]
        base = summary["base"]
        profile_totals = summary["profile_totals"]
        self.detail_mode.setText(f"输入模式：{summary['mode']}")
        self.detail_base.setText(
            "基础配置："
            f"{base['mappings']} 个映射 · {base['presets']} 个预设 · "
            f"{base['actions']} 个动作"
        )
        self.detail_profiles.setText(
            "配置档案："
            f"{summary['profiles']} 个（启用 {summary['enabled_profiles']} 个）"
        )
        self.detail_profile_contents.setText(
            "档案内容："
            f"{profile_totals['mappings']} 个映射 · "
            f"{profile_totals['presets']} 个预设 · "
            f"{profile_totals['actions']} 个动作"
        )
        self.detail_active.setText(
            f"运行方案：{summary['active_name']}　编辑方案：{summary['editor_name']}"
        )
        note = f"档案名称：{summary['profile_names']}"
        if snapshot.get("kind") == "rejected":
            note += (
                "\n此候选配置通过了格式校验，但上次未能被输入引擎完整应用；"
                "恢复前请先确认驱动、Kanata 和绑定设备状态。"
            )
        self.detail_note.setText(note)
        self.restore_button.setEnabled(True)

    def _request_snapshot_load(self, snapshot):
        # Rapid selection changes must not start one 25 MB validation task per
        # clicked row. Finish the one active task, then load only the latest
        # selected row when its details are rendered again.
        if self._snapshot_load_active:
            return
        self._snapshot_load_active = True
        snapshot["loading"] = True
        task = _SnapshotLoadTask(snapshot["path"])
        task.signals.finished.connect(self._on_snapshot_loaded)
        self._snapshot_pool.start(task)

    @Slot(object)
    def _on_snapshot_loaded(self, result):
        self._snapshot_load_active = False
        path = str(result.get("path") or "")
        snapshot = next(
            (item for item in self._snapshots if str(item.get("path")) == path),
            None,
        )
        if snapshot is None:
            return
        snapshot["payload"] = result.get("payload")
        snapshot["summary"] = result.get("summary")
        snapshot["error"] = str(result.get("error") or "")
        snapshot["loading"] = False
        snapshot["loaded"] = True
        try:
            index = self._snapshots.index(snapshot)
        except ValueError:
            index = -1
        item = self.snapshot_tree.topLevelItem(index) if index >= 0 else None
        if item is not None:
            label = snapshot["type_label"]
            if snapshot["error"]:
                label += "（无法读取）"
                item.setForeground(0, QColor("#ff8496"))
                item.setForeground(1, QColor("#ff8496"))
            item.setText(0, label)
        if self._selected_snapshot is snapshot:
            self._render_snapshot(snapshot)
        elif self._selected_snapshot is not None:
            self._render_snapshot(self._selected_snapshot)

    def _accept_selected(self):
        if (
            self._selected_snapshot is None
            or self._selected_snapshot.get("payload") is None
            or self._selected_snapshot.get("error")
        ):
            return
        self.accept()

    def selected_snapshot(self):
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        return self._selected_snapshot
