import unittest
from pathlib import Path

from engine.kanata_command_runtime import KanataCommandRuntimeMixin


ROOT = Path(__file__).resolve().parents[1]


class _ProbeRuntime(KanataCommandRuntimeMixin):
    def __init__(self, acknowledge=True):
        self.fake_key_names_generation = 3
        self.tcp_error_generation = 0
        self.command_socket = object()
        self.last_command_error = ""
        self._requested = False
        self._acknowledge = acknowledge

    def _request_fake_key_names_now(self):
        self._requested = True
        return True

    def _drain_tcp_messages(self, _wait_seconds=0.0):
        if self._requested and self._acknowledge:
            self.fake_key_names_generation += 1
            self._requested = False

    @staticmethod
    def is_running():
        return True


class HoldReleaseShutdownTests(unittest.TestCase):
    def test_protocol_probe_confirms_server_consumed_prior_release_batch(self):
        runtime = _ProbeRuntime(acknowledge=True)
        self.assertTrue(runtime._confirm_server_processed_commands_now(0.08))

    def test_protocol_probe_fails_without_server_acknowledgement(self):
        runtime = _ProbeRuntime(acknowledge=False)
        self.assertFalse(runtime._confirm_server_processed_commands_now(0.05))
        self.assertIn("未确认", runtime.last_command_error)

    def test_full_stop_disables_source_layer_before_releasing_outputs(self):
        text = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        start = text.index("    def _set_running_impl")
        stop = text.index('            self._set_loading_message(\n                "正在停止输入引擎"', start)
        end = text.index("            self.running = False", stop)
        block = text[stop:end]
        self.assertLess(
            block.index("self._change_runtime_profile_layer(\n                    DISABLED_LAYER_NAME"),
            block.index("remaining = self.stop_all_macros"),
        )

    def test_mapping_pause_closes_trigger_gate_before_cleanup(self):
        text = (ROOT / "ui" / "input_runtime.py").read_text("utf-8")
        start = text.index("    def set_mappings_enabled")
        end = text.index("    def emergency_stop", start)
        block = text[start:end]
        self.assertLess(
            block.index("self.mappings_enabled = False"),
            block.index("self.stop_all_macros"),
        )
        self.assertIn("_failsafe_release_runtime_targets(", block)
        self.assertIn("force_all=False, allow_mouse_targets=False", block)

    def test_virtual_key_release_waits_for_kanata_protocol_confirmation(self):
        runtime = (ROOT / "engine" / "kanata_command_runtime.py").read_text("utf-8")
        self.assertIn('"probe": True', runtime)
        self.assertIn("_confirm_server_processed_commands_now", runtime)
        main = (ROOT / "ui" / "main_window.py").read_text("utf-8")
        start = main.index("    def _release_runtime_virtual_keys")
        end = main.index("    def _failsafe_release_runtime_targets", start)
        block = main[start:end]
        self.assertIn('"Release", wait=True', block)
        self.assertIn("engine.flush_commands", block)


if __name__ == "__main__":
    unittest.main()
