"""Application-log catalogue loading, selection and resolved evidence.

The module is intentionally pure: loading YAML, selecting plugin roots from
already-collected identity/override data and validating probe facts.  It never
runs a command, touches the network or mutates a guest.

Public API
----------

``load_log_catalogue(local_dir)``
    Load packaged manifests plus an optional additive local directory.  Duplicate
    IDs/aliases, cycles, illegal include edges and unsafe paths fail here.

``community_script_identity(facts)``
    Read the already-collected ``pull_rc``/``script_stdout`` introspection facts.
    This is the only identity source; service/hostname/path guessing is absent.

``select_log_plugins(catalogue, script_id=, override=, auto=)``
    Purely select owner roots.  An unresolved identity produces
    ``LogSelection.status == "unsupported"``; an explicit disabled/empty choice
    produces ``"empty"``; invalid overrides/unknown catalogue IDs raise
    :class:`LogSelectionError`.

``build_logging_request(selection)``
    Build the bounded JSON-safe request handed to step 2's read-only helper.
    Sources are keyed by the full ``owner/source`` or
    ``owner/dependency/source`` key and contain one descriptor per candidate
    ``layout_id``; the chosen layout alone later supplies the renderer source.

``validate_log_layout(selection, facts)``
    Consume the helper response.  Unit facts choose exactly one layout per
    owner/dependency; source facts are then validated for the chosen layout.
    Layout/unit/evidence problems raise :class:`LogLayoutError`.  Source-level
    outcomes are explicit (``active`` / ``quiet`` / ``unmeasurable`` /
    ``inactive`` / ``acl-needed`` / ``partial`` / ``blocked``); only
    active/quiet sources are renderable for final deployment.  Descriptors are
    retained for every outcome, including safe ``acl-needed`` syntax-candidate
    sources and prerequisite-missing ``unmeasurable`` sources.

``resolve_disk_usage_policy(...)``
    Resolve generic plus root/include disk-ratio metadata independently from
    Alloy source deployment or Loki state.

Probe request shape
-------------------

``{"plugins": {owner_id: {"kind": ..., "journal_units": [unit...],
"layouts": [{"id", "required_units", "running_units_any", "source_ids"}],
"includes": {dependency_id: {...}}}},
"sources": {full_key: {"owner_id", "service_id", "source_id", "layouts":
{layout_id: source_descriptor}}}}``.

A unit is ``{"name","scope","user","key"}``.  A source descriptor contains
``kind``, ``app``, ``log_kind``, ``component``, ``required``, ``multiline``,
``allowed_roots``, ``alternate_roots``, ``paths``, ``exact_files``,
``exclude_paths``, ``roots`` and ``service_user``.  All values are data; no
command, HCL, URL or deletion directive exists in the request.

Probe response / validation facts shape
---------------------------------------

``{"plugins": {owner_id: {"units": {unit_key: {"status": "active" | "inactive" |
"missing" | "unavailable", "instances": {concrete_unit: status}}}, "includes":
{dependency_id: {"units": {...}}}}}`` plus ``"sources"``::

    sources[full_key]["layouts"][layout_id] = {
        "status": "ok" | "quiet" | "missing_root" | "denied" |
                  "unavailable" | "failed" | "partial",
        "truncated": bool,                 # source-level completeness marker
        "roots": {root_path: {
            "exists": bool, "is_dir": bool, "is_symlink": bool,
            "device": int | None, "blocked_ancestor": bool,
            "access": {"traversable": bool, "readable": bool,
                       "default_acl": bool},
            "status": <same status vocabulary>, "error": str | None}},
        "exact_files": {path: {
            "exists": bool, "is_symlink": bool, "device": int | None,
            "inode": int | None, "size": int | None, "mtime_ns": int | None,
            "same_device": bool, "parent_traversable": bool,
            "access": {"readable": bool},
            "status": <same status vocabulary>, "error": str | None}},
        "matches": [{
            "pattern": declared_path, "matched": int, "skipped": int,
            "truncated": bool,
            "status": "ok" | "quiet" | "missing_root" | "denied" |
                      "unavailable" | "failed" | "partial",
            "error": str | None,
            "files": [{"path": str, "device": int, "inode": int,
                       "size": int | None, "mtime_ns": int | None}]}],
    }

``unit_key`` is ``system:<unit>`` or ``user:<user>:<unit>``.  ``roots`` must
cover exactly the effective directory roots of that layout descriptor and
``matches`` exactly its declared path patterns; exact-file evidence covers the
declared exact files.  ``matched`` is the number of post-exclusion files this
entry accounts for and ``truncated`` must be explicit whenever that list is not
complete.  Missing requested evidence is an error, never a verified source.

Status semantics are deliberately distinct: ``denied`` becomes a repairable
``acl-needed`` finding; ``quiet`` means an existing accessible root with zero
matches; ``unavailable`` means access could not be measured because a collector
prerequisite (for example the Alloy user/runtime) is missing, so an explicit
allow-install flow may install and re-probe without inventing ``access=true``;
``failed`` means unsafe/failed path or enumeration evidence and is blocked;
``partial``/``truncated`` is never quiet or verified.  Human ``error`` prose is
never parsed for policy.  ``ResolvedLogSelection.validation_candidate_sources``
retains safe ``acl-needed`` descriptors so a syntax-validation config can be
built before ACL repair, while final deployment still requires active/quiet
access proof.  Device/inode identity conflicts across sources are rejected from
the complete returned file list only.
"""
from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    cast,
)

import yaml
from pydantic import ValidationError

from proxmox_fleet.housekeeping_io import QUARANTINE_DIRNAME
from proxmox_fleet.lxc_parse import script_name_from_update
from proxmox_fleet.models.log_plugins import (
    DiskUsagePolicy,
    GuestLogOverride,
    GuestLogOverrideKey,
    LogLayout,
    LogPlugin,
    LogSource,
    MultilineOptions,
    UnitEvidence,
    literal_prefix,
    pattern_beneath,
    path_depth,
    patterns_overlap,
)

BUILTIN_CATALOGUE_PACKAGE = "proxmox_fleet.log_catalogue"
_MANIFEST_SUFFIXES = frozenset({".yml", ".yaml"})

#: Generic diagnostic policy used when no service profile applies.
GENERIC_DISK_DEFAULT_SHARE_PERCENT = 20.0
GENERIC_DISK_MIN_DIRECTORY_MB = 256
GENERIC_DISK_PATH_CAP_PERCENT = 85.0
GENERIC_DISK_PATH_CAPS: Mapping[str, float] = MappingProxyType({"/usr": 85.0})

GENERIC_DISK_NO_PROFILE = "no recognized service profile; generic disk thresholds"
GENERIC_DISK_UNAVAILABLE = "service profiles unavailable; generic disk thresholds"
GENERIC_DISK_INVALID = "service profile selection invalid; generic thresholds"

_UNIT_STATUSES = frozenset({"active", "inactive", "missing", "unavailable"})
UnitStatus = Literal["active", "inactive", "missing", "unavailable"]
_MAX_FINDING_CHARS = 160


class LogPluginError(ValueError):
    """Base class for catalogue/selection/layout errors."""


class LogCatalogueError(LogPluginError):
    """A manifest set could not be loaded or is internally inconsistent."""


class LogSelectionError(LogPluginError):
    """An override request is unknown, ambiguous or incompatible."""


class LogLayoutError(LogPluginError):
    """Probe facts do not establish exactly one valid layout/source state."""


class GuestOverrideError(LogSelectionError):
    """A guest override key is malformed or ambiguous in full discovery."""


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LogCatalogue:
    """Validated immutable manifest set plus exact community-script aliases."""

    plugins: Mapping[str, LogPlugin]
    aliases: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugins", MappingProxyType(dict(self.plugins)))
        object.__setattr__(self, "aliases", MappingProxyType(dict(self.aliases)))

    def plugin(self, service_id: str) -> LogPlugin:
        try:
            return self.plugins[service_id]
        except KeyError as exc:
            raise LogSelectionError(f"unknown service id {service_id!r}") from exc

    def auto_plugin_id(self, script_id: Optional[str]) -> Optional[str]:
        """Exact community-script identity lookup; never a hostname/service guess."""
        if not script_id:
            return None
        return self.aliases.get(script_id)

    def known_service_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self.plugins))


# --------------------------------------------------------------------------- #
# Selection / resolved models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LogSelection:
    """Offline root selection plus immutable override/evidence context."""

    status: Literal["selected", "empty", "unsupported"]
    owner_ids: Tuple[str, ...]
    catalogue: LogCatalogue = field(repr=False)
    mode: Literal["auto", "replace", "extend", "disabled"]
    disable_sources: FrozenSet[str] = frozenset()
    locations: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    layouts: Mapping[str, str] = field(default_factory=dict)
    evidence: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "locations", MappingProxyType(dict(self.locations)))
        object.__setattr__(self, "layouts", MappingProxyType(dict(self.layouts)))
        object.__setattr__(self, "disable_sources", frozenset(self.disable_sources))


