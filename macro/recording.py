import copy
import math


_MOVE_PREFIXES = {
    "rel:": "rel",
    "pct:": "pct",
    "window:": "window",
    "client:": "client",
}
_MOVE_MODE_PREFIX = {
    "screen": "",
    "rel": "rel:",
    "pct": "pct:",
    "window": "window:",
    "client": "client:",
}


def _action_id(action):
    return str(action.get("action_id") or "")


def _position(target):
    text = str(target or "0,0").strip()
    mode = "screen"
    for prefix, candidate in _MOVE_PREFIXES.items():
        if text.startswith(prefix):
            mode = candidate
            text = text[len(prefix):]
            break
    try:
        x, y = (float(value.strip()) for value in text.split(",", 1))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return mode, x, y


def _number_text(value, decimals=4):
    rounded = round(float(value), decimals)
    if abs(rounded - round(rounded)) < 10 ** (-decimals):
        return str(int(round(rounded)))
    return f"{rounded:.{decimals}f}".rstrip("0").rstrip(".")


def _position_text(mode, x, y):
    decimals = 4 if mode == "pct" else 2
    return (
        _MOVE_MODE_PREFIX.get(mode, "")
        + f"{_number_text(x, decimals)},{_number_text(y, decimals)}"
    )


def _point_segment_distance(point, start, end):
    if start == end:
        return math.dist(point, start)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    projection = (
        (point[0] - start[0]) * dx + (point[1] - start[1]) * dy
    ) / length_squared
    projection = max(0.0, min(1.0, projection))
    nearest = (start[0] + projection * dx, start[1] + projection * dy)
    return math.dist(point, nearest)


def _rdp_indices(points, tolerance):
    """Return indexes kept by iterative Ramer-Douglas-Peucker simplification."""
    if len(points) <= 2 or tolerance <= 0:
        return set(range(len(points)))
    kept = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        start_index, end_index = pending.pop()
        maximum = -1.0
        maximum_index = -1
        start = points[start_index]
        end = points[end_index]
        for index in range(start_index + 1, end_index):
            distance = _point_segment_distance(points[index], start, end)
            if distance > maximum:
                maximum = distance
                maximum_index = index
        if maximum > tolerance and maximum_index > start_index:
            kept.add(maximum_index)
            pending.append((start_index, maximum_index))
            pending.append((maximum_index, end_index))
    return kept


def _mandatory_rdp_indices(points, tolerance, mandatory):
    if not points:
        return set()
    required = sorted({0, len(points) - 1, *mandatory})
    kept = set(required)
    for start, end in zip(required, required[1:]):
        segment = points[start:end + 1]
        kept.update(start + index for index in _rdp_indices(segment, tolerance))
    return kept


def _plain_wait(action, protected_ids):
    try:
        jitter = int(action.get("jitter_ms", 0) or 0)
    except (TypeError, ValueError):
        jitter = 1
    return (
        action.get("type") == "等待"
        and not action.get("children")
        and jitter == 0
        and _action_id(action) not in protected_ids
    )


def _wait_duration(action):
    try:
        return max(1, int(action.get("wait_ms", 1)))
    except (TypeError, ValueError):
        return 1


def _combined_waits(wait_actions):
    """Combine unprotected waits while respecting the schema's 600000 ms cap."""
    if not wait_actions:
        return []
    if len(wait_actions) == 1:
        return [copy.deepcopy(wait_actions[0])]
    total = sum(_wait_duration(action) for action in wait_actions)
    total_jitter = sum(max(0, int(action.get("jitter_ms", 0) or 0))
                       for action in wait_actions)
    template = copy.deepcopy(wait_actions[0])
    result = []
    first = True
    while total > 0:
        duration = min(600_000, total)
        action = copy.deepcopy(template)
        action["wait_ms"] = duration
        action["jitter_ms"] = min(600_000, total_jitter)
        action["children"] = []
        if not first:
            action.pop("action_id", None)
        result.append(action)
        total -= duration
        total_jitter = max(0, total_jitter - 600_000)
        first = False
    return result


