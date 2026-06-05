# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The Ansible→Python migration is **complete**: `fleet-update` (→ `driver.run_fleet()`) is
the only entrypoint. There is no `fleet-update.yml` playbook and no `--use-*-flow` flags —
Ansible runs only as the execution primitives in `ansible/primitives/*.yml`.

`fleet-update.py` (repo root) is the recommended human-facing interface — it auto-bootstraps
into `.venv` and exposes friendly flags. The `fleet-update` console command (registered by pip)
is the programmatic / cron interface.

```bash
# Fleet-wide dry-run (no changes), forces a Discord/ntfy notification
./fleet-update.py --dry-run --force-notify

# Full run with forced notification
./fleet-update.py --force-notify

# Bypass maintenance windows
./fleet-update.py --force-window

# Pass raw extra vars (same as the old -e interface, still supported)
./fleet-update.py -e custom_allow_reboot=false

# Using the console command directly (requires active venv or full venv path)
fleet-update --check -e force_notify=true

# Install required collections (for the primitives + molecule)
ansible-galaxy collection install community.proxmox community.general

# Python unit tests (no Ansible or PVE needed)
pip install -e '.[dev]'        # includes types-PyYAML, mypy, pytest, pydantic
pytest tests/unit/ -v
pytest tests/unit/test_briefing.py -v          # single file
pytest tests/unit/ -k "run_fleet"              # single test

# Python type checking
python -m mypy proxmox_fleet/

# Static analysis
yamllint .
ansible-lint ansible/primitives/

# Molecule scenarios (drive the Python flows via stub pct/vzdump scripts, against localhost)
cd roles/lxc_update && molecule test -s lxc_update_normal      # normal | rollback | snapfail
cd roles/lxc_update && molecule converge -s lxc_update_normal  # converge only, no verify/destroy
cd roles/custom_update && molecule test -s custom_update_normal  # custom flow via RunnerExecutor
```

`hosts.ini` and `vars.yml` are gitignored (contain secrets/IPs). Copy from `.example` files to run locally.

## Manager Setup (first time on Debian manager LXC)

Debian's system Python is externally managed (PEP 668) — pip cannot install system-wide. Use a virtualenv:

```bash
apt install python3.13-venv          # or python3.X-venv matching your Python version
python3 -m venv .venv
source .venv/bin/activate
pip install -e .                     # installs proxmox_fleet + fleet-update CLI
ansible-galaxy collection install community.proxmox community.general
```

Activate the venv at the start of each shell session: `source .venv/bin/activate`

## File Map

