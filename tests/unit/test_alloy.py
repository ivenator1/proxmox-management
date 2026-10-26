"""Unit coverage for transport-independent Alloy compliance policy."""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import replace
from typing import Any

import pytest

from proxmox_fleet import log_plugins
from proxmox_fleet.models.log_plugins import GuestLogOverride, MultilineOptions
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


def _render(sources: Any = (), **overrides: object) -> Any:
    kwargs: dict[str, object] = {
        "node": "pve1",
        "cluster": "main",
        "lxc_id": "123",
        "name": "npm",
        "sources": tuple(sources),
        "retention_hours": 48,
    }
    kwargs.update(overrides)
    return render_lxc_log_config(_desired(_GUEST_BASE), **kwargs)


_CATALOGUE = log_plugins.load_log_catalogue()

# Exact pre-cutover renderer output for the literal identity below, captured
# before the catalogue/profiles migration (base ``logging {}\n``, retention 168).
_LEGACY_BASELINE_SHA256 = {
    "none": "cb9576f4ac801880f97de9b603eae7be7ff1f76ebd97dd3202dad2c657e7e1ae",
    "npm": "8331c2431701e8ba5b359695d9c3abb3f08cf7880845aa657754aa7ae60c8532",
    "pbs": "f60a0dda210cc84215d7f51163528171967b65899cab073261040682967b5599",
    "npm+pbs": "e4b83a4879656a829bcb6bd29623e650445ee7f9ede9bae086aabf6497441d64",
}


def _active_probe_facts(request: dict[str, Any]) -> dict[str, Any]:
    """Synthesize complete readable/quiet probe facts for a logging request."""
    units: dict[str, Any] = {}

    def _collect(section: dict[str, Any]) -> None:
        for layout in section.get("layouts", []):
            for unit in (
                *layout.get("required_units", []),
                *layout.get("running_units_any", []),
            ):
                units[unit["key"]] = {"status": "active"}
        for unit in section.get("journal_units", []):
            units[unit["key"]] = {"status": "active"}

    for entry in request["plugins"].values():
        _collect(entry)
        for included in entry.get("includes", {}).values():
            _collect(included)

    plugins: dict[str, Any] = {}
    for owner_id, entry in request["plugins"].items():
        payload: dict[str, Any] = {"units": units}
        if entry.get("includes"):
            payload["includes"] = {
                dependency_id: {"units": units}
                for dependency_id in entry["includes"]
            }
        plugins[owner_id] = payload

    sources: dict[str, Any] = {}
    for key, entry in request["sources"].items():
        layouts: dict[str, Any] = {}
        for layout_id, payload in entry["layouts"].items():
            roots = {
                root: {
                    "status": "ok",
                    "exists": True,
                    "is_dir": True,
                    "is_symlink": False,
                    "blocked_ancestor": False,
                    "device": 1,
                    "access": {
                        "traversable": True,
                        "readable": True,
                        "default_acl": True,
                    },
                }
                for root in payload["roots"]
            }
            matches = [
                {
                    "pattern": pattern,
                    "status": "quiet",
                    "matched": 0,
                    "skipped": 0,
                    "truncated": False,
                    "files": [],
                }
                for pattern in payload["paths"]
            ]
            exact_files = {
                path: {
                    "status": "quiet",
                    "exists": False,
                    "is_symlink": False,
                    "parent_traversable": True,
                }
                for path in payload["exact_files"]
            }
            layouts[layout_id] = {
                "roots": roots,
                "matches": matches,
                "exact_files": exact_files,
            }
        sources[key] = {"layouts": layouts}
    return {"plugins": plugins, "sources": sources}


def _resolved_sources(
    script_id: str | None,
    *,
    plugins: tuple[str, ...] | None = None,
) -> Any:
    """Resolve the real built-in catalogue for one identity/manual selection."""
    override = (
        None
        if plugins is None
        else GuestLogOverride(mode="replace", plugins=list(plugins))
    )
    selection = log_plugins.select_log_plugins(
        _CATALOGUE,
        script_id=script_id,
        override=override,
        auto=True,
    )
    request = log_plugins.build_logging_request(selection)
    return log_plugins.validate_log_layout(selection, _active_probe_facts(request))


def _baseline_render(sources: Any) -> Any:
    return render_lxc_log_config(
        _desired("logging {}\n"),
        node="pve01",
        cluster="default",
        lxc_id="123",
        name="legacy-smoke",
        sources=tuple(sources),
        retention_hours=168,
    )


def _source(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "owner_id": "nginxproxymanager",
        "service_id": "nginxproxymanager",
        "source_id": "application",
        "key": "nginxproxymanager/application",
        "layout_id": "default",
        "kind": "app",
        "app": "nginxproxymanager",
        "log_kind": "application",
        "component": "fleet_npm",
        "required": True,
        "multiline": None,
        "paths": ("/data/logs/**/*.log",),
        "exclude_paths": ("/data/logs/**/.fleet-housekeeping-quarantine/**",),
        "roots": ("/data/logs",),
        "exact_files": (),
        "service_user": None,
        "status": "active",
    }
    defaults.update(overrides)
    return log_plugins.ResolvedLogSource(**defaults)


