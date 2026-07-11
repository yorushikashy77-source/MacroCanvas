import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActionMenuDragInteraction59StaticTests(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text("utf-8")

    def test_action_menu_buttons_insert_after_current_item(self):
        preset_source = self.read("ui/preset_editor.py")
        workflow_source = self.read("ui/editor_workflow.py")
        self.assertIn("def add_action_from_menu", workflow_source)
        self.assertIn("_action_insert_position_after_current", workflow_source)
        self.assertIn("parent.indexOfChild(current)", workflow_source)
        self.assertIn("table.indexOfTopLevelItem(current)", workflow_source)
        self.assertIn("self.add_action_from_menu", preset_source)
        self.assertNotIn("clicked.connect(lambda _checked=False, c=card: self.add_action({", preset_source)
        ast.parse(preset_source)
        ast.parse(workflow_source)

    def test_offscreen_drag_uses_visible_edge_row_not_viewport_fallback(self):
        source = self.read("ui/editors.py")
        self.assertIn("def _nearest_visible_drop_target", source)
        start = source.index("    def _calculate_drop_target")
        end = source.index("    def _update_drop_indicator", start)
        block = source[start:end]
        self.assertIn("edge_target, edge_position = self._nearest_visible_drop_target", block)
        self.assertIn("return edge_target, edge_position", block)
        self.assertNotIn("if target is None:\n            return None, on_viewport", block)
        ast.parse(source)

    def test_drag_wheel_is_handled_from_viewport_and_item_widgets(self):
        source = self.read("ui/editors.py")
        self.assertIn("self.viewport().installEventFilter(self)", source)
        self.assertIn("event_type == QEvent.Wheel", source)
        self.assertIn("def _handle_drag_wheel_event", source)
        self.assertIn("self._handle_drag_wheel_event(event)", source)
        self.assertIn("拖拽期间滚轮只服务于动作树", source)
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
