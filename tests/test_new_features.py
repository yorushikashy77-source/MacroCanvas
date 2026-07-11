import unittest

from config.profiles import profile_matches, select_profile
from config.transfer import clone_preset_for_import
from engine.kanata import KanataConfigBuilder
from macro.recording import simplify_recorded_actions


class ImportExportTests(unittest.TestCase):
    def test_import_rewrites_action_and_loop_ids(self):
        preset = {
            "id": "preset-old", "name": "测试",
            "actions": [
                {"type": "键盘点击", "action_id": "a", "target": "A", "children": []},
                {"type": "循环动作", "id": "loop", "target_action_ids": ["a"],
                 "children": []},
            ],
        }
        copied = clone_preset_for_import(preset)
        self.assertNotEqual(copied["id"], preset["id"])
        new_action_id = copied["actions"][0]["action_id"]
        self.assertNotEqual(new_action_id, "a")
        self.assertEqual(copied["actions"][1]["target_action_ids"], [new_action_id])


class RecordingCleanupTests(unittest.TestCase):
    def test_safe_cleanup_trims_waits_and_merges_wheel(self):
        actions = [
            {"type": "等待", "wait_ms": 100, "children": []},
            {"type": "鼠标滚轮", "target": "向上", "steps": 1, "children": []},
            {"type": "鼠标滚轮", "target": "向上", "steps": 2, "children": []},
            {"type": "等待", "wait_ms": 100, "children": []},
        ]
        cleaned = simplify_recorded_actions(actions)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["steps"], 3)


class ProfileTests(unittest.TestCase):
    def test_process_and_title_matching_is_case_insensitive(self):
        profile = {
            "enabled": True,
            "process_names": ["Game.EXE"],
            "title_contains": ["Arena"],
        }
        self.assertTrue(profile_matches(profile, r"C:\Games\game.exe", "My ARENA"))
        self.assertIs(select_profile([profile], "GAME.EXE", "arena"), profile)


class CoordinateTests(unittest.TestCase):
    def test_percentage_coordinates_generate_normalized_values(self):
        self.assertEqual(
            KanataConfigBuilder._normalized_mouse_position("pct:50,30"),
            (32768, 19660),
        )
        with self.assertRaises(ValueError):
            KanataConfigBuilder._normalized_mouse_position("rel:10,20")


if __name__ == "__main__":
    unittest.main()
