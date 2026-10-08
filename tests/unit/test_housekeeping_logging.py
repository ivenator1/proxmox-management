"""Behavioural coverage for ``prepare_lxc_logging`` and its typed probe helpers.

These tests exercise the real logging policy, renderer and reconciler through
a scripted guest execution boundary.
"""
from __future__ import annotations

import hashlib

import pytest

from proxmox_fleet import housekeeping
from proxmox_fleet.alloy import DesiredAlloyConfig
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult

# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #


def _effective_npm(base: DesiredAlloyConfig) -> DesiredAlloyConfig:
    return housekeeping.render_lxc_log_config(
        base, node="pve", cluster="cluster", lxc_id="123", name="guest",
        profiles={"npm"}, retention_hours=48,
    )


def _base(*, env: bool = True, endpoint: str | None = "http://10.0.0.1:3100") -> DesiredAlloyConfig:
    lines = ['loki.write "default" {']
    if endpoint:
        lines.append(f'  endpoint {{ url = "{endpoint}/loki/api/v1/push" }}')
    lines.append("}")
    lines.append('loki.source.journal "journal" {')
    if env:
        lines.append('  max_age = coalesce(sys.env("FLEET_JOURNAL_MAX_AGE"), "48h")')
    else:
        lines.append('  max_age = "12h"')
    lines.append("}")
    content = "\n".join(lines) + "\n"
    return DesiredAlloyConfig(content=content, sha256=hashlib.sha256(content.encode()).hexdigest())


@pytest.mark.parametrize("fault", ["none", "noop", "symlink"])
def test_environment_configuration_requires_observed_safe_file_state(tmp_path, fault):
    from tests.unit.test_housekeeping_retention import Guest as FilesystemGuest

    guest = FilesystemGuest(tmp_path / "guest")
    path = guest.put(housekeeping.FLEET_ALLOY_ENV_DROPIN, housekeeping.journal_env_content(24))
    original = path.read_bytes()
    outside = tmp_path / "outside.conf"
    if fault == "symlink":
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
    guest.acknowledge_without_write = fault == "noop"
    base = _base()
    guest.put("/etc/alloy/config.alloy", _effective_npm(base).content)
    result = housekeeping.prepare_lxc_logging(
        guest, GlobalSettings(housekeeping_loki_url="http://10.0.0.1:3100"),
        node="pve", cluster="cluster", lxc_id="123", name="guest", base=base,
    )
    if fault == "none":
        assert path.read_text() == housekeeping.journal_env_content(48)
        assert result.summary.status == "Configured"
        assert not result.failed
    else:
        assert path.read_bytes() == original
        assert result.summary.status == "Blocked"
        assert result.failed
        if fault == "symlink":
            assert path.is_symlink()
            assert outside.read_bytes() == original


def _env_hash(hours: int = 48) -> str:
    return hashlib.sha256(housekeeping.journal_env_content(hours).encode()).hexdigest()


def _npm_evidence(ok: bool = True) -> dict:
    return {
        "npm.service": ok,
        "openresty.service": ok,
        "app_root": ok,
        "log_root": ok,
        "detected": ok,
    }


_ALL_BINARIES = {
    "alloy": "/usr/bin/alloy",
    "setfacl": "/usr/bin/setfacl",
    "getfacl": "/usr/bin/getfacl",
    "logrotate": "/usr/sbin/logrotate",
    "apt-get": "/usr/bin/apt-get",
    "runuser": "/usr/sbin/runuser",
    "journalctl": "/usr/bin/journalctl",
    "systemctl": "/usr/bin/systemctl",
    "python3": "/usr/bin/python3",
    "yarn": "/usr/bin/yarn",
    "npm": "/usr/bin/npm",
    "pnpm": None,
    "proxmox-backup-manager": None,
}


