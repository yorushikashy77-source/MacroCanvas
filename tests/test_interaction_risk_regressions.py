import threading
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from engine.interception import InterceptionOutput
from ui.editor_workflow import EditorWorkflowMixin
from ui.editors import ActionDurationEditor, ActionTreeWidget
from ui.input_runtime import InputRuntimeMixin
from ui.mapping_editor import MappingEditorMixin
from ui.trigger_conflicts import TriggerConflictMixin


ROOT = Path(__file__).resolve().parents[1]


class _ConflictHarness(TriggerConflictMixin):
    global_toggle_enabled = True
    global_toggle_modifiers = "Ctrl+Shift"
    global_toggle_key = "F10"
    macro_pause_enabled = True
    macro_pause_modifiers = "Ctrl"
    macro_pause_key = "F9"
    emergency_modifiers = "无"
    emergency_key = "F8"
    recording_cancel_modifiers = "无"
    recording_cancel_key = "F7"
    recording_finish_modifiers = "无"
    recording_finish_key = "F8"
    profiles = []

    def __init__(self, mappings=None):
        self.base_profile_payload = {
            "mappings": list(mappings or []),
            "presets": [],
        }

    def _store_editor_payload(self):
        pass


class TriggerConflictRegressionTests(unittest.TestCase):
    def test_default_emergency_and_record_finish_f8_is_allowed(self):
        reports = _ConflictHarness().analyze_trigger_conflicts()
        errors = [item["message"] for item in reports if item["severity"] == "error"]
        self.assertEqual(errors, [])

    def test_same_main_key_with_different_finish_modifiers_is_still_rejected(self):
        harness = _ConflictHarness()
        harness.recording_finish_modifiers = "Ctrl"
        reports = harness.analyze_trigger_conflicts()
        errors = [item["message"] for item in reports if item["severity"] == "error"]
        self.assertTrue(any("共用主键 F8" in message for message in errors))

    def test_impossible_released_condition_is_rejected_before_apply(self):
        mappings = [
            {
                "id": "same-source",
                "enabled": True,
                "name": "来源键松开条件",
                "source_modifiers": "无",
                "source": "鼠标右键",
                "condition_enabled": True,
                "condition_input": "鼠标右键",
                "condition_state": "松开时",
            },
            {
                "id": "required-modifier",
                "enabled": True,
                "name": "修饰键松开条件",
                "source_modifiers": "Ctrl",
                "source": "A",
                "condition_enabled": True,
                "condition_input": "Ctrl",
                "condition_state": "松开时",
            },
        ]
        errors = _ConflictHarness(mappings).detect_trigger_conflicts()
        self.assertTrue(any("来源主键相同" in message for message in errors))
        self.assertTrue(any("来源快捷键的修饰键" in message for message in errors))


class _TaskController:
    def __init__(self, start_result):
        self.start_result = bool(start_result)
        self.started = []
        self.stopped = []

    def is_running(self, _task_id):
        return False

    def start(self, task):
        self.started.append(dict(task))
        return self.start_result

    def stop(self, task_id):
        self.stopped.append(task_id)


class _HoldTriggerHarness(InputRuntimeMixin):
    def __init__(self, start_result):
        self.input_state_lock = threading.RLock()
        self.held_trigger_ids = {}
        self.active_profile_id = "profile-a"
        self.macro_controller = _TaskController(start_result)
        self.diagnostics = []

    @staticmethod
    def _macro_backend_active():
        return True

    def write_diagnostic(self, event, **fields):
        self.diagnostics.append((event, fields))


class HoldTriggerOwnershipRegressionTests(unittest.TestCase):
    @staticmethod
    def _task():
        return {
            "id": "preset-1",
            "name": "按住任务",
            "execution_mode": "按住循环",
        }

    def test_failed_start_does_not_claim_or_stop_existing_task(self):
        harness = _HoldTriggerHarness(start_result=False)
        harness.handle_trigger_task(self._task(), "F6", True, False)
        self.assertEqual(harness.held_trigger_ids, {})
        harness.handle_trigger_task(self._task(), "F6", False, False)
        self.assertEqual(harness.macro_controller.stopped, [])
        self.assertTrue(any(
            event == "trigger_task_release_ignored"
            for event, _fields in harness.diagnostics
        ))

    def test_successful_start_is_stopped_by_its_own_release(self):
        harness = _HoldTriggerHarness(start_result=True)
        harness.handle_trigger_task(self._task(), "F6", True, False)
        self.assertEqual(harness.held_trigger_ids, {"F6": {"preset-1"}})
        harness.handle_trigger_task(self._task(), "F6", False, False)
        self.assertEqual(harness.macro_controller.stopped, ["preset-1"])
        self.assertEqual(harness.held_trigger_ids, {})


class _Timer:
    def __init__(self):
        self.starts = []

    def start(self, *args):
        self.starts.append(args)

    def stop(self):
        pass