@dataclass(frozen=True)
class ResolvedUnit:
    """One unit's scoped evidence and the predicate roles it served."""

    name: str
    scope: Literal["system", "user"]
    user: Optional[str]
    status: UnitStatus
    instances: Tuple[Tuple[str, str], ...] = ()
    roles: Tuple[str, ...] = ()

    @property
    def key(self) -> str:
        if self.scope == "user":
            return f"user:{self.user}:{self.name}"
        return f"system:{self.name}"


@dataclass(frozen=True)
class ResolvedLogService:
    """A selected owner/dependency occurrence with its chosen layout evidence."""

    owner_id: str
    service_id: str
    kind: Literal["app", "dependency"]
    layout_id: str
    units: Tuple[ResolvedUnit, ...]
    journal_units: Tuple[ResolvedUnit, ...]
    service_user: Optional[str]
    has_primary_predicate: bool

    @property
    def occurrence_key(self) -> str:
        return self.service_id if self.owner_id == self.service_id else f"{self.owner_id}/{self.service_id}"


@dataclass(frozen=True)
class ResolvedLogSource:
    """A source resolved for exactly one chosen layout.

    ``status`` is deliberately explicit.  Only ``active`` sources may be
    rendered; ``acl-needed`` must be repaired and re-probed, ``inactive`` is an
    optional source whose audited root/file is currently absent and ``blocked``
    is a required source or probe failure.  ``delivery`` stays unverified:
    configuration/read evidence never proves Loki arrival.
    """

    owner_id: str
    service_id: str
    source_id: str
    key: str
    layout_id: str
    kind: Literal["app", "dependency"]
    app: str
    log_kind: str
    component: str
    required: bool
    multiline: Optional[MultilineOptions]
    paths: Tuple[str, ...]
    exclude_paths: Tuple[str, ...]
    roots: Tuple[str, ...]
    exact_files: Tuple[str, ...]
    service_user: Optional[str]
    status: Literal[
        "active", "quiet", "unmeasurable", "inactive", "acl-needed", "partial", "blocked"
    ]
    findings: Tuple[str, ...] = ()
    live_files: int = 0
    delivery: Literal["unverified"] = "unverified"

    @property
    def renderable(self) -> bool:
        """Only fully verified/quiet configurations may reach final deployment."""
        return self.status in {"active", "quiet"}

    @property
    def quiet(self) -> bool:
        return self.status == "quiet" or (
            self.status == "active" and self.live_files == 0
        )


@dataclass(frozen=True)
class ResolvedLogSelection:
    """Result of validating one selection against probe unit/source facts."""

    status: Literal["selected", "empty", "unsupported"]
    owners: Tuple[str, ...]
    services: Tuple[ResolvedLogService, ...]
    sources: Tuple[ResolvedLogSource, ...]
    evidence: Tuple[str, ...] = ()
    findings: Tuple[str, ...] = ()

    @property
    def renderable_sources(self) -> Tuple[ResolvedLogSource, ...]:
        return tuple(source for source in self.sources if source.renderable)

    @property
    def validation_candidate_sources(self) -> Tuple[ResolvedLogSource, ...]:
        """Safe path/unit descriptors usable for a pre-ACL syntax candidate.

        ``acl-needed`` sources are included: their layout/path metadata is
        safe and validated, but final deployment still requires a successful
        scoped ACL repair/re-probe to reach ``active``/``quiet``.
        """
        return tuple(
            source
            for source in self.sources
            if source.status in {"active", "quiet", "acl-needed"}
        )

    @property
    def acl_repair_needed(self) -> bool:
        return any(source.status == "acl-needed" for source in self.sources)

    @property
    def unmeasurable(self) -> bool:
        """True when collector/user prerequisites prevent an access measurement.

        Probe ``status="unavailable"`` is prerequisite-missing/unmeasurable, not
        an ACL denial and not unsafe path evidence.  An explicit allow-install
        path may install Alloy, then must re-probe instead of inventing access.
        """
        return any(source.status == "unmeasurable" for source in self.sources)

    @property
    def partial(self) -> bool:
        return any(source.status == "partial" for source in self.sources)

    @property
    def blocked(self) -> bool:
        """True when a source is blocked or only partially evidenced."""
        return any(
            source.status in {"blocked", "partial"} for source in self.sources
        )

    @property
    def complete(self) -> bool:
        """True when the selection resolved and every selected source is renderable."""
        return self.status == "selected" and all(
            source.renderable for source in self.sources
        )


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _iter_manifests(local_dir: Optional[Path]) -> Iterable[Tuple[str, str]]:
    package = resources.files(BUILTIN_CATALOGUE_PACKAGE)
    entries = sorted(package.iterdir(), key=lambda entry: entry.name)
    for entry in entries:
        if not entry.is_file() or Path(entry.name).suffix not in _MANIFEST_SUFFIXES:
            continue
        yield entry.name, entry.read_text(encoding="utf-8")
    if local_dir is None:
        return
    directory = Path(local_dir)
    if not directory.exists() or not directory.is_dir():
        raise LogCatalogueError(f"log plugin directory {str(directory)!r} is not a directory")
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.suffix not in _MANIFEST_SUFFIXES:
            continue
        if entry.is_symlink():
            raise LogCatalogueError(f"log plugin file {entry.name!r} must not be a symlink")
        if not entry.is_file():
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError as exc:
            raise LogCatalogueError(f"cannot read log plugin file {entry.name!r}: {exc}") from exc
        yield entry.name, text


def _parse_manifest(name: str, text: str) -> LogPlugin:
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise LogCatalogueError(f"{name}: invalid YAML: {exc}") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise LogCatalogueError(f"{name}: must contain exactly one YAML mapping")
    try:
        plugin = LogPlugin.model_validate(documents[0])
    except ValidationError as exc:
        raise LogCatalogueError(f"{name}: {exc}") from exc
    if Path(name).stem != plugin.id:
        raise LogCatalogueError(
            f"{name}: filename stem must equal plugin id {plugin.id!r}"
        )
    return plugin


def _find_cycle(graph: Mapping[str, Sequence[str]]) -> Optional[Tuple[str, ...]]:
    """Return one include cycle (including self-edge) with its precise path."""
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(node: str) -> Optional[Tuple[str, ...]]:
        state[node] = 1
        stack.append(node)
        for child in graph.get(node, ()):
            if child not in graph:
                continue
            if state.get(child) == 1:
                start = stack.index(child)
                return tuple(stack[start:] + [child])
            if state.get(child, 0) == 0:
                found = visit(child)
                if found is not None:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for service_id in sorted(graph):
        if state.get(service_id, 0) == 0:
            found = visit(service_id)
            if found is not None:
                return found
    return None


def _validate_catalogue(plugins: Mapping[str, LogPlugin]) -> LogCatalogue:
    graph: Dict[str, Sequence[str]] = {
        plugin_id: tuple(plugin.includes) for plugin_id, plugin in plugins.items()
    }
    cycle = _find_cycle(graph)
    if cycle is not None:
        raise LogCatalogueError("include cycle: " + " -> ".join(cycle))

    for plugin_id, plugin in plugins.items():
        for include in plugin.includes:
            target = plugins.get(include)
            if target is None:
                raise LogCatalogueError(
                    f"{plugin_id!r} includes unknown service {include!r}"
                )
            if plugin.kind != "app":
                raise LogCatalogueError(
                    f"dependency {plugin_id!r} must not declare includes"
                )
            if target.kind != "dependency":
                raise LogCatalogueError(
                    f"app {plugin_id!r} must include dependencies, not app {include!r}"
                )

    aliases: Dict[str, str] = {}
    for plugin_id, plugin in plugins.items():
        for script_id in plugin.community_script_ids:
            existing = aliases.get(script_id)
            if existing is not None and existing != plugin_id:
                raise LogCatalogueError(
                    f"community-script identity {script_id!r} is claimed by "
                    f"{existing!r} and {plugin_id!r}"
                )
            aliases[script_id] = plugin_id
    for script_id, plugin_id in aliases.items():
        other = plugins.get(script_id)
        if other is not None and script_id != plugin_id:
            raise LogCatalogueError(
                f"community-script identity {script_id!r} conflicts with service id "
                f"{other.id!r}"
            )

    fixed_components: Dict[str, str] = {}
    for plugin_id, plugin in plugins.items():
        for layout in plugin.layouts:
            for source in layout.sources:
                if source.component is None:
                    continue
                previous = fixed_components.get(source.component)
                if previous is not None and previous != plugin_id:
                    raise LogCatalogueError(
                        f"component {source.component!r} is used by both "
                        f"{previous!r} and {plugin_id!r}"
                    )
                fixed_components[source.component] = plugin_id

    return LogCatalogue(plugins=dict(plugins), aliases=aliases)


def load_log_catalogue(local_dir: Optional[Path] = None) -> LogCatalogue:
    """Load built-in manifests plus optional additive local manifests."""
    plugins: Dict[str, LogPlugin] = {}
    for name, text in _iter_manifests(local_dir):
        plugin = _parse_manifest(name, text)
        if plugin.id in plugins:
            raise LogCatalogueError(f"duplicate plugin id {plugin.id!r}")
        plugins[plugin.id] = plugin
    return _validate_catalogue(plugins)


# --------------------------------------------------------------------------- #
# Identity and guest override lookup
# --------------------------------------------------------------------------- #


