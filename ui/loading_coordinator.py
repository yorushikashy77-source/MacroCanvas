"""Nested in-window loading overlay coordination."""

from PySide6.QtCore import QEventLoop
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox, QWidget

from ui.overlays import LoadingOverlay


class LoadingCoordinatorMixin:
    def _loading_host_widget(self):
        active = QApplication.activeWindow()
        if isinstance(active, QWidget) and active.isVisible():
            if not isinstance(active, (QMessageBox, QInputDialog)):
                return active
        return self

    def _begin_loading(self, title, detail="", host=None):
        requested_host = host
        if not isinstance(requested_host, QWidget) or not requested_host.isVisible():
            host = self._loading_host_widget()
        else:
            host = requested_host
        if not isinstance(host, QWidget) or not host.isVisible():
            host = self
        title = str(title or "正在处理")
        detail = str(detail or "请稍候……")
        if not self.loading_task_stack or self.loading_overlay is None:
            self.loading_overlay = LoadingOverlay(host)
            actual_host = host
        else:
            actual_host = self.loading_overlay.parentWidget() or host
        self.loading_task_stack.append({
            "title": title,
            "detail": detail,
            "host": actual_host,
        })
        self.loading_event_counter = 0
        self.loading_overlay.start_loading(title, detail)
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 12
        )

    def _set_loading_message(self, title=None, detail=None, force=True):
        if not self.loading_task_stack:
            return
        current = self.loading_task_stack[-1]
        if title is not None:
            current["title"] = str(title)
        if detail is not None:
            current["detail"] = str(detail)
        if self.loading_overlay is not None:
            self.loading_overlay.set_message(title, detail)
        if force:
            self._loading_checkpoint(force=True)

    def _loading_checkpoint(self, force=False):
        if not self.loading_task_stack:
            return
        self.loading_event_counter += 1
        if not force and self.loading_event_counter % 12:
            return
        if self.loading_overlay is not None:
            self.loading_overlay.sync_geometry()
            self.loading_overlay.raise_()
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 10
        )

    def _end_loading(self):
        if not self.loading_task_stack:
            return
        self.loading_task_stack.pop()
        self.loading_event_counter = 0
        if self.loading_task_stack:
            previous = self.loading_task_stack[-1]
            if self.loading_overlay is not None:
                self.loading_overlay.set_message(
                    previous["title"], previous["detail"]
                )
                self.loading_overlay.raise_()
        else:
            overlay = self.loading_overlay
            self.loading_overlay = None
            if overlay is not None:
                overlay.stop_loading()
                overlay.deleteLater()
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 8
        )
