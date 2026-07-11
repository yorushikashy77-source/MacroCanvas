import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InteractionRiskFixes49StaticTests(unittest.TestCase):
    def test_desktop_kanata_controls_have_python_fallback_without_owning_normal_input(self):
        text = (ROOT / "ui" / "input_runtime.py").read_text("utf-8")
        self.assertIn("def _handle_kanata_owned_control_input", text)
        self.assertIn("kanata_owned_control_input", text)
        self.assertIn("return bool(control_consumed)", text)
        self.assertIn("python_control_already_consumed", text)
        self.assertIn('(\"global_toggle\", getattr(self, \"runtime_global_toggle_key\", \"\"))', text)

    def test_clean_but_stopped_config_is_saved_not_applied(self):
        text = (ROOT / "ui" / "editor_workflow.py").read_text("utf-8")
        self.assertIn('elif getattr(self, "running", False):', text)
        self.assertIn("self.config_state = ConfigState.SAVED", text)
        self.assertIn("配置已保存；启动输入引擎后确认运行", text)
        self.assertIn("配置与当前运行中的已应用版本一致", text)

    def test_profile_capture_rejects_self_or_invalid_foreground_window(self):
        text = (ROOT / "ui" / "profile_manager.py").read_text("utf-8")
        self.assertIn("foreground_window_belongs_to_current_process", text)
        self.assertIn("捕获到 MacroCanvas 自身窗口", text)
        self.assertIn("未读取到有效前台窗口", text)
        self.assertIn("未读取到目标程序进程名", text)

    def test_settings_close_explains_macrocanvas_foreground_isolation(self):
        text = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        self.assertIn('self.profile_input_suspend_reason = "macrocanvas_foreground"', text)
        self.assertIn("设置已关闭；MacroCanvas 仍在前台，映射保持隔离", text)

    def test_same_main_key_warning_mentions_loose_modifier_priority(self):
        text = (ROOT / "ui" / "trigger_conflicts.py").read_text("utf-8")
        self.assertIn("来源映射允许临时额外修饰键", text)
        self.assertIn("修饰键更具体", text)
        self.assertIn("条件优先级更高", text)


if __name__ == "__main__":
    unittest.main()
