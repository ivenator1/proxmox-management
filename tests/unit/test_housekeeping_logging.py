"""Behavioural coverage for ``prepare_lxc_logging`` and its typed probe helpers.

These tests exercise the real catalogue selection, renderer and reconciler
through a scripted guest execution boundary.  Probe logging evidence is built
from the actual manager request, so a test can only pass when the catalogue and
probe contract agree.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence

import pytest

from proxmox_fleet import housekeeping
from proxmox_fleet import log_plugins as lp
from proxmox_fleet.alloy import DesiredAlloyConfig
from proxmox_fleet.models.log_plugins import GuestLogOverride
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult

_CATALOGUE = None


def _catalogue():
    global _CATALOGUE
    if _CATALOGUE is None:
        _CATALOGUE = lp.load_log_catalogue()
    return _CATALOGUE


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _base(
    *, env: bool = True, endpoint: str | None = "http://10.0.0.1:3100"
) -> DesiredAlloyConfig:
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
    return DesiredAlloyConfig(content=content, sha256=_sha(content))


def _env_hash(hours: int = 48) -> str:
    return _sha(housekeeping.journal_env_content(hours))


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


def _unit_records(entry: Dict[str, Any]) -> Dict[str, Any]:
    units: Dict[str, Any] = {}

    def add(unit: Dict[str, Any]) -> None:
        units[unit["key"]] = {"status": "active", "instances": {}}

    for layout in entry.get("layouts", []):
        for unit in (*layout.get("required_units", []), *layout.get("running_units_any", [])):
            add(unit)
    for unit in entry.get("journal_units", []):
        units.setdefault(unit["key"], {"status": "active", "instances": {}})
    return units


def _logging_facts(
    request: Optional[Dict[str, Any]],
    *,
    ready: bool = True,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    if request is None:
        return {"plugins": {}, "sources": {}}
    # ``status="unavailable"`` mirrors the real helper when the collector
    # prerequisite (ACL tools) is missing: no access could be measured.
    unavailable = status == "unavailable"
    plugins: Dict[str, Any] = {}
    for owner, entry in request.get("plugins", {}).items():
        includes = {
            dependency: {"units": _unit_records(dependency_entry)}
            for dependency, dependency_entry in entry.get("includes", {}).items()
        }
        plugins[owner] = {"units": _unit_records(entry), "includes": includes}
    sources: Dict[str, Any] = {}
    for key, source_entry in request.get("sources", {}).items():
        layouts: Dict[str, Any] = {}
        for layout_id, spec in source_entry.get("layouts", {}).items():
            roots: Dict[str, Any] = {}
            for root in spec.get("roots", []):
                if unavailable:
                    roots[root] = {
                        "exists": False,
                        "is_dir": False,
                        "is_symlink": False,
                        "device": None,
                        "blocked_ancestor": False,
                        "status": "unavailable",
                        "error": "the guest ACL tools are unavailable",
                    }
                    continue
                roots[root] = {
                    "exists": True,
                    "is_dir": True,
                    "is_symlink": False,
                    "device": 7,
                    "blocked_ancestor": False,
                    "status": "ok" if ready else "denied",
                    "access": {
                        "traversable": ready,
                        "readable": ready,
                        "default_acl": ready,
                    },
                }
            if unavailable:
                matches = [
                    {
                        "pattern": pattern,
                        "matched": 0,
                        "skipped": 0,
                        "truncated": False,
                        "status": "unavailable",
                        "error": "the guest ACL tools are unavailable",
                        "files": [],
                    }
                    for pattern in spec.get("paths", [])
                ]
            else:
                matches = [
                    {
                        "pattern": pattern,
                        "matched": 0,
                        "skipped": 0,
                        "truncated": False,
                        "status": "quiet" if ready else "denied",
                        "files": [],
                    }
                    for pattern in spec.get("paths", [])
                ]
            exact: Dict[str, Any] = {}
            for path in spec.get("exact_files", []):
                if unavailable:
                    exact[path] = {
                        "exists": False,
                        "is_symlink": False,
                        "device": None,
                        "inode": None,
                        "size": None,
                        "mtime_ns": None,
                        "status": "unavailable",
                        "error": "the guest ACL tools are unavailable",
                    }
                    continue
                exact[path] = {
                    "exists": True,
                    "is_symlink": False,
                    "device": 7,
                    "inode": 8,
                    "size": 1,
                    "mtime_ns": 2,
                    "same_device": True,
                    "parent_traversable": ready,
                    "status": "ok" if ready else "denied",
                    "access": {"readable": ready},
                    "is_file": True,
                }
            layouts[layout_id] = {
                "status": (
                    "unavailable"
                    if unavailable
                    else "ok" if ready else "denied"
                ),
                "truncated": False,
                "roots": roots,
                "exact_files": exact,
                "matches": matches,
            }
        sources[key] = {"layouts": layouts}
    return {"plugins": plugins, "sources": sources}


def _facts(
    *,
    logging: Optional[Dict[str, Any]] = None,
    binaries: Optional[Dict[str, Any]] = None,
    access_ready: bool = True,
    alloy_user: bool = True,
    alloy_env_hash: str = "",
    running: bool = True,
    template: bool = False,
    os_type: str = "debian",
    profiles=(),
    evidence=None,
) -> dict:
    evidence = {"npm": {"detected": False}, "pbs": {"detected": False}, **(evidence or {})}
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
        "binaries": dict(binaries if binaries is not None else _ALL_BINARIES),
        "log_access_ready": access_ready,
        "log_access_detail": {"alloy_user": alloy_user, "dirs": {}, "files": {}},
        "policy_sha256": {"journald": "", "alloy_env": alloy_env_hash, "npm_logrotate": ""},
        "profile_evidence": evidence,
        "logging": logging or {"plugins": {}, "sources": {}},
    }


def _probe(facts: dict) -> PrimitiveResult:
    return PrimitiveResult(rc=0, facts=facts)


def _next(items: list):
    if len(items) > 1:
        return items.pop(0)
    return items[0]


def _npm_request() -> Dict[str, Any]:
    selection = lp.select_log_plugins(
        _catalogue(), script_id="nginxproxymanager", override=None, auto=True
    )
    return lp.build_logging_request(selection)


_REQUEST = _npm_request()


class ScriptedExecutor:
    """Scripted executor: queued guest probes plus explicit Alloy state."""

    host = "pve"

    def __init__(
        self,
        probes: List[PrimitiveResult],
        *,
        apply_results: Optional[List[PrimitiveResult]] = None,
        acl_results: Optional[List[PrimitiveResult]] = None,
        alloy_binary: bool = True,
        alloy_config_sha: str = "",
        validation_fails: bool = False,
        deploy_fails: bool = False,
        install_fails: bool = False,
    ) -> None:
        self._probes = list(probes)
        self._applies = list(apply_results or [])
        self._acls = list(acl_results or [])
        self.binary_present = alloy_binary
        self.config_sha = alloy_config_sha
        self.validation_fails = validation_fails
        self.deploy_fails = deploy_fails
        self.install_fails = install_fails
        self.applied: List[str] = []
        self.acl_calls: List[Dict[str, Any]] = []
        self.reconciled: List[Dict[str, Any]] = []
        self.validation_calls: List[str] = []
        self.probe_calls = 0
        self.logging_requests: List[Optional[Dict[str, Any]]] = []

    def housekeeping_probe(
        self, lxc_id: str, *, logging_request: Optional[Dict[str, Any]] = None
    ) -> PrimitiveResult:
        self.probe_calls += 1
        self.logging_requests.append(logging_request)
        return _next(self._probes)

    def housekeeping_apply(self, lxc_id: str, *, command: str) -> PrimitiveResult:
        self.applied.append(command)
        if self._applies:
            return _next(self._applies)
        return PrimitiveResult(rc=0, changed=True)

    def housekeeping_log_acl(
        self,
        lxc_id: str,
        *,
        roots: Sequence[str] = (),
        exact_files: Sequence[str] = (),
    ) -> PrimitiveResult:
        roots = tuple(roots)
        exact_files = tuple(exact_files)
        self.acl_calls.append(
            {"roots": roots, "exact_files": exact_files}
        )
        if self._acls:
            return _next(self._acls)
        changed = bool(roots or exact_files)
        return PrimitiveResult(
            rc=0,
            changed=changed,
            facts={
                "status": "complete",
                "changed": changed,
                "repaired_roots": len(roots),
                "repaired_files": len(exact_files),
                "skipped_missing": 0,
                "error_count": 0,
                "reason": "",
            },
        )

    def alloy_probe(self, *, lxc_id: Optional[str] = None) -> PrimitiveResult:
        return PrimitiveResult(
            rc=0,
            facts={
                "binary_present": self.binary_present,
                "package_manager": "apt",
                "config_sha256": self.config_sha,
                "journal_member": True,
                "service_enabled": True,
                "service_active": True,
            },
        )

    def alloy_reconcile(self, **kwargs) -> PrimitiveResult:
        self.reconciled.append(dict(kwargs))
        if kwargs.get("validate_only"):
            self.validation_calls.append(kwargs["desired_content"])
            if self.validation_fails:
                return PrimitiveResult(rc=1, failed=True, stderr="invalid HCL", facts={})
            return PrimitiveResult(rc=0, changed=False, facts={})
        if self.deploy_fails:
            return PrimitiveResult(rc=1, failed=True, stderr="alloy deploy failed", facts={})
        if kwargs.get("install") and self.install_fails:
            return PrimitiveResult(rc=1, failed=True, stderr="apt failed", facts={})
        if kwargs.get("install"):
            self.binary_present = True
        if kwargs.get("configure"):
            self.config_sha = _sha(kwargs["desired_content"])
        return PrimitiveResult(rc=0, changed=True, facts={})


def _prepare(
    executor,
    settings=None,
    *,
    base=None,
    script_id: Optional[str] = "nginxproxymanager",
    log_override: Optional[GuestLogOverride] = None,
    dry_run: bool = False,
    allow_install: bool = False,
    **kw,
):
    return housekeeping.prepare_lxc_logging(
        executor,
        settings or GlobalSettings(),
        node=kw.pop("node", "pve"),
        cluster=kw.pop("cluster", "cluster"),
        lxc_id=kw.pop("lxc_id", "123"),
        name=kw.pop("name", "guest"),
        base=base or _base(),
        catalogue=_catalogue(),
        script_id=script_id,
        log_override=log_override,
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
        _facts(
            profiles=["npm"],
            evidence={
                "npm": {
                    "npm.service": True,
                    "openresty.service": True,
                    "app_root": True,
                    "log_root": True,
                    "detected": True,
                }
            },
        )
    )
    assert recognized == {"npm"}
    assert warnings == []

    recognized, warnings = housekeeping.recognized_profiles(_facts(profiles=["redis"]))
    assert recognized == set()
    assert any("unsupported profile" in w for w in warnings)


def test_probe_housekeeping_raises_on_failed_primitive():
    executor = ScriptedExecutor([PrimitiveResult(rc=1, failed=True, stderr="boom")])
    with pytest.raises(housekeeping.HousekeepingError):
        housekeeping.probe_housekeeping(executor, "123")


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


def test_probe_housekeeping_rejects_incomplete_or_malformed_logging_evidence():
    facts = _facts()
    facts.pop("policy_sha256")
    with pytest.raises(housekeeping.HousekeepingError, match="incomplete facts"):
        housekeeping.probe_housekeeping(ScriptedExecutor([_probe(facts)]), "123")

    logging = _logging_facts(_REQUEST)
    del logging["sources"]["nginxproxymanager/application"]["layouts"]["default"][
        "truncated"
    ]
    facts = _facts(logging=logging)
    with pytest.raises(housekeeping.HousekeepingError, match="logging"):
        housekeeping.probe_housekeeping(
            ScriptedExecutor([_probe(facts)]),
            "123",
            logging_request=_REQUEST,
        )


def test_probe_housekeeping_forwards_the_request():
    logging = _logging_facts(_REQUEST)
    executor = ScriptedExecutor([_probe(_facts(logging=logging))])
    probe = housekeeping.probe_housekeeping(
        executor, "123", logging_request=_REQUEST
    )
    assert executor.logging_requests == [_REQUEST]
    assert probe.logging == logging


# --------------------------------------------------------------------------- #
# prepare_lxc_logging — selection and failure boundaries
# --------------------------------------------------------------------------- #


def test_unknown_identity_enforces_base_config_without_app_sources():
    base = _base()
    executor = ScriptedExecutor(
        [_probe(_facts(alloy_env_hash=_env_hash()))],
        alloy_config_sha="d" * 64,
    )
    result = _prepare(executor, base=base, script_id=None)

    assert result.failed is False
    assert result.summary is not None and result.summary.status == "Configured"
    assert result.effective_alloy == base
    # The exact fleet base config is validated and deployed; an unresolvable
    # identity never blocks baseline system logging.
    assert executor.validation_calls == [base.content]
    deploys = [call for call in executor.reconciled if not call.get("validate_only")]
    assert len(deploys) == 1
    assert deploys[0]["desired_content"] == base.content
    assert executor.config_sha == base.sha256
    assert executor.acl_calls == []
    # Application coverage stays visible as a diagnostic only.
    assert any("unsupported" in warning for warning in result.warnings)


def test_explicit_empty_selection_returns_base_and_reconciles():
    executor = ScriptedExecutor(
        [_probe(_facts(alloy_env_hash=_env_hash()))],
        alloy_config_sha=_base().sha256,
    )
    result = _prepare(
        executor,
        log_override=GuestLogOverride(mode="disabled"),
    )

    assert result.failed is False
    assert result.effective_alloy == _base()
    assert result.summary is None
    assert executor.reconciled == []


def test_selected_guest_is_silent_when_already_compliant():
    base = _base()
    selection = lp.select_log_plugins(
        _catalogue(), script_id="nginxproxymanager", override=None, auto=True
    )
    candidate = housekeeping.render_lxc_log_config(
        base,
        node="pve",
        cluster="cluster",
        lxc_id="123",
        name="guest",
        sources=lp.validate_log_layout(
            selection, _logging_facts(_REQUEST)
        ).renderable_sources,
        retention_hours=48,
    )
    executor = ScriptedExecutor(
        [_probe(_facts(logging=_logging_facts(_REQUEST), alloy_env_hash=_env_hash()))],
        alloy_config_sha=candidate.sha256,
    )
    result = _prepare(executor, base=base)

    assert result.failed is False
    assert result.changed is False
    assert result.summary is None
    assert result.warnings == []
    assert result.effective_alloy == candidate
    assert executor.validation_calls == []
    assert executor.reconciled == []


def test_invalid_candidate_blocks_before_acl_or_environment():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, ready=False),
                    access_ready=False,
                    alloy_env_hash="",
                )
            )
        ],
        validation_fails=True,
    )
    result = _prepare(executor, base=base)

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.effective_alloy is None
    assert executor.applied == []
    assert executor.acl_calls == []
    assert executor.validation_calls
    assert all(call["validate_only"] for call in executor.reconciled)


def test_repairs_permissions_then_reprobes_then_reconciles():
    base = _base()
    not_ready = _probe(
        _facts(
            logging=_logging_facts(_REQUEST, ready=False),
            access_ready=False,
            alloy_env_hash=_env_hash(),
        )
    )
    ready = _probe(
        _facts(logging=_logging_facts(_REQUEST), alloy_env_hash=_env_hash())
    )
    executor = ScriptedExecutor([not_ready, ready])
    result = _prepare(executor, base=base)

    # One scoped permission-repair operation is invoked; its transport payload
    # is runtime-proven, while the consumer outcome is asserted below.
    assert len(executor.acl_calls) == 1
    assert executor.applied == []
    assert executor.probe_calls == 3
    assert executor.reconciled and executor.reconciled[-1]["desired_content"]
    assert result.summary is not None and result.summary.status == "Configured"
    assert result.changed is True
    assert result.effective_alloy is not None
    assert "fleet_npm" in result.effective_alloy.content


def test_acl_repair_failure_blocks_and_returns_no_config():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, ready=False),
                    access_ready=False,
                    alloy_env_hash="",
                )
            )
        ],
        acl_results=[
            PrimitiveResult(rc=1, failed=True, stderr="setfacl: denied")
        ],
    )
    result = _prepare(executor, base=base)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert any("scoped log access" in w for w in result.warnings)
    # The transport failure precedes every environment/live mutation.
    assert executor.applied == []
    assert all(call["validate_only"] for call in executor.reconciled)


def test_acl_repair_incomplete_helper_status_blocks_before_mutation():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, ready=False),
                    access_ready=False,
                    alloy_env_hash="",
                )
            )
        ],
        acl_results=[
            PrimitiveResult(
                rc=0,
                changed=False,
                facts={
                    "status": "failed",
                    "changed": False,
                    "repaired_roots": 0,
                    "repaired_files": 0,
                    "skipped_missing": 1,
                    "error_count": 1,
                    "reason": "declared root is unsafe",
                },
            )
        ],
    )
    result = _prepare(executor, base=base)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert any("declared root is unsafe" in w for w in result.warnings)
    assert executor.applied == []
    assert all(call["validate_only"] for call in executor.reconciled)


def test_acl_repair_malformed_helper_facts_block_before_mutation():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, ready=False),
                    access_ready=False,
                    alloy_env_hash="",
                )
            )
        ],
        acl_results=[
            PrimitiveResult(
                rc=0,
                changed=False,
                facts={
                    "status": "complete",
                    "changed": False,
                    "repaired_roots": 0,
                    "repaired_files": 0,
                    "skipped_missing": 0,
                    "error_count": 0,
                },
            )
        ],
    )
    result = _prepare(executor, base=base)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert any("malformed reason" in w for w in result.warnings)
    assert executor.applied == []
    assert all(call["validate_only"] for call in executor.reconciled)


def test_missing_acl_tools_blocks_without_running_commands():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, ready=False),
                    access_ready=False,
                    binaries={**_ALL_BINARIES, "setfacl": None, "getfacl": None},
                    alloy_env_hash=_env_hash(),
                )
            )
        ]
    )
    result = _prepare(executor, base=base)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert executor.applied == []
    assert executor.acl_calls == []
    assert all(not call.get("install") for call in executor.reconciled)
    assert all(not call.get("configure") for call in executor.reconciled)
    assert any("ACL tools missing" in w for w in result.warnings)
    assert any("--alloy-only" in w for w in result.warnings)


def test_explicit_alloy_setup_installs_missing_acl_prerequisite():
    """An existing Alloy install with absent ACL tools is bootstrapped by setup.

    The real helper reports file sources as unmeasurable when ``getfacl`` is
    unavailable; the explicit install path must still provision the guest ACL
    prerequisite, re-probe the same selection and finish configuring Alloy.
    """
    base = _base()
    missing_tools = {**_ALL_BINARIES, "setfacl": None, "getfacl": None}
    ready_logging = _logging_facts(_REQUEST)
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, status="unavailable"),
                    binaries=missing_tools,
                    alloy_env_hash=_env_hash(),
                )
            ),
            _probe(_facts(logging=ready_logging, alloy_env_hash=_env_hash())),
            _probe(_facts(logging=ready_logging, alloy_env_hash=_env_hash())),
        ]
    )

    result = _prepare(executor, base=base, allow_install=True)

    assert result.summary is not None and result.summary.status == "Configured"
    assert result.failed is False
    assert result.effective_alloy is not None
    install_calls = [call for call in executor.reconciled if call.get("install")]
    assert len(install_calls) == 1
    assert install_calls[0]["configure"] is False
    live_calls = [call for call in executor.reconciled if call.get("configure")]
    assert live_calls
    assert executor.reconciled.index(install_calls[0]) < executor.reconciled.index(
        live_calls[0]
    )
    assert executor.config_sha == result.effective_alloy.sha256
    assert executor.acl_calls == []


def _missing_acl_tools() -> Dict[str, Any]:
    return {**_ALL_BINARIES, "setfacl": None, "getfacl": None}


def test_explicit_alloy_setup_failure_blocks_before_validation_acl_or_config():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, status="unavailable"),
                    binaries=_missing_acl_tools(),
                    alloy_env_hash=_env_hash(),
                )
            )
        ],
        install_fails=True,
    )

    result = _prepare(executor, base=base, allow_install=True)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert any("installation failed" in w for w in result.warnings)
    assert executor.validation_calls == []
    assert executor.acl_calls == []
    assert executor.applied == []
    assert all(not call.get("configure") for call in executor.reconciled)


def test_explicit_alloy_setup_still_missing_tools_blocks_before_mutation():
    base = _base()
    unavailable = _logging_facts(_REQUEST, status="unavailable")
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=unavailable,
                    binaries=_missing_acl_tools(),
                    alloy_env_hash=_env_hash(),
                )
            ),
            _probe(
                _facts(
                    logging=unavailable,
                    binaries=_missing_acl_tools(),
                    alloy_env_hash=_env_hash(),
                )
            ),
        ]
    )

    result = _prepare(executor, base=base, allow_install=True)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert any(
        "ACL tools missing" in warning and "--alloy-only" in warning
        for warning in result.warnings
    )
    assert executor.validation_calls == []
    assert executor.acl_calls == []
    assert executor.applied == []
    assert all(not call.get("configure") for call in executor.reconciled)


def test_dry_run_reports_acl_package_without_install():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, status="unavailable"),
                    binaries=_missing_acl_tools(),
                    alloy_env_hash=_env_hash(),
                )
            )
        ]
    )

    result = _prepare(executor, base=base, allow_install=True, dry_run=True)

    assert result.summary is not None and result.summary.status == "Audit"
    assert result.failed is False
    assert executor.reconciled == []
    assert executor.acl_calls == []
    assert executor.applied == []
    assert executor.probe_calls == 1
    assert any(
        "acl' package" in warning and "--alloy-only" in warning
        for warning in result.warnings
    )


def test_explicit_alloy_setup_repairs_access_after_installing_one_tool():
    base = _base()
    denied = _logging_facts(_REQUEST, ready=False)
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=denied,
                    access_ready=False,
                    binaries={**_ALL_BINARIES, "setfacl": None},
                    alloy_env_hash=_env_hash(),
                )
            ),
            _probe(
                _facts(
                    logging=denied,
                    access_ready=False,
                    alloy_env_hash=_env_hash(),
                )
            ),
            _probe(_facts(logging=_logging_facts(_REQUEST), alloy_env_hash=_env_hash())),
        ]
    )

    result = _prepare(executor, base=base, allow_install=True)

    assert result.summary is not None and result.summary.status == "Configured"
    assert result.failed is False
    assert result.effective_alloy is not None
    install_calls = [call for call in executor.reconciled if call.get("install")]
    assert len(install_calls) == 1
    assert install_calls[0]["configure"] is False
    assert executor.acl_calls
    assert executor.acl_calls[0]["roots"] == ("/data/logs",)
    assert executor.config_sha == result.effective_alloy.sha256


def test_journal_only_selection_does_not_install_acl_tools():
    base = _base()
    selection = lp.select_log_plugins(
        _catalogue(), script_id="dashy", override=None, auto=True
    )
    logging = _logging_facts(lp.build_logging_request(selection))
    executor = ScriptedExecutor(
        [
            _probe(_facts(logging=logging, binaries=_missing_acl_tools(), alloy_env_hash=_env_hash())),
            _probe(_facts(logging=logging, binaries=_missing_acl_tools(), alloy_env_hash=_env_hash())),
        ]
    )

    result = _prepare(executor, base=base, script_id="dashy", allow_install=True)

    assert result.failed is False
    assert result.effective_alloy is not None
    assert all(not call.get("install") for call in executor.reconciled)


def test_empty_selection_does_not_install_acl_tools():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(None),
                    binaries=_missing_acl_tools(),
                    alloy_env_hash=_env_hash(),
                )
            ),
            _probe(
                _facts(
                    logging=_logging_facts(None),
                    binaries=_missing_acl_tools(),
                    alloy_env_hash=_env_hash(),
                )
            ),
        ]
    )

    result = _prepare(
        executor,
        base=base,
        allow_install=True,
        log_override=GuestLogOverride(mode="replace", plugins=[]),
    )

    assert result.failed is False
    assert all(not call.get("install") for call in executor.reconciled)


def test_missing_alloy_blocks_when_install_disallowed():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST),
                    alloy_env_hash=_env_hash(),
                )
            )
        ],
        alloy_binary=False,
    )
    result = _prepare(executor, base=base, allow_install=False)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert executor.reconciled == []
    assert executor.applied == []
    assert executor.acl_calls == []
    assert any("Alloy is not installed" in w for w in result.warnings)


def test_missing_alloy_installs_when_allowed_then_reprobes():
    base = _base()
    executor = ScriptedExecutor(
        [
            _probe(_facts(logging=_logging_facts(_REQUEST), alloy_env_hash=_env_hash())),
            _probe(_facts(logging=_logging_facts(_REQUEST), alloy_env_hash=_env_hash())),
            _probe(_facts(logging=_logging_facts(_REQUEST), alloy_env_hash=_env_hash())),
        ],
        alloy_binary=False,
    )
    result = _prepare(executor, base=base, allow_install=True)

    install_calls = [call for call in executor.reconciled if call.get("install")]
    assert install_calls and install_calls[0]["configure"] is False
    assert result.summary is not None and result.summary.status == "Configured"
    assert result.failed is False
    assert result.effective_alloy is not None


def test_fresh_alloy_setup_provisions_acl_tools_in_the_same_install():
    base = _base()
    ready_logging = _logging_facts(_REQUEST)
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, status="unavailable"),
                    binaries={
                        **_ALL_BINARIES,
                        "alloy": None,
                        "setfacl": None,
                        "getfacl": None,
                    },
                    alloy_env_hash=_env_hash(),
                )
            ),
            _probe(_facts(logging=ready_logging, alloy_env_hash=_env_hash())),
            _probe(_facts(logging=ready_logging, alloy_env_hash=_env_hash())),
        ],
        alloy_binary=False,
    )

    result = _prepare(executor, base=base, allow_install=True)

    assert result.summary is not None and result.summary.status == "Configured"
    assert result.failed is False
    assert result.effective_alloy is not None
    install_calls = [call for call in executor.reconciled if call.get("install")]
    assert len(install_calls) == 1
    assert install_calls[0]["configure"] is False
    assert executor.config_sha == result.effective_alloy.sha256


def test_unsupported_guest_os_blocks():
    base = _base()
    executor = ScriptedExecutor(
        [_probe(_facts(logging=_logging_facts(_REQUEST), os_type="alpine"))]
    )
    result = _prepare(executor, base=base)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert executor.applied == []
    assert any("Debian" in w for w in result.warnings)


def test_stopped_guest_with_selection_is_not_verified():
    base = _base()
    executor = ScriptedExecutor([_probe(_facts(running=False))])
    result = _prepare(executor, base=base)

    assert result.summary is None
    assert result.changed is False
    assert result.effective_alloy is None
    assert any("stopped" in w for w in result.warnings)
    assert executor.applied == []
    assert executor.acl_calls == []


def test_probe_failure_blocks_without_effective_config():
    executor = ScriptedExecutor(
        [PrimitiveResult(rc=1, failed=True, stderr="unreachable")]
    )
    result = _prepare(executor)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None


def test_custom_journal_source_reports_explicit_migration():
    base = _base(env=False)
    executor = ScriptedExecutor(
        [_probe(_facts())], alloy_config_sha=base.sha256
    )
    result = _prepare(
        executor, base=base, log_override=GuestLogOverride(mode="disabled")
    )

    assert result.summary is None
    assert executor.applied == []
    assert executor.reconciled == []
    assert any("explicit migration" in w for w in result.warnings)
    assert not result.failed


def test_loki_endpoint_conflict_blocks():
    base = _base(endpoint="http://10.0.0.1:3100")
    executor = ScriptedExecutor([_probe(_facts(logging=_logging_facts(_REQUEST)))])
    result = _prepare(
        executor,
        settings=GlobalSettings(housekeeping_loki_url="http://10.9.9.9:3100"),
        base=base,
    )

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert all("10.0.0.1" not in w and "10.9.9.9" not in w for w in result.warnings)
    assert executor.reconciled == []


def test_matching_loki_endpoint_is_accepted():
    base = _base(endpoint="http://10.10.10.39:3100")
    selection = lp.select_log_plugins(
        _catalogue(), script_id="nginxproxymanager", override=None, auto=True
    )
    candidate = housekeeping.render_lxc_log_config(
        base,
        node="pve",
        cluster="cluster",
        lxc_id="123",
        name="guest",
        sources=lp.validate_log_layout(
            selection, _logging_facts(_REQUEST)
        ).renderable_sources,
        retention_hours=48,
    )
    executor = ScriptedExecutor(
        [_probe(_facts(logging=_logging_facts(_REQUEST), alloy_env_hash=_env_hash()))],
        alloy_config_sha=candidate.sha256,
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
    executor = ScriptedExecutor(
        [
            _probe(
                _facts(
                    logging=_logging_facts(_REQUEST, ready=False),
                    access_ready=False,
                    alloy_env_hash="",
                )
            )
        ]
    )
    result = _prepare(executor, base=base, dry_run=True)

    assert result.summary is not None and result.summary.status == "Audit"
    assert result.changed is False
    assert result.failed is False
    assert executor.applied == []
    assert executor.acl_calls == []
    assert executor.reconciled == []
    assert any("scoped read" in w for w in result.warnings)
    assert any("environment drop-in" in w for w in result.warnings)
    assert any("validation is pending" in w for w in result.warnings)


def test_dry_run_compliant_guest_returns_no_summary():
    base = _base()
    executor = ScriptedExecutor(
        [_probe(_facts(alloy_env_hash=_env_hash()))],
        alloy_config_sha=base.sha256,
    )
    result = _prepare(
        executor,
        base=base,
        log_override=GuestLogOverride(mode="disabled"),
        dry_run=True,
    )

    assert result.summary is None
    assert result.warnings == []
    assert executor.applied == []
    assert executor.acl_calls == []
    assert executor.reconciled == []


@pytest.mark.parametrize("fault", ["none", "noop", "symlink"])
def test_environment_configuration_requires_observed_safe_file_state(tmp_path, fault):
    from tests.unit.test_housekeeping_retention import Guest as FilesystemGuest

    guest = FilesystemGuest(tmp_path / "guest")
    path = guest.put(
        housekeeping.FLEET_ALLOY_ENV_DROPIN, housekeeping.journal_env_content(24)
    )
    original = path.read_bytes()
    outside = tmp_path / "outside.conf"
    if fault == "symlink":
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
    guest.acknowledge_without_write = fault == "noop"
    base = _base()
    # The guest already runs the exact base config, so these cases exercise
    # only the environment drop-in write and its verification.
    guest.put("/etc/alloy/config.alloy", base.content)
    result = _prepare(
        guest,
        GlobalSettings(housekeeping_loki_url="http://10.0.0.1:3100"),
        base=base,
        log_override=GuestLogOverride(mode="disabled"),
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
