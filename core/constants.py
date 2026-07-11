import os
import json
import sys
from enum import Enum
from pathlib import Path

from PySide6.QtCore import Qt


APP_NAME = "MacroCanvas"
APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / APP_NAME
CONFIG_PATH = APP_DIR / "config.json"
CONFIG_BACKUP_DIR = APP_DIR / "config_backups"
CONFIG_BACKUP_LIMIT = 10
LEGACY_CONFIG_PATH = APP_DIR / "mappings.json"
KANATA_SETTINGS_PATH = APP_DIR / "components.json"

# Official upstream pages for the runtime components used by MacroCanvas.
KANATA_GITHUB_URL = "https://github.com/jtroo/kanata"
KANATA_RELEASES_URL = "https://github.com/jtroo/kanata/releases"
INTERCEPTION_GITHUB_URL = "https://github.com/oblitum/Interception"
INTERCEPTION_RELEASES_URL = "https://github.com/oblitum/Interception/releases"


def kanata_dir():
    """Return the configured/portable Kanata component directory.

    The small component setting is intentionally separate from config.json so a
    damaged main configuration can be recovered without losing the executable
    location, and changing this path never serializes pending editor contents.
    """
    override = str(os.getenv("MACROCANVAS_KANATA_DIR", "") or "").strip()
    if override:
        return Path(override).expanduser()
    try:
        payload = json.loads(KANATA_SETTINGS_PATH.read_text("utf-8"))
        configured = str(payload.get("kanata_dir", "") or "").strip()
        if configured:
            return Path(configured).expanduser()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    application_root = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[1]
    )
    portable = application_root / "kanata"
    local_components = APP_DIR / "kanata"
    legacy = Path(r"E:\kanata")
    for candidate in (portable, local_components, legacy):
        if candidate.exists():
            return candidate
    return portable
KANATA_CONFIG_PATH = APP_DIR / "kanata.kbd"
KANATA_LOG_PATH = APP_DIR / "kanata.log"
KANATA_KEYBOARD_CONFIG_PATH = APP_DIR / "kanata-keyboard.kbd"
KANATA_KEYBOARD_LOG_PATH = APP_DIR / "kanata-keyboard.log"
DIAGNOSTIC_LOG_PATH = APP_DIR / "diagnostic.log"
DIAGNOSTIC_MAX_LINES = 500
DIAGNOSTIC_TRIM_INTERVAL = 50
KANATA_FALLBACK_PORT = 5829
# 录制结果最终受单预设动作上限约束。原始事件按最坏情况下“一个事件 + 一个等待”
# 预留容量，避免长时间录制在主线程转换前无界占用内存。
MAX_RECORDING_RAW_EVENTS = 5_000
MAX_RECORDING_DURATION_MS = 30 * 60 * 1000


MOUSE_NAMES = ["鼠标左键", "鼠标右键", "鼠标中键", "鼠标侧键 1", "鼠标侧键 2"]
KEY_NAMES = (
    ["Ctrl", "Shift", "Alt", "Caps Lock", "Tab", "Enter", "Space", "Esc",
     "Backspace", "Delete", "Insert", "Home", "End", "Page Up", "Page Down",
     "Print Screen", "Pause", "Menu", "方向上", "方向下", "方向左", "方向右",
     "静音", "音量减", "音量加", "上一曲", "下一曲", "播放/暂停",
     "`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/"]
    + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("0123456789")
    + [f"F{i}" for i in range(1, 25)]
)
INPUT_NAMES = MOUSE_NAMES + KEY_NAMES
SOURCE_NAMES = [name for name in INPUT_NAMES if name != "Esc"]
TRIGGER_NAMES = [name for name in INPUT_NAMES if name != "Esc"]
GLOBAL_TOGGLE_KEYS = [
    name for name in SOURCE_NAMES if name not in ("Ctrl", "Shift", "Alt")
]
SYSTEM_HOTKEY_KEYS = [name for name in INPUT_NAMES if name != "Esc"]
ACTION_TYPES = ["键盘点击", "鼠标点击", "鼠标滚轮", "鼠标移动", "等待"]
LOOP_ACTION_TYPE = "循环动作"
LOOP_DATA_ROLE = int(Qt.ItemDataRole.UserRole)
LOOP_TYPE_ROLE = LOOP_DATA_ROLE + 1
ACTION_ID_ROLE = LOOP_TYPE_ROLE + 1
LOOP_EXECUTION_MODES = ["执行次数", "无限循环"]
LOOP_COLOR_THEMES = [
    {"background": "#102a36", "control": "#173b49", "accent": "#38bdf8", "text": "#dff7ff"},
    {"background": "#2b1d38", "control": "#3b2850", "accent": "#c084fc", "text": "#f4e8ff"},
    {"background": "#382713", "control": "#4a341a", "accent": "#f59e0b", "text": "#fff0cf"},
    {"background": "#143023", "control": "#1d4330", "accent": "#4ade80", "text": "#e3ffed"},
    {"background": "#3a1c28", "control": "#4d2635", "accent": "#fb7185", "text": "#ffe6eb"},
    {"background": "#1d2b45", "control": "#273a5b", "accent": "#60a5fa", "text": "#e6f0ff"},
]
EXECUTION_MODES = ["执行一次", "固定次数", "按住循环", "开关循环", "无限循环"]
MAPPING_MODES = ["同步按住"] + EXECUTION_MODES
MAPPING_CONDITION_STATES = ["按住时", "松开时"]
MODIFIER_OPTIONS = ["无", "Ctrl", "Shift", "Alt", "Ctrl+Shift", "Ctrl+Alt", "Shift+Alt", "Ctrl+Shift+Alt"]
_MOUSE_HWID_CACHE = None
_KEYBOARD_HWID_CACHE = None


class EngineState(Enum):
    STOPPED = "已停止"
    RUNNING = "运行中"
    FAILED = "启动失败"


class ConfigState(Enum):
    SAVED = "已保存"
    DIRTY = "有未应用更改"
    APPLIED = "已应用"
    FAILED = "应用失败"


class MacroState(Enum):
    IDLE = "空闲"
    COUNTDOWN = "倒计时"
    RUNNING = "执行中"
    PAUSED = "暂停"
    RECORDING = "录制中"
    STOPPING = "正在停止"
    STOP_TIMEOUT = "停止超时"