```
fleet-update.py                         # Runnable wrapper — ./fleet-update.py [--dry-run|--force-notify|--verbose|--force-window|-e K=V]; auto-bootstraps .venv
ansible.cfg                             # forks=20, pipelining=true, inventory=./hosts.ini
vars.yml / vars.yml.example             # Secrets + behaviour flags (gitignored; copy from .example)
hosts.ini / hosts.ini.example           # Inventory (gitignored; copy from .example)
.ansible-lint                           # profile: moderate; targets ansible/primitives/ (name[casing] demoted to warning)
.yamllint.yml                           # extends: default; line-length warning at 160
.github/workflows/ci.yml                # yamllint, ansible-lint (primitives), syntax-check (primitives), unit-tests (Python 3.10/3.11/3.12 matrix + coverage), mypy, ruff (incl. fleet-update.py), lint-wrapper, bandit security scan, + molecule matrices (lxc, custom)
pyproject.toml                          # package config; fleet-update entrypoint; [dev] extras include types-PyYAML for mypy
proxmox_fleet/
  models/
    config.py                           # CustomConfig pydantic schema (custom_update config files)
    state.py                            # FleetState + per-type records; dump_for_ansible() writes fleet_* JSON
    settings.py                         # GlobalSettings pydantic model for vars.yml; load() returns defaults on missing file; includes LXC, VM, remote, node/manager + PVE API fields + configurable timeouts/retries (apt_proxy_check_timeout, node_reboot_port_wait_timeout, snapshot_retries/delay, notifier_retries, deadmans_retries, node_apt_retries/delay)
  flows/
    _pkg.py                             # shared pkg-manager helpers: detect_pkg_mgr(), upgrade_cmd() (LC_ALL=C-pinned), kuma_healthy()
    custom.py                           # run_custom_update() — the full custom flow (detect→backup→update→health→report)
    lxc.py                              # run_lxc_update() — the full LXC flow (introspect→detect→backup→update→health→report); try/except/finally = block/rescue/always
    vm.py                               # run_vm_update() — the full VM flow; two-executor pattern: executor=VM SSH, node_executor=Proxmox node SSH (for qm rollback/status)
    remote.py                           # run_remote_update() — the full remote host flow (pre_update_cmd→detect_pkg_mgr→upgrade→reboot→health→report); no snapshot/always block
    node.py                             # run_node_update() + run_manager_update() — Phase 2+3; apt w/ 5 retries, robust reboot check, manager-host skip, proxy wait
  deps.py                               # validate_depends_order() + dependency_failed() — ports of Phase-0a Jinja logic
  driver.py                             # run_fleet() end-to-end orchestrator (pre-flight → all phases → _merge_state → notify) + per-phase run_*_phase() helpers
  executor.py                           # Executor protocol + RunnerExecutor; snapshot() for proxmox_snap primitive; snapshot_with_retry() shared retry helper (LXC + VM); 8 primitive-backed methods: introspect(), vzdump(), lxc_os_update(), lxc_app_update(), post_update(), pct_rollback(), pct_start(), pct_stop()
  http.py                               # Manager-local HTTP: get_json, poll_until, request, post_json
  inventory.py                          # _iter_section() + load_custom_hosts/proxmox_nodes/proxmox_vms/remote_hosts() — manual hosts.ini parsers (+ host_vars merge); maintenance_window raw dicts parsed into typed MaintenanceWindow at load time
  lxc_parse.py                          # parse_pct_config(), parse_pct_status(), parse_ct_script() — regex helpers for lxc flow
  orchestration.py                      # run_serial(), run_concurrent() — Python equivalents of serial/forks
  runner.py                             # invoke_primitive() — thin ansible-runner wrapper; passes project_dir=os.getcwd()
  steps.py                              # run_steps() — executes update_steps with per-step timeout + when gate
  status.py                             # all status decision trees: custom_status(), lxc_*(), vm_status(), vm_rescue_status(), remote_status(), node_status(), manager_status()
  changes.py                            # change detection helpers; lxc_os_changed(), dpkg_hash_differs(), lxc_os_pkg_count()
  window.py                             # in_window() — port of tasks/check-window.yml using stdlib zoneinfo; accepts MaintenanceWindow or plain dict; tz-aware datetimes are converted via astimezone()
  briefing.py                           # render_briefing() byte-parity port of discord_briefing.j2 + prepare_body/title/color/should_notify
  history.py                            # build_run_summary() + write_history() — port of persist-history.yml; records rendered briefing body; microsecond-precision timestamps prevent same-second collision
  notifiers.py                          # resolve_notifiers() + dispatch() (discord/ntfy) + ping_deadmans() — port of notify.yml + Phase-4 shim
  cli.py                                # fleet-update CLI — parses --check / -e / --inventory / --vars-file, propagates fleet_dry_run/lxc_verbose/force_notify/force_window into settings, then calls driver.run_fleet()
config_templates/
  custom_system.yml.example             # Fully-commented schema template — copy to configs/<name>.yml
configs/
  .gitkeep                              # Real configs/*.yml are gitignored; commit *.yml.example worked examples only
  gitea.yml.example                     # Worked example: Gitea binary update
ansible/
  primitives/
    run_shell.yml                       # Single-action primitive: run a shell command, return rc/stdout/stderr via set_stats
    reboot_host.yml                     # Single-action primitive: reboot and wait
    discover_lxcs.yml                   # Tag-filter LXC discovery shell on a Proxmox node
    pct_config.yml / pct_status.yml     # Read container config / status
    pct_start.yml / pct_stop.yml        # Start / stop a container
    pct_pull.yml                        # Copy file from container to node
    snapshot.yml                        # community.proxmox.proxmox_snap create/delete (runs on localhost; snap_state=present|absent)
    rollback.yml                        # pct rollback BEFORE_UPDATE_AUTO
    vzdump.yml                          # vzdump backup
    lxc_os_update.yml                   # OS upgrade inside container (pct exec)
    lxc_app_update.yml                  # /usr/bin/update with /tmp/.nc/clear trick + resource scaling
    lxc_introspect.yml                  # Batched read: pct config + pct status + pct pull /usr/bin/update + cat script content → 1 subprocess; returns config_stdout, status_stdout, pull_rc, script_stdout via set_stats
    lxc_post_update.yml                 # Batched read: dpkg/apk hash after update + version file → 1 subprocess; returns dpkg_hash_after, version_after via set_stats
tests/
  requirements.txt                      # pytest, pyyaml — all that's needed for the plain-Python unit tests
  unit/                                 # pytest tests; no Ansible or PVE required
    data/briefing_golden.json           # Captured discord_briefing.j2 bytes — locks render_briefing() parity
roles/                                  # Molecule scenarios ONLY — each drives a Python flow via mol_run_flow.py
  lxc_update/molecule/
    mol_run_flow.py                     # Converge helper: builds MolLxcExecutor (snapshot stubbed), calls run_lxc_update(), dumps JSON
    lxc_update_normal/                  # Running container, vzdump backup, no update script → NO SCRIPT
    lxc_update_rollback/                # Kuma unreachable, no snapshot → FAILED (no rollback)
    lxc_update_snapfail/                # snapshot() returns changed=False → warning, update continues
  custom_update/molecule/
    mol_run_flow.py                     # Converge helper: loads config, builds RunnerExecutor, calls run_custom_update(), dumps JSON
    custom_update_normal/               # Version changes 1.0 → 1.1; "Updated: 1.0 → 1.1" recorded
    custom_update_noop/                 # Version unchanged; record suppressed (idle)
    custom_update_rescue/               # Update step exits 1; rollback_command runs; fleet_failed=True
    custom_update_dry_run/              # custom_dry_run=true; detect only; "dry-run: X → Y" recorded
    custom_update_uptodate/             # update_only_if_outdated=true; version matches; update steps skipped
    custom_update_per_step/             # per-step when: gate referencing steps.NAME stdout
```

