import ctypes
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if not hasattr(ctypes, "WINFUNCTYPE"):
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE

try:
    from PySide6.QtCore import QPoint, QPointF, QSize, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QTreeWidgetItem
    from ui.editors import ActionTreeWidget
except Exception as exc:  # pragma: no cover - optional GUI dependency
    QApplication = None
    ActionTreeWidget = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(ActionTreeWidget is None, f"PySide6/Qt unavailable: {IMPORT_ERROR}")
class ActionMenuDragInteraction68QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_tree(self, count=40):
        tree = ActionTreeWidget()
        tree.setColumnCount(5)
        tree.setColumnWidth(0, 145)
        tree.resize(520, 300)
        for index in range(count):
            item = QTreeWidgetItem([f"动作 {index}", "", "", "", ""])
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEnabled
            )
            item.setSizeHint(0, QSize(0, 44))
            tree.addTopLevelItem(item)
        tree.show()
        self.app.processEvents()
        return tree

    @staticmethod
    def wheel_event(widget, pos, delta_y, buttons=Qt.MouseButton.NoButton):
        return QWheelEvent(
            QPointF(pos), QPointF(widget.mapToGlobal(pos)),
            QPoint(0, 0), QPoint(0, delta_y), buttons,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
        )

    def test_left_hold_wheel_has_same_immediate_delta_as_normal_wheel(self):
        normal = self.make_tree()
        row_rect = normal.visualItemRect(normal.topLevelItem(0))
        pos = QPoint(30, row_rect.center().y())
        before = normal.verticalScrollBar().value()
        self.app.sendEvent(normal.viewport(), self.wheel_event(normal.viewport(), pos, -120))
        self.app.processEvents()
        normal_delta = normal.verticalScrollBar().value() - before

        held = self.make_tree()
        row_rect = held.visualItemRect(held.topLevelItem(0))
        pos = QPoint(30, row_rect.center().y())
        QTest.mousePress(held.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
        self.app.processEvents()
        self.assertTrue(held._drag_capture_active)
        before = held.verticalScrollBar().value()
        self.app.sendEvent(
            held.viewport(),
            self.wheel_event(held.viewport(), pos, -120, Qt.MouseButton.LeftButton),
        )
        self.app.processEvents()
        held_delta = held.verticalScrollBar().value() - before

        self.assertEqual(held_delta, normal_delta)
        self.assertGreater(held_delta, 0)

    def test_manual_drag_replaces_native_drag_and_scrolls_one_row_per_tick(self):
        tree = self.make_tree()
        self.assertFalse(tree.dragEnabled())
        row_rect = tree.visualItemRect(tree.topLevelItem(0))
        pos = QPoint(30, row_rect.center().y())
        QTest.mousePress(tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
        QTest.mouseMove(tree.viewport(), pos + QPoint(0, QApplication.startDragDistance() + 8))
        self.app.processEvents()
        self.assertTrue(tree._manual_dragging)
        self.assertTrue(tree._drag_active)
        before = tree.verticalScrollBar().value()
        tree._scroll_one_action_card(1)
        self.app.processEvents()
        self.assertEqual(tree.verticalScrollBar().value() - before, 1)
        self.assertEqual(tree._drag_scroll_timer.interval(), 500)


if __name__ == "__main__":
    unittest.main()