def community_script_identity(facts: Mapping[str, Any]) -> Optional[str]:
    """Resolve the community-script name from existing introspection facts.

    ``pull_rc`` must be a successful integer (or integer-like) exit code; the
    already-pulled ``script_stdout`` is parsed by the existing
    :func:`lxc_parse.script_name_from_update` — no second parser, no execution
    and no GitHub dependency.  Missing/unresolved input returns ``None``.
    """
    if not isinstance(facts, Mapping):
        return None
    raw_pull_rc = facts.get("pull_rc")
    if raw_pull_rc is None:
        return None
    try:
        pull_rc = int(raw_pull_rc)
    except (TypeError, ValueError):
        return None
    if pull_rc != 0:
        return None
    raw = facts.get("script_stdout")
    content = raw if isinstance(raw, str) else str(raw or "")
    name = script_name_from_update(content)
    return name or None


class GuestIdentity(Protocol):
    """Structural identity accepted from the existing checkpoint ``GuestKey``.

    Read-only properties keep this compatible with the frozen/slotted
    ``housekeeping_checkpoint.GuestKey`` rather than requiring settable fields.
    """

    @property
    def cluster(self) -> str:
        ...

    @property
    def node(self) -> str:
        ...

    @property
    def lxc_id(self) -> str:
        ...


@dataclass(frozen=True)
class GuestLogOverrideIndex:
    """Full-discovery-validated override lookup.

    Build once with the complete discovered guest set, then ask for each guest.
    This prevents a ``--limit``-restricted run from hiding a duplicate bare ID.
    Full ``cluster/node/id`` keys take precedence over a bare-id key.
    """

    full: Mapping[str, GuestLogOverride] = field(default_factory=dict)
    bare: Mapping[str, GuestLogOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "full", MappingProxyType(dict(self.full)))
        object.__setattr__(self, "bare", MappingProxyType(dict(self.bare)))

    def for_guest(self, guest: GuestIdentity) -> Optional[GuestLogOverride]:
        qualified = f"{guest.cluster}/{guest.node}/{guest.lxc_id}"
        if qualified in self.full:
            return self.full[qualified]
        return self.bare.get(str(guest.lxc_id))

    @classmethod
    def build(
        cls,
        overrides: Mapping[str, GuestLogOverride],
        *,
        discovered: Sequence[GuestIdentity],
    ) -> "GuestLogOverrideIndex":
        unique: Dict[Tuple[str, str, str], GuestIdentity] = {}
        for guest in discovered:
            unique[(str(guest.cluster), str(guest.node), str(guest.lxc_id))] = guest
        full: Dict[str, GuestLogOverride] = {}
        bare: Dict[str, GuestLogOverride] = {}
        used_bare: set = set()
        for raw_key, override in overrides.items():
            try:
                parsed = GuestLogOverrideKey.parse(raw_key)
            except ValueError as exc:
                raise GuestOverrideError(str(exc)) from exc
            if parsed.is_bare:
                if parsed.guest_id in used_bare:
                    raise GuestOverrideError(
                        f"duplicate bare guest override key {parsed.guest_id!r}"
                    )
                used_bare.add(parsed.guest_id)
                candidates = [
                    (key, guest)
                    for key, guest in unique.items()
                    if key[2] == parsed.guest_id
                ]
                if not candidates:
                    raise GuestOverrideError(
                        f"bare guest override key {parsed.guest_id!r} matches no "
                        "discovered fleet identity"
                    )
                if len(candidates) > 1:
                    identities = ", ".join(
                        f"{key[0]}/{key[1]}/{key[2]}" for key, _ in candidates
                    )
                    raise GuestOverrideError(
                        f"bare guest override key {parsed.guest_id!r} is ambiguous "
                        f"across full discovery: {identities}"
                    )
                bare[parsed.guest_id] = override
            else:
                qualified = parsed.qualified
                if qualified is None:
                    raise GuestOverrideError("full guest override key is incomplete")
                full[qualified] = override
        return cls(full=full, bare=bare)


def index_guest_log_overrides(
    overrides: Mapping[str, GuestLogOverride],
    *,
    discovered: Sequence[GuestIdentity],
) -> GuestLogOverrideIndex:
    """Build a full-discovery override lookup (see :class:`GuestLogOverrideIndex`)."""
    return GuestLogOverrideIndex.build(overrides, discovered=discovered)


def resolve_guest_log_override(
    overrides: Mapping[str, GuestLogOverride],
    *,
    guest: GuestIdentity,
    discovered: Sequence[GuestIdentity],
) -> Optional[GuestLogOverride]:
    """Convenience one-shot lookup using the full discovery set."""
    return index_guest_log_overrides(overrides, discovered=discovered).for_guest(guest)


# --------------------------------------------------------------------------- #
# Selection helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Occurrence:
    owner_id: str
    service_id: str
    plugin: LogPlugin
    layouts: Tuple[LogLayout, ...]


def _override_layout_key(occurrence: _Occurrence) -> str:
    if occurrence.owner_id == occurrence.service_id:
        return occurrence.owner_id
    return f"{occurrence.owner_id}/{occurrence.service_id}"


def _candidate_layouts(plugin: LogPlugin, key: str, overrides: Mapping[str, str]) -> Tuple[LogLayout, ...]:
    wanted = overrides.get(key)
    if wanted is None:
        return tuple(plugin.layouts)
    matches = tuple(layout for layout in plugin.layouts if layout.id == wanted)
    if not matches:
        raise LogSelectionError(f"unknown layout {wanted!r} for {key!r}")
    return matches


def _occurrences(selection: LogSelection) -> Tuple[_Occurrence, ...]:
    occurrences: List[_Occurrence] = []
    for owner_id in sorted(selection.owner_ids):
        plugin = selection.catalogue.plugin(owner_id)
        occurrences.append(
            _Occurrence(
                owner_id=owner_id,
                service_id=owner_id,
                plugin=plugin,
                layouts=_candidate_layouts(plugin, owner_id, selection.layouts),
            )
        )
        for dependency_id in sorted(plugin.includes):
            dependency = selection.catalogue.plugin(dependency_id)
            occurrences.append(
                _Occurrence(
                    owner_id=owner_id,
                    service_id=dependency_id,
                    plugin=dependency,
                    layouts=_candidate_layouts(
                        dependency,
                        f"{owner_id}/{dependency_id}",
                        selection.layouts,
                    ),
                )
            )
    return tuple(occurrences)


def _source_key(occurrence: _Occurrence, source_id: str) -> str:
    if occurrence.owner_id == occurrence.service_id:
        return f"{occurrence.owner_id}/{source_id}"
    return f"{occurrence.owner_id}/{occurrence.service_id}/{source_id}"


def _resolved_labels(
    occurrence: _Occurrence, source: LogSource
) -> Tuple[str, str]:
    if occurrence.owner_id == occurrence.service_id:
        return source.app, source.log_kind
    return occurrence.owner_id, source.id


def _default_component(source_key: str) -> str:
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    return f"fleet_{digest[:24]}"


def _component_for(
    occurrence: _Occurrence, source: LogSource, source_key: str
) -> str:
    if occurrence.owner_id == occurrence.service_id and source.component is not None:
        return source.component
    return _default_component(source_key)


def _find_source_in_layout(layout: LogLayout, source_id: str) -> Optional[LogSource]:
    for source in layout.sources:
        if source.id == source_id:
            return source
    return None


def _validate_source_override_key(
    selection: LogSelection,
    occurrences: Sequence[_Occurrence],
    key: str,
    *,
    locations: bool,
) -> None:
    parts = key.split("/")
    if len(parts) == 2:
        owner_id, source_id = parts
        candidates = [
            occurrence
            for occurrence in occurrences
            if occurrence.owner_id == occurrence.service_id == owner_id
        ]
        if not candidates:
            raise LogSelectionError(
                f"source override {key!r} targets unselected service {owner_id!r}"
            )
    else:
        owner_id, dependency_id, source_id = parts
        candidates = [
            occurrence
            for occurrence in occurrences
            if occurrence.owner_id == owner_id and occurrence.service_id == dependency_id
        ]
        if not candidates:
            raise LogSelectionError(
                f"source override {key!r} targets an unselected dependency"
            )

    containing: List[Tuple[_Occurrence, LogSource]] = []
    for occurrence in candidates:
        for layout in occurrence.layouts:
            source = _find_source_in_layout(layout, source_id)
            if source is not None:
                containing.append((occurrence, source))
    if not containing:
        raise LogSelectionError(f"unknown source override {key!r}")
    if locations:
        paths = selection.locations.get(key)
        if paths:
            for _occurrence, source in containing:
                _effective_descriptor(source, paths)


def _source_exists_in_plugin(plugin: LogPlugin, source_id: str) -> bool:
    return any(
        _find_source_in_layout(layout, source_id) is not None
        for layout in plugin.layouts
    )


