"""Search, filter, and safe bulk enable/disable helpers for editor cards."""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QMessageBox

from ui.operation_state import operation_blocks


class CatalogToolsMixin:
    @staticmethod
    def _enabled_filter_matches(card, filter_text):
        if filter_text == "已启用":
            return card.enabled.isChecked()
        if filter_text == "已停用":
            return not card.enabled.isChecked()
        return True

    @staticmethod
    def _hotkey_search_text(editor):
        try:
            modifiers, key = editor.value()
            return f"{modifiers} {key}"
        except (AttributeError, TypeError, ValueError):
            return ""

    def _mapping_search_text(self, card):
        condition = ""
        try:
            enabled, key, state = card.source_hotkey.condition_value()
            if enabled:
                condition = f"{key} {state}"
        except (AttributeError, TypeError, ValueError):
            pass
        return " ".join((
            card.name.text(),
            self._hotkey_search_text(card.source_hotkey),
            self._hotkey_search_text(card.target_hotkey),
            card.mode.currentText(),
            condition,
        )).casefold()

    def _preset_search_text(self, card):
        return " ".join((
            card.name.text(),
            self._hotkey_search_text(card.trigger_hotkey),
            card.execution_mode.currentText(),
        )).casefold()

    def refresh_mapping_filters(self):
        query_widget = getattr(self, "mapping_search", None)
        filter_widget = getattr(self, "mapping_enabled_filter", None)
        if query_widget is None or filter_widget is None:
            return 0
        query = query_widget.text().strip().casefold()
        filter_text = filter_widget.currentText()
        visible = 0
        cards = list(getattr(self, "mapping_cards", []) or [])
        for card in cards:
            matched = (
                (not query or query in self._mapping_search_text(card))
                and self._enabled_filter_matches(card, filter_text)
            )
            card.setVisible(matched)
            visible += int(matched)
        if hasattr(self, "mapping_filter_result"):
            self.mapping_filter_result.setText(f"显示 {visible} / {len(cards)}")
        return visible

    def refresh_preset_filters(self):
        query_widget = getattr(self, "preset_search", None)
        filter_widget = getattr(self, "preset_enabled_filter", None)
        if query_widget is None or filter_widget is None:
            return 0
        query = query_widget.text().strip().casefold()
        filter_text = filter_widget.currentText()
        visible = 0
        cards = list(getattr(self, "preset_cards", []) or [])
        for card in cards:
            matched = (
                (not query or query in self._preset_search_text(card))
                and self._enabled_filter_matches(card, filter_text)
            )
            card.setVisible(matched)
            visible += int(matched)
        if hasattr(self, "preset_filter_result"):
            self.preset_filter_result.setText(f"显示 {visible} / {len(cards)}")
        return visible

    def _bulk_set_enabled(self, kind, enabled):
        blocked, snapshot = operation_blocks(self, "bulk_edit")
        if blocked:
            QMessageBox.information(
                self, "当前无法批量修改",
                f"{snapshot.label}，请等待该操作结束后再批量修改。",
            )
            return 0
        cards = list(getattr(self, f"{kind}_cards", []) or [])
        visible = [card for card in cards if not card.isHidden()]
        changed = [card for card in visible if card.enabled.isChecked() != enabled]
        if not changed:
            QMessageBox.information(
                self, "没有需要修改的项目",
                "当前筛选结果为空，或项目已经处于目标状态。",
            )
            return 0
        blockers = [QSignalBlocker(card.enabled) for card in changed]
        try:
            for card in changed:
                card.enabled.setChecked(enabled)
        finally:
            blockers.clear()
        self.data_changed()
        if kind == "mapping":
            self.refresh_mapping_filters()
        else:
            self.refresh_preset_filters()
        return len(changed)

    def enable_filtered_mappings(self):
        return self._bulk_set_enabled("mapping", True)

    def disable_filtered_mappings(self):
        return self._bulk_set_enabled("mapping", False)

    def enable_filtered_presets(self):
        return self._bulk_set_enabled("preset", True)

    def disable_filtered_presets(self):
        return self._bulk_set_enabled("preset", False)
