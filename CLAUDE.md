# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Syntax check (no PVE infrastructure needed)
ansible-playbook fleet-update.yml --syntax-check

# Dry run — no changes, forces Discord notification
ansible-playbook fleet-update.yml --check -e "force_notify=true"

# Version comparison only (no updates applied)
ansible-playbook fleet-update.yml -e "lxc_dry_run=true force_notify=true"

# Full run against one node only
ansible-playbook fleet-update.yml --limit pve-01

# Full run with forced notification
ansible-playbook fleet-update.yml -e "force_notify=true"

# Install required collections
ansible-galaxy collection install community.proxmox community.general

# Jinja2 unit tests (no Ansible or PVE needed)
pip install -r tests/requirements.txt
pytest tests/unit/ -v
pytest tests/unit/test_report_tmp_app.py -v    # single file
pytest tests/unit/ -k "test_version_updated"   # single test

# Python type checking
pip install -e '.[dev]'        # includes types-PyYAML, mypy, pytest, pydantic
python -m mypy proxmox_fleet/

# Static analysis
yamllint .
ansible-lint fleet-update.yml

# Molecule scenario (runs against localhost via stub pct/vzdump scripts)
cd roles/lxc_update && molecule test -s lxc_update_normal
cd roles/lxc_update && molecule converge -s lxc_update_normal  # converge only, no verify/destroy
cd roles/custom_update && molecule test -s custom_update_normal  # Python flow via RunnerExecutor

# Python driver (Phase 0b via Python instead of custom_update role)
fleet-update --use-custom-flow --check -e fleet_dry_run=true   # dry-run via Python driver
fleet-update --use-custom-flow --vars-file vars.yml            # full run via Python driver
```

`hosts.ini` and `vars.yml` are gitignored (contain secrets/IPs). Copy from `.example` files to run locally.

## File Map

```
fleet-update.yml                        # Main playbook — 7 phases + "Merge Python state" play between 0b and 1
ansible.cfg                             # forks=20, pipelining=true, inventory=./hosts.ini
vars.yml / vars.yml.example             # Secrets + behaviour flags (gitignored; copy from .example)
hosts.ini / hosts.ini.example           # Inventory (gitignored; copy from .example)
.ansible-lint                           # profile: moderate; excludes role task files that use {{ role_path }} includes
.yamllint.yml                           # extends: default; line-length warning at 160
.github/workflows/ci.yml                # yamllint, ansible-lint, syntax-check, unit-tests, + molecule matrices (lxc, custom)
pyproject.toml                          # package config; [dev] extras include types-PyYAML for mypy
proxmox_fleet/
  models/
    config.py                           # CustomConfig pydantic schema (custom_update config files)
    state.py                            # FleetState + per-type records; dump_for_ansible() writes fleet_* JSON
    settings.py                         # GlobalSettings pydantic model for vars.yml; load() returns defaults on missing file
  flows/
    custom.py                           # run_custom_update() — the full custom flow (detect→backup→update→health→report)
  deps.py                               # validate_depends_order() + dependency_failed() — ports of Phase-0a Jinja logic
  driver.py                             # run_custom_phase() — Phase 0a+0b in Python: load hosts, validate deps, serial loop
  executor.py                           # Executor protocol + RunnerExecutor (ansible-runner backed)
  http.py                               # Manager-local HTTP: get_json, poll_until, request, post_json
  inventory.py                          # load_custom_hosts() — line-by-line hosts.ini parser + host_vars/ merge
  orchestration.py                      # run_serial(), run_concurrent() — Python equivalents of serial/forks
  runner.py                             # invoke_primitive() — thin ansible-runner wrapper; passes project_dir=os.getcwd()
  steps.py                              # run_steps() — executes update_steps with per-step timeout + when gate
  status.py                             # custom_status(), custom_should_report(), is_outdated() — ported decision trees
  changes.py                            # change detection helpers
  window.py                             # in_window() — port of tasks/check-window.yml using stdlib zoneinfo
  cli.py                                # fleet-update CLI; --use-custom-flow routes Phase 0b through driver.py
tasks/
  fleet-state-append.yml                # Shared state accumulator — always use this, never inline set_fact+delegate
templates/
  discord_briefing.j2                   # Discord embed body — renders fleet_*_data lists into markdown
config_templates/
  custom_system.yml.example             # Fully-commented schema template — copy to configs/<name>.yml
