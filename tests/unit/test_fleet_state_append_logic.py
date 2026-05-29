"""
Unit tests for the set_fact expressions in tasks/fleet-state-append.yml.

Uses NativeEnvironment to get Python objects back instead of strings,
and AnsibleHostVars to simulate the `hostvars['localhost']` proxy that
returns Undefined for missing keys (matching Ansible's behaviour).
"""
import jinja2
import pytest
from tests.conftest import make_native_env, AnsibleHostVars

# Expressions extracted from tasks/fleet-state-append.yml

LXC_DATA_EXPR = (
    "{{ hostvars['localhost']['fleet_lxc_data'] | default([]) "
    "+ ([fleet_record_payload] if fleet_record_type == 'lxc' else []) }}"
)
VM_DATA_EXPR = (
    "{{ hostvars['localhost']['fleet_vm_data'] | default([]) "
    "+ ([fleet_record_payload] if fleet_record_type == 'vm' else []) }}"
)
REMOTE_DATA_EXPR = (
    "{{ hostvars['localhost']['fleet_remote_data'] | default([]) "
    "+ ([fleet_record_payload] if fleet_record_type == 'remote_host' else []) }}"
)
NODE_DATA_EXPR = (
    "{{ hostvars['localhost']['fleet_node_data'] | default([]) "
    "+ ([fleet_record_payload] if fleet_record_type in ['node', 'manager'] else []) }}"
)
CUSTOM_DATA_EXPR = (
    "{{ hostvars['localhost']['fleet_custom_data'] | default([]) "
    "+ ([fleet_record_payload] if fleet_record_type == 'custom' else []) }}"
)

CHANGED_EXPR = (
    "{{ (hostvars['localhost']['fleet_changed'] | default(false)) "
    "or (fleet_record_changed | default(false) | bool) }}"
)
FAILED_EXPR = (
    "{{ (hostvars['localhost']['fleet_failed'] | default(false)) "
    "or (fleet_record_failed | default(false) | bool) }}"
)
ERROR_LOG_EXPR = (
    "{{ hostvars['localhost']['fleet_error_log'] | default([]) "
    "+ ([fleet_error_detail] if fleet_error_detail is defined else []) }}"
)


@pytest.fixture
def env():
    return make_native_env()


def r(env, expr, **ctx):
    return env.from_string(expr).render(**ctx)


def empty_hostvars():
    return {'localhost': AnsibleHostVars()}


def populated_hostvars(**overrides):
    h = AnsibleHostVars(
        fleet_lxc_data=[],
        fleet_vm_data=[],
        fleet_remote_data=[],
        fleet_node_data=[],
        fleet_changed=False,
        fleet_failed=False,
        fleet_error_log=[],
    )
    h.update(overrides)
    return {'localhost': h}


# --- fleet_lxc_data ---

def test_initial_lxc_state(env):
    result = r(env, LXC_DATA_EXPR,
               hostvars=empty_hostvars(),
               fleet_record_type='lxc',
               fleet_record_payload={'id': '101', 'name': 'sonarr'})
    assert result == [{'id': '101', 'name': 'sonarr'}]


def test_lxc_accumulation(env):
    h = populated_hostvars(fleet_lxc_data=[{'id': '101'}])
    result = r(env, LXC_DATA_EXPR,
               hostvars=h,
               fleet_record_type='lxc',
               fleet_record_payload={'id': '102', 'name': 'radarr'})
    assert len(result) == 2
    assert result[0]['id'] == '101'
    assert result[1]['id'] == '102'


def test_lxc_type_does_not_pollute_vm(env):
    result = r(env, VM_DATA_EXPR,
               hostvars=empty_hostvars(),
               fleet_record_type='lxc',
               fleet_record_payload={'id': '101'})
    assert result == []


# --- fleet_vm_data ---

def test_vm_type_goes_to_vm_list(env):
    result = r(env, VM_DATA_EXPR,
               hostvars=empty_hostvars(),
               fleet_record_type='vm',
               fleet_record_payload={'vmid': '200', 'name': 'my-vm'})
    assert result == [{'vmid': '200', 'name': 'my-vm'}]


# --- fleet_remote_data ---

