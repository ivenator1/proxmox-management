"""Behavioral regressions for the shipped declarative log catalogue.

These tests use the real loader/selection/validation stack and synthetic
probe facts.  They never contact a guest, read a log body or mutate a host.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

import pytest

from proxmox_fleet.log_plugins import (
    LogLayoutError,
    LogSelectionError,
    build_logging_request,
    disk_usage_extra_paths,
    load_log_catalogue,
    resolve_disk_usage_policy,
    select_log_plugins,
    validate_log_layout,
)
from proxmox_fleet.models.log_plugins import GuestLogOverride

AUDITED_APPS = frozenset(
    {
        "nginxproxymanager",
        "proxmox-backup-server",
        "plex",
        "sonarr",
        "radarr",
        "prowlarr",
        "qbittorrent",
        "seerr",
        "tautulli",
        "apt-cacher-ng",
        "obsidian-livesync",
        "elementsynapse",
        "hermesagent",
        "technitiumdns",
        "proxmox-datacenter-manager",
        "firecrawl",
        "uptimekuma",
        "grafana",
        "homepage",
        "gitea-mirror",
        "dashy",
        "flaresolverr",
        "forgejo",
        "byparr",
        "searxng",
        "unifi-os-server",
        "fleet-manager",
    }
)
AUDITED_DEPENDENCIES = frozenset(
    {"openresty", "couchdb", "synapse-admin", "postgresql", "rabbitmq", "redis", "valkey"}
)
AUTO_APPS = AUDITED_APPS - {"fleet-manager"}


@pytest.fixture(scope="module")
def catalogue():
    return load_log_catalogue()


def _unit_keys(payload: Mapping[str, Any]) -> Iterable[str]:
    for unit in payload.get("journal_units", []):
        yield unit["key"]
    for layout in payload.get("layouts", []):
        for unit in layout.get("required_units", []):
            yield unit["key"]
        for unit in layout.get("running_units_any", []):
            yield unit["key"]


def _quiet_facts(
    request: Mapping[str, Any],
    *,
    statuses: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Build complete synthetic evidence for one logging request.

    Defaults are active units and quiet-but-accessible sources; callers pass
    explicit statuses to exercise optional/inactive/blocked semantics.
    """
    overrides = dict(statuses or {})
    plugins: Dict[str, Any] = {}
    for owner, payload in request["plugins"].items():
        units = {
            key: {"status": overrides.get(key, "active"), "instances": {}}
            for key in _unit_keys(payload)
        }
        includes = {
            include: {
                "units": {
                    key: {
                        "status": overrides.get(key, "active"),
                        "instances": {},
                    }
                    for key in _unit_keys(include_payload)
                }
            }
            for include, include_payload in payload.get("includes", {}).items()
        }
        plugins[owner] = {"units": units, "includes": includes}

    sources: Dict[str, Any] = {}
    for key, entry in request["sources"].items():
        layouts: Dict[str, Any] = {}
        for layout_id, descriptor in entry["layouts"].items():
            roots = {
                root: {
                    "exists": True,
                    "is_dir": True,
                    "is_symlink": False,
                    "device": 1,
                    "blocked_ancestor": False,
                    "access": {
                        "traversable": True,
                        "readable": True,
                        "default_acl": True,
                    },
                    "status": "quiet",
                    "error": None,
                }
                for root in descriptor["roots"]
            }
            exact_files = {
                path: {
                    "exists": False,
                    "is_symlink": False,
                    "device": None,
                    "inode": None,
                    "size": None,
                    "mtime_ns": None,
                    "same_device": True,
                    "parent_traversable": True,
                    "access": {"readable": True},
                    "status": "quiet",
                    "error": None,
                }
                for path in descriptor["exact_files"]
            }
            matches = [
                {
                    "pattern": pattern,
                    "matched": 0,
                    "skipped": 0,
                    "truncated": False,
                    "status": "quiet",
                    "files": [],
                    "error": None,
                }
                for pattern in descriptor["paths"]
            ]
            layouts[layout_id] = {
                "status": "quiet",
                "truncated": False,
                "roots": roots,
                "exact_files": exact_files,
                "matches": matches,
            }
        sources[key] = {"layouts": layouts}
    return {"plugins": plugins, "sources": sources}


