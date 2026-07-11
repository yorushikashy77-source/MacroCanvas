import unittest
from pathlib import Path

from macro.recording import simplify_recorded_actions


class RecordingCleanupRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.recording_workflow_text = (
            root / "ui" / "recording_workflow.py"
        ).read_text(encoding="utf-8")
        cls.editor_workflow_text = (
            root / "ui" / "editor_workflow.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def count(actions, action_type):
        def walk(items):
            for action in items:
                yield action
                yield from walk(action.get("children", []))

        return sum(action.get("type") == action_type for action in walk(actions))

    def test_recording_import_preview_reuses_organized_actions(self):
        method = self.recording_workflow_text[
            self.recording_workflow_text.index("    def preview_recording_import"):
            self.recording_workflow_text.index("    def convert_recording_to_actions")
        ]
        self.assertIn("organized_cache", method)
        self.assertIn('organized_cache["key"] == key', method)
        self.assertIn("return organized_actions(), mode.currentText()", method)

    def test_bulk_action_loading_suppresses_per_item_refresh(self):
        load_method = self.editor_workflow_text[
            self.editor_workflow_text.index("    def load_actions"):
            self.editor_workflow_text.index("    def open_action_cleanup_dialog")
        ]
        add_method = self.editor_workflow_text[
            self.editor_workflow_text.index("    def add_action"):
            self.editor_workflow_text.index(
                "    @staticmethod\n    def update_action_duration_field"
            )
        ]
        self.assertIn("table.setUpdatesEnabled(False)", load_method)
        self.assertIn("table.blockSignals(True)", load_method)
        self.assertIn("self._bulk_loading_actions = True", load_method)
        self.assertIn("getattr(self, \"_bulk_loading_actions\", False)", add_method)

    def test_short_wait_mouse_path_is_simplified_and_timing_is_kept(self):
        actions = []
        for index in range(5):
            actions.append({
                "type": "鼠标移动",
                "action_id": f"move-{index}",
                "target": f"{index * 10},{index * 10}",
                "children": [],
            })
            if index < 4:
                actions.append({"type": "等待", "wait_ms": 80, "children": []})

        cleaned = simplify_recorded_actions(
            actions,
            adjust_timing=False,
            trim_edge_waits=False,
            merge_gap_ms=120,
            move_tolerance=6,
        )

        self.assertEqual(self.count(cleaned, "鼠标移动"), 2)
        self.assertEqual(self.count(cleaned, "等待"), 1)
        self.assertEqual(cleaned[1]["wait_ms"], 320)

    def test_all_coordinate_modes_can_be_simplified(self):
        for prefix in ("", "pct:", "window:", "client:", "rel:"):
            with self.subTest(prefix=prefix):
                actions = []
                for index in range(4):
                    target = (
                        "rel:10,10"
                        if prefix == "rel:"
                        else f"{prefix}{index * 10},{index * 10}"
                    )
                    actions.append({
                        "type": "鼠标移动", "target": target, "children": []
                    })
                    if index < 3:
                        actions.append({
                            "type": "等待", "wait_ms": 80, "children": []
                        })
                cleaned = simplify_recorded_actions(
                    actions, adjust_timing=False, trim_edge_waits=False
                )
                self.assertEqual(self.count(cleaned, "鼠标移动"), 2)

    def test_relative_path_preserves_total_displacement(self):
        actions = [
            {"type": "鼠标移动", "target": "rel:10,0", "children": []},
            {"type": "等待", "wait_ms": 80, "children": []},
            {"type": "鼠标移动", "target": "rel:10,0", "children": []},
            {"type": "等待", "wait_ms": 80, "children": []},
            {"type": "鼠标移动", "target": "rel:10,0", "children": []},
        ]
        cleaned = simplify_recorded_actions(
            actions, adjust_timing=False, trim_edge_waits=False
        )
        targets = [
            action["target"] for action in cleaned
            if action.get("type") == "鼠标移动"
        ]
        self.assertEqual(targets, ["rel:10,0", "rel:20,0"])

    def test_loop_referenced_action_is_not_removed(self):
        actions = [
            {
                "type": "鼠标移动", "action_id": "a",
                "target": "0,0", "children": [],
            },
            {"type": "等待", "wait_ms": 80, "children": []},
            {
                "type": "鼠标移动", "action_id": "b",
                "target": "10,10", "children": [],
            },
            {"type": "等待", "wait_ms": 80, "children": []},
            {
                "type": "鼠标移动", "action_id": "c",
                "target": "20,20", "children": [],
            },
        ]
        cleaned = simplify_recorded_actions(
            actions,
            protected_action_ids={"b"},
            adjust_timing=False,
            trim_edge_waits=False,
        )
        ids = [
            action.get("action_id") for action in cleaned
            if action.get("type") == "鼠标移动"
        ]
        self.assertEqual(ids, ["a", "b", "c"])

    def test_wheel_merge_respects_step_limit(self):
        actions = [
            {"type": "鼠标滚轮", "target": "向上", "steps": 60, "children": []},
            {"type": "等待", "wait_ms": 50, "children": []},
            {"type": "鼠标滚轮", "target": "向上", "steps": 60, "children": []},
        ]
        cleaned = simplify_recorded_actions(
            actions, adjust_timing=False, trim_edge_waits=False
        )
        self.assertEqual([action["steps"] for action in cleaned], [100, 20])

    def test_child_leading_wait_is_not_trimmed(self):
        actions = [{
            "type": "键盘点击", "target": "A", "hold_ms": 20,
            "children": [
                {"type": "等待", "wait_ms": 50, "children": []},
                {"type": "键盘点击", "target": "B", "hold_ms": 20, "children": []},
            ],
        }]
        cleaned = simplify_recorded_actions(actions)
        self.assertEqual(cleaned[0]["children"][0]["type"], "等待")


if __name__ == "__main__":
    unittest.main()