def _facts(
    *,
    profiles=(),
    evidence=None,
    binaries=None,
    access_ready=True,
    alloy_user=True,
    alloy_env_hash="",
    running=True,
    template=False,
    os_type="debian",
) -> dict:
    evidence = {"npm": {"detected": False}, "pbs": {"detected": False}, **(evidence or {})}
    if binaries is None:
        binaries = dict(_ALL_BINARIES)
    return {
        "guest": {
            "name": "guest",
            "os_type": os_type,
            "is_running": running,
            "is_template": template,
        },
        "disk": {"total_bytes": 100, "available_bytes": 50, "used_percent": 50.0},
        "journal_bytes": 0,
        "profiles": list(profiles),
        "files": [],
        "cache_paths": {},
        "busy_tools": [],
        "binaries": binaries,
        "log_access_ready": access_ready,
        "log_access_detail": {"alloy_user": alloy_user, "dirs": {}, "files": {}},
        "policy_sha256": {"journald": "", "alloy_env": alloy_env_hash, "npm_logrotate": ""},
        "profile_evidence": evidence,
    }


def _probe(facts: dict) -> PrimitiveResult:
    return PrimitiveResult(rc=0, facts=facts)


def _alloy_facts(config_sha256: str, *, binary_present: bool = True) -> dict:
    return {
        "binary_present": binary_present,
        "package_manager": "apt",
        "config_sha256": config_sha256,
        "journal_member": True,
        "service_enabled": True,
        "service_active": True,
    }


def _next(items: list):
    if len(items) > 1:
        return items.pop(0)
    return items[0]


class ScriptedExecutor:
    """Scripted Executor: queued housekeeping probes and per-command results."""

    host = "pve"

    def __init__(
        self,
        probes: list[PrimitiveResult],
        *,
        apply_results: list[PrimitiveResult] | None = None,
        alloy_probes: list[PrimitiveResult] | None = None,
        deploy: PrimitiveResult | None = None,
    ) -> None:
        self._probes = list(probes)
        self._applies = list(apply_results or [])
        self._alloy_probes = list(alloy_probes or [])
        self._deploy = deploy or PrimitiveResult(rc=0, changed=True)
        self.applied: list[str] = []
        self.reconciled: list[dict] = []
        self.probe_calls = 0
        self.alloy_probe_calls = 0

    def housekeeping_probe(self, lxc_id: str) -> PrimitiveResult:
        self.probe_calls += 1
        return _next(self._probes)

    def housekeeping_apply(self, lxc_id: str, *, command: str) -> PrimitiveResult:
        self.applied.append(command)
        if self._applies:
            return _next(self._applies)
        return PrimitiveResult(rc=0, changed=True)

    def alloy_probe(self, *, lxc_id: str | None = None) -> PrimitiveResult:
        self.alloy_probe_calls += 1
        assert self._alloy_probes, "unexpected alloy_probe call"
        return _next(self._alloy_probes)

    def alloy_reconcile(self, **kwargs) -> PrimitiveResult:
        self.reconciled.append(kwargs)
        return self._deploy


def _prepare(executor, settings=None, *, base=None, dry_run=False, allow_install=False, **kw):
    return housekeeping.prepare_lxc_logging(
        executor,
        settings or GlobalSettings(),
        node=kw.pop("node", "pve"),
        cluster=kw.pop("cluster", "cluster"),
        lxc_id=kw.pop("lxc_id", "123"),
        name=kw.pop("name", "guest"),
        base=base or _base(),
        dry_run=dry_run,
        allow_install=allow_install,
    )


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def test_recognized_profiles_requires_full_evidence_and_warns_on_partial():
    recognized, warnings = housekeeping.recognized_profiles(
        _facts(profiles=["npm"], evidence={"npm": {"npm.service": True, "detected": True}})
    )
    assert recognized == set()
    assert any("incomplete profile evidence" in w for w in warnings)

    recognized, warnings = housekeeping.recognized_profiles(
        _facts(profiles=["npm"], evidence={"npm": _npm_evidence(True)})
    )
    assert recognized == {"npm"}
    assert warnings == []

    # Evidence complete even when the probe did not list the profile: trusted.
    recognized, warnings = housekeeping.recognized_profiles(
        _facts(profiles=[], evidence={"npm": _npm_evidence(True)})
    )
    assert recognized == {"npm"}

    recognized, warnings = housekeeping.recognized_profiles(_facts(profiles=["redis"]))
    assert recognized == set()
    assert any("unsupported profile" in w for w in warnings)


