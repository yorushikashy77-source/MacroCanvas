import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActionMenuDragInteraction61StaticTests(unittest.TestCase):
    def source(self):
        return (ROOT / "ui/editors.py").read_text("utf-8")

    def test_native_dragmove_no_longer_controls_edge_autoscroll(self):
        source = self.source()
        drag_enter = source[source.index("    def dragEnterEvent"):source.index("    def dragMoveEvent")]
        drag_move = source[source.index("    def dragMoveEvent"):source.index("    def dragLeaveEvent")]
        drag_leave = source[source.index("    def dragLeaveEvent"):source.index("    def wheelEvent")]
        self.assertIn("不再调用 QTreeWidget 的原生 dragEnterEvent", drag_enter)
        self.assertIn("event.setDropAction(Qt.MoveAction)", drag_enter)
        self.assertIn("event.accept()", drag_enter)
        self.assertIn("不调用 QTreeWidget.dragMoveEvent", drag_move)
        self.assertIn("滚动只能由 _drag_scroll_timer", drag_move)
        self.assertIn("event.setDropAction(Qt.MoveAction)", drag_move)
        self.assertIn("event.accept()", drag_move)
        self.assertNotIn("super().dragEnterEvent(event)", drag_enter)
        self.assertNotIn("super().dragMoveEvent(event)", drag_move)
        self.assertNotIn("super().dragLeaveEvent(event)", drag_leave)
        ast.parse(source)

    def test_edge_autoscroll_is_fixed_slow_and_requires_action_content(self):
        source = self.source()
        self.assertIn("self.setAutoScroll(False)", source)
        self.assertIn("self._drag_scroll_margin = 82", source)
        self.assertIn("self._drag_scroll_interval_s = 0.5", source)
        self.assertIn("self._drag_scroll_timer.setInterval(500)", source)
        direction = source[
            source.index("    def _drag_scroll_direction_for_pos"):
            source.index("    def _drag_scroll_interval")
        ]
        self.assertIn("self._can_scroll(self.verticalScrollBar(), direction)", direction)
        self.assertIn("outer = self._enclosing_scroll_area()", direction)
        self.assertIn("self._outer_has_action_content_beyond(outer, direction)", direction)
        self.assertNotIn("scrollToItem", direction)
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
