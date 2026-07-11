import json
from pathlib import Path

from config.storage import (
    canonical_json_signature,
    list_config_snapshots,
    load_valid_snapshot,
    write_deduplicated_snapshot,
)


def _validator(value):
    if not isinstance(value, dict) or "value" not in value:
        raise ValueError("invalid")
    return value


def test_snapshot_deduplicates_semantically_identical_json(tmp_path):
    first = write_deduplicated_snapshot(
        tmp_path, "saved", json.dumps({"value": 1}, indent=2), limit=10
    )
    second = write_deduplicated_snapshot(
        tmp_path, "saved", '{"value":1}', limit=10
    )
    assert first is not None
    assert second is None
    assert len(list_config_snapshots(tmp_path, "saved")) == 1


def test_saved_and_applied_snapshots_are_independent(tmp_path):
    write_deduplicated_snapshot(
        tmp_path, "saved", '{"value":1}', limit=10
    )
    write_deduplicated_snapshot(
        tmp_path, "applied", '{"value":2}', limit=10
    )
    saved, _ = load_valid_snapshot(tmp_path, "saved", _validator)
    applied, _ = load_valid_snapshot(tmp_path, "applied", _validator)
    assert saved == {"value": 1}
    assert applied == {"value": 2}


def test_failed_or_saved_only_update_does_not_replace_applied_snapshot(tmp_path):
    write_deduplicated_snapshot(
        tmp_path, "applied", '{"value":1}', limit=10
    )
    write_deduplicated_snapshot(
        tmp_path, "saved", '{"value":2}', limit=10
    )
    applied, _ = load_valid_snapshot(tmp_path, "applied", _validator)
    assert applied == {"value": 1}


def test_load_can_exclude_current_applied_snapshot(tmp_path):
    write_deduplicated_snapshot(
        tmp_path, "applied", '{"value":1}', limit=10
    )
    write_deduplicated_snapshot(
        tmp_path, "applied", '{"value":2}', limit=10
    )
    current = canonical_json_signature({"value": 2})
    previous, _ = load_valid_snapshot(
        tmp_path, "applied", _validator, exclude_signature=current
    )
    assert previous == {"value": 1}


def test_retention_limit_is_per_snapshot_kind(tmp_path):
    for value in range(12):
        write_deduplicated_snapshot(
            tmp_path, "saved", json.dumps({"value": value}), limit=10
        )
    assert len(list_config_snapshots(tmp_path, "saved")) == 10