class _AutoApplyHarness(EditorWorkflowMixin):
    def __init__(self):
        self.profile_switch_confirmation_active = True
        self.settings_dialog_active = False
        self.loading_task_stack = []
        self.auto_apply_timer = _Timer()
        self.apply_calls = 0

    def apply_changes(self):
        self.apply_calls += 1


class AutoApplyTransactionRegressionTests(unittest.TestCase):
    def test_auto_apply_is_deferred_during_profile_confirmation(self):
        harness = _AutoApplyHarness()
        harness.auto_apply_config()
        self.assertEqual(harness.apply_calls, 0)
        self.assertEqual(harness.auto_apply_timer.starts, [(500,)])


class _MappingEditorHarness(QWidget, MappingEditorMixin, EditorWorkflowMixin):
    def __init__(self):
        super().__init__()
        self.mapping_cards = []
        self.mapping_layout = QVBoxLayout(self)
        self.initializing = True

    def data_changed(self):
        pass

    def _loading_checkpoint(self, *args, **kwargs):
        pass

    def delete_mapping(self, _card):
        pass


class DurationRangeRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_action_duration_keeps_long_valid_values(self):
        editor = ActionDurationEditor("键盘点击", 15_000, 20_000)
        self.assertEqual(editor.value(), 15_000)
        self.assertEqual(editor.jitterValue(), 20_000)
        self.assertEqual(editor.base.maximum(), 600_000)
        self.assertEqual(editor.random.maximum(), 600_000)

    def test_mapping_duration_keeps_long_valid_values(self):
        harness = _MappingEditorHarness()
        harness.add_mapping({
            "id": "mapping-long-duration",
            "enabled": True,
            "name": "长按映射",
            "source_modifiers": "无",
            "source": "F6",
            "target_modifiers": "无",
            "target": "A",
            "condition_enabled": False,
            "condition_input": "鼠标左键",
            "condition_state": "按住时",
            "mode": "执行一次",
            "hold_ms": 15_000,
            "hold_jitter_ms": 20_000,
            "loop_count": 1,
            "loop_interval_ms": 0,
            "loop_interval_jitter_ms": 0,
            "speed_percent": 100,
            "max_runtime_s": 0,
        })
        card = harness.mapping_cards[-1]
        self.assertEqual(card.hold.value(), 15_000)
        self.assertEqual(card.hold_jitter.value(), 20_000)
        self.assertEqual(card.hold.maximum(), 600_000)
        self.assertEqual(card.hold_jitter.maximum(), 600_000)


class _ActionEditorHarness(EditorWorkflowMixin):
    def __init__(self):
        self.selected_preset_card = None

    def select_preset_card(self, card):
        self.selected_preset_card = card

    def update_card_action_summary(self, _card):
        pass

    def _loading_checkpoint(self, *args, **kwargs):
        pass

    def data_changed(self):
        pass


class _ActionCard:
    def __init__(self):
        self.action_table = ActionTreeWidget()
        self.action_table.setColumnCount(5)
        self.action_title = QLabel()
        self._actions_loaded = True



class LegacyComboRegressionTests(unittest.TestCase):
    def test_action_editor_round_trip_preserves_legacy_modifiers(self):
        QApplication.instance() or QApplication([])
        harness = _ActionEditorHarness()
        card = _ActionCard()
        item = harness.add_action({
            "action_id": "legacy-combo",
            "type": "键盘点击",
            "target": "A",
            "modifiers": "Ctrl+Shift",
            "hold_ms": 150,
            "jitter_ms": 0,
            "children": [],
        }, save=False, card=card)
        rebuilt = harness.action_from_item(card.action_table, item)
        self.assertEqual(rebuilt["modifiers"], "Ctrl+Shift")
        self.assertEqual(rebuilt["target"], "A")

    def test_atomic_combo_backend_uses_ordered_press_and_reverse_release(self):
        output = InterceptionOutput.__new__(InterceptionOutput)
        events = []
        output.send_key = lambda name, down: events.append((name, down)) or True
        output.send_mouse_button = lambda *_args: True

        action = {
            "type": "键盘点击",
            "target": "A",
            "modifiers": "Ctrl+Shift",
        }
        self.assertTrue(output.send_combo_action(action, "Press"))
        self.assertTrue(output.send_combo_action(action, "Release"))
        self.assertEqual(events, [
            ("Ctrl", True), ("Shift", True), ("A", True),
            ("A", False), ("Shift", False), ("Ctrl", False),
        ])

    def test_migration_keeps_modifier_field_instead_of_parallel_tree(self):
        source = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        start = source.index("                    legacy_modifiers = modifier_names(")
        end = source.index("                    if legacy_delay:", start)
        block = source[start:end]
        self.assertIn('copied["modifiers"] = "+".join(legacy_modifiers)', block)
        self.assertIn("migrated.append(copied)", block)
        self.assertNotIn('"target": legacy_modifiers[0]', block)


if __name__ == "__main__":
    unittest.main()
