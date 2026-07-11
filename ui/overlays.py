from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QVBoxLayout, QWidget,
)


class ActivityOverlay(QWidget):
    """Top-right, click-through status HUD for macro execution and recording."""

    def __init__(self):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        try:
            flags |= Qt.WindowType.WindowTransparentForInput
        except AttributeError:
            pass
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowOpacity(0.92)

        root = QFrame(self)
        root.setObjectName("activityOverlayCard")
        self._root = root
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card_layout = QVBoxLayout(root)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(4)
        self.title_label = QLabel()
        self.title_label.setObjectName("activityOverlayTitle")
        self.detail_label = QLabel()
        self.detail_label.setObjectName("activityOverlayDetail")
        self.detail_label.setWordWrap(True)
        card_layout.addWidget(self.title_label)
        card_layout.addWidget(self.detail_label)
        layout.addWidget(root)
        self.setMinimumWidth(330)
        self.setMaximumWidth(520)
        self._message_generation = 0
        self.hide()

    def show_message(self, title, detail="", accent="#7dd3fc"):
        self._message_generation += 1
        generation = self._message_generation
        self.title_label.setText(str(title))
        self.detail_label.setText(str(detail))
        self._root.setStyleSheet(
            "QFrame#activityOverlayCard {"
            "background: rgba(18, 24, 38, 218);"
            f"border: 2px solid {accent};"
            "border-radius: 12px;"
            "}"
            "QLabel#activityOverlayTitle {"
            "color: #f8fafc; font-size: 15px; font-weight: 700;"
            "background: transparent; border: none;"
            "}"
            "QLabel#activityOverlayDetail {"
            "color: #dbeafe; font-size: 13px;"
            "background: transparent; border: none;"
            "}"
        )
        self.adjustSize()
        self._move_to_top_right()
        self.show()
        self.raise_()
        return generation

    def _move_to_top_right(self):
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        margin = 22
        self.move(
            geometry.right() - self.width() - margin + 1,
            geometry.top() + margin,
        )

    def hide_message(self, generation=None):
        if generation is not None and generation != self._message_generation:
            return False
        self._message_generation += 1
        self.hide()
        return True


class LoadingSpinner(QWidget):
    """Small timer-driven spinner used by the in-window loading card."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._step = 0
        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._advance)
        self.setFixedSize(46, 46)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent; border: none;")

    def start(self):
        if not self._timer.isActive():
            self._timer.start()
        self.show()
        self.update()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _advance(self):
        self._step = (self._step + 1) % 12
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        inner_radius = 11.0
        outer_radius = 18.0
        for index in range(12):
            phase = (index - self._step) % 12
            alpha = max(36, 255 - phase * 18)
            color = QColor(132, 106, 255, alpha)
            pen = QPen(color, 3.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            angle = math.radians(index * 30.0 - 90.0)
            start = QPointF(
                center.x() + math.cos(angle) * inner_radius,
                center.y() + math.sin(angle) * inner_radius,
            )
            end = QPointF(
                center.x() + math.cos(angle) * outer_radius,
                center.y() + math.sin(angle) * outer_radius,
            )
            painter.drawLine(start, end)


class LoadingOverlay(QWidget):
    """Card-style loading overlay embedded in an existing program window.

    It is a child widget rather than a top-level dialog, so it never appears in
    the taskbar and does not look like a separate Windows progress window.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("loadingOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        # The loading card is informational only.  It must never turn the editor
        # into a modal/disabled surface; user-input locking is controlled solely
        # by the macro-running state in MainWindow.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.addStretch(1)

        center_row = QHBoxLayout()
        center_row.addStretch(1)
        self.card = QFrame(self)
        self.card.setObjectName("loadingCard")
        self.card.setMinimumWidth(370)
        self.card.setMaximumWidth(520)
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.card.setGraphicsEffect(shadow)
        self.card.setStyleSheet(
            "QFrame#loadingCard {"
            "background: rgba(20, 27, 39, 246);"
            "border: 1px solid rgba(132, 106, 255, 180);"
            "border-radius: 16px;"
            "}"
            "QLabel#loadingTitle {"
            "background: transparent; border: none;"
            "color: #f8fafc; font-size: 16px; font-weight: 700;"
            "}"
            "QLabel#loadingDetail {"
            "background: transparent; border: none;"
            "color: #9eacc0; font-size: 13px;"
            "}"
        )
        card_layout = QHBoxLayout(self.card)
        card_layout.setContentsMargins(22, 18, 24, 18)
        card_layout.setSpacing(16)
        self.spinner = LoadingSpinner(self.card)
        card_layout.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(5)
        self.title_label = QLabel("正在处理")
        self.title_label.setObjectName("loadingTitle")
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("loadingDetail")
        self.detail_label.setWordWrap(True)
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.detail_label)
        card_layout.addLayout(text_layout, 1)

        center_row.addWidget(self.card)
        center_row.addStretch(1)
        outer.addLayout(center_row)
        outer.addStretch(1)
        self.hide()

    def attach_to(self, parent):
        if parent is None:
            return
        if self.parentWidget() is not parent:
            self.setParent(parent)
        self.sync_geometry()

    def sync_geometry(self):
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())

    def start_loading(self, title, detail=""):
        self.title_label.setText(str(title or "正在处理"))
        self.detail_label.setText(str(detail or "请稍候……"))
        self.sync_geometry()
        self.spinner.start()
        self.show()
        self.raise_()
        self.update()

    def set_message(self, title=None, detail=None):
        if title is not None:
            self.title_label.setText(str(title))
        if detail is not None:
            self.detail_label.setText(str(detail))
        self.raise_()
        self.update()

    def stop_loading(self):
        self.spinner.stop()
        self.hide()

    def event(self, event):
        return super().event(event)

    def paintEvent(self, _event):
        # Keep the parent editor visually unchanged.  Only the centered card and
        # spinner are drawn; no full-window dark mask is painted.
        return
