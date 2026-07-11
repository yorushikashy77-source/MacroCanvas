import threading
import unittest
from unittest.mock import patch

from ui.input_listener_lifecycle import InputListenerLifecycleMixin


class _TextStub:
    def __init__(self):
        self.text = ""
        self.style = ""

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)


class _OutputStub:
    calls = None

    def __init__(self):
        self.context = object()
        self.keyboard_device = 1
        self.mouse_device = 11
        self.started = 0
        self.stopped = 0

    def start(self):
        if self.calls is not None:
            self.calls.append("output.start")
        self.started += 1
        return True

    def stop(self):
        if self.calls is not None:
            self.calls.append("output.stop")
        self.stopped += 1
        return True


class _LiveHook:
    def __init__(self):
        self.context = object()
        self.thread = type(
            "ThreadStub", (), {"is_alive": lambda _self: True}
        )()
        self.stop_event = threading.Event()
        self.capture_mouse_move = False
        self.update_calls = []
        self.stop_calls = []

    def update_capture_mouse_move(self, enabled):
        self.update_calls.append(bool(enabled))
        self.capture_mouse_move = bool(enabled)
        return True

    def stop(self, timeout=1.0):
        self.stop_calls.append(timeout)
        return True


class _ColdHook(_LiveHook):
    calls = None

    def __init__(self, *_args, **kwargs):
        super().__init__()
        self.context = None
        self.thread = None
        self.capture_mouse_move = bool(kwargs.get("capture_mouse_move"))

    def start(self):
        if self.calls is not None:
            self.calls.append("hook.start")
        self.context = object()
        self.thread = type(
            "ThreadStub", (), {"is_alive": lambda _self: True}
        )()
        return True


_DEFAULT_HOOK = object()


class _Harness(InputListenerLifecycleMixin):
    def __init__(self, hook=_DEFAULT_HOOK):
        self.interception_input_hook = _LiveHook() if hook is _DEFAULT_HOOK else hook
        self.interception_input_control_only = True
        self.interception_control_modifiers = {"Ctrl"}
        self.interception_control_sources = {"kbd": "Ctrl"}
        self.interception_record_mouse_move = True
        self.interception_output = None
        self.engine_hint = _TextStub()
        self.diagnostics = []

    def _runtime_is_game_mode(self):
        return True

    def _interception_source_callback(self, *_args):
        return False

    def _handle_interception_raw_input(self, *_args):
        pass

    def write_diagnostic(self, event, **payload):
        self.diagnostics.append((event, payload))


class InterceptionListenerReuseTests(unittest.TestCase):
    def test_full_mode_reuses_live_control_listener_when_filter_changes(self):
        harness = _Harness()
        original_hook = harness.interception_input_hook
        output = _OutputStub()

        with (
            patch("ui.input_listener_lifecycle.os.name", "nt"),
            patch(
                "ui.input_listener_lifecycle.InterceptionOutput",
                return_value=output,
            ),
        ):
            self.assertTrue(
                harness.start_interception_input_hook(control_only=False)
            )

        self.assertIs(harness.interception_input_hook, original_hook)
        self.assertFalse(harness.interception_input_control_only)
        self.assertEqual(harness.interception_input_hook.update_calls, [True])
        self.assertEqual(harness.interception_input_hook.stop_calls, [])
        self.assertEqual(output.started, 1)
        self.assertEqual(output.stopped, 0)
        self.assertEqual(harness.interception_control_modifiers, set())
        self.assertEqual(harness.interception_control_sources, {})
        self.assertTrue(
            any(
                event == "interception_input_listener_mode_changed"
                and payload.get("reused_context") is True
                and payload.get("capture_mouse_move") is True
                for event, payload in harness.diagnostics
            )
        )

    def test_full_mode_cold_start_attaches_input_before_output(self):
        calls = []
        _ColdHook.calls = calls
        _OutputStub.calls = calls
        harness = _Harness(hook=None)
        output = _OutputStub()

        try:
            with (
                patch("ui.input_listener_lifecycle.os.name", "nt"),
                patch(
                    "ui.input_listener_lifecycle.InterceptionInputHook",
                    _ColdHook,
                ),
                patch(
                    "ui.input_listener_lifecycle.InterceptionOutput",
                    return_value=output,
                ),
            ):
                self.assertTrue(
                    harness.start_interception_input_hook(control_only=False)
                )
        finally:
            _ColdHook.calls = None
            _OutputStub.calls = None

        self.assertEqual(calls[:2], ["hook.start", "output.start"])
        self.assertIsInstance(harness.interception_input_hook, _ColdHook)
        self.assertFalse(harness.interception_input_control_only)
        self.assertEqual(output.started, 1)


if __name__ == "__main__":
    unittest.main()
