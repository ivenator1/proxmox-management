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

from proxmox_fleet.models.state import FleetState

# Discord embed colours — kept identical to fleet-update.yml Phase 4.
_COLOR_FAILED = 15158332
_COLOR_OK = 3066993


def render_briefing(state: FleetState) -> str:
    """Render the briefing body exactly as ``discord_briefing.j2`` would.

    Returns the *raw* template output (no trim/truncate) so the golden parity
    test can compare it against the Jinja shim byte-for-byte.
    """
    parts: list[str] = []

    for i, n in enumerate(state.node):
        if i != 0:
            parts.append("\n\n")
        parts.append(f"**{n.node}: ({n.status})**")
        # Reboot reasons (ordinary marker/kernel + NVIDIA mismatch) render only
        # when the record carries them — legacy key-free records stay identical.
        for reason in (n.reboot_reasons or []):
            parts.append(f"\n- reboot required: {reason}")
        if n.node != "Ansible-Manager":
            node_lxcs = [lx for lx in state.lxc if lx.node == n.node]
            node_vms = [vm for vm in state.vm if vm.node == n.node]
            if node_lxcs:
                parts.append("\n- LXC")
                for lx in node_lxcs:
                    seg = f"\n  - {lx.name} ({lx.id}) — {lx.app}"
                    if lx.os and lx.os != "None":
                        seg += f" | OS: {lx.os}"
                    if not lx.snap:
                        seg += " *(no snap)*"
                    parts.append(seg)
            if node_vms:
                parts.append("\n- VM")
                for vm in node_vms:
                    seg = f"\n  - {vm.name} ({vm.vmid}) — {vm.status}"
                    if vm.pkg_count:
                        seg += f" ({vm.pkg_count} upgraded)"
                    parts.append(seg)
            if not node_lxcs and not node_vms:
                parts.append("\n- *No container changes.*")

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

    if state.warnings:
        parts.append("\n\n**⚠️ Warnings**")
        for w in state.warnings:
            parts.append(f"\n- **{w.host}** at `{w.task}`: {w.warning}")

    # No trailing newline: the template's final source newline is stripped by
    # Jinja's keep_trailing_newline=False (and the driver applies `| trim` anyway).
    return "".join(parts)


def _truncate(s: str, length: int = 255, killwords: bool = False,
              end: str = "...", leeway: int = 5) -> str:
    """Port of Jinja2's ``do_truncate`` filter (used as ``truncate(4000, False, '\\n...')``)."""
    if len(s) <= length + leeway:
        return s
    if killwords:
        return s[: length - len(end)] + end
    result = s[: length - len(end)].rsplit(" ", 1)[0]
    return result + end


def prepare_body(state: FleetState) -> str:
    """The notifier/history body: ``render_briefing(state) | trim | truncate(4000, ...)``."""
    return _truncate(render_briefing(state).strip(), 4000, False, "\n...")


def briefing_title(failed: bool) -> str:
    """Discord embed title (with emoji) — mirrors ``_briefing_title``."""
    return "⚠️ Briefing: Failures Detected" if failed else "✅ Briefing: All Systems Clear"


def ntfy_title(failed: bool) -> str:
    """ASCII-safe ntfy 'Title' header — mirrors ``_ntfy_title``."""
    return "Fleet Update: Failures Detected" if failed else "Fleet Update: All Systems Clear"


def discord_color(failed: bool) -> int:
    """Discord embed colour — mirrors ``_discord_color``."""
    return _COLOR_FAILED if failed else _COLOR_OK


def should_notify(*, force_notify: bool, dry_run: bool, changed: bool, failed: bool) -> bool:
    """Whether to send a notification — mirrors ``_should_notify``."""
    return bool(force_notify or dry_run or changed or failed)
