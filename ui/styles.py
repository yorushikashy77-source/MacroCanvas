STYLESHEET = """
QMainWindow, QWidget {
    background: #0c1119;
    color: #edf2f9;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
}
QMenuBar {
    background: #0f151f;
    color: #cbd4e1;
    border-bottom: 1px solid #202a38;
    padding: 3px 6px;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 11px;
    border-radius: 6px;
}
QMenuBar::item:selected { background: #222c3a; color: white; }
QMenu {
    background: #151d28;
    color: #e9eef6;
    border: 1px solid #303c4e;
    padding: 6px;
}
QMenu::item { padding: 8px 24px; border-radius: 5px; }
QMenu::item:selected { background: #7357f6; color: white; }
QLabel#heading { font-size: 17px; font-weight: 650; color: #f2f5fa; }
QLabel#sectionLabel { font-size: 15px; font-weight: 650; color: #dfe6f1; }
QLabel#muted { color: #8491a3; font-size: 13px; }

QWidget#hotkeyEditor { background: transparent; }
QPushButton#hotkeyCaptureButton {
    background: #1e2734;
    color: #edf2f9;
    border: 1px solid #303c4e;
    border-right: none;
    border-top-left-radius: 7px;
    border-bottom-left-radius: 7px;
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
    padding: 7px 8px;
    font-weight: 600;
}
QPushButton#hotkeyCaptureButton:hover {
    background: #263244;
    border-color: #7357f6;
}
QPushButton#hotkeyCaptureButton:pressed { background: #18212d; }
QPushButton#hotkeyCaptureButton[conditionActive="true"] {
    border-color: #6f5be7;
    background: #24243a;
}
QPushButton#hotkeyManualButton {
    background: #263142;
    color: #aeb9c9;
    border: 1px solid #303c4e;
    border-left: 1px solid #3c4a5f;
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
    border-top-right-radius: 7px;
    border-bottom-right-radius: 7px;
    padding: 7px 7px;
    font-size: 12px;
    font-weight: 650;
}
QPushButton#hotkeyManualButton:hover {
    background: #344157;
    color: #ffffff;
    border-color: #7357f6;
}
QPushButton#hotkeyManualButton:pressed { background: #1c2634; }
QPushButton#hotkeyManualButton[conditionActive="true"] {
    background: #5f49cf;
    color: #ffffff;
    border-color: #846aff;
}
QPushButton#hotkeyManualButton[conditionActive="true"]:hover {
    background: #7357f6;
}
QDialog#hotkeyDialog { background: #0d131c; }
QLabel#hotkeyDialogTitle {
    color: #f5f7fb;
    font-size: 19px;
    font-weight: 700;
}
QLabel#hotkeyDialogHint {
    color: #8d9aad;
    font-size: 12px;
}
QLabel#hotkeyDialogSectionTitle {
    color: #dfe6f1;
    font-size: 14px;
    font-weight: 650;
}
QFrame#hotkeyDialogSection {
    background: #141c27;
    border: 1px solid #2a3648;
    border-radius: 9px;
}
QDialog#hotkeyDialog QComboBox {
    margin: 2px 0;
    min-height: 22px;
}
QDialog#hotkeyDialog QLabel,
QFrame#hotkeyDialogSection QLabel,
QFrame#hotkeyDialogSection QCheckBox {
    background: transparent;
}
QDialog#hotkeyDialog QDialogButtonBox QPushButton {
    min-width: 72px;
}
QPushButton#hotkeyDialogSave {
    background: #7357f6;
    color: #ffffff;
    border: 1px solid #846aff;
}
QPushButton#hotkeyDialogSave:hover { background: #846aff; }
QPushButton#hotkeyDialogSave:pressed { background: #6047df; }
QPushButton#hotkeyDialogCancel {
    background: #222c3a;
    color: #cbd4e1;
    border: 1px solid #303c4e;
}
QPushButton#hotkeyDialogCancel:hover {
    background: #2c3849;
    color: #ffffff;
}
QWidget#hotkeyConditionFields { background: transparent; }

QWidget#presetContainer, QWidget#mappingContainer { background: transparent; }
QFrame#mappingCard {
    background: #151c27;
    border: 1px solid #283446;
    border-radius: 10px;
}
QFrame#parameterArea {
    background: #101720;
    border: 1px solid #222e3d;
    border-radius: 8px;
}
QLabel#mappingArrow {
    color: #718096;
    font-size: 18px;
    font-weight: 700;
    padding: 16px 2px 0 2px;
}
QPushButton#collapseButton {
    background: #1d2633;
    color: #aeb9c9;
    border: 1px solid #303c4e;
    padding: 7px 11px;
}
QPushButton#collapseButton:hover {
    background: #283445;
    border-color: #7357f6;
    color: #ffffff;
}
QPushButton#collapseButton:checked {
    background: #2a3150;
    border-color: #7357f6;
    color: #ffffff;
}
QFrame#presetQuickActions {
    background: #101823;
    border: 1px solid #263448;
    border-radius: 8px;
}
QPushButton#testAction {
    background: #123a34;
    color: #70f0cf;
    border: 1px solid #267d69;
    font-weight: 650;
}
QPushButton#testAction:hover {
    background: #185046;
    border-color: #45b99d;
}
QPushButton#testAction:pressed { background: #0d2e29; }
QPushButton#recordAction {
    background: #3b2618;
    color: #ffc17f;
    border: 1px solid #925d31;
    font-weight: 650;
}
QPushButton#recordAction:hover {
    background: #53351f;
    border-color: #d58a46;
}
QPushButton#recordAction:pressed { background: #2d1c12; }
QPushButton#loopActionButton {
    background: #12334a;
    color: #8bdcff;
    border: 1px solid #2a86b5;
    font-weight: 650;
}
QPushButton#loopActionButton:hover { background: #194761; border-color: #55c6f5; }
QPushButton#loopActionSelecting {
    background: #3b2554;
    color: #e6c7ff;
    border: 1px solid #9b67cf;
    font-weight: 650;
}
QPushButton#loopActionSelecting:hover { background: #4c3069; border-color: #c797f2; }
QPushButton#loopActionReady {
    background: #0f5360;
    color: #c6fbff;
    border: 1px solid #31bdd0;
    font-weight: 700;
}
QPushButton#loopActionReady:hover { background: #166a78; border-color: #67e8f9; }
QPushButton#collapseButton:disabled {
    background: #171e28;
    color: #667386;
    border-color: #242e3b;
}
QFrame#presetCard {
    background: #151c27;
    border: 1px solid #283446;
    border-radius: 12px;
}
QFrame#presetCard[selected="true"] {
    border: 1px solid #7357f6;
    background: #181f2c;
}
QDialog#actionDialog {
    background: #0b1119;
}
QFrame#actionArea {
    background: #0e141d;
    border: 1px solid #202b3a;
    border-radius: 9px;
}
QTableWidget#actionTable, QTreeWidget#actionTable {
    background: #0c121a;
    alternate-background-color: #121a25;
    border-color: #1e2a39;
}
QLabel#fieldLabel {
    color: #7f8da1;
    font-size: 11px;
    padding-left: 7px;
}
QWidget#fieldGroup { background: transparent; }
QScrollArea { background: transparent; border: none; }
QFrame#card {
    background: #151b25;
    border: 1px solid #202a38;
    border-radius: 14px;
}
QFrame#subpanel {
    background: #111720;
    border: 1px solid #222c3a;
    border-radius: 10px;
}
QSplitter::handle {
    background: transparent;
    width: 10px;
}
QLabel#statusOff, QLabel#statusOn, QLabel#statusPaused {
    background: #151b25;
    border: 1px solid #293445;
    border-radius: 17px;
    padding: 7px 12px;
    font-weight: 600;
}
QLabel#statusOff { color: #94a0b1; }
QLabel#statusOn { color: #35dec9; border-color: #226e69; }
QLabel#statusPaused {
    color: #71efa0;
    background: #14231b;
    border-color: #2d8d57;
}
QPushButton {
    min-height: 20px;
    padding: 8px 15px;
    border: none;
    border-radius: 8px;
    font-weight: 600;
}
QPushButton#primary { background: #7357f6; color: white; }
QPushButton#primary:hover { background: #846aff; }
QPushButton#secondary { background: #222c3a; color: #cbd4e1; }
QPushButton#secondary:hover { background: #2c3849; }
QPushButton#stop { background: #bd455c; color: white; }
QPushButton#mappingPaused {
    background: #1f7a4d;
    color: #e4ffed;
    border: 1px solid #48dc86;
}
QPushButton#mappingPaused:hover { background: #278d5a; }
QPushButton#dangerGhost { background: #34212a; color: #ff8496; padding: 7px 13px; }
QPushButton#dangerGhost:hover { background: #492933; }
QTabWidget::pane {
    background: #151b25;
    border: 1px solid #202a38;
    border-radius: 12px;
    top: -1px;
}
QTabBar::tab {
    background: #101620;
    color: #8f9caf;
    padding: 10px 22px;
    margin-right: 4px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}
QTabBar::tab:selected { background: #151b25; color: #ffffff; }
QTableWidget, QTreeWidget {
    background: #111720;
    alternate-background-color: #171e29;
    border: 1px solid #222c3a;
    border-radius: 8px;
    color: #e8edf5;
}
QTableWidget::item:selected, QTreeWidget::item:selected { background: #293052; color: white; }
QTreeWidget#actionTable::item:selected {
    background: transparent;
    color: #e8edf5;
}
QTreeWidget#actionTable::item:hover { background: transparent; }
QHeaderView::section {
    background: #101620;
    color: #8f9caf;
    border: none;
    border-bottom: 1px solid #283243;
    padding: 9px;
    font-weight: 600;
}
QComboBox, QLineEdit, QSpinBox {
    background: #1e2734;
    color: #edf2f9;
    border: 1px solid #303c4e;
    border-radius: 7px;
    padding: 7px 9px;
    margin: 4px 6px;
}
QComboBox:hover, QLineEdit:focus, QSpinBox:focus { border-color: #7357f6; }
QComboBox:disabled { color: #788598; background: #171e28; }
QComboBox QAbstractItemView {
    background: #1c2430;
    color: #edf2f9;
    selection-background-color: #7357f6;
    border: 1px solid #354154;
}
QCheckBox::indicator {
    width: 18px; height: 18px;
    border: 1px solid #536078; border-radius: 5px;
    background: #111720;
}
QCheckBox::indicator:checked { background: #7357f6; border-color: #8c76ff; }
"""
