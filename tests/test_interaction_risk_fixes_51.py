import unittest
from pathlib import Path

from ui.hotkey_settings import HotkeySettingsMixin


ROOT = Path(__file__).resolve().parents[1]


class _HotkeyValidator(HotkeySettingsMixin):
    def __init__(self, conflict=""):
        self.conflict = conflict

    def global_hotkey_conflict(self, _modifiers, _key):
        return self.conflict


class HotkeyValidationTests(unittest.TestCase):
    def setUp(self):
        self.validator = _HotkeyValidator()
        self.valid = {
            "toggle_enabled": True,
            "toggle": ("Ctrl+Shift", "F10"),
            "pause_enabled": True,
            "pause": ("Ctrl", "F9"),
            "emergency": ("无", "F8"),
            "cancel": ("无", "F7"),
            "finish": ("无", "F8"),
        }

    def test_shipped_emergency_and_finish_shortcut_is_valid(self):
        self.assertIsNone(self.validator._hotkey_candidate_error(**self.valid))

    def test_duplicate_active_controls_are_rejected(self):
        candidate = dict(self.valid)
        candidate["pause"] = candidate["toggle"]
        issue = self.validator._hotkey_candidate_error(**candidate)
        self.assertEqual(issue[0], "快捷键冲突")

    def test_modifier_cannot_be_the_main_key(self):
        candidate = dict(self.valid)
        candidate["cancel"] = ("无", "Ctrl")
        issue = self.validator._hotkey_candidate_error(**candidate)
        self.assertEqual(issue[0], "快捷键无效")

    def test_enabled_runtime_mapping_conflict_is_rejected(self):
        validator = _HotkeyValidator("基础配置 · 示例映射")
        issue = validator._hotkey_candidate_error(**self.valid)
        self.assertIn("示例映射", issue[1])


class InteractionFlowStaticTests(unittest.TestCase):
    def test_hotkey_dialog_validates_before_accepting(self):
        text = (ROOT / "ui" / "hotkey_settings.py").read_text("utf-8")
        self.assertIn("def validate_and_accept", text)
        self.assertIn("buttons.accepted.connect(validate_and_accept)", text)
        self.assertNotIn("buttons.accepted.connect(dialog.accept)", text)

    def test_profile_manager_discards_self_foreground_context(self):
        text = (ROOT / "ui" / "profile_manager.py").read_text("utf-8")
        self.assertIn("foreground_is_self = foreground_window_belongs_to_current_process()", text)
        self.assertIn("self.use_process_button.setEnabled(has_process)", text)
        self.assertIn("尚未捕获有效目标窗口", text)

    def test_import_menu_has_one_current_editor_operation(self):
        text = (ROOT / "ui" / "configuration_transfer.py").read_text("utf-8")
        self.assertIn("导入到当前编辑方案", text)
        self.assertNotIn("复制到当前配置", text)
        self.assertNotIn("合并到当前配置", text)

    def test_startup_clean_config_is_saved_until_runtime_starts(self):
        text = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        start = text.index("        self.config_state = (", text.index("generated ="))
        block = text[start:text.index("        if generated:", start)]
        self.assertIn("ConfigState.SAVED if generated", block)
        self.assertNotIn("ConfigState.APPLIED if generated", block)


if __name__ == "__main__":
    unittest.main()
