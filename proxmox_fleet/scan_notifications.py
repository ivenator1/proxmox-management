"""Manual-update scan state and fleet-briefing notification selection.

``scan.py`` refreshes the normalized manual mappings and records actionable
results here without dispatching anything. The dashboard continues to read the
pending snapshot. During the next fleet run, Phase 4 selects first/change/daily
reminder entries from this state and includes them in the ordinary fleet
briefing. Manual systems therefore remain scan-only and never affect
``FleetState`` totals, failure status, or the process exit code.

Selection contract
------------------
Each entry is classified exactly once:

- **unreachable** → skipped entirely; its persisted state is left untouched.
- **genuine check error** (``error`` set, not unreachable) → records attention
  for first / change / reminder selection.
- **pending** (``update_available`` or ``reboot_required``) → records attention
  for first / change / reminder selection.
- **current** (nothing to report) → clears that host's own state entry.

A host not observed in this scan keeps its state (limited/``--limit`` scans
must not wipe hosts they never looked at). State writes are best-effort: a
failed write never fails the scan.

Reminder semantics
------------------
Per actionable host, state retains the latest result and fingerprint plus the
last-notified fingerprint/timestamp. A fleet run selects a host when it has
never been notified (first), its current fingerprint differs (change), or
``now >= last_notified + reminder_hours`` (reminder, exact boundary included).
``last_notified`` advances only after the combined fleet dispatch attempt.
Manual check errors remain presentation-only attention and never become a fleet
failure. The subsection has no trailing newline and is capped at 4000 chars.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

_BODY_LIMIT = 4000
_STATE_FILE = "manual-notify-state.json"
_STATE_VERSION = 2
# Default gap between notifications for an unchanged host.
_REMINDER_HOURS_DEFAULT = 24.0


@dataclass
class ManualDecision:
    """Pure manual-attention selection outcome — no side effects or I/O.

    ``new_state`` is the state to persist *after* the combined fleet dispatch
    attempt: selected hosts get ``last_notified`` advanced to *now* and all
    other recorded hosts remain untouched.
    """

    notify: bool
    """Whether anything was selected and a dispatch attempt is warranted."""
    failed: bool
    """True iff a check error is selected; classification only, not fleet failure."""
    body: str
    """Rendered notification body (<=4000 chars, no trailing newline)."""
    selected: List[str]
    """Host keys selected for notification, in entry order."""
    reasons: Dict[str, str]
    """Per-host selection reason: ``first``/``change``/``reminder``/``force``."""
    pending_count: int
    """Selected hosts with a pending update/reboot (warning section)."""
    error_count: int
    """Selected hosts with a genuine check error (failure section)."""
    new_state: Dict[str, Dict[str, Any]]
    """State to persist after the dispatch attempt (see class docstring)."""


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------

def _norm(value: Any) -> str:
    """Collapse whitespace and strip — transient formatting never survives."""
    return " ".join(str(value if value is not None else "").split())


def _entry_details(entry: Mapping[str, Any]) -> List[str]:
    """Normalize the result's details list while tolerating legacy strings."""
    value = entry.get("details")
    if isinstance(value, (list, tuple)):
        return [detail for item in value if (detail := _norm(item))]
    detail = _norm(value)
    return [detail] if detail else []


def _entry_detail_lines(entry: Mapping[str, Any]) -> List[str]:
    """Return physical detail lines so every line can be nested in Markdown."""
    value = entry.get("details")
    values = value if isinstance(value, (list, tuple)) else [value]
    lines: List[str] = []
    for item in values:
        for raw_line in str(item if item is not None else "").splitlines():
            if line := _norm(raw_line):
                lines.append(line)
    return lines


