"""
Unit tests for the tmp_custom Jinja2 expression in
roles/custom_update/tasks/report.yml.
"""
import pytest
from tests.conftest import render, make_result

TMP_CUSTOM = """\
{%- set _cw_type = (custom_cfg.changed_when | default({})).type | default('version') -%}
{%- set _reboot_done = custom_reboot_res is defined and (custom_reboot_res.changed | default(false)) -%}
{%- if custom_dry_run | bool -%}
dry-run: {{ custom_ver_before | default('?') }} → {{ custom_latest_ver | default('unknown') }}
{%- elif _cw_type == 'always' -%}
Updated{% if _reboot_done %} + Rebooted{% endif %}
{%- elif _cw_type == 'command'
     and custom_changed_cmd_res is defined
     and not (custom_changed_cmd_res.skipped | default(false)) -%}
{%- if custom_changed_cmd_res.rc | default(1) == 0 -%}
Updated{% if _reboot_done %} + Rebooted{% endif %}
{%- else -%}OK{%- endif -%}
{%- elif custom_ver_before | default('') | trim != ''
     and custom_ver_after | default('') | trim != '' -%}
{%- if custom_ver_before | trim != custom_ver_after | trim -%}
Updated: {{ custom_ver_before | trim }} → {{ custom_ver_after | trim }}{% if _reboot_done %} + Rebooted{% endif %}
{%- else -%}OK{%- endif -%}
{%- else -%}
Updated{% if _reboot_done %} + Rebooted{% endif %}
{%- endif -%}"""

BASE = dict(
    custom_dry_run=False,
    custom_cfg={'changed_when': {'type': 'version'}},
    custom_ver_before='',
    custom_ver_after='',
)


def t(**kwargs):
    ctx = {**BASE, **kwargs}
    return render(TMP_CUSTOM, **ctx)


def test_dry_run_with_versions():
    assert t(custom_dry_run=True, custom_ver_before='1.0', custom_latest_ver='1.1') == 'dry-run: 1.0 → 1.1'


def test_dry_run_unknown_latest():
    assert t(custom_dry_run=True, custom_ver_before='1.0') == 'dry-run: 1.0 → unknown'


def test_changed_when_always():
    assert t(custom_cfg={'changed_when': {'type': 'always'}}) == 'Updated'


def test_changed_when_always_with_reboot():
    assert t(
        custom_cfg={'changed_when': {'type': 'always'}},
        custom_reboot_res=make_result(changed=True),
    ) == 'Updated + Rebooted'


def test_changed_when_command_exit_zero():
    assert t(
        custom_cfg={'changed_when': {'type': 'command', 'command': 'check-version'}},
        custom_changed_cmd_res={'rc': 0, 'skipped': False},
    ) == 'Updated'


def test_changed_when_command_exit_nonzero():
    assert t(
        custom_cfg={'changed_when': {'type': 'command', 'command': 'check-version'}},
        custom_changed_cmd_res={'rc': 1, 'skipped': False},
    ) == 'OK'


def test_changed_when_command_with_reboot():
    assert t(
        custom_cfg={'changed_when': {'type': 'command', 'command': 'check-version'}},
        custom_changed_cmd_res={'rc': 0, 'skipped': False},
        custom_reboot_res=make_result(changed=True),
    ) == 'Updated + Rebooted'


def test_version_changes():
    assert t(custom_ver_before='1.0', custom_ver_after='1.1') == 'Updated: 1.0 → 1.1'


def test_version_unchanged():
    assert t(custom_ver_before='1.0', custom_ver_after='1.0') == 'OK'


def test_version_changed_with_reboot():
    assert t(
        custom_ver_before='1.0',
        custom_ver_after='1.1',
        custom_reboot_res=make_result(changed=True),
    ) == 'Updated: 1.0 → 1.1 + Rebooted'


def test_no_version_data_fallback():
    assert t() == 'Updated'


def test_no_version_data_fallback_with_reboot():
    assert t(custom_reboot_res=make_result(changed=True)) == 'Updated + Rebooted'
