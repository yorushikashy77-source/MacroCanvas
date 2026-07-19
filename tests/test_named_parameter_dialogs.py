import unittest
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QLabel, QLineEdit, QPushButton, QTableWidget,
)

from ui.editors import ActionTreeWidget
from ui.editor_workflow import EditorWorkflowMixin
from ui.styles import STYLESHEET


class _DialogHarness(EditorWorkflowMixin):
    def __init__(self):
        self.selected_preset_card = None
        self.preset_cards = []
        self.loading_task_stack = []
        self.initializing = False
        self.data_change_count = 0

    def select_preset_card(self, card):
        self.selected_preset_card = card
        self.action_table = card.action_table

    def update_card_action_summary(self, _card):
        pass

    def _loading_checkpoint(self, *_args, **_kwargs):
        pass

    def action_changed(self, _card=None):
        self.data_change_count += 1

    def data_changed(self):
        self.data_change_count += 1


def _card(preset_id, parameters=None):
    table = ActionTreeWidget()
    table.setColumnCount(5)
    dialog = QDialog()
    return SimpleNamespace(
        preset_id=preset_id,
        name=QLineEdit(preset_id),
        parameter_definitions=list(parameters or []),
        action_dialog=dialog,
        action_table=table,
        action_title=QLabel(),
        loop_points_button=QPushButton(),
        _actions_loaded=True,
        _pending_actions=[],
    )


def _dialog_button(dialog, standard_button):
    box = dialog.findChild(QDialogButtonBox)
    if box is None:
        raise AssertionError("dialog button box was not created")
    button = box.button(standard_button)
    if button is None:
        raise AssertionError(f"dialog button {standard_button} was not created")
    return button


class NamedParameterDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def run_dialog_action(self, open_dialog, action):
        errors = []

        def operate():
            dialog = QApplication.activeModalWidget()
            try:
                if not isinstance(dialog, QDialog):
                    raise AssertionError("expected an active Qt modal dialog")
                action(dialog)
            except BaseException as error:
                errors.append(error)
                if isinstance(dialog, QDialog):
                    dialog.reject()

        QTimer.singleShot(0, operate)
        open_dialog()
        if errors:
            raise errors[0]

    def test_variable_definition_dialog_accepts_and_cancel_preserves(self):
        harness = _DialogHarness()
        card = _card("child")
        harness.preset_cards.append(card)

        def add_key_variable(dialog):
            add = next(
                button for button in dialog.findChildren(QPushButton)
                if "添加变量" in button.text()
            )
            add.click()
            table = dialog.findChild(QTableWidget)
            self.assertEqual(table.rowCount(), 1)
            table.item(0, 0).setText("目标键")
            table.cellWidget(0, 1).setCurrentText("按键")
            table.item(0, 2).setText("A")
            _dialog_button(
                dialog, QDialogButtonBox.StandardButton.Ok
            ).click()

        self.run_dialog_action(
            lambda: harness.edit_preset_variables(card), add_key_variable
        )
        self.assertEqual(card.parameter_definitions, [{
            "name": "目标键", "type": "按键", "default": "A",
        }])

        def change_then_cancel(dialog):
            table = dialog.findChild(QTableWidget)
            table.item(0, 2).setText("B")
            _dialog_button(
                dialog, QDialogButtonBox.StandardButton.Cancel
            ).click()

        self.run_dialog_action(
            lambda: harness.edit_preset_variables(card), change_then_cancel
        )
        self.assertEqual(card.parameter_definitions[0]["default"], "A")

    def test_variable_definition_row_fits_styled_editors(self):
        harness = _DialogHarness()
        card = _card("child")
        card.action_dialog.setStyleSheet(STYLESHEET)
        harness.preset_cards.append(card)

        def inspect_editor_sizes(dialog):
            add = next(
                button for button in dialog.findChildren(QPushButton)
                if "添加变量" in button.text()
            )
            add.click()
            table = dialog.findChild(QTableWidget)
            combo = table.cellWidget(0, 1)
            QApplication.processEvents()

            self.assertGreaterEqual(combo.height(), combo.sizeHint().height())
            self.assertGreaterEqual(
                table.rowHeight(0), table.fontMetrics().height() + 24
            )

            table.editItem(table.item(0, 2))
            QApplication.processEvents()
            editor = table.findChild(QLineEdit)
            self.assertIsNotNone(editor)
            self.assertGreaterEqual(editor.height(), editor.sizeHint().height())
            dialog.reject()

        self.run_dialog_action(
            lambda: harness.edit_preset_variables(card), inspect_editor_sizes
        )

    def test_action_binding_dialog_marks_and_rebuilds_the_row(self):
        harness = _DialogHarness()
        card = _card("child", [{
            "name": "延迟", "type": "时长", "default": 25,
        }])
        harness.preset_cards.append(card)
        item = harness.add_action({
            "action_id": "wait", "type": "等待", "wait_ms": 10,
            "children": [],
        }, save=False, card=card)
        card.action_table.setCurrentItem(item)
        item.setSelected(True)

        def bind_wait(dialog):
            combos = dialog.findChildren(QComboBox)
            self.assertEqual(len(combos), 1)
            index = combos[0].findData("延迟")
            self.assertGreaterEqual(index, 0)
            combos[0].setCurrentIndex(index)
            _dialog_button(
                dialog, QDialogButtonBox.StandardButton.Ok
            ).click()

        self.run_dialog_action(
            lambda: harness.edit_selected_action_variables(card), bind_wait
        )
        rebuilt = harness.action_from_item(card.action_table, item)
        self.assertEqual(
            rebuilt["parameter_bindings"], {"wait_ms": "延迟"}
        )
        self.assertIn("◇", item.text(0))

        actions = harness.collect_visible_actions(card)
        harness.load_actions(actions, card)
        restored = card.action_table.topLevelItem(0)
        self.assertEqual(
            harness.action_from_item(card.action_table, restored)
            ["parameter_bindings"],
            {"wait_ms": "延迟"},
        )
        self.assertIn("◇", restored.text(0))

    def test_submacro_override_dialog_filters_and_cleans_values(self):
        harness = _DialogHarness()
        child = _card("child", [{
            "name": "目标键", "type": "按键", "default": "A",
        }])
        root = _card("root")
        harness.preset_cards.extend([root, child])
        harness.add_action({
            "action_id": "key", "type": "键盘点击", "target": "A",
            "hold_ms": 1, "parameter_bindings": {"target": "目标键"},
            "children": [],
        }, save=False, card=child)
        call = harness.add_action({
            "action_id": "call", "type": "调用子宏",
            "preset_id": "child", "repeat_count": 1,
            "speed_percent": 100, "children": [],
        }, save=False, card=root)
        root.action_table.setCurrentItem(call)
        call.setSelected(True)

        def override_key(dialog):
            checkbox = next(
                control for control in dialog.findChildren(QCheckBox)
                if control.text() == "覆盖默认值"
            )
            checkbox.setChecked(True)
            combo = dialog.findChild(QComboBox)
            self.assertIsNotNone(combo)
            self.assertGreaterEqual(combo.findText("B"), 0)
            combo.setCurrentText("B")
            _dialog_button(
                dialog, QDialogButtonBox.StandardButton.Ok
            ).click()

        self.run_dialog_action(
            lambda: harness.edit_selected_action_variables(root), override_key
        )
        rebuilt = harness.action_from_item(root.action_table, call)
        self.assertEqual(rebuilt["parameter_values"], {"目标键": "B"})
        self.assertIn("◇", call.text(0))

        child.parameter_definitions = []
        harness._sanitize_named_parameter_metadata()
        rebuilt = harness.action_from_item(root.action_table, call)
        self.assertNotIn("parameter_values", rebuilt)
        self.assertNotIn("◇", call.text(0))


if __name__ == "__main__":
    unittest.main()