configs/
  .gitkeep                              # Real configs/*.yml are gitignored; commit *.yml.example worked examples only
  gitea.yml.example                     # Worked example: Gitea binary update
ansible/
  primitives/
    run_shell.yml                       # Single-action primitive: run a shell command, return rc/stdout/stderr via set_stats
    reboot_host.yml                     # Single-action primitive: reboot and wait
tests/
  requirements.txt                      # pytest, jinja2, pyyaml — all that's needed for Jinja-shim unit tests
  conftest.py                           # Ansible-compatible Jinja2 env: regex_search (list return), bool, failed/search tests
  unit/                                 # 338 pytest tests; no Ansible or PVE required
  integration/
    test_fleet_state_append.yml         # Standalone ansible-playbook test for delegate_to+delegate_facts accumulation
roles/
  lxc_update/
    defaults/main.yml                   # Default values for all lxc_* vars + kuma_health_check_retries/delay
    tasks/
      main.yml                          # Orchestrator: introspect → block(detect→backup→dry_check|update→health_check→report) → rescue(rollback) → always
      introspect.yml                    # pct config + pct status; starts stopped containers; sets lxc_name, lxc_os, lxc_is_running, lxc_was_stopped
      detect.yml                        # Pulls /usr/bin/update, extracts ct script name, fetches .sh from GitHub, parses resource requirements
      backup.yml                        # vzdump and/or snapshot (BEFORE_UPDATE_AUTO) based on lxc_backup_strategy
      dry_check.yml                     # Reads installed version + fetches latest GitHub release; sets dry_run_status
      update.yml                        # OS update first, dpkg hash before/after community script, ver before/after, reboot if needed
      health_check.yml                  # Polls Uptime Kuma; failure → rescue (no ignore_errors); retries/delay are vars
      report.yml                        # Builds tmp_app/tmp_os strings; appends LXC record (skips idle containers)
    molecule/
      lxc_update_normal/                # Running container, vzdump backup, app+OS update
      lxc_update_template/              # pct config returns template:1 → all tasks skipped
      lxc_update_stopped/               # pct status:stopped → start → update → stop in always block
      lxc_update_dry_run/               # lxc_dry_run=true → only dry_check runs, no backup/update
      lxc_update_rescue/                # vzdump stub exits 1 → rescue block fires, fleet_failed=True
      lxc_update_rollback/              # health_check fails (Kuma unreachable) → rescue fires, FAILED recorded
  vm_update/
    defaults/main.yml                   # Default values for vm_* vars
    tasks/
      main.yml                          # Orchestrator: block(snapshot→update→health_check→report) → rescue → always(delete snapshot)
      snapshot.yml                      # Creates BEFORE_UPDATE_AUTO snapshot via PVE API
      update.yml                        # apt/dnf/apk upgrade + reboot check
      health_check.yml                  # Polls Uptime Kuma (vm_kuma_map)
      report.yml                        # Appends VM record to fleet_vm_data
    molecule/default/                   # Runs role in check_mode against localhost; no PVE needed
  remote_host_update/
    defaults/main.yml                   # Default values for remote_* vars
    tasks/
      main.yml                          # Orchestrator: block(update→health_check→report) → rescue (no always block — no snapshots)
      update.yml                        # apt/dnf/apk upgrade + reboot check
      health_check.yml                  # Polls Uptime Kuma (remote_kuma_map)
      report.yml                        # Appends remote host record to fleet_remote_data
    molecule/default/                   # Runs role in check_mode against localhost; no PVE needed
  custom_update/
    defaults/main.yml                   # custom_dry_run, custom_allow_reboot, custom_kuma_map, kuma health check vars (legacy path)
    tasks/
      main.yml                          # Orchestrator: load_config → block(detect→backup→update→health_check→report) → rescue(rollback_command)
      load_config.yml                   # include_vars configs/{{ custom_config }}.yml → combine custom_overrides → custom_cfg
      detect.yml                        # version_command (before); latest_version via GitHub API or command (delegated to localhost)
      backup.yml                        # backup_command if defined
      update.yml                        # loop update_steps; version_command (after); changed_when command; reboot if cfg.reboot
      health_check.yml                  # kuma | command | http | none; failure → rescue (no ignore_errors)
      report.yml                        # tmp_custom decision tree; appends fleet_custom_data (skips idle)
    molecule/
      mol_run_flow.py                   # Shared converge helper: loads config, builds RunnerExecutor, calls run_custom_update(), writes dump_for_ansible() JSON
      custom_update_normal/             # Version changes 1.0 → 1.1; "Updated: 1.0 → 1.1" recorded
      custom_update_noop/               # Version unchanged; record suppressed (idle)
      custom_update_rescue/             # Update step exits 1; rollback_command runs; fleet_failed=True
      custom_update_dry_run/            # custom_dry_run=true; detect only; "dry-run: X → Y" recorded
      custom_update_uptodate/           # update_only_if_outdated=true; version matches; update steps skipped
      custom_update_per_step/           # per-step when: gate referencing steps.NAME stdout
