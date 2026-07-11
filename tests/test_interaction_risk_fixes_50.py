import unittest
from pathlib import Path

from config.profiles import profile_match_overlaps


ROOT = Path(__file__).resolve().parents[1]


class InteractionRiskFixes50StaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile_text = (ROOT / "ui" / "profile_workflow.py").read_text("utf-8")
        cls.trigger_text = (ROOT / "ui" / "trigger_conflicts.py").read_text("utf-8")
        cls.profiles_text = (ROOT / "config" / "profiles.py").read_text("utf-8")
        cls.main_text = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        cls.runtime_text = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")

    def test_macrocanvas_foreground_suspend_failure_fails_closed(self):
        start = self.profile_text.index("        layer_ok = self._change_runtime_profile_layer")
        end = self.profile_text.index("        paused_ids = []", start)
        block = self.profile_text[start:end]
        self.assertIn('if str(reason or "") == "macrocanvas_foreground"', block)
        self.assertIn("self.output_shutdown_in_progress = True", block)
        self.assertIn("self.profile_trigger_allowed = False", block)
        self.assertIn("self.macrocanvas_foreground_suspend_failed = True", block)
        self.assertIn("self.engine_state = EngineState.FAILED", block)
        self.assertIn("fail_closed=True", block)

    def test_foreground_checker_does_not_mark_failed_suspend_as_safe(self):
        start = self.profile_text.index("    def check_foreground_profile")
        method = self.profile_text[start:]
        self.assertIn('getattr(self, "macrocanvas_foreground_suspend_failed", False)', method)
        self.assertIn("suspended = self._suspend_active_profile_input", method)
        self.assertIn("if not suspended:", method)
        self.assertIn("return", method)

    def test_suspend_failure_latch_is_initialized_and_cleared_by_safe_paths(self):
        self.assertIn("self.macrocanvas_foreground_suspend_failed = False", self.main_text)
        self.assertIn("self.macrocanvas_foreground_suspend_failed = False", self.runtime_text)
        self.assertIn("self.macrocanvas_foreground_suspend_failed = False", self.main_text)

    def test_auto_apply_defers_warning_confirmation_to_manual_apply(self):
        self.assertIn('getattr(self, "_auto_apply_in_progress", False)', self.trigger_text)
        self.assertIn("auto_apply_trigger_warning_deferred", self.trigger_text)
        self.assertIn("自动应用已暂停", self.trigger_text)
        self.assertIn("self.auto_apply_timer.stop()", self.trigger_text)
        warning_pos = self.trigger_text.index("auto_apply_trigger_warning_deferred")
        question_pos = self.trigger_text.index("QMessageBox.question", warning_pos)
        self.assertLess(warning_pos, question_pos)

    def test_profile_overlap_warning_is_part_of_apply_analysis(self):
        self.assertIn("def profile_match_overlaps", self.profiles_text)
        self.assertIn("def analyze_profile_match_overlaps", self.trigger_text)
        self.assertIn("reports.extend(self.analyze_profile_match_overlaps())", self.trigger_text)
        self.assertIn("前台匹配条件可能重叠", self.trigger_text)
        self.assertIn("按档案列表顺序优先使用", self.trigger_text)


class ProfileOverlapHelperTests(unittest.TestCase):
    def test_exact_same_process_profiles_can_overlap(self):
        left = {"enabled": True, "process_names": ["game.exe"], "title_contains": []}
        right = {"enabled": True, "process_names": ["C:/Games/game.exe"], "title_contains": []}
        self.assertTrue(profile_match_overlaps(left, right))

    def test_different_exact_process_profiles_do_not_overlap(self):
        left = {"enabled": True, "process_names": ["game.exe"], "title_contains": []}
        right = {"enabled": True, "process_names": ["editor.exe"], "title_contains": []}
        self.assertFalse(profile_match_overlaps(left, right))

    def test_title_only_profile_can_overlap_process_profile(self):
        left = {"enabled": True, "process_names": [], "title_contains": ["boss"]}
        right = {"enabled": True, "process_names": ["game.exe"], "title_contains": []}
        self.assertTrue(profile_match_overlaps(left, right))

    def test_disabled_or_empty_profiles_are_ignored(self):
        left = {"enabled": False, "process_names": ["game.exe"], "title_contains": []}
        right = {"enabled": True, "process_names": ["game.exe"], "title_contains": []}
        self.assertFalse(profile_match_overlaps(left, right))
        left = {"enabled": True, "process_names": [], "title_contains": []}
        self.assertFalse(profile_match_overlaps(left, right))


if __name__ == "__main__":
    unittest.main()
