import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HotkeyEditorIntegrationStaticTests(unittest.TestCase):
    def test_mapping_condition_is_owned_by_source_hotkey_editor(self):
        mapping_text = (ROOT / "ui" / "mapping_editor.py").read_text(
            encoding="utf-8"
        )
        workflow_text = (ROOT / "ui" / "editor_workflow.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("allow_condition=True", mapping_text)
        self.assertIn("condition_options=SOURCE_NAMES", mapping_text)
        self.assertNotIn('QCheckBox("附加触发条件")', mapping_text)
        self.assertNotIn("card.condition_row", mapping_text)
        self.assertIn("card.source_hotkey.condition_value()", workflow_text)

    def test_manual_dialog_contains_condition_controls(self):
        editors_text = (ROOT / "ui" / "editors.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('dialog.setObjectName("hotkeyDialog")', editors_text)
        self.assertIn('QCheckBox("启用附加触发条件")', editors_text)
        self.assertIn('condition_form.addRow("条件输入", condition_key)', editors_text)
        self.assertIn('condition_form.addRow("条件状态", condition_state)', editors_text)
        self.assertIn("condition_fields.setVisible", editors_text)

    def test_segmented_hotkey_style_is_shared_by_presets_and_actions(self):
        editors_text = (ROOT / "ui" / "editors.py").read_text(
            encoding="utf-8"
        )
        preset_text = (ROOT / "ui" / "preset_editor.py").read_text(
            encoding="utf-8"
        )
        styles_text = (ROOT / "ui" / "styles.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('setObjectName("hotkeyCaptureButton")', editors_text)
        self.assertIn('setObjectName("hotkeyManualButton")', editors_text)
        self.assertIn("self.key_editor = HotkeyEdit(", editors_text)
        self.assertIn("card.trigger_hotkey = HotkeyEdit(", preset_text)
        self.assertIn("QPushButton#hotkeyCaptureButton", styles_text)
        self.assertIn("QPushButton#hotkeyManualButton", styles_text)
        self.assertIn('[conditionActive="true"]', styles_text)


if __name__ == "__main__":
    unittest.main()