@pytest.mark.parametrize(
    ("script_id", "plugins", "baseline"),
    [
        ("nginxproxymanager", None, "npm"),
        ("proxmox-backup-server", None, "pbs"),
        (None, ("nginxproxymanager", "proxmox-backup-server"), "npm+pbs"),
    ],
)
def test_render_default_selections_match_precutover_baseline(
    script_id, plugins, baseline
):
    resolved = _resolved_sources(script_id, plugins=plugins)
    result = _baseline_render(resolved.renderable_sources)
    assert result.sha256 == _LEGACY_BASELINE_SHA256[baseline]
    assert result.sha256 == hashlib.sha256(result.content.encode("utf-8")).hexdigest()


def test_render_empty_selection_returns_base_bytes_unchanged():
    base = _desired("logging {}\n")
    result = render_lxc_log_config(
        base,
        node="pve01",
        cluster="default",
        lxc_id="123",
        name="legacy-smoke",
        sources=(),
        retention_hours=168,
    )
    assert result is base
    assert result.sha256 == _LEGACY_BASELINE_SHA256["none"]


def test_render_legacy_components_labels_and_source_order():
    resolved = _resolved_sources(
        None, plugins=("nginxproxymanager", "proxmox-backup-server")
    )
    content = _baseline_render(resolved.renderable_sources).content
    order = [
        'loki.source.file "fleet_npm"',
        'loki.source.file "fleet_pbs_tasks"',
        'loki.source.file "fleet_pbs_api"',
    ]
    positions = [content.index(name) for name in order]
    assert positions == sorted(positions)
    assert 'loki.process "fleet_file_pack"' in content
    assert "forward_to = [loki.write.default.receiver]" in content
    assert json.dumps("/data/logs/**/*.log") in content
    assert json.dumps("/data/logs/**/.fleet-housekeeping-quarantine/**") in content
    assert json.dumps(
        "/var/log/proxmox-backup/tasks/[0-9A-F][0-9A-F]/UPID:*"
    ) in content
    assert json.dumps(
        "/var/log/proxmox-backup/tasks/**/.fleet-housekeeping-quarantine/**"
    ) in content
    api = content.split('loki.source.file "fleet_pbs_api"')[1]
    # Literal (non-quarantine) read paths never carry the auto quarantine glob.
    assert "__path_exclude__" not in api
    assert json.dumps("/var/log/proxmox-backup/api/access.log") in api
    assert json.dumps("/var/log/proxmox-backup/api/auth.log") in api
    assert api.index("access.log") < api.index("auth.log")
    assert f"app = {json.dumps('proxmox-backup')}" in content
    assert f"log_kind = {json.dumps('application')}" in content
    assert f"log_kind = {json.dumps('task')}" in content
    assert f"log_kind = {json.dumps('api')}" in content
    # filename is packed into the JSON body, never an indexed label
    assert "filename = " not in content
    assert json.dumps("filename") in content


def test_render_candidate_and_final_sources_produce_identical_bytes():
    resolved = _resolved_sources("nginxproxymanager")
    candidate = tuple(
        replace(source, status="acl-needed") for source in resolved.sources
    )
    assert len(candidate) == 1
    candidate_config = _baseline_render(candidate)
    final_config = _baseline_render(resolved.renderable_sources)
    assert candidate_config.content == final_config.content
    assert candidate_config.sha256 == _LEGACY_BASELINE_SHA256["npm"]


def test_render_rejects_unverified_source_status():
    for status in ("blocked", "inactive", "partial", "unmeasurable"):
        with pytest.raises(AlloyConfigError, match="not deployable"):
            _render([_source(status=status)])


def test_render_rejects_duplicate_components_and_overlapping_paths():
    first = _source(
        component="fleet_dup",
        key="owner/first",
        paths=("/data/logs/a/**/*.log",),
        roots=("/data/logs/a",),
    )
    second = _source(
        component="fleet_dup",
        key="owner/second",
        paths=("/data/logs/b/**/*.log",),
        roots=("/data/logs/b",),
    )
    with pytest.raises(AlloyConfigError, match="claimed by both"):
        _render([first, second])
    overlapping = _source(
        component="fleet_other",
        key="owner/third",
        paths=("/data/logs/a/specific.log",),
        roots=("/data/logs/a",),
        exclude_paths=(),
    )
    with pytest.raises(AlloyConfigError, match="ownership conflict"):
        _render([first, overlapping])


def test_render_rejects_invalid_component_names():
    with pytest.raises(AlloyConfigError, match="invalid Alloy component"):
        _render([_source(component="bad-name")])


