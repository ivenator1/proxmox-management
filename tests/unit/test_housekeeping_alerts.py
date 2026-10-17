"""Run-scoped housekeeping alert streaks and persistence failure behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proxmox_fleet import housekeeping_alerts


def _path(history_dir: Path) -> Path:
    return history_dir / "housekeeping-alert-state.json"


def _count(history_dir: Path) -> int:
    payload = json.loads(_path(history_dir).read_text(encoding="utf-8"))
    assert set(payload) == {"version", "consecutive_failures"}
    assert payload["version"] == 1
    return int(payload["consecutive_failures"])


def _failure(history_dir: Path) -> housekeeping_alerts.AlertDecision:
    return housekeeping_alerts.record_run(history_dir, failed=True, attempted=True, dry_run=False)


def test_counts_completed_runs_and_alerts_from_twelfth_failure(tmp_path: Path) -> None:
    history_dir = tmp_path / "new-history"
    for count in range(1, 15):
        decision = _failure(history_dir)
        assert decision.allow_automatic is (count >= 12)
        assert decision.warning is None
        assert _count(history_dir) == count
    assert not _path(history_dir).with_suffix(".json.tmp").exists()


def test_failure_before_maintenance_attempt_still_counts(tmp_path: Path) -> None:
    decision = housekeeping_alerts.record_run(tmp_path, failed=True, attempted=False, dry_run=False)
    assert not decision.allow_automatic
    assert _count(tmp_path) == 1


def test_actual_success_resets_streak_and_next_failure_starts_at_one(tmp_path: Path) -> None:
    for _ in range(12):
        _failure(tmp_path)
    decision = housekeeping_alerts.record_run(tmp_path, failed=False, attempted=True, dry_run=False)
    assert not decision.allow_automatic
    assert decision.warning is None
    assert _count(tmp_path) == 0
    assert not _failure(tmp_path).allow_automatic
    assert _count(tmp_path) == 1


def test_skipped_success_preserves_existing_streak(tmp_path: Path) -> None:
    for _ in range(11):
        _failure(tmp_path)
    before = _path(tmp_path).read_bytes()
    decision = housekeeping_alerts.record_run(tmp_path, failed=False, attempted=False, dry_run=False)
    assert not decision.allow_automatic
    assert decision.warning is None
    assert _path(tmp_path).read_bytes() == before
    assert _failure(tmp_path).allow_automatic


@pytest.mark.parametrize("failed", [False, True])
def test_dry_runs_preserve_streak_without_automatic_alerts(tmp_path: Path, failed: bool) -> None:
    for _ in range(12):
        _failure(tmp_path)
    before = _path(tmp_path).read_bytes()
    decision = housekeeping_alerts.record_run(tmp_path, failed=failed, attempted=True, dry_run=True)
    assert not decision.allow_automatic
    assert decision.warning is None
    assert _path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("dry_run,attempted", [(True, True), (False, False)])
def test_non_attempts_do_not_create_history_directory(tmp_path: Path, dry_run: bool, attempted: bool) -> None:
    history_dir = tmp_path / "absent"
    housekeeping_alerts.record_run(history_dir, failed=False, attempted=attempted, dry_run=dry_run)
    assert not history_dir.exists()


@pytest.mark.parametrize("payload", [
    "[" * 10000,
    "not json",
    "[]",
    "null",
    '{}',
    '{"version": 2, "consecutive_failures": 11}',
    '{"version": true, "consecutive_failures": 11}',
    '{"version": 1, "consecutive_failures": -1}',
    '{"version": 1, "consecutive_failures": true}',
    '{"version": 1, "consecutive_failures": 11.0}',
    '{"version": 1, "consecutive_failures": "11"}',
])
def test_corrupt_state_fails_open_without_overwriting_evidence(tmp_path: Path, payload: str) -> None:
    _path(tmp_path).write_text(payload, encoding="utf-8")
    for _ in range(2):
        decision = _failure(tmp_path)
        assert decision.allow_automatic
        assert decision.warning is not None
        assert str(tmp_path) not in decision.warning
        assert _path(tmp_path).read_text(encoding="utf-8") == payload


def test_invalid_utf8_fails_open(tmp_path: Path) -> None:
    _path(tmp_path).write_bytes(b"\xff")
    decision = _failure(tmp_path)
    assert decision.allow_automatic
    assert decision.warning is not None


def test_actual_success_repairs_corrupt_state(tmp_path: Path) -> None:
    _path(tmp_path).write_text("corrupt", encoding="utf-8")
    decision = housekeeping_alerts.record_run(tmp_path, failed=False, attempted=True, dry_run=False)
    assert not decision.allow_automatic
    assert decision.warning is None
    assert _count(tmp_path) == 0


def test_unreadable_state_fails_open_with_safe_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> str:
        raise PermissionError("secret/token and private/path")

    monkeypatch.setattr(Path, "read_text", denied)
    decision = _failure(tmp_path)
    assert decision.allow_automatic
    assert decision.warning is not None
    assert "secret" not in decision.warning
    assert "private/path" not in decision.warning


@pytest.mark.parametrize("failed", [False, True])
def test_save_error_never_raises_and_current_failure_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool,
) -> None:
    _failure(tmp_path)
    before = _path(tmp_path).read_bytes()

    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("secret/token and private/path")

    monkeypatch.setattr(Path, "replace", denied)
    decision = housekeeping_alerts.record_run(tmp_path, failed=failed, attempted=True, dry_run=False)
    assert decision.allow_automatic is failed
    assert decision.warning is not None
    assert "secret" not in decision.warning
    assert "private/path" not in decision.warning
    assert _path(tmp_path).read_bytes() == before


def test_history_directory_error_fails_open(tmp_path: Path) -> None:
    history_dir = tmp_path / "not-a-directory"
    history_dir.write_text("occupied", encoding="utf-8")
    decision = _failure(history_dir)
    assert decision.allow_automatic
    assert decision.warning is not None