def test_probe_housekeeping_raises_on_failed_primitive():
    executor = ScriptedExecutor([PrimitiveResult(rc=1, failed=True, stderr="boom")])
    with pytest.raises(housekeeping.HousekeepingError):
        housekeeping.probe_housekeeping(executor, "123")


def test_probe_housekeeping_types_facts():
    probe = housekeeping.probe_housekeeping(
        ScriptedExecutor(
            [_probe(_facts(profiles=["npm"], evidence={"npm": _npm_evidence(True)}, access_ready=False))]
        ),
        "123",
    )
    assert probe.is_running and not probe.is_template
    assert probe.os_type == "debian"
    assert probe.recognized == ("npm",)
    assert probe.log_access_ready is False
    assert probe.binaries["alloy"] == "/usr/bin/alloy"


def test_probe_housekeeping_rejects_incomplete_facts():
    facts = _facts()
    facts.pop("policy_sha256")
    with pytest.raises(housekeeping.HousekeepingError, match="incomplete facts"):
        housekeeping.probe_housekeeping(ScriptedExecutor([_probe(facts)]), "123")


def test_probe_housekeeping_rejects_truncated_binaries():
    facts = _facts()
    facts["binaries"].pop("journalctl")
    with pytest.raises(housekeeping.HousekeepingError, match="binary facts"):
        housekeeping.probe_housekeeping(ScriptedExecutor([_probe(facts)]), "123")


def test_probe_housekeeping_rejects_malformed_file_record():
    facts = _facts()
    facts["files"] = [{"path": "/data/logs/access.log", "device": 1}]
    with pytest.raises(housekeeping.HousekeepingError, match="malformed file record"):
        housekeeping.probe_housekeeping(ScriptedExecutor([_probe(facts)]), "123")


def test_prepare_blocks_on_incomplete_probe_facts():
    facts = _facts()
    del facts["log_access_detail"]
    executor = ScriptedExecutor([_probe(facts)])
    result = _prepare(executor)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert executor.applied == []
    assert executor.reconciled == []