def _selection_for(catalogue, plugin_id: str):
    if plugin_id == "fleet-manager":
        return select_log_plugins(
            catalogue,
            script_id=None,
            override=GuestLogOverride(mode="replace", plugins=[plugin_id]),
            auto=False,
        )
    return select_log_plugins(
        catalogue, script_id=plugin_id, override=None, auto=True
    )


def _request_for(catalogue, plugin_id: str) -> Dict[str, Any]:
    return build_logging_request(_selection_for(catalogue, plugin_id))


def _descriptor(request: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    layouts = request["sources"][key]["layouts"]
    assert list(layouts) == ["default"]
    return layouts["default"]


def test_shipped_catalogue_matches_the_audited_service_matrix(catalogue) -> None:
    assert len(catalogue.plugins) == len(AUDITED_APPS) + len(AUDITED_DEPENDENCIES)
    assert set(catalogue.plugins) == AUDITED_APPS | AUDITED_DEPENDENCIES
    assert {
        plugin_id
        for plugin_id, plugin in catalogue.plugins.items()
        if plugin.kind == "app"
    } == AUDITED_APPS
    assert {
        plugin_id
        for plugin_id, plugin in catalogue.plugins.items()
        if plugin.kind == "dependency"
    } == AUDITED_DEPENDENCIES

    # Every audited community-script identity is exact; dependencies and the
    # manager have no automatic alias at all.
    assert dict(catalogue.aliases) == {plugin_id: plugin_id for plugin_id in AUTO_APPS}
    for plugin_id in AUDITED_DEPENDENCIES:
        assert catalogue.auto_plugin_id(plugin_id) is None
    assert catalogue.auto_plugin_id("fleet-manager") is None


def test_every_audited_app_selects_and_validates(catalogue) -> None:
    for plugin_id in sorted(AUTO_APPS):
        selection = select_log_plugins(
            catalogue, script_id=plugin_id, override=None, auto=True
        )
        assert selection.status == "selected", plugin_id
        assert selection.owner_ids == (plugin_id,), plugin_id
        request = build_logging_request(selection)
        facts = _quiet_facts(request)
        resolved = validate_log_layout(selection, facts)
        plugin = catalogue.plugins[plugin_id]
        assert resolved.status == "selected", plugin_id
        assert resolved.complete, plugin_id
        assert {service.service_id for service in resolved.services} == {
            plugin_id,
            *plugin.includes,
        }, plugin_id
        assert {source.key for source in resolved.sources} == set(request["sources"])


def test_explicit_only_manager_ignores_debian_identity(catalogue) -> None:
    assert catalogue.auto_plugin_id("debian") is None
    assert catalogue.auto_plugin_id(None) is None
    assert catalogue.auto_plugin_id("gluetun") is None

    unsupported = select_log_plugins(
        catalogue, script_id="debian", override=None, auto=True
    )
    assert unsupported.status == "unsupported"
    assert unsupported.owner_ids == ()
    assert build_logging_request(unsupported) == {"plugins": {}, "sources": {}}

    selection = select_log_plugins(
        catalogue,
        script_id=None,
        override=GuestLogOverride(mode="replace", plugins=["fleet-manager"]),
        auto=False,
    )
    assert selection.status == "selected"
    request = build_logging_request(selection)
    plugin_payload = request["plugins"]["fleet-manager"]
    assert {
        unit["key"] for unit in plugin_payload["journal_units"]
    } == {
        "system:fleet-dashboard.service",
        "system:fleet-scan.service",
        "system:fleet-housekeeping.service",
    }
    descriptor = _descriptor(request, "fleet-manager/application")
    assert descriptor["exact_files"] == [
        "/var/log/fleet-update.log",
        "/var/log/ansible-fleet-update.log",
    ]
    assert descriptor["paths"] == ["/var/log/fleet-update/dashboard-runs/*.log"]
    assert descriptor["required"] is False
    assert all(
        not path.endswith((".json", ".sqlite", ".sqlite3", ".lock"))
        for path in descriptor["exact_files"] + descriptor["paths"]
    )

    # Normally inactive timer services are journal evidence only; the active
    # dashboard satisfies the running-state predicate.
    facts = _quiet_facts(
        request,
        statuses={
            "system:fleet-scan.service": "inactive",
            "system:fleet-housekeeping.service": "inactive",
        },
    )
    resolved = validate_log_layout(selection, facts)
    assert resolved.complete
    assert [source.status for source in resolved.sources] == ["quiet"]


def test_legacy_npm_and_pbs_sources_preserve_labels_order_and_quarantine(
    catalogue,
) -> None:
    npm_request = _request_for(catalogue, "nginxproxymanager")
    assert list(npm_request["sources"]) == ["nginxproxymanager/application"]
    npm = _descriptor(npm_request, "nginxproxymanager/application")
    assert npm["component"] == "fleet_npm"
    assert npm["app"] == "nginxproxymanager"
    assert npm["log_kind"] == "application"
    assert npm["allowed_roots"] == ["/data/logs"]
    assert npm["paths"] == ["/data/logs/**/*.log"]
    assert npm["exclude_paths"] == [
        "/data/logs/**/.fleet-housekeeping-quarantine/**"
    ]
    assert list(npm_request["plugins"]["nginxproxymanager"]["includes"]) == [
        "openresty"
    ]
    openresty = npm_request["plugins"]["nginxproxymanager"]["includes"]["openresty"]
    assert openresty["layouts"][0]["required_units"] == [
        {
            "name": "openresty.service",
            "scope": "system",
            "user": None,
            "key": "system:openresty.service",
        }
    ]
    assert validate_log_layout(
        _selection_for(catalogue, "nginxproxymanager"),
        _quiet_facts(npm_request),
    ).complete

    pbs_request = _request_for(catalogue, "proxmox-backup-server")
    assert list(pbs_request["sources"]) == [
        "proxmox-backup-server/tasks",
        "proxmox-backup-server/api",
    ]
    tasks = _descriptor(pbs_request, "proxmox-backup-server/tasks")
    assert tasks["component"] == "fleet_pbs_tasks"
    assert tasks["app"] == "proxmox-backup"
    assert tasks["log_kind"] == "task"
    assert tasks["paths"] == [
        "/var/log/proxmox-backup/tasks/[0-9A-F][0-9A-F]/UPID:*"
    ]
    assert tasks["exclude_paths"] == [
        "/var/log/proxmox-backup/tasks/**/.fleet-housekeeping-quarantine/**"
    ]
    api = _descriptor(pbs_request, "proxmox-backup-server/api")
    assert api["component"] == "fleet_pbs_api"
    assert api["app"] == "proxmox-backup"
    assert api["log_kind"] == "api"
    assert api["exact_files"] == [
        "/var/log/proxmox-backup/api/access.log",
        "/var/log/proxmox-backup/api/auth.log",
    ]
    assert api["paths"] == []
    assert api["exclude_paths"] == []
    assert validate_log_layout(
        _selection_for(catalogue, "proxmox-backup-server"),
        _quiet_facts(pbs_request),
    ).complete


def test_firecrawl_selects_and_gates_on_verified_dependency_evidence(
    catalogue,
) -> None:
    selection = _selection_for(catalogue, "firecrawl")
    request = build_logging_request(selection)
    assert list(request["sources"]) == [
        "firecrawl/postgresql/database",
        "firecrawl/rabbitmq/queue",
        "firecrawl/rabbitmq/queue-startup",
        "firecrawl/rabbitmq/queue-error",
        "firecrawl/redis/cache-service",
    ]
    plugin_payload = request["plugins"]["firecrawl"]
    assert list(plugin_payload["includes"]) == [
        "postgresql",
        "rabbitmq",
        "redis",
    ]
    assert plugin_payload["layouts"][0]["required_units"] == [
        {
            "name": "firecrawl.service",
            "scope": "system",
            "user": None,
            "key": "system:firecrawl.service",
        },
        {
            "name": "firecrawl-playwright.service",
            "scope": "system",
            "user": None,
            "key": "system:firecrawl-playwright.service",
        },
    ]
    for include, unit_name in (
        ("postgresql", "postgresql@17-main.service"),
        ("rabbitmq", "rabbitmq-server.service"),
        ("redis", "redis-server.service"),
    ):
        assert plugin_payload["includes"][include]["layouts"][0][
            "required_units"
        ] == [
            {
                "name": unit_name,
                "scope": "system",
                "user": None,
                "key": f"system:{unit_name}",
            }
        ]

    expected_kinds = {
        "firecrawl/postgresql/database": (
            "database",
            ["postgresql-17-main.log"],
        ),
        "firecrawl/rabbitmq/queue": ("queue", ["rabbit@*.log"]),
        "firecrawl/rabbitmq/queue-startup": (
            "queue-startup",
            ["rabbitmq-server.log"],
        ),
        "firecrawl/rabbitmq/queue-error": (
            "queue-error",
            ["rabbitmq-server.error.log"],
        ),
        "firecrawl/redis/cache-service": (
            "cache-service",
            ["redis-server.log"],
        ),
    }
    components = set()
    for key, (log_kind, basenames) in expected_kinds.items():
        descriptor = _descriptor(request, key)
        assert descriptor["app"] == "firecrawl"
        assert descriptor["log_kind"] == log_kind
        assert descriptor["component"].startswith("fleet_")
        assert descriptor["component"] not in {
            "fleet_npm",
            "fleet_pbs_tasks",
            "fleet_pbs_api",
        }
        assert descriptor["component"] not in components
        components.add(descriptor["component"])
        patterns = descriptor["paths"] + descriptor["exact_files"]
        assert [pattern.rsplit("/", 1)[-1] for pattern in patterns] == basenames

    facts = _quiet_facts(request)
    resolved = validate_log_layout(selection, facts)
    assert resolved.complete
    assert {source.key for source in resolved.sources} == set(request["sources"])
    assert {service.service_id for service in resolved.services} == {
        "firecrawl",
        "postgresql",
        "rabbitmq",
        "redis",
    }

    # A dependency's required unit is not optional just because it has a
    # source: missing child evidence blocks the whole bundle.
    blocked = _quiet_facts(
        request, statuses={"system:redis-server.service": "inactive"}
    )
    with pytest.raises(LogLayoutError, match="no layout matched for 'firecrawl/redis'"):
        validate_log_layout(selection, blocked)


def test_journal_only_service_keeps_source_less_active_evidence(catalogue) -> None:
    selection = _selection_for(catalogue, "dashy")
    request = build_logging_request(selection)
    assert request["plugins"]["dashy"]["journal_units"] == [
        {
            "name": "dashy.service",
            "scope": "system",
            "user": None,
            "key": "system:dashy.service",
        }
    ]
    assert request["sources"] == {}
    facts = _quiet_facts(request)
    resolved = validate_log_layout(selection, facts)
    assert resolved.complete
    assert len(resolved.services) == 1
    service = resolved.services[0]
    assert service.service_id == "dashy"
    assert service.has_primary_predicate is True
    assert [(unit.key, unit.status) for unit in service.units] == [
        ("system:dashy.service", "active")
    ]
    assert resolved.sources == ()

    inactive = _quiet_facts(
        request, statuses={"system:dashy.service": "inactive"}
    )
    with pytest.raises(LogLayoutError, match="no layout matched for 'dashy'"):
        validate_log_layout(selection, inactive)


def test_hermes_supports_system_and_user_scope_with_optional_gateway(
    catalogue,
) -> None:
    plugin = catalogue.plugins["hermesagent"]
    layout = plugin.layouts[0]
    assert layout.service_user == "hermes"
    assert layout.required_units == []
    predicates = {(unit.scope, unit.user, unit.name) for unit in layout.running_units_any}
    assert predicates == {
        ("system", None, "hermes-dashboard.service"),
        ("system", None, "hermes-gateway.service"),
        ("user", "hermes", "hermes-dashboard.service"),
        ("user", "hermes", "hermes-gateway.service"),
    }
    assert {
        (unit.scope, unit.user, unit.name) for unit in plugin.journal_units
    } == predicates

    selection = _selection_for(catalogue, "hermesagent")
    request = build_logging_request(selection)
    user_keys = {
        "user:hermes:hermes-dashboard.service",
        "user:hermes:hermes-gateway.service",
    }
    dashboard_only = _quiet_facts(
        request,
        statuses={
            "system:hermes-gateway.service": "inactive",
            **{key: "unavailable" for key in user_keys},
        },
    )
    resolved = validate_log_layout(selection, dashboard_only)
    assert resolved.complete
    statuses = {source.key: source.status for source in resolved.sources}
    assert statuses == {
        "hermesagent/application": "quiet",
        "hermesagent/error": "quiet",
    }

    gateway_only = _quiet_facts(
        request,
        statuses={
            "system:hermes-dashboard.service": "inactive",
            **{key: "unavailable" for key in user_keys},
        },
    )
    assert validate_log_layout(selection, gateway_only).complete

    all_inactive = _quiet_facts(
        request,
        statuses={
            "system:hermes-dashboard.service": "inactive",
            "system:hermes-gateway.service": "inactive",
            **{key: "inactive" for key in user_keys},
        },
    )
    with pytest.raises(LogLayoutError, match="no layout matched for 'hermesagent'"):
        validate_log_layout(selection, all_inactive)


def test_hermes_optional_exact_file_stays_inactive_instead_of_blocking(
    catalogue,
) -> None:
    selection = _selection_for(catalogue, "hermesagent")
    request = build_logging_request(selection)
    facts = _quiet_facts(request)
    for layout in facts["sources"]["hermesagent/error"]["layouts"].values():
        layout["status"] = "missing_root"
        for entry in layout["exact_files"].values():
            entry["parent_traversable"] = False
    resolved = validate_log_layout(selection, facts)
    statuses = {source.key: source.status for source in resolved.sources}
    assert statuses["hermesagent/error"] == "inactive"
    assert statuses["hermesagent/application"] == "quiet"
    assert resolved.blocked is False


def test_exact_file_sources_do_not_expose_application_data(catalogue) -> None:
    kuma = _request_for(catalogue, "uptimekuma")
    descriptor = _descriptor(kuma, "uptimekuma/error")
    assert descriptor["allowed_roots"] == []
    assert descriptor["paths"] == []
    assert descriptor["exact_files"] == ["/opt/uptime-kuma/data/error.log"]

    qbittorrent = _descriptor(
        _request_for(catalogue, "qbittorrent"), "qbittorrent/application"
    )
    assert qbittorrent["allowed_roots"] == [
        "/root/.local/share/qBittorrent/logs"
    ]
    assert all(
        ".config/qBittorrent" not in path
        for path in qbittorrent["paths"] + qbittorrent["exact_files"]
    )

    plex = _descriptor(_request_for(catalogue, "plex"), "plex/application")
    assert plex["allowed_roots"] == [
        "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Logs"
    ]
    assert plex["paths"] == [
        "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Logs/*.log"
    ]
    assert plex["exclude_paths"] == [
        "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Logs/*.[0-9]*.log",
        "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Logs/**/.fleet-housekeeping-quarantine/**",
    ]

    hermes = _request_for(catalogue, "hermesagent")
    for key in ("hermesagent/application", "hermesagent/error"):
        descriptor = _descriptor(hermes, key)
        assert descriptor["allowed_roots"] == []
        assert descriptor["paths"] == []
        assert all(
            path.startswith("/home/hermes/.hermes/logs/")
            and "*" not in path
            for path in descriptor["exact_files"]
        )
        assert all("/root/.hermes" not in path for path in descriptor["exact_files"])

    unifi = _descriptor(
        _request_for(catalogue, "unifi-os-server"), "unifi-os-server/application"
    )
    assert unifi["allowed_roots"] == []
    assert unifi["paths"] == []
    assert unifi["exact_files"] == [
        "/var/lib/uosserver/logs/uosserver.log",
        "/var/lib/uosserver/logs/uosserver-service.log",
        "/var/lib/uosserver/logs/uos-discovery-client.log",
        "/var/lib/uosserver/logs/updater-service.log",
    ]
    assert all(
        "podman" not in path and "docker" not in path
        for path in unifi["exact_files"]
    )

    tautulli = _descriptor(
        _request_for(catalogue, "tautulli"), "tautulli/application"
    )
    assert tautulli["allowed_roots"] == []
    assert tautulli["paths"] == []
    assert tautulli["exact_files"] == [
        "/opt/Tautulli/logs/tautulli.log",
        "/opt/Tautulli/logs/tautulli_api.log",
        "/opt/Tautulli/logs/plexapi.log",
        "/opt/Tautulli/logs/plex_websocket.log",
    ]

    firecrawl = _request_for(catalogue, "firecrawl")
    for key in (
        "firecrawl/postgresql/database",
        "firecrawl/redis/cache-service",
    ):
        descriptor = _descriptor(firecrawl, key)
        assert descriptor["allowed_roots"] == []
        assert descriptor["paths"] == []
        assert descriptor["exact_files"]


def test_homepage_uses_canonical_source_and_audited_alternate_location(
    catalogue,
) -> None:
    selection = _selection_for(catalogue, "homepage")
    request = build_logging_request(selection)
    default = _descriptor(request, "homepage/application")
    assert default["allowed_roots"] == ["/opt/homepage/config/logs"]
    assert default["alternate_roots"] == [
        "/opt/homepage/.next/standalone/config/logs"
    ]
    assert default["paths"] == ["/opt/homepage/config/logs/homepage.log"]
    assert default["roots"] == ["/opt/homepage/config/logs"]

    alternate_path = "/opt/homepage/.next/standalone/config/logs/homepage.log"
    alternate_selection = select_log_plugins(
        catalogue,
        script_id=None,
        override=GuestLogOverride(
            mode="replace",
            plugins=["homepage"],
            locations={"homepage/application": [alternate_path]},
        ),
        auto=False,
    )
    alternate_request = build_logging_request(alternate_selection)
    alternate = _descriptor(alternate_request, "homepage/application")
    assert alternate["paths"] == [alternate_path]
    assert alternate["roots"] == [
        "/opt/homepage/.next/standalone/config/logs"
    ]
    assert alternate["allowed_roots"] == ["/opt/homepage/config/logs"]

    with pytest.raises(LogSelectionError, match="outside the source's audited roots"):
        select_log_plugins(
            catalogue,
            script_id=None,
            override=GuestLogOverride(
                mode="replace",
                plugins=["homepage"],
                locations={"homepage/application": ["/var/log/syslog"]},
            ),
            auto=False,
        )


def test_shipped_disk_policies_name_deep_package_caches(catalogue) -> None:
    homepage, homepage_finding = resolve_disk_usage_policy(
        catalogue, script_id="homepage", override=None, auto=True
    )
    assert homepage_finding is None
    assert homepage.expected_share_percent("/root/.npm/_cacache") == 20.0
    assert (
        homepage.expected_share_percent(
            "/root/.local/share/pnpm/store/v3/files"
        )
        == 20.0
    )
    assert disk_usage_extra_paths(homepage) == (
        "/root/.local/share/pnpm/store",
    )

    gitea, gitea_finding = resolve_disk_usage_policy(
        catalogue, script_id="gitea-mirror", override=None, auto=True
    )
    assert gitea_finding is None
    assert gitea.expected_share_percent("/root/.bun/install/cache/pkg") == 20.0
    assert disk_usage_extra_paths(gitea) == ("/root/.bun/install/cache",)


def test_standalone_dependency_selection_keeps_its_own_labels(catalogue) -> None:
    for dependency_id in sorted(AUDITED_DEPENDENCIES):
        assert catalogue.auto_plugin_id(dependency_id) is None
        selection = select_log_plugins(
            catalogue,
            script_id=None,
            override=GuestLogOverride(
                mode="replace", plugins=[dependency_id]
            ),
            auto=False,
        )
        assert selection.status == "selected"
        assert selection.owner_ids == (dependency_id,)
        request = build_logging_request(selection)
        assert dependency_id in request["plugins"]
        resolved = validate_log_layout(selection, _quiet_facts(request))
        assert resolved.complete
        assert all(
            service.service_id == dependency_id
            for service in resolved.services
        )

    couchdb = _descriptor(
        _request_for_dependency(catalogue, "couchdb"), "couchdb/database"
    )
    assert couchdb["app"] == "couchdb"
    assert couchdb["log_kind"] == "database"
    assert couchdb["component"].startswith("fleet_")


def _request_for_dependency(catalogue, dependency_id: str) -> Dict[str, Any]:
    selection = select_log_plugins(
        catalogue,
        script_id=None,
        override=GuestLogOverride(mode="replace", plugins=[dependency_id]),
        auto=False,
    )
    return build_logging_request(selection)
