"""Briefing render — the Python port of ``templates/discord_briefing.j2``.

BYTE-FOR-BYTE PARITY with the Jinja template is non-negotiable: the same
``\\n``/``\\n\\n`` separators, ``**bold**``, ``*(no snap)*``, ``— `` em-dash,
section order, the ``OS:``/``'None'`` guard, and the idle
``*No container changes.*`` line. A golden test in ``tests/unit/test_briefing.py``
renders both this module and the surviving Jinja shim over a shared fixture set
and asserts they are identical.

``render_briefing()`` reproduces the raw template output (including the leading/
trailing newlines the template emits). ``prepare_body()`` applies the driver's
``| trim | truncate(4000, False, '\\n...')`` post-processing — the string actually
handed to the notifier embed and recorded in the run history.
"""

from __future__ import annotations

from proxmox_fleet.models.state import (
    DiskUsageEntry,
    DiskUsageInsight,
    FleetState,
    WarningEntry,
)

# Discord embed colours — kept identical to fleet-update.yml Phase 4.
_COLOR_FAILED = 15158332
_COLOR_WARNING = 16766720
_COLOR_OK = 3066993

# Budgets for the structured disk-usage enrichment. The enriched warning keeps
# the original message prefix and stays compact; the priority block stays well
# inside the overall 4000-character body budget even behind a long error log.
_DISK_USAGE_WARNING_LIMIT = 512
_DISK_USAGE_MAX_ENTRIES = 3
_DISK_DISPLAY_PATH_LIMIT = 80
_DISK_DETAIL_DISPLAY_LIMIT = 120
_DISK_PRIORITY_BLOCK_LIMIT = 1600


def human_bytes(value: int) -> str:
    """Render non-negative byte totals with binary units for reports and UI."""
    value = max(0, value)
    if value < 1024:
        return f"{value} B"
    size = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        size /= 1024
        if size < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}"
    raise AssertionError("unreachable byte unit")


def _sanitize_display_text(value: str) -> str:
    """Make a metadata string safe for a single-line Markdown warning.

    Backticks are replaced with apostrophes so an embedded path cannot escape
    its code span; control/format characters become spaces. Metadata itself is
    never mutated — only this rendered copy.
    """
    chars: list[str] = []
    for char in value:
        if char == "`":
            chars.append("'")
        elif char.isprintable():
            chars.append(char)
        else:
            chars.append(" ")
    return " ".join("".join(chars).split())


def _bounded_display(value: str, limit: int) -> str:
    """Sanitize and clip a display string to ``limit`` characters."""
    text = _sanitize_display_text(value)
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _format_share_percent(value: float) -> str:
    rounded = round(float(value), 1)
    if rounded == int(rounded):
        return f"{int(rounded)}%"
    return f"{rounded:.1f}%"


def _disk_entry_label(entry: DiskUsageEntry) -> str:
    if entry.kind == "root-files":
        return "files directly under /"
    path = _bounded_display(entry.path, _DISK_DISPLAY_PATH_LIMIT)
    if entry.kind == "directory-other":
        return f"other contents under {path}"
    return path


def _disk_entry_clause(entry: DiskUsageEntry) -> str:
    return (
        f"`{_disk_entry_label(entry)}`: {human_bytes(entry.allocated_bytes)} "
        f"({_format_share_percent(entry.share_percent)} of used; "
        f"limit {_format_share_percent(entry.max_expected_share_percent)})"
    )


def _disk_omitted_clause(count: int) -> str:
    if count == 1:
        return "1 more oversized directory not shown"
    return f"{count} more oversized directories not shown"


def _disk_status_clause(insight: DiskUsageInsight) -> str:
    """One bounded clause explaining a non-list outcome (never a second warning)."""
    if insight.status == "unavailable":
        return "breakdown unavailable"
    if insight.status == "partial":
        if insight.entries:
            return "breakdown incomplete; more may be oversized"
        return "breakdown incomplete; oversized directories may be missing"
    if not insight.entries:
        return "no dominant directory on the used space"
    return ""