```

## Architecture

### Play order in `fleet-update.yml`

| Phase | Hosts | Purpose |
|---|---|---|
| Pre-Flight | localhost | Verify apt-cacher-ng proxy is reachable |
| Phase 0 | `remote_hosts` | Non-Proxmox hosts via `remote_host_update` role |
| Phase 0a | localhost | Validate `custom_hosts` `depends_on` ordering (fail loud on missing/after) |
| Phase 0b | `custom_hosts` | Non-standard systems via `custom_update` role (skipped when `skip_phase_0b=true`) |
| *(merge)* | localhost | Load Python driver output into `fleet_custom_data`/`fleet_changed`/`fleet_failed` (active when `fleet_custom_state_path` is defined) |
| Phase 1 | `proxmox_nodes` | Tag-filtered LXC discovery + `lxc_update` role per container |
| Phase 1b | `proxmox_vms` | QEMU VMs via `vm_update` role |
| Phase 2 | `proxmox_nodes` | PVE node OS update + sequential reboot (`serial: 1`, `any_errors_fatal: true`) |
| Phase 3 | localhost | Manager container self-update |
| Phase 4 | localhost | Persist run history → dispatch notifiers → dead-man's-switch ping |

Each phase ORs the master `fleet_dry_run` into its role's dry flag via an eager `set_fact` (no self-reference recursion); `fleet_dry_run` also forces a notification.

**`--use-custom-flow` CLI flag**: when set, `cli.py` calls `driver.run_custom_phase()` before the playbook, writes `/tmp/fleet_custom_state.json`, and passes `skip_phase_0b=true` + `fleet_custom_state_path` as extravars. Phase 0b is skipped; the merge play loads the JSON and seeds `fleet_changed`/`fleet_failed` **before Phase 1** so the downstream `fleet-state-append.yml` calls OR-join correctly. Without the flag the legacy `custom_update` role runs unchanged.

### State accumulation pattern

All fleet state lives as facts on `localhost` across plays. Every role and play appends to it via `tasks/fleet-state-append.yml` using `delegate_to: localhost` + `delegate_facts: true` + `check_mode: no`. The five state lists are `fleet_lxc_data`, `fleet_vm_data`, `fleet_remote_data`, `fleet_node_data`, and `fleet_custom_data`. `fleet_changed`, `fleet_failed`, `fleet_error_log` (`list[{host, task, error}]`), and `fleet_warning_log` (`list[{host, task, warning}]`, non-fatal) are also maintained here.

Do not write `set_fact` + `delegate_to: localhost` blocks directly — always call `tasks/fleet-state-append.yml` instead. A warning-only call passes `fleet_record_type: warning` + `fleet_warning_detail` (no list matches, so only the warning is appended).

### Phase 4 subsystems

- **Notifiers** (`tasks/notify.yml`): the briefing is rendered **once** from `discord_briefing.j2` into `_briefing_body` and fanned out to a `notifiers` list (types `discord`, `ntfy`). Back-compat: if `notifiers` is unset but `discord_webhook` is, a single Discord notifier is synthesized. ntfy reuses the same body verbatim; only the transport envelope differs.
- **Run history** (`tasks/persist-history.yml`): writes `run-<UTC-ts>.json` + `latest.json` to `fleet_history_dir`, pruned to `fleet_history_keep`. Gated on `fleet_history_enabled`.
- **Dead-man's-switch**: pings `fleet_deadmans_url` (`/fail` on failure) so its absence alerts when the orchestrator stops running.

### Cross-cutting subsystems

- **Snapshot-only rollback + warnings**: LXC and VM roles roll back via snapshot (`pct/qm rollback BEFORE_UPDATE_AUTO`) only when the snapshot was actually taken (`*_snap_res.changed`). A failed snapshot records a non-fatal warning and continues; rescue app/status string is `FAILED (NO SNAPSHOT)` vs `FAILED + ROLLED BACK` vs `FAILED`. `lxc_backup_strategy: both` / `vm_backup_strategy: both` take a simultaneous vzdump (never used for restore).
- **Fleet-wide dry-run**: `-e fleet_dry_run=true` puts every role in simulate mode. VM/remote use a dedicated `check_mode: yes` simulate task and report `WOULD UPDATE`/`OK`.
- **Maintenance windows** (`tasks/check-window.yml`): inventory hosts (remote/vm/custom) with a `maintenance_window` dict are silently skipped outside the window; `force_window=true` bypasses.

### Role structure (`roles/lxc_update/`)

`tasks/main.yml` is the orchestrator:
- `introspect.yml` runs **outside** the block (fail loud if `pct config` fails)
- Inside the block: `detect.yml` → `backup.yml` → `dry_check.yml` or `update.yml` → `health_check.yml` → `report.yml`
- Rescue block captures `ansible_failed_task.name` and `ansible_failed_result.stderr` as the **first** `set_fact` before anything else (subsequent tasks reset these vars), then calls `fleet-state-append.yml`
- Rescue block: capture failure → attempt `pct rollback BEFORE_UPDATE_AUTO` (only if `snap_res.changed`) → wait for container → set `lxc_rollback_done: true` → fleet-state-append with `FAILED + ROLLED BACK` or `FAILED`
- Always block: delete snapshot (only if `snap_res.changed`), stop container if `lxc_was_stopped` **and** `not lxc_rollback_done` (rollback restores the container, so don't stop it again)

`vm_update` and `remote_host_update` follow the same block/rescue/always pattern. `remote_host_update` has no always block (no snapshots to clean up).

### `custom_update` role structure

`tasks/main.yml` orchestrator:
- `load_config.yml` runs **outside** the block (fail loud on bad config) — loads `configs/{{ custom_config }}.yml` and merges `custom_overrides` (from inventory) into `custom_cfg`
- Inside the block: `detect.yml` → `backup.yml` (if `backup_command` defined and not dry-run) → `update.yml` (if not dry-run) → `health_check.yml` (if `health_check.type != none` and not dry-run) → `report.yml`
- Rescue block: capture failure → run `rollback_command` (ignore_errors) → fleet-state-append `FAILED`
- No always block (v1 — no snapshot to clean up)

**`custom_config` inventory var**: each host in `[custom_hosts]` must have `custom_config=<name>` pointing to `configs/<name>.yml`. Optionally set `custom_overrides: {...}` in host_vars to deep-merge over the config file.

**Config files**: `configs/*.yml` is gitignored. Commit `configs/*.yml.example` as templates. Real configs live only on the Ansible manager. See `config_templates/custom_system.yml.example` for the full schema.

**`tmp_custom` decision tree in `report.yml`** (custom_update):
- `custom_dry_run=true` → `dry-run: <before> → <latest>`
- `changed_when.type == always` → `Updated [+ Rebooted]`
- `changed_when.type == command`, exit 0 → `Updated [+ Rebooted]`
- `changed_when.type == command`, exit non-0 → `OK`
- `changed_when.type == version` (default), before/after differ → `Updated: X → Y [+ Rebooted]`
- `changed_when.type == version`, before/after same → `OK`
- No version data (no `version_command`) → `Updated [+ Rebooted]` (fallback)

### `update.yml` task order and change detection

The task order matters for correct attribution:
1. Read `lxc_ver_before`
2. OS update (`apt dist-upgrade` / `apk upgrade`) — runs **first** so OS packages get credited to the OS line, not the app line
3. Read `dpkg_hash_before` — md5sum of `dpkg-query -W` (package→version pairs) after OS update, before community script
4. Scale up resources (if `needs_resource_scale`)
5. Community script (`app_update_res`)
6. Read `dpkg_hash_after` — same query; if hash matches `dpkg_hash_before`, nothing was installed
7. Read `lxc_ver_after`
8. Scale down → reboot check

**`tmp_app` decision tree in `report.yml`** (in priority order):
- Version files differ → `Updated: X → Y`
- Version files both non-empty and equal → `OK` (confirmed no app change)
- dpkg hash differs → `UPDATED` (packages changed, no version file)
- dpkg hash matches → `OK` (nothing installed, no version file)
- No hash data (non-apt OS) → `UPDATED` (fallback)
- `app_update_res.changed` is false → `OK`

**Why dpkg hash instead of stdout parsing:** `PHS_SILENT=1` (set by `lxc_unattended: true`) routes apt's stdout to `/dev/null` inside community scripts, so keywords like `0 upgraded, 0 newly installed` never appear in `app_update_res.stdout`. The dpkg hash is a direct query, immune to output suppression.

### `detect.yml` flow and version file convention

`detect.yml` does three things in sequence:
1. `pct pull {lxc_id} /usr/bin/update /tmp/ansible_update_{lxc_id}` — extracts the community-scripts update script from the container
2. Greps the script for `ct/NAME.sh` to get the ct script name (e.g. `sonarr`); sets `lxc_no_update_script: true` if not found
3. Fetches `https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/{name}.sh` (delegated to localhost) — parses `var_cpu`/`var_ram` for build resources, and `pct set $CTID -cores/-memory` for run resources

**Version files**: community-scripts store the installed app version at `~/.{scriptname}` inside the container (e.g. `~/.sonarr` contains `4.0.17.2952`). `update.yml` reads this before and after the update as `lxc_ver_before`/`lxc_ver_after`. `dry_check.yml` reads it as `installed_ver` and compares against the latest GitHub release.

**Resource scaling**: when `lxc_build_cpu > lxc_run_cpu`, `needs_resource_scale` is true — `update.yml` scales the container up before the update script runs and back down after.

### Uptime Kuma integration

`health_check.yml` (all three roles) polls `{kuma_url}/api/status-page/heartbeat/{kuma_slug}` and waits for the mapped monitor to show status `1`. It only fires when `lxc_id` (or equivalent) is in `lxc_kuma_map` (or `vm_kuma_map`/`remote_kuma_map`) **and** something actually changed. Kuma credentials and maps are in `vars.yml`.

### Key non-obvious details

- **Tag-based LXC discovery** (Phase 1): only LXCs tagged `community-script` or `proxmox-helper-scripts` in PVE are processed. Untagged containers are never touched. Tags must be set in PVE UI → Container → Options → Tags.
- **`include_role` not `import_role`**: roles are called in a loop; `import_role` is static (parse-time) and cannot be used inside loops.
- **URI calls in `detect.yml` are delegated to localhost**: PVE nodes may not have outbound HTTPS to GitHub. The Ansible manager always does.
- **`pve_node` inventory var** (VM inventory): must match the PVE node's **inventory hostname** (not its IP) — it is used as a key into `hostvars` for the snapshot API call: `hostvars[pve_node]['ansible_host']`.
- **PBS is transparent**: setting `lxc_backup_storage` to a PBS storage name routes `vzdump` to PBS automatically — no special code path.
- **`[proxmox_vms]`, `[remote_hosts]`, and `[custom_hosts]` must exist** in `hosts.ini` even if empty (just the group header). Ansible raises "no hosts matched" otherwise.
- **`custom_config` is a required per-host inventory var** for `[custom_hosts]` — set it in hosts.ini or host_vars. The role will fail loudly if it's missing (include_vars will not find the file).
- **Node reboot is skipped** when `manager_lxc_id` runs on that node — rebooting the node would kill the manager mid-run.
- **Discord `check_mode: no`**: the URI task has this so `--check` runs still produce a notification when `force_notify=true`.
- **`lxc_backup_strategy`** is a four-value enum: `snapshot | vzdump | both | none` — not boolean flags.
- **`/tmp/.nc/clear` trick** in `update.yml`: overrides the `clear` shell command with a no-op so community-scripts update output isn't wiped from Ansible's stdout capture.
- **Snapshot name is fixed**: always `BEFORE_UPDATE_AUTO`. The `always:` cleanup hardcodes this name — changing it in `backup.yml` without also changing `main.yml` would leave orphaned snapshots.
- **`report.yml` skips idle containers**: the `when:` condition only appends a record when something changed or failed. Fully up-to-date containers with nothing to do produce no Discord entry.
- **`lxc_continue_on_error`**: when `true`, Phase 1 uses `ignore_errors: yes` on the LXC loop, so a single failing container doesn't abort the rest of the node's containers.
- **`health_check.yml` no longer has `ignore_errors: yes`** in `lxc_update` — Kuma failure now triggers the rescue block (and snapshot rollback if a snapshot was taken). Retries and delay are controlled by `kuma_health_check_retries` (default 5) and `kuma_health_check_delay` (default 30s), which molecule scenarios can override to 1/1 for fast tests.
- **Rollback only fires when `snap_res.changed`** — if `lxc_backup_strategy: none` or the snapshot API call failed (both result in `snap_res.changed=false`), the rescue skips rollback and just records `FAILED`.
- **Auto-rollback covers snapshots only, never vzdump**: a `vzdump`-only strategy produces a backup archive but `snap_res` is never set, so a failed update is **not** automatically restored — only `FAILED` is recorded. Restore vzdump archives manually. Use `snapshot` or `both` if you want automatic rollback.
- **`custom_update` config dir**: `load_config.yml` reads `{{ custom_config_dir | default(playbook_dir ~ '/configs') }}/<name>.yml`. Molecule sets `custom_config_dir: /tmp/mol_custom_<scenario>/configs` — do **not** try to override `playbook_dir` (it is a reserved magic variable and the override is silently ignored).
- **Deferred Jinja in custom configs**: command strings are rendered eagerly (validation, combine, loop), so a step `command` cannot interpolate runtime facts like `custom_step_results`. `register` stashes stdout for use in a later step's `when:` only. `load_config` skips the `combine` when there are no `custom_overrides` to avoid prematurely rendering deferred `{{ }}`.
- **`invoke_primitive` requires CWD = project root**: `ansible_runner.run()` is called with `project_dir=os.getcwd()`. Without an explicit `project_dir`, ansible-runner creates a fresh `tempfile.mkdtemp()` as `private_data_dir` and looks for the playbook at `<tempdir>/project/ansible/primitives/<name>.yml` — which never exists. The production CLI runs from the project directory; `mol_run_flow.py` calls `os.chdir(_project_root)` at module load to guarantee this for molecule runs.
- **`FleetState.dump_for_ansible()` + merge-play timing**: the "Merge Python custom state" play in `fleet-update.yml` must come **between Phase 0b and Phase 1** — not in Phase 4 pre_tasks. Loading it later would let Phases 1–3 `fleet-state-append.yml` calls start from Ansible-default `fleet_changed=false`/`fleet_failed=false` and silently drop the Python driver's values via OR-join.
- **Inventory parser must not use `configparser`**: `configparser` splits on the first `=` and mis-parses Ansible host lines of the form `hostname key=val key=val …` (it produces key=`hostname key` and value=`val key=val …`). `proxmox_fleet/inventory.py` uses a manual regex parser instead.
- **`--use-custom-flow` flag is temporary**: once a real `--check` run confirms parity, flip it to the unconditional default in `cli.py`, remove the flag, and delete the legacy `roles/custom_update/tasks/` files and Jinja-shim tests `test_custom_report.py`, `test_run_step.py`, `test_custom_depends.py`.

### Testing infrastructure

**Jinja2 unit tests** (Jinja shim — `tests/unit/`) exercise the conditional logic inside role task files without Ansible or PVE:

| File | What it covers |
|---|---|
| `test_report_tmp_app.py` | 11-branch `tmp_app` + rollback rescue app string |
| `test_report_tmp_os.py` | `tmp_os` expression + the `None`-guard in the payload |
| `test_report_when_condition.py` | The `when:` gate that suppresses idle `OK`/`OK` containers |
| `test_dry_check_status.py` | `dry_run_status` branches in `dry_check.yml` |
| `test_detect_regex.py` | `regex_search` patterns in `detect.yml` and `needs_resource_scale` |
| `test_discord_briefing.py` | Full `discord_briefing.j2` including Custom Systems section |
| `test_fleet_state_append_logic.py` | `set_fact` expressions in `tasks/fleet-state-append.yml` including `fleet_custom_data` |
| `test_introspect_regex.py` | `pct config` output parsing in `introspect.yml` |
| `test_custom_report.py` | `tmp_custom` tree + `custom_changed` + `custom_is_outdated` (Tier 5) |
| `test_vm_report.py` | VM success status tree, rescue rollback string, dry-run `WOULD UPDATE` |
| `test_notify.py` | notifier back-compat shim + ntfy header construction |
| `test_persist_history.py` | run-summary count assembly |
| `test_run_step.py` | per-step `timeout` command wrapping |
| `test_check_window.py` | maintenance-window day/time/wrap/force logic |
| `test_custom_depends.py` | Phase 0a dependency-order validator + runtime `_dep_failed` gate |

**Plain-Python unit tests** (no Jinja shim — `tests/unit/`) test the typed Python modules directly:

| File | What it covers |
|---|---|
| `test_config_model.py` | `CustomConfig` model validation and defaults |
| `test_state_model.py` | `FleetState` construction, `from_raw()` aliases, `dump_for_ansible()` |
| `test_settings.py` | `GlobalSettings.load()`, field defaults, missing-file behaviour |
| `test_deps.py` | `validate_depends_order()` + `dependency_failed()` — mirrors `test_custom_depends.py` |
| `test_window.py` | `in_window()` — mirrors `test_check_window.py` case-for-case |
| `test_inventory.py` | `load_custom_hosts()` with `tmp_path` fixtures |
| `test_driver.py` | `run_custom_phase()` with monkeypatched `RunnerExecutor`; dep-abort, window skip, dry-run propagation, state JSON output |
| `test_orchestration.py` | `run_serial()` / `run_concurrent()` |
| `test_http.py` | HTTP helpers with monkeypatched urllib |
| `test_status_custom.py` | `custom_status()` — mirrors `test_custom_report.py` |
| `test_steps.py` | `run_steps()` — mirrors `test_run_step.py` |
| `test_flow_custom.py` | `run_custom_update()` end-to-end with fake executor |

`tests/conftest.py` implements Ansible-specific Jinja2 filters (`regex_search` list-return, `regex_replace`, `combine`, `intersect`) and tests (`failed`, `search`, `equalto`, `succeeded`) so templates render identically to Ansible. When writing new tests, use `render()` for string output and `render_native()` / `make_native_env()` (via `NativeEnvironment`) for Python objects.

**Molecule scenarios** (`roles/lxc_update/molecule/`) test lxc role orchestration against localhost using stub `pct`/`vzdump` shell scripts placed in `/tmp/mol_stubs/` by `prepare.yml`. **`roles/custom_update/molecule/`** scenarios drive `mol_run_flow.py` which builds a real `RunnerExecutor` and calls `run_custom_update()` — the full Python→ansible-runner→primitive→shell-stub stack. Each scenario's `converge.yml` invokes `mol_run_flow.py` with `ansible.builtin.command`; `verify.yml` loads the `dump_for_ansible()` JSON with `include_vars`. Idempotency checking is disabled for all scenarios — backup and update operations are intentionally non-idempotent.

**CI** (`.github/workflows/ci.yml`): `yamllint`, `ansible-lint`, `syntax-check`, `unit-tests` (pytest), plus two molecule matrices — `molecule-lxc-update` (normal, rollback, snapfail) and `molecule-custom-update` (normal, noop, rescue, dry_run, uptodate, per_step). The `molecule-custom-update` job installs `ansible-runner` and `pip install -e .` so `mol_run_flow.py` can import `proxmox_fleet`. The `ansible-lint` job excludes role task files that use dynamic `include_tasks` via `exclude_paths` in `.ansible-lint` (the `load-failure[not-found]` rule is unskippable).

### Jinja2 / Ansible patterns

- **`regex_search` with capture groups**: always use `(value | regex_search('pattern', '\\1') or [''])[0]`. Never use `| first` — `regex_search` returns Python `None` (not Jinja2 `Undefined`) on no match, so `| default([])` does not help and `| first` on `None` crashes.
- **Empty `>-` block → Python `None`**: a `set_fact` using a `>-` YAML block scalar whose Jinja2 evaluates to an empty string stores `None`, not `""`. Guard downstream with `{{ '' if var is none else var | trim }}`. The `discord_briefing.j2` OS field and `report.yml` `os:` payload both use this pattern.
- **Explicit `{{ '\n' }}` for newlines in templates**: do not rely on template-source newlines when `{%- -%}` tags are present — they strip adjacent whitespace including newlines. Use `{{ '\n' }}` (or `{{ '\n\n' }}` for blank lines) as explicit output that cannot be stripped by control-tag whitespace rules.
- **Discord embed markdown**: embed descriptions support `**bold**`, `*italic*`, `` `code` ``, `- ` bullet lists, and `\n` newlines. They do **not** support `>` blockquotes or `#` headers (those work only in regular messages, not webhook embeds).
