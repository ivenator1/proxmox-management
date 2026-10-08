"""Unit coverage for transport-independent Alloy compliance policy."""

from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any

import pytest

from proxmox_fleet.runner import PrimitiveResult

alloy_mod: Any = importlib.import_module("proxmox_fleet.alloy")
AlloyConfigError = alloy_mod.AlloyConfigError
DesiredAlloyConfig = alloy_mod.DesiredAlloyConfig
load_desired_config = alloy_mod.load_desired_config
parse_probe = alloy_mod.parse_probe
reconcile_alloy = alloy_mod.reconcile_alloy
render_lxc_log_config = alloy_mod.render_lxc_log_config


def _desired(content: str = "logging {}\n") -> DesiredAlloyConfig:
    return DesiredAlloyConfig(
        content=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def _facts(desired: DesiredAlloyConfig, **overrides: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "binary_present": True,
        "package_manager": "apt",
        "config_sha256": desired.sha256,
        "journal_member": True,
        "service_enabled": True,
        "service_active": True,
    }
    facts.update(overrides)
    return facts


def _probe_result(facts: dict[str, object]) -> PrimitiveResult:
    return PrimitiveResult(rc=0, facts=facts)


class AlloyExecutor:
    host = "guest"

    def __init__(
        self,
        probes: list[PrimitiveResult],
        deploy: PrimitiveResult | None = None,
    ) -> None:
        self.probes = list(probes)
        self.deploy = deploy or PrimitiveResult(rc=0, changed=True)
        self.reconcile_calls: list[dict[str, object]] = []
        self.probe_lxc_ids: list[str | None] = []

    def alloy_probe(self, *, lxc_id: str | None = None) -> PrimitiveResult:
        self.probe_lxc_ids.append(lxc_id)
        return self.probes.pop(0)

    def alloy_reconcile(self, **kwargs: object) -> PrimitiveResult:
        self.reconcile_calls.append(kwargs)
        return self.deploy


def test_desired_config_loads_and_hashes_exact_content(tmp_path):
    path = tmp_path / "guest.alloy"
    path.write_text("loki.write \"default\" {}\n", encoding="utf-8")
    desired = load_desired_config(path)
    assert desired.content == "loki.write \"default\" {}\n"
    assert desired.sha256 == hashlib.sha256(desired.content.encode()).hexdigest()


@pytest.mark.parametrize("content", ["", "   \n", "url = \"http://REPLACE_WITH_LOKI_ENDPOINT:3100\"\n"])
def test_desired_config_rejects_empty_or_placeholder(tmp_path, content):
    path = tmp_path / "guest.alloy"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(AlloyConfigError):
        load_desired_config(path)


def test_desired_config_rejects_missing_file(tmp_path):
    with pytest.raises(AlloyConfigError, match="cannot read"):
        load_desired_config(tmp_path / "missing.alloy")


def test_parse_probe_coerces_ansible_string_booleans():
    probe = parse_probe({
        "binary_present": "true",
        "package_manager": " APT ",
        "config_sha256": "ABC",
        "journal_member": "yes",
        "service_enabled": "1",
        "service_active": False,
    })
    assert probe.binary_present is True
    assert probe.package_manager == "apt"
    assert probe.config_sha256 == "abc"
    assert probe.journal_member is True
    assert probe.service_enabled is True
    assert probe.service_active is False


def test_apt_missing_install_reconciles_every_dimension():
    desired = _desired()
    before = _facts(
        desired,
        binary_present=False,
        config_sha256="",
        journal_member=False,
        service_enabled=False,
        service_active=False,
    )
    ex = AlloyExecutor([_probe_result(before), _probe_result(_facts(desired))])
    result = reconcile_alloy(ex, desired, lxc_id="101")
    assert result.status == "Installed"
    assert result.changed is True
    assert result.warning is None
    assert ex.probe_lxc_ids == ["101", "101"]
    assert ex.reconcile_calls == [{
        "lxc_id": "101",
        "desired_content": desired.content,
        "install": True,
        "configure": True,
        "add_journal_group": True,
        "repair_service": True,
    }]


def test_config_only_drift_validates_and_restarts_without_install():
    desired = _desired()
    before = _facts(desired, config_sha256="old")
    ex = AlloyExecutor([_probe_result(before), _probe_result(_facts(desired))])
    result = reconcile_alloy(ex, desired)
    assert result.status == "Configured"
    call = ex.reconcile_calls[0]
    assert call["install"] is False
    assert call["configure"] is True
    assert call["add_journal_group"] is False
    assert call["repair_service"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"journal_member": False},
        {"service_enabled": False},
        {"service_active": False},
    ],
)
def test_service_or_group_drift_is_classified_as_service_repair(overrides):
    desired = _desired()
    ex = AlloyExecutor([
        _probe_result(_facts(desired, **overrides)),
        _probe_result(_facts(desired)),
    ])
    result = reconcile_alloy(ex, desired)
    assert result.status == "Service repaired"
    assert result.changed is True


