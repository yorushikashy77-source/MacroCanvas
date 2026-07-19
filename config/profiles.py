"""Configuration-profile matching and runtime naming helpers.

Profiles only own mappings and presets. Global hotkeys, the selected backend,
recording settings and diagnostics remain part of the base application config.
"""

from __future__ import annotations

import copy
import hashlib
import ntpath
from typing import Iterable


BASE_PROFILE_ID = ""
BASE_LAYER_NAME = "base"
DISABLED_LAYER_NAME = "disabled"


def executable_name(value):
    """Return a process filename for both Windows and POSIX-shaped paths."""
    text = str(value or "").strip().replace("/", "\\")
    return ntpath.basename(text).lower()


def profile_matches(profile, process_name, window_title):
    if not profile.get("enabled", False):
        return False
    process_name = executable_name(process_name)
    title = str(window_title or "").lower()
    processes = [
        executable_name(value)
        for value in profile.get("process_names", [])
        if str(value).strip()
    ]
    titles = [
        str(value).strip().lower()
        for value in profile.get("title_contains", [])
        if str(value).strip()
    ]
    process_ok = not processes or process_name in processes
    title_ok = not titles or any(value in title for value in titles)
    return process_ok and title_ok and bool(processes or titles)


def profile_match_constraints(profile):
    processes = {
        executable_name(value)
        for value in profile.get("process_names", [])
        if str(value).strip()
    }
    titles = {
        str(value).strip().lower()
        for value in profile.get("title_contains", [])
        if str(value).strip()
    }
    return processes, titles


def profile_match_overlaps(left, right):
    """Return whether two enabled profiles can match the same foreground window."""
    if not left.get("enabled", False) or not right.get("enabled", False):
        return False
    left_processes, left_titles = profile_match_constraints(left)
    right_processes, right_titles = profile_match_constraints(right)
    if not (left_processes or left_titles) or not (right_processes or right_titles):
        return False

    # Empty process list means “any process”; otherwise process matching is exact.
    process_overlap = (
        not left_processes
        or not right_processes
        or bool(left_processes & right_processes)
    )
    if not process_overlap:
        return False

    # Empty title list means “any title”.  When both sides have title fragments,
    # a single window title can still contain fragments from both profiles, so
    # this is intentionally conservative and warns rather than blocks.
    return True


def select_profile(profiles, process_name, window_title):
    """Return the first enabled matching profile; list order is priority."""
    for profile in profiles or []:
        if profile_matches(profile, process_name, window_title):
            return profile
    return None


def profile_token(profile_id, length=12):
    """Stable compact alphanumeric token suitable for Kanata identifiers."""
    value = str(profile_id or "base")
    return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def profile_layer_name(profile_id):
    if not profile_id:
        return BASE_LAYER_NAME
    return f"pf{profile_token(profile_id, 12)}"


def profile_namespace(profile_id):
    if not profile_id:
        return ""
    return f"q{profile_token(profile_id, 10)}"


def profile_payload(profile):
    payload = profile.get("payload") if isinstance(profile, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    return {
        "mappings": copy.deepcopy(payload.get("mappings", [])),
        "presets": copy.deepcopy(payload.get("presets", [])),
    }


def normalize_profile(profile):
    """Normalize old profile records without changing their matching meaning."""
    item = copy.deepcopy(profile if isinstance(profile, dict) else {})
    item["id"] = str(item.get("id") or "")
    item["name"] = str(item.get("name") or "未命名档案")
    item["enabled"] = bool(item.get("enabled", False))
    item["process_names"] = [
        str(value).strip()
        for value in item.get("process_names", [])
        if str(value).strip()
    ]
    item["title_contains"] = [
        str(value).strip()
        for value in item.get("title_contains", [])
        if str(value).strip()
    ]
    item["payload"] = profile_payload(item)
    # Kept only so an old file round-trips safely. Runtime matching no longer
    # preserves a profile when the foreground window stops matching it.
    item["allow_other_windows"] = False
    return item


def enabled_profiles(profiles: Iterable[dict]):
    return [normalize_profile(item) for item in profiles or [] if item.get("enabled", False)]


def profile_summary(profile):
    payload = profile_payload(profile)
    mappings = payload["mappings"]
    presets = payload["presets"]
    mouse_names = {
        "鼠标左键", "鼠标右键", "鼠标中键", "鼠标侧键 1", "鼠标侧键 2",
    }

    def action_counts(actions):
        total = 0
        outputs = 0
        branch_types = {"条件成立分支", "否则分支"}
        non_output_types = {
            "等待", "循环动作", "调用子宏", "条件分支", "等待条件",
            *branch_types,
        }
        stack = list(actions or [])
        while stack:
            action = stack.pop()
            if action.get("type") not in branch_types:
                total += 1
            if action.get("type") not in non_output_types:
                outputs += 1
            stack.extend(action.get("children", []) or [])
        return total, outputs

    action_total = 0
    action_outputs = 0
    for preset in presets:
        count, outputs = action_counts(preset.get("actions", []))
        action_total += count
        action_outputs += outputs

    virtual_keys = len(mappings) + action_outputs
    for mapping in mappings:
        if not mapping.get("enabled"):
            continue
        source = mapping.get("source", "F6")
        mode = mapping.get("mode", "同步按住")
        if mapping.get("condition_enabled", False):
            virtual_keys += 2
        elif source not in mouse_names and mode != "同步按住":
            virtual_keys += 2
        elif source in mouse_names and mode in ("按住循环", "开关循环", "无限循环"):
            virtual_keys += 1
    virtual_keys += 2 * sum(1 for item in presets if item.get("enabled"))
    condition_sources = {
        mapping.get("condition_input", "鼠标左键")
        for mapping in mappings
        if mapping.get("enabled") and mapping.get("condition_enabled", False)
    }
    virtual_keys += 2 * len(condition_sources)

    return {
        "mappings": len(mappings),
        "presets": len(presets),
        "actions": action_total,
        "virtual_keys": virtual_keys,
    }
