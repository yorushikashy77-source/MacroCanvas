import unittest
from pathlib import Path


class ProfileProtectionRemovalStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.main_text = (root / "ui" / "main_window.py").read_text(encoding="utf-8")
        cls.profile_text = (root / "ui" / "profile_workflow.py").read_text(encoding="utf-8")
        cls.input_text = (root / "ui" / "input_runtime.py").read_text(encoding="utf-8")
        cls.kanata_text = (root / "engine" / "kanata.py").read_text(encoding="utf-8")
        cls.profile_model_text = (root / "config" / "profiles.py").read_text(encoding="utf-8")
        cls.all_text = "\n".join((
            cls.main_text,
            cls.profile_text,
            cls.input_text,
            cls.kanata_text,
            cls.profile_model_text,
        ))

    def test_transition_guard_state_is_removed(self):
        removed_names = (
            "profile_switch_waiting",
            "profile_pending_id",
            "profile_pending_since",
            "profile_switch_stable_seconds",
            "profile_focus_isolation_active",
            "profile_focus_layer_suspended",
            "profile_transition_blocked",
            "profile_blocked_sources",
            "profile_input_state_signal",
        )
        for name in removed_names:
            self.assertNotIn(name, self.all_text)

    def test_foreground_profile_switch_uses_short_stability_window(self):
        start = self.profile_text.index("    def check_foreground_profile")
        method = self.profile_text[start:]
        self.assertIn('reason="foreground_direct"', method)
        self.assertIn("select_profile(self.runtime_profiles", method)
        self.assertIn("time.monotonic", method)
        self.assertIn("foreground_profile_stable_seconds", method)
        self.assertIn('reason="foreground_candidate_detected"', method)
        self.assertNotIn("physical_down", method)

    def test_no_switching_kanata_layer_is_generated(self):
        self.assertNotIn("SWITCHING_LAYER_NAME", self.profile_model_text)
        self.assertNotIn("(deflayer switching", self.kanata_text)

    def test_input_runtime_has_no_transition_source_block(self):
        self.assertNotIn("source_held_during_profile_switch", self.input_text)
        self.assertNotIn("source_held_during_focus_or_profile_switch", self.input_text)
        self.assertNotIn("_queue_profile_input_state_refresh", self.input_text)

    def test_macrocanvas_foreground_is_safe_zone(self):
        self.assertIn(
            "foreground_window_belongs_to_current_process",
            self.profile_text,
        )
        self.assertIn(
            'reason="macrocanvas_foreground"',
            self.profile_text,
        )
        self.assertIn(
            "macrocanvas_foreground_suspended",
            self.profile_text,
        )
        self.assertIn(
            "foreground_window_belongs_to_current_process",
            self.input_text,
        )
        self.assertIn(
            'reason="macrocanvas_foreground"',
            self.input_text,
        )

    def test_profile_transition_releases_sync_outputs_before_clearing_state(self):
        start = self.profile_text.index("    def _clear_profile_transition_state")
        end = self.profile_text.index("    def _change_runtime_profile_layer", start)
        method = self.profile_text[start:end]
        self.assertIn("self._release_all_sync_mappings()", method)
        self.assertNotIn("self.sync_output_counts.clear()", method)
        self.assertNotIn("self.active_sync_by_source.clear()", method)


if __name__ == "__main__":
    unittest.main()
