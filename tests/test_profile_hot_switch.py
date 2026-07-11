import unittest
from pathlib import Path

from config.profiles import (
    normalize_profile, profile_layer_name, profile_matches, profile_namespace,
    profile_summary, select_profile,
)
from config.schema import validate_config_payload
from engine.kanata import KanataConfigBuilder


class ProfileModelTests(unittest.TestCase):
    def test_windows_path_and_priority(self):
        first = {
            "enabled": True,
            "process_names": ["game.exe"],
            "title_contains": [],
        }
        second = {
            "enabled": True,
            "process_names": ["game.exe"],
            "title_contains": ["arena"],
        }
        self.assertTrue(profile_matches(first, r"C:\\Games\\GAME.EXE", "Arena"))
        self.assertIs(select_profile([first, second], "game.exe", "Arena"), first)

    def test_profile_payload_keeps_only_mappings_and_presets(self):
        profile = normalize_profile({
            "id": "p1",
            "payload": {
                "engine_backend": "游戏模式（Interception）",
                "emergency_key": "F9",
                "mappings": [{"id": "m1"}],
                "presets": [{"id": "s1", "actions": []}],
            },
        })
        self.assertEqual(set(profile["payload"]), {"mappings", "presets"})

    def test_layer_and_namespace_are_stable_and_distinct(self):
        self.assertEqual(profile_layer_name("abc"), profile_layer_name("abc"))
        self.assertNotEqual(profile_layer_name("abc"), profile_layer_name("def"))
        self.assertNotEqual(profile_namespace("abc"), profile_namespace("def"))

    def test_virtual_key_estimate_matches_builder_for_one_layer(self):
        mapping = {
            "id": "m1", "enabled": True, "source_modifiers": "无",
            "source": "F6", "target_modifiers": "无", "target": "A",
            "mode": "执行一次", "hold_ms": 50,
        }
        preset = {
            "id": "p1", "enabled": True, "trigger_modifiers": "无",
            "trigger": "F7", "actions": [
                {"type": "键盘点击", "target": "B", "children": []},
                {"type": "等待", "wait_ms": 50, "children": []},
            ],
        }
        summary = profile_summary({
            "payload": {"mappings": [mapping], "presets": [preset]}
        })
        builder = KanataConfigBuilder([mapping], [preset])
        builder.build()
        self.assertEqual(summary["virtual_keys"] + 1, len(builder.virtual_keys))


class ProfileSchemaTests(unittest.TestCase):
    def test_profile_snapshot_accepts_legacy_global_fields_but_validates_actions(self):
        payload = {
            "version": 23,
            "profile_auto_switch_enabled": True,
            "mappings": [],
            "presets": [],
            "profiles": [{
                "id": "profile1",
                "name": "测试",
                "enabled": True,
                "process_names": ["game.exe"],
                "title_contains": [],
                "payload": {
                    "engine_backend": "普通模式（winIOv2）",
                    "mappings": [],
                    "presets": [],
                },
            }],
        }
        self.assertIs(validate_config_payload(payload), payload)


class MultiLayerKanataTests(unittest.TestCase):
    def _mapping(self, mapping_id, source, target):
        return {
            "id": mapping_id,
            "enabled": True,
            "source_modifiers": "无",
            "source": source,
            "target_modifiers": "无",
            "target": target,
            "mode": "执行一次",
            "hold_ms": 50,
        }

    def test_profiles_are_precompiled_into_distinct_layers(self):
        profile_id = "profile-one"
        builder = KanataConfigBuilder(
            [self._mapping("same", "F6", "A")],
            [],
            profiles=[{
                "id": profile_id,
                "name": "档案",
                "enabled": True,
                "payload": {
                    "mappings": [self._mapping("same", "F7", "B")],
                    "presets": [],
                },
            }],
        )
        text = builder.build()
        layer = profile_layer_name(profile_id)
        namespace = profile_namespace(profile_id)
        self.assertIn("(deflayer base", text)
        self.assertIn(f"(deflayer {layer}", text)
        self.assertIn("(deflayer disabled", text)
        self.assertNotIn("(deflayer switching", text)
        self.assertIn("mc-trigger base mapping same down", text)
        self.assertIn(f"mc-trigger {layer} mapping same down", text)
        self.assertIn(KanataConfigBuilder.mapping_key("same"), builder.virtual_key_names)
        self.assertIn(
            KanataConfigBuilder.mapping_key("same", namespace),
            builder.virtual_key_names,
        )
        self.assertIn("f6", text)
        self.assertIn("f7", text)

    def test_single_layer_keeps_legacy_trigger_message_shape(self):
        text = KanataConfigBuilder(
            [self._mapping("m1", "F6", "A")], []
        ).build()
        self.assertIn("mc-trigger mapping m1 down", text)
        self.assertNotIn("mc-trigger base mapping m1 down", text)

    def test_virtual_key_limit_counts_all_profiles(self):
        old_limit = KanataConfigBuilder.MAX_VIRTUAL_KEYS
        try:
            KanataConfigBuilder.MAX_VIRTUAL_KEYS = 3
            with self.assertRaises(ValueError):
                KanataConfigBuilder(
                    [self._mapping("m1", "F6", "A")],
                    [],
                    profiles=[{
                        "id": "p1", "enabled": True,
                        "payload": {
                            "mappings": [self._mapping("m2", "F7", "B")],
                            "presets": [],
                        },
                    }],
                ).build()
        finally:
            KanataConfigBuilder.MAX_VIRTUAL_KEYS = old_limit


class MainWindowStaticFlowTests(unittest.TestCase):
    def test_profile_switch_is_direct_without_held_input_wait(self):
        text = (Path(__file__).parents[1] / "ui" / "profile_workflow.py").read_text(
            encoding="utf-8"
        )
        start = text.index("    def _activate_profile_by_id")
        end = text.index("    def check_foreground_profile", start)
        method = text[start:end]
        self.assertNotIn("SWITCHING_LAYER_NAME", method)
        self.assertNotIn("if self.physical_down:", method)
        self.assertIn('transition="disabled_then_released"', method)
        self.assertIn("_change_runtime_profile_layer", method)
        self.assertNotIn("self.engine.stop()", method)
        self.assertNotIn("self.engine.start(", method)
        self.assertIn("stop_all_macros", method)

    def test_auto_match_uses_applied_profile_snapshot(self):
        text = (Path(__file__).parents[1] / "ui" / "profile_workflow.py").read_text(
            encoding="utf-8"
        )
        start = text.index("    def check_foreground_profile")
        method = text[start:]
        self.assertIn("runtime_profile_auto_switch_enabled", method)
        self.assertIn("select_profile(self.runtime_profiles", method)
        self.assertNotIn("select_profile(self.profiles", method)

    def test_normal_hook_has_no_profile_transition_source_block(self):
        text = (Path(__file__).parents[1] / "ui" / "input_runtime.py").read_text(
            encoding="utf-8"
        )
        start = text.index("    def _global_hook_callback")
        end = text.index("    @Slot(str, bool)\n    def handle_global_input", start)
        method = text[start:end]
        self.assertIn('"kanata_owns_input"', method)
        self.assertIn('"kanata_control_consumed"', method)
        self.assertNotIn("profile_blocked_sources", method)
        self.assertNotIn("_queue_profile_input_state_refresh", method)


if __name__ == "__main__":
    unittest.main()
