"""Parsing helpers for LXC introspect/detect output.

Pure functions over raw stdout strings — no I/O, no Ansible.
Patterns are verbatim from test_introspect_regex.py and test_detect_regex.py.
"""

from __future__ import annotations

import re
from typing import Optional


def parse_pct_config(stdout: str) -> dict:
    """Extract {name, os_type, is_template, cores, memory} from `pct config <id>`.

    ``cores``/``memory`` are the container's live allocation, as strings ("" when
    the key is absent and PVE's default applies). They are anchored to the start
    of a line because the ``description:`` field holds a blob of URL-encoded HTML
    that an unanchored pattern can match inside.
    """
    name_m = re.search(r"hostname: (\S+)", stdout)
    os_m = re.search(r"ostype: (\w+)", stdout)
    cores_m = re.search(r"^cores: (\d+)", stdout, re.MULTILINE)
    memory_m = re.search(r"^memory: (\d+)", stdout, re.MULTILINE)
    return {
        "name": name_m.group(1) if name_m else "Unknown",
        "os_type": os_m.group(1) if os_m else "debian",
        "is_template": "template: 1" in stdout,
        "cores": cores_m.group(1) if cores_m else "",
        "memory": memory_m.group(1) if memory_m else "",
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

    Returns {build_cpu, build_ram, gh_repo, var_os, var_version}; any missing
    field is "".

    ``build_cpu``/``build_ram`` are the script's declared spec. There is no
    run-side counterpart here any more: the old ``pct set $CTID -cores N`` lines
    are gone from every current ct script (and from build.func/install.func), so
    upstream no longer does temporary build-time scaling. The container's live
    allocation — the only sane run-side source — comes from ``parse_pct_config``,
    and :func:`resource_scale_plan` combines the two.
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

    gh_repo = _first(r'check_for_gh_release\s+"[^"]+"\s+"([^"]+)"')

    return {
        "build_cpu": _var("var_cpu"),
        "build_ram": _var("var_ram"),
        "gh_repo": gh_repo,
        "var_os": _var("var_os").lower(),
        "var_version": _var("var_version"),
    }


def _as_int(value: object) -> int:
    """Best-effort int for a parsed field; 0 when absent or unparseable."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def resource_scale_plan(ct_info: dict, pct_info: dict) -> dict:
    """Decide whether a container is under-provisioned for its update script.

    Build requirements come from the ct script's ``var_cpu``/``var_ram``; the
    current allocation comes from ``pct config``. Returns
    {needs_scale, build_cpu, build_ram, run_cpu, run_ram} as strings, where the
    build_* values are the temporary target and the run_* values are the live
    pre-scale allocation to restore afterwards.

    Targets are ``max(script, current)`` so a container provisioned *above* its
    script's spec is never shrunk, and needs_scale is False whenever the current
    allocation is unreadable — a container created by the script itself already
    matches, so this only fires on hand-provisioned ones.

    Whether the plan is acted on is a separate decision (``lxc_resource_scaling``):
    upstream dropped build-time scaling, so executing it re-introduces an
    optimization the scripts no longer perform rather than restoring parity.
    """
    cur_cpu = _as_int(pct_info.get("cores"))
    cur_ram = _as_int(pct_info.get("memory"))
    if not cur_cpu or not cur_ram:
        return {"needs_scale": False, "build_cpu": "", "build_ram": "",
                "run_cpu": "", "run_ram": ""}

    target_cpu = max(_as_int(ct_info.get("build_cpu")), cur_cpu)
    target_ram = max(_as_int(ct_info.get("build_ram")), cur_ram)
    return {
        "needs_scale": target_cpu > cur_cpu or target_ram > cur_ram,
        "build_cpu": str(target_cpu),
        "build_ram": str(target_ram),
        "run_cpu": str(cur_cpu),
        "run_ram": str(cur_ram),
    }


def script_name_from_update(content: str) -> Optional[str]:
    """Extract the community-scripts name from /usr/bin/update content.

    Returns the name (e.g. 'sonarr') or None if no ct/*.sh reference found.
    """
    m = re.search(r"ct/([^.]+)\.sh", content)
    return m.group(1) if m else None