def _validate_override_catalogue_ids(
    catalogue: LogCatalogue, override: GuestLogOverride
) -> None:
    """Reject unknown override IDs even when no identity selects a root."""
    for key in (*override.disable_sources, *override.locations):
        parts = key.split("/")
        try:
            if len(parts) == 2:
                owner_id, source_id = parts
                plugin = catalogue.plugin(owner_id)
                exists = _source_exists_in_plugin(plugin, source_id)
            else:
                owner_id, dependency_id, source_id = parts
                owner = catalogue.plugin(owner_id)
                if dependency_id not in owner.includes:
                    raise LogSelectionError(
                        f"source override {key!r} targets an unknown include"
                    )
                dependency = catalogue.plugin(dependency_id)
                exists = _source_exists_in_plugin(dependency, source_id)
        except LogSelectionError as exc:
            if str(exc).startswith("unknown source override"):
                raise
            raise LogSelectionError(f"unknown source override {key!r}") from exc
        if not exists:
            raise LogSelectionError(f"unknown source override {key!r}")
    for key, layout_id in override.layouts.items():
        parts = key.split("/")
        try:
            if len(parts) == 1:
                plugin = catalogue.plugin(parts[0])
            else:
                owner = catalogue.plugin(parts[0])
                if parts[1] not in owner.includes:
                    raise LogSelectionError(
                        f"layout override {key!r} targets an unknown include"
                    )
                plugin = catalogue.plugin(parts[1])
        except LogSelectionError as exc:
            raise LogSelectionError(
                f"layout override {key!r} targets an unknown service/include"
            ) from exc
        if not any(layout.id == layout_id for layout in plugin.layouts):
            raise LogSelectionError(f"unknown layout {layout_id!r} for {key!r}")


def _validate_selection_overrides(
    selection: LogSelection, occurrences: Sequence[_Occurrence]
) -> None:
    for key in selection.locations:
        _validate_source_override_key(selection, occurrences, key, locations=True)
    for key in selection.disable_sources:
        _validate_source_override_key(selection, occurrences, key, locations=False)
    for key in selection.layouts:
        parts = key.split("/")
        if len(parts) == 1:
            matches = [
                occurrence
                for occurrence in occurrences
                if occurrence.owner_id == occurrence.service_id == key
            ]
            if not matches:
                raise LogSelectionError(f"layout override {key!r} targets an unselected service")
        else:
            owner_id, dependency_id = parts
            matches = [
                occurrence
                for occurrence in occurrences
                if occurrence.owner_id == owner_id
                and occurrence.service_id == dependency_id
            ]
            if not matches:
                raise LogSelectionError(
                    f"layout override {key!r} targets an unselected dependency"
                )


def select_log_plugins(
    catalogue: LogCatalogue,
    *,
    script_id: Optional[str],
    override: Optional[GuestLogOverride],
    auto: bool,
) -> LogSelection:
    """Select owner roots without probe/network access or guest mutation."""
    if override is None:
        override = GuestLogOverride()
    mode = override.mode
    explicit = tuple(override.plugins)
    if mode == "auto" and explicit:
        raise LogSelectionError("mode 'auto' must not carry explicit plugins")
    if mode == "disabled" and (
        explicit
        or override.disable_plugins
        or override.disable_sources
        or override.locations
        or override.layouts
    ):
        raise LogSelectionError("mode 'disabled' must not carry additions")
    for plugin_id in (*explicit, *override.disable_plugins):
        catalogue.plugin(plugin_id)
    _validate_override_catalogue_ids(catalogue, override)

    evidence: List[str] = []
    auto_id = catalogue.auto_plugin_id(script_id) if auto else None

    if mode == "disabled":
        return LogSelection(
            status="empty",
            owner_ids=(),
            catalogue=catalogue,
            mode=mode,
            evidence=("empty selection: override mode disabled",),
        )

    if mode == "replace":
        roots = set(explicit)
        if explicit:
            evidence.append("explicit replacement: " + ", ".join(sorted(roots)))
    elif mode == "extend":
        roots = set(explicit)
        if auto_id is not None:
            roots.add(auto_id)
            evidence.append(f"auto identity {script_id!r} -> plugin {auto_id!r}")
        if explicit:
            evidence.append("explicit additions: " + ", ".join(sorted(explicit)))
    else:
        roots = {auto_id} if auto_id is not None else set()
        if auto_id is not None:
            evidence.append(f"auto identity {script_id!r} -> plugin {auto_id!r}")

    disabled_roots = set(override.disable_plugins)
    removed = sorted(roots & disabled_roots)
    if removed:
        roots -= disabled_roots
        evidence.append("disabled plugins: " + ", ".join(removed))

    for disabled in disabled_roots:
        for root in roots:
            parent = catalogue.plugin(root)
            if disabled in parent.includes:
                raise LogSelectionError(
                    f"included dependency {disabled!r} cannot be disabled with "
                    "disable_plugins; disable its qualified sources instead"
                )

    if not roots:
        if mode in ("auto", "extend") and auto and auto_id is None and not explicit:
            reason = (
                f"no recognized service profile for community-script identity {script_id!r}"
                if script_id
                else "no community-script identity resolved"
            )
            return LogSelection(
                status="unsupported",
                owner_ids=(),
                catalogue=catalogue,
                mode=mode,
                evidence=(f"unsupported identity: {reason}",),
            )
        if mode == "replace":
            detail = "explicit replacement with no plugins"
        elif not auto and mode == "auto":
            detail = "automatic selection disabled with no explicit plugins"
        elif mode == "extend" and not explicit:
            detail = "automatic selection disabled or unresolved with no explicit additions"
        else:
            detail = "all selected plugins were disabled" if disabled_roots else "no selected plugins"
        return LogSelection(
            status="empty",
            owner_ids=(),
            catalogue=catalogue,
            mode=mode,
            evidence=(f"empty selection: {detail}",),
        )

    locations = {key: tuple(paths) for key, paths in override.locations.items()}
    selection = LogSelection(
        status="selected",
        owner_ids=tuple(sorted(roots)),
        catalogue=catalogue,
        mode=mode,
        disable_sources=frozenset(override.disable_sources),
        locations=locations,
        layouts=dict(override.layouts),
        evidence=tuple(evidence),
    )
    _validate_selection_overrides(selection, _occurrences(selection))
    return selection


# --------------------------------------------------------------------------- #
# Effective source descriptors
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _EffectiveSource:
    paths: Tuple[str, ...]
    exact_files: Tuple[str, ...]
    roots: Tuple[str, ...]
    exclude_paths: Tuple[str, ...]


def _effective_roots(source: LogSource, paths: Sequence[str]) -> Tuple[str, ...]:
    roots = set()
    candidates = tuple(source.allowed_roots) + tuple(source.alternate_roots)
    for pattern in paths:
        containing = [root for root in candidates if pattern_beneath(pattern, root)]
        if not containing:
            raise LogSelectionError(
                f"source path {pattern!r} is outside the source's audited roots"
            )
        roots.add(max(containing, key=len))
    return tuple(sorted(roots))


def _quarantine_pattern(root: str) -> str:
    return f"{root}/**/{QUARANTINE_DIRNAME}/**"


def _effective_descriptor(
    source: LogSource, locations: Optional[Tuple[str, ...]]
) -> Optional[_EffectiveSource]:
    """Apply a locations override within the source's own audited scope."""
    if locations is None:
        paths = tuple(source.paths)
        exact_files = tuple(source.exact_files)
    elif not locations:
        return None
    elif source.allowed_roots:
        for pattern in locations:
            if not any(
                pattern_beneath(pattern, root)
                for root in (*source.allowed_roots, *source.alternate_roots)
            ):
                raise LogSelectionError(
                    f"location {pattern!r} is outside the source's audited roots"
                )
        paths = tuple(locations)
        exact_files = tuple(source.exact_files)
    else:
        for pattern in locations:
            if pattern not in source.exact_files:
                raise LogSelectionError(
                    f"location {pattern!r} is not one of the source's exact files"
                )
        paths = ()
        exact_files = tuple(locations)

    if not paths and not exact_files:
        return None
    patterns: List[str] = [*paths, *exact_files]
    for index, first in enumerate(patterns):
        for second in patterns[index + 1 :]:
            if patterns_overlap(first, second):
                raise LogSelectionError(
                    f"source {source.id!r} has overlapping patterns after override"
                )

    roots = _effective_roots(source, paths)
    excludes: List[str] = list(source.exclude_paths)
    for root in roots:
        quarantine = _quarantine_pattern(root)
        if quarantine not in excludes:
            excludes.append(quarantine)
    return _EffectiveSource(
        paths=paths,
        exact_files=exact_files,
        roots=roots,
        exclude_paths=tuple(excludes),
    )


def _iter_layout_sources(
    selection: LogSelection,
    occurrence: _Occurrence,
    layout: LogLayout,
) -> Iterable[Tuple[LogSource, Optional[_EffectiveSource]]]:
    for source in layout.sources:
        key = _source_key(occurrence, source.id)
        if key in selection.disable_sources:
            yield source, None
            continue
        override = selection.locations.get(key)
        descriptor = _effective_descriptor(source, override)
        if descriptor is None:
            yield source, None
            continue
        yield source, descriptor


# --------------------------------------------------------------------------- #
# Logging request
# --------------------------------------------------------------------------- #


def _unit_payload(unit: UnitEvidence) -> Dict[str, Any]:
    return {
        "name": unit.name,
        "scope": unit.scope,
        "user": unit.user,
        "key": unit.key,
    }