def _move_context_signature(action):
    context = action.get("recording_context")
    if not isinstance(context, dict):
        return None
    virtual_screen = context.get("virtual_screen")
    if isinstance(virtual_screen, (list, tuple)):
        virtual_screen = tuple(virtual_screen)
    return (
        str(context.get("mode") or ""),
        str(context.get("process") or "").casefold(),
        str(context.get("title") or ""),
        context.get("width"),
        context.get("height"),
        context.get("monitor_count"),
        virtual_screen,
    )


def _compatible_move(action, mode, context_signature):
    if action.get("type") != "鼠标移动" or action.get("children"):
        return None
    position = _position(action.get("target"))
    if position is None or position[0] != mode:
        return None
    if _move_context_signature(action) != context_signature:
        return None
    return position


def _consume_move_run(
    items, start, gap_limit, tolerance, protected_ids, percentage_size=None,
):
    first_position = _position(items[start].get("target"))
    if first_position is None or items[start].get("children"):
        return [items[start]], start + 1
    mode = first_position[0]
    context_signature = _move_context_signature(items[start])
    actions = [items[start]]
    raw_positions = [(first_position[1], first_position[2])]
    wait_groups = []
    cursor = start + 1

    while cursor < len(items):
        probe = cursor
        waits = []
        wait_total = 0
        while probe < len(items) and _plain_wait(items[probe], protected_ids):
            candidate_duration = _wait_duration(items[probe])
            if wait_total + candidate_duration > gap_limit:
                break
            waits.append(items[probe])
            wait_total += candidate_duration
            probe += 1
        if probe >= len(items):
            break
        position = _compatible_move(
            items[probe], mode, context_signature
        )
        if position is None:
            break
        actions.append(items[probe])
        raw_positions.append((position[1], position[2]))
        wait_groups.append(waits)
        cursor = probe + 1

    if len(actions) < 3:
        return [items[start]], start + 1

    if mode == "rel":
        points = []
        current_x = current_y = 0.0
        for delta_x, delta_y in raw_positions:
            current_x += delta_x
            current_y += delta_y
            points.append((current_x, current_y))
    elif mode == "pct":
        try:
            width, height = percentage_size or (1920, 1080)
            width = max(1, int(width))
            height = max(1, int(height))
        except (TypeError, ValueError, OverflowError):
            width, height = 1920, 1080
        points = [
            (
                position_x * max(1, width - 1) / 100,
                position_y * max(1, height - 1) / 100,
            )
            for position_x, position_y in raw_positions
        ]
    else:
        points = raw_positions

    effective_tolerance = max(0.0, float(tolerance))
    mandatory = {
        index for index, action in enumerate(actions)
        if _action_id(action) in protected_ids
    }
    kept = sorted(_mandatory_rdp_indices(points, effective_tolerance, mandatory))
    if len(kept) >= len(actions):
        return [items[start]], start + 1

    result = []
    previous_kept = None
    for kept_position, action_index in enumerate(kept):
        action = copy.deepcopy(actions[action_index])
        if mode == "rel":
            origin = (0.0, 0.0) if previous_kept is None else points[previous_kept]
            endpoint = points[action_index]
            action["target"] = _position_text(
                mode, endpoint[0] - origin[0], endpoint[1] - origin[1]
            )
        result.append(action)
        previous_kept = action_index
        if kept_position + 1 >= len(kept):
            continue
        next_index = kept[kept_position + 1]
        waits = []
        for group_index in range(action_index, next_index):
            waits.extend(wait_groups[group_index])
        result.extend(_combined_waits(waits))
    return result, cursor


