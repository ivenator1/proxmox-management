"""Tests for proxmox_fleet.models.config — the typed replacement for the
assert chain in roles/custom_update/tasks/load_config.yml.

These import plain Python (no Jinja shim).
"""
import glob
import os

import pytest
import yaml
from pydantic import ValidationError

from proxmox_fleet.models.config import (
    ChangedWhen,
    CustomConfig,
    HealthCheck,
    LatestVersion,
    UpdateStep,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _minimal(**overrides):
    base = {"name": "app", "update_steps": [{"name": "s", "command": "true"}]}
    base.update(overrides)
    return base


# --- top-level required fields -------------------------------------------------

def test_minimal_config_validates():
    cfg = CustomConfig.model_validate(_minimal())
    assert cfg.name == "app"
    assert cfg.update_steps[0].command == "true"


def test_missing_name_rejected():
    with pytest.raises(ValidationError):
        CustomConfig.model_validate({"update_steps": [{"name": "s", "command": "true"}]})


def test_blank_name_rejected():
    with pytest.raises(ValidationError):
        CustomConfig.model_validate(_minimal(name="   "))


def test_missing_update_steps_rejected():
    with pytest.raises(ValidationError):
        CustomConfig.model_validate({"name": "app"})


def test_empty_update_steps_rejected():
    with pytest.raises(ValidationError):
        CustomConfig.model_validate(_minimal(update_steps=[]))


def test_step_needs_name_and_command():
    with pytest.raises(ValidationError):
        UpdateStep.model_validate({"name": "s"})
    with pytest.raises(ValidationError):
        UpdateStep.model_validate({"name": "", "command": "true"})
    with pytest.raises(ValidationError):
        UpdateStep.model_validate({"name": "s", "command": "  "})


# --- enum / conditional fields -------------------------------------------------

def test_changed_when_command_requires_command():
    ChangedWhen.model_validate({"type": "version"})
    ChangedWhen.model_validate({"type": "command", "command": "test -f /x"})
    with pytest.raises(ValidationError):
        ChangedWhen.model_validate({"type": "command"})


def test_health_check_kuma_requires_slug_and_monitor_id():
    HealthCheck.model_validate({"type": "kuma", "kuma_slug": "g", "kuma_monitor_id": "7"})
    with pytest.raises(ValidationError):
        HealthCheck.model_validate({"type": "kuma", "kuma_slug": "g"})
    with pytest.raises(ValidationError):
        HealthCheck.model_validate({"type": "kuma", "kuma_monitor_id": "7"})


def test_health_check_monitor_id_int_coerced_to_str():
    hc = HealthCheck.model_validate({"type": "kuma", "kuma_slug": "g", "kuma_monitor_id": 7})
    assert hc.kuma_monitor_id == "7"


def test_health_check_http_requires_url():
    HealthCheck.model_validate({"type": "http", "url": "http://x"})
    with pytest.raises(ValidationError):
        HealthCheck.model_validate({"type": "http"})


def test_health_check_command_requires_command():
    with pytest.raises(ValidationError):
        HealthCheck.model_validate({"type": "command"})


def test_latest_version_github_requires_repo():
    LatestVersion.model_validate({"type": "github_release", "repo": "a/b"})
    with pytest.raises(ValidationError):
        LatestVersion.model_validate({"type": "github_release"})


def test_latest_version_command_requires_command():
    with pytest.raises(ValidationError):
        LatestVersion.model_validate({"type": "command"})


def test_bad_enum_rejected():
    with pytest.raises(ValidationError):
        ChangedWhen.model_validate({"type": "bogus"})


# --- outdated gate -------------------------------------------------------------

def test_outdated_gate_requires_version_command_and_source():
    with pytest.raises(ValidationError):
        CustomConfig.model_validate(_minimal(update_only_if_outdated=True))
    with pytest.raises(ValidationError):
        CustomConfig.model_validate(_minimal(
            update_only_if_outdated=True, version_command="v --version"))
    # valid: has both version_command and a latest_version source
    CustomConfig.model_validate(_minimal(
        update_only_if_outdated=True,
        version_command="v --version",
        latest_version={"type": "github_release", "repo": "a/b"},
    ))


# --- passthrough preservation --------------------------------------------------

def test_extra_step_keys_preserved():
    cfg = CustomConfig.model_validate(_minimal(
        update_steps=[{"name": "s", "command": "true", "some_future_key": 1}]))
    assert cfg.update_steps[0].model_dump().get("some_future_key") == 1


def test_command_kept_opaque():
    # A command that *looks* like a Jinja template must survive untouched.
    raw = "deploy --token {{ vault_token }} && echo {{ custom_step_results.x }}"
    cfg = CustomConfig.model_validate(_minimal(update_steps=[{"name": "s", "command": raw}]))
    assert cfg.update_steps[0].command == raw


# --- worked examples validate --------------------------------------------------

@pytest.mark.parametrize("path", glob.glob(os.path.join(REPO_ROOT, "configs", "*.yml.example")))
def test_shipped_example_configs_validate(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    CustomConfig.model_validate(data)
