"""
Unit tests for the run-summary count expressions in tasks/persist-history.yml.
The file is mostly file I/O; the only logic worth testing is the count assembly.
"""
import pytest
from tests.conftest import make_native_env

COUNTS = """\
{{ {
  'lxc': (fleet_lxc_data | default([])) | length,
  'vm': (fleet_vm_data | default([])) | length,
  'remote': (fleet_remote_data | default([])) | length,
  'node': (fleet_node_data | default([])) | length,
  'custom': (fleet_custom_data | default([])) | length,
  'errors': (fleet_error_log | default([])) | length,
  'warnings': (fleet_warning_log | default([])) | length
} }}"""


def rn(**ctx):
    return make_native_env().from_string(COUNTS).render(**ctx)


def test_counts_all_empty():
    assert rn() == {'lxc': 0, 'vm': 0, 'remote': 0, 'node': 0,
                    'custom': 0, 'errors': 0, 'warnings': 0}


def test_counts_populated():
    result = rn(
        fleet_lxc_data=[{'id': '1'}, {'id': '2'}],
        fleet_custom_data=[{'name': 'gitea'}],
        fleet_warning_log=[{'warning': 'snap failed'}],
    )
    assert result['lxc'] == 2
    assert result['custom'] == 1
    assert result['warnings'] == 1
    assert result['vm'] == 0
