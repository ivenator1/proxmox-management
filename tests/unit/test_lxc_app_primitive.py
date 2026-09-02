"""Shell-level regression tests for the LXC app-update execution primitive."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml
from jinja2 import Environment  # pyright: ignore[reportMissingImports]


_PRIMITIVE = Path("ansible/primitives/lxc_app_update.yml")


def _render_inner_shell(*, bypass: bool) -> str:
    env = Environment()
    env.filters["bool"] = bool
    rendered = env.from_string(_PRIMITIVE.read_text(encoding="utf-8")).render(
        target_hosts="localhost",
        lxc_id="101",
        lxc_shell="bash",
        lxc_unattended=True,
        lxc_needs_scale=False,
        lxc_build_cpu="",
        lxc_build_ram="",
        lxc_run_cpu="",
        lxc_run_ram="",
        lxc_bypass_storage_guard=bypass,
        _app={"rc": 0, "stdout": "", "stderr": ""},
    )
    play = yaml.safe_load(rendered)
    command = str(play[0]["tasks"][1]["ansible.builtin.shell"])
    prefix = "pct exec 101 -- bash -c '"
    assert command.startswith(prefix)
    assert command.rstrip().endswith("'")
    return command[len(prefix):].rstrip()[:-1]


@pytest.mark.parametrize(
    "fetcher,update_command",
    [
        ("curl", 'bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/test.sh)"'),
        ("wget", 'bash -c "$(wget -qLO - https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/test.sh)"'),
    ],
)
def test_storage_guard_bypass_filters_fetched_ct_script(
    tmp_path: Path, fetcher: str, update_command: str,
) -> None:
    """The installed wrapper stays intact while its fetched ct script is filtered."""
    nc_dir = tmp_path / ".nc"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    app_ran = tmp_path / "app-ran"

    update = tmp_path / "update"
    original_update = f"#!/usr/bin/env bash\n{update_command}\n"
    update.write_text(original_update, encoding="utf-8")
    update.chmod(0o755)

    fake_fetcher = fake_bin / fetcher
    fake_fetcher.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<\"EOF\"\n"
        "update_script() {\n"
        "  check_container_storage\n"
        f'  printf ok > "{app_ran}"\n'
        "}\n"
        "update_script\n"
        "EOF\n",
        encoding="utf-8",
    )
    fake_fetcher.chmod(0o755)

    inner = _render_inner_shell(bypass=True)
    harness = (
        inner.replace("/tmp/.nc", str(nc_dir))
        .replace("/tmp/fleet-fetch.", str(tmp_path / "fleet-fetch."))
        .replace("/usr/bin/update", str(update))
    )
    run_env: Dict[str, Any] = dict(os.environ)
    run_env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    result = subprocess.run(
        ["bash", "-c", harness],
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert app_ran.read_text(encoding="utf-8") == "ok"
    assert update.read_text(encoding="utf-8") == original_update
    assert not list(tmp_path.glob("fleet-fetch.*"))
