# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The Ansible→Python migration is **complete**: `fleet-update` (→ `driver.run_fleet()`) is
the only entrypoint. There is no `fleet-update.yml` playbook and no `--use-*-flow` flags —
Ansible runs only as the execution primitives in `ansible/primitives/*.yml`.

```bash
# Fleet-wide dry-run (no changes), forces a Discord/ntfy notification
fleet-update --check -e fleet_dry_run=true -e force_notify=true

# Full run with forced notification
fleet-update -e force_notify=true

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
ansible.cfg                             # forks=20, pipelining=true, inventory=./hosts.ini
vars.yml / vars.yml.example             # Secrets + behaviour flags (gitignored; copy from .example)
hosts.ini / hosts.ini.example           # Inventory (gitignored; copy from .example)
.ansible-lint                           # profile: moderate; targets ansible/primitives/ (name[casing] demoted to warning)
.yamllint.yml                           # extends: default; line-length warning at 160
.github/workflows/ci.yml                # yamllint, ansible-lint (primitives), syntax-check (primitives), unit-tests, mypy, + molecule matrices (lxc, custom)
pyproject.toml                          # package config; fleet-update entrypoint; [dev] extras include types-PyYAML for mypy
proxmox_fleet/
  models/
    config.py                           # CustomConfig pydantic schema (custom_update config files)
    state.py                            # FleetState + per-type records; dump_for_ansible() writes fleet_* JSON
    settings.py                         # GlobalSettings pydantic model for vars.yml; load() returns defaults on missing file; includes LXC, VM, remote, node/manager + PVE API fields
  flows/
    custom.py                           # run_custom_update() — the full custom flow (detect→backup→update→health→report)
    lxc.py                              # run_lxc_update() — the full LXC flow (introspect→detect→backup→update→health→report); try/except/finally = block/rescue/always
    vm.py                               # run_vm_update() — the full VM flow; two-executor pattern: executor=VM SSH, node_executor=Proxmox node SSH (for qm rollback/status)
    remote.py                           # run_remote_update() — the full remote host flow (pre_update_cmd→detect_pkg_mgr→upgrade→reboot→health→report); no snapshot/always block
    node.py                             # run_node_update() + run_manager_update() — Phase 2+3; apt w/ 5 retries, robust reboot check, manager-host skip, proxy wait
  deps.py                               # validate_depends_order() + dependency_failed() — ports of Phase-0a Jinja logic
  driver.py                             # run_fleet() end-to-end orchestrator (pre-flight → all phases → _merge_state → notify) + per-phase run_*_phase() helpers
  executor.py                           # Executor protocol + RunnerExecutor; snapshot() added for proxmox_snap primitive
  http.py                               # Manager-local HTTP: get_json, poll_until, request, post_json
  inventory.py                          # load_custom_hosts() + load_proxmox_nodes() — line-by-line hosts.ini parsers
  lxc_parse.py                          # parse_pct_config(), parse_pct_status(), parse_ct_script() — regex helpers for lxc flow
  orchestration.py                      # run_serial(), run_concurrent() — Python equivalents of serial/forks
  runner.py                             # invoke_primitive() — thin ansible-runner wrapper; passes project_dir=os.getcwd()
  steps.py                              # run_steps() — executes update_steps with per-step timeout + when gate
  status.py                             # all status decision trees: custom_status(), lxc_*(), vm_status(), vm_rescue_status(), remote_status(), node_status(), manager_status()
  changes.py                            # change detection helpers; lxc_os_changed(), dpkg_hash_differs(), lxc_os_pkg_count()
  window.py                             # in_window() — port of tasks/check-window.yml using stdlib zoneinfo
  briefing.py                           # render_briefing() byte-parity port of discord_briefing.j2 + prepare_body/title/color/should_notify
  history.py                            # build_run_summary() + write_history() — port of persist-history.yml; records rendered briefing body
  notifiers.py                          # resolve_notifiers() + dispatch() (discord/ntfy) + ping_deadmans() — port of notify.yml + Phase-4 shim
  cli.py                                # fleet-update CLI — parses --check / -e / --inventory / --vars-file, then calls driver.run_fleet()
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
`FleetState` (the `_fold_*_outcome()` helpers); `run_fleet()` then `_merge_state()`s the
per-phase states into one. The state lists are `lxc`, `vm`, `remote`, `node`, `custom`,
with `changed`/`failed` flags and `errors`/`warnings` logs (`models/state.py`). The
`run_*_phase()` helpers can still `dump_for_ansible()` a phase's state to JSON when given a
`state_output_path` (used by tooling / molecule); `run_fleet()` passes `None` and merges
in-memory.

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
- **`FleetState.dump_for_ansible()` + merge-play timing**: there are now **five** merge plays in `fleet-update.yml` — (a) remote state between Phase 0 and Phase 0a, (b) custom state between Phase 0b and Phase 1, (c) LXC state between Phase 1 and Phase 1b, (d) VM state between Phase 1b and Phase 2, (e) node state between the VM merge and Phase 2. Each must stay in its correct position — loading state after a later phase would OR-join against already-false flags and silently drop the Python driver's values.
- **Inventory parser must not use `configparser`**: `configparser` splits on the first `=` and mis-parses Ansible host lines of the form `hostname key=val key=val …` (it produces key=`hostname key` and value=`val key=val …`). `proxmox_fleet/inventory.py` uses a manual regex parser instead.
- **`--use-custom-flow` flag is temporary**: once a real `--check` run confirms parity, flip it to the unconditional default in `cli.py`, remove the flag, and delete the legacy `roles/custom_update/tasks/` files and Jinja-shim tests `test_custom_report.py`, `test_run_step.py`, `test_custom_depends.py`.
- **`--use-lxc-flow` flag is temporary**: same retire step — confirm parity via `fleet-update --use-lxc-flow --check -e fleet_dry_run=true`, then flip to default and delete `roles/lxc_update/tasks/`, `roles/lxc_update/defaults/`, and six Jinja-shim tests (`test_report_tmp_app.py`, `test_report_tmp_os.py`, `test_report_when_condition.py`, `test_dry_check_status.py`, `test_detect_regex.py`, `test_introspect_regex.py`).
- **`--use-vm-flow` flag is temporary**: confirm parity via `fleet-update --use-vm-flow --check -e fleet_dry_run=true`, then flip to default and delete `roles/vm_update/tasks/`, `roles/vm_update/defaults/`, `tests/unit/test_vm_report.py`.
- **`--use-remote-flow` flag is temporary**: confirm parity via `fleet-update --use-remote-flow --check -e fleet_dry_run=true`, then flip to default and delete `roles/remote_host_update/tasks/`, `roles/remote_host_update/defaults/`.
- **`--use-node-flow` flag is temporary**: confirm parity via `fleet-update --use-node-flow --check -e fleet_dry_run=true`, then flip to default in `cli.py` and remove the flag. No legacy role tasks to delete — Phase 2/3 were inline in the playbook. Gate `skip_phase_2/3` permanently to `true`, then remove the Phase 2/3 plays from `fleet-update.yml`.
- **`--use-notify-flow` flag is temporary**: unlike the other flows, the Python notify phase runs **after** the playbook (it needs the *merged* fleet state). When set, `cli.py` adds `skip_phase_4=true` + `fleet_final_state_path`; the playbook's Phase 4 dumps the merged `fleet_*` facts to JSON (the `when: skip_phase_4` "Dump merged fleet state" task) instead of briefing; `cli.py` then loads that JSON into a `FleetState` and calls `driver.run_notify_phase()` regardless of playbook rc (a failure briefing must fire on failure), gated on the dump existing. Confirm byte-parity via `fleet-update --use-notify-flow --check -e fleet_dry_run=true -e force_notify=true` vs a flag-off run, then flip to default in `cli.py`, remove the flag, delete `templates/discord_briefing.j2` + `tasks/notify.yml` + `tasks/persist-history.yml`, and (once the Jinja shim has no other users) delete `tests/conftest.py` + the parity shim tests. Parity is also locked by the **golden test** in `test_briefing.py`.
- **Briefing byte-parity — no trailing newline**: `render_briefing()` must NOT emit a trailing `\n`. The Jinja env's `keep_trailing_newline=False` (default) strips `discord_briefing.j2`'s final source newline, so the golden test compares against output with no trailing newline. `prepare_body()` applies `.strip()` + a port of Jinja's `truncate(4000, killwords=False, end='\n...', leeway=5)` — match the algorithm exactly (return unchanged when `len <= 4005`).
- **`settings.notifiers` is `Optional[...]` defaulting to `None`**: this preserves the Ansible `notifiers is defined` distinction — an explicit `notifiers: []` in `vars.yml` means "no notifiers" and must NOT fall back to synthesizing one from `discord_webhook`; only an *unset* (`None`) value triggers the back-compat shim.
- **`run_shell.yml` has `check_mode: false`** on the shell task — the command **always executes** regardless of Ansible check mode. Python controls dry-run by passing either a simulate command (`apt-get -s`) or a real command; Ansible's `--check` flag is bypassed at the shell level. `reboot_host.yml` has the same `check_mode: false` for consistency; the node flow additionally guards the reboot call with `not dry_run` in Python so it never fires during dry-run regardless.
- **`run_node_update` retry uses injectable `_sleep`**: `orchestration.retry(apt_fn, retries=5, delay=30.0, sleep=_sleep)`. Callers pass `_sleep=lambda s: None` in tests to avoid 150 s of wait. `run_node_phase()` in the driver doesn't pass `_sleep`, so real runs use `time.sleep`. Tests that call via `run_node_phase()` must monkeypatch `time.sleep` to keep fast.
- **`vm_apt_res` register-overwrite bug in legacy `vm_update` role**: in dry-run mode (`vm_dry_run=True`), the "Simulate apt" task registers `vm_apt_res` with `changed=True`, but the skipped "Update VM packages (apt)" task (in the real-update block) also registers `vm_apt_res` with `{skipped: true, changed: false}`, overwriting the simulate result. This causes `vm_pkg_res.changed = False` in `report.yml` and the VM record is silently dropped from the Discord briefing. The Python driver (`--use-vm-flow`) is unaffected — it uses `run_shell` directly with no register overwrite.
- **`executor.snapshot(vmid, *, snap_state, api_host, ...)` added for Phase 3**: invokes `snapshot.yml` (which runs `community.proxmox.proxmox_snap` on localhost). `api_host` must be the node's `ansible_host` IP — not the inventory name. `vmid` (not `lxc_id`) because the same primitive handles both LXC containers and QEMU VMs. Molecule overrides this with `MolLxcExecutor` (touch-file stub).
- **Two-executor pattern for VMs** (Phase 4a): `run_vm_update()` takes `executor` (bound to the VM guest via SSH, for package upgrades) and `node_executor` (bound to the Proxmox node via SSH, for `qm rollback`/`qm status`). Using the wrong executor causes `qm` commands to SSH into the VM and fail silently.
- **HA-aware VM node discovery** (Phase 4a): `driver.run_vm_phase()` calls `pvesh get /cluster/resources --type vm` on the first available Proxmox node to build a live `{vmid: (node, api_host)}` map. `pve_node` in inventory is only a fallback hint — it goes stale when HA migrates a VM.
- **Package manager detection uses `if/elif/else`**: `&&`/`||` chains with equal precedence cause all branches to fire on Debian systems (right-hand side of `&&` runs when left succeeds, right-hand side of `||` is skipped — but the next `&&` in the chain still runs). Always use `if which apt-get ...; then echo apt; elif which dnf ...; then echo dnf; fi` for unambiguous detection.
- **`lxc_parse.py` owns all regex extraction**: `parse_pct_config()`, `parse_pct_status()`, `parse_ct_script()`. Patterns are verbatim from `test_introspect_regex.py` and `test_detect_regex.py` — if a pattern changes, update both the parser and its parity test.

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
| `test_settings.py` | `GlobalSettings.load()`, field defaults (including node/manager fields), missing-file behaviour |
| `test_deps.py` | `validate_depends_order()` + `dependency_failed()` — mirrors `test_custom_depends.py` |
| `test_window.py` | `in_window()` — mirrors `test_check_window.py` case-for-case |
| `test_inventory.py` | `load_custom_hosts()` with `tmp_path` fixtures |
| `test_driver.py` | `run_custom_phase()` + `run_node_phase()` with monkeypatched `RunnerExecutor`; dep-abort, window skip, dry-run propagation, abort-on-first-failure, state JSON output |
| `test_orchestration.py` | `run_serial()` / `run_concurrent()` |
| `test_http.py` | HTTP helpers with monkeypatched urllib |
| `test_status_custom.py` | `custom_status()` — mirrors `test_custom_report.py` |
| `test_steps.py` | `run_steps()` — mirrors `test_run_step.py` |
| `test_flow_custom.py` | `run_custom_update()` end-to-end with fake executor |
| `test_status_lxc.py` | `lxc_app_status()`, `lxc_os_status()`, `lxc_rescue_app_status()`, `lxc_dry_run_status()`, `lxc_should_report()`, `parse_pct_config/status()`, `parse_ct_script()` — mirrors 6 existing Jinja test files |
| `test_flow_lxc.py` | `run_lxc_update()` end-to-end with `ScriptedLxcExecutor` (snapshot stubbed) |
| `test_status_vm.py` | `vm_status()`, `vm_rescue_status()`, `vm_should_report()` — mirrors `test_vm_report.py` |
| `test_flow_vm.py` | `run_vm_update()` end-to-end with `ScriptedVmExecutor` + `ScriptedNodeExecutor` (two-executor pattern) |
| `test_status_remote.py` | `remote_status()`, `remote_should_report()` |
| `test_flow_remote.py` | `run_remote_update()` end-to-end with `ScriptedRemoteExecutor` |
| `test_status_node.py` | `node_status()` (5 branches), `manager_status()` (3 branches), `node_should_report()` |
| `test_flow_node.py` | `run_node_update()` + `run_manager_update()` with `ScriptedNodeExecutor`; retry, reboot, proxy wait, manager-host skip |
| `test_briefing.py` | `render_briefing()` behavioural cases + a **golden parity** test asserting byte-equality with the live `discord_briefing.j2` via the Jinja shim; `prepare_body`/title/color/`should_notify` |
| `test_history.py` | `build_run_summary()` counts + the `briefing` field; `write_history()` write/prune/`latest.json` — mirrors `test_persist_history.py` |
| `test_notifiers.py` | `resolve_notifiers()` shim, `dispatch()` discord/ntfy payloads + headers, `ping_deadmans()` `/fail` suffix — mirrors `test_notify.py` |

`tests/conftest.py` implements Ansible-specific Jinja2 filters (`regex_search` list-return, `regex_replace`, `combine`, `intersect`) and tests (`failed`, `search`, `equalto`, `succeeded`) so templates render identically to Ansible. When writing new tests, use `render()` for string output and `render_native()` / `make_native_env()` (via `NativeEnvironment`) for Python objects.

**Molecule scenarios** (`roles/lxc_update/molecule/`) — the three CI-active scenarios (`normal`, `rollback`, `snapfail`) now drive `mol_run_flow.py` which builds a `MolLxcExecutor` and calls `run_lxc_update()` directly (same Python→ansible-runner→stub-shell stack as custom_update). The four non-CI scenarios (`template`, `stopped`, `dry_run`, `rescue`) still use the legacy role. **`roles/custom_update/molecule/`** scenarios all drive `mol_run_flow.py` → `run_custom_update()`. Each scenario's `converge.yml` invokes `mol_run_flow.py` with `ansible.builtin.command`; `verify.yml` loads the `dump_for_ansible()` JSON with `include_vars`. Idempotency checking is disabled for all scenarios — backup and update operations are intentionally non-idempotent.

**CI** (`.github/workflows/ci.yml`): `yamllint`, `ansible-lint`, `syntax-check`, `unit-tests` (pytest), plus two molecule matrices — `molecule-lxc-update` (normal, rollback, snapfail) and `molecule-custom-update` (normal, noop, rescue, dry_run, uptodate, per_step). Both molecule jobs now install `ansible-runner` and `pip install -e .` so `mol_run_flow.py` can import `proxmox_fleet`. The `ansible-lint` job excludes role task files that use dynamic `include_tasks` via `exclude_paths` in `.ansible-lint` (the `load-failure[not-found]` rule is unskippable).

### Jinja2 / Ansible patterns

- **`regex_search` with capture groups**: always use `(value | regex_search('pattern', '\\1') or [''])[0]`. Never use `| first` — `regex_search` returns Python `None` (not Jinja2 `Undefined`) on no match, so `| default([])` does not help and `| first` on `None` crashes.
- **Empty `>-` block → Python `None`**: a `set_fact` using a `>-` YAML block scalar whose Jinja2 evaluates to an empty string stores `None`, not `""`. Guard downstream with `{{ '' if var is none else var | trim }}`. The `discord_briefing.j2` OS field and `report.yml` `os:` payload both use this pattern.
- **Explicit `{{ '\n' }}` for newlines in templates**: do not rely on template-source newlines when `{%- -%}` tags are present — they strip adjacent whitespace including newlines. Use `{{ '\n' }}` (or `{{ '\n\n' }}` for blank lines) as explicit output that cannot be stripped by control-tag whitespace rules.
- **Discord embed markdown**: embed descriptions support `**bold**`, `*italic*`, `` `code` ``, `- ` bullet lists, and `\n` newlines. They do **not** support `>` blockquotes or `#` headers (those work only in regular messages, not webhook embeds).
