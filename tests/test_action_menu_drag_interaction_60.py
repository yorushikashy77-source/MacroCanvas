import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActionMenuDragInteraction60StaticTests(unittest.TestCase):
    def source(self):
        return (ROOT / "ui/editors.py").read_text("utf-8")

    def test_drag_autoscroll_uses_fixed_timer_not_dragmove_burst(self):
        source = self.source()
        self.assertIn("import time", source)
        self.assertIn("self._drag_scroll_interval_s = 0.5", source)
        self.assertIn("self._drag_scroll_timer.setInterval(500)", source)
        self.assertIn("def _drag_scroll_interval", source)
        self.assertIn("time.monotonic()", source)
        interval = source[
            source.index("    def _drag_scroll_interval"):
            source.index("    def _ordered_effective_action_items")
        ]
        self.assertIn("固定 0.5 秒一步", interval)
        self.assertNotIn("time_pressure", interval)
        self.assertNotIn("distance_pressure", interval)
        start = source.index("    def dragMoveEvent")
        end = source.index("    def dragLeaveEvent", start)
        block = source[start:end]
        self.assertNotIn("self._auto_scroll_once()", block)
        self.assertIn("滚动只能由 _drag_scroll_timer", block)
        ast.parse(source)

    def test_offscreen_drag_target_uses_visible_edge_row_not_viewport_snap(self):
        source = self.source()
        target_block = source[
            source.index("    def _calculate_drop_target"):
            source.index("    def _update_drop_indicator", source.index("    def _calculate_drop_target"))
        ]
        self.assertIn("鼠标拖出动作树可视范围", target_block)
        self.assertIn("_nearest_visible_drop_target", target_block)
        self.assertNotIn("return None, on_viewport", target_block)
        self.assertNotIn("return None, QAbstractItemView.DropIndicatorPosition.OnViewport", target_block)
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
