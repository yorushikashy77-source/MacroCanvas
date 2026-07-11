import re


MODIFIER_ORDER = ["Ctrl", "Shift", "Alt"]


def modifier_names(value):
    if not value or value == "无":
        return []
    return [name for name in MODIFIER_ORDER if name in value.split("+")]


def modifier_set(value):
    return set(modifier_names(value))


def source_modifiers_match(configured, current):
    """Return whether a source trigger accepts the current modifier state.

    Source mappings and preset triggers use loose modifier matching: the
    configured modifiers must be held, but temporary extra Ctrl / Shift / Alt
    keys do not block the source key.  Selection still prefers the rule with
    the most configured modifiers, so Ctrl+Caps Lock can override Caps Lock
    while Caps Lock remains usable under Ctrl / Shift / Alt when no more
    specific rule exists.
    """
    return modifier_set(configured).issubset(modifier_set(current))


def source_modifier_specificity(configured):
    return len(modifier_set(configured))


def combo_text(modifiers, key):
    names = modifier_names(modifiers)
    return "+".join(names + [key]) if names else key


def normalize_input_name(value):
    """Return one canonical source name for mappings and presets alike."""
    text = str(value or "").strip()
    compact = re.sub(r"[\s_\-]+", "", text).lower()
    aliases = {
        "鼠标侧键1": "鼠标侧键 1",
        "鼠标侧键2": "鼠标侧键 2",
        "鼠标后退键": "鼠标侧键 1",
        "鼠标前进键": "鼠标侧键 2",
        "xbutton1": "鼠标侧键 1",
        "xbutton2": "鼠标侧键 2",
        "mouse4": "鼠标侧键 1",
        "mouse5": "鼠标侧键 2",
        "browserback": "鼠标侧键 1",
        "browserbackward": "鼠标侧键 1",
        "browserforward": "鼠标侧键 2",
        "vk-a6": "鼠标侧键 1",
        "vk-a7": "鼠标侧键 2",
        "vk-05": "鼠标侧键 1",
        "vk-06": "鼠标侧键 2",
    }
    return aliases.get(compact, text)


def mapping_condition_satisfied(mapping, held_inputs):
    """Return whether one optional mapping-state condition currently matches."""
    if not bool(mapping.get("condition_enabled", False)):
        return True
    condition_input = normalize_input_name(mapping.get("condition_input", ""))
    if not condition_input:
        return False
    is_pressed = condition_input in set(held_inputs or ())
    state = str(mapping.get("condition_state") or "按住时")
    if state == "按住时":
        return is_pressed
    if state == "松开时":
        return not is_pressed
    return False
