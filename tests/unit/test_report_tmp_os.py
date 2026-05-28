"""
Unit tests for the `tmp_os` Jinja2 expression in
roles/lxc_update/tasks/report.yml (lines 27-37).
"""
from tests.conftest import render, make_result

TMP_OS = """\
{%- if lxc_is_template | bool or not lxc_is_running | bool or lxc_dry_run | bool -%}
{%- elif lxc_id in (os_update_exclude_list | default([])) -%}SKIPPED
{%- elif os_update_res is defined and os_update_res is failed -%}FAILED
{%- elif lxc_reboot_action is defined and lxc_reboot_action.changed -%}
{%- set _pkg = (os_update_res.stdout | default('') | regex_search('(\\d+ upgraded)', '\\1') or [''])[0] -%}
Updated{% if _pkg %} ({{ _pkg }}){% endif %} & Rebooted
{%- elif os_update_res is defined and os_update_res.changed -%}
{%- set _pkg = (os_update_res.stdout | default('') | regex_search('(\\d+ upgraded)', '\\1') or [''])[0] -%}
Updated{% if _pkg %} ({{ _pkg }}){% endif %}
{%- else -%}OK{%- endif -%}"""

# Also test the None-guard applied in report.yml payload (line 48)
NONE_GUARD = "{{ '' if tmp_os is none else tmp_os | trim }}"

BASE = dict(
    lxc_is_template=False,
    lxc_is_running=True,
    lxc_dry_run=False,
    lxc_id='101',
)


def t(**kwargs):
    ctx = {**BASE, **kwargs}
    return render(TMP_OS, **ctx)


def test_template_container():
    assert t(lxc_is_template=True) == ''


def test_not_running():
    assert t(lxc_is_running=False) == ''


def test_dry_run():
    assert t(lxc_dry_run=True) == ''


def test_excluded_from_os_update():
    assert t(os_update_exclude_list=['101']) == 'SKIPPED'


def test_excluded_id_no_match():
    assert t(os_update_exclude_list=['999']) == 'OK'


def test_os_failed():
    assert t(os_update_res=make_result(failed=True)) == 'FAILED'


def test_updated_with_reboot_and_pkg_count():
    assert t(
        os_update_res=make_result(changed=True, stdout='2 upgraded, 0 newly installed'),
        lxc_reboot_action=make_result(changed=True),
    ) == 'Updated (2 upgraded) & Rebooted'


def test_updated_with_reboot_no_pkg_count():
    assert t(
        os_update_res=make_result(changed=True, stdout=''),
        lxc_reboot_action=make_result(changed=True),
    ) == 'Updated & Rebooted'


def test_updated_no_reboot_with_pkg_count():
    assert t(os_update_res=make_result(changed=True, stdout='3 upgraded, 1 newly installed')) == 'Updated (3 upgraded)'


def test_updated_no_reboot_no_pkg_count():
    assert t(os_update_res=make_result(changed=True, stdout='')) == 'Updated'


def test_all_ok():
    assert t() == 'OK'


def test_none_guard_empty_string():
    # When tmp_os is empty (Ansible stores None for empty >- blocks), the guard returns ''
    result = render(NONE_GUARD, tmp_os=None)
    assert result == ''


def test_none_guard_real_value():
    result = render(NONE_GUARD, tmp_os='SKIPPED')
    assert result == 'SKIPPED'
