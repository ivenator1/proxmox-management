"""
Unit tests for templates/discord_briefing.j2.

Renders the template with controlled fleet state dicts and asserts on the
markdown output that would be posted to Discord.
"""
import pathlib
import pytest
from tests.conftest import make_jinja2_env

TEMPLATE_PATH = pathlib.Path(__file__).parent.parent.parent / 'templates' / 'discord_briefing.j2'


@pytest.fixture(scope='module')
def template():
    env = make_jinja2_env()
    env.loader = None
    content = TEMPLATE_PATH.read_text()
    return env.from_string(content)


def render_briefing(template, **kwargs):
    defaults = dict(
        fleet_node_data=[],
        fleet_lxc_data=[],
        fleet_vm_data=[],
        fleet_remote_data=[],
        fleet_error_log=[],
    )
    return template.render(**{**defaults, **kwargs})


def lxc(node='pve-01', name='sonarr', id='101', app='Updated: v4.0 → v4.1', os='OK', snap=True):
    return dict(node=node, name=name, id=id, app=app, os=os, snap=snap)


def node(node='pve-01', status='OK'):
    return dict(node=node, status=status)


def test_node_header(template):
    result = render_briefing(template, fleet_node_data=[node()])
    assert '**pve-01: (OK)**' in result


def test_updated_lxc_appears(template):
    result = render_briefing(
        template,
        fleet_node_data=[node()],
        fleet_lxc_data=[lxc()],
    )
    assert '- sonarr (101) — Updated: v4.0 → v4.1' in result


def test_os_status_shown_when_non_empty(template):
    result = render_briefing(
        template,
        fleet_node_data=[node()],
        fleet_lxc_data=[lxc(os='Updated (2 upgraded)')],
    )
    assert '| OS: Updated (2 upgraded)' in result


def test_os_status_hidden_when_empty_string(template):
    result = render_briefing(
        template,
        fleet_node_data=[node()],
        fleet_lxc_data=[lxc(os='')],
    )
    assert '| OS:' not in result


def test_os_status_hidden_when_none_string(template):
    # Guards against the None→'None' coercion documented in CLAUDE.md
    result = render_briefing(
        template,
        fleet_node_data=[node()],
        fleet_lxc_data=[lxc(os='None')],
    )
    assert '| OS:' not in result


def test_no_snap_flag(template):
    result = render_briefing(
        template,
        fleet_node_data=[node()],
        fleet_lxc_data=[lxc(snap=False)],
    )
    assert '*(no snap)*' in result


def test_snap_present_no_flag(template):
    result = render_briefing(
        template,
        fleet_node_data=[node()],
        fleet_lxc_data=[lxc(snap=True)],
    )
    assert '*(no snap)*' not in result


def test_no_container_changes_shown(template):
    result = render_briefing(
        template,
        fleet_node_data=[node()],
        fleet_lxc_data=[],
        fleet_vm_data=[],
    )
    assert '*No container changes.*' in result


def test_ansible_manager_no_container_list(template):
    result = render_briefing(
        template,
        fleet_node_data=[node(node='Ansible-Manager', status='OK')],
    )
    assert 'Ansible-Manager: (OK)' in result
    assert '*No container changes.*' not in result


def test_vm_entry_shown(template):
    result = render_briefing(
        template,
        fleet_node_data=[node()],
        fleet_vm_data=[dict(node='pve-01', vmid='200', name='my-vm', status='UPDATED')],
    )
    assert '- VM 200 my-vm — UPDATED' in result


def test_remote_hosts_section_shown(template):
    result = render_briefing(
        template,
        fleet_remote_data=[dict(host='my-server', status='UPDATED & REBOOTED')],
    )
    assert '**Remote Hosts**' in result
    assert '- my-server — UPDATED & REBOOTED' in result


def test_remote_hosts_section_absent_when_empty(template):
    result = render_briefing(template)
    assert '**Remote Hosts**' not in result


def test_error_log_section_shown(template):
    result = render_briefing(
        template,
        fleet_error_log=[dict(
            host='pve-01/lxc-101',
            task='App update 101',
            error='Permission denied',
        )],
    )
    assert '**Error Log:**' in result
    assert 'pve-01/lxc-101' in result
    assert 'Permission denied' in result


def test_error_log_section_absent_when_empty(template):
    result = render_briefing(template)
    assert '**Error Log:**' not in result


def test_multiple_nodes_both_appear(template):
    result = render_briefing(
        template,
        fleet_node_data=[node('pve-01'), node('pve-02')],
        fleet_lxc_data=[
            lxc(node='pve-01', name='sonarr', id='101'),
            lxc(node='pve-02', name='radarr', id='102'),
        ],
    )
    assert 'pve-01' in result
    assert 'pve-02' in result
    assert 'sonarr' in result
    assert 'radarr' in result


def test_custom_systems_section_shown(template):
    result = render_briefing(
        template,
        fleet_custom_data=[dict(host='nas-01', name='Gitea', app='Updated: 1.20 → 1.21')],
    )
    assert '**Custom Systems**' in result
    assert '- **Gitea** (nas-01) — Updated: 1.20 → 1.21' in result


def test_custom_systems_section_absent_when_empty(template):
    result = render_briefing(template)
    assert '**Custom Systems**' not in result


def test_custom_systems_multiple_entries(template):
    result = render_briefing(
        template,
        fleet_custom_data=[
            dict(host='nas-01', name='Gitea', app='Updated: 1.20 → 1.21'),
            dict(host='nas-02', name='Grafana', app='OK'),
        ],
    )
    assert 'Gitea' in result
    assert 'Grafana' in result


def test_lxc_filtered_per_node(template):
    # sonarr is on pve-01, radarr is on pve-02 — they should not bleed across nodes.
    result = render_briefing(
        template,
        fleet_node_data=[node('pve-01'), node('pve-02')],
        fleet_lxc_data=[
            lxc(node='pve-01', name='sonarr', id='101'),
            lxc(node='pve-02', name='radarr', id='102'),
        ],
    )
    lines = result.splitlines()
    pve01_idx = next(i for i, l in enumerate(lines) if 'pve-01' in l)
    pve02_idx = next(i for i, l in enumerate(lines) if 'pve-02' in l)
    pve01_section = '\n'.join(lines[pve01_idx:pve02_idx])
    pve02_section = '\n'.join(lines[pve02_idx:])
    assert 'sonarr' in pve01_section
    assert 'radarr' not in pve01_section
    assert 'radarr' in pve02_section
    assert 'sonarr' not in pve02_section