def _consume_wheel_run(items, start, gap_limit, protected_ids):
    first = items[start]
    if (
        first.get("type") != "鼠标滚轮"
        or first.get("children")
        or _action_id(first) in protected_ids
    ):
        return [first], start + 1
    direction = first.get("target")
    total_steps = max(1, int(first.get("steps", 1) or 1))
    cursor = start + 1
    merged_count = 1
    while cursor < len(items):
        probe = cursor
        wait_total = 0
        while probe < len(items) and _plain_wait(items[probe], protected_ids):
            duration = _wait_duration(items[probe])
            if wait_total + duration > gap_limit:
                break
            wait_total += duration
            probe += 1
        if probe >= len(items):
            break
        candidate = items[probe]
        if (
            candidate.get("type") != "鼠标滚轮"
            or candidate.get("target") != direction
            or candidate.get("children")
            or _action_id(candidate) in protected_ids
        ):
            break
        total_steps += max(1, int(candidate.get("steps", 1) or 1))
        merged_count += 1
        cursor = probe + 1

    if merged_count == 1:
        return [first], start + 1

    result = []
    first_chunk = True
    while total_steps > 0:
        chunk = copy.deepcopy(first)
        chunk["steps"] = min(100, total_steps)
        chunk["children"] = []
        if not first_chunk:
            chunk.pop("action_id", None)
        result.append(chunk)
        total_steps -= chunk["steps"]
        first_chunk = False
    return result, cursor


def simplify_recorded_actions(
    actions,
    speed_percent=100,
    min_hold_ms=30,
    simplify_moves=True,
    merge_wheel=True,
    merge_gap_ms=120,
    move_tolerance=6,
    protected_action_ids=None,
    adjust_timing=True,
    trim_edge_waits=True,
    percentage_size=None,
):
    """Conservatively organize recorded or existing action trees.

    Mouse paths are simplified only inside one sibling level and one coordinate
    mode. Short waits between path samples are retained and accumulated between
    the remaining points. Actions referenced by loop cards can be protected by
    passing their IDs, preventing cleanup from deleting those reference targets.
    """
    scale = 100 / max(10, int(speed_percent or 100))
    gap_limit = max(0, int(merge_gap_ms or 0))
    protected_ids = {
        str(value) for value in (protected_action_ids or set()) if str(value)
    }

    def clean_level(source, depth=0):
        prepared = []
        for source_action in source or []:
            raw = copy.deepcopy(source_action)
            raw["children"] = clean_level(raw.get("children", []), depth + 1)
            kind = raw.get("type")
            if adjust_timing:
                if kind == "等待":
                    raw["wait_ms"] = max(
                        1, round(int(raw.get("wait_ms", 1)) * scale)
                    )
                elif kind not in ("鼠标移动", "鼠标滚轮", "循环动作"):
                    raw["hold_ms"] = max(
                        int(min_hold_ms),
                        round(int(raw.get("hold_ms", 30)) * scale),
                    )
            prepared.append(raw)

        cleaned = []
        index = 0
        while index < len(prepared):
            action = prepared[index]
            kind = action.get("type")
            if simplify_moves and kind == "鼠标移动":
                replacement, next_index = _consume_move_run(
                    prepared, index, gap_limit, move_tolerance, protected_ids,
                    percentage_size=percentage_size,
                )
                cleaned.extend(replacement)
                index = next_index
                continue
            if merge_wheel and kind == "鼠标滚轮":
                replacement, next_index = _consume_wheel_run(
                    prepared, index, gap_limit, protected_ids
                )
                cleaned.extend(replacement)
                index = next_index
                continue
            cleaned.append(action)
            index += 1

        # Only root-level recording padding is safe to trim. A leading wait in a
        # child timeline is an intentional concurrency offset and must remain.
        if trim_edge_waits and depth == 0:
            while (
                cleaned
                and cleaned[0].get("type") == "等待"
                and _action_id(cleaned[0]) not in protected_ids
            ):
                cleaned.pop(0)
            while (
                cleaned
                and cleaned[-1].get("type") == "等待"
                and _action_id(cleaned[-1]) not in protected_ids
            ):
                cleaned.pop()
        return cleaned

    return clean_level(actions)
