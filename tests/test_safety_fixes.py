import importlib
import sys
import time
import types
import unittest
from pathlib import Path

import config.schema as schema
from config.schema import validate_config_payload, validate_preset_payload


ROOT = Path(__file__).resolve().parents[1]


def _wait_action(action_id):
    return {
        "action_id": action_id,
        "type": "等待",
        "wait_ms": 1,
        "children": [],
    }


class SchemaSafetyTests(unittest.TestCase):
    def test_loop_references_must_exist_and_be_contiguous(self):
        actions = [
            _wait_action("a1"),
            _wait_action("a2"),
            _wait_action("a3"),
            {
                "id": "loop",
                "type": "循环动作",
                "target_action_ids": ["a1", "missing"],
                "children": [],
            },
        ]
        with self.assertRaisesRegex(ValueError, "不存在"):
            validate_preset_payload({"actions": actions})

        actions[-1]["target_action_ids"] = ["a1", "a3"]
        with self.assertRaisesRegex(ValueError, "连续"):
            validate_preset_payload({"actions": actions})

        actions[-1]["target_action_ids"] = ["a1", "a2"]
        self.assertIs(validate_preset_payload({"actions": actions})["actions"], actions)

        root = _wait_action("root")
        root["children"] = [_wait_action("c1"), _wait_action("c2")]
        nested_range = {
            "id": "nested-range",
            "type": "循环动作",
            "target_action_ids": ["c1", "c2"],
            "children": [],
        }
        self.assertEqual(
            validate_preset_payload({"actions": [root, nested_range]})["actions"][1],
            nested_range,
        )

    def test_nested_reference_loop_is_rejected_but_legacy_owned_loop_can_migrate(self):
        nested = _wait_action("root")
        nested["children"] = [
            _wait_action("child"),
            {
                "id": "nested-loop",
                "type": "循环动作",
                "target_action_ids": ["child"],
                "children": [],
            },
        ]
        with self.assertRaisesRegex(ValueError, "根层级"):
            validate_preset_payload({"actions": [nested]})

        legacy = {
            "type": "循环动作",
            "target_action_ids": [],
            "children": [_wait_action("legacy-child")],
        }
        self.assertEqual(
            validate_preset_payload({"actions": [legacy]})["actions"][0],
            legacy,
        )

    def test_global_limits_are_enforced(self):
        old = (
            schema.MAX_PROFILE_COUNT,
            schema.MAX_MAPPINGS_PER_SCOPE,
            schema.MAX_TOTAL_MAPPINGS,
        )
        try:
            schema.MAX_PROFILE_COUNT = 1
            schema.MAX_MAPPINGS_PER_SCOPE = 1
            schema.MAX_TOTAL_MAPPINGS = 1
            with self.assertRaisesRegex(ValueError, "档案数量"):
                validate_config_payload({
                    "mappings": [], "presets": [],
                    "profiles": [{"id": "p1"}, {"id": "p2"}],
                })
            with self.assertRaisesRegex(ValueError, "数量"):
                validate_config_payload({
                    "mappings": [{"id": "m1"}, {"id": "m2"}],
                    "presets": [],
                })
        finally:
            (
                schema.MAX_PROFILE_COUNT,
                schema.MAX_MAPPINGS_PER_SCOPE,
                schema.MAX_TOTAL_MAPPINGS,
            ) = old