def test_render_rejects_base_with_generated_component_names():
    source = _source()
    npm_base = _desired(_GUEST_BASE + '\nloki.source.file "fleet_npm" {}\n')
    with pytest.raises(AlloyConfigError, match="reserved"):
        render_lxc_log_config(
            npm_base,
            node="pve1",
            cluster="main",
            lxc_id="123",
            name="npm",
            sources=[source],
            retention_hours=48,
        )
    pack_base = _desired(_GUEST_BASE + '\nloki.process "fleet_file_pack" {}\n')
    with pytest.raises(AlloyConfigError, match="reserved"):
        render_lxc_log_config(
            pack_base,
            node="pve1",
            cluster="main",
            lxc_id="123",
            name="npm",
            sources=[source],
            retention_hours=48,
        )


def test_render_retention_controls_ignore_older_than_and_rejects_zero():
    result = _render([_source()], retention_hours=24)
    assert json.dumps("24h") in result.content
    assert json.dumps("12h") not in result.content
    with pytest.raises(AlloyConfigError, match="positive integer"):
        _render([_source()], retention_hours=0)


def test_render_escapes_hostile_values_and_keeps_hcl_balanced():
    hostile = 'na"me\\path\nnext'
    cluster = 'we"ird'
    result = _render([_source()], name=hostile, cluster=cluster)
    assert json.dumps(hostile) in result.content
    assert hostile not in result.content
    assert json.dumps(cluster) in result.content
    assert cluster not in result.content
    assert result.content.count("{") == result.content.count("}")


def test_render_handles_base_without_trailing_newline():
    base = _desired('loki.write "default" {}')
    result = render_lxc_log_config(
        base,
        node="pve1",
        cluster="main",
        lxc_id="123",
        name="npm",
        sources=[_source()],
        retention_hours=48,
    )
    assert result.content.startswith(base.content)
    assert 'loki.source.file "fleet_npm"' in result.content


def test_render_combines_multiple_exclusions_into_one_brace_union():
    source = _source(
        exclude_paths=(
            "/data/logs/**/.fleet-housekeeping-quarantine/**",
            "/data/logs/**/rotated/**",
        )
    )
    content = _render([source]).content
    expected = (
        "{/data/logs/**/.fleet-housekeeping-quarantine/**,/data/logs/**/rotated/**}"
    )
    assert f"__path_exclude__ = {json.dumps(expected)}," in content
    assert content.count("__path_exclude__") == 1


def test_render_literal_exact_file_drops_only_auto_quarantine_exclusion():
    source = _source(
        component="fleet_kuma",
        key="uptimekuma/error",
        source_id="error",
        app="uptimekuma",
        log_kind="error",
        paths=(),
        exact_files=("/opt/uptime-kuma/data/error.log",),
        roots=("/opt/uptime-kuma/data",),
        exclude_paths=("/opt/uptime-kuma/data/**/.fleet-housekeeping-quarantine/**",),
    )
    content = _render([source]).content
    stanza = content.split('loki.source.file "fleet_kuma"')[1]
    assert "__path_exclude__" not in stanza
    assert json.dumps("/opt/uptime-kuma/data/error.log") in stanza
    authored = replace(
        source, exclude_paths=("/opt/uptime-kuma/data/*.old",)
    )
    authored_stanza = _render([authored]).content.split(
        'loki.source.file "fleet_kuma"'
    )[1]
    assert json.dumps("/opt/uptime-kuma/data/*.old") in authored_stanza


def test_render_exact_only_source_renders_one_literal_target():
    source = _source(
        component="fleet_kuma",
        key="uptimekuma/error",
        source_id="error",
        app="uptimekuma",
        log_kind="error",
        paths=(),
        exact_files=("/opt/uptime-kuma/data/error.log",),
        roots=(),
        exclude_paths=(),
    )
    content = _render([source]).content
    assert content.count("__path__") == 1
    assert json.dumps("/opt/uptime-kuma/data/error.log") in content


def test_render_multiline_source_chains_into_shared_pack_process():
    multiline = MultilineOptions(
        firstline=r"^\d{4}-\d{2}-\d{2}", max_lines=200, max_wait_seconds=5
    )
    source = _source(
        component="fleet_custom",
        key="custom/app/application",
        owner_id="custom",
        service_id="app",
        source_id="application",
        app="custom",
        log_kind="application",
        multiline=multiline,
        paths=("/srv/app/logs/*.log",),
        roots=("/srv/app/logs",),
        exclude_paths=(),
    )
    content = _render([source]).content
    assert "stage.multiline {" in content
    assert f"firstline = {json.dumps(multiline.firstline)}" in content
    assert "max_lines = 200" in content
    assert 'max_wait_time = "5s"' in content
    multiline_component = content.split('loki.process "fleet_multiline_')[1].split(
        '"'
    )[0]
    assert "fleet_multiline_" in content
    assert (
        f"forward_to = [loki.process.fleet_multiline_{multiline_component}.receiver]"
        in content
    )
    source_stanza = content.split('loki.source.file "fleet_custom"')[1]
    assert (
        f"forward_to = [loki.process.fleet_multiline_{multiline_component}.receiver]"
        in source_stanza
    )
    # the shared packing process remains the single writer stage
    assert 'loki.process "fleet_file_pack"' in content
    assert "stage.pack {" in content
    assert "ingest_timestamp = true" in content
