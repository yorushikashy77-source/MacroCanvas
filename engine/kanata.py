import ctypes
import json
import os
import queue
import re
import select
import socket
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

from core.constants import *
from config.profiles import profile_layer_name, profile_namespace
from engine.process_guard import WindowsKillOnCloseJob
from engine.kanata_command_runtime import KanataCommandRuntimeMixin
from engine.trigger_resolver import MODIFIER_ORDER, modifier_names
from macro.actions import iter_action_tree

_MOUSE_HWID_CACHE = None
_KEYBOARD_HWID_CACHE = None
_MOUSE_HWID_CACHE_DIR = None
_KEYBOARD_HWID_CACHE_DIR = None


def is_running_as_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


KANATA_KEYS = {
    "鼠标左键": "mlft", "鼠标右键": "mrgt", "鼠标中键": "mmid",
    "鼠标侧键 1": "mbck", "鼠标侧键 2": "mfwd",
    "Ctrl": "lctl", "Shift": "lsft", "Alt": "lalt",
    "Caps Lock": "caps", "Tab": "tab", "Enter": "ret", "Space": "spc",
    "Esc": "esc", "Backspace": "bspc", "Delete": "del",
    "Insert": "ins", "Home": "home", "End": "end",
    "Page Up": "pgup", "Page Down": "pgdn",
    "Print Screen": "prtsc", "Pause": "pause", "Menu": "menu",
    "静音": "mute", "音量减": "vold", "音量加": "volu",
    "上一曲": "prev", "下一曲": "next", "播放/暂停": "pp",
    "方向上": "up", "方向下": "down", "方向左": "left", "方向右": "rght",
    "`": "grv", "-": "min", "=": "eql", "[": "lbrc", "]": "rbrc",
    "\\": "bksl", ";": "scln", "'": "apos", ",": "comm",
    ".": ".", "/": "/",
}
KANATA_KEYS.update({letter: letter.lower() for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
KANATA_KEYS.update({number: number for number in "0123456789"})
KANATA_KEYS.update({f"F{i}": f"f{i}" for i in range(1, 25)})

KANATA_TAP_MOUSE = {
    "鼠标左键": "mltp", "鼠标右键": "mrtp", "鼠标中键": "mmtp",
    "鼠标侧键 1": "mbtp", "鼠标侧键 2": "mftp",
}


def kanata_key(name):
    if name not in KANATA_KEYS:
        raise ValueError(f"Kanata 不支持按键：{name}")
    return KANATA_KEYS[name]


def kanata_output(modifiers, target, tap=False):
    mods = modifier_names(modifiers)
    if target in MOUSE_NAMES:
        mouse = KANATA_TAP_MOUSE[target] if tap else kanata_key(target)
        if not mods:
            return mouse
        keys = " ".join(kanata_key(mod) for mod in mods)
        return f"(multi {keys} {mouse} reverse-release-order)"
    key = kanata_key(target)
    if not mods:
        return key
    prefix = "".join({"Ctrl": "C-", "Shift": "S-", "Alt": "A-"}[mod] for mod in mods)
    return prefix + key


def _kanata_modifier_pressed_condition(modifier):
    left, right = {
        "Ctrl": ("lctl", "rctl"),
        "Shift": ("lsft", "rsft"),
        "Alt": ("lalt", "ralt"),
    }[modifier]
    return f"(or (input real {left}) (input real {right}))"


def kanata_modifier_condition(modifiers):
    """Return a held-state condition for the requested modifiers only."""
    checks = [
        _kanata_modifier_pressed_condition(modifier)
        for modifier in modifier_names(modifiers)
    ]
    if not checks:
        return None
    return checks[0] if len(checks) == 1 else f"(and {' '.join(checks)})"


def _kanata_impossible_modifier_condition(modifier):
    check = _kanata_modifier_pressed_condition(modifier)
    return f"(and {check} (not {check}))"


def kanata_exact_modifier_condition(modifiers, source=None):
    """Match one exact modifier set.

    Global controls keep using exact matching so temporary extra modifiers do
    not accidentally trigger engine toggle / pause / emergency shortcuts.  The
    source key itself is removed from the modifier snapshot in Python; mirror
    that behavior here for Ctrl/Shift/Alt source keys.
    """
    required = set(modifier_names(modifiers))
    source_modifier = source if source in MODIFIER_ORDER else None
    if source_modifier in required:
        return _kanata_impossible_modifier_condition(source_modifier)

    checks = []
    for modifier in MODIFIER_ORDER:
        if modifier == source_modifier:
            continue
        check = _kanata_modifier_pressed_condition(modifier)
        checks.append(check if modifier in required else f"(not {check})")
    if not checks:
        return None
    return checks[0] if len(checks) == 1 else f"(and {' '.join(checks)})"


def kanata_source_modifier_condition(modifiers, source=None):
    """Match a source trigger while allowing extra held modifiers.

    Basic mappings and preset triggers should still fire when the user is
    temporarily holding Ctrl / Shift / Alt.  More specific rules are ordered
    before less specific rules, so Ctrl+Caps Lock can override Caps Lock while
    Caps Lock remains the fallback under other modifier states.
    """
    required = set(modifier_names(modifiers))
    source_modifier = source if source in MODIFIER_ORDER else None
    if source_modifier in required:
        return _kanata_impossible_modifier_condition(source_modifier)

    checks = []
    for modifier in MODIFIER_ORDER:
        if modifier == source_modifier:
            continue
        if modifier in required:
            checks.append(_kanata_modifier_pressed_condition(modifier))
    if not checks:
        return None
    return checks[0] if len(checks) == 1 else f"(and {' '.join(checks)})"


def kanata_mapping_condition(mapping):
    if not bool(mapping.get("condition_enabled", False)):
        return None
    condition_input = mapping.get("condition_input", "鼠标左键")
    if condition_input in ("Ctrl", "Shift", "Alt"):
        check = kanata_modifier_condition(condition_input)
    else:
        check = f"(input real {kanata_key(condition_input)})"
    if mapping.get("condition_state", "按住时") == "松开时":
        return f"(not {check})"
    return check


def _interception_hardware_ids(device_start, device_end, blocked_marker):
    """Read exact raw hardware-id byte arrays from Interception devices."""
    dll_path = kanata_dir() / "interception.dll"
    if os.name != "nt" or not dll_path.exists():
        return []
    try:
        interception = ctypes.WinDLL(str(dll_path))
        interception.interception_create_context.restype = ctypes.c_void_p
        interception.interception_destroy_context.argtypes = [ctypes.c_void_p]
        interception.interception_get_hardware_id.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
        ]
        interception.interception_get_hardware_id.restype = ctypes.c_uint
        context = interception.interception_create_context()
    except (AttributeError, OSError):
        return []

    result = []
    seen = set()
    try:
        for device in range(device_start, device_end + 1):
            # interception_get_hardware_id returns the number of BYTES written.
            # Use a raw byte buffer so the returned MULTI_SZ is preserved exactly.
            buffer = ctypes.create_string_buffer(4096)
            length = int(interception.interception_get_hardware_id(
                context, device, buffer, ctypes.sizeof(buffer)
            ))
            if length <= 0:
                continue
            raw = bytes(buffer.raw[:length])
            description = raw.decode(
                "utf-16-le", errors="ignore"
            ).replace("\0", " ").upper()
            if blocked_marker in description or "VID_" not in description:
                continue
            if raw not in seen:
                seen.add(raw)
                result.append(list(raw))
    finally:
        if context:
            interception.interception_destroy_context(context)
    return result


def _windows_pnp_mouse_hwids():
    """Fallback: build the same UTF-16 MULTI_SZ IDs from present PnP mice."""
    if os.name != "nt":
        return []
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rows = @()
Get-PnpDevice -PresentOnly | Where-Object { $_.Class -eq 'Mouse' } | ForEach-Object {
    $property = Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_HardwareIds'
    if ($property -and $property.Data) {
        $rows += [PSCustomObject]@{
            InstanceId = $_.InstanceId
            HardwareIds = @($property.Data)
        }
    }
}
$rows | ConvertTo-Json -Compress -Depth 5
"""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]

    result = []
    seen = set()
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        instance_id = str(item.get("InstanceId", "")).upper()
        if any(marker in instance_id for marker in (
            "ROOT\\", "RDP_", "VMBUS", "VIRTUAL", "LDVMOUSE"
        )):
            continue
        hardware_ids = item.get("HardwareIds", [])
        if isinstance(hardware_ids, str):
            hardware_ids = [hardware_ids]
        hardware_ids = [
            str(value) for value in hardware_ids
            if value and "VID_" in str(value).upper()
        ]
        if not hardware_ids:
            continue
        # DevicePropertyHardwareID is a REG_MULTI_SZ. Interception returns the
        # exact UTF-16 bytes of that value, including the final double NUL.
        raw = ("\0".join(hardware_ids) + "\0\0").encode("utf-16-le")
        if raw not in seen:
            seen.add(raw)
            result.append(list(raw))
    return result


def interception_mouse_hwids():
    """Return present physical HID mouse paths as Kanata UTF-16 byte arrays."""
    global _MOUSE_HWID_CACHE, _MOUSE_HWID_CACHE_DIR
    current_dir = str(kanata_dir().resolve())
    if _MOUSE_HWID_CACHE is not None and _MOUSE_HWID_CACHE_DIR == current_dir:
        return [list(item) for item in _MOUSE_HWID_CACHE]
    _MOUSE_HWID_CACHE_DIR = current_dir
    if os.name != "nt":
        return []
    dll_path = kanata_dir() / "interception.dll"
    if not dll_path.exists():
        _MOUSE_HWID_CACHE = []
        return []
    try:
        interception = ctypes.WinDLL(str(dll_path))
        interception.interception_create_context.restype = ctypes.c_void_p
        interception.interception_destroy_context.argtypes = [ctypes.c_void_p]
        interception.interception_get_hardware_id.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
        ]
        interception.interception_get_hardware_id.restype = ctypes.c_uint
        context = interception.interception_create_context()
    except (AttributeError, OSError):
        _MOUSE_HWID_CACHE = []
        return []

    result = []
    seen = set()
    try:
        # Interception reserves device IDs 11..20 for mice.
        for device in range(11, 21):
            buffer = ctypes.create_unicode_buffer(500)
            length = int(interception.interception_get_hardware_id(
                context, device, buffer, 500
            ))
            if not length:
                continue
            raw_bytes = ctypes.string_at(ctypes.addressof(buffer), length)
            description = raw_bytes.decode(
                "utf-16-le", errors="ignore"
            ).replace("\0", " ")
            upper_description = description.upper()
            if (
                "LDVMOUSE" in upper_description
                or "VID_" not in upper_description
            ):
                continue
            raw = list(raw_bytes)
            key = bytes(raw)
            if key not in seen:
                seen.add(key)
                result.append(raw)
    finally:
        if context:
            interception.interception_destroy_context(context)
    _MOUSE_HWID_CACHE = [list(item) for item in result]
    return result

def interception_keyboard_hwids():
    """Return physical keyboard hardware IDs for Kanata."""
    global _KEYBOARD_HWID_CACHE, _KEYBOARD_HWID_CACHE_DIR
    current_dir = str(kanata_dir().resolve())
    # A wireless receiver can be assigned a different Interception slot after
    # unplug/replug.  A directory-only cache then keeps the old device list and
    # makes the receiver appear unmatched until the whole process is restarted.
    # Refresh on every config build; this operation is only used while writing
    # Kanata configuration and must reflect the current physical topology.
    _KEYBOARD_HWID_CACHE_DIR = current_dir
    _KEYBOARD_HWID_CACHE = _interception_hardware_ids(1, 10, "LDVKEYBOARD")
    return [list(item) for item in _KEYBOARD_HWID_CACHE]


class KanataConfigBuilder:
    """Generate one Kanata configuration containing base and profile layers.

    The base configuration and every enabled profile are compiled once. Runtime
    profile switching therefore only sends ChangeLayer over the existing TCP
    connection; the Kanata process and its interception devices stay alive.
    """

    GENERATED_CONFIG_REVISION = "conditional-mapping-v2"
    MAX_VIRTUAL_KEYS = 767

    def __init__(
        self, mappings, presets, global_toggle_enabled=True,
        global_toggle_modifiers="Ctrl+Shift", global_toggle_key="F10",
        macro_pause_enabled=True, macro_pause_modifiers="Ctrl",
        macro_pause_key="F9",
        mouse_hwids=None, keyboard_hwids=None, keyboard_hwids_exclude=None,
        emergency_modifiers="无", emergency_key="F8",
        emit_diagnostics=False, profiles=None, **_compatibility_options,
    ):
        self.mappings = list(mappings)
        self.presets = list(presets)
        self.profiles = [
            dict(item) for item in (profiles or [])
            if isinstance(item, dict) and item.get("enabled", False)
        ]
        self.global_toggle_enabled = bool(global_toggle_enabled)
        self.global_toggle_modifiers = global_toggle_modifiers
        self.global_toggle_key = global_toggle_key
        self.macro_pause_enabled = bool(macro_pause_enabled)
        self.macro_pause_modifiers = macro_pause_modifiers
        self.macro_pause_key = macro_pause_key
        self.emergency_modifiers = emergency_modifiers
        self.emergency_key = emergency_key
        self.emit_diagnostics = bool(emit_diagnostics)
        self.mouse_hwids = list(mouse_hwids or [])
        self.keyboard_hwids = list(keyboard_hwids or [])
        self.keyboard_hwids_exclude = list(keyboard_hwids_exclude or [])
        self.virtual_keys = []
        self.virtual_key_names = set()
        self.aliases = []
        self.loop_virtual_keys = []
        self.releasable_virtual_keys = set()
        self.profile_layers = {}
        self.condition_state_keys = {}

    @staticmethod
    def _safe_id(value):
        cleaned = "".join(
            character.lower() for character in str(value)
            if character.isalnum()
        )
        return (cleaned or "item")[:24]

    @staticmethod
    def _scoped(name, namespace=""):
        namespace = "".join(
            character.lower() for character in str(namespace or "")
            if character.isalnum()
        )
        return f"{namespace}{name}" if namespace else name

    @classmethod
    def mapping_key(cls, mapping_id, namespace=""):
        return cls._scoped(f"m{cls._safe_id(mapping_id)}", namespace)

    @classmethod
    def mapping_loop_key(cls, mapping_id, namespace=""):
        return cls._scoped(f"l{cls._safe_id(mapping_id)}", namespace)

    @classmethod
    def generated_config_is_current(cls, text):
        return f"Generator revision: {cls.GENERATED_CONFIG_REVISION}" in str(
            text or ""
        )

    @classmethod
    def preset_action_key(cls, preset_id, action_index, namespace=""):
        return cls._scoped(
            f"p{cls._safe_id(preset_id)}a{int(action_index)}", namespace
        )

    @classmethod
    def trigger_key(cls, kind, item_id, phase, namespace=""):
        prefix = "mt" if kind == "mapping" else "pt"
        suffix = "d" if phase == "down" else "u"
        return cls._scoped(
            f"{prefix}{cls._safe_id(item_id)}{suffix}", namespace
        )

    @staticmethod
    def _normalized_mouse_position(target):
        text = str(target or "0,0")
        if text.startswith("pct:"):
            try:
                x, y = (float(part.strip()) for part in text[4:].split(",", 1))
            except (TypeError, ValueError):
                x = y = 0
            return (
                round(max(0.0, min(100.0, x)) * 65535 / 100),
                round(max(0.0, min(100.0, y)) * 65535 / 100),
            )
        if text.startswith(("rel:", "window:", "client:")):
            raise ValueError(
                "相对/窗口鼠标坐标仅支持游戏模式（Interception）"
            )
        try:
            x, y = (int(part.strip()) for part in text.split(",", 1))
        except (TypeError, ValueError):
            x, y = 0, 0

        left = top = 0
        width, height = 1920, 1080
        try:
            user32 = ctypes.windll.user32
            virtual_width = int(user32.GetSystemMetrics(78))
            virtual_height = int(user32.GetSystemMetrics(79))
            if virtual_width > 0 and virtual_height > 0:
                left = int(user32.GetSystemMetrics(76))
                top = int(user32.GetSystemMetrics(77))
                width, height = virtual_width, virtual_height
            else:
                width = max(1, int(user32.GetSystemMetrics(0)))
                height = max(1, int(user32.GetSystemMetrics(1)))
        except (AttributeError, OSError):
            pass

        normalized_x = round((x - left) * 65535 / max(1, width - 1))
        normalized_y = round((y - top) * 65535 / max(1, height - 1))
        return (
            max(0, min(65535, normalized_x)),
            max(0, min(65535, normalized_y)),
        )

    @classmethod
    def action_output(cls, action):
        kind = action.get("type")
        if kind in ("等待", LOOP_ACTION_TYPE):
            return None
        if kind == "鼠标滚轮":
            direction = "up" if action.get("target") == "向上" else "down"
            return f"(mwheel-{direction} 65535 120)"
        if kind == "鼠标移动":
            x, y = cls._normalized_mouse_position(action.get("target", "0,0"))
            return f"(setmouse {x} {y})"
        return kanata_output(
            action.get("modifiers", "无"),
            action.get("target", "A"),
            tap=False,
        )

    def _add_virtual_key(self, name, action, releasable=False):
        if action is None:
            return
        if name in self.virtual_key_names:
            raise ValueError(f"Kanata 虚拟键名称重复：{name}")
        self.virtual_key_names.add(name)
        self.virtual_keys.append((name, action))
        if releasable:
            self.releasable_virtual_keys.add(name)

    def _register_condition_state_source(self, source):
        """Create one Kanata message pair for a physical condition input."""
        if source in self.condition_state_keys:
            return
        token = kanata_key(source)
        index = len(self.condition_state_keys)
        down_key = f"cs{index}d"
        up_key = f"cs{index}u"
        self._add_virtual_key(
            down_key, f"(push-msg mc-state {token} down)"
        )
        self._add_virtual_key(
            up_key, f"(push-msg mc-state {token} up)"
        )
        self.condition_state_keys[source] = (down_key, up_key)

    def _with_condition_state_notifications(self, source, action):
        state_keys = self.condition_state_keys.get(source)
        if not state_keys:
            return action
        down_key, up_key = state_keys
        return (
            f"(multi (on-press tap-vkey {down_key}) "
            f"(on-release tap-vkey {up_key}) {action})"
        )

    def _trigger_action(self, kind, item_id, namespace, scope):
        safe_id = self._safe_id(item_id)
        message_id = "".join(
            character.lower() for character in str(item_id)
            if character.isalnum()
        ) or safe_id
        down_key = self.trigger_key(kind, safe_id, "down", namespace)
        up_key = self.trigger_key(kind, safe_id, "up", namespace)
        trigger_prefix = (
            f"mc-trigger {scope} {kind} {message_id}"
            if self.profiles
            else f"mc-trigger {kind} {message_id}"
        )
        self._add_virtual_key(
            down_key, f"(push-msg {trigger_prefix} down)"
        )
        self._add_virtual_key(
            up_key, f"(push-msg {trigger_prefix} up)"
        )
        return (
            f"(multi (on-press tap-vkey {down_key}) "
            f"(on-release tap-vkey {up_key}))"
        )

    def _mapping_action(self, mapping, namespace):
        output_key = self.mapping_key(mapping.get("id"), namespace)
        hold_ms = max(20, int(mapping.get("hold_ms", 100)))
        interval = max(0, int(mapping.get("loop_interval_ms", 0)))
        speed = max(10, int(mapping.get("speed_percent", 100)))
        scale = 100 / speed
        hold_ms = max(1, int(hold_ms * scale))
        interval = max(0, int(interval * scale))
        pulse = f"(hold-for-duration {hold_ms} {output_key})"
        mode = mapping.get("mode", "同步按住")
        if mode == "同步按住":
            # Keep direct physical mappings behind a fake key.  This gives the
            # GUI a deterministic Release target when the foreground profile is
            # changed between the physical Down and Up packets.
            return (
                f"(multi (on-press press-vkey {output_key}) "
                f"(on-release release-vkey {output_key}))"
            )
        if mode == "执行一次":
            return f"(macro {pulse})"
        if mode == "固定次数":
            count = max(1, int(mapping.get("loop_count", 1)))
            parts = []
            for index in range(count):
                parts.append(pulse)
                if interval and index + 1 < count:
                    parts.append(str(interval))
            return f"(macro {' '.join(parts)})"

        loop_key = self.mapping_loop_key(mapping.get("id"), namespace)
        cycle = f"{pulse} {max(1, interval)}"
        self._add_virtual_key(
            loop_key, f"(macro-repeat {cycle})", releasable=True
        )
        self.loop_virtual_keys.append(loop_key)
        if mode == "按住循环":
            return (
                f"(multi (on-press press-vkey {loop_key}) "
                f"(on-release release-vkey {loop_key}))"
            )
        if mode == "无限循环":
            return f"(on-press press-vkey {loop_key})"
        return f"(on-press toggle-vkey {loop_key})"

    @staticmethod
    def _control_action(command, layer_action, release_actions):
        parts = []
        if layer_action:
            parts.append(layer_action)
        parts.extend(release_actions)
        parts.append(f"(push-msg mc-control {command} request down)")
        return f"(multi {' '.join(parts)})"

    def _layer_definitions(self):
        definitions = [{
            "profile_id": "",
            "layer": "base",
            "namespace": "",
            "scope": "base",
            "mappings": list(self.mappings),
            "presets": list(self.presets),
        }]
        for profile in self.profiles:
            profile_id = str(profile.get("id") or "")
            if not profile_id:
                continue
            payload = profile.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            layer = profile_layer_name(profile_id)
            namespace = profile_namespace(profile_id)
            if layer in self.profile_layers.values():
                raise ValueError("配置档案生成了重复的 Kanata 图层名称")
            self.profile_layers[profile_id] = layer
            definitions.append({
                "profile_id": profile_id,
                "layer": layer,
                "namespace": namespace,
                "scope": layer,
                "mappings": list(payload.get("mappings", [])),
                "presets": list(payload.get("presets", [])),
            })
        return definitions

    def _register_outputs(self, definition):
        namespace = definition["namespace"]
        seen_names = set()
        for mapping in definition["mappings"]:
            name = self.mapping_key(mapping.get("id"), namespace)
            if name in seen_names:
                raise ValueError(
                    f"配置层 {definition['layer']} 中基础映射 ID 重复："
                    f"{mapping.get('id')}"
                )
            seen_names.add(name)
            self._add_virtual_key(
                name,
                kanata_output(
                    mapping.get("target_modifiers", "无"),
                    mapping.get("target", "A"),
                    tap=False,
                ),
                releasable=True,
            )

        for preset in definition["presets"]:
            for action_index, action in enumerate(
                iter_action_tree(preset.get("actions", []))
            ):
                output = self.action_output(action)
                if output is None:
                    continue
                name = self.preset_action_key(
                    preset.get("id"), action_index, namespace
                )
                if name in seen_names:
                    raise ValueError(
                        f"配置层 {definition['layer']} 的预设动作虚拟键重复："
                        f"{preset.get('name', '未命名预设')}"
                    )
                seen_names.add(name)
                self._add_virtual_key(name, output, releasable=True)

    def _layer_rules(self, definition):
        namespace = definition["namespace"]
        scope = definition["scope"]
        rules_by_source = {}
        for index, mapping in enumerate(definition["mappings"]):
            if not mapping.get("enabled"):
                continue
            source = mapping.get("source", "F6")
            if self.keyboard_hwids_exclude and source not in MOUSE_NAMES:
                continue
            mode = mapping.get("mode", "同步按住")
            condition = kanata_mapping_condition(mapping)
            mouse_sync_target = (
                mode == "同步按住" and mapping.get("target") in MOUSE_NAMES
            )
            if condition is not None or mouse_sync_target:
                # Conditional mappings and mouse-button hold outputs are dispatched
                # through Python so Release can be matched against the foreground
                # window where the Press was created.
                output = self._trigger_action(
                    "mapping", mapping.get("id"), namespace, scope
                )
            elif source in MOUSE_NAMES or mode == "同步按住":
                output = self._mapping_action(mapping, namespace)
            else:
                output = self._trigger_action(
                    "mapping", mapping.get("id"), namespace, scope
                )
            rules_by_source.setdefault(source, []).append((
                mapping.get("source_modifiers", "无"), condition,
                output, 0, index, "source"
            ))
        for index, preset in enumerate(definition["presets"]):
            if not preset.get("enabled"):
                continue
            source = preset.get("trigger", "F1")
            if self.keyboard_hwids_exclude and source not in MOUSE_NAMES:
                continue
            output = self._trigger_action(
                "preset", preset.get("id"), namespace, scope
            )
            rules_by_source.setdefault(source, []).append((
                preset.get("trigger_modifiers", "无"), None,
                output, 1, index, "source"
            ))
        return rules_by_source

    def _rules_to_action(self, source, rules):
        if not rules:
            return kanata_key(source)
        rules.sort(key=lambda item: (
            -len(modifier_names(item[0])),
            0 if item[1] is not None else 1,
            item[3], item[4],
        ))
        switch_cases = []
        direct = None
        for item in rules:
            if len(item) == 5:
                modifiers, mapping_condition, output, _kind_order, _index = item
                match_mode = "exact"
            else:
                (
                    modifiers, mapping_condition, output, _kind_order, _index,
                    match_mode,
                ) = item
            if match_mode == "source":
                modifier_condition = kanata_source_modifier_condition(
                    modifiers, source
                )
            else:
                modifier_condition = kanata_exact_modifier_condition(
                    modifiers, source
                )
            conditions = [
                item for item in (modifier_condition, mapping_condition) if item
            ]
            if not conditions:
                if direct is None:
                    direct = output
                continue
            condition = (
                conditions[0] if len(conditions) == 1
                else f"(and {' '.join(conditions)})"
            )
            switch_cases.append(f"({condition}) {output} break")
        if not switch_cases:
            return direct or kanata_key(source)
        alias = f"src{len(self.aliases)}"
        fallback = direct or kanata_key(source)
        self.aliases.append((
            alias,
            f"(switch {' '.join(switch_cases)} () {fallback} break)",
        ))
        return f"@{alias}"

    def build(self):
        self._add_virtual_key("mchealth", "nop0")
        definitions = self._layer_definitions()
        layer_rules = {}
        source_order = []
        source_seen = set()

        def remember_source(source):
            if source not in source_seen:
                source_seen.add(source)
                source_order.append(source)

        for definition in definitions:
            self._register_outputs(definition)
            rules = self._layer_rules(definition)
            layer_rules[definition["layer"]] = rules
            for source in rules:
                remember_source(source)
            for mapping in definition["mappings"]:
                if mapping.get("enabled") and mapping.get("condition_enabled"):
                    condition_source = mapping.get(
                        "condition_input", "鼠标左键"
                    )
                    remember_source(condition_source)
                    self._register_condition_state_source(condition_source)

        release_actions = [
            f"(on-press release-vkey {name})"
            for name in sorted(self.releasable_virtual_keys)
        ]
        emergency_action = self._control_action(
            "emergency", None, release_actions
        )
        # The global switch now starts/stops the whole input engine.  Kanata
        # only reports the request; the GUI performs the bounded shutdown and
        # keeps the lightweight Windows hook alive so the same shortcut can
        # start the engine again.
        toggle_action = (
            self._control_action("toggle", None, release_actions)
            if self.global_toggle_enabled else None
        )
        pause_action = (
            self._control_action("pause", None, [])
            if self.macro_pause_enabled else None
        )

        if not self.keyboard_hwids_exclude:
            remember_source(self.emergency_key)
            if toggle_action:
                remember_source(self.global_toggle_key)
            if pause_action:
                remember_source(self.macro_pause_key)
            for definition in definitions:
                rules = layer_rules[definition["layer"]]
                rules.setdefault(self.emergency_key, []).append((
                    self.emergency_modifiers, None, emergency_action, -2, -2,
                    "exact"
                ))
                if toggle_action:
                    rules.setdefault(self.global_toggle_key, []).append((
                        self.global_toggle_modifiers, None, toggle_action, -1, -1,
                        "exact"
                    ))
                if pause_action:
                    rules.setdefault(self.macro_pause_key, []).append((
                        self.macro_pause_modifiers, None, pause_action, -3, -3,
                        "exact"
                    ))

        if len(self.virtual_keys) > self.MAX_VIRTUAL_KEYS:
            per_layer = []
            for definition in definitions:
                per_layer.append(
                    f"{definition['layer']}："
                    f"{len(definition['mappings'])} 映射 / "
                    f"{len(definition['presets'])} 预设"
                )
            raise ValueError(
                f"Kanata 虚拟键数量为 {len(self.virtual_keys)}，超过上限 "
                f"{self.MAX_VIRTUAL_KEYS}。请减少启用档案或动作数量。\n"
                + "；".join(per_layer)
            )

        lines = [
            ";; Generated by MacroCanvas. Manual edits will be overwritten.",
            f";; Generator revision: {self.GENERATED_CONFIG_REVISION}",
            ";; Base and enabled profiles are precompiled for hot layer switching.",
            "(defcfg",
            "  process-unmapped-keys no",
            "  override-release-on-activation no",
            "  concurrent-tap-hold yes",
        ]
        if self.mouse_hwids:
            lines.append("  windows-interception-mouse-hwids (")
            for raw in self.mouse_hwids:
                lines.append("    " + ",".join(str(value) for value in raw))
            lines.append("  )")
        if self.keyboard_hwids_exclude:
            lines.append("  windows-interception-keyboard-hwids-exclude (")
            for raw in self.keyboard_hwids_exclude:
                lines.append("    " + ",".join(str(value) for value in raw))
            lines.append("  )")
        if self.keyboard_hwids:
            lines.append("  windows-interception-keyboard-hwids (")
            for raw in self.keyboard_hwids:
                lines.append("    " + ",".join(str(value) for value in raw))
            lines.append("  )")
        lines.extend([")", ""])

        lines.append("(defvirtualkeys")
        for name, action in self.virtual_keys:
            lines.append(f"  {name} {action}")
        lines.extend([")", ""])

        if not source_order:
            source_order = ["F24"]
        sources = [kanata_key(source) for source in source_order]

        compiled_layers = []
        for definition in definitions:
            rules = layer_rules[definition["layer"]]
            actions = [
                self._with_condition_state_notifications(
                    source,
                    self._rules_to_action(
                        source, list(rules.get(source, []))
                    ),
                )
                for source in source_order
            ]
            compiled_layers.append((definition["layer"], actions))

        disabled_rules = {}
        if not self.keyboard_hwids_exclude:
            disabled_rules.setdefault(self.emergency_key, []).append((
                self.emergency_modifiers, None, emergency_action, -2, -2,
                "exact"
            ))
            if toggle_action:
                disabled_rules.setdefault(self.global_toggle_key, []).append((
                    self.global_toggle_modifiers, None, toggle_action, -1, -1,
                    "exact"
                ))
            if pause_action:
                disabled_rules.setdefault(self.macro_pause_key, []).append((
                    self.macro_pause_modifiers, None, pause_action, -3, -3,
                    "exact"
                ))
        disabled_actions = [
            self._with_condition_state_notifications(
                source,
                self._rules_to_action(
                    source, list(disabled_rules.get(source, []))
                ),
            )
            for source in source_order
        ]

        if self.aliases:
            lines.append("(defalias")
            for name, action in self.aliases:
                lines.append(f"  {name} {action}")
            lines.extend([")", ""])

        lines.extend([
            "(defsrc",
            "  " + " ".join(sources),
            ")",
            "",
        ])
        for layer, actions in compiled_layers:
            lines.extend([
                f"(deflayer {layer}",
                "  " + " ".join(actions),
                ")",
                "",
            ])
        lines.extend([
            "(deflayer disabled",
            "  " + " ".join(disabled_actions),
            ")",
            "",
        ])
        return "\n".join(lines)

class KanataEngine(KanataCommandRuntimeMixin):
    EXECUTABLES = {
        "普通模式（winIOv2）": "kanata_windows_tty_winIOv2_x64.exe",
        "游戏模式（Interception）": "kanata_windows_tty_wintercept_x64.exe",
    }

    def __init__(
        self, config_path=KANATA_CONFIG_PATH, log_path=KANATA_LOG_PATH,
        instance_name="main",
    ):
        self.config_path = Path(config_path)
        self.log_path = Path(log_path)
        self.instance_name = instance_name
        self.process = None
        self.log_file = None
        self.current_backend = None
        # Every launch gets a private loopback port. A fixed port can connect the
        # app to an orphaned Kanata process from an earlier run, whose virtual-key
        # table does not match the current kanata.kbd file.
        self.control_port = None

        # All ActOnFakeKey requests are written to one persistent TCP stream.
        # Opening a new connection and waiting for recv() from inside a low-level
        # Windows hook can deadlock the hook with Kanata's generated output. It can
        # also make Windows silently remove the hook after LowLevelHooksTimeout.
        self.command_queue = queue.Queue()
        self.command_stop = threading.Event()
        self.command_thread = None
        self.command_socket = None
        self.command_socket_lock = threading.RLock()
        self.receive_buffer = b""
        self.last_command_error = ""
        self.tcp_error_generation = 0
        self.fake_key_names_received = False
        self.fake_key_names_generation = 0
        self.available_fake_keys = set()
        self.message_callback = None
        self.active_virtual_keys = set()
        self.active_virtual_keys_lock = threading.RLock()
        # Mouse releases can be quarantined while another foreground window is
        # active.  Killing the engine resets those keys without injecting a
        # MouseUp into the unrelated window.
        self.quarantined_virtual_keys = set()
        self.current_layer = "base"
        self.process_job = None
        self.last_start_warning = ""

    def set_message_callback(self, callback):
        self.message_callback = callback

    @staticmethod
    def _find_free_control_port():
        # Ask Windows for a currently unused ephemeral loopback port. Kanata is
        # started on this port immediately afterwards, so this process cannot
        # accidentally talk to an older instance still listening on port 5829.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _config_defines_virtual_key(self, name):
        try:
            config = self.config_path.read_text("utf-8", errors="replace")
        except OSError:
            return False
        pattern = rf"(?m)^\s*{re.escape(name)}\s+"
        return bool(re.search(pattern, config))

    def _stop_stale_app_instances(self):
        # A previous GUI crash can leave its Kanata child alive. Kill only Kanata
        # processes whose command line contains MacroCanvas' own config path;
        # unrelated user-managed Kanata instances are left untouched.
        if os.name != "nt":
            return
        escaped = str(self.config_path).replace("'", "''")
        script = (
            f"$cfg = '{escaped}'; "
            "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
            "Where-Object { "
            "$_.Name -like 'kanata*.exe' -and "
            "$_.CommandLine -and $_.CommandLine.Contains($cfg) "
            "} | ForEach-Object { "
            "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue "
            "}"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", script,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def executable(self, backend):
        return kanata_dir() / self.EXECUTABLES[backend]

    def check_dependencies(self, backend):
        executable = self.executable(backend)
        if not executable.exists():
            return False, f"找不到 Kanata：{executable}"
        if backend == "游戏模式（Interception）":
            dll = kanata_dir() / "interception.dll"
            if not dll.exists():
                return False, (
                    "游戏模式缺少 interception.dll。\n\n"
                    "请从 Kanata 官方 release 的 Interception/wintercept "
                    "配套包中取得该 DLL，并放到当前 Kanata 组件目录；同时还需要按"
                    "官方说明安装 Interception 驱动并重启电脑。"
                )
        return True, ""

    def validate(self, backend):
        ok, message = self.check_dependencies(backend)
        if not ok:
            return False, message
        executable = self.executable(backend)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                [
                    str(executable), "--cfg", str(self.config_path),
                    "--check", "--no-wait",
                ],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10, creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"Kanata 校验程序无法启动：{error}"
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output

    def start(self, backend, validate_config=True):
        # The health-check key must be present in the exact config that is about
        # to be loaded. This catches stale or externally replaced config files.
        if not self._config_defines_virtual_key("mchealth"):
            return False, (
                "当前 kanata.kbd 中缺少启动检查键 mchealth。"
                "请点击“应用更改”后重新启动 Kanata。"
            )
        if validate_config:
            ok, message = self.validate(backend)
            if not ok:
                return False, message or "Kanata 配置校验失败"
        if not self.stop():
            return False, "旧的 Kanata 实例或命令线程未能安全停止"
        self._stop_stale_app_instances()
        # Give Windows a brief moment to release the Interception/winIO handle
        # and any loopback listener owned by the terminated child.
        time.sleep(0.12)
        try:
            self.control_port = self._find_free_control_port()
        except OSError as error:
            return False, f"无法分配 Kanata TCP 控制端口：{error}"

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_path.open("w", encoding="utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(
                [
                    str(self.executable(backend)),
                    "--cfg", str(self.config_path),
                    "--port", f"127.0.0.1:{self.control_port}",
                    "--no-wait",
                ],
                cwd=str(kanata_dir()),
                stdout=self.log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            self.process_job = WindowsKillOnCloseJob()
            self.last_start_warning = ""
            if os.name == "nt" and not self.process_job.assign(self.process):
                self.last_start_warning = (
                    self.process_job.last_error
                    or "Kanata 子进程自动清理保护绑定失败"
                )
                self.process_job.close()
                self.process_job = None
        except OSError as error:
            if self.log_file:
                self.log_file.close()
                self.log_file = None
            self.control_port = None
            return False, f"Kanata 进程无法启动：{error}"
        self.current_backend = backend

        # A running process is insufficient: every mode now uses ActOnFakeKey.
        # Wait until the server accepts a persistent connection and successfully
        # receives a harmless virtual-key command.
        ok, message = self._open_and_probe_tcp(timeout=4.0)
        if not ok:
            log = self.read_log()
            self._terminate_process_only(sweep_orphans=True)
            detail = message or "Kanata TCP 控制通道无法连接"
            if log:
                detail += "\n\n" + log
            return False, detail

        self._start_command_worker()
        message = "Kanata 输入引擎与 TCP 控制通道运行中"
        if self.last_start_warning:
            message += f"；警告：{self.last_start_warning}"
        return True, message

    @staticmethod
    def _kill_process_tree(pid):
        """Best-effort termination of a Kanata launcher and all descendants."""
        if os.name != "nt" or not pid:
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(int(pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                creationflags=creationflags,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

    def _terminate_process_only(self, sweep_orphans=False):
        process = self.process
        pid = getattr(process, "pid", None) if process else None
        process_stopped = True
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1.2)
            except (OSError, PermissionError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except (OSError, PermissionError, subprocess.TimeoutExpired):
                    process_stopped = False
        # Some Kanata Windows builds may leave a descendant process behind even
        # after the Popen handle exits. taskkill /T handles that process tree.
        self._kill_process_tree(pid)
        if process and process.poll() is None:
            try:
                process.wait(timeout=0.5)
            except (OSError, PermissionError, subprocess.TimeoutExpired):
                process_stopped = False
        if process_stopped:
            self.process = None
            if self.process_job:
                self.process_job.close()
                self.process_job = None
            self.current_backend = None
        else:
            # Preserve the live handle so a later shutdown retry can continue
            # managing it instead of reporting a false stopped state.
            self.process = process
        self._close_command_socket()
        self.control_port = None
        if self.log_file:
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None
        if sweep_orphans:
            # Final fallback for a detached descendant whose PID is no longer in
            # the original process tree: only kill Kanata instances using this
            # application's own generated config path.
            self._stop_stale_app_instances()
        return process_stopped

    def stop(self, timeout=3.0):
        had_running_process = bool(self.process and self.process.poll() is None)
        release_ok = True
        if had_running_process:
            # Do not trust task-local state alone: a timed-out Release used to be
            # forgotten and could leave Alt/Ctrl/Shift logically held.
            release_ok = bool(self.release_all_virtual_keys(timeout=1.5))
            self.flush_commands(timeout=0.8)
        worker_ok = self._stop_command_worker(
            timeout=min(2.0, max(0.2, float(timeout)))
        )
        self._close_command_socket()
        process_ok = bool(self._terminate_process_only(sweep_orphans=True))
        if process_ok:
            with self.active_virtual_keys_lock:
                self.active_virtual_keys.clear()
                self.quarantined_virtual_keys.clear()
        return bool(worker_ok and process_ok and release_ok)

    def is_running(self):
        return bool(self.process and self.process.poll() is None)

    def trigger_virtual_key(self, name):
        return self.trigger_virtual_key_action(name, "Tap")

    def trigger_virtual_key_action(self, name, action):
        if not self.is_running():
            raise OSError("Kanata 尚未运行")
        if not self.queue_virtual_key_action(
            name, action, wait=True, timeout=1.0
        ):
            raise OSError(self.last_command_error or "Kanata 命令发送失败")
        return "queued"

    def read_log(self):
        try:
            text = self.log_path.read_text("utf-8", errors="replace")
        except OSError:
            return ""
        # Remove ANSI colour codes before displaying redirected terminal output.
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        return text.strip()
