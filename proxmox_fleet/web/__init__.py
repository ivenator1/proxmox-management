"""Fleet web dashboard — pending updates, run history, run trigger.

Optional subsystem behind ``pip install -e '.[web]'`` (fastapi + uvicorn).
Read-only pages serve straight from ``fleet_history_dir`` (the run history and
pending-scan snapshots already written by the CLI); the run-trigger endpoint
launches ``python -m proxmox_fleet.cli`` as a detached subprocess so a
dashboard restart never kills a mid-flight run.
"""