def test_stopped_guest_sparse_facts_are_accepted():
    # The helper's stopped-guest facts are complete-but-sparse: no binaries and
    # a null disk must not be treated as a truncated stream.
    facts = _facts(running=False, binaries={}, access_ready=False)
    facts["disk"] = None
    facts["log_access_detail"] = {"alloy_user": False, "dirs": {}, "files": {}}
    executor = ScriptedExecutor([_probe(facts)])
    result = _prepare(executor, base=_base())

    assert result.summary is None
    assert any("stopped" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# prepare_lxc_logging — real runs
# --------------------------------------------------------------------------- #


def test_compliant_profile_free_guest_is_a_noop():
    base = _base()
    executor = ScriptedExecutor(
        [_probe(_facts(access_ready=True, alloy_env_hash=_env_hash()))],
        alloy_probes=[PrimitiveResult(rc=0, facts=_alloy_facts(base.sha256))],
    )
    result = _prepare(executor, base=base)

    assert result.summary is None
    assert result.changed is False
    assert result.failed is False
    assert result.warnings == []
    assert result.effective_alloy is base
    assert executor.applied == []
    # Alloy still goes through the real reconciliation path (no drift → no deploy).
    assert executor.reconciled == []


def test_partial_evidence_warns_and_skips_profile_work():
    base = _base(env=False)
    facts = _facts(
        profiles=["npm"],
        evidence={"npm": {"npm.service": True, "openresty.service": True, "detected": True}},
        access_ready=False,
    )
    executor = ScriptedExecutor(
        [_probe(facts)],
        alloy_probes=[PrimitiveResult(rc=0, facts=_alloy_facts(base.sha256))],
    )
    result = _prepare(executor, base=base)

    assert result.summary is None
    assert executor.applied == []
    assert any("incomplete profile evidence" in w for w in result.warnings)
    # Unsupported profile never authorizes ACL repair despite access_ready=False.


def test_repairs_permissions_then_reprobes_then_reconciles():
    base = _base()
    stub = _effective_npm(base)
    not_ready = _probe(
        _facts(
            profiles=["npm"],
            evidence={"npm": _npm_evidence(True)},
            access_ready=False,
            alloy_env_hash=_env_hash(),
        )
    )
    ready = _probe(
        _facts(
            profiles=["npm"],
            evidence={"npm": _npm_evidence(True)},
            access_ready=True,
            alloy_env_hash=_env_hash(),
        )
    )
    executor = ScriptedExecutor(
        [not_ready, ready],
        alloy_probes=[
            PrimitiveResult(rc=0, facts=_alloy_facts("")),
            PrimitiveResult(rc=0, facts=_alloy_facts(stub.sha256)),
        ],
    )
    result = _prepare(executor, base=base)

    assert executor.applied == housekeeping.acl_repair_commands({"npm"})
    assert executor.probe_calls == 2
    assert executor.reconciled and executor.reconciled[0]["desired_content"] == stub.content
    assert result.summary is not None and result.summary.status == "Configured"
    assert result.changed is True
    assert result.effective_alloy == stub


def test_acl_repair_failure_blocks_and_returns_effective_config():
    base = _base()
    stub = _effective_npm(base)
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    profiles=["npm"],
                    evidence={"npm": _npm_evidence(True)},
                    access_ready=False,
                    alloy_env_hash=_env_hash(),
                )
            )
        ],
        apply_results=[PrimitiveResult(rc=1, failed=True, stderr="setfacl: denied")],
        alloy_probes=[PrimitiveResult(rc=0, facts=_alloy_facts(stub.sha256))],
    )
    result = _prepare(executor, base=base)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy == stub
    # Blocked on the first permission command → no env work, no reconcile.
    assert executor.reconciled == []
    assert any("scoped log access" in w for w in result.warnings)


def test_missing_acl_tools_blocks_without_running_commands():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    profiles=["npm"],
                    evidence={"npm": _npm_evidence(True)},
                    access_ready=False,
                    binaries={**_ALL_BINARIES, "setfacl": None, "getfacl": None},
                    alloy_env_hash=_env_hash(),
                )
            )
        ],
        alloy_probes=[PrimitiveResult(rc=0, facts=_alloy_facts(base.sha256))],
    )
    result = _prepare(executor, base=base)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert executor.applied == []
    assert any("ACL tools missing" in w for w in result.warnings)


def test_missing_alloy_blocks_when_install_disallowed():
    base = _base()
    executor = ScriptedExecutor(
        [_probe(_facts(binaries={**_ALL_BINARIES, "alloy": None}, alloy_env_hash=_env_hash()))],
        alloy_probes=[],
    )
    result = _prepare(executor, base=base, allow_install=False)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is base
    assert executor.reconciled == []
    assert executor.applied == []
    assert any("Alloy is not installed" in w for w in result.warnings)


def test_missing_alloy_installs_when_allowed_then_reprobes():
    base = _base()
    missing = PrimitiveResult(
        rc=0, facts=_alloy_facts(base.sha256, binary_present=False)
    )
    aligned = PrimitiveResult(rc=0, facts=_alloy_facts(base.sha256))
    executor = ScriptedExecutor(
        [
            _probe(_facts(binaries={**_ALL_BINARIES, "alloy": None}, alloy_env_hash=_env_hash())),
            _probe(_facts(alloy_env_hash=_env_hash())),
        ],
        alloy_probes=[missing, aligned],
    )
    result = _prepare(executor, base=base, allow_install=True)

    assert executor.reconciled and executor.reconciled[0]["install"] is True
    assert result.summary is not None and result.summary.status == "Configured"
    assert result.failed is False