def test_remote_host_type_goes_to_remote_list(env):
    result = r(env, REMOTE_DATA_EXPR,
               hostvars=empty_hostvars(),
               fleet_record_type='remote_host',
               fleet_record_payload={'host': 'server1', 'status': 'UPDATED'})
    assert result == [{'host': 'server1', 'status': 'UPDATED'}]


# --- fleet_node_data ---

def test_node_type_goes_to_node_list(env):
    result = r(env, NODE_DATA_EXPR,
               hostvars=empty_hostvars(),
               fleet_record_type='node',
               fleet_record_payload={'node': 'pve-01', 'status': 'OK'})
    assert result == [{'node': 'pve-01', 'status': 'OK'}]


def test_custom_type_goes_to_custom_list(env):
    result = r(env, CUSTOM_DATA_EXPR,
               hostvars=empty_hostvars(),
               fleet_record_type='custom',
               fleet_record_payload={'host': 'nas-01', 'name': 'Gitea', 'app': 'Updated: 1.20 → 1.21'})
    assert result == [{'host': 'nas-01', 'name': 'Gitea', 'app': 'Updated: 1.20 → 1.21'}]


def test_custom_type_does_not_pollute_lxc(env):
    result = r(env, LXC_DATA_EXPR,
               hostvars=empty_hostvars(),
               fleet_record_type='custom',
               fleet_record_payload={'host': 'nas-01', 'name': 'Gitea'})
    assert result == []


def test_lxc_type_does_not_pollute_custom(env):
    result = r(env, CUSTOM_DATA_EXPR,
               hostvars=empty_hostvars(),
               fleet_record_type='lxc',
               fleet_record_payload={'id': '101', 'name': 'sonarr'})
    assert result == []


def test_manager_type_goes_to_node_list(env):
    result = r(env, NODE_DATA_EXPR,
               hostvars=empty_hostvars(),
               fleet_record_type='manager',
               fleet_record_payload={'node': 'Ansible-Manager', 'status': 'OK'})
    assert result == [{'node': 'Ansible-Manager', 'status': 'OK'}]


# --- fleet_changed ---

def test_changed_false_stays_false(env):
    result = r(env, CHANGED_EXPR,
               hostvars=populated_hostvars(fleet_changed=False),
               fleet_record_changed=False)
    assert result is False


def test_changed_new_true_sets_true(env):
    result = r(env, CHANGED_EXPR,
               hostvars=populated_hostvars(fleet_changed=False),
               fleet_record_changed=True)
    assert result is True


def test_changed_existing_true_preserved(env):
    result = r(env, CHANGED_EXPR,
               hostvars=populated_hostvars(fleet_changed=True),
               fleet_record_changed=False)
    assert result is True


def test_changed_default_when_not_provided(env):
    result = r(env, CHANGED_EXPR,
               hostvars=empty_hostvars())
    assert result is False


# --- fleet_failed ---

def test_failed_or_logic(env):
    result = r(env, FAILED_EXPR,
               hostvars=populated_hostvars(fleet_failed=False),
               fleet_record_failed=True)
    assert result is True


def test_failed_preserved(env):
    result = r(env, FAILED_EXPR,
               hostvars=populated_hostvars(fleet_failed=True),
               fleet_record_failed=False)
    assert result is True


# --- fleet_error_log ---

def test_error_log_appended_when_detail_provided(env):
    detail = {'host': 'pve-01', 'task': 'Update', 'error': 'Timeout'}
    result = r(env, ERROR_LOG_EXPR,
               hostvars=empty_hostvars(),
               fleet_error_detail=detail)
    assert result == [detail]


def test_error_log_not_appended_when_detail_absent(env):
    result = r(env, ERROR_LOG_EXPR,
               hostvars=populated_hostvars(fleet_error_log=[{'host': 'existing'}]))
    assert result == [{'host': 'existing'}]


def test_error_log_accumulates(env):
    existing = [{'host': 'pve-01', 'task': 't1', 'error': 'err1'}]
    new_detail = {'host': 'pve-02', 'task': 't2', 'error': 'err2'}
    result = r(env, ERROR_LOG_EXPR,
               hostvars=populated_hostvars(fleet_error_log=existing),
               fleet_error_detail=new_detail)
    assert len(result) == 2
    assert result[1] == new_detail
