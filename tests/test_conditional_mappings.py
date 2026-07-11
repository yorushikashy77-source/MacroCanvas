import threading
import unittest

from config.profiles import profile_summary
from config.schema import validate_config_payload
from engine.kanata import KanataConfigBuilder
from engine.trigger_resolver import mapping_condition_satisfied
from ui.input_runtime import InputRuntimeMixin
from ui.trigger_conflicts import TriggerConflictMixin


class MappingConditionModelTests(unittest.TestCase):
    def test_pressed_and_released_states_use_current_physical_snapshot(self):
        pressed = {
            "condition_enabled": True,
            "condition_input": "鼠标左键",
            "condition_state": "按住时",
        }
        released = dict(pressed, condition_state="松开时")
        self.assertTrue(mapping_condition_satisfied(pressed, {"鼠标左键"}))
        self.assertFalse(mapping_condition_satisfied(pressed, set()))
        self.assertFalse(mapping_condition_satisfied(released, {"鼠标左键"}))
        self.assertTrue(mapping_condition_satisfied(released, set()))
        self.assertTrue(mapping_condition_satisfied({}, set()))

    def test_schema_accepts_complete_condition_and_rejects_missing_state(self):
        mapping = {
            "id": "m1",
            "enabled": True,
            "source_modifiers": "无",
            "source": "鼠标右键",
            "target_modifiers": "无",
            "target": "Space",
            "condition_enabled": True,
            "condition_input": "鼠标左键",
            "condition_state": "按住时",
        }
        payload = {"mappings": [mapping], "presets": []}
        self.assertIs(validate_config_payload(payload), payload)
        invalid = {"mappings": [{
            key: value for key, value in mapping.items()
            if key != "condition_state"
        }], "presets": []}
        with self.assertRaises(ValueError):
            validate_config_payload(invalid)


class ConditionalKanataTests(unittest.TestCase):
    @staticmethod
    def _mapping(state="按住时"):
        return {
            "id": "m1",
            "enabled": True,
            "source_modifiers": "无",
            "source": "鼠标右键",
            "target_modifiers": "无",
            "target": "Space",
            "condition_enabled": True,
            "condition_input": "鼠标左键",
            "condition_state": state,
            "mode": "同步按住",
            "hold_ms": 100,
        }

    def test_conditional_sync_mapping_uses_state_switch_and_python_trigger(self):
        mapping = self._mapping()
        builder = KanataConfigBuilder(
            [mapping], [], global_toggle_enabled=False,
            macro_pause_enabled=False,
        )
        text = builder.build()
        self.assertIn("(input real mlft)", text)
        self.assertIn("(not (or (input real lctl) (input real rctl)))", text)
        self.assertIn("mc-trigger mapping m1 down", text)
        self.assertIn("push-msg mc-state mlft down", text)
        self.assertIn("push-msg mc-state mlft up", text)
        self.assertIn("mrgt mlft", text)
        summary = profile_summary({
            "payload": {"mappings": [mapping], "presets": []}
        })
        self.assertEqual(summary["virtual_keys"] + 1, len(builder.virtual_keys))

    def test_released_condition_generates_not_input_check(self):
        text = KanataConfigBuilder(
            [self._mapping("松开时")], [], global_toggle_enabled=False,
            macro_pause_enabled=False,
        ).build()
        self.assertIn("(not (input real mlft))", text)


class _Controller:
    def __init__(self):
        self.stopped = []

    def stop(self, task_id):
        self.stopped.append(task_id)


class _ConditionalRuntimeHarness(InputRuntimeMixin):
    def __init__(self, rule):
        self.input_state_lock = threading.RLock()
        self.data_lock = threading.RLock()
        self.physical_down = set()
        self.active_sync_by_source = {"鼠标右键": {rule["id"]: dict(rule)}}
        self.held_trigger_ids = {}
        self.runtime_trigger_rules = [dict(rule)]
        self.macro_controller = _Controller()
        self.suspended_mapping_ids = set()
        self.suspended_preset_ids = set()
        self.released = []
        self.diagnostics = []

    def _release_sync_mapping(self, mapping):
        self.released.append(mapping["id"])
        return True

    def write_diagnostic(self, event, **fields):
        self.diagnostics.append((event, fields))


class ConditionalRuntimeTests(unittest.TestCase):
    def test_sync_output_is_released_when_condition_becomes_false(self):
        rule = {
            "id": "m1",
            "enabled": True,
            "mode": "同步按住",
            "condition_enabled": True,
            "condition_input": "鼠标左键",
            "condition_state": "按住时",
            "_runtime_kind": "mapping",
        }
        harness = _ConditionalRuntimeHarness(rule)
        harness.physical_down.add("鼠标左键")
        self.assertFalse(harness._release_invalid_conditional_holds())
        harness.handle_kanata_state("鼠标左键", False)
        self.assertEqual(harness.released, ["m1"])
        self.assertEqual(harness.active_sync_by_source, {})

        # The later source Up must not release a second owner's shared target.
        harness._dispatch_runtime_mapping_rule(
            rule, "kanata:base:mapping:m1", False, False
        )
        self.assertEqual(harness.released, ["m1"])


class _ConflictHarness(TriggerConflictMixin):
    global_toggle_enabled = False
    global_toggle_modifiers = "无"
    global_toggle_key = "F10"
    macro_pause_enabled = False
    macro_pause_modifiers = "无"
    macro_pause_key = "F9"
    emergency_modifiers = "无"
    emergency_key = "F8"
    recording_cancel_modifiers = "无"
    recording_cancel_key = "F7"
    recording_finish_modifiers = "无"
    recording_finish_key = "F6"
    profiles = []

    def __init__(self, mappings):
        self.base_profile_payload = {"mappings": mappings, "presets": []}

    def _store_editor_payload(self):
        pass


class ConditionalConflictTests(unittest.TestCase):
    @staticmethod
    def _mapping(mapping_id, *, condition_enabled, condition_state="按住时"):
        return {
            "id": mapping_id,
            "enabled": True,
            "name": mapping_id,
            "source_modifiers": "无",
            "source": "鼠标右键",
            "condition_enabled": condition_enabled,
            "condition_input": "鼠标左键",
            "condition_state": condition_state,
        }

    def test_conditional_override_can_share_trigger_with_fallback(self):
        harness = _ConflictHarness([
            self._mapping("conditional", condition_enabled=True),
            self._mapping("fallback", condition_enabled=False),
        ])
        errors = [
            item for item in harness.analyze_trigger_conflicts()
            if item["severity"] == "error"
        ]
        self.assertEqual(errors, [])

    def test_duplicate_condition_is_still_an_error(self):
        harness = _ConflictHarness([
            self._mapping("first", condition_enabled=True),
            self._mapping("second", condition_enabled=True),
        ])
        errors = [
            item for item in harness.analyze_trigger_conflicts()
            if item["severity"] == "error"
        ]
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