def fingerprint(entry: Mapping[str, Any]) -> str:
    """Pure, deterministic semantic fingerprint of one manual mapping entry.

    Hashes the fields that carry notification meaning — ``adapter``,
    ``current``, ``latest``, ``update_available``, ``reboot_required``,
    ``summary``, ``details`` — plus the genuine-check ``error`` text: a changed
    error message is a changed notification (the error first/change/reminder
    contract needs the error inside the hash). Whitespace/newlines are
    normalized away and no timestamp participates, so the same semantic state
    always yields the same fingerprint. Display-only guidance (``apply_hint``)
    and transport flags (``unreachable``) are excluded.
    """
    parts = [
        _norm(entry.get("adapter")),
        _norm(entry.get("current")),
        _norm(entry.get("latest")),
        "1" if entry.get("update_available") else "0",
        "1" if entry.get("reboot_required") else "0",
        _norm(entry.get("summary")),
        "\x1e".join(_entry_details(entry)),
        _norm(entry.get("error")),
    ]
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Classification helpers
# --------------------------------------------------------------------------

def _key(entry: Mapping[str, Any]) -> str:
    """Stable inventory-host identity, with an adapter fallback for legacy data."""
    key = str(entry.get("host") or entry.get("adapter") or "").strip()
    return key or "?"


def _label(entry: Mapping[str, Any]) -> str:
    """Human label that retains the inventory identity when it differs."""
    host = _key(entry)
    display = _norm(entry.get("display_name"))
    return f"{display} ({host})" if display and display != host else host


def _has_error(entry: Mapping[str, Any]) -> bool:
    return bool(entry.get("error"))


def _is_pending(entry: Mapping[str, Any]) -> bool:
    return bool(entry.get("update_available") or entry.get("reboot_required"))


def _entry_category(entry: Mapping[str, Any]) -> str:
    """Classify one observed host for the notification state machine."""
    if entry.get("unreachable"):
        return "unreachable"
    if _has_error(entry):
        return "error"
    if _is_pending(entry):
        return "pending"
    return "clean"


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def _as_utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a persisted timestamp; unparseable values come back as None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        # Python 3.9's fromisoformat rejects the "Z" suffix, so widen it.
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------
# Body rendering
# --------------------------------------------------------------------------

def _trim_body(body: str, limit: int) -> str:
    """Enforce the notifier-safe body: no trailing whitespace, <= limit chars."""
    body = body.strip()
    if len(body) <= limit:
        return body
    return body[: limit - 3].rstrip() + "..."


def render_body(
    pending: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]],
    *,
    limit: int = _BODY_LIMIT,
) -> str:
    """Render due manual entries as one node-like fleet subsection.

    Category, host, detail, and apply-hint lines are nested at successively
    deeper indentation levels. Returns <= *limit* chars with no trailing
    newline.
    """
    if not pending and not errors:
        return ""
    lines = ["**Manual Systems: (ATTENTION REQUIRED)**"]
    if pending:
        lines.append("- Updates / reboots")
        for entry in pending:
            current = _norm(entry.get("current"))
            latest = _norm(entry.get("latest"))
            arrow = f"{current} → {latest}" if (current or latest) else "update available"
            reboot = " (reboot required)" if entry.get("reboot_required") else ""
            lines.append(f"  - **{_label(entry)}** — {arrow}{reboot}")
            for detail in _entry_detail_lines(entry):
                lines.append(f"    - {detail}")
            hint = _norm(entry.get("apply_hint"))
            if hint:
                lines.append(f"    - GUI apply: {hint}")
    if errors:
        lines.append("- Check errors")
        for entry in errors:
            err = _norm(entry.get("error")) or "check failed"
            lines.append(f"  - **{_label(entry)}** — {err}")
    return _trim_body("\n".join(lines), limit)


# --------------------------------------------------------------------------
# Persisted state
# --------------------------------------------------------------------------

def _state_path(history_dir: Union[str, Path]) -> Path:
    return Path(history_dir) / _STATE_FILE


