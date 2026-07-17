"""Read-only dialog for side-effect-free macro previews."""

from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)


def format_duration(minimum, maximum):
    def one(value):
        if value is None:
            return "无固定上限"
        if value < 1000:
            return f"{value} ms"
        return f"{value / 1000:.2f} 秒"

    if maximum is None:
        return f"至少 {one(minimum)}，无固定上限"
    if minimum == maximum:
        return one(minimum)
    return f"{one(minimum)} ～ {one(maximum)}"


class SimulationPreviewDialog(QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"安全模拟预览 · {report['name']}")
        self.resize(960, 650)
        layout = QVBoxLayout(self)

        summary = QLabel(
            f"不会发送任何键鼠输入。执行模式：{report['execution_mode']}　"
            f"速度：{report['speed_percent']}%\n"
            f"单轮估算：{format_duration(report['one_cycle_min_ms'], report['one_cycle_max_ms'])}　"
            f"总时长估算：{format_duration(report['total_min_ms'], report['total_max_ms'])}"
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        if report["warnings"]:
            warning = QLabel("注意：" + "\n".join(report["warnings"]))
            warning.setObjectName("warningText")
            warning.setWordWrap(True)
            layout.addWidget(warning)

        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["序号", "路径", "预计开始", "预计耗时", "动作"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for row, event in enumerate(report["events"]):
            table.insertRow(row)
            values = (
                str(row + 1),
                event["path"],
                format_duration(event["start_min_ms"], event["start_max_ms"]),
                format_duration(event["duration_min_ms"], event["duration_max_ms"]),
                event["description"],
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
