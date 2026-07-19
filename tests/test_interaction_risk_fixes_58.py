import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InteractionRiskFix58StaticTests(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text("utf-8")

    def test_force_release_foreground_sets_visible_suspension_state(self):
        source = self.read("ui/main_window.py")
        self.assertIn('"force_release_macrocanvas_foreground_isolated"', source)
        marker = source.index('"force_release_macrocanvas_foreground_isolated"')
        block = source[marker - 500:marker + 250]
        self.assertIn('self.profile_input_temporarily_suspended = True', block)
        self.assertIn('self.profile_input_suspend_reason = "macrocanvas_foreground"', block)
        ast.parse(source)

    def test_global_pause_tracks_only_its_owned_tasks(self):
        source = self.read("ui/macro_controls.py")
        start = source.index("    def toggle_all_macro_pause")
        end = source.index("    def pause_or_resume_current", start)
        block = source[start:end]
        self.assertIn("_global_pause_macro_ids", block)
        self.assertIn("target_ids = running_ids if should_pause else owned_ids", block)
        self.assertIn("owned_ids.difference_update(succeeded)", block)
        ast.parse(source)

    def test_empty_macro_uses_unified_finish_cleanup(self):
        source = self.read("macro/scheduler.py")
        start = source.index("    def run(self):")
        end = source.index("class MacroController", start)
        block = source[start:end]
        self.assertIn(
            'if not actions:\n                self.finish_reason = "empty"\n'
            '                return',
            block,
        )
        self.assertIn(
            'if not self.is_active():\n'
            '                self.finish_reason = "backend_inactive"\n'
            '                return',
            block,
        )
        self.assertIn("self._emit_task_finished_once()", block)
        self.assertNotIn('self.signals.task_finished.emit(self.preset["id"])', block)
        ast.parse(source)

    def test_interception_quarantines_mouse_modifiers_until_mouseup(self):
        source = self.read("engine/interception.py")
        self.assertIn("mouse_release_modifier_quarantine", source)
        self.assertIn("_release_quarantined_mouse_modifiers", source)
        self.assertIn("deferred_modifier_names", source)
        self.assertIn("target in self.mouse_release_quarantined", source)
        ast.parse(source)

    def test_multiple_runtime_candidates_are_visible_in_ui(self):
        source = self.read("ui/input_runtime.py")
        self.assertIn("runtime_trigger_multiple_candidates", source)
        self.assertIn("runtime_shadow_warning_last", source)
        self.assertIn("同一快捷键有多条规则同时成立", source)
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
