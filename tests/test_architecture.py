import unittest

from engine.interception import InterceptionInputHook, InterceptionOutput
from engine.kanata import KanataConfigBuilder, KanataEngine
from engine.kanata_command_runtime import KanataCommandRuntimeMixin
from engine.win_input import WinInput
from macro.scheduler import MacroController, MacroTask
from ui.editors import ActionTreeWidget, HotkeyEdit
from ui.config_persistence import ConfigPersistenceMixin
from ui.configuration_transfer import ConfigurationTransferMixin
from ui.action_execution import ActionExecutionMixin
from ui.engine_configuration import EngineConfigurationMixin
from ui.hotkey_settings import HotkeySettingsMixin
from ui.loading_coordinator import LoadingCoordinatorMixin
from ui.runtime_lifecycle import RuntimeLifecycleMixin
from ui.shutdown_coordinator import ShutdownCoordinatorMixin
from ui.macro_controls import MacroControlsMixin
from ui.input_listener_lifecycle import InputListenerLifecycleMixin
from ui.mapping_editor import MappingEditorMixin
from ui.preset_editor import PresetEditorMixin
from ui.main_window import MainWindow
from ui.runtime_diagnostics import RuntimeDiagnosticsMixin
from ui.trigger_conflicts import TriggerConflictMixin
from ui.widget_behaviors import WheelEditBlocker


class ArchitectureTests(unittest.TestCase):
    def test_components_live_in_dedicated_modules(self):
        expected = {
            KanataEngine: "engine.kanata",
            KanataCommandRuntimeMixin: "engine.kanata_command_runtime",
            WinInput: "engine.win_input",
            InterceptionOutput: "engine.interception",
            InterceptionInputHook: "engine.interception",
            MacroTask: "macro.scheduler",
            MacroController: "macro.scheduler",
            HotkeyEdit: "ui.editors",
            ActionTreeWidget: "ui.editors",
            ConfigPersistenceMixin: "ui.config_persistence",
            ConfigurationTransferMixin: "ui.configuration_transfer",
            ActionExecutionMixin: "ui.action_execution",
            EngineConfigurationMixin: "ui.engine_configuration",
            HotkeySettingsMixin: "ui.hotkey_settings",
            LoadingCoordinatorMixin: "ui.loading_coordinator",
            RuntimeLifecycleMixin: "ui.runtime_lifecycle",
            ShutdownCoordinatorMixin: "ui.shutdown_coordinator",
            MacroControlsMixin: "ui.macro_controls",
            InputListenerLifecycleMixin: "ui.input_listener_lifecycle",
            MappingEditorMixin: "ui.mapping_editor",
            PresetEditorMixin: "ui.preset_editor",
            RuntimeDiagnosticsMixin: "ui.runtime_diagnostics",
            TriggerConflictMixin: "ui.trigger_conflicts",
            WheelEditBlocker: "ui.widget_behaviors",
            MainWindow: "ui.main_window",
        }
        for component, module in expected.items():
            self.assertEqual(component.__module__, module)

    def test_main_window_delegates_cross_cutting_responsibilities(self):
        self.assertTrue(issubclass(MainWindow, ConfigPersistenceMixin))
        self.assertTrue(issubclass(MainWindow, ConfigurationTransferMixin))
        self.assertTrue(issubclass(MainWindow, ActionExecutionMixin))
        self.assertTrue(issubclass(MainWindow, EngineConfigurationMixin))
        self.assertTrue(issubclass(MainWindow, RuntimeDiagnosticsMixin))
        self.assertTrue(issubclass(MainWindow, TriggerConflictMixin))
        self.assertTrue(issubclass(MainWindow, HotkeySettingsMixin))
        self.assertTrue(issubclass(MainWindow, LoadingCoordinatorMixin))
        self.assertTrue(issubclass(MainWindow, RuntimeLifecycleMixin))
        self.assertTrue(issubclass(MainWindow, ShutdownCoordinatorMixin))
        self.assertTrue(issubclass(MainWindow, MacroControlsMixin))
        self.assertTrue(issubclass(MainWindow, InputListenerLifecycleMixin))
        self.assertTrue(issubclass(MainWindow, MappingEditorMixin))
        self.assertTrue(issubclass(MainWindow, PresetEditorMixin))
        self.assertNotIn("save_config", MainWindow.__dict__)
        self.assertNotIn("write_diagnostic", MainWindow.__dict__)
        self.assertNotIn("open_global_hotkey_settings", MainWindow.__dict__)
        self.assertNotIn("generate_kanata_config", MainWindow.__dict__)
        self.assertNotIn("analyze_trigger_conflicts", MainWindow.__dict__)
        self.assertNotIn("import_configuration_file", MainWindow.__dict__)
        self.assertNotIn("_begin_loading", MainWindow.__dict__)
        self.assertNotIn("stop_all_macros", MainWindow.__dict__)
        self.assertNotIn("set_running", MainWindow.__dict__)
        self.assertNotIn("apply_changes", MainWindow.__dict__)
        self.assertNotIn("shutdown", MainWindow.__dict__)
        self.assertNotIn("closeEvent", MainWindow.__dict__)
        self.assertNotIn("run_from_current_action", MainWindow.__dict__)
        self.assertNotIn("add_mapping", MainWindow.__dict__)
        self.assertNotIn("add_preset", MainWindow.__dict__)

    def test_kanata_engine_delegates_command_runtime(self):
        self.assertTrue(issubclass(KanataEngine, KanataCommandRuntimeMixin))
        self.assertNotIn("queue_virtual_key_action", KanataEngine.__dict__)
        self.assertNotIn("_command_worker", KanataEngine.__dict__)

    def test_normal_mode_builder_accepts_current_control_options(self):
        text = KanataConfigBuilder(
            [], [],
            emergency_modifiers="Ctrl+Shift",
            emergency_key="F9",
            emit_diagnostics=True,
        ).build()
        self.assertIn("f9", text.lower())
        self.assertIn("mc-control emergency", text)


if __name__ == "__main__":
    unittest.main()