def test_compliant_guest_is_silent_noop():
    desired = _desired()
    ex = AlloyExecutor([_probe_result(_facts(desired))])
    result = reconcile_alloy(ex, desired)
    assert result.status is None
    assert result.changed is False
    assert result.warning is None
    assert ex.reconcile_calls == []


def test_dry_run_audits_without_mutation():
    desired = _desired()
    ex = AlloyExecutor([_probe_result(_facts(desired, service_active=False))])
    result = reconcile_alloy(ex, desired, dry_run=True)
    assert result.changed is False
    assert "audit only" in str(result.warning)
    assert ex.reconcile_calls == []


def test_missing_non_apt_guest_warns_without_install_attempt():
    desired = _desired()
    ex = AlloyExecutor([
        _probe_result(_facts(desired, binary_present=False, package_manager="dnf"))
    ])
    result = reconcile_alloy(ex, desired)
    assert "installation is unavailable" in str(result.warning)
    assert ex.reconcile_calls == []


def test_preinstalled_non_apt_guest_still_reconciles_config():
    desired = _desired()
    ex = AlloyExecutor([
        _probe_result(_facts(desired, package_manager="dnf", config_sha256="old")),
        _probe_result(_facts(desired, package_manager="dnf")),
    ])
    result = reconcile_alloy(ex, desired)
    assert result.status == "Configured"
    assert ex.reconcile_calls[0]["install"] is False


def test_staged_config_validation_failure_preserves_unresolved_warning():
    desired = _desired()
    before = _facts(desired, config_sha256="old")
    deploy = PrimitiveResult(rc=2, failed=True, stderr="configuration validation failed")
    ex = AlloyExecutor([_probe_result(before), _probe_result(before)], deploy=deploy)
    result = reconcile_alloy(ex, desired)
    assert result.status is None
    assert "configuration validation failed" in str(result.warning)
    assert "configuration differs" in str(result.warning)


def test_final_service_verification_failure_is_attention_not_exception():
    desired = _desired()
    before = _facts(desired, service_active=False)
    final = _facts(desired, service_active=False)
    ex = AlloyExecutor([_probe_result(before), _probe_result(final)])
    result = reconcile_alloy(ex, desired)
    assert result.changed is True
    assert "service is not active" in str(result.warning)


_GUEST_BASE = (
    'loki.relabel "journal" {\n'
    "  forward_to = []\n"
    "}\n"
    "\n"
    'loki.source.journal "journal" {\n'
    "  forward_to    = [loki.write.default.receiver]\n"
    "  relabel_rules = loki.relabel.journal.rules\n"
    '  labels        = { job = "systemd-journal", role = "guest" }\n'
    '  max_age       = coalesce(sys.env("FLEET_JOURNAL_MAX_AGE"), "48h")\n'
    "}\n"
    "\n"
    'loki.write "default" {\n'
    "  endpoint {\n"
    '    url = "http://10.0.0.1:3100/loki/api/v1/push"\n'
    "  }\n"
    "}\n"
)


def _render(profiles: set[str], **overrides: object) -> Any:
    kwargs: dict[str, object] = {
        "node": "pve1",
        "cluster": "main",
        "lxc_id": "123",
        "name": "npm",
        "profiles": profiles,
        "retention_hours": 48,
    }
    kwargs.update(overrides)
    return render_lxc_log_config(_desired(_GUEST_BASE), **kwargs)


def test_render_appends_npm_source_and_preserves_base_verbatim():
    base = _desired(_GUEST_BASE)
    result = render_lxc_log_config(
        base,
        node="pve1",
        cluster="main",
        lxc_id="123",
        name="npm",
        profiles={"npm"},
        retention_hours=48,
    )
    assert result.content.startswith(_GUEST_BASE)
    assert result.content != base.content
    assert result.sha256 == hashlib.sha256(result.content.encode("utf-8")).hexdigest()
    # base journal component names and storage config are untouched
    assert 'loki.source.journal "journal"' in result.content
    assert 'loki.write "default"' in result.content
    # only the npm source plus the shared packer are generated
    assert 'loki.source.file "fleet_npm"' in result.content
    assert 'loki.process "fleet_file_pack"' in result.content
    assert 'loki.source.file "fleet_pbs_tasks"' not in result.content
    assert alloy_mod.NPM_LIVE_LOG_GLOB in result.content


