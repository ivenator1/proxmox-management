# TODO: automatic application-log plugins

Status: implementation plan with user-confirmed scope; plugin system not implemented. Creating this document does not authorize a live logging rollout or broaden file-deletion permissions.

## Goal and confirmed requirements

- [x] Record this implementation plan in the repository.
- [x] Make supported applications work without manual plugin selection: identify the running app, select its log plugin, and reconcile the resulting Alloy configuration.
- [x] Prefer the installed Proxmox community-script identity as the application identifier.
- [x] Make adding another supported app require a declarative plugin file, not application-specific Python or Ansible changes.
- [x] Allow explicit plugin selection, disabling and log-location overrides through a settings file.
- [x] Keep collection, historical archive coverage and deletion authorization distinct.

"Automatic" means no per-guest configuration for a supported, normally installed community-script application once managed logging is enabled. Other installations require a settings override. It does not mean inventing paths for an unsupported app, bypassing exclusions, installing missing prerequisites during recurring maintenance, or silently modifying the firewall.

## Existing implementation to reuse

- `proxmox_fleet/lxc_parse.py::script_name_from_update()` extracts the script name from `/usr/bin/update`, supporting both legacy `ct/APP.sh` references and the newer `UPDATE_SCRIPT_NAME` wrapper. Do not add a second parser or execute the update script to identify an app.
- `ansible/primitives/lxc_introspect.yml` and `proxmox_fleet/flows/lxc.py` already provide/read `script_stdout` and `pull_rc`. Logging-only currently returns before the normal app-detection section, so the shared identity resolver must also work in logging-only and housekeeping-only modes.
- `proxmox_fleet/alloy.py` owns desired-content hashes, configuration validation, reconciliation and live file-source rendering. Its current `npm`/`pbs` mapping is hard-coded; migrate those mappings into plugin files without changing their established behavior.
- `GlobalSettings.load()` reads `vars.yml` or the existing `--vars-file` selection. Put overrides in this typed settings interface rather than introducing a separate configuration-loading path.
- `driver.run_fleet()` remains the orchestrator. Python owns selection and policy; Ansible remains a thin execution layer.
- Existing archive/checkpoint/lineage/retention modules own acknowledgements and deletion safety. A plugin match is not evidence of archive completion or delivery health.

## User-confirmed scope decisions

- [x] **D1 — Community scripts only:** automatically select only from a recognized installed community-script identity. If that identity is absent or unresolved, report the finding and require a settings override; do not fall back to service/process/path matching. Those probes validate an already selected plugin, not infer another app.
- [x] **D2 — Live collection only for new apps:** no new-app historical archive importer, local-retention tightening or file deletion in this implementation. Existing NPM/PBS backfill and gated retention remain unchanged. Normal live-reader catch-up is not a declaration of complete historical archive coverage.
- [x] **D3 — Whole community-script installation:** one plugin covers the primary app and the dependencies installed as part of that script's standard deployment, including databases. Use the display name `Proxmox community script [app]` and the verified primary community-script ID as its stable identity. Do not rename upstream scripts or require separate manual dependency-plugin selection. Verify sources against the actual installed layout; do not include unrelated services merely because they are running.
- [x] **D4 — Audited installed apps first:** provide the initial catalogue for the audited installed managed-LXC apps and verified layouts, not the entire upstream catalogue. Unsupported apps remain explicit findings; adding support requires another manifest, not guessed paths.

## Implementation checklist

### 1. Define the plugin-file interface and catalogue loader

- [ ] Define a versioned, typed YAML schema for a plugin: stable ID, supported community-script IDs/aliases, installation/running-state evidence, source IDs, allowed roots, live file globs, exclusions and bounded supported parsing options.
- [ ] Represent journal-only applications explicitly; do not create redundant file readers merely to claim a plugin exists.
- [ ] Represent the primary app and its standard community-script-installed dependencies in one manifest. Use the display name `Proxmox community script [app]`; declare source requirements/optional layout variants from verified installations, not a second manual dependency selection.
- [ ] Support packaged built-in manifests and a settings-selected local plugin directory. Verify package installation includes built-in data files.
- [ ] Validate the catalogue once per run; reject unsupported versions, duplicate IDs, conflicting aliases, unsafe paths and invalid source definitions before guest mutations.
- [ ] Treat plugin files as declarative data. No Python imports, shell commands, application upgrades, arbitrary HCL injection or runtime downloading/executing plugins.
- [ ] Define safe path/variable substitution explicitly, if needed. Do not accept unrestricted template evaluation.
- [ ] Keep component names stable and collision-free across plugins and shared sources; preserve Alloy positions when selection is unchanged.
- [ ] Compute one effective desired configuration/hash from the base configuration, resolved plugins and overrides.

### 2. Detect running applications and select plugins automatically