def format_disk_usage_warning(base: str, insight: DiskUsageInsight) -> str:
    """Append compact oversized consumers to an existing disk warning.

    The original message prefix is preserved verbatim. At most three entries are
    shown; the complete warning is bounded to 512 characters without a trailing
    newline. Full paths/bytes remain in the structured ``DiskUsageInsight``;
    only this display copy sanitizes control characters/backticks and clips
    paths.
    """
    considered = [] if insight.status == "unavailable" else list(insight.entries)
    entries = considered[:_DISK_USAGE_MAX_ENTRIES]
    mandatory = _disk_status_clause(insight)
    detail = (
        _bounded_display(insight.detail, _DISK_DETAIL_DISPLAY_LIMIT)
        if insight.detail
        else ""
    )
    if insight.status == "unavailable" and detail:
        mandatory = f"breakdown unavailable: {detail}"
        detail = ""

    def compose(shown: list[DiskUsageEntry], *, with_detail: bool, with_omitted: bool) -> str:
        clauses = [_disk_entry_clause(entry) for entry in shown]
        if mandatory:
            clauses.append(mandatory)
        if with_detail and detail:
            clauses.append(detail)
        hidden = insight.omitted + len(considered) - len(shown)
        if with_omitted and insight.status != "unavailable" and hidden > 0:
            clauses.append(_disk_omitted_clause(hidden))
        return base + "".join(f"; {clause}" for clause in clauses)

    shown = list(entries)
    while True:
        for with_detail, with_omitted in ((True, True), (True, False), (False, False)):
            candidate = compose(shown, with_detail=with_detail, with_omitted=with_omitted)
            if len(candidate) <= _DISK_USAGE_WARNING_LIMIT:
                return candidate
        if not shown:
            break
        shown.pop()

    # Even a status-only suffix can exceed the budget behind an unusually long
    # legacy prefix; the prefix wins because it is the original warning text.
    return base


def _disk_priority_omitted_line(count: int) -> str:
    noun = "warning" if count == 1 else "warnings"
    return f"\n- *{count} more disk {noun} in full history.*"


def _render_disk_priority_block(warnings: list[WarningEntry]) -> str:
    """Render the leading, bounded disk-space block for enriched warnings.

    The block is placed before node/error sections so a long error log cannot
    hide the disk insight behind the overall 4000-character truncation. Full
    warnings stay in state/history; only the block's display is bounded.
    """
    if not warnings:
        return ""
    header = "**⚠️ Disk space**"
    lines: list[str] = []
    used = len(header)
    total = len(warnings)
    for index, warning in enumerate(warnings):
        line = f"\n- **{warning.host}** at `{warning.task}`: {warning.warning}"
        remaining_after = total - (index + 1)
        reserve = len(_disk_priority_omitted_line(remaining_after)) if remaining_after else 0
        if used + len(line) + reserve > _DISK_PRIORITY_BLOCK_LIMIT:
            break
        lines.append(line)
        used += len(line)
    omitted = total - len(lines)
    if omitted:
        suffix = _disk_priority_omitted_line(omitted)
        while lines and used + len(suffix) > _DISK_PRIORITY_BLOCK_LIMIT:
            used -= len(lines.pop())
            omitted = total - len(lines)
            suffix = _disk_priority_omitted_line(omitted)
        if used + len(suffix) <= _DISK_PRIORITY_BLOCK_LIMIT:
            lines.append(suffix)
        else:
            return header[: _DISK_PRIORITY_BLOCK_LIMIT]
    return (header + "".join(lines))[: _DISK_PRIORITY_BLOCK_LIMIT]


