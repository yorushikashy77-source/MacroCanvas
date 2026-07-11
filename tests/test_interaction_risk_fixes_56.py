import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InteractionRiskFixes56StaticTests(unittest.TestCase):
    def test_quarantined_mouse_release_blocks_new_output_before_gate_flag(self):
        source = (ROOT / "ui" / "runtime_guards.py").read_text("utf-8")
        self.assertIn("def pending_quarantined_mouse_release_names", source)
        self.assertIn("pending_release_summary()", source)
        self.assertIn("quarantined_mouse_releases", source)
        self.assertIn("鼠标按键等待安全释放", source)
        self.assertLess(
            source.index("pending_quarantined_mouse_release_names(owner)"),
            source.index('getattr(owner, "output_shutdown_in_progress", False)'),
        )

    def test_macro_stop_waiting_state_is_not_immediate_timeout(self):
        source = (ROOT / "ui" / "macro_controls.py").read_text("utf-8")
        self.assertIn("MACRO_STOP_TIMEOUT_SECONDS = 2.0", source)
        self.assertIn("def _set_macro_stop_waiting_display", source)
        self.assertIn("MacroState.STOP_TIMEOUT if timed_out else MacroState.STOPPING", source)
        self.assertNotIn('self.macro_state = MacroState.STOP_TIMEOUT\n            self.macro_status_detail = f"仍有 {len(remaining)} 个任务正在退出"', source)

    def test_loop_progress_exposes_inner_reference_position(self):
        source = (ROOT / "macro" / "scheduler.py").read_text("utf-8")
        ast.parse(source)
        self.assertIn("loop_inner_step", source)
        self.assertIn("loop_inner_total", source)
        self.assertIn("引用动作", source)
        self.assertIn("progress_callback=progress_callback", source)

    def test_enable_mapping_message_mentions_safe_mouse_release(self):
        source = (ROOT / "ui" / "input_runtime.py").read_text("utf-8")
        self.assertIn("有鼠标按键等待回到原窗口后释放", source)
        self.assertIn("强制释放键鼠", source)


if __name__ == "__main__":
    unittest.main()