def _layout_payload(layout: LogLayout) -> Dict[str, Any]:
    return {
        "id": layout.id,
        "required_units": [_unit_payload(unit) for unit in layout.required_units],
        "running_units_any": [_unit_payload(unit) for unit in layout.running_units_any],
        "source_ids": [source.id for source in layout.sources],
    }


def _source_layout_payload(
    occurrence: _Occurrence,
    layout: LogLayout,
    source: LogSource,
    descriptor: _EffectiveSource,
    source_key: str,
) -> Dict[str, Any]:
    app, log_kind = _resolved_labels(occurrence, source)
    multiline = None
    if source.multiline is not None:
        multiline = {
            "firstline": source.multiline.firstline,
            "max_lines": source.multiline.max_lines,
            "max_wait_seconds": source.multiline.max_wait_seconds,
        }
    return {
        "layout_id": layout.id,
        "kind": occurrence.plugin.kind,
        "app": app,
        "log_kind": log_kind,
        "component": _component_for(occurrence, source, source_key),
        "required": source.required,
        "multiline": multiline,
        "allowed_roots": list(source.allowed_roots),
        "alternate_roots": list(source.alternate_roots),
        "paths": list(descriptor.paths),
        "exact_files": list(descriptor.exact_files),
        "exclude_paths": list(descriptor.exclude_paths),
        "roots": list(descriptor.roots),
        "service_user": layout.service_user,
    }


def build_logging_request(selection: LogSelection) -> Dict[str, Any]:
    """Build the JSON-safe read-only helper request for all candidate layouts."""
    if selection.status != "selected":
        return {"plugins": {}, "sources": {}}
    plugins: Dict[str, Any] = {}
    sources: Dict[str, Any] = {}

    for occurrence in _occurrences(selection):
        if occurrence.owner_id == occurrence.service_id:
            owner_entry = plugins.setdefault(
                occurrence.owner_id,
                {
                    "owner_id": occurrence.owner_id,
                    "service_id": occurrence.owner_id,
                    "kind": occurrence.plugin.kind,
                    "journal_units": [
                        _unit_payload(unit) for unit in occurrence.plugin.journal_units
                    ],
                    "layouts": [],
                    "includes": {},
                },
            )
            entry = owner_entry
        else:
            owner_entry = plugins.setdefault(
                occurrence.owner_id,
                {
                    "owner_id": occurrence.owner_id,
                    "service_id": occurrence.owner_id,
                    "kind": selection.catalogue.plugin(occurrence.owner_id).kind,
                    "journal_units": [
                        _unit_payload(unit)
                        for unit in selection.catalogue.plugin(
                            occurrence.owner_id
                        ).journal_units
                    ],
                    "layouts": [],
                    "includes": {},
                },
            )
            entry = owner_entry["includes"].setdefault(
                occurrence.service_id,
                {
                    "owner_id": occurrence.owner_id,
                    "service_id": occurrence.service_id,
                    "kind": occurrence.plugin.kind,
                    "journal_units": [
                        _unit_payload(unit) for unit in occurrence.plugin.journal_units
                    ],
                    "layouts": [],
                },
            )

        entry["layouts"] = [_layout_payload(layout) for layout in occurrence.layouts]

        for layout in occurrence.layouts:
            for source, descriptor in _iter_layout_sources(selection, occurrence, layout):
                if descriptor is None:
                    continue
                key = _source_key(occurrence, source.id)
                source_entry = sources.setdefault(
                    key,
                    {
                        "owner_id": occurrence.owner_id,
                        "service_id": occurrence.service_id,
                        "source_id": source.id,
                        "layouts": {},
                    },
                )
                source_entry["layouts"][layout.id] = _source_layout_payload(
                    occurrence, layout, source, descriptor, key
                )
    return {"plugins": plugins, "sources": sources}


# --------------------------------------------------------------------------- #
# Unit facts / layout validation
# --------------------------------------------------------------------------- #


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LogLayoutError(f"{label} must be a mapping")
    return value


def _require_bool(value: Mapping[str, Any], key: str, label: str) -> bool:
    if key not in value or not isinstance(value[key], bool):
        raise LogLayoutError(f"{label}.{key} must be a boolean")
    return value[key]


def _require_nonnegative_int(value: Mapping[str, Any], key: str, label: str) -> int:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise LogLayoutError(f"{label}.{key} must be a nonnegative integer")
    return raw


def _unit_status(value: object, label: str) -> UnitStatus:
    if not isinstance(value, str) or value not in _UNIT_STATUSES:
        raise LogLayoutError(f"{label} has an invalid unit status")
    return cast(UnitStatus, value)


def _resolve_unit(evidence: UnitEvidence, units: Mapping[str, Any]) -> ResolvedUnit:
    if evidence.key not in units:
        raise LogLayoutError(
            f"missing requested unit evidence {evidence.key!r}; probe evidence "
            "must be complete"
        )
    raw = _mapping(units[evidence.key], f"unit evidence {evidence.key!r}")
    if "status" not in raw:
        raise LogLayoutError(f"unit evidence {evidence.key!r} is missing status")
    supplied = _unit_status(raw.get("status"), f"unit evidence {evidence.key!r}")
    instances_raw = raw.get("instances", {})
    instances_map = _mapping(instances_raw, f"unit evidence {evidence.key!r}.instances")
    instances: List[Tuple[str, str]] = []
    for instance, instance_status in instances_map.items():
        if not isinstance(instance, str) or "*" in instance or "/" in instance:
            raise LogLayoutError(f"unit evidence {evidence.key!r} has an invalid instance")
        instances.append(
            (instance, _unit_status(instance_status, f"instance {instance!r}"))
        )
    status: UnitStatus = supplied
    if "*" in evidence.name:
        if not instances:
            if supplied == "active":
                raise LogLayoutError(
                    f"unit evidence {evidence.key!r} claims active without instances"
                )
        else:
            values = [value for _name, value in instances]
            derived: UnitStatus
            if "active" in values:
                derived = "active"
            elif "unavailable" in values:
                derived = "unavailable"
            elif "inactive" in values:
                derived = "inactive"
            else:
                derived = "missing"
            if supplied != derived:
                raise LogLayoutError(
                    f"unit evidence {evidence.key!r} status {supplied!r} contradicts "
                    f"instance evidence {derived!r}"
                )
            status = derived
    return ResolvedUnit(
        name=evidence.name,
        scope=evidence.scope,
        user=evidence.user,
        status=status,
        instances=tuple(instances),
    )


def _layout_failures(layout: LogLayout, resolved: Mapping[str, ResolvedUnit]) -> List[str]:
    failures: List[str] = []
    for unit in layout.required_units:
        status = resolved[unit.key].status
        if status != "active":
            failures.append(f"{unit.key} is {status}")
    if layout.running_units_any and not any(
        resolved[unit.key].status == "active" for unit in layout.running_units_any
    ):
        statuses = ", ".join(
            f"{unit.key}={resolved[unit.key].status}" for unit in layout.running_units_any
        )
        failures.append(f"no running_units_any member is active ({statuses})")
    return failures


def _choose_layout(
    selection: LogSelection,
    occurrence: _Occurrence,
    units: Mapping[str, Any],
) -> Tuple[LogLayout, Tuple[ResolvedUnit, ...], Tuple[ResolvedUnit, ...]]:
    all_evidence: List[UnitEvidence] = []
    for layout in occurrence.layouts:
        all_evidence.extend(layout.required_units)
        all_evidence.extend(layout.running_units_any)
    all_evidence.extend(occurrence.plugin.journal_units)

    resolved: Dict[str, ResolvedUnit] = {}
    for evidence in all_evidence:
        # A unit may appear in several predicate lists/journal metadata; the
        # facts mapping is keyed once and the resolved predicates merge later.
        if evidence.key not in resolved:
            resolved[evidence.key] = _resolve_unit(evidence, units)

    matching: List[LogLayout] = []
    reasons: List[str] = []
    for layout in occurrence.layouts:
        failures = _layout_failures(layout, resolved)
        if failures:
            reasons.append(f"{layout.id}: " + "; ".join(failures))
        else:
            matching.append(layout)
    key = _override_layout_key(occurrence)
    if not matching:
        raise LogLayoutError(
            f"no layout matched for {key!r}: " + (" | ".join(reasons) or "no layouts")
        )
    if len(matching) > 1:
        raise LogLayoutError(
            f"ambiguous layout for {key!r}: "
            + ", ".join(layout.id for layout in matching)
        )
    chosen = matching[0]

    ordered: List[ResolvedUnit] = []
    by_key: Dict[str, ResolvedUnit] = {}
    for role, evidence_list in (
        ("required", chosen.required_units),
        ("running_any", chosen.running_units_any),
        ("journal", occurrence.plugin.journal_units),
    ):
        for evidence in evidence_list:
            unit = resolved[evidence.key]
            previous = by_key.get(unit.key)
            if previous is None:
                unit = ResolvedUnit(
                    name=unit.name,
                    scope=unit.scope,
                    user=unit.user,
                    status=unit.status,
                    instances=unit.instances,
                    roles=(role,),
                )
                by_key[unit.key] = unit
                ordered.append(unit)
            elif role not in previous.roles:
                updated = ResolvedUnit(
                    name=previous.name,
                    scope=previous.scope,
                    user=previous.user,
                    status=previous.status,
                    instances=previous.instances,
                    roles=tuple((*previous.roles, role)),
                )
                by_key[updated.key] = updated
                ordered[ordered.index(previous)] = updated

    layout_units = tuple(
        unit for unit in ordered if unit.roles != ("journal",)
    )
    journal_units = tuple(unit for unit in ordered if "journal" in unit.roles)
    return chosen, layout_units, journal_units


