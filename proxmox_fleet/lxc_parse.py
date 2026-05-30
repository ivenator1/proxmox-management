"""Parsing helpers for LXC introspect/detect output.

Pure functions over raw stdout strings — no I/O, no Ansible.
Patterns are verbatim from test_introspect_regex.py and test_detect_regex.py.
"""

from __future__ import annotations

import re
from typing import Optional


def parse_pct_config(stdout: str) -> dict:
    """Extract {name, os_type, is_template} from `pct config <id>` stdout."""
    name_m = re.search(r"hostname: (\S+)", stdout)
    os_m = re.search(r"ostype: (\w+)", stdout)
    return {
        "name": name_m.group(1) if name_m else "Unknown",
        "os_type": os_m.group(1) if os_m else "debian",
        "is_template": "template: 1" in stdout,
    }


def parse_pct_status(stdout: str) -> dict:
    """Extract {is_running, was_stopped} from `pct status <id>` stdout."""
    return {
        "is_running": "status: running" in stdout,
        "was_stopped": "status: stopped" in stdout,
    }


def parse_ct_script(content: str) -> dict:
    """Extract resource requirements and GH repo from a community-scripts ct/*.sh file.

    Returns {build_cpu, build_ram, run_cpu, run_ram, gh_repo, needs_resource_scale}.
    Any missing field is "". needs_resource_scale is True only when both build_cpu
    and run_cpu are non-empty integers and build_cpu > run_cpu.
    """

    def _first(pattern: str) -> str:
        m = re.search(pattern, content)
        return m.group(1) if m else ""

    build_cpu = _first(r'var_cpu="(\d+)"')
    build_ram = _first(r'var_ram="(\d+)"')
    run_cpu = _first(r"pct set \$CTID -cores (\d+)")
    run_ram = _first(r"pct set \$CTID -memory (\d+)")
    gh_repo = _first(r'check_for_gh_release\s+"[^"]+"\s+"([^"]+)"')

    needs_scale = False
    if build_cpu and run_cpu:
        try:
            needs_scale = int(build_cpu) > int(run_cpu)
        except ValueError:
            pass

    return {
        "build_cpu": build_cpu,
        "build_ram": build_ram,
        "run_cpu": run_cpu,
        "run_ram": run_ram,
        "gh_repo": gh_repo,
        "needs_resource_scale": needs_scale,
    }


def script_name_from_update(content: str) -> Optional[str]:
    """Extract the community-scripts name from /usr/bin/update content.

    Returns the name (e.g. 'sonarr') or None if no ct/*.sh reference found.
    """
    m = re.search(r"ct/([^.]+)\.sh", content)
    return m.group(1) if m else None