def test_render_npm_labels_and_quarantine_exclusion():
    result = _render({"npm"})
    expected = {
        "job": "lxc-file",
        "delivery": "live",
        "cluster": "main",
        "node": "pve1",
        "guest_id": "123",
        "host": "npm",
        "app": "nginxproxymanager",
        "log_kind": "application",
    }
    for key, value in expected.items():
        assert f"{key} = {json.dumps(value)}" in result.content
    quarantine = f"/data/logs/**/{alloy_mod.QUARANTINE_DIRNAME}/**"
    assert quarantine in result.content
    # filename is packed into the JSON body, never an indexed label
    assert "filename = " not in result.content
    assert json.dumps("filename") in result.content


def test_render_pbs_covers_tasks_and_api_sources():
    result = _render({"pbs"})
    assert 'loki.source.file "fleet_pbs_tasks"' in result.content
    assert 'loki.source.file "fleet_pbs_api"' in result.content
    assert 'loki.source.file "fleet_npm"' not in result.content
    assert alloy_mod.PBS_LIVE_TASKS_GLOB in result.content
    for api_path in alloy_mod.PBS_LIVE_API_GLOBS:
        assert api_path in result.content
    assert f"/var/log/proxmox-backup/tasks/**/{alloy_mod.QUARANTINE_DIRNAME}/**" in result.content
    assert f'app = {json.dumps("proxmox-backup")}' in result.content
    assert f"log_kind = {json.dumps('task')}" in result.content
    assert f"log_kind = {json.dumps('api')}" in result.content
    # UPID identity stays inside the glob only, never an indexed label
    assert "UPID = " not in result.content


def test_render_both_profiles_generates_every_source():
    result = _render({"npm", "pbs"})
    for component in ("fleet_npm", "fleet_pbs_tasks", "fleet_pbs_api"):
        assert f'loki.source.file "{component}"' in result.content


def test_render_retention_hours_drive_ignore_older_than():
    result = _render({"npm"}, retention_hours=24)
    assert json.dumps("24h") in result.content
    assert json.dumps("12h") not in result.content


def test_render_without_discovered_profiles_returns_base_unchanged():
    base = _desired(_GUEST_BASE)
    result = render_lxc_log_config(
        base,
        node="pve1",
        cluster="main",
        lxc_id="123",
        name="npm",
        profiles=set(),
        retention_hours=48,
    )
    assert result.content == base.content
    assert result.sha256 == base.sha256


def test_render_rejects_unknown_profiles():
    with pytest.raises(AlloyConfigError, match="unsupported log profile"):
        _render({"nginx"})
    with pytest.raises(AlloyConfigError, match="unsupported log profile"):
        _render({"npm", "pbs", "docker"})


def test_render_rejects_reserved_component_conflicts():
    for snippet in (
        'loki.process "fleet_file_pack" {}',
        'loki.source.file "fleet_npm" {}',
        'loki.source.file "fleet_pbs_api" {}',
    ):
        base = _desired(_GUEST_BASE + "\n" + snippet + "\n")
        with pytest.raises(AlloyConfigError, match="reserved"):
            render_lxc_log_config(
                base,
                node="pve1",
                cluster="main",
                lxc_id="123",
                name="npm",
                profiles={"npm"},
                retention_hours=48,
            )


def test_render_refuses_to_double_render_generated_config():
    first = _render({"npm"})
    with pytest.raises(AlloyConfigError, match="reserved"):
        render_lxc_log_config(
            DesiredAlloyConfig(content=first.content, sha256=first.sha256),
            node="pve1",
            cluster="main",
            lxc_id="123",
            name="npm",
            profiles={"npm"},
            retention_hours=48,
        )


def test_render_escapes_hostile_label_values():
    hostile = 'na"me\\path\nnext'
    cluster = 'we"ird'
    result = _render({"npm"}, name=hostile, cluster=cluster)
    assert json.dumps(hostile) in result.content
    assert hostile not in result.content
    assert json.dumps(cluster) in result.content
    assert cluster not in result.content
    # escaping must keep the appended HCL structurally balanced
    assert result.content.count("{") == result.content.count("}")


def test_render_handles_base_without_trailing_newline():
    base = _desired('loki.write "default" {}')
    result = render_lxc_log_config(
        base,
        node="pve1",
        cluster="main",
        lxc_id="123",
        name="npm",
        profiles={"npm"},
        retention_hours=48,
    )
    assert result.content.startswith(base.content)
    assert 'loki.source.file "fleet_npm"' in result.content
