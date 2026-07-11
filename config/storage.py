import json
import os
import time
import uuid
from pathlib import Path


def atomic_write_text(path, text, encoding="utf-8"):
    """Durably replace a text file without exposing a partial write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def canonical_json_signature(value):
    """Return a stable signature string for JSON data or serialized JSON."""
    if isinstance(value, str):
        value = json.loads(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def list_config_snapshots(directory, prefix, legacy_prefixes=()):
    """List snapshot files newest first.

    ``legacy_prefixes`` is used only for backward-compatible saved snapshots.
    Applied snapshots intentionally never treat old generic backups as proof
    that a configuration was successfully loaded by an input backend.
    """
    directory = Path(directory)
    paths = []
    seen = set()
    for candidate_prefix in (prefix, *legacy_prefixes):
        for path in directory.glob(f"{candidate_prefix}-*.json"):
            resolved = str(path)
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(path)

    def modified_time(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(paths, key=modified_time, reverse=True)


def write_deduplicated_snapshot(
    directory,
    prefix,
    text,
    limit=10,
    legacy_prefixes=(),
):
    """Write one snapshot only when it differs from the latest valid snapshot.

    Returns the new path when a snapshot is created, otherwise ``None``.
    Invalid older files are ignored for comparison but can still be removed by
    the retention limit.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    new_signature = canonical_json_signature(text)

    for path in list_config_snapshots(directory, prefix, legacy_prefixes):
        try:
            latest_signature = canonical_json_signature(
                path.read_text("utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if latest_signature == new_signature:
            return None
        break

    stamp = time.strftime("%Y%m%d-%H%M%S")
    snapshot = directory / (
        f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}.json"
    )
    atomic_write_text(snapshot, text)

    paths = list_config_snapshots(directory, prefix, legacy_prefixes)
    for stale in paths[max(1, int(limit)):]:
        try:
            stale.unlink()
        except OSError:
            pass
    return snapshot


def load_valid_snapshot(
    directory,
    prefix,
    validator,
    legacy_prefixes=(),
    exclude_signature=None,
    max_bytes=None,
):
    """Load the newest valid snapshot, optionally excluding one config state."""
    for path in list_config_snapshots(directory, prefix, legacy_prefixes):
        try:
            if max_bytes is not None and path.stat().st_size > int(max_bytes):
                continue
            raw = json.loads(path.read_text("utf-8"))
            data = validator(raw)
            signature = canonical_json_signature(data)
            if exclude_signature is not None and signature == exclude_signature:
                continue
            return data, path
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            continue
    return None, None
