"""Manual-mapping scan notifications — reminder-gated alerting for the pending
scan's *manual* entries (hosts whose updates must be applied by hand through
the GUI, plus genuine check errors).

``scan.py`` assembles the normalized manual mappings (one dict per host with
``adapter``/``current``/``latest``/``update_available``/``reboot_required``/
``summary``/``details``/``apply_hint``/``unreachable``/``error``), then calls
:func:`run_manual_notifications` once — that helper loads the persisted
per-host state, decides what needs notifying, dispatches through the existing
``notifiers.dispatch`` fan-out, and persists the post-attempt state. The pure
primitives (:func:`decide_manual_notifications`, :func:`fingerprint`,
:func:`render_body`, :func:`load_state`/:func:`save_state`) are exported for
tests and for integrations that want to control dispatch themselves.

Selection contract
------------------
Each entry is classified exactly once:

- **unreachable** → skipped entirely; its persisted state is left untouched.
- **genuine check error** (``error`` set, not unreachable) → notifies on
  first / change / reminder with failure severity.
- **pending** (``update_available`` or ``reboot_required``) → notifies on
  first / change / reminder with warning severity.
- **current** (nothing to report) → clears that host's own state entry.

A host not observed in this scan keeps its state (limited/``--limit`` scans
must not wipe hosts they never looked at). State writes are best-effort: a
failed write never fails the scan.

Reminder semantics
------------------
Per host, the state entry is ``{"fingerprint": ..., "last_notified": ...}``.
A host notifies when it has no entry (first), when its
:func:`fingerprint` differs from the stored one (change), or when
``now >= last_notified + reminder_hours`` (reminder, exact boundary included).
``last_notified`` advances to *now* after a dispatch attempt — the scan
integration persists :attr:`ManualDecision.new_state` after it dispatches (or
:func:`run_manual_notifications` does so itself), so a failed dispatch never
spams the next scan.

Severity metadata
-----------------
:attr:`ManualDecision.failed` is True exactly when at least one genuine check
error is selected for this notification, so ``scan.py`` can choose
:func:`scan_title`/:func:`scan_color` (failure red vs warning amber) and its
own failure bookkeeping from one flag. The body never carries a trailing
newline and is capped at 4000 chars.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from proxmox_fleet import notifiers

# Discord embed colours — failure red matches the update briefing; amber
# signals "manual updates available, no check errors".
_COLOR_FAILED = 15158332
_COLOR_WARNING = 16766720

_BODY_LIMIT = 4000
_STATE_FILE = "manual-notify-state.json"
_STATE_VERSION = 1
# Default gap between notifications for an unchanged host.
_REMINDER_HOURS_DEFAULT = 24.0


@dataclass
class ManualDecision:
    """Pure selection outcome for one scan — no side effects, no I/O.

    ``new_state`` is the state to persist *after* a dispatch attempt: selected
    hosts get ``last_notified`` advanced to *now*, currently-observed clean
    hosts are removed, and absent/unreachable hosts are left untouched.
    """

    notify: bool
    """Whether anything was selected and a dispatch attempt is warranted."""
    failed: bool
    """True iff at least one genuine check error is selected (failure severity)."""
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


@dataclass
class ManualNotifyReport:
    """Outcome of the one-call :func:`run_manual_notifications` integration."""

    notify: bool
    failed: bool
    body: str
    title: str
    ntfy_title: str
    color: int
    selected: List[str]
    reasons: Dict[str, str]
    dispatched: bool
    """Whether a dispatch attempt was made (True iff ``notify``)."""
    state_saved: bool
    """Whether the persisted state on disk reflects ``new_state`` after the run."""
    state_path: Path
    """Where ``manual-notify-state.json`` lives (or would live)."""


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------

def _norm(value: Any) -> str:
    """Collapse whitespace and strip — transient formatting never survives."""
    return " ".join(str(value if value is not None else "").split())


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
        _norm(entry.get("details")),
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
    """The per-host identity of an entry — its ``adapter``."""
    key = str(entry.get("adapter") or "").strip()
    return key or "?"


def _has_error(entry: Mapping[str, Any]) -> bool:
    return bool(entry.get("error"))


def _is_pending(entry: Mapping[str, Any]) -> bool:
    return bool(entry.get("update_available") or entry.get("reboot_required"))


def _notify_reason(
    stored: Optional[Mapping[str, Any]],
    fp: str,
    now: datetime,
    reminder_hours: float,
    force: bool,
) -> Optional[str]:
    """Why this host should notify now, or None to stay silent."""
    if force:
        return "force"
    if stored is None:
        return "first"
    if stored.get("fingerprint") != fp:
        return "change"
    last = _parse_ts(stored.get("last_notified"))
    if last is None or now >= last + timedelta(hours=reminder_hours):
        return "reminder"
    return None


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
    """Render the notification body: ``Manual updates required`` then, only
    when present, a separate ``Manual check errors`` section.

    Per pending host: ``host — current → latest`` (``(reboot required)`` when
    a reboot is pending), the details line, and the GUI apply hint. Per error
    host: ``host — <error>``. Returns <= *limit* chars with no trailing
    newline (truncation is at a word/whitespace boundary and ends with ``...``).
    """
    sections: List[str] = []
    if pending:
        lines = ["**Manual updates required**"]
        for entry in pending:
            host = _key(entry)
            current = _norm(entry.get("current"))
            latest = _norm(entry.get("latest"))
            arrow = f"{current} → {latest}" if (current or latest) else "update available"
            reboot = " (reboot required)" if entry.get("reboot_required") else ""
            lines.append(f"- **{host}** — {arrow}{reboot}")
            details = _norm(entry.get("details"))
            if details:
                lines.append(f"  {details}")
            hint = _norm(entry.get("apply_hint"))
            if hint:
                lines.append(f"  GUI apply: {hint}")
        sections.append("\n".join(lines))
    if errors:
        lines = ["**Manual check errors**"]
        for entry in errors:
            host = _key(entry)
            err = _norm(entry.get("error")) or "check failed"
            lines.append(f"- **{host}** — {err}")
        sections.append("\n".join(lines))
    return _trim_body("\n\n".join(sections), limit)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def decide_manual_notifications(
    entries: Sequence[Mapping[str, Any]],
    state: Mapping[str, Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    reminder_hours: float = _REMINDER_HOURS_DEFAULT,
    force: bool = False,
) -> ManualDecision:
    """Pure selection: which hosts notify now, and the state to persist after.

    *entries* are the normalized manual mappings from this scan; *state* is the
    persisted per-host ``{fingerprint, last_notified}`` map (see
    :func:`load_state`). Unreachable hosts are skipped untouched; error hosts
    notify on first/change/reminder with failure severity; pending
    update/reboot hosts notify on first/change/reminder; clean hosts clear
    their own entry. Hosts absent from *entries* keep their state (limited
    scans never wipe unobserved hosts).

    *now* defaults to the current UTC time and is injected for deterministic
    tests. ``force=True`` notifies every error/pending host regardless of the
    reminder window (mirrors ``settings.force_notify``).
    """
    now_utc = _as_utc(now)
    new_state: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in state.items()}
    selected: List[str] = []
    reasons: Dict[str, str] = {}
    pending_selected: List[Mapping[str, Any]] = []
    error_selected: List[Mapping[str, Any]] = []

    for entry in entries:
        host = _key(entry)
        if entry.get("unreachable"):
            # Could not look — never a notification, and never a state change.
            continue
        if _has_error(entry):
            fp = fingerprint(entry)
            reason = _notify_reason(new_state.get(host), fp, now_utc, reminder_hours, force)
            if reason is not None:
                selected.append(host)
                reasons[host] = reason
                error_selected.append(entry)
                new_state[host] = {"fingerprint": fp, "last_notified": _to_iso(now_utc)}
        elif _is_pending(entry):
            fp = fingerprint(entry)
            reason = _notify_reason(new_state.get(host), fp, now_utc, reminder_hours, force)
            if reason is not None:
                selected.append(host)
                reasons[host] = reason
                pending_selected.append(entry)
                new_state[host] = {"fingerprint": fp, "last_notified": _to_iso(now_utc)}
        else:
            # Currently clean — this host's own state entry is cleared.
            new_state.pop(host, None)

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


# --------------------------------------------------------------------------
# Title/colour metadata for scan.py
# --------------------------------------------------------------------------

def scan_title(failed: bool) -> str:
    """Discord embed title — failure red when check errors were selected."""
    return "❌ Scan: Manual Check Errors" if failed else "⚠️ Scan: Manual Updates Available"


def scan_ntfy_title(failed: bool) -> str:
    """ASCII-safe ntfy 'Title' header — mirrors :func:`scan_title`."""
    return "Fleet Scan: Manual Check Errors" if failed else "Fleet Scan: Manual Updates Available"


def scan_color(failed: bool) -> int:
    """Discord embed colour — red on check errors, amber for updates only."""
    return _COLOR_FAILED if failed else _COLOR_WARNING


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
        path.write_text(
            json.dumps(payload, indent=4, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------
# One-call integration
# --------------------------------------------------------------------------

def run_manual_notifications(
    entries: Sequence[Mapping[str, Any]],
    *,
    history_dir: Union[str, Path],
    notifiers_list: Optional[Sequence[Mapping[str, Any]]] = None,
    now: Optional[datetime] = None,
    reminder_hours: float = _REMINDER_HOURS_DEFAULT,
    force: bool = False,
    retries: int = 15,
    dispatch_fn: Callable[..., None] = notifiers.dispatch,
) -> ManualNotifyReport:
    """One-call integration for ``scan.py``: load → decide → dispatch → persist.

    Loads the persisted state from *history_dir*, runs
    :func:`decide_manual_notifications`, and when anything was selected makes a
    dispatch attempt through *dispatch_fn* (default ``notifiers.dispatch``,
    which swallows per-notifier failures — a failed dispatch never aborts the
    scan). Immediately after the attempt the post-attempt state is persisted:
    selected hosts' ``last_notified`` is advanced, currently-observed clean
    hosts are cleared, and absent/unreachable hosts are untouched (limited
    scans never wipe unobserved hosts). State writes are best-effort and
    reported via :attr:`ManualNotifyReport.state_saved`.

    *notifiers_list* is the resolved notifier list (``notifiers.resolve_notifiers``
    output); ``None``/empty still counts as an attempt (zero targets) so the
    reminder window still advances. *now* defaults to the current UTC time and
    is injectable for deterministic tests; *force* mirrors
    ``settings.force_notify``.
    """
    state = load_state(history_dir)
    decision = decide_manual_notifications(
        entries,
        state,
        now=now,
        reminder_hours=reminder_hours,
        force=force,
    )
    if decision.notify:
        # Dispatch attempt — advance last_notified right after, so a failure
        # here does not cause a re-notification on the next scan.
        dispatch_fn(
            list(notifiers_list) if notifiers_list is not None else [],
            title=scan_title(decision.failed),
            ntfy_title=scan_ntfy_title(decision.failed),
            body=decision.body,
            color=scan_color(decision.failed),
            failed=decision.failed,
            retries=retries,
        )
    changed = decision.new_state != state
    state_saved = (not changed) or save_state(history_dir, decision.new_state)
    return ManualNotifyReport(
        notify=decision.notify,
        failed=decision.failed,
        body=decision.body,
        title=scan_title(decision.failed),
        ntfy_title=scan_ntfy_title(decision.failed),
        color=scan_color(decision.failed),
        selected=decision.selected,
        reasons=decision.reasons,
        dispatched=decision.notify,
        state_saved=state_saved,
        state_path=_state_path(history_dir),
    )
