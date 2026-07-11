import copy
import uuid


def remap_action_ids(actions, preserve_external=False):
    """Clone an action tree and rewrite action/loop references consistently.

    ``preserve_external`` is used by in-app copy/paste: references to actions
    outside the copied selection remain intact when pasting into the same
    preset, and the destination's normal synchronization removes them when they
    do not exist there. Imports keep the stricter historical behaviour.
    """
    cloned = copy.deepcopy(actions or [])
    id_map = {}

    def allocate(items):
        for action in items:
            if action.get("type") != "循环动作":
                old = str(action.get("action_id") or uuid.uuid4().hex)
                new = uuid.uuid4().hex
                id_map[old] = new
                action["action_id"] = new
            allocate(action.get("children", []))

    def rewrite(items):
        for action in items:
            if action.get("type") == "循环动作":
                action["id"] = uuid.uuid4().hex
                target_ids = []
                for value in map(str, action.get("target_action_ids", [])):
                    if value in id_map:
                        target_ids.append(id_map[value])
                    elif preserve_external:
                        target_ids.append(value)
                action["target_action_ids"] = target_ids
                action["children"] = []
            else:
                rewrite(action.get("children", []))

    allocate(cloned)
    rewrite(cloned)
    return cloned


def clone_preset_for_import(preset, suffix="（导入）"):
    copied = copy.deepcopy(preset)
    copied["id"] = uuid.uuid4().hex
    copied["name"] = (str(copied.get("name") or "未命名预设") + suffix)
    copied["actions"] = remap_action_ids(copied.get("actions", []))
    return copied


def clone_mapping_for_import(mapping, suffix="（导入）"):
    copied = copy.deepcopy(mapping)
    copied["id"] = uuid.uuid4().hex
    copied["name"] = str(copied.get("name") or "基础映射") + suffix
    return copied

def clone_profile_for_import(profile, name=None):
    """Clone one configuration profile with an isolated ID namespace.

    Profile matching rules are preserved, while all mapping, preset, action and
    loop IDs inside the snapshot are regenerated so the imported profile can
    safely coexist with the current configuration.
    """
    copied = copy.deepcopy(profile if isinstance(profile, dict) else {})
    copied["id"] = uuid.uuid4().hex
    copied["name"] = str(name or copied.get("name") or "未命名档案")
    payload = copied.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    copied["payload"] = {
        "mappings": [
            clone_mapping_for_import(mapping, suffix="")
            for mapping in payload.get("mappings", []) or []
        ],
        "presets": [
            clone_preset_for_import(preset, suffix="")
            for preset in payload.get("presets", []) or []
        ],
    }
    return copied


def _unique_import_name(original, existing_names, suffix="（导入）"):
    base = str(original or "未命名")
    candidate = f"{base}{suffix}"
    if candidate not in existing_names:
        existing_names.add(candidate)
        return candidate
    number = 2
    while True:
        candidate = f"{base}{suffix}{number}"
        if candidate not in existing_names:
            existing_names.add(candidate)
            return candidate
        number += 1


def merge_full_configurations(current, imported):
    """Merge all mapping/profile content while keeping current global settings.

    Scalar application settings cannot be meaningfully merged.  The current
    backend, hotkeys, diagnostics and active/editor profile selections therefore
    remain unchanged.  Imported base mappings/presets and all imported profiles
    are appended with fresh IDs.
    """
    merged = copy.deepcopy(current if isinstance(current, dict) else {})
    source = imported if isinstance(imported, dict) else {}

    merged_mappings = list(merged.get("mappings", []) or [])
    mapping_names = {str(item.get("name") or "") for item in merged_mappings}
    for mapping in source.get("mappings", []) or []:
        copied = clone_mapping_for_import(mapping, suffix="")
        copied["name"] = _unique_import_name(
            copied.get("name") or "基础映射", mapping_names
        )
        merged_mappings.append(copied)
    merged["mappings"] = merged_mappings

    merged_presets = list(merged.get("presets", []) or [])
    preset_names = {str(item.get("name") or "") for item in merged_presets}
    for preset in source.get("presets", []) or []:
        copied = clone_preset_for_import(preset, suffix="")
        copied["name"] = _unique_import_name(
            copied.get("name") or "未命名预设", preset_names
        )
        merged_presets.append(copied)
    merged["presets"] = merged_presets

    merged_profiles = list(merged.get("profiles", []) or [])
    profile_names = {str(item.get("name") or "") for item in merged_profiles}
    for profile in source.get("profiles", []) or []:
        imported_name = _unique_import_name(
            profile.get("name") if isinstance(profile, dict) else "未命名档案",
            profile_names,
        )
        merged_profiles.append(clone_profile_for_import(profile, imported_name))
    merged["profiles"] = merged_profiles
    return merged