def render_briefing(state: FleetState, *, manual_section: str = "") -> str:
    """Render the briefing body exactly as ``discord_briefing.j2`` would.

    Returns the *raw* template output (no trim/truncate) so the golden parity
    test can compare it against the Jinja shim byte-for-byte.
    """
    parts: list[str] = []

    def append_guest_results(node_name: str) -> bool:
        """Append this node's LXC/VM records and report whether any existed."""
        node_lxcs = [lx for lx in state.lxc if lx.node == node_name]
        node_vms = [vm for vm in state.vm if vm.node == node_name]
        if node_lxcs:
            parts.append("\n- LXC")
            for lx in node_lxcs:
                seg = f"\n  - {lx.name} ({lx.id})"
                if lx.app:
                    seg += f" — {lx.app}"
                if lx.os and lx.os != "None":
                    seg += f" | OS: {lx.os}"
                if lx.alloy:
                    seg += f"{' |' if lx.app or lx.os else ' —'} Alloy: {lx.alloy}"
                if lx.housekeeping is not None:
                    summary = lx.housekeeping
                    separator = " |" if lx.app or (lx.os and lx.os != "None") or lx.alloy else " —"
                    seg += (
                        f"{separator} Housekeeping: {summary.status} "
                        f"({human_bytes(summary.bytes_reclaimed)} reclaimed; "
                        f"{human_bytes(summary.bytes_archived)} archived)"
                    )
                # Alloy-only runs intentionally skip snapshots; reserve the
                # marker for update records where a snapshot was applicable.
                if not lx.snap and (lx.app or lx.os):
                    seg += " *(no snap)*"
                parts.append(seg)
        if node_vms:
            parts.append("\n- VM")
            for vm in node_vms:
                seg = f"\n  - {vm.name} ({vm.vmid})"
                if vm.status:
                    seg += f" — {vm.status}"
                if vm.pkg_count:
                    seg += f" ({vm.pkg_count} upgraded)"
                if vm.alloy:
                    seg += f"{' |' if vm.status else ' —'} Alloy: {vm.alloy}"
                parts.append(seg)
        return bool(node_lxcs or node_vms)

    rendered_nodes = {n.node for n in state.node}
    for i, n in enumerate(state.node):
        if i != 0:
            parts.append("\n\n")
        parts.append(f"**{n.node}: ({n.status})**")
        # Reboot reasons (ordinary marker/kernel + NVIDIA mismatch) render only
        # when the record carries them — legacy key-free records stay identical.
        for reason in (n.reboot_reasons or []):
            parts.append(f"\n- reboot required: {reason}")
        checks = n.checks or {}
        target_kernel = checks.get("target_kernel")
        if n.reboot_reasons and checks.get("nvidia_post_reboot_ready") and target_kernel:
            parts.append(
                f"\n- NVIDIA target {target_kernel}: DKMS/modules ready for reboot"
            )
        if n.node != "Ansible-Manager" and not append_guest_results(n.node):
            parts.append("\n- *No container changes.*")

    # Limited or guest-only runs do not execute the node phase, but their LXC
    # and VM records still need a node section in notifications. Preserve first
    # appearance order while rendering each otherwise-unrepresented node once.
    guest_only_nodes: list[str] = []
    for lxc_record in state.lxc:
        if lxc_record.node not in rendered_nodes and lxc_record.node not in guest_only_nodes:
            guest_only_nodes.append(lxc_record.node)
    for vm_record in state.vm:
        if vm_record.node not in rendered_nodes and vm_record.node not in guest_only_nodes:
            guest_only_nodes.append(vm_record.node)
    for node_name in guest_only_nodes:
        if parts:
            parts.append("\n\n")
        parts.append(f"**{node_name}: (GUEST RESULTS)**")
        append_guest_results(node_name)

    if manual_section:
        if parts:
            parts.append("\n\n")
        parts.append(manual_section.strip())

    if state.remote:
        parts.append("\n\n**Remote Hosts**")
        for r in state.remote:
            parts.append(f"\n- {r.host} — {r.status}")

    if state.custom:
        parts.append("\n\n**Custom Systems**")
        for c in state.custom:
            parts.append(f"\n- **{c.name}** ({c.host}) — {c.app}")

    if state.errors:
        parts.append("\n\n**Error Log:**")
        for e in state.errors:
            parts.append(f"\n- **{e.host}** failed at `{e.task}`: `{e.error}`")

    # Warnings carrying a structured disk insight render in the leading
    # priority block; legacy warnings keep the exact historical section bytes.
    disk_warnings = [w for w in state.warnings if w.disk_usage is not None]
    generic_warnings = [w for w in state.warnings if w.disk_usage is None]
    if generic_warnings:
        parts.append("\n\n**⚠️ Warnings**")
        for w in generic_warnings:
            parts.append(f"\n- **{w.host}** at `{w.task}`: {w.warning}")

    # No trailing newline: the template's final source newline is stripped by
    # Jinja's keep_trailing_newline=False (and the driver applies `| trim` anyway).
    body = "".join(parts)
    priority = _render_disk_priority_block(disk_warnings)
    if not priority:
        return body
    if not body:
        return priority
    return f"{priority}\n\n{body}"


def _truncate(s: str, length: int = 255, killwords: bool = False,
              end: str = "...", leeway: int = 5) -> str:
    """Port of Jinja2's ``do_truncate`` filter (used as ``truncate(4000, False, '\\n...')``)."""
    if len(s) <= length + leeway:
        return s
    if killwords:
        return s[: length - len(end)] + end
    result = s[: length - len(end)].rsplit(" ", 1)[0]
    return result + end


def prepare_body(state: FleetState, *, manual_section: str = "") -> str:
    """Return the trimmed, notifier-safe fleet and manual briefing body."""
    return _truncate(
        render_briefing(state, manual_section=manual_section).strip(),
        4000,
        False,
        "\n...",
    )


def briefing_title(failed: bool, *, attention: bool = False) -> str:
    """Discord embed title, with manual attention kept distinct from failure."""
    if failed:
        return "⚠️ Briefing: Failures Detected"
    if attention:
        return "⚠️ Briefing: Manual Attention Required"
    return "✅ Briefing: All Systems Clear"


def ntfy_title(failed: bool, *, attention: bool = False) -> str:
    """ASCII-safe ntfy title, with manual attention kept distinct from failure."""
    if failed:
        return "Fleet Update: Failures Detected"
    if attention:
        return "Fleet Update: Manual Attention Required"
    return "Fleet Update: All Systems Clear"


def discord_color(failed: bool, *, attention: bool = False) -> int:
    """Discord embed colour: red failure, amber attention, green success."""
    if failed:
        return _COLOR_FAILED
    return _COLOR_WARNING if attention else _COLOR_OK


def should_notify(*, force_notify: bool, dry_run: bool, changed: bool, failed: bool) -> bool:
    """Whether to send a notification — mirrors ``_should_notify``."""
    return bool(force_notify or dry_run or changed or failed)
