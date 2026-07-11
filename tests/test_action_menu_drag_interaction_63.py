import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActionMenuDragInteraction63StaticTests(unittest.TestCase):
    def source(self):
        return (ROOT / "ui/editors.py").read_text("utf-8")

    def test_edge_autoscroll_waits_before_first_fixed_step(self):
        source = self.source()
        self.assertIn("self._drag_scroll_interval_s = 0.5", source)
        self.assertIn("self._drag_scroll_timer.setInterval(500)", source)
        interval_block = source[
            source.index("    def _drag_scroll_interval"):
            source.index("    def _ordered_effective_action_items")
        ]
        self.assertIn("第一次进入边缘也等待完整间隔", interval_block)
        self.assertIn("self._drag_scroll_last_step_at = now", interval_block)
        self.assertIn("return self._drag_scroll_interval_s", interval_block)
        self.assertNotIn("elapsed /", interval_block)
        self.assertNotIn("time_pressure", interval_block)
        self.assertNotIn("distance_pressure", interval_block)
        ast.parse(source)

    def test_view_scroll_advances_by_action_row_without_scroll_to_item_snap(self):
        source = self.source()
        self.assertIn("def _action_card_step_pixels", source)
        scroll_block = source[
            source.index("    def _scroll_item_one_card_into_view"):
            source.index("    def _scroll_one_action_card")
        ]
        self.assertIn("tree_step = self._tree_action_scroll_step(items)", scroll_block)
        self.assertIn("outer_step = self._action_card_step_pixels(items)", scroll_block)
        self.assertIn("direction * tree_step", scroll_block)
        self.assertIn("direction * outer_step", scroll_block)
        self.assertNotIn("self.scrollToItem", scroll_block)
        self.assertNotIn("PositionAtTop", scroll_block)
        self.assertNotIn("PositionAtBottom", scroll_block)
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