- [ ] Reuse the existing read-only introspection facts and community-script parser in all relevant logging/maintenance entrypoints.
- [ ] Resolve a plugin by its explicit community-script ID/alias, not fuzzy app names, hostname guesses or executable update-script evaluation.
- [ ] Verify the selected plugin's running-app and layout evidence before applying access/configuration changes. Report stopped/missing/incompatible layouts; never start an app or container to obtain a match.
- [ ] If community-script identity is absent or unresolved, produce an explicit finding and require a settings override. Do not implement automatic service/process/path or hostname fallback.
- [ ] Resolve the whole standard app/dependency bundle through its primary script identity. Validate source ownership/layout deterministically; reject conflicting sources rather than inferring an extra app from an unrelated service.
- [ ] Keep selection usable without GitHub access when local identity and the installed catalogue suffice. Optional upstream metadata enrichment must stay manager-side and must not be required for ordinary selection.
- [ ] Report detection evidence, selected plugin/source IDs and the reason for selection, rejection or ambiguity without printing script contents, credentials or log bodies.
- [ ] Re-evaluate identity/layout after drift; do not retain a stale plugin merely because it matched on a previous run.

### 3. Add settings-file overrides

The settings below are planned capabilities, not currently supported configuration keys. Finalize their names/types during implementation.

- [ ] Add an automatic-selection switch and local catalogue-directory setting adjacent to the existing Alloy settings.
- [ ] Add a typed per-guest override map in `vars.yml` / `--vars-file`, keyed by cluster/node/guest identity. Reject ambiguous bare guest IDs across clusters rather than guessing.
- [ ] Support explicit modes: `auto`, `replace`, `extend` and `disabled`. An absent override means auto; an explicit empty replacement must not fall back to auto.
- [ ] Allow overrides to force a known plugin, add a known plugin, disable a plugin/source, and replace a declared source's log locations for a nonstandard installation.
- [ ] Define precedence: existing fleet/Alloy exclusions first; explicit guest override next; automatic selection next; plugin defaults last. An override cannot bypass exclusions or safety checks.
- [ ] Validate selected IDs, allowed roots, location substitutions and conflicting sources before deployment. A settings override must not authorize arbitrary reads, broad permission changes or deletion.
- [ ] Preserve existing settings and secrets when editing configuration. Update `vars.yml.example` and README with credential-free examples after behavior is implemented and verified.

### 4. Use one shared collection/reconciliation implementation

- [ ] Extend the existing source renderer to consume resolved plugin data rather than one branch per application.
- [ ] Use the same selection and effective hash in normal LXC runs, logging-only preparation and housekeeping-only runs. Later reconciliation must not restore a journal-only base over selected file sources.
- [ ] Keep `--scan` read-only: it may report identity/selection/coverage findings, but must not repair permissions, deploy configuration, install packages or restart services.
- [ ] Preserve the existing journal reader, endpoint, storage path and unrelated service overrides. Do not reset positions or create a second journal reader.
- [ ] Reuse scoped access checks as the Alloy user. If repair is authorized, limit ACLs to validated source roots and required parents/default inheritance; never run Alloy as root or make files world-readable.
- [ ] Missing ACL/runtime prerequisites block affected sources with useful diagnostics; recurring maintenance must not install packages automatically.
- [ ] Preserve bounded labels and packed filename metadata. Do not index request/user/IP values, UPIDs, filenames or timestamps.
- [ ] Observe post-operation configuration, read access and service health. Configuration command success is not proof that logs were delivered.
- [ ] Verify natural or approved disposable log records from each enabled source in Loki; distinguish a quiet app, a missing file source, a permission failure and a network/delivery outage.
- [ ] Handle supported rotation, new files/subdirectories, multiline/oversized records and restart/resume through the shared implementation, not app-specific forwarding wrappers.

### 5. Preserve archive and retention safety

- [ ] Make new-app plugins live collection-only: no new-app historical file importer, file-retention tightening, native file-rotation change or file-pruning operation. Preserve the existing fleet journald policy and NPM/PBS maintenance behavior unchanged.
- [ ] Preserve NPM/PBS batch acknowledgement, complete frozen-prefix coverage and full current-file coverage as separate states.
- [ ] Keep existing NPM/PBS bounded capture/import/checkpoint behavior and original bytes/provenance; retain unacknowledged input on errors or crashes.
- [ ] Preserve the existing NPM/PBS central deletion authorization: current identity/digest, age, writer exclusions, complete archive coverage, matching verified configuration/delivery and filesystem permissions must all pass.
- [ ] Keep final filesystem/quarantine checks as defense in depth. A plugin or override cannot turn a collection path/glob into a deletion entitlement.
- [ ] Make existing NPM/PBS manifests preserve their exact current/live/archive/control exclusions and writer handling. Do not replay fully acknowledged history during migration.
- [ ] Preserve finite-buffer/outage limitations and at-least-once archive semantics; do not claim lossless native copytruncate, exactly-once delivery or infinite outage retention.
- [ ] Do not change central Loki retention, resize disks, prune PBS datastores, or introduce application/database/dependency cleanup.

