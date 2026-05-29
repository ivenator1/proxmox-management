"""
Unit tests for the notifier-dispatch expressions in fleet-update.yml (Phase 4
back-compat shim) and tasks/notify.yml (ntfy header construction).
"""
import pytest
from tests.conftest import make_native_env

# Back-compat shim from fleet-update.yml Phase 4
NOTIFIERS_SHIM = """\
{{ notifiers if notifiers is defined
   else ([{'type': 'discord', 'enabled': true, 'webhook': discord_webhook}]
         if (discord_webhook is defined and discord_webhook | default('') | trim != '')
         else []) }}"""

# ntfy headers expression from tasks/notify.yml
NTFY_HEADERS = """\
{{ {'Title': _ntfy_title,
    'Priority': (notifier.priority | default(5 if fleet_failed | default(false) else 3)) | string,
    'Tags': (notifier.tags | default('warning' if fleet_failed | default(false) else 'white_check_mark')),
    'Markdown': 'yes'}
   | combine({'Authorization': 'Bearer ' ~ notifier.token}
             if (notifier.token is defined and notifier.token | default('') | trim != '') else {}) }}"""


def rn(expr, **ctx):
    return make_native_env().from_string(expr).render(**ctx)


# --- back-compat shim ---

def test_explicit_notifiers_passed_through():
    notifiers = [{'type': 'ntfy', 'enabled': True, 'url': 'https://ntfy.sh/x'}]
    assert rn(NOTIFIERS_SHIM, notifiers=notifiers, discord_webhook='ignored') == notifiers


def test_shim_synthesizes_discord_from_webhook():
    result = rn(NOTIFIERS_SHIM, discord_webhook='https://discord.test/hook')
    assert result == [{'type': 'discord', 'enabled': True, 'webhook': 'https://discord.test/hook'}]


def test_shim_empty_when_no_webhook_and_no_notifiers():
    assert rn(NOTIFIERS_SHIM, discord_webhook='') == []


# --- ntfy headers ---

def test_ntfy_headers_defaults_on_failure():
    h = rn(NTFY_HEADERS, _ntfy_title='Fleet Update: Failures Detected',
           fleet_failed=True, notifier={'type': 'ntfy', 'url': 'x'})
    assert h['Title'] == 'Fleet Update: Failures Detected'
    assert h['Priority'] == '5'
    assert h['Tags'] == 'warning'
    assert h['Markdown'] == 'yes'
    assert 'Authorization' not in h


def test_ntfy_headers_defaults_on_success():
    h = rn(NTFY_HEADERS, _ntfy_title='Fleet Update: All Systems Clear',
           fleet_failed=False, notifier={'type': 'ntfy', 'url': 'x'})
    assert h['Priority'] == '3'
    assert h['Tags'] == 'white_check_mark'


def test_ntfy_headers_explicit_overrides():
    h = rn(NTFY_HEADERS, _ntfy_title='t', fleet_failed=False,
           notifier={'type': 'ntfy', 'url': 'x', 'priority': 4, 'tags': 'package'})
    assert h['Priority'] == '4'
    assert h['Tags'] == 'package'


def test_ntfy_headers_authorization_when_token():
    h = rn(NTFY_HEADERS, _ntfy_title='t', fleet_failed=False,
           notifier={'type': 'ntfy', 'url': 'x', 'token': 'tk_abc'})
    assert h['Authorization'] == 'Bearer tk_abc'
