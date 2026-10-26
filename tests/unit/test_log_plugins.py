"""Behavioral tests for the declarative application-log catalogue.

Everything here uses real temporary YAML files, the real loader/selection
functions and plain fact mappings.  No live guest, Ansible run or Alloy binary
is involved.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from pydantic import ValidationError

from proxmox_fleet.housekeeping_checkpoint import GuestKey
from proxmox_fleet.log_plugins import (
    GENERIC_DISK_INVALID,
    GENERIC_DISK_NO_PROFILE,
    GENERIC_DISK_UNAVAILABLE,
    GuestOverrideError,
    LogCatalogueError,
    LogLayoutError,
    LogSelectionError,
    build_logging_request,
    community_script_identity,
    disk_usage_extra_paths,
    index_guest_log_overrides,
    load_log_catalogue,
    resolve_disk_usage_policy,
    select_log_plugins,
    validate_log_layout,
)
from proxmox_fleet.models.log_plugins import (
    DiskUsagePolicy,
    GuestLogOverride,
    GuestLogOverrideKey,
    LogPlugin,
)


# --------------------------------------------------------------------------- #
# Manifest / fact builders
# --------------------------------------------------------------------------- #


def _unit(
    name: str, *, scope: str = "system", user: Optional[str] = None
) -> Dict[str, Any]:
    unit: Dict[str, Any] = {"name": name, "scope": scope}
    if user is not None:
        unit["user"] = user
    return unit


def _source(
    source_id: str,
    app: str,
    *,
    root: Optional[str] = None,
    paths: Optional[List[str]] = None,
    exact_files: Optional[List[str]] = None,
    alternate_roots: Optional[List[str]] = None,
    exclude_paths: Optional[List[str]] = None,
    component: Optional[str] = None,
    required: bool = True,
    log_kind: Optional[str] = None,
    multiline: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    source: Dict[str, Any] = {
        "id": source_id,
        "app": app,
        "log_kind": log_kind or source_id,
        "required": required,
        "paths": list(paths or ([] if root is None else [f"{root}/*.log"])),
    }
    if root is not None:
        source["allowed_roots"] = [root]
    if exact_files is not None:
        source["exact_files"] = list(exact_files)
    if alternate_roots is not None:
        source["alternate_roots"] = list(alternate_roots)
    if exclude_paths is not None:
        source["exclude_paths"] = list(exclude_paths)
    if component is not None:
        source["component"] = component
    if multiline is not None:
        source["multiline"] = multiline
    return source


def _layout(
    *,
    layout_id: str = "default",
    required_units: Optional[List[Dict[str, Any]]] = None,
    running_units_any: Optional[List[Dict[str, Any]]] = None,
    sources: Optional[List[Dict[str, Any]]] = None,
    service_user: Optional[str] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "id": layout_id,
        "required_units": list(required_units or []),
        "running_units_any": list(running_units_any or []),
        "sources": list(sources or []),
    }
    if service_user is not None:
        body["service_user"] = service_user
    return body


def _manifest(
    plugin_id: str,
    *,
    kind: str = "app",
    sources: Optional[List[Dict[str, Any]]] = None,
    required_units: Optional[List[Dict[str, Any]]] = None,
    running_units_any: Optional[List[Dict[str, Any]]] = None,
    includes: Optional[List[str]] = None,
    community_script_ids: Optional[List[str]] = None,
    journal_units: Optional[List[Dict[str, Any]]] = None,
    layouts: Optional[List[Dict[str, Any]]] = None,
    disk_usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if community_script_ids is None:
        community_script_ids = [plugin_id] if kind == "app" else []
    if layouts is None:
        layouts = [
            _layout(
                required_units=required_units
                if required_units is not None
                else [_unit(f"{plugin_id}.service")],
                running_units_any=running_units_any,
                sources=sources,
            )
        ]
    body: Dict[str, Any] = {
        "schema_version": 1,
        "id": plugin_id,
        "display_name": f"Proxmox community script {plugin_id}",
        "kind": kind,
        "community_script_ids": community_script_ids,
        "journal_units": journal_units or [],
        "includes": includes or [],
        "layouts": layouts,
    }
    if disk_usage is not None:
        body["disk_usage"] = disk_usage
    return body


def _load(
    manifests: List[Dict[str, Any]], names: Optional[List[str]] = None
):
    directory = Path(tempfile.mkdtemp())
    for index, manifest in enumerate(manifests):
        name = names[index] if names is not None else f"{manifest['id']}.yml"
        (directory / name).write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return load_log_catalogue(directory)


def _model(manifest: Dict[str, Any]) -> LogPlugin:
    """Validate one manifest through a real YAML round-trip without the packaged set."""
    return LogPlugin.model_validate(
        yaml.safe_load(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
    )


def _root_fact(
    *,
    exists: bool = True,
    status: str = "ok",
    readable: bool = True,
    traversable: bool = True,
    is_dir: bool = True,
    is_symlink: bool = False,
    blocked_ancestor: bool = False,
    device: Optional[int] = 10,
) -> Dict[str, Any]:
    return {
        "exists": exists,
        "is_dir": is_dir,
        "is_symlink": is_symlink,
        "device": device if exists else None,
        "blocked_ancestor": blocked_ancestor,
        "access": {
            "traversable": traversable,
            "default_acl": True,
            "readable": readable,
        },
        "status": status,
        "error": None,
    }


def _file_entry(path: str, *, device: int = 10, inode: int = 20) -> Dict[str, Any]:
    return {"path": path, "device": device, "inode": inode, "size": 100, "mtime_ns": 1}


def _match(
    pattern: str,
    files: Optional[List[Dict[str, Any]]] = None,
    *,
    status: str = "ok",
    matched: Optional[int] = None,
    skipped: int = 0,
    truncated: bool = False,
) -> Dict[str, Any]:
    entries = list(files or [])
    return {
        "pattern": pattern,
        "matched": len(entries) if matched is None else matched,
        "skipped": skipped,
        "truncated": truncated,
        "status": status,
        "error": None,
        "files": entries,
    }


def _exact_fact(
    *,
    exists: bool = True,
    status: str = "ok",
    readable: bool = True,
    is_symlink: bool = False,
    same_device: bool = True,
    parent_traversable: bool = True,
    device: int = 30,
    inode: int = 40,
) -> Dict[str, Any]:
    return {
        "exists": exists,
        "is_symlink": is_symlink,
        "device": device if exists else None,
        "inode": inode if exists else None,
        "size": 10 if exists else None,
        "mtime_ns": 5 if exists else None,
        "same_device": same_device,
        "parent_traversable": parent_traversable,
        "access": {"readable": readable},
        "status": status,
        "error": None,
    }


def _source_fact(
    *,
    roots: Optional[Dict[str, Dict[str, Any]]] = None,
    exact_files: Optional[Dict[str, Dict[str, Any]]] = None,
    matches: Optional[List[Dict[str, Any]]] = None,
    status: str = "ok",
    truncated: bool = False,
) -> Dict[str, Any]:
    return {
        "status": status,
        "truncated": truncated,
        "roots": roots or {},
        "exact_files": exact_files or {},
        "matches": matches or [],
    }


def _simple_source_fact(
    root: str,
    pattern: str,
    files: Optional[List[Dict[str, Any]]] = None,
    *,
    root_fact: Optional[Dict[str, Any]] = None,
    match_status: str = "ok",
    truncated: bool = False,
    matched: Optional[int] = None,
) -> Dict[str, Any]:
    entries = list(files or [])
    return _source_fact(
        roots={root: root_fact or _root_fact(device=10)},
        matches=[
            _match(
                pattern,
                entries,
                status=match_status,
                matched=matched,
                truncated=truncated,
            )
        ],
    )


def _active_units(*names: str) -> Dict[str, Dict[str, Any]]:
    return {name: {"status": "active"} for name in names}


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def test_community_script_identity_reads_only_successful_pull_facts():
    wrapper = (
        'export UPDATE_SCRIPT_NAME="sonarr"\n'
        'bash -c "$(curl -fsSL "${URL}/ct/${UPDATE_SCRIPT_NAME}.sh")"\n'
    )
    assert community_script_identity({"pull_rc": 0, "script_stdout": wrapper}) == "sonarr"
    assert community_script_identity(
        {"pull_rc": "0", "script_stdout": "source <(curl -fsSL x/ct/radarr.sh)"}
    ) == "radarr"
    assert community_script_identity({"pull_rc": 1, "script_stdout": wrapper}) is None
    assert community_script_identity({"script_stdout": wrapper}) is None
    assert community_script_identity(
        {"pull_rc": 0, "script_stdout": "${URL}/ct/${UPDATE_SCRIPT_NAME}.sh"}
    ) is None
    assert community_script_identity({}) is None


# --------------------------------------------------------------------------- #
# Catalogue loading / schema
# --------------------------------------------------------------------------- #


def _demo_dependency() -> Dict[str, Any]:
    return _manifest(
        "demodb",
        kind="dependency",
        sources=[
            _source("database", "demodb", root="/var/log/demodb"),
        ],
    )


def _demo_app(includes: Optional[List[str]] = None) -> Dict[str, Any]:
    return _manifest(
        "demoapp",
        includes=includes or [],
        sources=[_source("application", "demoapp", root="/var/log/demoapp")],
    )


def test_load_and_exact_alias_automatic_selection():
    catalogue = _load([_demo_app(["demodb"]), _demo_dependency()])
    assert "demoapp" in catalogue.known_service_ids()
    assert "demodb" in catalogue.known_service_ids()
    assert catalogue.auto_plugin_id("demoapp") == "demoapp"
    assert catalogue.auto_plugin_id(None) is None
    assert catalogue.auto_plugin_id("debian") is None


def test_plugin_id_is_not_an_implicit_script_alias():
    explicit_only = _manifest(
        "explicit-only-manager",
        community_script_ids=[],
        sources=[],
        required_units=[_unit("fleet-dashboard.service")],
    )
    catalogue = _load([explicit_only])
    assert catalogue.auto_plugin_id("explicit-only-manager") is None
    assert catalogue.auto_plugin_id("debian") is None


def test_missing_local_catalogue_directory_is_an_error(tmp_path):
    with pytest.raises(LogCatalogueError, match="not a directory"):
        load_log_catalogue(tmp_path / "missing")


def test_manifest_requires_exact_filename_stem():
    with pytest.raises(LogCatalogueError, match="filename stem"):
        _load([_demo_app()], names=["wrong.yml"])


def test_manifest_unknown_fields_and_versions_fail_closed():
    extra = _demo_app()
    extra["commands"] = ["rm -rf /"]
    with pytest.raises(LogCatalogueError, match="extra_forbidden"):
        _load([extra])

    version = _demo_app()
    version["schema_version"] = 2
    with pytest.raises(LogCatalogueError):
        _load([version])

    nested = _demo_app()
    nested["layouts"][0]["sources"][0]["archive_directives"] = ["delete"]
    with pytest.raises(LogCatalogueError, match="extra_forbidden"):
        _load([nested])


def test_duplicate_ids_and_aliases_fail_closed():
    first = _demo_app()
    second = _demo_app()
    with pytest.raises(LogCatalogueError, match="duplicate plugin id"):
        _load([first, second], names=["demoapp.yml", "demoapp.yaml"])

    other = _manifest(
        "otherapp",
        community_script_ids=["demoapp"],
        sources=[_source("application", "otherapp", root="/var/log/otherapp")],
    )
    with pytest.raises(LogCatalogueError, match="claimed by"):
        _load([_demo_app(), other])

    alias_collision = _manifest(
        "aliasapp",
        community_script_ids=["demodb"],
        sources=[_source("application", "aliasapp", root="/var/log/aliasapp")],
    )
    with pytest.raises(LogCatalogueError, match="conflicts with service id"):
        _load([_demo_dependency(), alias_collision])


def test_include_graph_rejects_cycles_missing_and_illegal_edges():
    app_cycle = _manifest(
        "app-a",
        includes=["dep-b"],
        sources=[_source("application", "app-a", root="/var/log/app-a")],
    )
    dep_cycle = _manifest(
        "dep-b",
        kind="dependency",
        includes=["app-a"],
        sources=[_source("database", "dep-b", root="/var/log/dep-b")],
    )
    with pytest.raises(LogCatalogueError, match="include cycle"):
        _load([app_cycle, dep_cycle])

    with pytest.raises(LogCatalogueError, match="includes unknown service"):
        _load([_demo_app(["missing"])])

    second_app = _manifest(
        "other-app",
        sources=[_source("application", "other-app", root="/var/log/other-app")],
    )
    with pytest.raises(LogCatalogueError, match="must include dependencies"):
        _load([_demo_app(["other-app"]), second_app])

    first_dep = _manifest(
        "dep-a",
        kind="dependency",
        includes=["dep-b"],
        sources=[_source("database", "dep-a", root="/var/log/dep-a")],
    )
    second_dep = _manifest(
        "dep-b",
        kind="dependency",
        sources=[_source("database", "dep-b", root="/var/log/dep-b")],
    )
    with pytest.raises(LogCatalogueError, match="must not declare includes"):
        _load([first_dep, second_dep])


def test_dependency_aliases_are_rejected():
    dependency = _demo_dependency()
    dependency["community_script_ids"] = ["demodb"]
    with pytest.raises(LogCatalogueError, match="must not declare community_script_ids"):
        _load([dependency])


def test_source_less_layout_needs_a_real_primary_predicate():
    bad = _manifest("journalapp", sources=[], required_units=[])
    with pytest.raises(LogCatalogueError, match="source-less layout"):
        _load([bad])


def test_app_without_includes_needs_predicate_and_dependency_too():
    app_no_predicate = _manifest(
        "app-a",
        sources=[_source("application", "app-a", root="/var/log/app-a")],
        required_units=[],
        layouts=[_layout(sources=[_source("application", "app-a", root="/var/log/app-a")])],
    )
    with pytest.raises(LogCatalogueError, match="app without includes"):
        _load([app_no_predicate])

    dep_no_predicate = _manifest(
        "dep-a",
        kind="dependency",
        sources=[_source("database", "dep-a", root="/var/log/dep-a")],
        required_units=[],
        layouts=[_layout(sources=[_source("database", "dep-a", root="/var/log/dep-a")])],
    )
    with pytest.raises(LogCatalogueError, match="dependency layout"):
        _load([dep_no_predicate])


def test_journal_units_do_not_supply_primary_running_evidence():
    plugin = _manifest(
        "journalapp",
        journal_units=[_unit("journalapp.service")],
        sources=[],
        required_units=[],
    )
    with pytest.raises(LogCatalogueError, match="source-less layout"):
        _load([plugin])


@pytest.mark.parametrize(
    "root",
    [
        "/",
        "/var",
        "/var/log",
        "/opt",
        "/home/user",
        "/home/user/.ssh",
        "/proc/1",
        "/dev",
        "relative/logs",
        "/var/log/../etc",
        "/var/log/{bad}",
        "/var/log/*",
    ],
)
def test_unsafe_directory_roots_are_rejected(root: str):
    manifest = _manifest(
        "unsafeapp",
        sources=[
            {
                "id": "application",
                "app": "unsafeapp",
                "log_kind": "application",
                "allowed_roots": [root],
                "paths": ["/var/log/unsafeapp/*.log"],
            }
        ],
    )
    with pytest.raises(LogCatalogueError):
        _load([manifest])


def test_source_paths_must_stay_inside_declared_scope():
    outside = _manifest(
        "demoapp",
        sources=[
            _source(
                "application",
                "demoapp",
                root="/var/log/demoapp",
                paths=["/var/log/other/*.log"],
            )
        ],
    )
    with pytest.raises(LogCatalogueError, match="must be beneath an allowed root"):
        _load([outside])

    glob_exact = _manifest(
        "demoapp",
        sources=[
            _source(
                "application",
                "demoapp",
                root=None,
                paths=[],
                exact_files=["/var/log/demoapp/*.log"],
            )
        ],
    )
    with pytest.raises(LogCatalogueError, match="glob"):
        _load([glob_exact])

    bad_exclusion = _manifest(
        "demoapp",
        sources=[
            _source(
                "application",
                "demoapp",
                root="/var/log/demoapp",
                exclude_paths=["/var/log/other/**/*.log"],
            )
        ],
    )
    with pytest.raises(LogCatalogueError, match="exclusion"):
        _load([bad_exclusion])


def test_overlapping_source_patterns_are_rejected():
    overlapping = _manifest(
        "demoapp",
        sources=[
            _source(
                "application",
                "demoapp",
                root="/var/log/demoapp",
                paths=[
                    "/var/log/demoapp/*.log",
                    "/var/log/demoapp/sub/*.log",
                ],
            )
        ],
    )
    with pytest.raises(LogCatalogueError, match="overlapping source patterns"):
        _load([overlapping])


def test_rabbitmq_style_disjoint_files_and_exclusions_are_representable():
    source = _source(
        "queue",
        "test-rabbitmq-patterns",
        root="/var/log/rabbitmq",
        paths=[
            "/var/log/rabbitmq/rabbit@*.log",
            "/var/log/rabbitmq/rabbitmq-server.log",
            "/var/log/rabbitmq/rabbitmq-server.error.log",
        ],
        exclude_paths=["/var/log/rabbitmq/*.old.log"],
    )
    catalogue = _load(
        [
            _manifest(
                "test-rabbitmq-patterns",
                kind="dependency",
                sources=[source],
            )
        ]
    )
    assert catalogue.plugin("test-rabbitmq-patterns").layouts[0].sources[
        0
    ].paths[0].startswith("/var/log/rabbitmq/rabbit@")


def test_distinct_concrete_files_with_common_prefix_are_representable():
    catalogue = _load(
        [
            _manifest(
                "concrete",
                sources=[
                    _source(
                        "application",
                        "concrete",
                        root=None,
                        paths=[],
                        exact_files=[
                            "/var/log/concrete/app.log",
                            "/var/log/concrete/app.log.1",
                        ],
                    )
                ],
            )
        ]
    )
    assert catalogue.plugin("concrete").layouts[0].sources[0].exact_files == [
        "/var/log/concrete/app.log",
        "/var/log/concrete/app.log.1",
    ]


def test_exact_file_only_source_is_representable():
    catalogue = _load(
        [
            _manifest(
                "test-exactapp",
                sources=[
                    _source(
                        "error",
                        "test-exactapp",
                        root=None,
                        paths=[],
                        exact_files=["/opt/uptime-kuma/data/error.log"],
                        required=False,
                    )
                ],
            )
        ]
    )
    source = catalogue.plugin("test-exactapp").layouts[0].sources[0]
    assert source.exact_files == ["/opt/uptime-kuma/data/error.log"]
    assert source.allowed_roots == []


def test_legacy_npm_pbs_labels_and_components_are_preserved():
    legacy = _manifest(
        "proxmox-backup-server",
        sources=[
            _source(
                "tasks",
                "proxmox-backup",
                root="/var/log/proxmox-backup/tasks",
                paths=["/var/log/proxmox-backup/tasks/[0-9A-F][0-9A-F]/UPID:*"],
                log_kind="task",
                component="fleet_pbs_tasks",
            ),
            _source(
                "api",
                "proxmox-backup",
                root=None,
                paths=[],
                exact_files=[
                    "/var/log/proxmox-backup/api/access.log",
                    "/var/log/proxmox-backup/api/auth.log",
                ],
                log_kind="api",
                component="fleet_pbs_api",
            ),
        ],
    )
    plugin = _model(legacy)
    assert plugin.layouts[0].sources[0].component == "fleet_pbs_tasks"
    assert plugin.layouts[0].sources[1].exact_files == [
        "/var/log/proxmox-backup/api/access.log",
        "/var/log/proxmox-backup/api/auth.log",
    ]

    broken = _manifest(
        "nginxproxymanager",
        sources=[
            _source(
                "application",
                "nginxproxymanager",
                root="/data/logs",
                paths=["/data/logs/**/*.log"],
                component="fleet_npm_other",
            )
        ],
    )
    with pytest.raises(ValidationError, match="legacy source"):
        _model(broken)


def test_new_sources_must_use_plugin_id_and_source_id_labels():
    wrong_app = _manifest(
        "demoapp",
        sources=[
            _source("application", "not-demoapp", root="/var/log/demoapp"),
        ],
    )
    with pytest.raises(LogCatalogueError, match="must use app="):
        _load([wrong_app])

    wrong_kind = _manifest(
        "demoapp",
        sources=[
            _source(
                "application",
                "demoapp",
                root="/var/log/demoapp",
                log_kind="application-legacy",
            )
        ],
    )
    with pytest.raises(LogCatalogueError, match="must use app="):
        _load([wrong_kind])


def test_dependency_sources_cannot_pin_components():
    dependency = _demo_dependency()
    dependency["layouts"][0]["sources"][0]["component"] = "fleet_demodb"
    with pytest.raises(LogCatalogueError, match="must not pin a component"):
        _load([dependency])


def test_duplicate_or_reserved_components_are_rejected():
    first = _manifest(
        "one",
        sources=[_source("application", "one", root="/var/log/one", component="fleet_fixed")],
    )
    second = _manifest(
        "two",
        sources=[_source("application", "two", root="/var/log/two", component="fleet_fixed")],
    )
    with pytest.raises(LogCatalogueError, match="used by both"):
        _load([first, second])

    reserved = _manifest(
        "one",
        sources=[
            _source("application", "one", root="/var/log/one", component="fleet_file_pack")
        ],
    )
    with pytest.raises(LogCatalogueError, match="reserved"):
        _load([reserved])


def test_unit_evidence_validation():
    with pytest.raises(LogCatalogueError):
        _load(
            [
                _manifest(
                    "userunit",
                    sources=[],
                    required_units=[_unit("app.service", scope="user")],
                )
            ]
        )
    with pytest.raises(LogCatalogueError):
        _load(
            [
                _manifest(
                    "systemunit",
                    sources=[],
                    required_units=[_unit("app.service", user="alice")],
                )
            ]
        )
    with pytest.raises(LogCatalogueError):
        _load(
            [
                _manifest(
                    "shellunit",
                    sources=[],
                    required_units=[_unit("app.service; rm -rf /")],
                )
            ]
        )

    glob = _load(
        [
            _manifest(
                "test-postgresql",
                kind="dependency",
                sources=[],
                required_units=[_unit("postgresql@*-main.service")],
            )
        ]
    )
    assert glob.plugin("test-postgresql").layouts[0].required_units[0].name.endswith(
        ".service"
    )


def test_disk_policy_validation():
    for bad in (0, 101, float("nan"), float("inf"), "20"):
        manifest = _manifest(
            "demoapp",
            sources=[_source("application", "demoapp", root="/var/log/demoapp")],
            disk_usage={"default_max_share_percent": bad},
        )
        with pytest.raises(LogCatalogueError):
            _load([manifest])
    for bad_floor in (-1, 1.5, True):
        manifest = _manifest(
            "demoapp",
            sources=[_source("application", "demoapp", root="/var/log/demoapp")],
            disk_usage={"min_directory_mb": bad_floor},
        )
        with pytest.raises(LogCatalogueError):
            _load([manifest])

    for prefix in ("/", "relative", "/var/*", "/var/../etc", "/" + "/".join(["x"] * 17)):
        manifest = _manifest(
            "demoapp",
            sources=[_source("application", "demoapp", root="/var/log/demoapp")],
            disk_usage={"path_max_share_percent": {prefix: 50}},
        )
        with pytest.raises(LogCatalogueError):
            _load([manifest])

    long_prefix = "/" + ("x" * 600)
    manifest = _manifest(
        "demoapp",
        sources=[_source("application", "demoapp", root="/var/log/demoapp")],
        disk_usage={"path_max_share_percent": {long_prefix: 50}},
    )
    with pytest.raises(LogCatalogueError):
        _load([manifest])

    too_many = {f"/p/x{index}": 20 for index in range(33)}
    manifest = _manifest(
        "demoapp",
        sources=[_source("application", "demoapp", root="/var/log/demoapp")],
        disk_usage={"path_max_share_percent": too_many},
    )
    with pytest.raises(LogCatalogueError):
        _load([manifest])


# --------------------------------------------------------------------------- #
# Selection modes / overrides
# --------------------------------------------------------------------------- #


def _selected(catalogue, script_id: str, override: Optional[GuestLogOverride] = None):
    return select_log_plugins(
        catalogue, script_id=script_id, override=override, auto=True
    )


def test_auto_selects_identity_and_include_roots():
    catalogue = _load([_demo_app(["demodb"]), _demo_dependency()])
    selection = _selected(catalogue, "demoapp")
    assert selection.status == "selected"
    assert selection.owner_ids == ("demoapp",)
    assert selection.mode == "auto"
    assert any("auto identity" in item for item in selection.evidence)


def test_unsupported_identity_is_distinct_from_empty_selection():
    catalogue = _load([_demo_app()])
    unsupported = select_log_plugins(
        catalogue, script_id="debian", override=None, auto=True
    )
    assert unsupported.status == "unsupported"
    assert unsupported.owner_ids == ()

    no_identity = select_log_plugins(
        catalogue, script_id=None, override=None, auto=True
    )
    assert no_identity.status == "unsupported"

    disabled = select_log_plugins(
        catalogue,
        script_id="demoapp",
        override=GuestLogOverride(mode="disabled"),
        auto=True,
    )
    assert disabled.status == "empty"

    auto_off = select_log_plugins(
        catalogue, script_id="demoapp", override=None, auto=False
    )
    assert auto_off.status == "empty"

    empty_replace = select_log_plugins(
        catalogue,
        script_id="demoapp",
        override=GuestLogOverride(mode="replace", plugins=[]),
        auto=True,
    )
    assert empty_replace.status == "empty"


def test_replace_extend_disable_plugin_semantics():
    catalogue = _load([_demo_app(["demodb"]), _demo_dependency()])

    replaced = select_log_plugins(
        catalogue,
        script_id="debian",
        override=GuestLogOverride(mode="replace", plugins=["demodb"]),
        auto=True,
    )
    assert replaced.status == "selected"
    assert replaced.owner_ids == ("demodb",)

    extended = select_log_plugins(
        catalogue,
        script_id="demoapp",
        override=GuestLogOverride(mode="extend", plugins=["demodb"]),
        auto=True,
    )
    assert extended.owner_ids == ("demoapp", "demodb")

    disabled = select_log_plugins(
        catalogue,
        script_id="demoapp",
        override=GuestLogOverride(mode="auto", disable_plugins=["demoapp"]),
        auto=True,
    )
    assert disabled.status == "empty"

    auto_off_replace = select_log_plugins(
        catalogue,
        script_id=None,
        override=GuestLogOverride(mode="replace", plugins=["demodb"]),
        auto=False,
    )
    assert auto_off_replace.status == "selected"


def test_invalid_selection_overrides_fail_closed():
    catalogue = _load([_demo_app(["demodb"]), _demo_dependency()])
    with pytest.raises(LogSelectionError, match="unknown service"):
        select_log_plugins(
            catalogue,
            script_id=None,
            override=GuestLogOverride(mode="replace", plugins=["nope"]),
            auto=True,
        )
    with pytest.raises(LogSelectionError, match="unknown source override"):
        select_log_plugins(
            catalogue,
            script_id="demoapp",
            override=GuestLogOverride(
                mode="auto", disable_sources=["demoapp/not-a-source"]
            ),
            auto=True,
        )
    with pytest.raises(LogSelectionError, match="unknown layout"):
        select_log_plugins(
            catalogue,
            script_id="demoapp",
            override=GuestLogOverride(mode="auto", layouts={"demoapp": "missing"}),
            auto=True,
        )
    with pytest.raises(LogSelectionError, match="cannot be disabled"):
        select_log_plugins(
            catalogue,
            script_id="demoapp",
            override=GuestLogOverride(mode="auto", disable_plugins=["demodb"]),
            auto=True,
        )
    with pytest.raises(LogSelectionError, match="unknown source override"):
        select_log_plugins(
            catalogue,
            script_id="debian",
            override=GuestLogOverride(
                mode="auto", disable_sources=["demoapp/not-a-source"]
            ),
            auto=True,
        )


def test_override_nested_validation_fails_closed():
    with pytest.raises(ValidationError, match="must not carry explicit plugins"):
        GuestLogOverride(mode="auto", plugins=["demoapp"])
    with pytest.raises(ValidationError, match="must not carry"):
        GuestLogOverride(mode="disabled", disable_sources=["demoapp/application"])
    with pytest.raises(ValidationError, match="overlap"):
        GuestLogOverride(
            mode="extend",
            plugins=["demoapp"],
            disable_plugins=["demoapp"],
        )
    with pytest.raises(ValidationError, match="invalid source key"):
        GuestLogOverride(mode="replace", plugins=["demoapp"], disable_sources=["a/b/c/d"])
    with pytest.raises(ValidationError, match="invalid locations source key"):
        GuestLogOverride(
            mode="replace",
            plugins=["demoapp"],
            locations={"a/b/c/d": ["/var/log/x/*.log"]},
        )
    with pytest.raises(ValidationError, match="invalid layout override key"):
        GuestLogOverride(
            mode="replace", plugins=["demoapp"], layouts={"a/b/c": "default"}
        )
    with pytest.raises(ValidationError):
        GuestLogOverride(
            mode="replace",
            plugins=["demoapp"],
            locations={"demoapp/application": ["/var/../etc/passwd"]},
        )


# --------------------------------------------------------------------------- #
# Guest override keys
# --------------------------------------------------------------------------- #


def test_guest_override_key_parsing():
    assert GuestLogOverrideKey.parse("135").guest_id == "135"
    full = GuestLogOverrideKey.parse("default/pve01/135")
    assert full.cluster == "default"
    assert full.node == "pve01"
    assert full.qualified == "default/pve01/135"
    for bad in ("default/135", "default/pve01/0135", "not-a-number", "", "a/b/c/d"):
        with pytest.raises(ValueError):
            GuestLogOverrideKey.parse(bad)


def test_guest_override_index_full_key_precedence_and_bare_uniqueness():
    discovered = [
        GuestKey("default", "pve01", "135"),
        GuestKey("other", "pve02", "200"),
    ]
    overrides = {
        "default/pve01/135": GuestLogOverride(mode="replace", plugins=["one"]),
        "135": GuestLogOverride(mode="disabled"),
        "200": GuestLogOverride(mode="disabled"),
    }
    index = index_guest_log_overrides(overrides, discovered=discovered)
    guest = discovered[0]
    full_override = index.for_guest(guest)
    assert full_override is not None
    assert full_override.mode == "replace"
    other_override = index.for_guest(discovered[1])
    assert other_override is not None
    assert other_override.mode == "disabled"

    ambiguous = list(discovered) + [GuestKey("third", "pve03", "135")]
    with pytest.raises(GuestOverrideError, match="ambiguous"):
        index_guest_log_overrides(
            {"135": GuestLogOverride()}, discovered=ambiguous
        )
    with pytest.raises(GuestOverrideError, match="matches no"):
        index_guest_log_overrides(
            {"999": GuestLogOverride()}, discovered=discovered
        )


# --------------------------------------------------------------------------- #
# Logging request shape
# --------------------------------------------------------------------------- #


def test_logging_request_has_per_layout_source_descriptors_and_no_union():
    catalogue = _load(
        [
            _manifest(
                "demoapp",
                sources=[
                    _source(
                        "application",
                        "demoapp",
                        root="/var/log/demoapp",
                        alternate_roots=["/srv/demoapp-logs"],
                        component="fleet_demoapp",
                    )
                ],
            )
        ]
    )
    selection = _selected(catalogue, "demoapp")
    request = build_logging_request(selection)
    source_entry = request["sources"]["demoapp/application"]
    descriptor = source_entry["layouts"]["default"]
    assert descriptor["roots"] == ["/var/log/demoapp"]
    assert descriptor["component"] == "fleet_demoapp"
    assert descriptor["app"] == "demoapp"
    assert descriptor["log_kind"] == "application"
    assert (
        "/var/log/demoapp/**/.fleet-housekeeping-quarantine/**"
        in descriptor["exclude_paths"]
    )
    assert "commands" not in descriptor
    assert request["plugins"]["demoapp"]["layouts"][0]["source_ids"] == [
        "application"
    ]

    located = _selected(
        catalogue,
        "demoapp",
        GuestLogOverride(
            mode="auto",
            locations={"demoapp/application": ["/srv/demoapp-logs/**/*.log"]},
        ),
    )
    located_request = build_logging_request(located)
    located_descriptor = located_request["sources"]["demoapp/application"][
        "layouts"
    ]["default"]
    assert located_descriptor["roots"] == ["/srv/demoapp-logs"]
    assert "/var/log/demoapp" not in located_descriptor["roots"]


def test_location_widening_and_exact_file_relocation_fail_closed():
    catalogue = _load(
        [
            _manifest(
                "demoapp",
                sources=[_source("application", "demoapp", root="/var/log/demoapp")],
            )
        ]
    )
    with pytest.raises(LogSelectionError, match="outside the source's audited roots"):
        _selected(
            catalogue,
            "demoapp",
            GuestLogOverride(
                mode="auto",
                locations={"demoapp/application": ["/var/log/other/*.log"]},
            ),
        )

    kuma = _load(
        [
            _manifest(
                "test-exactapp",
                sources=[
                    _source(
                        "error",
                        "test-exactapp",
                        root=None,
                        paths=[],
                        exact_files=["/opt/uptime-kuma/data/error.log"],
                    )
                ],
            )
        ]
    )
    with pytest.raises(LogSelectionError, match="not one of the source's exact files"):
        _selected(
            kuma,
            "test-exactapp",
            GuestLogOverride(
                mode="auto",
                locations={
                    "test-exactapp/error": ["/opt/uptime-kuma/data/other.log"]
                },
            ),
        )


# --------------------------------------------------------------------------- #
# Layout validation
# --------------------------------------------------------------------------- #


def _owner_dep_facts(
    *,
    owner_unit: str = "system:demoapp.service",
    dep_unit: str = "system:demodb.service",
    dep_status: str = "active",
    source_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    facts: Dict[str, Any] = {
        "plugins": {
            "demoapp": {
                "units": {owner_unit: {"status": "active"}},
                "includes": {"demodb": {"units": {dep_unit: {"status": dep_status}}}},
            }
        },
        "sources": {
            "demoapp/application": {
                "layouts": {
                    "default": _simple_source_fact(
                        "/var/log/demoapp",
                        "/var/log/demoapp/*.log",
                        [_file_entry("/var/log/demoapp/app.log", inode=11)],
                    )
                }
            },
            "demoapp/demodb/database": {
                "layouts": {
                    "default": _simple_source_fact(
                        "/var/log/demodb",
                        "/var/log/demodb/*.log",
                        [_file_entry("/var/log/demodb/db.log", inode=12)],
                    )
                }
            },
        },
    }
    if source_overrides:
        facts["sources"].update(source_overrides)
    return facts


def test_layout_validation_resolves_owner_dependency_and_labels():
    catalogue = _load(
        [
            _manifest(
                "demoapp",
                includes=["demodb"],
                sources=[
                    _source(
                        "application",
                        "demoapp",
                        root="/var/log/demoapp",
                        component="fleet_demoapp",
                    )
                ],
            ),
            _demo_dependency(),
        ]
    )
    selection = _selected(catalogue, "demoapp")
    request = build_logging_request(selection)
    dependency_source = request["sources"]["demoapp/demodb/database"][
        "layouts"
    ]["default"]
    assert dependency_source["app"] == "demoapp"
    assert dependency_source["log_kind"] == "database"
    assert dependency_source["component"].startswith("fleet_")
    assert dependency_source["component"] != "fleet_demodb"

    resolved = validate_log_layout(selection, _owner_dep_facts())
    assert [service.service_id for service in resolved.services] == [
        "demoapp",
        "demodb",
    ]
    assert [service.layout_id for service in resolved.services] == [
        "default",
        "default",
    ]
    assert [source.key for source in resolved.sources] == [
        "demoapp/application",
        "demoapp/demodb/database",
    ]
    assert resolved.sources[0].app == "demoapp"
    assert resolved.sources[1].app == "demoapp"
    assert resolved.sources[1].service_id == "demodb"
    assert resolved.sources[1].log_kind == "database"
    assert resolved.sources[0].component == "fleet_demoapp"
    assert (
        "/var/log/demoapp/**/.fleet-housekeeping-quarantine/**"
        in resolved.sources[0].exclude_paths
    )
    assert resolved.sources[0].status == "active"
    assert resolved.complete is True
    assert not resolved.blocked


def test_standalone_dependency_source_uses_its_own_owner_id():
    catalogue = _load([_demo_app(["demodb"]), _demo_dependency()])
    selection = select_log_plugins(
        catalogue,
        script_id=None,
        override=GuestLogOverride(mode="replace", plugins=["demodb"]),
        auto=True,
    )
    facts = {
        "plugins": {
            "demodb": {"units": {"system:demodb.service": {"status": "active"}}}
        },
        "sources": {
            "demodb/database": {
                "layouts": {
                    "default": _simple_source_fact(
                        "/var/log/demodb",
                        "/var/log/demodb/*.log",
                        [_file_entry("/var/log/demodb/db.log")],
                    )
                }
            }
        },
    }
    resolved = validate_log_layout(selection, facts)
    assert resolved.sources[0].owner_id == "demodb"
    assert resolved.sources[0].service_id == "demodb"
    assert resolved.sources[0].app == "demodb"
    assert resolved.sources[0].log_kind == "database"


def test_journal_only_service_retains_evidence_but_never_sources():
    catalogue = _load(
        [
            _manifest(
                "journalapp",
                sources=[],
                required_units=[_unit("journalapp.service")],
                journal_units=[
                    _unit("journalapp-optional.service"),
                    _unit("journalapp-gateway.service", scope="user", user="hermes"),
                ],
            )
        ]
    )
    selection = _selected(catalogue, "journalapp")
    resolved = validate_log_layout(
        selection,
        {
            "plugins": {
                "journalapp": {
                    "units": {
                        "system:journalapp.service": {"status": "active"},
                        "system:journalapp-optional.service": {"status": "inactive"},
                        "user:hermes:journalapp-gateway.service": {
                            "status": "unavailable"
                        },
                    }
                }
            }
        },
    )
    assert resolved.services[0].layout_id == "default"
    assert resolved.sources == ()
    statuses = {unit.key: unit.status for unit in resolved.services[0].journal_units}
    assert statuses["system:journalapp-optional.service"] == "inactive"
    assert statuses["user:hermes:journalapp-gateway.service"] == "unavailable"
    assert resolved.complete is True


def test_missing_journal_unit_evidence_is_incomplete():
    catalogue = _load(
        [
            _manifest(
                "journalapp",
                sources=[],
                required_units=[_unit("journalapp.service")],
                journal_units=[_unit("journalapp-extra.service")],
            )
        ]
    )
    selection = _selected(catalogue, "journalapp")
    with pytest.raises(LogLayoutError, match="missing requested unit evidence"):
        validate_log_layout(
            selection,
            {
                "plugins": {
                    "journalapp": {
                        "units": {"system:journalapp.service": {"status": "active"}}
                    }
                }
            },
        )


def test_inactive_required_unit_blocks_and_unavailable_is_distinct():
    catalogue = _load([_demo_app()])
    selection = _selected(catalogue, "demoapp")
    with pytest.raises(LogLayoutError, match="demoapp.service is inactive"):
        validate_log_layout(
            selection,
            {
                "plugins": {
                    "demoapp": {
                        "units": {"system:demoapp.service": {"status": "inactive"}}
                    }
                }
            },
        )
    with pytest.raises(LogLayoutError, match="demoapp.service is unavailable"):
        validate_log_layout(
            selection,
            {
                "plugins": {
                    "demoapp": {
                        "units": {"system:demoapp.service": {"status": "unavailable"}}
                    }
                }
            },
        )


def test_hermes_style_running_any_allows_optional_inactive_gateway():
    catalogue = _load(
        [
            _manifest(
                "test-agent",
                sources=[],
                required_units=[],
                running_units_any=[
                    _unit("hermes-dashboard.service"),
                    _unit("hermes-gateway.service"),
                    _unit("hermes-gateway.service", scope="user", user="hermes"),
                ],
            )
        ]
    )
    selection = _selected(catalogue, "test-agent")
    resolved = validate_log_layout(
        selection,
        {
            "plugins": {
                "test-agent": {
                    "units": {
                        "system:hermes-dashboard.service": {"status": "active"},
                        "system:hermes-gateway.service": {"status": "inactive"},
                        "user:hermes:hermes-gateway.service": {"status": "unavailable"},
                    }
                }
            }
        },
    )
    assert resolved.services[0].layout_id == "default"


def test_ambiguous_layouts_require_explicit_selection():
    catalogue = _load(
        [
            _manifest(
                "demoapp",
                layouts=[
                    _layout(
                        layout_id="first",
                        required_units=[_unit("demoapp-a.service")],
                        sources=[
                            _source(
                                "application",
                                "demoapp",
                                root="/var/log/demoapp-a",
                            )
                        ],
                    ),
                    _layout(
                        layout_id="second",
                        required_units=[_unit("demoapp-b.service")],
                        sources=[
                            _source(
                                "application",
                                "demoapp",
                                root="/var/log/demoapp-b",
                            )
                        ],
                    ),
                ],
            )
        ]
    )
    selection = _selected(catalogue, "demoapp")
    facts = {
        "plugins": {
            "demoapp": {
                "units": {
                    "system:demoapp-a.service": {"status": "active"},
                    "system:demoapp-b.service": {"status": "active"},
                }
            }
        }
    }
    with pytest.raises(LogLayoutError, match="ambiguous layout"):
        validate_log_layout(selection, facts)

    explicit = select_log_plugins(
        catalogue,
        script_id="demoapp",
        override=GuestLogOverride(mode="auto", layouts={"demoapp": "second"}),
        auto=True,
    )
    assert explicit.layouts == {"demoapp": "second"}


def test_same_source_id_across_layout_variants_is_per_layout():
    catalogue = _load(
        [
            _manifest(
                "demoapp",
                layouts=[
                    _layout(
                        layout_id="v1",
                        required_units=[_unit("demoapp-v1.service")],
                        sources=[
                            _source(
                                "application",
                                "demoapp",
                                root="/var/log/demoapp-v1",
                            )
                        ],
                    ),
                    _layout(
                        layout_id="v2",
                        required_units=[_unit("demoapp-v2.service")],
                        sources=[
                            _source(
                                "application",
                                "demoapp",
                                root="/var/log/demoapp-v2",
                            )
                        ],
                    ),
                ],
            )
        ]
    )
    selection = _selected(catalogue, "demoapp")
    request = build_logging_request(selection)
    layouts = request["sources"]["demoapp/application"]["layouts"]
    assert set(layouts) == {"v1", "v2"}
    assert layouts["v1"]["paths"] == ["/var/log/demoapp-v1/*.log"]
    assert layouts["v2"]["paths"] == ["/var/log/demoapp-v2/*.log"]

    resolved = validate_log_layout(
        selection,
        {
            "plugins": {
                "demoapp": {
                    "units": {
                        "system:demoapp-v1.service": {"status": "active"},
                        "system:demoapp-v2.service": {"status": "inactive"},
                    }
                }
            },
            "sources": {
                "demoapp/application": {
                    "layouts": {
                        "v1": _simple_source_fact(
                            "/var/log/demoapp-v1",
                            "/var/log/demoapp-v1/*.log",
                            [_file_entry("/var/log/demoapp-v1/app.log")],
                        ),
                        "v2": _simple_source_fact(
                            "/var/log/demoapp-v2",
                            "/var/log/demoapp-v2/*.log",
                            [_file_entry("/var/log/demoapp-v2/app.log", inode=99)],
                        ),
                    }
                }
            },
        },
    )
    assert resolved.services[0].layout_id == "v1"
    assert resolved.sources[0].paths == ("/var/log/demoapp-v1/*.log",)


def test_dependency_evidence_failure_blocks_whole_bundle():
    catalogue = _load([_demo_app(["demodb"]), _demo_dependency()])
    selection = _selected(catalogue, "demoapp")
    facts = _owner_dep_facts(dep_status="inactive")
    with pytest.raises(LogLayoutError, match="no layout matched"):
        validate_log_layout(selection, facts)


def test_missing_source_evidence_is_never_verified():
    catalogue = _load([_demo_app()])
    selection = _selected(catalogue, "demoapp")
    with pytest.raises(LogLayoutError, match="missing requested source evidence"):
        validate_log_layout(
            selection,
            {
                "plugins": {
                    "demoapp": {
                        "units": {"system:demoapp.service": {"status": "active"}}
                    }
                }
            },
        )


def test_optional_missing_root_is_inactive_required_root_is_blocked():
    optional = _load(
        [
            _manifest(
                "demoapp",
                sources=[
                    _source(
                        "application",
                        "demoapp",
                        root="/var/log/demoapp",
                        required=False,
                    )
                ],
            )
        ]
    )
    selection = _selected(optional, "demoapp")
    facts = {
        "plugins": {
            "demoapp": {"units": {"system:demoapp.service": {"status": "active"}}}
        },
        "sources": {
            "demoapp/application": {
                "layouts": {
                    "default": _source_fact(
                        roots={
                            "/var/log/demoapp": _root_fact(
                                exists=False,
                                status="missing_root",
                                readable=False,
                                traversable=False,
                                device=None,
                            )
                        },
                        matches=[
                            _match(
                                "/var/log/demoapp/*.log",
                                [],
                                status="missing_root",
                                matched=0,
                            )
                        ],
                        status="missing_root",
                    )
                }
            }
        },
    }
    resolved = validate_log_layout(selection, facts)
    assert resolved.sources[0].status == "inactive"
    assert resolved.sources[0].renderable is False
    assert resolved.complete is False
    assert "optional root" in resolved.findings[0]

    required = _load(
        [
            _manifest(
                "demoapp",
                sources=[_source("application", "demoapp", root="/var/log/demoapp")],
            )
        ]
    )
    selection = _selected(required, "demoapp")
    facts["sources"]["demoapp/application"]["layouts"]["default"]["status"] = "missing_root"
    resolved = validate_log_layout(selection, facts)
    assert resolved.sources[0].status == "blocked"
    assert resolved.blocked is True
    assert resolved.renderable_sources == ()


def test_quiet_and_denied_and_truncated_outcomes():
    catalogue = _load([_demo_app()])
    selection = _selected(catalogue, "demoapp")
    quiet = validate_log_layout(
        selection,
        {
            "plugins": {
                "demoapp": {"units": {"system:demoapp.service": {"status": "active"}}}
            },
            "sources": {
                "demoapp/application": {
                    "layouts": {
                        "default": _source_fact(
                            roots={"/var/log/demoapp": _root_fact()},
                            matches=[
                                _match(
                                    "/var/log/demoapp/*.log",
                                    [],
                                    status="quiet",
                                    matched=0,
                                )
                            ],
                            status="quiet",
                        )
                    }
                }
            },
        },
    )
    assert quiet.sources[0].status == "quiet"
    assert quiet.sources[0].renderable is True
    assert quiet.sources[0].live_files == 0

    denied = validate_log_layout(
        selection,
        {
            "plugins": {
                "demoapp": {"units": {"system:demoapp.service": {"status": "active"}}}
            },
            "sources": {
                "demoapp/application": {
                    "layouts": {
                        "default": _source_fact(
                            roots={
                                "/var/log/demoapp": _root_fact(
                                    status="denied",
                                    readable=False,
                                    traversable=False,
                                )
                            },
                            matches=[
                                _match(
                                    "/var/log/demoapp/*.log",
                                    [],
                                    status="denied",
                                    matched=0,
                                )
                            ],
                            status="denied",
                        )
                    }
                }
            },
        },
    )
    assert denied.sources[0].status == "acl-needed"
    assert denied.acl_repair_needed is True
    assert denied.sources[0].renderable is False
    assert [source.key for source in denied.validation_candidate_sources] == [
        "demoapp/application"
    ]

    truncated = validate_log_layout(
        selection,
        {
            "plugins": {
                "demoapp": {"units": {"system:demoapp.service": {"status": "active"}}}
            },
            "sources": {
                "demoapp/application": {
                    "layouts": {
                        "default": _source_fact(
                            roots={"/var/log/demoapp": _root_fact()},
                            matches=[
                                _match(
                                    "/var/log/demoapp/*.log",
                                    [_file_entry("/var/log/demoapp/app.log")],
                                    matched=10,
                                    truncated=True,
                                )
                            ],
                            status="partial",
                            truncated=True,
                        )
                    }
                }
            },
        },
    )
    assert truncated.sources[0].status == "partial"
    assert truncated.partial is True
    assert truncated.blocked is True
    assert truncated.sources[0].renderable is False


def test_unmeasurable_access_is_distinct_from_failed_evidence():
    catalogue = _load([_demo_app()])
    selection = _selected(catalogue, "demoapp")

    missing_access = _root_fact(status="unavailable")
    missing_access.pop("access")
    unmeasurable = validate_log_layout(
        selection,
        {
            "plugins": {
                "demoapp": {"units": {"system:demoapp.service": {"status": "active"}}}
            },
            "sources": {
                "demoapp/application": {
                    "layouts": {
                        "default": _source_fact(
                            roots={"/var/log/demoapp": missing_access},
                            matches=[
                                _match(
                                    "/var/log/demoapp/*.log",
                                    [],
                                    status="unavailable",
                                    matched=0,
                                )
                            ],
                            status="unavailable",
                        )
                    }
                }
            },
        },
    )
    assert unmeasurable.sources[0].status == "unmeasurable"
    assert unmeasurable.unmeasurable is True
    assert unmeasurable.blocked is False
    assert unmeasurable.complete is False
    assert unmeasurable.sources[0].renderable is False
    assert unmeasurable.validation_candidate_sources == ()

    failed = validate_log_layout(
        selection,
        {
            "plugins": {
                "demoapp": {"units": {"system:demoapp.service": {"status": "active"}}}
            },
            "sources": {
                "demoapp/application": {
                    "layouts": {
                        "default": _source_fact(
                            roots={
                                "/var/log/demoapp": _root_fact(status="failed")
                            },
                            matches=[
                                _match(
                                    "/var/log/demoapp/*.log",
                                    [],
                                    status="failed",
                                    matched=0,
                                )
                            ],
                            status="failed",
                        )
                    }
                }
            },
        },
    )
    assert failed.sources[0].status == "blocked"
    assert failed.blocked is True
    assert failed.unmeasurable is False


def test_contradictory_source_evidence_is_rejected():
    catalogue = _load([_demo_app()])
    selection = _selected(catalogue, "demoapp")
    with pytest.raises(LogLayoutError, match="marked ok but absent"):
        validate_log_layout(
            selection,
            {
                "plugins": {
                    "demoapp": {
                        "units": {"system:demoapp.service": {"status": "active"}}
                    }
                },
                "sources": {
                    "demoapp/application": {
                        "layouts": {
                            "default": _source_fact(
                                roots={
                                    "/var/log/demoapp": _root_fact(
                                        exists=False,
                                        status="ok",
                                        readable=False,
                                        traversable=False,
                                        device=None,
                                    )
                                },
                                matches=[
                                    _match(
                                        "/var/log/demoapp/*.log",
                                        [],
                                        status="missing_root",
                                        matched=0,
                                    )
                                ],
                            )
                        }
                    }
                },
            },
        )

    with pytest.raises(LogLayoutError, match="omits matched files"):
        validate_log_layout(
            selection,
            {
                "plugins": {
                    "demoapp": {
                        "units": {"system:demoapp.service": {"status": "active"}}
                    }
                },
                "sources": {
                    "demoapp/application": {
                        "layouts": {
                            "default": _source_fact(
                                roots={"/var/log/demoapp": _root_fact()},
                                matches=[
                                    _match(
                                        "/var/log/demoapp/*.log",
                                        [],
                                        matched=3,
                                    )
                                ],
                            )
                        }
                    }
                },
            },
        )


def test_duplicate_file_identity_across_sources_is_rejected():
    catalogue = _load([_demo_app(["demodb"]), _demo_dependency()])
    selection = _selected(catalogue, "demoapp")
    facts = _owner_dep_facts()
    facts["sources"]["demoapp/demodb/database"]["layouts"]["default"] = (
        _simple_source_fact(
            "/var/log/demodb",
            "/var/log/demodb/*.log",
            [_file_entry("/var/log/demodb/db.log", device=10, inode=11)],
        )
    )
    with pytest.raises(LogLayoutError, match="file identity"):
        validate_log_layout(selection, facts)


def test_cross_owner_overlapping_sources_are_rejected():
    shared_app = _manifest(
        "shared-app",
        includes=["shared-dep"],
        sources=[_source("application", "shared-app", root="/var/log/shared")],
    )
    other_app = _manifest(
        "other-app",
        includes=["shared-dep"],
        sources=[_source("application", "other-app", root="/var/log/other-app")],
    )
    shared_dep = _manifest(
        "shared-dep",
        kind="dependency",
        sources=[
            _source(
                "database",
                "shared-dep",
                root="/var/log/shared/sub",
                paths=["/var/log/shared/sub/*.log"],
            )
        ],
    )
    catalogue = _load([shared_app, other_app, shared_dep])
    selection = select_log_plugins(
        catalogue,
        script_id=None,
        override=GuestLogOverride(
            mode="replace", plugins=["shared-app", "other-app"]
        ),
        auto=True,
    )
    facts = {
        "plugins": {
            "other-app": {
                "units": {"system:other-app.service": {"status": "active"}},
                "includes": {
                    "shared-dep": {
                        "units": {"system:shared-dep.service": {"status": "active"}}
                    }
                },
            },
            "shared-app": {
                "units": {"system:shared-app.service": {"status": "active"}},
                "includes": {
                    "shared-dep": {
                        "units": {"system:shared-dep.service": {"status": "active"}}
                    }
                },
            },
        }
    }
    with pytest.raises(LogLayoutError, match="ownership conflict"):
        validate_log_layout(selection, facts)


def test_disable_sources_and_empty_locations_remove_sources():
    catalogue = _load(
        [
            _manifest(
                "demoapp",
                includes=["demodb"],
                sources=[_source("application", "demoapp", root="/var/log/demoapp")],
            ),
            _demo_dependency(),
        ]
    )
    disabled = select_log_plugins(
        catalogue,
        script_id="demoapp",
        override=GuestLogOverride(
            mode="auto", disable_sources=["demoapp/application"]
        ),
        auto=True,
    )
    request = build_logging_request(disabled)
    assert "demoapp/application" not in request["sources"]
    assert "demoapp/demodb/database" in request["sources"]

    resolved = validate_log_layout(
        disabled,
        {
            "plugins": {
                "demoapp": {
                    "units": {"system:demoapp.service": {"status": "active"}},
                    "includes": {
                        "demodb": {
                            "units": {"system:demodb.service": {"status": "active"}}
                        }
                    },
                }
            },
            "sources": {
                "demoapp/demodb/database": {
                    "layouts": {
                        "default": _simple_source_fact(
                            "/var/log/demodb",
                            "/var/log/demodb/*.log",
                            [_file_entry("/var/log/demodb/db.log")],
                        )
                    }
                }
            },
        },
    )
    assert [source.key for source in resolved.sources] == [
        "demoapp/demodb/database"
    ]
    assert len(resolved.services) == 2

    emptied = select_log_plugins(
        catalogue,
        script_id="demoapp",
        override=GuestLogOverride(
            mode="auto", locations={"demoapp/application": []}
        ),
        auto=True,
    )
    assert "demoapp/application" not in build_logging_request(emptied)["sources"]


# --------------------------------------------------------------------------- #
# Disk policy
# --------------------------------------------------------------------------- #


def _generic_policy_snapshot(policy: DiskUsagePolicy) -> Dict[str, float]:
    return dict(policy.path_max_share_percent)


def test_disk_policy_generic_and_findings():
    policy, finding = resolve_disk_usage_policy(
        None, script_id="sonarr", override=None, auto=True
    )
    assert finding == GENERIC_DISK_UNAVAILABLE
    assert policy.default_max_share_percent == 20.0
    assert policy.min_directory_mb == 256
    assert _generic_policy_snapshot(policy) == {"/usr": 85.0}

    catalogue = _load([_demo_app()])
    policy, finding = resolve_disk_usage_policy(
        catalogue, script_id="debian", override=None, auto=True
    )
    assert finding == GENERIC_DISK_NO_PROFILE

    policy, finding = resolve_disk_usage_policy(
        catalogue, script_id="demoapp", override=None, auto=False
    )
    assert finding is None
    assert _generic_policy_snapshot(policy) == {"/usr": 85.0}

    policy, finding = resolve_disk_usage_policy(
        catalogue,
        script_id="demoapp",
        override=GuestLogOverride(mode="replace", plugins=["nope"]),
        auto=True,
    )
    assert finding == GENERIC_DISK_INVALID


def test_disk_policy_merges_roots_includes_and_longest_prefix():
    root_a = _manifest(
        "root-a",
        includes=["dep-a"],
        sources=[_source("application", "root-a", root="/var/log/root-a")],
        disk_usage={
            "default_max_share_percent": 40,
            "min_directory_mb": 128,
            "path_max_share_percent": {"/var/a": 30, "/usr": 50},
        },
    )
    root_b = _manifest(
        "root-b",
        sources=[_source("application", "root-b", root="/var/log/root-b")],
        disk_usage={
            "default_max_share_percent": 60,
            "min_directory_mb": 64,
            "path_max_share_percent": {"/var/a": 70, "/var/b": 20},
        },
    )
    dep_a = _manifest(
        "dep-a",
        kind="dependency",
        sources=[_source("database", "dep-a", root="/var/log/dep-a")],
        disk_usage={
            "default_max_share_percent": 1,
            "min_directory_mb": 0,
            "path_max_share_percent": {"/var/a": 90, "/opt/dep": 80},
        },
    )
    catalogue = _load([root_a, root_b, dep_a])
    selection = select_log_plugins(
        catalogue,
        script_id=None,
        override=GuestLogOverride(mode="replace", plugins=["root-a", "root-b"]),
        auto=True,
    )
    assert selection.owner_ids == ("root-a", "root-b")
    policy, finding = resolve_disk_usage_policy(
        catalogue,
        script_id=None,
        override=GuestLogOverride(mode="replace", plugins=["root-a", "root-b"]),
        auto=True,
    )
    assert finding is None
    assert policy.default_max_share_percent == 60.0
    assert policy.min_directory_mb == 128
    assert policy.path_max_share_percent == {
        "/usr": 50.0,
        "/var/a": 70.0,
        "/opt/dep": 80.0,
        "/var/b": 20.0,
    }
    assert policy.expected_share_percent("/var/a/log") == 70.0
    assert policy.expected_share_percent("/usr/lib") == 50.0
    assert policy.expected_share_percent("/opt/dep/cache") == 80.0
    assert policy.expected_share_percent("/var/c") is None
    extra = DiskUsagePolicy(
        default_max_share_percent=20,
        min_directory_mb=0,
        path_max_share_percent={
            "/usr": 85,
            "/root/.npm": 20,
            "/root/.local/share/pnpm/store": 20,
            "/var/log": 30,
        },
    )
    assert disk_usage_extra_paths(extra) == ("/root/.local/share/pnpm/store",)


def test_disk_policy_too_many_merged_prefixes_falls_back_to_generic():
    caps_a = {f"/pool/a/p{index}": 20 for index in range(17)}
    caps_b = {f"/pool/b/p{index}": 20 for index in range(17)}
    app_a = _manifest(
        "app-a",
        sources=[_source("application", "app-a", root="/var/log/app-a")],
        disk_usage={"path_max_share_percent": caps_a},
    )
    app_b = _manifest(
        "app-b",
        sources=[_source("application", "app-b", root="/var/log/app-b")],
        disk_usage={"path_max_share_percent": caps_b},
    )
    catalogue = _load([app_a, app_b])
    policy, finding = resolve_disk_usage_policy(
        catalogue,
        script_id=None,
        override=GuestLogOverride(mode="replace", plugins=["app-a", "app-b"]),
        auto=True,
    )
    assert finding == GENERIC_DISK_INVALID
    assert policy.path_max_share_percent == {"/usr": 85.0}
