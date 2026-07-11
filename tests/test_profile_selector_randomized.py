import os
import random
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox

from ui.profile_workflow import ProfileWorkflowMixin


class _SelectorHarness(ProfileWorkflowMixin):
    """Small real-Qt harness for the queued profile selector transaction."""

    def __init__(self):
        self.profile_selector_combo = QComboBox()
        self.profile_selector_combo.addItem("基础配置", "")
        self.profile_selector_combo.addItem("洛克王国", "rock")
        self.profile_selector_combo.addItem("新配置档案 2", "new-2")
        self._profile_selector_change_generation = 0
        self.visible_profile_id = ""
        self.selection_log = []
        self.reject_next_selection = False
        self.profile_selector_combo.currentIndexChanged.connect(
            self.on_main_profile_index_changed
        )
        self.profile_selector_combo.view().clicked.connect(
            self.on_main_profile_view_clicked
        )

    def _visible_editor_profile_id(self):
        return self.visible_profile_id

    def _refresh_editor_profile_labels(self):
        pass

    def refresh_profile_selector_state(self):
        pass

    def on_main_profile_selected(self, index, target_id=None):
        target_id = str(target_id or "")
        if self.reject_next_selection:
            self.reject_next_selection = False
            self._sync_profile_selector_to_visible()
            return
        self.visible_profile_id = target_id
        self.selection_log.append((index, target_id))
        self._sync_profile_selector_to_visible()


class ProfileSelectorRandomizedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.harness = _SelectorHarness()
        self.combo = self.harness.profile_selector_combo
        self.combo.resize(220, 34)
        self.combo.show()
        self.app.processEvents()

    def tearDown(self):
        self.combo.close()
        self.combo.deleteLater()
        self.app.processEvents()

    def _click_popup_row(self, index):
        self.combo.showPopup()
        self.app.processEvents()
        view = self.combo.view()
        model_index = self.combo.model().index(index, self.combo.modelColumn())
        rect = view.visualRect(model_index)
        point = rect.center() if rect.isValid() else QPoint(20, 10 + index * 24)
        QTest.mouseClick(
            view.viewport(), Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, point,
        )

    def _settle(self):
        for _ in range(4):
            self.app.processEvents()
            QTest.qWait(1)

    def test_random_mouse_switches_keep_name_and_loaded_profile_aligned(self):
        rng = random.Random(0xC0DEC0DE)
        iterations = int(os.environ.get("PROFILE_SELECTOR_RANDOM_ITERATIONS", "300"))
        for _ in range(iterations):
            target_index = rng.randrange(self.combo.count())
            self._click_popup_row(target_index)
            self._settle()
            expected = str(self.combo.itemData(target_index) or "")
            self.assertEqual(expected, self.harness.visible_profile_id)
            self.assertEqual(expected, str(self.combo.currentData() or ""))
            self.assertFalse(self.combo.view().isVisible())

    def test_rejected_mouse_switch_is_repainted_back_to_visible_profile(self):
        self._click_popup_row(1)
        self._settle()
        self.assertEqual("rock", self.harness.visible_profile_id)

        self.harness.reject_next_selection = True
        self._click_popup_row(2)
        self._settle()
        self.assertEqual("rock", self.harness.visible_profile_id)
        self.assertEqual("rock", str(self.combo.currentData() or ""))

    def test_native_popup_index_rollback_does_not_discard_committed_request(self):
        # Reproduce the Windows popup ordering that caused the real failure:
        # the signal commits a target, but currentData transiently returns to
        # the old row before the queued callback runs.
        self.combo.setCurrentIndex(2)
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(0)
        self.combo.blockSignals(False)
        self._settle()
        self.assertEqual("new-2", self.harness.visible_profile_id)
        self.assertEqual("new-2", str(self.combo.currentData() or ""))

    def test_activated_repairs_an_existing_same_index_name_mismatch(self):
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(1)
        self.combo.blockSignals(False)
        self.assertEqual("", self.harness.visible_profile_id)

        self.harness.on_main_profile_activated(1)
        self._settle()
        self.assertEqual("rock", self.harness.visible_profile_id)
        self.assertEqual("rock", str(self.combo.currentData() or ""))


if __name__ == "__main__":
    unittest.main()