## Architecture

### Phase order in `driver.run_fleet()`

`fleet-update` → `driver.run_fleet()` runs these in order, threading one in-memory
`FleetState` through (`_merge_state()` folds each phase's returned state in — the
in-Python replacement for the old "Merge Python state" plays):

| Phase | Target | Purpose |
|---|---|---|
| Pre-Flight | manager | `http.wait_for_port()` on the apt-cacher-ng proxy; `SystemExit(1)` if unreachable |
| Phase 0 | `remote_hosts` | `run_remote_phase()` → `flows/remote.py` per host (concurrent, `remote_forks`) |
| Phase 0a/0b | `custom_hosts` | `run_custom_phase()` — validates `depends_on` order (fail loud), then `flows/custom.py` serially |
| Phase 1 | `proxmox_nodes` | `run_lxc_phase()` — tag-filtered discovery + `flows/lxc.py` per container (concurrent, `lxc_forks`) |
| Phase 1b | `proxmox_vms` | `run_vm_phase()` — pvesh HA location discovery + `flows/vm.py` per VM (concurrent, `vm_forks`) |
| Phase 2 | `proxmox_nodes` | `run_node_phase()` — serial node OS update + reboot (abort-on-first-failure) |
| Phase 3 | manager | manager self-update (runs unconditionally, even after a node failure) |
| Phase 4 | manager | `run_notify_phase()` — render briefing once → dispatch notifiers → write history → dead-man ping |

`run_fleet()` returns exit code 1 if any phase recorded a failure, else 0. Each phase's
dry-run flag is `check or fleet_dry_run or <phase>_dry_run`; `fleet_dry_run` also forces a
notification (`briefing.should_notify`).

### State accumulation pattern

Each `flows/*` call returns a per-host outcome; each `run_*_phase()` folds those into a
`FleetState` (the single `_fold_outcome(state, outcome, bucket)` helper); `run_fleet()` then `_merge_state()`s the
per-phase states into one. The state lists are `lxc`, `vm`, `remote`, `node`, `custom`,
with `changed`/`failed` flags and `errors`/`warnings` logs (`models/state.py`). The
`run_*_phase()` helpers can still `dump_for_ansible()` a phase's state to JSON when given a
`state_output_path` (used by tooling / molecule); `run_fleet()` passes `None` and merges
in-memory.

### Architecture notes

The primitive consolidation is complete. All primitives are now wired:
- **`flows/lxc.py`** uses `executor.introspect()`, `executor.lxc_os_update()`, `executor.lxc_app_update()`, `executor.post_update()`, `executor.vzdump()`, `executor.pct_rollback()`, `executor.pct_start()`, `executor.pct_stop()` — no more inline `run_shell` strings for those operations.
- **Two batched read primitives** (`lxc_introspect.yml`, `lxc_post_update.yml`) replace ~10 individual `run_shell` calls per container, reducing subprocess spawns from ~15 to ~7–8.
- The remaining `run_shell` calls in the LXC flow are: `ver_before` (script name unknown until after detect), `dpkg_before` (must run after OS update, before app update), and the post-start status re-check (conditional).
- `discover_lxcs` is still called via `run_shell` with the discovery command — it is a thin shell loop, not a multi-read candidate.

See `docs/migration-roadmap.md` → "Post-migration backlog" for full history.

### Phase 4 subsystems

`driver.run_notify_phase()` consumes the merged `FleetState` and drives all three:

- **Notifiers** (`notifiers.py` + `briefing.py`): `briefing.prepare_body()` renders the body **once**; `notifiers.dispatch()` fans it out to a `notifiers` list (types `discord`, `ntfy`). Back-compat (`resolve_notifiers()`): if `settings.notifiers` is unset (`None`) but `discord_webhook` is set, a single Discord notifier is synthesized; an explicit `[]` means "no notifiers". ntfy reuses the same body verbatim; only the transport envelope differs.
- **Run history** (`history.py`): `write_history()` writes `run-<UTC-ts>.json` + `latest.json` to `fleet_history_dir`, pruned to `fleet_history_keep`, and records the rendered briefing body. Gated on `fleet_history_enabled`.
- **Dead-man's-switch** (`notifiers.ping_deadmans()`): pings `fleet_deadmans_url` (`/fail` on failure) so its absence alerts when the orchestrator stops running.

### Cross-cutting subsystems

- **Snapshot-only rollback + warnings**: the LXC and VM flows roll back via snapshot (`pct/qm rollback BEFORE_UPDATE_AUTO`) only when the snapshot was actually taken (`snap_taken`, i.e. the snapshot primitive returned `changed=True`). A failed snapshot records a non-fatal warning and continues; rescue app/status string is `FAILED (NO SNAPSHOT)` vs `FAILED + ROLLED BACK` vs `FAILED` (see `status.lxc_rescue_app_status` / `vm_rescue_status`). `lxc_backup_strategy: both` / `vm_backup_strategy: both` take a simultaneous vzdump (never used for restore).
- **Fleet-wide dry-run**: `-e fleet_dry_run=true` (or `--check`, or a per-phase `<phase>_dry_run`) puts every flow in simulate mode — Python builds a simulate command (`apt-get -s` / `dnf --assumeno` / `apk -s`) and reports `WOULD UPDATE`/`OK`. `fleet_dry_run` also forces a notification.
- **Maintenance windows** (`window.in_window`): inventory hosts (remote/vm/custom) with a `maintenance_window` key in `host_vars` are silently skipped outside the window; `force_window=true` (or `-e force_window=true`) bypasses. The raw dict is parsed into a typed `MaintenanceWindow` by `inventory.py` at load time — invalid keys fail loud. `window.in_window` accepts `MaintenanceWindow` or plain `dict`, and converts tz-aware `now` values via `astimezone()` before comparing. Evaluated per-host in the `run_*_phase()` helpers before the flow runs.

