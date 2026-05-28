"""
Unit tests for the regex_search patterns used in
roles/lxc_update/tasks/detect.yml and dry_check.yml.

These test the extraction logic in isolation from the full Ansible task flow.
"""
from tests.conftest import render

FULL_SCRIPT = """\
#!/usr/bin/env bash
APP="sonarr"
var_cpu="4"
var_ram="2048"
# other stuff
check_for_gh_release "Sonarr/Sonarr" "Sonarr/Sonarr"
pct set $CTID -cores 2
pct set $CTID -memory 1024
"""

NO_RESOURCE_SCRIPT = 'APP="app"\n'

PARTIAL_RESOURCE_SCRIPT = 'var_cpu="2"\n# no pct set lines\n'

NO_GH_RELEASE_SCRIPT = 'var_cpu="2"\npct set $CTID -cores 1\n'


def extract(pattern_expr, content):
    """Render `(content | regex_search(pattern, '\\1') or [''])[0]`."""
    tmpl = "{{ (content | regex_search(" + pattern_expr + ", '\\\\1') or [''])[0] }}"
    return render(tmpl, content=content)


def test_var_cpu_full_script():
    assert extract("'var_cpu=\"(\\\\d+)\"'", FULL_SCRIPT) == '4'


def test_var_cpu_no_resource_script():
    assert extract("'var_cpu=\"(\\\\d+)\"'", NO_RESOURCE_SCRIPT) == ''


def test_var_cpu_partial_script():
    assert extract("'var_cpu=\"(\\\\d+)\"'", PARTIAL_RESOURCE_SCRIPT) == '2'


def test_var_ram_full_script():
    assert extract("'var_ram=\"(\\\\d+)\"'", FULL_SCRIPT) == '2048'


def test_var_ram_no_resource_script():
    assert extract("'var_ram=\"(\\\\d+)\"'", NO_RESOURCE_SCRIPT) == ''


def test_run_cpu_full_script():
    assert extract(r"'pct set \$CTID -cores (\d+)'", FULL_SCRIPT) == '2'


def test_run_cpu_partial_script():
    assert extract(r"'pct set \$CTID -cores (\d+)'", PARTIAL_RESOURCE_SCRIPT) == ''


def test_run_ram_full_script():
    assert extract(r"'pct set \$CTID -memory (\d+)'", FULL_SCRIPT) == '1024'


def test_run_ram_no_resource_script():
    assert extract(r"'pct set \$CTID -memory (\d+)'", NO_RESOURCE_SCRIPT) == ''


def test_gh_repo_extraction():
    assert extract(
        r"""'check_for_gh_release\\s+\"[^\"]+\"\\s+\"([^\"]+)\"'""",
        FULL_SCRIPT,
    ) == 'Sonarr/Sonarr'


def test_gh_repo_absent():
    assert extract(
        r"""'check_for_gh_release\\s+\"[^\"]+\"\\s+\"([^\"]+)\"'""",
        NO_GH_RELEASE_SCRIPT,
    ) == ''


# needs_resource_scale decision logic from detect.yml
NEEDS_SCALE = (
    "{{ lxc_build_cpu | default('') != '' and lxc_run_cpu | default('') != '' "
    "and (lxc_build_cpu | int) > (lxc_run_cpu | int) }}"
)


def needs_scale(**kwargs):
    return render(NEEDS_SCALE, **kwargs)


def test_needs_scale_build_greater_than_run():
    assert needs_scale(lxc_build_cpu='4', lxc_run_cpu='2') == 'True'


def test_needs_scale_build_equal_to_run():
    assert needs_scale(lxc_build_cpu='2', lxc_run_cpu='2') == 'False'


def test_needs_scale_build_less_than_run():
    assert needs_scale(lxc_build_cpu='2', lxc_run_cpu='4') == 'False'


def test_needs_scale_empty_build_cpu():
    assert needs_scale(lxc_build_cpu='', lxc_run_cpu='2') == 'False'


def test_needs_scale_empty_run_cpu():
    assert needs_scale(lxc_build_cpu='4', lxc_run_cpu='') == 'False'


def test_needs_scale_both_empty():
    assert needs_scale() == 'False'
