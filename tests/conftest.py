"""Pytest-wide cleanup for the shared PySide6 application instance."""

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


def pytest_sessionfinish(session, exitstatus):
    """Release test-created Qt windows before Python starts finalizing bindings."""
    del session, exitstatus
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return
    for widget in list(app.topLevelWidgets()):
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
