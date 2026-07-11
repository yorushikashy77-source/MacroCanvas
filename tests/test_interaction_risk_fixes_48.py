import unittest
from pathlib import Path

from config.schema import validate_config_payload


ROOT = Path(__file__).resolve().parents[1]


class RuntimeSchemaFailClosedTests(unittest.TestCase):
    def test_enabled_mapping_requires_explicit_source_and_target(self):
        with self.assertRaisesRegex(ValueError, "source"):
            validate_config_payload({
                "mappings": [{
                    "id": "m1",
                    "enabled": True,
                    "target_modifiers": "无",
                    "target": "A",
                }],
                "presets": [],
            })
        with self.assertRaisesRegex(ValueError, "target"):
            validate_config_payload({
                "mappings": [{
                    "id": "m1",
                    "enabled": True,
                    "source_modifiers": "无",
                    "source": "F6",
                }],
                "presets": [],
            })

    def test_enabled_preset_requires_trigger_actions_and_action_targets(self):
        with self.assertRaisesRegex(ValueError, "trigger"):
            validate_config_payload({
                "mappings": [],
                "presets": [{"id": "p1", "enabled": True, "actions": []}],
            })
        with self.assertRaisesRegex(ValueError, "actions"):
            validate_config_payload({
                "mappings": [],
                "presets": [{
                    "id": "p1",
                    "enabled": True,
                    "trigger_modifiers": "无",
                    "trigger": "F1",
                    "actions": [],
                }],
            })
        with self.assertRaisesRegex(ValueError, "target"):
            validate_config_payload({
                "mappings": [],
                "presets": [{
                    "id": "p1",
                    "enabled": True,
                    "trigger_modifiers": "无",
                    "trigger": "F1",
                    "actions": [{"type": "键盘点击", "children": []}],
                }],
            })

    def test_enabled_profile_requires_a_match_condition(self):
        with self.assertRaisesRegex(ValueError, "匹配"):
            validate_config_payload({
                "mappings": [],
                "presets": [],
                "profiles": [{
                    "id": "profile-1",
                    "name": "空条件档案",
                    "enabled": True,
                    "process_names": [],
                    "title_contains": [],
                    "payload": {"mappings": [], "presets": []},
                }],
            })


class ProfileManagerStaticTextTests(unittest.TestCase):
    def test_profile_manager_uses_staging_wording_and_disables_blank_profiles(self):
        manager = (ROOT / "ui" / "profile_manager.py").read_text("utf-8")
        self.assertIn("暂存档案修改", manager)
        self.assertIn('"enabled": False', manager)
        self.assertIn("_profile_has_match_condition", manager)
        self.assertIn("请先设置至少一个匹配条件", manager)


if __name__ == "__main__":
    unittest.main()