# --------------------------------------------------------------------------- #
# Source facts / validation
# --------------------------------------------------------------------------- #


def _sanitize_finding(text: str) -> str:
    cleaned = " ".join(str(text).split())
    cleaned = cleaned.replace("`", "'")
    if len(cleaned) > _MAX_FINDING_CHARS:
        cleaned = cleaned[: _MAX_FINDING_CHARS - 3] + "..."
    return cleaned


_PROBE_STATUSES = frozenset(
    {"ok", "quiet", "missing_root", "denied", "unavailable", "failed", "partial"}
)


def _probe_status(value: object, label: str) -> str:
    if not isinstance(value, str) or value not in _PROBE_STATUSES:
        raise LogLayoutError(f"{label} has an invalid probe status")
    return value


def _access_flags(
    entry: Mapping[str, Any], label: str, keys: Sequence[str]
) -> Dict[str, bool]:
    access = entry.get("access")
    if access is None:
        if "accessible" not in entry:
            raise LogLayoutError(f"{label}.access is required")
        accessible = _require_bool(entry, "accessible", label)
        return {key: accessible for key in keys}
    mapping = _mapping(access, f"{label}.access")
    flags = {key: _require_bool(mapping, key, f"{label}.access") for key in keys}
    if "default_acl" in mapping:
        flags["default_acl"] = _require_bool(mapping, "default_acl", f"{label}.access")
    return flags


def _optional_nonnegative_int(
    entry: Mapping[str, Any], key: str, label: str
) -> Optional[int]:
    if key not in entry or entry[key] is None:
        return None
    return _require_nonnegative_int(entry, key, label)


def _validate_optional_metric(entry: Mapping[str, Any], key: str, label: str) -> None:
    if key not in entry or entry[key] is None:
        return
    _require_nonnegative_int(entry, key, label)


def _validate_observed_path(value: object) -> str:
    """Validate one observed regular-file path from probe evidence.

    Observed filenames are evidence, not authored configuration, so their exact
    spelling is preserved.  PBS task filenames legitimately contain
    systemd-style backslash escapes (for example ``store1\\x3act-100``); only
    structural path safety is enforced here.
    """
    if not isinstance(value, str):
        raise ValueError("path must be a string")
    if not value:
        raise ValueError("path must not be empty")
    if "\x00" in value:
        raise ValueError("path must not contain NUL")
    if not value.startswith("/") or value.startswith("//"):
        raise ValueError("path must be an absolute Linux path")
    if value != posixpath.normpath(value):
        raise ValueError(
            "path must be normalized (no '.', '..', '//' or trailing slash)"
        )
    return value


