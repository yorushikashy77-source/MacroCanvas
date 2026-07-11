import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActionMenuDragInteraction62StaticTests(unittest.TestCase):
    def source(self):
        return (ROOT / "ui/editors.py").read_text("utf-8")

    def test_edge_autoscroll_steps_by_action_card_slots(self):
        source = self.source()
        self.assertIn("以“动作卡片”为单位", source)
        self.assertIn("self._drag_scroll_interval_s", source)
        self.assertIn("def _ordered_effective_action_items", source)
        self.assertIn("def _drop_slot_from_target", source)
        self.assertIn("def _drop_target_from_slot", source)
        self.assertIn("def _scroll_one_action_card", source)
        step_block = source[
            source.index("    def _scroll_one_action_card"):
            source.index("    def _nearest_visible_drop_target")
        ]
        self.assertIn("current_slot + direction", step_block)
        self.assertIn("max(0, min(len(items),", step_block)
        self.assertIn("_scroll_item_one_card_into_view", step_block)
        self.assertNotIn("scrollToItem", step_block)
        ast.parse(source)

    def test_edge_dragmove_does_not_recalculate_target_every_mouse_event(self):
        source = self.source()
        update_block = source[
            source.index("    def _update_drop_indicator"):
            source.index("    def _update_drop_indicator_from_global")
        ]
        self.assertIn("进入边缘步进区", update_block)
        self.assertIn("if direction:", update_block)
        self.assertIn("return", update_block)
        auto_block = source[
            source.index("    def _auto_scroll_once"):
            source.index("    def _clear_drag_state")
        ]
        self.assertIn("interval = self._drag_scroll_interval", auto_block)
        self.assertIn("now - self._drag_scroll_last_step_at < interval", auto_block)
        self.assertIn("self._scroll_one_action_card(direction)", auto_block)
        self.assertNotIn("self._update_drop_indicator_from_global(global_pos)", auto_block)
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