def load_state(history_dir: Union[str, Path]) -> Dict[str, Dict[str, Any]]:
    """Read the persisted per-host notification state, defensively.

    A missing, unreadable, or structurally corrupt file comes back as an empty
    state — a bad state file must never fail a scan. Entries without a
    ``fingerprint`` are dropped (nothing to compare against).
    """
    path = _state_path(history_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in hosts.items():
        if isinstance(value, dict) and "fingerprint" in value:
            out[str(key)] = dict(value)
    return out


def save_state(
    history_dir: Union[str, Path],
    state: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Write the per-host notification state; never raises (returns success).

    Best-effort by design — persistence is auxiliary, so a write failure is
    reported via the return value and never propagates into the scan.
    """
    path = _state_path(history_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _STATE_VERSION, "hosts": dict(state)}
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=4, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)
        return True
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------
# Scan recording and fleet-run selection
# --------------------------------------------------------------------------

def record_manual_results(
    entries: Sequence[Mapping[str, Any]],
    *,
    history_dir: Union[str, Path],
) -> bool:
    """Refresh actionable manual state without sending a notification.

    Pending/error entries replace their latest observed payload while retaining
    the last fleet-notification markers. Clean entries are removed. Unreachable
    and absent hosts remain untouched so transient or limited scans cannot wipe
    known state. Returns whether the resulting state is safely persisted.
    """
    state = load_state(history_dir)
    new_state: Dict[str, Dict[str, Any]] = {key: dict(value) for key, value in state.items()}
    for entry in entries:
        host = _key(entry)
        category = _entry_category(entry)
        if category == "unreachable":
            continue
        if category == "clean":
            new_state.pop(host, None)
            continue

        fp = fingerprint(entry)
        previous = new_state.get(host, {})
        record: Dict[str, Any] = {
            "fingerprint": fp,
            "entry": dict(entry),
        }
        last_notified = previous.get("last_notified")
        if last_notified:
            record["last_notified"] = last_notified
        notified_fingerprint = previous.get("notified_fingerprint")
        if notified_fingerprint is None and last_notified:
            # Version-1 state stored the last-notified fingerprint directly.
            notified_fingerprint = previous.get("fingerprint")
        if notified_fingerprint:
            record["notified_fingerprint"] = notified_fingerprint
        new_state[host] = record

    return (new_state == state) or save_state(history_dir, new_state)


def decide_stored_notifications(
    state: Mapping[str, Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    reminder_hours: float = _REMINDER_HOURS_DEFAULT,
    force: bool = False,
) -> ManualDecision:
    """Select due recorded entries for the next fleet-run briefing.

    The returned ``new_state`` is the post-dispatch-attempt state. Callers must
    persist it only after the ordinary fleet notification has been attempted.
    """
    now_utc = _as_utc(now)
    new_state: Dict[str, Dict[str, Any]] = {key: dict(value) for key, value in state.items()}
    selected: List[str] = []
    reasons: Dict[str, str] = {}
    pending_selected: List[Mapping[str, Any]] = []
    error_selected: List[Mapping[str, Any]] = []

    for host in sorted(state):
        stored = state[host]
        entry = stored.get("entry")
        if not isinstance(entry, Mapping):
            continue
        category = _entry_category(entry)
        if category not in {"pending", "error"}:
            continue

        fp = str(stored.get("fingerprint") or fingerprint(entry))
        notified_fp = stored.get("notified_fingerprint")
        last_notified = _parse_ts(stored.get("last_notified"))
        if force:
            reason: Optional[str] = "force"
        elif not notified_fp or last_notified is None:
            reason = "first"
        elif notified_fp != fp:
            reason = "change"
        elif now_utc >= last_notified + timedelta(hours=reminder_hours):
            reason = "reminder"
        else:
            reason = None
        if reason is None:
            continue

        selected.append(host)
        reasons[host] = reason
        if category == "error":
            error_selected.append(entry)
        else:
            pending_selected.append(entry)
        updated = dict(stored)
        updated["fingerprint"] = fp
        updated["notified_fingerprint"] = fp
        updated["last_notified"] = _to_iso(now_utc)
        new_state[host] = updated

    return ManualDecision(
        notify=bool(selected),
        failed=bool(error_selected),
        body=render_body(pending_selected, error_selected),
        selected=selected,
        reasons=reasons,
        pending_count=len(pending_selected),
        error_count=len(error_selected),
        new_state=new_state,
    )