def _resolve_source_facts(
    source: ResolvedLogSource,
    facts: Mapping[str, Any],
) -> Tuple[ResolvedLogSource, Tuple[Tuple[str, int, int], ...]]:
    """Validate one chosen-layout source descriptor against probe evidence."""
    findings: List[str] = []
    identities: List[Tuple[str, int, int]] = []
    blocked = False
    partial = False
    unmeasurable = False
    acl_needed = False
    active = False
    inactive = False
    live_files = 0

    def record(text: str) -> None:
        message = _sanitize_finding(f"{source.key}: {text}")
        if message not in findings:
            findings.append(message)

    if "truncated" in facts and not isinstance(facts["truncated"], bool):
        raise LogLayoutError(f"source {source.key!r}.truncated must be a boolean")
    source_truncated = bool(facts.get("truncated", False))
    if source_truncated:
        partial = True
        record("source evidence is truncated; ownership/delivery remains unverified")

    roots_raw = _mapping(facts.get("roots", {}), f"source {source.key!r}.roots")
    if set(roots_raw) != set(source.roots):
        raise LogLayoutError(
            f"source {source.key!r} root evidence does not match the resolved roots"
        )
    root_exists: Dict[str, bool] = {}
    root_ok: Dict[str, bool] = {}
    root_devices: Dict[str, Optional[int]] = {}
    for root in source.roots:
        label = f"source {source.key!r}.roots[{root!r}]"
        raw = _mapping(roots_raw[root], label)
        status = _probe_status(raw.get("status"), label)
        exists = _require_bool(raw, "exists", label)
        is_dir = _require_bool(raw, "is_dir", label)
        is_symlink = _require_bool(raw, "is_symlink", label)
        blocked_ancestor = _require_bool(raw, "blocked_ancestor", label)
        device = _optional_nonnegative_int(raw, "device", label)
        if exists:
            _require_nonnegative_int(raw, "device", label)
        root_exists[root] = exists
        root_ok[root] = False
        root_devices[root] = device

        if exists and is_symlink:
            blocked = True
            record(f"root {root} is a symlink; symlink traversal is refused")
            continue
        if exists and blocked_ancestor:
            blocked = True
            record(f"root {root} has a blocked ancestor")
            continue
        if exists and not is_dir:
            blocked = True
            record(f"root {root} is not a directory")
            continue
        if status == "missing_root":
            if exists:
                raise LogLayoutError(
                    f"source {source.key!r} root {root!r} exists but is marked missing_root"
                )
            if "access" in raw or "accessible" in raw:
                access = _access_flags(
                    raw, label, ("traversable", "readable", "default_acl")
                )
                if access["traversable"] or access["readable"]:
                    raise LogLayoutError(
                        f"source {source.key!r} root {root!r} is absent but marked accessible"
                    )
            if source.required:
                blocked = True
                record(f"required root {root} is absent")
            else:
                inactive = True
                record(f"optional root {root} is absent; source inactive until it appears")
            continue
        if status == "unavailable":
            unmeasurable = True
            record(f"root {root} access is unmeasurable (collector prerequisite missing)")
            continue
        if status == "failed":
            blocked = True
            record(f"root {root} evidence failed")
            continue
        if status == "partial":
            partial = True
            record(f"root {root} evidence is partial")
            continue
        if status == "denied":
            acl_needed = True
            record(f"root {root} is denied to the Alloy user (ACL needed)")
            continue
        if not exists:
            raise LogLayoutError(
                f"source {source.key!r} root {root!r} is marked {status} but absent"
            )
        access = _access_flags(
            raw, label, ("traversable", "readable", "default_acl")
        )
        if (
            not access["traversable"]
            or not access["readable"]
            or not access["default_acl"]
        ):
            acl_needed = True
            record(f"root {root} lacks Alloy read/traversal/default ACL (ACL needed)")
            continue
        root_ok[root] = True
        active = True
        if device is None:
            raise LogLayoutError(f"source {source.key!r} root {root!r} has no device")

    matches_raw = facts.get("matches", [])
    if not isinstance(matches_raw, list):
        raise LogLayoutError(f"source {source.key!r}.matches must be a list")
    matches: Dict[str, Mapping[str, Any]] = {}
    for item in matches_raw:
        entry = _mapping(item, f"source {source.key!r} match evidence")
        pattern = entry.get("pattern")
        if not isinstance(pattern, str):
            raise LogLayoutError(f"source {source.key!r} match has no pattern")
        if pattern in matches:
            raise LogLayoutError(f"source {source.key!r} repeats pattern {pattern!r}")
        matches[pattern] = entry
    if set(matches) != set(source.paths):
        raise LogLayoutError(
            f"source {source.key!r} pattern evidence does not match the resolved paths"
        )

    effective_root_for: Dict[str, str] = {}
    for pattern in source.paths:
        prefix = literal_prefix(pattern)
        candidates = [root for root in source.roots if prefix.startswith(root + "/")]
        if not candidates:
            raise LogLayoutError(
                f"source {source.key!r} pattern {pattern!r} has no resolved root"
            )
        effective_root_for[pattern] = max(candidates, key=len)

    for pattern in source.paths:
        label = f"source {source.key!r} match {pattern!r}"
        entry = matches[pattern]
        status = _probe_status(entry.get("status"), label)
        matched = _require_nonnegative_int(entry, "matched", label)
        skipped = _optional_nonnegative_int(entry, "skipped", label) or 0
        truncated = _require_bool(entry, "truncated", label)
        root = effective_root_for[pattern]
        prefix = literal_prefix(pattern)
        files_raw = entry.get("files", [])
        if not isinstance(files_raw, list):
            raise LogLayoutError(f"{label}.files must be a list")
        if matched < len(files_raw):
            raise LogLayoutError(f"{label} matched count is smaller than its file list")
        if truncated:
            partial = True
            record(f"pattern below {root} was truncated; ownership is unverified")
        if (
            not truncated
            and status in {"ok", "quiet"}
            and matched != len(files_raw)
            and matched - skipped != len(files_raw)
        ):
            raise LogLayoutError(
                f"{label} omits matched files without an explicit truncated marker"
            )
        if status == "quiet" and (matched or files_raw):
            raise LogLayoutError(f"{label} is quiet but reports matched files")
        if status == "unavailable":
            unmeasurable = True
            record(f"pattern access below {root} is unmeasurable (collector prerequisite missing)")
            continue
        if status == "failed":
            blocked = True
            record(f"pattern evidence below {root} failed")
            continue
        if status == "partial":
            partial = True
            record(f"pattern evidence below {root} is partial")
            continue
        if status == "denied":
            acl_needed = True
            record(f"pattern below {root} is denied to the Alloy user (ACL needed)")
            continue
        if status == "missing_root":
            if root_exists[root] or files_raw:
                raise LogLayoutError(
                    f"{label} claims missing_root but reports a root/files"
                )
            continue
        if not root_exists[root]:
            raise LogLayoutError(
                f"{label} reports {status} below an absent root"
            )
        for raw_file in files_raw:
            file_label = f"{label} file evidence"
            file_entry = _mapping(raw_file, file_label)
            try:
                path = _validate_observed_path(file_entry.get("path"))
            except ValueError as exc:
                raise LogLayoutError(f"{label} has invalid file evidence: {exc}") from exc
            if not path.startswith(prefix):
                raise LogLayoutError(f"{label} file evidence is outside the pattern")
            exists = (
                _require_bool(file_entry, "exists", file_label)
                if "exists" in file_entry
                else True
            )
            if not exists:
                continue
            if not root_exists[root]:
                raise LogLayoutError(f"{label} has file evidence below an absent root")
            live_files += 1
            device = _require_nonnegative_int(file_entry, "device", file_label)
            inode = _require_nonnegative_int(file_entry, "inode", file_label)
            _validate_optional_metric(file_entry, "size", file_label)
            _validate_optional_metric(file_entry, "mtime_ns", file_label)
            if root_devices[root] is not None and device != root_devices[root]:
                raise LogLayoutError(
                    f"{label} file evidence crosses a mount/device boundary"
                )
            identities.append((path, device, inode))
            accessible = (
                _require_bool(file_entry, "accessible", file_label)
                if "accessible" in file_entry
                else root_ok[root]
            )
            if not accessible:
                acl_needed = True
                record(f"matched file below {root} is not readable by the Alloy user")
            elif root_ok[root] and not truncated and status in {"ok", "quiet"}:
                active = True
        if (
            not files_raw
            and root_ok[root]
            and not truncated
            and status in {"ok", "quiet"}
        ):
            active = True
            record(f"no current files match {pattern}; source is quiet, delivery unverified")

    exact_raw = _mapping(
        facts.get("exact_files", {}), f"source {source.key!r}.exact_files"
    )
    if set(exact_raw) != set(source.exact_files):
        raise LogLayoutError(
            f"source {source.key!r} exact-file evidence does not match the resolved files"
        )
    for path in source.exact_files:
        label = f"source {source.key!r}.exact_files[{path!r}]"
        entry = _mapping(exact_raw[path], label)
        status = _probe_status(entry.get("status"), label)
        exists = _require_bool(entry, "exists", label)
        is_symlink = _require_bool(entry, "is_symlink", label)
        parent = (
            _require_bool(entry, "parent_traversable", label)
            if "parent_traversable" in entry
            else None
        )
        same_device = (
            _require_bool(entry, "same_device", label)
            if "same_device" in entry
            else None
        )
        _validate_optional_metric(entry, "size", label)
        _validate_optional_metric(entry, "mtime_ns", label)
        if exists:
            if is_symlink:
                blocked = True
                record("declared exact file is a symlink; symlink traversal is refused")
                continue
            if same_device is False:
                blocked = True
                record("declared exact file is on a different device")
                continue
            if "is_file" in entry and not _require_bool(entry, "is_file", label):
                blocked = True
                record("declared exact file is not a regular file")
                continue
            device = _require_nonnegative_int(entry, "device", label)
            inode = _require_nonnegative_int(entry, "inode", label)
            identities.append((path, device, inode))
            live_files += 1
        if status == "unavailable":
            unmeasurable = True
            record("exact-file access is unmeasurable (collector prerequisite missing)")
            continue
        if status == "failed":
            blocked = True
            record("exact-file evidence failed")
            continue
        if status == "partial":
            partial = True
            record("exact-file evidence is partial")
            continue
        if status == "denied":
            acl_needed = True
            record("declared exact file is denied to the Alloy user (ACL needed)")
            continue
        if status == "missing_root":
            if exists or parent is True:
                raise LogLayoutError(
                    f"{label} is marked missing_root but contradicts file/parent evidence"
                )
        if exists and status == "quiet":
            raise LogLayoutError(f"{label} is marked quiet but exists")
        if exists:
            access = _access_flags(entry, label, ("readable",))
            if not access["readable"]:
                acl_needed = True
                record("declared exact file is not readable by the Alloy user (ACL needed)")
            elif parent is False:
                acl_needed = True
                record("declared exact-file parent is not traversable by the Alloy user")
            else:
                active = True
            continue
        if status == "ok":
            raise LogLayoutError(f"{label} is marked ok but is absent")
        if parent:
            active = True
            record("declared exact file is absent; configured for future creation")
        elif source.required:
            blocked = True
            record("required exact file is absent and its parent is not traversable")
        else:
            inactive = True
            record("optional exact file is absent and its parent is not traversable")

    if blocked:
        status_value: Literal[
            "active",
            "quiet",
            "unmeasurable",
            "inactive",
            "acl-needed",
            "partial",
            "blocked",
        ] = "blocked"
    elif unmeasurable:
        status_value = "unmeasurable"
    elif partial:
        status_value = "partial"
    elif acl_needed:
        status_value = "acl-needed"
    elif active:
        status_value = "quiet" if live_files == 0 else "active"
    elif inactive:
        status_value = "inactive"
    else:
        status_value = "blocked"

    if "status" in facts:
        supplied = _probe_status(facts.get("status"), f"source {source.key!r}.status")
        if supplied == "unavailable" and status_value != "blocked":
            status_value = "unmeasurable"
        elif supplied == "failed":
            status_value = "blocked"
        elif supplied == "partial" and status_value not in {"blocked", "unmeasurable"}:
            status_value = "partial"
        elif supplied == "denied" and status_value not in {
            "blocked",
            "unmeasurable",
            "partial",
        }:
            status_value = "acl-needed"
        elif supplied == "missing_root" and status_value not in {
            "blocked",
            "unmeasurable",
            "partial",
        }:
            status_value = "blocked" if source.required else "inactive"
        elif supplied in {"ok", "quiet"} and status_value in {
            "blocked",
            "unmeasurable",
            "partial",
            "acl-needed",
            "inactive",
        }:
            raise LogLayoutError(
                f"source {source.key!r} status {supplied!r} contradicts its evidence"
            )
        elif supplied == "quiet" and live_files:
            raise LogLayoutError(
                f"source {source.key!r} status quiet contradicts live file evidence"
            )

    resolved = ResolvedLogSource(
        owner_id=source.owner_id,
        service_id=source.service_id,
        source_id=source.source_id,
        key=source.key,
        layout_id=source.layout_id,
        kind=source.kind,
        app=source.app,
        log_kind=source.log_kind,
        component=source.component,
        required=source.required,
        multiline=source.multiline,
        paths=source.paths,
        exclude_paths=source.exclude_paths,
        roots=source.roots,
        exact_files=source.exact_files,
        service_user=source.service_user,
        status=status_value,
        findings=tuple(findings),
        live_files=live_files,
    )
    return resolved, tuple(identities)


def _descriptor_source(
    occurrence: _Occurrence,
    layout: LogLayout,
    source: LogSource,
    descriptor: _EffectiveSource,
) -> ResolvedLogSource:
    key = _source_key(occurrence, source.id)
    app, log_kind = _resolved_labels(occurrence, source)
    return ResolvedLogSource(
        owner_id=occurrence.owner_id,
        service_id=occurrence.service_id,
        source_id=source.id,
        key=key,
        layout_id=layout.id,
        kind=occurrence.plugin.kind,
        app=app,
        log_kind=log_kind,
        component=_component_for(occurrence, source, key),
        required=source.required,
        multiline=source.multiline,
        paths=descriptor.paths,
        exclude_paths=descriptor.exclude_paths,
        roots=descriptor.roots,
        exact_files=descriptor.exact_files,
        service_user=layout.service_user,
        status="blocked",
    )


# --------------------------------------------------------------------------- #
# Layout validation
# --------------------------------------------------------------------------- #


def _empty_resolved(selection: LogSelection) -> ResolvedLogSelection:
    return ResolvedLogSelection(
        status=selection.status,
        owners=selection.owner_ids,
        services=(),
        sources=(),
        evidence=selection.evidence,
    )


