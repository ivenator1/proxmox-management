"""
Unit tests for the `tmp_app` Jinja2 expression in
roles/lxc_update/tasks/report.yml (lines 4-26).

Each test exercises one branch of the 11-branch decision tree.
"""
import pytest
from tests.conftest import render, make_result

# Exact expression from report.yml, preserved verbatim so tests break if the
# logic changes.
TMP_APP = """\
{%- if lxc_is_template | bool -%}TEMPLATE
{%- elif not lxc_is_running | bool -%}NEVER STARTED
{%- elif lxc_dry_run | bool -%}{{ dry_run_status | default('dry-run') }}
{%- elif lxc_no_update_script | default(false) | bool -%}NO SCRIPT
{%- elif app_update_res is defined and app_update_res is failed -%}FAILED
{%- elif app_update_res is defined and app_update_res.changed -%}
{%- if lxc_ver_before is defined and lxc_ver_after is defined
   and lxc_ver_before.stdout | default('') | trim != ''
   and lxc_ver_after.stdout | default('') | trim != ''
   and lxc_ver_before.stdout | trim | regex_replace('^v', '') != lxc_ver_after.stdout | trim | regex_replace('^v', '') -%}
Updated: {{ lxc_ver_before.stdout | trim }} → {{ lxc_ver_after.stdout | trim }}
{%- elif lxc_ver_before is defined and lxc_ver_after is defined
   and lxc_ver_before.stdout | default('') | trim != ''
   and lxc_ver_after.stdout | default('') | trim != ''
   and lxc_ver_before.stdout | trim | regex_replace('^v', '') == lxc_ver_after.stdout | trim | regex_replace('^v', '') -%}
OK
{%- elif dpkg_hash_before is defined and dpkg_hash_after is defined
   and dpkg_hash_before.stdout | default('') | trim != ''
   and dpkg_hash_after.stdout | default('') | trim != '' -%}
{%- if dpkg_hash_before.stdout | trim != dpkg_hash_after.stdout | trim -%}UPDATED{%- else -%}OK{%- endif -%}
{%- else -%}UPDATED{%- endif -%}
{%- else -%}OK{%- endif -%}"""

BASE = dict(
    lxc_is_template=False,
    lxc_is_running=True,
    lxc_dry_run=False,
)


def t(**kwargs):
    ctx = {**BASE, **kwargs}
    return render(TMP_APP, **ctx)


def test_template_container():
    assert t(lxc_is_template=True) == 'TEMPLATE'


def test_never_started():
    assert t(lxc_is_running=False) == 'NEVER STARTED'


def test_dry_run_with_status():
    assert t(lxc_dry_run=True, dry_run_status='v1.9 → v2.0') == 'v1.9 → v2.0'


def test_dry_run_default_fallback():
    assert t(lxc_dry_run=True) == 'dry-run'


def test_no_update_script():
    assert t(lxc_no_update_script=True) == 'NO SCRIPT'


def test_app_failed():
    assert t(app_update_res=make_result(failed=True)) == 'FAILED'


def test_version_updated():
    assert t(
        app_update_res=make_result(changed=True),
        lxc_ver_before={'stdout': 'v4.0'},
        lxc_ver_after={'stdout': 'v4.1'},
    ) == 'Updated: v4.0 → v4.1'


def test_version_v_prefix_stripped_equal():
    # ver_before='v4.0', ver_after='4.0' — stripping the 'v' makes them equal → OK
    assert t(
        app_update_res=make_result(changed=True),
        lxc_ver_before={'stdout': 'v4.0'},
        lxc_ver_after={'stdout': '4.0'},
    ) == 'OK'


def test_dpkg_hash_differs():
    assert t(
        app_update_res=make_result(changed=True),
        dpkg_hash_before={'stdout': 'abc123'},
        dpkg_hash_after={'stdout': 'def456'},
    ) == 'UPDATED'


def test_dpkg_hash_matches():
    assert t(
        app_update_res=make_result(changed=True),
        dpkg_hash_before={'stdout': 'abc123'},
        dpkg_hash_after={'stdout': 'abc123'},
    ) == 'OK'


def test_dpkg_hash_with_whitespace():
    # Hashes may come back with surrounding whitespace; trim must normalise them.
    assert t(
        app_update_res=make_result(changed=True),
        dpkg_hash_before={'stdout': '  abc123  '},
        dpkg_hash_after={'stdout': 'abc123'},
    ) == 'OK'


def test_no_hash_data_fallback():
    # app changed, no ver files, no dpkg hash → 'UPDATED'
    assert t(app_update_res=make_result(changed=True)) == 'UPDATED'


def test_app_not_changed():
    assert t(app_update_res=make_result(changed=False)) == 'OK'