def test_unsupported_guest_os_blocks():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    profiles=["pbs"],
                    evidence={
                        "pbs": {
                            "proxmox-backup-manager": True,
                            "proxmox-backup-proxy.service": True,
                            "task_root": True,
                            "api_root": True,
                            "detected": True,
                        }
                    },
                    os_type="alpine",
                )
            )
        ]
    )
    result = _prepare(executor, base=base)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert executor.applied == []
    assert any("Debian" in w for w in result.warnings)


def test_stopped_guest_is_skipped():
    base = _base()
    executor = ScriptedExecutor([_probe(_facts(running=False))])
    result = _prepare(executor, base=base)

    assert result.summary is None
    assert result.changed is False
    assert result.effective_alloy is base
    assert any("stopped" in w for w in result.warnings)
    assert executor.applied == []


def test_probe_failure_blocks_without_effective_config():
    executor = ScriptedExecutor([PrimitiveResult(rc=1, failed=True, stderr="unreachable")])
    result = _prepare(executor)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None



def test_custom_journal_source_reports_explicit_migration():
    base = _base(env=False)
    executor = ScriptedExecutor(
        [_probe(_facts())],
        alloy_probes=[PrimitiveResult(rc=0, facts=_alloy_facts(base.sha256))],
    )
    result = _prepare(executor, base=base)

    assert result.summary is None
    assert executor.applied == []
    assert any("explicit migration" in w for w in result.warnings)
    assert not result.failed


def test_loki_endpoint_conflict_blocks():
    base = _base(endpoint="http://10.0.0.1:3100")
    executor = ScriptedExecutor([_probe(_facts())], alloy_probes=[])
    result = _prepare(
        executor,
        settings=GlobalSettings(housekeeping_loki_url="http://10.9.9.9:3100"),
        base=base,
    )

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is base
    # The warning never echoes the endpoint or the HCL source.
    assert all("10.0.0.1" not in w and "10.9.9.9" not in w for w in result.warnings)
    assert executor.reconciled == []


def test_matching_loki_endpoint_is_accepted():
    base = _base(endpoint="http://10.10.10.39:3100")
    executor = ScriptedExecutor(
        [_probe(_facts(alloy_env_hash=_env_hash()))],
        alloy_probes=[PrimitiveResult(rc=0, facts=_alloy_facts(base.sha256))],
    )
    result = _prepare(
        executor,
        settings=GlobalSettings(housekeeping_loki_url="http://10.10.10.39:3100"),
        base=base,
    )
    assert result.summary is None
    assert not result.failed


# --------------------------------------------------------------------------- #
# prepare_lxc_logging — dry run
# --------------------------------------------------------------------------- #


def test_dry_run_audits_findings_without_writing():
    base = _base()
    stub = _effective_npm(base)
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    profiles=["npm"],
                    evidence={"npm": _npm_evidence(True)},
                    access_ready=False,
                    alloy_env_hash="",
                )
            )
        ],
        alloy_probes=[PrimitiveResult(rc=0, facts=_alloy_facts(stub.sha256))],
    )
    result = _prepare(executor, base=base, dry_run=True)

    assert result.summary is not None and result.summary.status == "Audit"
    assert result.changed is False
    assert result.failed is False
    assert result.effective_alloy == stub
    assert executor.applied == []
    assert executor.reconciled == []
    assert any("scoped read access" in w for w in result.warnings)
    assert any("environment drop-in" in w for w in result.warnings)


def test_dry_run_compliant_guest_returns_no_summary():
    base = _base()
    executor = ScriptedExecutor(
        [_probe(_facts(alloy_env_hash=_env_hash()))],
        alloy_probes=[PrimitiveResult(rc=0, facts=_alloy_facts(base.sha256))],
    )
    result = _prepare(executor, base=base, dry_run=True)

    assert result.summary is None
    assert result.warnings == []
    assert executor.applied == []
    assert executor.reconciled == []
