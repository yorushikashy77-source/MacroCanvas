import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActionMenuDragInteraction64StaticTests(unittest.TestCase):
    def source(self):
        return (ROOT / "ui/editors.py").read_text("utf-8")

    def test_tree_scrollbar_uses_row_units_not_card_pixels(self):
        source = self.source()
        self.assertIn("def _tree_action_scroll_step", source)
        helper = source[
            source.index("    def _tree_action_scroll_step"):
            source.index("    def _scroll_item_one_card_into_view")
        ]
        self.assertIn("ScrollPerItem", helper)
        self.assertIn("return 1", helper)
        self.assertIn("_action_card_step_pixels(items)", helper)
        scroll_block = source[
            source.index("    def _scroll_item_one_card_into_view"):
            source.index("    def _scroll_one_action_card")
        ]
        self.assertIn("tree_step = self._tree_action_scroll_step(items)", scroll_block)
        self.assertIn("outer_step = self._action_card_step_pixels(items)", scroll_block)
        self.assertIn("direction * tree_step", scroll_block)
        self.assertIn("direction * outer_step", scroll_block)
        ast.parse(source)

    def test_drag_wheel_uses_normal_wheel_path_with_remainders(self):
        source = self.source()
        wheel_block = source[
            source.index("    def _scroll_drag_wheel_like_normal"):
            source.index("    def _handle_drag_wheel_event")
        ]
        self.assertIn("QApplication.wheelScrollLines()", wheel_block)
        self.assertIn("_drag_wheel_angle_remainder", wheel_block)
        self.assertIn("_drag_wheel_pixel_remainder", wheel_block)
        self.assertIn("_scroll_normal_wheel_delta", wheel_block)
        self.assertIn("card_pixels = max(1, self._action_card_step_pixels())", wheel_block)
        self.assertNotIn("_scroll_one_action_card", wheel_block)
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
