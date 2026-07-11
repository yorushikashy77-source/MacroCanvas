import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActionMenuDragInteraction66StaticTests(unittest.TestCase):
    def source(self):
        return (ROOT / "ui/editors.py").read_text("utf-8")

    def test_global_drag_events_are_swallowed_while_drag_active(self):
        source = self.source()
        block = source[
            source.index("    def eventFilter"):
            source.index("    def _install_item_widget_event_filters")
        ]
        self.assertIn("event_type in (QEvent.DragMove, QEvent.DragEnter)", block)
        self.assertIn("self._update_drop_indicator_from_global(QCursor.pos())", block)
        self.assertIn("event.setDropAction(Qt.MoveAction)", block)
        self.assertIn("return True", block)
        ast.parse(source)

    def test_drag_wheel_uses_normal_scroll_lines_and_remainders(self):
        source = self.source()
        block = source[
            source.index("    def _scroll_drag_wheel_like_normal"):
            source.index("    def _handle_drag_wheel_event")
        ]
        self.assertIn("QApplication.wheelScrollLines()", block)
        self.assertIn("_drag_wheel_angle_remainder", block)
        self.assertIn("_drag_wheel_pixel_remainder", block)
        self.assertIn("_scroll_normal_wheel_delta", block)
        self.assertNotIn("_scroll_one_action_card", block)
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