### Flow structure (`flows/lxc.py`)

`run_lxc_update()` is a single function whose `try/except/finally` reproduces the old
`block/rescue/always`:
- **Introspect runs outside the `try`** (fail loud if `pct config` fails): parse name/os_type/template via `lxc_parse`, read status, start the container if it was stopped.
- **`try` body:** detect (pull `/usr/bin/update`, parse ct script, fetch GitHub ct script) → dry-check (version compare only) → backup (vzdump and/or snapshot) → update → health check → report.
- **`except` (rescue):** capture the failing step → `pct rollback BEFORE_UPDATE_AUTO` only if `snap_taken` → poll until running → set `rollback_done` → record `FAILED + ROLLED BACK` / `FAILED (NO SNAPSHOT)` / `FAILED`.
- **`finally` (always):** delete the snapshot (only if `snap_taken`); stop the container if `was_stopped` **and** `not rollback_done` (a rollback restores the container, so don't stop it again).

`run_vm_update()` and `run_remote_update()` follow the same try/except/finally shape. `run_remote_update()` has no `finally` (no snapshots to clean up). Per-host outcomes are folded into the `FleetState` by `driver._fold_outcome()`.

### `custom_update` flow (`flows/custom.py` + `driver.run_custom_phase`)

- **Config load is outside the flow** (fail loud on bad config): `driver._load_config()` reads `configs/<name>.yml` (`settings.configs_dir`), deep-merges `custom_overrides` from host_vars, and validates via the `CustomConfig` pydantic model.
- **Flow body:** detect (`version_command` + latest-version lookup) → backup (if `backup_command`) → `steps.run_steps()` → change detection → reboot → health check → report.
- **`except` (rescue):** run `rollback_command` (errors ignored) → record `FAILED`. No `finally` (v1 — no snapshot).

**`custom_config` inventory var**: each host in `[custom_hosts]` must have `custom_config=<name>` pointing to `configs/<name>.yml`. Optionally set `custom_overrides: {...}` in host_vars to deep-merge over the config file.

**Config files**: `configs/*.yml` is gitignored. Commit `configs/*.yml.example` as templates. Real configs live only on the manager. See `config_templates/custom_system.yml.example` for the full schema.

**`custom_status()` decision tree** (`status.py`):
- `dry_run=true` → `dry-run: <before> → <latest>`
- `update_only_if_outdated` and already current → `OK (up to date)`
- `changed_when.type == always` → `Updated [+ Rebooted]`
- `changed_when.type == command`, exit 0 → `Updated [+ Rebooted]`; exit non-0 → `OK`
- `changed_when.type == version` (default), before/after differ → `Updated: X → Y [+ Rebooted]`; same → `OK`
- No version data → `Updated [+ Rebooted]` (fallback)

### LXC update sequence and change detection (`flows/lxc.py`)

Step order matters for correct attribution:
1. Read `ver_before` (`cat ~/.<script>` via the OS-appropriate shell)
2. OS update (`_os_update_cmd`, `LC_ALL=C` apt/apk/dnf) — **first** so OS packages credit the OS line, not the app line
3. Read `dpkg_before` — `LC_ALL=C dpkg-query -W | sort | md5sum` (`_dpkg_hash_cmd`)
4. Scale up resources if `needs_resource_scale`
5. App update (`/usr/bin/update` with the `/tmp/.nc/clear` trick + `PHS_SILENT` when `lxc_unattended`)
6. Read `dpkg_after` — same query; equal to `dpkg_before` ⇒ nothing installed
7. Read `ver_after`
8. Scale down → reboot check

**`lxc_app_status()` decision tree** (`status.py`, priority order): version files differ → `Updated: X → Y`; both non-empty & equal → `OK`; dpkg hash differs → `UPDATED`; dpkg hash matches → `OK`; no hash data (non-apt OS) → `UPDATED` (fallback); `app_changed` false → `OK`.

**Why dpkg hash instead of stdout parsing:** `PHS_SILENT=1` (set by `lxc_unattended: true`) routes apt's stdout to `/dev/null` inside community scripts, so `0 upgraded, 0 newly installed` never appears in the app-update stdout. The dpkg hash is a direct query, immune to output suppression. (`LC_ALL=C` keeps both the hash sort order and the OS-update keyword parsing locale-independent.)

### Detect flow and version-file convention (`flows/lxc.py`)

The detect section, in sequence:
1. `pct pull {lxc_id} /usr/bin/update /tmp/ansible_update_{lxc_id}` — extracts the community-scripts update script. `pull` failing ⇒ `lxc_no_update_script=True`.
2. `cat /tmp/ansible_update_{lxc_id}` **on the node** to read the file content, then `lxc_parse.script_name_from_update(content)` **in Python** on the manager extracts the ct script name (e.g. `sonarr`). This keeps extract logic in Python and avoids a dependency on Perl-regex `grep -P` on the node.
3. Fetch `https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/{name}.sh` via `http.request` **on the manager** (PVE nodes may lack outbound HTTPS) — `lxc_parse.parse_ct_script()` reads `var_cpu`/`var_ram` (build resources) and `pct set $CTID -cores/-memory` (run resources).

**Version files**: community-scripts store the installed app version at `~/.{scriptname}` inside the container (e.g. `~/.sonarr` → `4.0.17.2952`). `_read_version()` reads it before/after the update; the dry-check path reads it and compares against the latest GitHub release tag.

**Resource scaling**: when build CPU > run CPU, `needs_resource_scale` is true — the flow scales the container up before the update script and back down after.

### Uptime Kuma integration

All flows poll `{kuma_url}/api/status-page/heartbeat/{kuma_slug}` via `http.poll_until` with the `_pkg.kuma_healthy` predicate and wait for the mapped monitor to show status `1`. It only fires when the host id is in `lxc_kuma_map` (or `vm_kuma_map`/`remote_kuma_map`) **and** something actually changed. A timeout raises and triggers the flow's rescue (and snapshot rollback if a snapshot was taken). Retries/delay come from `kuma_health_check_retries` (default 5) / `kuma_health_check_delay` (default 30s). Kuma map keys are coerced to `str` at settings load, so integer vmids in `vars.yml` work. Credentials/maps are in `vars.yml`.

### Key non-obvious details

- **Tag-based LXC discovery** (Phase 1): only LXCs tagged `community-script` or `proxmox-helper-scripts` in PVE are processed. Untagged containers are never touched. Tags must be set in PVE UI → Container → Options → Tags.
- **GitHub HTTPS runs on the manager**: ct-script and release lookups go through `http.request`/`http.get_json` on the manager (`urllib`), never on the PVE node — nodes may lack outbound HTTPS.
- **`pve_node` inventory var** is only a fallback hint: `driver.run_vm_phase()` discovers the live VM→node map via `pvesh get /cluster/resources` so HA migrations are handled; `pve_node` is used only when discovery is unavailable.
- **PBS is transparent**: setting `lxc_backup_storage` to a PBS storage name routes `vzdump` to PBS automatically — no special code path.
- **`[proxmox_vms]`, `[remote_hosts]`, and `[custom_hosts]` must exist** in `hosts.ini` even if empty (just the group header). Ansible raises "no hosts matched" otherwise.
- **`custom_config` is a required per-host inventory var** for `[custom_hosts]` — set it in hosts.ini or host_vars. The role will fail loudly if it's missing (include_vars will not find the file).
- **Node reboot is skipped** when `manager_lxc_id` runs on that node — rebooting the node would kill the manager mid-run.
- **`lxc_backup_strategy`** is a four-value enum: `snapshot | vzdump | both | none` — not boolean flags.
- **`/tmp/.nc/clear` trick** (`flows/lxc.py` app-update command): overrides the `clear` shell command with a no-op so community-scripts update output isn't wiped from the stdout capture.
- **Snapshot name is fixed**: always `BEFORE_UPDATE_AUTO`. The `finally`-block cleanup hardcodes this name — changing it where the snapshot is created without also changing the cleanup/rollback would leave orphaned snapshots.
- **Idle containers are suppressed** (`status.lxc_should_report`): a record is appended only when something changed or failed. Fully up-to-date containers produce no Discord entry.
- **`lxc_continue_on_error`**: Phase 1 runs containers concurrently per node (`run_concurrent`, `lxc_forks`); one failing container becomes a FAILED record and never aborts the others.
- **Kuma failure triggers rescue**: a health-check timeout raises (and rolls back if a snapshot was taken). Retries/delay come from `kuma_health_check_retries` (default 5) / `kuma_health_check_delay` (default 30s), which molecule overrides to 1/0 for fast tests.
- **Rollback only fires when `snap_taken`** — if `lxc_backup_strategy: none` or the snapshot primitive returned `changed=False`, the rescue skips rollback and just records `FAILED` (the latter records `FAILED (NO SNAPSHOT)`).
- **Auto-rollback covers snapshots only, never vzdump**: a `vzdump`-only strategy produces a backup archive but no snapshot, so a failed update is **not** automatically restored — only `FAILED` is recorded. Restore vzdump archives manually. Use `snapshot` or `both` for automatic rollback.
- **`custom_update` config dir**: `driver._load_config()` reads `<configs_dir>/<name>.yml` where `configs_dir` defaults to `configs` (override via `settings.configs_dir`). Molecule points it at a temp dir.
- **Custom-config commands are opaque strings**: `CustomConfig` validates them as literals and never renders them. `steps.run_steps()` resolves only `{{ steps.NAME }}` refs in Python at run time (the eager-templating fix); every other `{{ }}` is left for the shell/Ansible. `register` stashes a step's stdout for a later step's `when:`.
- **`invoke_primitive` requires CWD = project root**: `ansible_runner.run()` is called with `project_dir=os.getcwd()`. Without an explicit `project_dir`, ansible-runner creates a fresh `tempfile.mkdtemp()` as `private_data_dir` and looks for the playbook at `<tempdir>/project/ansible/primitives/<name>.yml` — which never exists. The production CLI runs from the project directory; `mol_run_flow.py` calls `os.chdir(_project_root)` at module load to guarantee this for molecule runs.
- **State merge is in-memory** (`driver._merge_state`): `run_fleet()` threads one `FleetState` through every phase and OR-joins `changed`/`failed`, concatenating record lists. The per-phase `dump_for_ansible()` JSON (fleet_*-keyed, reverse of `from_raw()`) is **only** for tooling and the molecule `verify.yml` (`mol_run_flow.py` writes it, `verify.yml` `include_vars` it); `run_fleet()` passes `state_output_path=None`.
- **Inventory parser must not use `configparser`**: `configparser` splits on the first `=` and mis-parses Ansible host lines of the form `hostname key=val key=val …` (it produces key=`hostname key` and value=`val key=val …`). `proxmox_fleet/inventory.py` uses a manual regex parser (`_iter_section`) instead, and merges `host_vars/<host>.yml` for every group (including `proxmox_nodes`, so a node's `ansible_host` IP can live in host_vars).
- **Package/locale commands pin `LC_ALL=C`** (`flows/_pkg.upgrade_cmd` + `flows/lxc._os_update_cmd`/`_dpkg_hash_cmd`): change detection greps English apt/apk/dnf summary lines, so the locale must be forced or a non-English host reports "changed" on every run. `window.in_window` likewise uses a fixed weekday list, not locale-sensitive `strftime('%a')`.
- **Shared pkg helpers** live in `flows/_pkg.py` (`detect_pkg_mgr`, `upgrade_cmd`, `kuma_healthy`) — used by the vm/remote/lxc/custom/node flows. Don't re-copy them into a flow.
- **Alpine uses `ash`**: `flows/lxc._read_version` (and the OS-update command) pick `ash` for `ostype: alpine`, else `bash` — Alpine containers often have no bash.
- **Briefing byte-parity — no trailing newline**: `render_briefing()` must NOT emit a trailing `\n` (the golden fixture in `tests/unit/data/briefing_golden.json`, captured from the retired `discord_briefing.j2`, has none). `prepare_body()` applies `.strip()` + a port of Jinja's `truncate(4000, killwords=False, end='\n...', leeway=5)` — match the algorithm exactly (return unchanged when `len <= 4005`).
- **`settings.notifiers` is `Optional[...]` defaulting to `None`**: this preserves the Ansible `notifiers is defined` distinction — an explicit `notifiers: []` in `vars.yml` means "no notifiers" and must NOT fall back to synthesizing one from `discord_webhook`; only an *unset* (`None`) value triggers the back-compat shim.
- **`run_shell.yml` has `check_mode: false`** on the shell task — the command **always executes** regardless of Ansible check mode. Python controls dry-run by passing either a simulate command (`apt-get -s`) or a real command; Ansible's `--check` flag is bypassed at the shell level. `reboot_host.yml` has the same `check_mode: false` for consistency; the node flow additionally guards the reboot call with `not dry_run` in Python so it never fires during dry-run regardless.
- **`run_node_update` retry uses injectable `_sleep`**: `orchestration.retry(apt_fn, retries=5, delay=30.0, sleep=_sleep)`. Callers pass `_sleep=lambda s: None` in tests to avoid 150 s of wait. `run_node_phase()` in the driver doesn't pass `_sleep`, so real runs use `time.sleep`. Tests that call via `run_node_phase()` must monkeypatch `time.sleep` to keep fast. `steps.run_steps()` has the same injectable `sleep` for honouring a step's `delay` between failed retries.
- **`executor.snapshot(vmid, *, snap_state, api_host, ...)`**: invokes `snapshot.yml` (which runs `community.proxmox.proxmox_snap` on localhost, ignoring the executor's host binding). `api_host` must be the node's `ansible_host` IP — not the inventory name. `vmid` (not `lxc_id`) because the same primitive handles both LXC containers and QEMU VMs. Molecule overrides this with `MolLxcExecutor` (touch-file stub).
- **`executor.snapshot_with_retry(executor, vmid, *, snap_state, retries=3, delay=15.0, _sleep=time.sleep, **api_params)`**: module-level free function (not on the `Executor` protocol) wrapping `orchestration.retry()` around `executor.snapshot()`. Used by both `flows/lxc.py` and `flows/vm.py` for both create (`until=changed`) and delete (`until=not failed`) calls. Returns a failed `PrimitiveResult` after exhausting retries so existing warning/fallback paths apply unchanged. Treats "CT is locked" PVE task-lock errors as transient. `_sleep` is injectable for tests.
- **LXC primitive methods on `Executor`**: `introspect(lxc_id)` → `lxc_introspect.yml`; `vzdump(lxc_id, *, backup_storage, lxc_name)` → `vzdump.yml`; `lxc_os_update(lxc_id, *, os_update_cmd)` → `lxc_os_update.yml`; `lxc_app_update(lxc_id, *, lxc_shell, lxc_unattended, lxc_needs_scale, lxc_build_cpu, lxc_build_ram, lxc_run_cpu, lxc_run_ram)` → `lxc_app_update.yml`; `post_update(lxc_id, *, lxc_shell, dpkg_hash_cmd, lxc_script_name)` → `lxc_post_update.yml`; `pct_rollback(lxc_id)` → `rollback.yml`; `pct_start(lxc_id)` → `pct_start.yml`; `pct_stop(lxc_id)` → `pct_stop.yml`. Each result passes through `_merge_facts` so stdout/rc from `set_stats` facts override the raw runner result. Results from `introspect` / `post_update` are unpacked from `.facts` dict keys (`config_stdout`, `status_stdout`, `pull_rc`, `script_stdout`, `dpkg_hash_after`, `version_after`).
- **`run_concurrent()` timeout parameter**: `orchestration.run_concurrent()` accepts `timeout: Optional[float] = None`. When set, each `future.result(timeout=timeout)` raises `concurrent.futures.TimeoutError` (caught by the existing `except BaseException` clause, becomes a per-item failure) rather than hanging forever on a stalled SSH connection. Default `None` preserves existing behaviour.
- **`_discover_vm_locations()` warning**: the silent `except Exception: return {}` in `driver.py` now prints `[vm phase] WARNING: cluster discovery failed (…), using pve_node hints` to `sys.stderr` before returning `{}` — surface the error without aborting the run.
- **Two-executor pattern for VMs** (Phase 4a): `run_vm_update()` takes `executor` (bound to the VM guest via SSH, for package upgrades) and `node_executor` (bound to the Proxmox node via SSH, for `qm rollback`/`qm status`). Using the wrong executor causes `qm` commands to SSH into the VM and fail silently.
- **HA-aware VM node discovery** (Phase 4a): `driver.run_vm_phase()` calls `pvesh get /cluster/resources --type vm` on the first available Proxmox node to build a live `{vmid: (node, api_host)}` map. `pve_node` in inventory is only a fallback hint — it goes stale when HA migrates a VM.
- **Package manager detection uses `if/elif/else`**: `&&`/`||` chains with equal precedence cause all branches to fire on Debian systems (right-hand side of `&&` runs when left succeeds, right-hand side of `||` is skipped — but the next `&&` in the chain still runs). Always use `if which apt-get ...; then echo apt; elif which dnf ...; then echo dnf; fi` for unambiguous detection.
- **`lxc_parse.py` owns all regex extraction**: `parse_pct_config()`, `parse_pct_status()`, `parse_ct_script()`, and `script_name_from_update()` (used in the detect flow — `cat` the update script on the node, parse the ct-script name in Python). Parity is locked by `tests/unit/test_status_lxc.py` — if a pattern changes, update both the parser and its test.

### Testing infrastructure

Tests are **plain Python** (`tests/unit/`) — no Ansible, no PVE, no Jinja shim (the old
`tests/conftest.py` Jinja filters and the `test_*_report.py`/`*_regex.py`/`test_discord_briefing.py`
shim tests were deleted with the roles). Flows are tested with a per-flow scripted fake executor
and monkeypatched `http`; status/parse/helper functions are tested directly. `ruff` and `mypy`
run clean over `proxmox_fleet/` (and `ruff` over `tests/` too).

| File | What it covers |
|---|---|
| `test_config_model.py` | `CustomConfig` model validation and defaults |
| `test_state_model.py` | `FleetState` construction, `from_raw()` aliases, `dump_for_ansible()` |
| `test_changes.py` | All 8 functions in `changes.py`: `normalize_version`, `is_outdated`, `lxc_os_changed`, `lxc_os_pkg_count`, `dpkg_hash_differs`, `vm_pkg_count`, `pkg_changed`, `custom_changed` — edge cases: leading `v` stripped, apt noop phrase, fail-open empty latest |
| `test_cli.py` | `_parse_extra_vars`, `_is_true`, `cli.main()` propagation of all flags; monkeypatches `driver.run_fleet` + `GlobalSettings.load` |
| `test_runner.py` | `runner._harvest()` with minimal fake runner: happy path via `set_stats`, `runner_on_failed`, unreachable, `runner.status=="failed"`, facts accumulate, non-int rc safe |
| `test_executor.py` | `_merge_facts` override semantics; `RunnerExecutor._shell` extravars (become/chdir/environment/changed_when/ignore_errors); `run_local` targets localhost; `snapshot()` fact-merge; `snapshot_with_retry` retry + sleep; contract tests for all 8 new primitive methods |
| `test_settings.py` | `GlobalSettings.load()`, field defaults (node/manager), missing-file, integer kuma-map key coercion, new timeout/retry field defaults |
| `test_deps.py` | `validate_depends_order()` + `dependency_failed()` |
| `test_window.py` | `in_window()` day/time/wrap/force + locale-independent weekday mapping; tz-aware datetime conversion; `MaintenanceWindow` model accepted directly |
| `test_inventory.py` | `load_custom_hosts/proxmox_nodes/proxmox_vms/remote_hosts()` with `tmp_path`; inline-vs-host_vars precedence (incl. node `ansible_host` from host_vars) |
| `test_pkg.py` | `detect_pkg_mgr()` (apt/dnf/apk/fallback), `upgrade_cmd()` per-mgr + `LC_ALL=C` pin, `kuma_healthy()` |
| `test_driver.py` | `run_*_phase()` with monkeypatched `RunnerExecutor`; dep-abort, window skip, dry-run propagation, abort-on-first-failure, state JSON output |
| `test_orchestration.py` | `run_serial()` / `run_concurrent()` / `retry()` |
| `test_http.py` | HTTP helpers with monkeypatched urllib |
| `test_status_custom.py` | `custom_status()` decision tree |
| `test_steps.py` | `run_steps()` interpolation, `when` gating, `timeout` wrap, retries + injected `delay` sleep |
| `test_flow_custom.py` | `run_custom_update()` end-to-end with fake executor |
| `test_status_lxc.py` | `lxc_app_status/os_status/rescue_app_status/dry_run_status/should_report`, `parse_pct_config/status`, `parse_ct_script`, `dpkg_hash_differs` |
| `test_flow_lxc.py` | `run_lxc_update()` end-to-end with `ScriptedLxcExecutor`; Alpine `ash` version read; `LC_ALL=C` on OS/dpkg commands; `snapshot_with_retry` retry-succeeds and retry-exhausted cases |
| `test_status_vm.py` | `vm_status()`, `vm_rescue_status()`, `vm_should_report()` |
| `test_flow_vm.py` | `run_vm_update()` end-to-end with `ScriptedVmExecutor` + `ScriptedNodeExecutor` (two-executor pattern); `snapshot_with_retry` retry success case |
| `test_status_remote.py` | `remote_status()`, `remote_should_report()` |
| `test_flow_remote.py` | `run_remote_update()` end-to-end with `ScriptedRemoteExecutor` |
| `test_status_node.py` | `node_status()` (5 branches), `manager_status()` (3 branches), `node_should_report()` |
| `test_flow_node.py` | `run_node_update()` + `run_manager_update()` with `ScriptedNodeExecutor`; retry, reboot, proxy wait, manager-host skip |
| `test_briefing.py` | `render_briefing()` behavioural cases + a **golden parity** test asserting byte-equality with `tests/unit/data/briefing_golden.json` (captured from the retired `.j2`); `prepare_body`/title/color/`should_notify` |
| `test_history.py` | `build_run_summary()` counts + the `briefing` field; `write_history()` write/prune/`latest.json`; `_ts_now()` microsecond-precision format |
| `test_notifiers.py` | `resolve_notifiers()` shim, `dispatch()` discord/ntfy payloads + headers, `ping_deadmans()` `/fail` suffix |
| `test_wrapper.py` | `fleet-update.py` wrapper — defaults, all friendly flags + aliases, `-e` propagation, friendly-flag-over-extravars precedence, `--inventory`/`--vars-file` forwarding, bad `-e` format exit, exit-code forwarding; + `cli.py` `force_window` propagation fix |

**Scripted fake executors**: each flow test defines a `Scripted*Executor` (in its own test file) whose `run_shell` returns queued `PrimitiveResult`s matched by command substring, records `.commands`, and stubs `snapshot()`/`reboot()`. There is no shared `conftest.py`.

**Molecule scenarios** drive the Python flows, not roles. `roles/lxc_update/molecule/` has three CI scenarios (`normal`, `rollback`, `snapfail`) whose `converge.yml` runs `mol_run_flow.py` → `MolLxcExecutor` (touch-file snapshot stub) → `run_lxc_update()`. `roles/custom_update/molecule/` has six (`normal`, `noop`, `rescue`, `dry_run`, `uptodate`, `per_step`) → `run_custom_update()`. `verify.yml` loads the `dump_for_ansible()` JSON with `include_vars`. (`roles/` now contains **only** these molecule harnesses — no role tasks/defaults.) Idempotency is disabled — backup/update are intentionally non-idempotent.

**CI** (`.github/workflows/ci.yml`): `yamllint`, `ansible-lint` (primitives), `syntax-check` (primitives), `unit-tests` (pytest on Python 3.10/3.11/3.12 matrix with `--cov=proxmox_fleet --cov-report=term-missing`), `mypy`, `ruff` (`proxmox_fleet/ tests/`), `bandit -r proxmox_fleet/ -ll` (medium+ severity security scan), plus two molecule matrices — `molecule-lxc-update` (normal, rollback, snapfail) and `molecule-custom-update` (normal, noop, rescue, dry_run, uptodate, per_step). Both molecule jobs install `ansible-runner` + `pip install -e .` so `mol_run_flow.py` can import `proxmox_fleet`.

### Briefing output constraints (`briefing.py`)

The briefing is now rendered in Python (`render_briefing`), not Jinja, but the **Discord embed markdown** constraints still apply: embed descriptions support `**bold**`, `*italic*`, `` `code` ``, `- ` bullet lists, and `\n` newlines; they do **not** support `>` blockquotes or `#` headers (those work only in regular messages, not webhook embeds). Byte-parity with the retired template is locked by the golden test (see "Briefing byte-parity" above).
