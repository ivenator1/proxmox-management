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


def parse_df_percent(stdout: str) -> Optional[int]:
    """Extract the used-capacity percentage from ``df -P /`` stdout.

    ``-P`` pins POSIX output so the capacity column stays 5th even when a long
    device name would otherwise wrap the line. Returns None when the output is
    empty or unparseable (e.g. the container was not running).
    """
    for line in stdout.splitlines():
        m = re.search(r"\s(\d{1,3})%\s", line)
        if m:
            return int(m.group(1))
    return None


def parse_os_release(stdout: str) -> dict:
    """Extract {id, version_id} from ``/etc/os-release`` contents.

    Values may or may not be quoted (``ID=debian`` but ``VERSION_ID="12"``).
    Missing fields come back as "".
    """

    def _val(key: str) -> str:
        m = re.search(rf'^{key}="?([^"\n]*)"?$', stdout, re.MULTILINE)
        return m.group(1).strip() if m else ""

    return {"id": _val("ID").lower(), "version_id": _val("VERSION_ID")}


def os_version_matches(cur_os: str, cur_ver: str, rec_os: str, rec_ver: str) -> bool:
    """Mirror of build.func's check_container_os_guard match test.

    Exact match, or a prefix on a dot boundary so alpine 3.22 accepts 3.22.1.
    Missing data on either side counts as a match (the guard returns 0 early),
    so an unparseable read never produces a false warning.
    """
    if not cur_os or not cur_ver or not rec_os or not rec_ver:
        return True
    if cur_os.lower() != rec_os.lower():
        return False
    return cur_ver == rec_ver or cur_ver.startswith(f"{rec_ver}.")


def parse_ct_script(content: str) -> dict:
    """Extract resource requirements and GH repo from a community-scripts ct/*.sh file.

    Returns {build_cpu, build_ram, run_cpu, run_ram, gh_repo, needs_resource_scale}.
    Any missing field is "". needs_resource_scale is True only when both build_cpu
    and run_cpu are non-empty integers and build_cpu > run_cpu.
    """

    def _first(pattern: str) -> str:
        m = re.search(pattern, content)
        return m.group(1) if m else ""

    def _var(name: str) -> str:
        """Read a var_* declaration in either supported form.

        Current community scripts write ``var_os="${var_os:-debian}"`` so the
        value can be overridden from the environment; older ones wrote a bare
        ``var_os="debian"``.
        """
        m = re.search(rf'{name}="\$\{{{name}:-([^}}"]*)\}}"', content)
        if m:
            return m.group(1).strip()
        m = re.search(rf'{name}="([^"$]*)"', content)
        return m.group(1).strip() if m else ""

    build_cpu = _first(r'var_cpu="(\d+)"')
    build_ram = _first(r'var_ram="(\d+)"')
    run_cpu = _first(r"pct set \$CTID -cores (\d+)")
    run_ram = _first(r"pct set \$CTID -memory (\d+)")
    gh_repo = _first(r'check_for_gh_release\s+"[^"]+"\s+"([^"]+)"')
    var_os = _var("var_os").lower()
    var_version = _var("var_version")

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
        "var_os": var_os,
        "var_version": var_version,
    }


def script_name_from_update(content: str) -> Optional[str]:
    """Extract the community-scripts name from /usr/bin/update content.

    Returns the name (e.g. 'sonarr') or None if no ct/*.sh reference found.
    """
    m = re.search(r"ct/([^.]+)\.sh", content)
    return m.group(1) if m else None
