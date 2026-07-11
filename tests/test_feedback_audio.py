import io
import unittest
import wave
from pathlib import Path

from ui.input_runtime import InputRuntimeMixin


ROOT = Path(__file__).resolve().parents[1]


class FeedbackAudioTests(unittest.TestCase):
    def test_feedback_cues_are_valid_nonempty_pcm_waves(self):
        for kind in InputRuntimeMixin._feedback_tones():
            payload = InputRuntimeMixin._feedback_wave_bytes(kind)
            with wave.open(io.BytesIO(payload), "rb") as source:
                self.assertEqual(1, source.getnchannels())
                self.assertEqual(2, source.getsampwidth())
                self.assertEqual(44100, source.getframerate())
                self.assertGreater(source.getnframes(), 4000)

    def test_feedback_uses_qt_audio_instead_of_windows_beep_threads(self):
        text = (ROOT / "ui" / "input_runtime.py").read_text("utf-8")
        self.assertIn("QSoundEffect", text)
        self.assertIn("feedback_signal.emit", text)
        self.assertNotIn("winsound.Beep", text)

    def test_engine_toggle_success_cue_follows_confirmed_transition(self):
        text = (ROOT / "ui" / "runtime_lifecycle.py").read_text("utf-8")
        start = text.index("    def toggle_running")
        end = text.index("    @Slot(bool)", start)
        method = text[start:end]
        self.assertLess(method.index("set_running"), method.index("_play_feedback"))
        self.assertIn("result is not False and self.running == target_enabled", method)

    def test_emergency_tone_precedes_cleanup(self):
        text = (ROOT / "ui" / "input_runtime.py").read_text("utf-8")
        start = text.index("    def emergency_stop")
        end = text.index("    @Slot(str, str, str, str)", start)
        method = text[start:end]
        self.assertLess(method.index("_play_feedback"), method.index("stop_all_macros"))


if __name__ == "__main__":
    unittest.main()
