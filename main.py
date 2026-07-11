import atexit
import sys

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from core.constants import APP_DIR, APP_NAME
from ui.main_window import MainWindow


def main():
    if sys.platform != "win32":
        raise SystemExit("MacroCanvas 目前仅支持 Windows。")
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    app = QApplication(sys.argv)
    # The event loop may exit only after MainWindow.closeEvent has completed the
    # cancellable shutdown transaction. This avoids an aboutToQuit callback whose
    # False result cannot stop QApplication from terminating.
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    APP_DIR.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(APP_DIR / "MacroCanvas.lock"))
    if not instance_lock.tryLock(100):
        QMessageBox.warning(
            None,
            "MacroCanvas 已在运行",
            "检测到另一个 MacroCanvas 实例。为避免配置覆盖、输入钩子冲突和"
            "互相终止 Kanata，本次启动已取消。",
        )
        return
    window = MainWindow()

    def quit_after_safe_window_close():
        if getattr(window, "_shutdown_complete", False):
            app.quit()
        else:
            # A close not routed through the coordinated closeEvent is rejected
            # by keeping the application alive and restoring the main window.
            window.show()
            window.raise_()
            window.activateWindow()

    app.lastWindowClosed.connect(quit_after_safe_window_close)
    atexit.register(window.emergency_shutdown_fallback)
    window.show()
    exit_code = app.exec()
    if not getattr(window, "_shutdown_complete", False):
        window.shutdown()
    instance_lock.unlock()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
