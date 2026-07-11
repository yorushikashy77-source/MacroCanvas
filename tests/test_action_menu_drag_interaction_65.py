import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActionMenuDragInteraction65StaticTests(unittest.TestCase):
    def source(self):
        return (ROOT / "ui/editors.py").read_text("utf-8")

    def test_drag_wheel_filter_is_global_only_during_drag(self):
        source = self.source()
        self.assertIn("self._drag_global_wheel_filter_installed = False", source)
        self.assertIn("def _set_drag_global_wheel_filter_enabled", source)
        filter_block = source[
            source.index("    def _set_drag_global_wheel_filter_enabled"):
            source.index("    def _clear_drag_state")
        ]
        self.assertIn("app.installEventFilter(self)", filter_block)
        self.assertIn("app.removeEventFilter(self)", filter_block)
        clear_block = source[
            source.index("    def _clear_drag_state"):
            source.index("    def dragEnterEvent")
        ]
        self.assertIn("self._set_drag_global_wheel_filter_enabled(False)", clear_block)
        drag_block = source[
            source.index("    def dragEnterEvent"):
            source.index("    def dragLeaveEvent")
        ]
        self.assertGreaterEqual(
            drag_block.count("self._set_drag_global_wheel_filter_enabled(True)"),
            2,
        )
        ast.parse(source)

    def test_no_accelerating_terms_remain_in_drag_interval(self):
        source = self.source()
        interval_block = source[
            source.index("    def _drag_scroll_interval"):
            source.index("    def _ordered_effective_action_items")
        ]
        forbidden = (
            "overflow /",
            "time_pressure",
            "distance_pressure",
            "** 3",
            "_drag_scroll_min_interval_s",
            "_drag_scroll_max_interval_s",
        )
        for token in forbidden:
            self.assertNotIn(token, interval_block)
        self.assertIn("self._drag_scroll_last_step_at = now", interval_block)
        self.assertIn("return self._drag_scroll_interval_s", interval_block)
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
