"""
Pytest configuration and shared utilities for Jinja2 template unit tests.

Provides an Ansible-compatible Jinja2 environment — the subset of Ansible's
filters and tests needed to render the expressions under test.
"""
import re
import pytest
import jinja2
from jinja2.nativetypes import NativeEnvironment


# ---------------------------------------------------------------------------
# Custom filters mirroring Ansible's behaviour
# ---------------------------------------------------------------------------

def _regex_search(value, pattern, *groups):
    """Ansible-compatible regex_search.

    With capture group args (e.g. '\\1'), returns a list of matched groups.
    Without args, returns the full match string.
    Returns None on no match (so the `(... or [''])[0]` idiom works).
    """
    m = re.search(pattern, str(value))
    if m is None:
        return None
    if groups:
        return [m.group(i + 1) for i in range(len(groups))]
    return m.group(0)


def _regex_replace(value, pattern, replacement=''):
    return re.sub(pattern, replacement, str(value))


def _bool_filter(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ('true', 'yes', '1', 'on')
    if value is None:
        return False
    return bool(value)


def _trim(value):
    if value is None or isinstance(value, jinja2.Undefined):
        return ''
    return str(value).strip()


def _lower(value):
    return str(value).lower()


def _intersect(a, b):
    """Ansible-compatible intersect filter — elements of a also present in b."""
    bset = list(b or [])
    seen = []
    for x in (a or []):
        if x in bset and x not in seen:
            seen.append(x)
    return seen


def _combine(*dicts, recursive=False, **kwargs):
    """Ansible-compatible combine filter — merges dicts left to right.

    Supports recursive=True for deep merge (matching Ansible's combine).
    """
    recursive = kwargs.get('recursive', recursive)
    result = {}
    for d in dicts:
        if not d:
            continue
        for k, v in d.items():
            if recursive and k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = _combine(result[k], v, recursive=True)
            else:
                result[k] = v
    return result


# ---------------------------------------------------------------------------
# Custom tests mirroring Ansible's behaviour
# ---------------------------------------------------------------------------

def _failed_test(obj):
    """Mirrors Ansible's `is failed` test on a registered result."""
    if isinstance(obj, jinja2.Undefined):
        return False
    if isinstance(obj, dict):
        return bool(obj.get('failed', False))
    return False


def _search_test(value, pattern):
    """Mirrors Ansible's `is search(pattern)` test."""
    return bool(re.search(pattern, str(value)))


def _equalto_test(a, b):
    return a == b


def _succeeded_test(obj):
    """Mirrors Ansible's `is succeeded` / `is success` test."""
    if isinstance(obj, jinja2.Undefined):
        return False
    if isinstance(obj, dict):
        return not bool(obj.get('failed', False))
    return False


# ---------------------------------------------------------------------------
# Environment factories
# ---------------------------------------------------------------------------

def _configure_env(env):
    """Attach Ansible-compatible filters and tests to a Jinja2 environment."""
    env.filters['regex_search'] = _regex_search
    env.filters['regex_replace'] = _regex_replace
    env.filters['bool'] = _bool_filter
    env.filters['trim'] = _trim
    env.filters['lower'] = _lower
    env.filters['combine'] = _combine
    env.filters['intersect'] = _intersect
    env.tests['failed'] = _failed_test
    env.tests['search'] = _search_test
    env.tests['equalto'] = _equalto_test
    env.tests['succeeded'] = _succeeded_test
    env.tests['success'] = _succeeded_test
    return env


def make_jinja2_env():
    """Standard Jinja2 environment — renders to string."""
    return _configure_env(jinja2.Environment(undefined=jinja2.Undefined))


def make_native_env():
    """NativeEnvironment — renders to Python objects (used for fleet-state tests)."""
    return _configure_env(NativeEnvironment(undefined=jinja2.Undefined))


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def render(template_str, **context):
    """Render a template string to a stripped string using the Ansible-compat env."""
    env = make_jinja2_env()
    return env.from_string(template_str).render(**context).strip()


def render_native(template_str, **context):
    """Render a template string and return the native Python object."""
    env = make_native_env()
    return env.from_string(template_str).render(**context)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def make_result(*, failed=False, changed=False, stdout='', rc=0, skipped=False):
    """Build a dict that mimics an Ansible registered task result."""
    return {
        'failed': failed,
        'changed': changed,
        'stdout': stdout,
        'rc': rc,
        'skipped': skipped,
    }


class AnsibleHostVars(dict):
    """Dict subclass that returns Undefined for missing keys, mirroring Ansible's hostvars."""
    def __missing__(self, key):
        return jinja2.Undefined(name=key)


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env():
    return make_jinja2_env()


@pytest.fixture
def native_env():
    return make_native_env()
