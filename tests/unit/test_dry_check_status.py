"""
Unit tests for the `dry_run_status` Jinja2 expression in
roles/lxc_update/tasks/dry_check.yml (lines 28-34).
"""
from tests.conftest import render

DRY_RUN_STATUS = """\
{%- if gh_repo == '' -%}no ver info
{%- elif gh_release.status | default(0) != 200 -%}fetch failed
{%- elif installed_ver.stdout | default('') == '' -%}unknown (latest: {{ gh_release.json.tag_name | default('?') }})
{%- elif installed_ver.stdout | trim | regex_replace('^v', '') == (gh_release.json.tag_name | default('') | trim | regex_replace('^v', '')) -%}{{ installed_ver.stdout | trim }}
{%- else -%}{{ installed_ver.stdout | trim }} → {{ gh_release.json.tag_name | default('?') }}
{%- endif -%}"""


def s(gh_repo='owner/repo', gh_release_status=200, gh_tag='v2.0', installed='v2.0'):
    gh_release = {'status': gh_release_status, 'json': {'tag_name': gh_tag}}
    installed_ver = {'stdout': installed}
    return render(DRY_RUN_STATUS, gh_repo=gh_repo, gh_release=gh_release, installed_ver=installed_ver)


def test_no_gh_repo():
    assert s(gh_repo='') == 'no ver info'


def test_gh_fetch_failed_404():
    assert s(gh_release_status=404) == 'fetch failed'


def test_gh_fetch_failed_connection_error():
    # URI task ran but got a connection error: status=0 (Ansible sets this on failure
    # when failed_when: false).
    gh_release = {'status': 0, 'json': {}}
    installed_ver = {'stdout': 'v1.0'}
    result = render(DRY_RUN_STATUS, gh_repo='owner/repo', gh_release=gh_release, installed_ver=installed_ver)
    assert result == 'fetch failed'


def test_installed_version_empty():
    result = s(installed='')
    assert result == 'unknown (latest: v2.0)'


def test_installed_version_empty_unknown_tag():
    gh_release = {'status': 200, 'json': {}}
    installed_ver = {'stdout': ''}
    result = render(DRY_RUN_STATUS, gh_repo='owner/repo', gh_release=gh_release, installed_ver=installed_ver)
    assert result == 'unknown (latest: ?)'


def test_up_to_date():
    assert s(installed='v2.0', gh_tag='v2.0') == 'v2.0'


def test_up_to_date_v_prefix_stripped():
    # installed='v2.0', latest='2.0' — stripping 'v' makes them equal
    assert s(installed='v2.0', gh_tag='2.0') == 'v2.0'


def test_update_available():
    assert s(installed='v1.9', gh_tag='v2.0') == 'v1.9 → v2.0'


def test_update_available_with_whitespace_in_installed():
    # installed_ver.stdout from pct exec may have trailing whitespace; trim normalises it.
    # tag_name from GitHub JSON does not have whitespace, so only installed is padded.
    gh_release = {'status': 200, 'json': {'tag_name': 'v2.0'}}
    installed_ver = {'stdout': ' v1.9 '}
    result = render(DRY_RUN_STATUS, gh_repo='owner/repo', gh_release=gh_release, installed_ver=installed_ver)
    assert result == 'v1.9 → v2.0'