### 6. Author and validate the initial catalogue

- [ ] Record and complete the supported-app/source matrix for the audited installed managed-LXC applications, including each script's standard installed dependencies.
- [ ] Migrate NPM and PBS into plugin files with behavioral equivalence and no legacy parallel mapping/shim.
- [ ] Cover verified gaps from the audit: Plex; Sonarr/Radarr/Prowlarr; qBittorrent; Seerr; Tautulli; APT cache; CouchDB/Obsidian LiveSync; Synapse; Hermes; UniFi OS; PDM; manager CLI logs; and the standard community-script-installed dependency/error sources. Non-community installations require overrides, and excluded guests remain excluded.
- [ ] Verify community-script IDs and aliases from actual supported installations; do not treat the above display names as already validated script IDs.
- [ ] Include Technitium manifests for its verified log layout. Separately verify network reachability: selecting a plugin will not fix the observed VLAN-to-Loki connectivity failure.
- [ ] Classify existing journal-only apps accurately; avoid collecting duplicate file mirrors without an explicit reason.
- [ ] Document unsupported container-in-container/private runtime layouts as coverage findings until their actual sources and access are verified.
- [ ] Preserve the current managed-LXC scope and exclusions. VM/node/manual-appliance expansion requires a separate explicit scope decision; do not include them silently.

### 7. Prove behavior before rollout

- [ ] Add deterministic consumer-visible regressions for legacy/current community wrappers, unresolved names, aliases, no metadata, contradictory evidence, stopped apps and ambiguous matches.
- [ ] Cover auto/replace/extend/disabled precedence, empty replacement, custom locations, invalid plugin IDs, duplicate sources, cluster collisions and excluded guests.
- [ ] Cover missing optional files versus failed enumeration/read access; no-profile findings must not masquerade as successful app-log coverage.
- [ ] Exercise ordinary update, logging-only and housekeeping-only paths to prove selection cannot be overwritten later and scans do not mutate anything.
- [ ] Exercise the real renderer/access/collector behavior against disposable files: append, rotation, fresh inherited permissions, multiline/oversized data, restart, configuration drift and outage recovery.
- [ ] Preserve the existing NPM/PBS crash/resume, archive lineage and deletion regressions during migration. Prove new-app sources cannot authorize archive import or deletion. Do not test wiring, command-string copies or mock echoes instead of behavior.
- [ ] Acceptance: add one plugin file and a normally installed matching guest; without application-specific code changes or per-guest settings, the correct sources are selected and their actual records arrive in Loki.
- [ ] Acceptance: a nonstandard installation can select/adjust that same plugin through the existing settings file; the effective configuration and delivered records reflect the override.
- [ ] Acceptance: unknown or conflicting detection produces a precise unsupported/ambiguous finding, no guessed source changes, and no deletion.
- [ ] Acceptance: a community script that installs an app plus a database automatically selects one correctly named plugin and delivers both verified live source sets, without an additional dependency selection.
- [ ] Acceptance: an app without recognized community-script identity does not auto-match by its hostname or running service; an explicit settings override is required.
- [ ] Run focused regressions, the affected existing suite and relevant typing/lint/security/primitive checks after implementation; record actual results, not planned checks as successes.

### 8. Documentation and controlled deployment

- [ ] Document the plugin schema, authoring/selection procedure, override precedence, supported catalogue, coverage limits and troubleshooting in existing repository documentation after runtime proof.
- [ ] Record collection/delivery findings without turning routine unchanged runs into notification noise or changing notification policy implicitly.
- [ ] Commit/publish implementation and deploy CT121 through its own `bash install.sh --update`, under the existing fleet lock; preserve real inventory/settings/configuration. Do not use ad hoc source transfer as the deployment mechanism.
- [ ] Stage dry-run/read-only selection and a bounded pilot before fleet application. Respect existing capacity/resource gates and shared import limits.
- [ ] Leave missing network/access/capacity prerequisites explicit; do not widen firewall access or permissions merely to obtain a green report.
- [ ] Never run fleet OS/app upgrades, snapshots, reboots or stopped-container starts as part of this logging rollout.

## Completion criteria

The implementation is complete only when the confirmed D1-D4 scope is proven end-to-end: community-script-only automatic selection, the whole standard app/dependency bundle, safe running-state/layout checks, deterministic settings overrides, file-only extensibility, actual live Loki delivery for the audited managed-LXC catalogue, and unchanged NPM/PBS archive/deletion guarantees. New-app historical import and retention/deletion are out of scope. A manifest catalogue alone, passing unit tests alone or an active Alloy process alone does not satisfy this checklist.