class StaticWorkflowSafetyTests(unittest.TestCase):
    def test_mapping_delete_isolated_before_widget_removal(self):
        text = (ROOT / "ui" / "mapping_editor.py").read_text("utf-8")
        method = text[text.index("    def delete_mapping"):text.index(
            "    @staticmethod\n    def update_mapping_mode_fields"
        )]
        self.assertLess(
            method.index("_suspend_mapping_runtime_for_delete"),
            method.index("self.mapping_cards.remove"),
        )
        suspend_start = text.index("    def _suspend_mapping_runtime_for_delete")
        suspend_end = text.index("    def delete_mapping", suspend_start)
        suspend = text[suspend_start:suspend_end]
        self.assertIn('mapping_task_id = f"mapping:{mapping_id}"', suspend)
        self.assertIn("self.set_running(\n                False", suspend)
        self.assertIn("allow_owned_mouse_force_release=True", suspend)
        self.assertNotIn("change_layer", suspend)
        runtime = (ROOT / "ui" / "input_runtime.py").read_text("utf-8")
        self.assertIn("suspended_mapping_ids", runtime)

    def test_apply_is_transactional_and_close_checks_dirty_state(self):
        text = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        method = text[text.index("    def _apply_changes_impl"):text.index(
            "    def set_running", text.index("    def _apply_changes_impl")
        )]
        self.assertLess(
            method.index("_save_config_payload(candidate"),
            method.index("_snapshot_runtime_config()"),
        )
        self.assertIn("_restore_apply_transaction", method)
        close = (ROOT / "ui" / "shutdown_coordinator.py").read_text("utf-8")
        self.assertIn("has_unapplied_changes", close)
        self.assertIn("应用并退出", close)
        self.assertIn("取消退出", close)

    def test_apply_runs_readable_health_check_before_raw_validation(self):
        text = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        method = text[text.index("    def _apply_changes_impl"):text.index(
            "    def set_running", text.index("    def _apply_changes_impl")
        )]
        self.assertIn("current_preset_health_issues", method)
        self.assertIn("open_preset_health_check", method)
        self.assertLess(
            method.index("current_preset_health_issues"),
            method.index("validate_config_payload("),
        )

    def test_auto_apply_defers_and_profile_order_is_editable(self):
        editor = (ROOT / "ui" / "editor_workflow.py").read_text("utf-8")
        self.assertIn("当前宏结束后再自动应用", editor)
        self.assertIn("_auto_apply_in_progress", editor)
        manager = (ROOT / "ui" / "profile_manager.py").read_text("utf-8")
        self.assertIn("def move_profile", manager)
        self.assertIn("MAX_PROFILE_COUNT", manager)

    def test_scheduler_propagates_release_failure_and_reports_stop_timeout(self):
        scheduler = (ROOT / "macro" / "scheduler.py").read_text("utf-8")
        self.assertIn("released = self.release(action)", scheduler)
        self.assertIn("return bool(ok and released)", scheduler)
        self.assertIn("return remaining_ids", scheduler)
        controls = (ROOT / "ui" / "macro_controls.py").read_text("utf-8")
        self.assertIn("MacroState.STOP_TIMEOUT", controls)
        self.assertIn("_poll_stopping_macros", controls)


class SchedulerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "PySide6.QtCore" not in sys.modules:
            qtcore = types.ModuleType("PySide6.QtCore")

            class QObject:
                pass

            class Signal:
                def __init__(self, *_args, **_kwargs):
                    pass

                def emit(self, *_args, **_kwargs):
                    pass

            qtcore.QObject = QObject
            qtcore.Signal = Signal
            pyside = types.ModuleType("PySide6")
            pyside.QtCore = qtcore
            sys.modules["PySide6"] = pyside
            sys.modules["PySide6.QtCore"] = qtcore
        cls.scheduler = importlib.import_module("macro.scheduler")

    def test_release_failure_retries_and_fails_the_action(self):
        phases = []

        def send(_action, phase, **_kwargs):
            phases.append(phase)
            return phase != "Release"

        class Engine:
            @staticmethod
            def is_running():
                return True

        class Signal:
            @staticmethod
            def emit(*_args, **_kwargs):
                pass

        class Signals:
            progress = Signal()
            action_activity = Signal()
            task_finished = Signal()
            state_changed = Signal()

        task = self.scheduler.MacroTask(
            {"id": "test", "name": "test", "actions": []},
            Engine(), Signals(), send_output=send, is_active=lambda: True,
        )
        task.started_at = time.perf_counter()
        self.assertFalse(task.run_action({
            "type": "鼠标点击", "target": "鼠标左键", "hold_ms": 1,
        }, 100))
        self.assertEqual(phases.count("Release"), 3)
        self.assertTrue(task.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
