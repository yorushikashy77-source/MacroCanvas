import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QLabel, QLineEdit, QTableWidget,
)

from core.constants import MacroState
from macro.simulation import simulate_preset
from ui.diagnostic_bundle import REDACTED, redact_log_text, write_diagnostic_bundle
from ui.operation_state import operation_blocks, operation_state_snapshot
from ui.catalog_tools import CatalogToolsMixin
from ui.runtime_diagnostics import RuntimeDiagnosticsMixin
from ui.simulation_preview import SimulationPreviewDialog


class OperationStateTests(unittest.TestCase):
    def owner(self, **fields):
        base = {
            "_shutdown_started": False,
            "loading_task_stack": [],
            "_config_apply_transaction_active": False,
            "_runtime_operation_active": False,
            "recording_session_active": False,
            "recording": False,
            "_test_countdown_preset_id": None,
            "profile_switch_confirmation_active": False,
            "settings_dialog_active": False,
            "macro_state": MacroState.IDLE,
        }
        base.update(fields)
        return SimpleNamespace(**base)

    def test_priority_and_command_matrix(self):
        owner = self.owner(
            recording_session_active=True,
            recording=True,
            loading_task_stack=["load"],
        )
        self.assertEqual(operation_state_snapshot(owner).key, "loading")
        blocked, snapshot = operation_blocks(owner, "bulk_edit")
        self.assertTrue(blocked)
        self.assertEqual(snapshot.key, "loading")

    def test_running_macro_does_not_block_bulk_editor_change(self):
        owner = self.owner(macro_state=MacroState.RUNNING)
        self.assertFalse(operation_blocks(owner, "bulk_edit")[0])


class SimulationTests(unittest.TestCase):
    def test_fixed_preset_duration_and_no_side_effects(self):
        preset = {
            "name": "demo",
            "execution_mode": "固定次数",
            "loop_count": 2,
            "loop_interval_ms": 100,
            "speed_percent": 100,
            "actions": [
                {"action_id": "a", "type": "等待", "wait_ms": 200, "jitter_ms": 50},
                {"action_id": "b", "type": "键盘点击", "target": "A", "hold_ms": 100},
            ],
        }
        original = json.loads(json.dumps(preset))
        report = simulate_preset(preset)
        self.assertEqual(report["one_cycle_min_ms"], 250)
        self.assertEqual(report["one_cycle_max_ms"], 350)
        self.assertEqual(report["total_min_ms"], 600)
        self.assertEqual(report["total_max_ms"], 800)
        self.assertEqual(len(report["events"]), 2)
        self.assertEqual(preset, original)

    def test_referenced_loop_is_bounded_and_reported_once(self):
        preset = {
            "execution_mode": "执行一次",
            "speed_percent": 100,
            "actions": [
                {"action_id": "a", "type": "等待", "wait_ms": 10},
                {"action_id": "b", "type": "等待", "wait_ms": 20},
                {
                    "type": "循环动作", "name": "循环项目1",
                    "execution_mode": "执行次数", "loop_count": 3,
                    "target_action_ids": ["a", "b"],
                    "timeline_mode": "sequential",
                },
            ],
        }
        report = simulate_preset(preset)
        self.assertEqual(report["total_min_ms"], 92)
        self.assertEqual(report["total_max_ms"], 92)
        self.assertEqual(len(report["events"]), 3)
        self.assertEqual(report["events"][0]["kind"], "loop")

    def test_infinite_mode_has_no_fake_upper_bound(self):
        report = simulate_preset({
            "execution_mode": "无限循环",
            "actions": [{"action_id": "a", "type": "等待", "wait_ms": 10}],
        })
        self.assertIsNone(report["total_max_ms"])
        self.assertTrue(report["warnings"])


class _HotkeyStub:
    def __init__(self, modifiers, key):
        self._value = modifiers, key

    def value(self):
        return self._value

    def condition_value(self):
        return False, "", ""


class _CatalogHarness(CatalogToolsMixin):
    def __init__(self):
        self.mapping_cards = []
        self.mapping_search = QLineEdit()
        self.mapping_enabled_filter = QComboBox()
        self.mapping_enabled_filter.addItems(["全部状态", "已启用", "已停用"])
        self.mapping_filter_result = QLabel()
        self.macro_state = MacroState.IDLE
        self.loading_task_stack = []
        self.changes = 0

    def data_changed(self):
        self.changes += 1


class CatalogToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def card(name, source, enabled):
        card = QFrame()
        card.enabled = QCheckBox()
        card.enabled.setChecked(enabled)
        card.name = QLineEdit(name)
        card.source_hotkey = _HotkeyStub("无", source)
        card.target_hotkey = _HotkeyStub("无", "A")
        card.mode = QComboBox()
        card.mode.addItem("执行一次")
        card.setVisible(True)
        return card

    def test_search_and_bulk_change_only_touch_visible_results(self):
        owner = _CatalogHarness()
        alpha = self.card("Alpha", "F1", True)
        beta = self.card("Beta", "F2", True)
        owner.mapping_cards = [alpha, beta]
        owner.mapping_search.setText("Alpha")
        self.assertEqual(owner.refresh_mapping_filters(), 1)
        self.assertFalse(alpha.isHidden())
        self.assertTrue(beta.isHidden())
        self.assertEqual(owner.disable_filtered_mappings(), 1)
        self.assertFalse(alpha.enabled.isChecked())
        self.assertTrue(beta.enabled.isChecked())
        self.assertEqual(owner.changes, 1)

    def test_simulation_dialog_renders_every_report_event(self):
        report = simulate_preset({
            "name": "preview",
            "execution_mode": "执行一次",
            "actions": [{"action_id": "a", "type": "等待", "wait_ms": 10}],
        })
        dialog = SimulationPreviewDialog(report)
        table = dialog.findChild(QTableWidget)
        self.assertIsNotNone(table)
        self.assertEqual(table.rowCount(), len(report["events"]))
        dialog.close()


class _FailedRunLocationHarness(RuntimeDiagnosticsMixin):
    def __init__(self):
        self.card = SimpleNamespace(preset_id="child")
        self.preset_cards = [self.card]

    def collect_visible_actions(self, card):
        self.assertIs(card, self.card)
        return [
            {"action_id": "first", "type": "键盘点击"},
            {
                "action_id": "branch", "type": "条件分支",
                "children": [{"action_id": "wait-space", "type": "等待条件"}],
            },
        ]

    def assertIs(self, left, right):
        if left is not right:
            raise AssertionError("Unexpected preset card")


class DiagnosticBundleTests(unittest.TestCase):
    def test_json_fields_and_paths_are_redacted(self):
        text = json.dumps({
            "event": "trigger_task",
            "process": "secret.exe",
            "preset_name": "private",
            "source_key": "F1",
            "detail": r"C:\\Users\\Alice\\private.txt",
        }, ensure_ascii=False)
        cleaned = redact_log_text(text, home=Path(r"C:\Users\Alice"))
        payload = json.loads(cleaned)
        self.assertEqual(payload["process"], REDACTED)
        self.assertEqual(payload["preset_name"], REDACTED)
        self.assertEqual(payload["source_key"], REDACTED)
        self.assertNotIn("Alice", payload["detail"])

    def test_unstructured_log_text_is_omitted(self):
        cleaned = redact_log_text("error while loading private-alias F1\n")
        self.assertNotIn("private-alias", cleaned)
        self.assertIn("已省略 1 行", cleaned)

    def test_bundle_contains_summaries_not_raw_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / "diagnostic.log"
            log.write_text('{"event":"x","window_title":"private"}\n', "utf-8")
            destination = root / "bundle.zip"
            included = write_diagnostic_bundle(
                destination,
                {"application": "MacroCanvas"},
                {"preset_count": 1},
                [("diagnostic.log", log)],
                home=root,
            )
            self.assertEqual(included, ["diagnostic.log"])
            with zipfile.ZipFile(destination) as archive:
                names = set(archive.namelist())
                self.assertIn("summary.json", names)
                self.assertIn("configuration-summary.json", names)
                self.assertIn("logs/diagnostic.log", names)
                self.assertNotIn("config.json", names)
                cleaned = archive.read("logs/diagnostic.log").decode("utf-8")
                self.assertNotIn("private", cleaned)

    def test_bundle_writes_redacted_failure_context_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            destination = root / "failure-bundle.zip"
            write_diagnostic_bundle(
                destination,
                {"application": "MacroCanvas"},
                {"preset_count": 1},
                [],
                home=root,
                extra_payloads=(
                    ("failed-run.json", {
                        "preset_name": "private preset",
                        "source": "F1",
                        "failure_action_type": "等待条件",
                    }),
                    ("health-check.json", {"issue_count": 2}),
                ),
            )
            with zipfile.ZipFile(destination) as archive:
                self.assertIn("context/failed-run.json", archive.namelist())
                self.assertIn("context/health-check.json", archive.namelist())
                context = json.loads(
                    archive.read("context/failed-run.json").decode("utf-8")
                )
                self.assertEqual(context["preset_name"], REDACTED)
                self.assertEqual(context["source"], REDACTED)
                self.assertEqual(context["failure_action_type"], "等待条件")

    def test_failed_run_locator_keeps_structural_position_without_names(self):
        harness = _FailedRunLocationHarness()
        locator = harness._failed_run_action_locator({
            "action_preset_id": "child",
            "action_id": "wait-space",
            "call_chain_ids": ["root", "child"],
        })

        self.assertTrue(locator["available"])
        self.assertEqual(locator["action_id"], "wait-space")
        self.assertEqual(locator["owner_token"], "child")
        self.assertEqual(locator["call_chain_tokens"], ["root", "child"])
        self.assertEqual(locator["position"], [2, 1])
        self.assertEqual(locator["position_label"], "动作 2 / 动作 1")


if __name__ == "__main__":
    unittest.main()