def validate_log_layout(
    selection: LogSelection, facts: Mapping[str, Any]
) -> ResolvedLogSelection:
    """Validate complete unit/source probe facts for one selected bundle.

    Exactly one layout must match for every owner/dependency occurrence.
    Missing/contradictory evidence raises rather than producing a verified
    source.  Source-level ``inactive``/``acl-needed``/``blocked`` results are
    retained on the resolved source and are never rendered.
    """
    if selection.status != "selected":
        return _empty_resolved(selection)
    if not isinstance(facts, Mapping):
        raise LogLayoutError("probe facts must be a mapping")
    plugins_facts = _mapping(facts.get("plugins"), "probe facts.plugins")
    sources_facts_raw = facts.get("sources")
    sources_facts = (
        _mapping(sources_facts_raw, "probe facts.sources")
        if sources_facts_raw is not None
        else {}
    )

    occurrences = _occurrences(selection)
    resolved_services: List[ResolvedLogService] = []
    resolved_descriptors: List[Tuple[_Occurrence, LogLayout, ResolvedLogSource]] = []
    layout_evidence: List[str] = []
    used_override_keys: set = set()

    for occurrence in occurrences:
        if occurrence.owner_id == occurrence.service_id:
            entry_raw = plugins_facts.get(occurrence.owner_id)
        else:
            owner_raw = plugins_facts.get(occurrence.owner_id)
            owner_entry = _mapping(
                owner_raw, f"probe facts.plugins[{occurrence.owner_id!r}]"
            )
            includes = _mapping(
                owner_entry.get("includes"),
                f"probe facts.plugins[{occurrence.owner_id!r}].includes",
            )
            entry_raw = includes.get(occurrence.service_id)
        entry = _mapping(
            entry_raw,
            f"probe facts for {occurrence.owner_id}/{occurrence.service_id}",
        )
        units = _mapping(
            entry.get("units"),
            f"probe facts for {occurrence.owner_id}/{occurrence.service_id}.units",
        )
        chosen, layout_units, journal_units = _choose_layout(selection, occurrence, units)
        layout_evidence.append(
            f"{_override_layout_key(occurrence)} layout {chosen.id}"
        )
        resolved_services.append(
            ResolvedLogService(
                owner_id=occurrence.owner_id,
                service_id=occurrence.service_id,
                kind=occurrence.plugin.kind,
                layout_id=chosen.id,
                units=layout_units,
                journal_units=journal_units,
                service_user=chosen.service_user,
                has_primary_predicate=chosen.has_primary_predicate,
            )
        )
        for source in chosen.sources:
            key = _source_key(occurrence, source.id)
            if key in selection.disable_sources or key in selection.locations:
                used_override_keys.add(key)
        for source, descriptor in _iter_layout_sources(selection, occurrence, chosen):
            if descriptor is None:
                continue
            resolved_descriptors.append(
                (
                    occurrence,
                    chosen,
                    _descriptor_source(occurrence, chosen, source, descriptor),
                )
            )

    for key in (*selection.disable_sources, *selection.locations):
        if key not in used_override_keys:
            raise LogSelectionError(
                f"source override {key!r} is not present in the selected layout"
            )

    if not any(service.has_primary_predicate for service in resolved_services):
        raise LogLayoutError(
            "selected bundle has no primary running-state predicate; journal "
            "units and empty layouts cannot prove an application is running"
        )

    # Ownership conflicts are checked on the chosen layouts only.
    pattern_owners: List[Tuple[str, str]] = []
    components: Dict[str, str] = {}
    for _occurrence, _layout, resolved_source in resolved_descriptors:
        previous = components.get(resolved_source.component)
        if previous is not None:
            raise LogLayoutError(
                f"component {resolved_source.component!r} is claimed by both "
                f"{previous!r} and {resolved_source.key!r}"
            )
        components[resolved_source.component] = resolved_source.key
        for pattern in (*resolved_source.paths, *resolved_source.exact_files):
            pattern_owners.append((resolved_source.key, pattern))
    for index, (first_key, first) in enumerate(pattern_owners):
        for second_key, second in pattern_owners[index + 1 :]:
            if patterns_overlap(first, second):
                raise LogLayoutError(
                    f"source ownership conflict between {first_key!r} and "
                    f"{second_key!r}: patterns overlap"
                )

    resolved_sources: List[ResolvedLogSource] = []
    identities: Dict[Tuple[int, int], str] = {}
    concrete_paths: Dict[str, str] = {}
    for _occurrence, _layout, resolved_source in resolved_descriptors:
        if resolved_source.key not in sources_facts:
            raise LogLayoutError(
                f"missing requested source evidence for {resolved_source.key!r}; "
                "source facts must be complete"
            )
        source_fact = _mapping(
            sources_facts[resolved_source.key],
            f"source evidence {resolved_source.key!r}",
        )
        layouts_raw = _mapping(
            source_fact.get("layouts"),
            f"source evidence {resolved_source.key!r}.layouts",
        )
        layout_fact = _mapping(
            layouts_raw.get(resolved_source.layout_id),
            f"source evidence {resolved_source.key!r}.layouts"
            f"[{resolved_source.layout_id!r}]",
        )
        resolved, source_identities = _resolve_source_facts(
            resolved_source, layout_fact
        )
        for path, device, inode in source_identities:
            if path in concrete_paths:
                raise LogLayoutError(
                    f"concrete source path is owned by both {concrete_paths[path]!r} "
                    f"and {resolved_source.key!r}"
                )
            concrete_paths[path] = resolved_source.key
            identity = (device, inode)
            if identity in identities:
                raise LogLayoutError(
                    f"source file identity (device,inode) is shared by "
                    f"{identities[identity]!r} and {resolved_source.key!r}"
                )
            identities[identity] = resolved_source.key
        resolved_sources.append(resolved)

    findings: List[str] = []
    for resolved_source in resolved_sources:
        findings.extend(resolved_source.findings)
    return ResolvedLogSelection(
        status="selected",
        owners=selection.owner_ids,
        services=tuple(resolved_services),
        sources=tuple(resolved_sources),
        evidence=tuple((*selection.evidence, *layout_evidence)),
        findings=tuple(findings),
    )


# --------------------------------------------------------------------------- #
# Disk policy
# --------------------------------------------------------------------------- #


def _generic_disk_policy() -> DiskUsagePolicy:
    return DiskUsagePolicy(
        default_max_share_percent=GENERIC_DISK_DEFAULT_SHARE_PERCENT,
        min_directory_mb=GENERIC_DISK_MIN_DIRECTORY_MB,
        path_max_share_percent=dict(GENERIC_DISK_PATH_CAPS),
    )


def disk_usage_extra_paths(policy: DiskUsagePolicy) -> Tuple[str, ...]:
    """Sorted deep (depth > 3) prefixes that need their own bounded counter."""
    return tuple(
        sorted(
            path
            for path in policy.path_max_share_percent
            if path_depth(path) > 3
        )
    )


def resolve_disk_usage_policy(
    catalogue: Optional[LogCatalogue],
    *,
    script_id: Optional[str],
    override: Optional[GuestLogOverride],
    auto: bool,
) -> Tuple[DiskUsagePolicy, Optional[str]]:
    """Resolve root/include disk metadata independently from Alloy deployment."""
    if catalogue is None:
        return _generic_disk_policy(), GENERIC_DISK_UNAVAILABLE
    try:
        selection = select_log_plugins(
            catalogue,
            script_id=script_id,
            override=override,
            auto=auto,
        )
    except LogPluginError:
        return _generic_disk_policy(), GENERIC_DISK_INVALID

    if selection.status == "unsupported":
        return _generic_disk_policy(), GENERIC_DISK_NO_PROFILE
    if selection.status == "empty":
        return _generic_disk_policy(), None

    root_defaults: List[float] = []
    root_floors: List[int] = []
    root_caps: Dict[str, float] = {}
    included_caps: Dict[str, float] = {}
    for owner_id in selection.owner_ids:
        plugin = catalogue.plugin(owner_id)
        policy = plugin.disk_usage
        if policy is not None:
            if policy.default_max_share_percent is not None:
                root_defaults.append(policy.default_max_share_percent)
            if policy.min_directory_mb is not None:
                root_floors.append(policy.min_directory_mb)
            for path, cap in policy.path_max_share_percent.items():
                root_caps[path] = max(root_caps.get(path, 0.0), cap)
        for dependency_id in plugin.includes:
            dependency = catalogue.plugin(dependency_id)
            dependency_policy = dependency.disk_usage
            if dependency_policy is None:
                continue
            for path, cap in dependency_policy.path_max_share_percent.items():
                included_caps[path] = max(included_caps.get(path, 0.0), cap)

    merged: Dict[str, float] = dict(GENERIC_DISK_PATH_CAPS)
    for path, cap in included_caps.items():
        merged[path] = max(merged.get(path, 0.0), cap)
    # Root-explicit caps override generic/included caps; ties take the maximum
    # among roots, which root_caps already holds.
    merged.update(root_caps)
    try:
        policy = DiskUsagePolicy(
            default_max_share_percent=max(
                root_defaults, default=GENERIC_DISK_DEFAULT_SHARE_PERCENT
            ),
            min_directory_mb=max(
                root_floors, default=GENERIC_DISK_MIN_DIRECTORY_MB
            ),
            path_max_share_percent=merged,
        )
    except ValueError:
        return _generic_disk_policy(), GENERIC_DISK_INVALID
    return policy, None
