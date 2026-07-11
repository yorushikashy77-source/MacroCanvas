"""Reusable event filters shared by application windows."""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QWidget,
)


class WheelEditBlocker(QObject):
    """Prevent the wheel from accidentally editing closed value controls."""

    _BLOCKED_TYPES = (QComboBox, QAbstractSpinBox)

    @staticmethod
    def _scroll_step_for_area(area, scroll_bar, orientation, delta_value):
        notches = max(1, abs(delta_value) // 120)
        if isinstance(area, QAbstractItemView):
            scroll_mode = (
                area.verticalScrollMode()
                if orientation == Qt.Orientation.Vertical
                else area.horizontalScrollMode()
            )
            if scroll_mode == QAbstractItemView.ScrollPerItem:
                # QTreeWidget 在 ScrollPerItem 模式下，滚动条单位是“条目”
                # 而不是像素。继续使用像素兜底的 24 会导致一次滚轮跳过
                # 约 24 个动作卡片；这里改为跟随系统的普通滚轮行数。
                return max(1, QApplication.wheelScrollLines()) * notches
        return max(24, scroll_bar.singleStep() * 3) * notches

    @classmethod
    def _scroll_nearest_area(cls, widget, event):
        delta = event.angleDelta()
        parent = widget.parentWidget() if isinstance(widget, QWidget) else None
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                vertical = parent.verticalScrollBar()
                if delta.y() and vertical.maximum() > vertical.minimum():
                    step = cls._scroll_step_for_area(
                        parent, vertical, Qt.Orientation.Vertical, delta.y()
                    )
                    vertical.setValue(
                        vertical.value() + (-step if delta.y() > 0 else step)
                    )
                    return True

                horizontal = parent.horizontalScrollBar()
                if delta.x() and horizontal.maximum() > horizontal.minimum():
                    step = cls._scroll_step_for_area(
                        parent, horizontal, Qt.Orientation.Horizontal, delta.x()
                    )
                    horizontal.setValue(
                        horizontal.value() + (-step if delta.x() > 0 else step)
                    )
                    return True
            parent = parent.parentWidget()
        return False

    @classmethod
    def _blocked_owner(cls, watched):
        widget = watched if isinstance(watched, QWidget) else None
        while widget is not None:
            # An open combo popup should still scroll through available items.
            if isinstance(widget, QAbstractItemView):
                return None
            if isinstance(widget, cls._BLOCKED_TYPES):
                return widget
            widget = widget.parentWidget()
        return None

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Wheel:
            return False
        owner = self._blocked_owner(watched)
        if owner is None:
            return False
        self._scroll_nearest_area(owner, event)
        event.accept()
        return True
