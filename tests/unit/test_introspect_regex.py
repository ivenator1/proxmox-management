"""
Unit tests for the regex_search patterns and inline conditions used in
roles/lxc_update/tasks/introspect.yml.

Tests parse `pct config` and `pct status` stdout — the expressions used to
set lxc_name, lxc_os, lxc_is_template, lxc_is_running, lxc_was_stopped.
"""
from tests.conftest import render

SAMPLE_CONFIG = """\
arch: amd64
cores: 2
hostname: sonarr
memory: 2048
ostype: ubuntu
rootfs: local:101/vm-101-disk-0.raw,size=10G
tags: community-script
"""

TEMPLATE_CONFIG = SAMPLE_CONFIG + "template: 1\n"

CONFIG_NO_HOSTNAME = """\
arch: amd64
ostype: debian
cores: 1
"""

CONFIG_NO_OSTYPE = """\
arch: amd64
hostname: myapp
cores: 1
"""

STATUS_RUNNING = "status: running"
STATUS_STOPPED = "status: stopped"

# Expressions from introspect.yml
LXC_NAME_EXPR = "{{ (lxc_config_stdout | regex_search('hostname: (\\\\S+)', '\\\\1') or ['Unknown'])[0] }}"
LXC_OS_EXPR = "{{ (lxc_config_stdout | regex_search('ostype: (\\\\w+)', '\\\\1') or ['debian'])[0] }}"
IS_TEMPLATE_EXPR = "{{ 'template: 1' in lxc_config_stdout }}"
IS_RUNNING_EXPR = "{{ 'status: running' in lxc_status_stdout }}"
WAS_STOPPED_EXPR = "{{ 'status: stopped' in lxc_status_stdout }}"


def test_lxc_name_extracted():
    assert render(LXC_NAME_EXPR, lxc_config_stdout=SAMPLE_CONFIG) == 'sonarr'


def test_lxc_name_missing_falls_back_to_unknown():
    assert render(LXC_NAME_EXPR, lxc_config_stdout=CONFIG_NO_HOSTNAME) == 'Unknown'


def test_lxc_os_extracted():
    assert render(LXC_OS_EXPR, lxc_config_stdout=SAMPLE_CONFIG) == 'ubuntu'


def test_lxc_os_missing_falls_back_to_debian():
    assert render(LXC_OS_EXPR, lxc_config_stdout=CONFIG_NO_OSTYPE) == 'debian'


def test_is_template_false():
    assert render(IS_TEMPLATE_EXPR, lxc_config_stdout=SAMPLE_CONFIG) == 'False'


def test_is_template_true():
    assert render(IS_TEMPLATE_EXPR, lxc_config_stdout=TEMPLATE_CONFIG) == 'True'


def test_is_running_true():
    assert render(IS_RUNNING_EXPR, lxc_status_stdout=STATUS_RUNNING) == 'True'


def test_is_running_false_when_stopped():
    assert render(IS_RUNNING_EXPR, lxc_status_stdout=STATUS_STOPPED) == 'False'


def test_was_stopped_true():
    assert render(WAS_STOPPED_EXPR, lxc_status_stdout=STATUS_STOPPED) == 'True'


def test_was_stopped_false_when_running():
    assert render(WAS_STOPPED_EXPR, lxc_status_stdout=STATUS_RUNNING) == 'False'
