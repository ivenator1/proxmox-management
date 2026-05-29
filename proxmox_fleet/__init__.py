"""proxmox_fleet — typed-Python control plane for the fleet updater.

Python owns all logic (sequencing, gating, rescue/rollback, status decisions,
reporting, config + state schemas) and all manager-local IO. Ansible is reduced
to a catalogue of single-purpose, remote/privileged execution primitives invoked
via ``proxmox_fleet.runner``.

See the migration plan and CLAUDE.md for the boundary contract.
"""

__version__ = "0.1.0"
