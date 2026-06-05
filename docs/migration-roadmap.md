# Migration Roadmap — Ansible → Python Control Plane

This document tracks the in-progress migration of `proxmox-management` to a hybrid
architecture: **Python owns all logic** (sequencing, gating, rescue/rollback, status
decisions, reporting, config/state schemas, manager-local IO); **Ansible is reduced to
single-purpose execution primitives** invoked via `ansible-runner`. It is the handoff
doc — a later session should be able to resume from here without further context.

## Guiding principle
> Use Ansible only for what it is good at — remote, privileged, multi-host execution over
> SSH and the Proxmox API. Use Python for everything else (all logic, orchestration,
> manager-local IO, and file writes).

- **Ansible (execution primitives, `ansible/primitives/*.yml`):** apt/dnf/apk, reboot,
  pct/qm/vzdump, `community.proxmox` snapshots, community-script runs, `pct pull`. Each is
  a thin task file that performs one action and returns results via `set_stats` — **no
  decisions, no `set_fact` status, no `block/rescue`, no branching `when:`**.
- **Python (`proxmox_fleet/`):** Kuma poll, GitHub lookups, HTTP health-check, Discord/ntfy
  POST, dead-man ping, TCP port waits, history files — all manager-local IO via stdlib
  `urllib`/`socket` (see `http.py`); plus every decision and the orchestration
  (`orchestration.py` = the `forks`/`serial`/`retries` equivalents).

## Conventions established (reuse these)
- **Executor boundary** (`proxmox_fleet/executor.py`): a flow is bound to one host and
  calls `run_shell` (target), `run_local` (manager), `reboot`, `snapshot` (proxmox API, delegated
  to localhost inside the primitive). `RunnerExecutor` invokes primitives; tests inject a scripted
  fake. New flows take an `Executor`. Phase 3 added `snapshot(vmid, *, snap_state, api_host, …)`;
  `api_host` must be the node's `ansible_host` IP, not the inventory name. `vmid` (not `lxc_id`)
  because `community.proxmox.proxmox_snap` handles both LXC containers and QEMU VMs.
  Phase 4a added a **two-executor pattern** for VMs: `executor` bound to the VM guest (SSH, for
  package upgrades), `node_executor` bound to the Proxmox node (SSH, for `qm rollback`/`qm status`).
  The driver resolves the current node via `pvesh get /cluster/resources` so HA migrations are
  handled automatically — `pve_node` in inventory is only a fallback hint.
- **Primitives** return values via `ansible.builtin.set_stats: { data: {...}, aggregate: false }`;
  `runner._harvest` reads `res['ansible_stats']['data']` into `PrimitiveResult.facts`.
- **`invoke_primitive` CWD contract**: `ansible_runner.run()` is called with
  `project_dir=os.getcwd()` so the subprocess CWD is the project root and the relative path
  `ansible/primitives/<name>.yml` resolves correctly. Without this, ansible-runner creates a
  temp dir and the playbook is never found. Every caller of `RunnerExecutor` must ensure
  CWD is the project root before creating the executor — production CLI does this naturally;
  `mol_run_flow.py` calls `os.chdir(_project_root)` at module load.
- **Status parity**: every `tmp_*` status string is reproduced in `status.py` and locked by a
  test that mirrors the old Jinja test case-for-case (e.g. `test_status_custom.py` ↔
  `test_custom_report.py`). Do the same for lxc/vm/node.
- **Eager-templating fix** lives in `steps.py`: command `{{ steps.NAME }}` refs are resolved
  in Python; all other `{{ }}` pass through to Ansible. Do NOT render full commands in Python.
- **Tests import plain Python** (no Jinja shim). Flows are tested with a fake executor +
  monkeypatched `http`. Keep the old Jinja-shim tests until the matching logic is the default,
  then delete them with `conftest.py` in Phase 5.
- **Inventory parsing** (`proxmox_fleet/inventory.py`): use the manual line-by-line parser,
  not `configparser` — `configparser` splits on the first `=` and mis-parses Ansible host
  lines of the form `hostname key=val key=val …`.
- **`FleetState.dump_for_ansible(path)`**: writes `fleet_*`-keyed JSON (reverse of `from_raw()`
  alias map). There are now four merge plays in `fleet-update.yml`: (a) "Merge Python remote state"
  between Phase 0 and Phase 0a, (b) "Merge Python custom state" between Phase 0b and Phase 1,
  (c) "Merge Python lxc state" between Phase 1 and Phase 1b, (d) "Merge Python VM state" between
  Phase 1b and Phase 2. All gate on `is defined` for their respective `fleet_*_state_path` extravar.
  Each must stay in its correct position — loading state after a later phase would OR-join against
  already-false flags.
- **`GlobalSettings`** (`proxmox_fleet/models/settings.py`): pydantic model for `vars.yml`;
  `extra="allow"` tolerates unknown keys. `load(path)` returns all defaults when the file is
  missing — safe for `--check` runs with no secrets file.

## Commands
```bash
pip install -e '.[dev]'        # package + mypy/pytest/pyyaml
pip install -e '.[runner]'     # + ansible-runner (manager / driver runs)
python -m mypy proxmox_fleet/
pytest tests/unit/ -v
```

---

## Status — ✅ migration complete

The hybrid migration is finished. **`driver.run_fleet()` is the end-to-end orchestrator**
the `fleet-update` CLI calls; the legacy `fleet-update.yml` monolith, the `roles/*/tasks`
+ `defaults`, the `tasks/*.yml` helpers, `templates/discord_briefing.j2`, and the
`tests/conftest.py` Jinja shim have all been removed. Ansible survives only as the
single-purpose execution primitives in `ansible/primitives/*.yml`. The implementation
notes below are retained as historical record.

| Phase | Scope | State |
|---|---|---|
| 0 | Package skeleton, runner, orchestration, http, cli, CI (mypy + pip install -e .) | ✅ done (`0dad3b8`) |
| 1 | pydantic `CustomConfig` + `FleetState` schemas | ✅ done (`0dad3b8`) |
| 2 | `custom_update` → `flows/custom.py` (+ `status.py`/`changes.py`); default; legacy role + shim tests deleted | ✅ done |
| 3 | `lxc_update` → `flows/lxc.py` + primitives; default; legacy role + shim tests deleted | ✅ done |
| 4a | `vm_update` + `remote_host_update` → `flows/{vm,remote}.py`; default; legacy roles deleted | ✅ done |
| 4b | node OS update + manager self-update → `flows/node.py`; default | ✅ done |
| 5 | Briefing/history/notifiers in Python (byte-parity, golden-test gated); monolith removed; `conftest.py` + `.j2` deleted | ✅ done |

What exists now: `proxmox_fleet/{__init__,cli,__main__,runner,orchestration,http,steps,executor,status,changes,deps,driver,inventory,window,lxc_parse,briefing,history,notifiers}.py`,
`proxmox_fleet/models/{config,state,settings}.py`, `proxmox_fleet/flows/{custom,lxc,vm,remote,node}.py`,
`ansible/primitives/{run_shell,reboot_host,discover_lxcs,pct_config,pct_status,pct_start,pct_stop,pct_pull,snapshot,rollback,vzdump,lxc_os_update,lxc_app_update,lxc_introspect,lxc_post_update}.yml`,
`roles/{custom_update,lxc_update}/molecule/mol_run_flow.py` (flow-driven molecule scenarios only).
The CLI is `driver.run_fleet()` driven by either `./fleet-update.py` (recommended human-facing
wrapper with friendly flags + venv auto-bootstrap) or the `fleet-update` console command
(`fleet-update [--check] [-e key=val ...]`). The `--use-*-flow` flags and the per-phase merge
plays are gone. All 15 primitives are now invoked via typed `Executor` methods — no inline
`run_shell` strings remain for the LXC flow's main operations. 460+ plain-Python tests green
across Python 3.10/3.11/3.12, mypy clean. The golden briefing parity is now locked by
`tests/unit/data/briefing_golden.json` (captured from the retired `.j2`) rather than the live
Jinja shim.

---

## Phase 2-wire — implementation notes (🔶 wired, retire pending)

Goal was to route Phase 0b through `flows/custom.py` behind a flag, prove parity via molecule,
then retire the role. The wiring is done; the retire step waits for a real `--check` run to
confirm parity on live infrastructure.

### What was built

- **`proxmox_fleet/models/settings.py`** — `GlobalSettings` pydantic model for `vars.yml`;
  `model_config = ConfigDict(extra="allow")` so unknown keys from `vars.yml` are tolerated.
  `GlobalSettings.load(path)` silently uses all defaults when the file is missing.
- **`proxmox_fleet/inventory.py`** — `load_custom_hosts()` parses `[custom_hosts]` from
  `hosts.ini` and merges `host_vars/<host>.yml`. Uses manual line-by-line regex rather than
  `configparser` — configparser splits on the first `=` which mis-parses
  `hostname key=val key=val …` host lines (turns `hostname key` into the config key).
- **`proxmox_fleet/deps.py`** — `validate_depends_order()` (port of Phase-0a `_dep_problems`)
  and `dependency_failed()` (port of `_dep_failed`).
- **`proxmox_fleet/window.py`** — `in_window()` (port of `tasks/check-window.yml`). Uses
  `from zoneinfo import ZoneInfo` directly — the project requires Python ≥ 3.9 so the
  `backports.zoneinfo` fallback is dead code and must not be added.
- **`proxmox_fleet/driver.py`** — `run_custom_phase(settings, inventory, ...)`: loads hosts,
  validates dep order (SystemExit on problems), serial loop with window gate → dep gate →
  `run_custom_update()` → fold outcome into `FleetState`; calls
  `state.dump_for_ansible(state_output_path)` at the end.
- **`proxmox_fleet/models/state.py`** — added `dump_for_ansible(path)` which writes
  `fleet_*`-keyed JSON (the reverse of `from_raw()`'s alias map) for the Ansible merge play.
- **`proxmox_fleet/cli.py`** — `--use-custom-flow` + `--vars-file` flags. When set, calls
  `driver.run_custom_phase()` then sets `skip_phase_0b=true` and `fleet_custom_state_path`
  as extravars for the playbook.
- **`fleet-update.yml`** — two surgical edits: (a) Phase 0b block gated on
  `when: not (skip_phase_0b | default(false) | bool)`; (b) a "Merge Python custom state into
  fleet" play inserted **between Phase 0b and Phase 1** so `fleet_changed`/`fleet_failed` are
  seeded before Phases 1–3 run `fleet-state-append.yml` and OR-join against them.
- **`roles/custom_update/molecule/mol_run_flow.py`** — shared converge helper. Loads config,
  builds `RunnerExecutor(host, inventory=...)`, calls `run_custom_update()`, writes
  `dump_for_ansible()` output. All six scenario `converge.yml` files drive this script.
- **Six `prepare.yml`** files — added `pip install -e .` + write `/tmp/mol_hosts.ini` with
  `ansible_connection=local`.
- **`.github/workflows/ci.yml`** — `molecule-custom-update` job installs `ansible-runner` and
  `pip install -e .` so `mol_run_flow.py` can import `proxmox_fleet` and `RunnerExecutor` works.

### Key pitfalls discovered

- **`invoke_primitive` must pass `project_dir`**: without an explicit `project_dir`, ansible-runner
  creates a fresh `tempfile.mkdtemp()` as `private_data_dir` and looks for the playbook at
  `<tempdir>/project/ansible/primitives/run_shell.yml` — which never exists. The fix is
  `project_dir=os.getcwd()` in `ansible_runner.run()`; callers must ensure CWD is the project
  root. `mol_run_flow.py` does `os.chdir(_project_root)` at module load; the production CLI runs
  from the project directory by convention.
- **Merge play timing**: loading the Python-produced fleet state in Phase 4 pre_tasks would cause
  Phases 1–3 fleet-state-append calls to OR-join against `false`/`[]` (the Ansible-default
  initial values), silently dropping the Python driver's `fleet_changed`/`fleet_failed` flags.
  The merge play must come **before Phase 1**.
- **Block naming for ansible-lint**: a `block:` task must have a `name:` and `when:` must appear
  before `block:` in key order (`key-order[task]`/`name[missing]` rules).
- **`types-PyYAML` stub**: mypy raises `import-untyped` for `import yaml` unless `types-PyYAML`
  is in the dev extras.

### Retire step (after real-run parity is confirmed)

```bash
# Run with the flag and compare results against a flag-off run on the same target:
fleet-update --use-custom-flow --check -e fleet_dry_run=true
fleet-update --check -e fleet_dry_run=true
# Confirm fleet_custom_data, fleet_changed, fleet_failed match.

# Then delete:
rm -rf roles/custom_update/tasks/ roles/custom_update/defaults/
rm tests/unit/test_custom_report.py tests/unit/test_run_step.py tests/unit/test_custom_depends.py
# Flip --use-custom-flow to the unconditional default in cli.py and remove the flag.
```

---

## Phase 3 — `lxc_update` (🔶 wired, retire pending)

Move `roles/lxc_update/tasks/*` into `flows/lxc.py`. The per-container `block/rescue/always`
becomes Python `try/except/finally`.

### What was built

- **`proxmox_fleet/lxc_parse.py`** — `parse_pct_config()`, `parse_pct_status()`,
  `parse_ct_script()`, `script_name_from_update()`. All regex patterns copied verbatim from
  `test_introspect_regex.py` and `test_detect_regex.py` — parity locked there.
- **`proxmox_fleet/status.py`** — `lxc_app_status()` (11-branch `tmp_app`), `lxc_os_status()`
  (`tmp_os`), `lxc_rescue_app_status()` (rollback string), `lxc_should_report()` (when gate),
  `lxc_dry_run_status()` (dry-run status string).
- **`proxmox_fleet/changes.py`** — `lxc_os_changed()`, `lxc_os_pkg_count()`, `dpkg_hash_differs()`.
- **`proxmox_fleet/executor.py`** — `snapshot(lxc_id, *, snap_state, api_host, …)` added to
  Protocol + RunnerExecutor. Invokes `snapshot.yml` which runs `community.proxmox.proxmox_snap`
  on localhost. `api_host` = node's `ansible_host` IP (not inventory name).
- **`proxmox_fleet/flows/lxc.py`** — `run_lxc_update(node, lxc_id, executor, settings, *, dry_run,
  api_host)`. Full `try/except/finally` mirroring `block/rescue/always`:
  introspect (outside try, fail-loud) → detect → dry_check → backup → update → health → report /
  rescue (rollback if snap_taken) / always (delete snapshot; stop if was_stopped and not rolled back).
  `_discover_lxcs(executor, settings)` does the tag-filter shell loop.
- **`proxmox_fleet/driver.py`** — `run_lxc_phase(settings, inventory_path, …)`: serial over
  `load_proxmox_nodes()`, concurrent per node via `run_concurrent(max_workers=lxc_forks)`, folds
  `LxcFlowOutcome` into `FleetState`, writes `dump_for_ansible()`.
- **`proxmox_fleet/inventory.py`** — `load_proxmox_nodes()` added; parses `[proxmox_nodes]`.
- **`proxmox_fleet/models/settings.py`** — LXC fields added: `lxc_dry_run`, `lxc_auto_reboot`,
  `lxc_unattended`, `lxc_backup_strategy/storage`, `lxc_tags`, `lxc_forks`, `lxc_kuma_map`,
  `exclude_list`, `os_update_exclude_list`, `snapshot_exclude_list`, `pve_api_*`.
- **`proxmox_fleet/cli.py`** — `--use-lxc-flow` flag. Calls `driver.run_lxc_phase()`, writes
  `/tmp/fleet_lxc_state.json`, passes `skip_phase_1=true` + `fleet_lxc_state_path` to playbook.
- **`fleet-update.yml`** — two surgical edits: (a) Phase 1 block gated on `skip_phase_1`; (b)
  "Merge Python lxc state" play inserted between Phase 1 and Phase 1b.
- **11 primitives** in `ansible/primitives/`: `discover_lxcs.yml`, `pct_config/status/start/stop/
  pull.yml`, `snapshot.yml` (single file, `snap_state=present|absent`), `rollback.yml`,
  `vzdump.yml`, `lxc_os_update.yml`, `lxc_app_update.yml` (includes `/tmp/.nc/clear` trick +
  resource scaling).
- **`roles/lxc_update/molecule/mol_run_flow.py`** — `MolLxcExecutor(RunnerExecutor)` overrides
  `snapshot()` with a touch-file stub (no PVE API needed in molecule). Three CI scenarios
  (`normal`, `rollback`, `snapfail`) reworked to drive this; four non-CI scenarios still use
  the legacy role.
- **`tests/unit/test_status_lxc.py`** — 90 parity tests mirroring `test_report_tmp_app.py`,
  `test_report_tmp_os.py`, `test_report_when_condition.py`, `test_dry_check_status.py`,
  `test_detect_regex.py`, `test_introspect_regex.py`.
- **`tests/unit/test_flow_lxc.py`** — 14 integration tests with `ScriptedLxcExecutor`.

### Key pitfalls discovered

- **`"FAILED (NO SNAPSHOT)"` vs `"FAILED"`**: `snapshot_failed=True` only when a snapshot was
  explicitly requested AND `snap_res.changed=False` (API returned no-op). When
  `backup_strategy=none` (no snapshot attempted at all), `snapshot_failed` stays False → plain
  `"FAILED"`. Tests must distinguish these two scenarios.
- **dpkg hash is a fallback, not a parallel check**: when both `ver_before` and `ver_after` are
  non-empty, version comparison wins and dpkg hash is never consulted. Hash only fires when
  there is no version file (empty cat output from the container).
- **Reboot check is inside the `not lxc_no_update_script` guard**: the `/var/run/reboot-required`
  check is only run when `pct pull` succeeded (container has an update script). A container with
  no update script never gets a reboot check.
- **`snapshot.yml` runs `hosts: localhost`** (not the node): `community.proxmox.proxmox_snap`
  speaks the PVE API directly. `RunnerExecutor.snapshot()` passes no `host_pattern` — the
  primitive hardcodes localhost. This differs from all other lxc primitives which target the node.
- **`lxc_app_update.yml` is not single-action**: it does scale-up → update → scale-down. This is
  an accepted deviation because the three steps must be atomic from the control-plane's perspective
  (all-or-nothing with respect to resource state). Python cannot call three separate primitives
  here without risking orphaned scaled-up resources on failure.

### Retire step (after real-run parity is confirmed)

```bash
# Run with the flag and compare results against a flag-off run:
fleet-update --use-lxc-flow --check -e fleet_dry_run=true
fleet-update --check -e fleet_dry_run=true
# Confirm fleet_lxc_data, fleet_changed, fleet_failed match.

# Then delete:
rm -rf roles/lxc_update/tasks/ roles/lxc_update/defaults/
rm tests/unit/test_report_tmp_app.py tests/unit/test_report_tmp_os.py \
   tests/unit/test_report_when_condition.py tests/unit/test_dry_check_status.py \
   tests/unit/test_detect_regex.py tests/unit/test_introspect_regex.py
# Flip --use-lxc-flow to the unconditional default in cli.py and remove the flag.
```

### Live-run testing (2026-06-01, `testing` branch)

Full live runs against 5 Proxmox nodes (ONeill, Carter, Tealc, Jackson, Hammond), 20 tagged LXCs.
Testing environment: manager LXC CTID 121 on node 10.10.10.44, `/root/test/proxmox-management`.

**Bugs fixed during testing:**
- `[proxmox_nodes:vars]` parsed as host entries — `load_proxmox_nodes()` was stripping `:vars` suffix via `.split(":")[0]`, keeping `in_section=True` and treating `ansible_user=root` etc. as node names. Fixed to compare full section name.
- Discovery shell (`pct list | awk 'NR>1 {print $1}' | while ...`) returning 0 containers — two root causes: (a) `awk`'s `$1` expanded by shell before awk saw it through extravars/Jinja2/shell quoting layers; fixed by replacing awk with `tail -n +2 | while read vmid rest`. (b) `grep -q ... && echo "$id"` exits rc=1 when last container doesn't match, causing `_harvest()` to discard stdout; fixed with `if/fi` so loop always exits 0.
- Discovery running with `check=True` — shell commands don't execute remotely in check mode; fixed with a dedicated `discovery_executor` with `check=False`.
- `ansible_runner.run()` in `cli.py` missing `project_dir` — relative `inventory="hosts.ini"` didn't resolve when ansible-runner used a tempdir, causing `proxmox_nodes` group to show "no hosts matched". Fixed with `inventory=str(Path(args.inventory).resolve())` (same as `invoke_primitive`).
- `_harvest()` discarding stdout from failed tasks — added stdout collection from `runner_on_failed` events as defence-in-depth.
- `project_dir` duplicated in `cli.py` after merge of parallel fix — removed duplicate.

**Verified working:**
- All 20 containers discovered and processed correctly across 5 nodes.
- OS updates execute inside containers (`pct exec ... apt dist-upgrade`).
- App update scripts (`/usr/bin/update`) run and produce output.
- Version before/after comparison works — caught nginxproxymanager `2.14.0 → 2.15.0` update on Tealc/123.
- `lxc_verbose=true` (`-e lxc_verbose=true`) prints per-step diagnostics: script name, snapshot result, os_update stdout, app_update stdout, ver before→after, dpkg diff.
- Concurrent execution confirmed — output for multiple containers on the same node interleaves correctly.

**Known gaps / open issues:**
- **Python driver is slower than the legacy Ansible role** despite concurrent per-container execution. Root cause: each `executor.run_shell()` call spawns a new `ansible-runner` subprocess (~12–15 per container), each paying full Ansible framework load + new SSH connection cost. The legacy role is one long-running `ansible-playbook` with `pipelining=true` and one SSH connection per node reused across all tasks. See future TODO below.
- **Snapshot "CT is locked (snapshot-delete)"** — Hammond/106 uptimekuma snapshot failed with this error. Caused by a Proxmox task lock from a prior snapshot-delete (from an earlier test run) still held when the new snapshot was attempted. No retry logic exists yet; the flow records a warning and continues without rollback capability.
- **Containers with no version file** (`technitiumdns`, `plex`, `apt-cacher-ng`, `proxmox-backup-server`) — `ver: '' → ''`; change detection falls back to dpkg hash. This is correct behaviour per the status decision tree.

**Speed optimisation: ✅ done (2026-06-05).**
Two batched read primitives (`lxc_introspect.yml`, `lxc_post_update.yml`) were added and all
LXC flow operations wired to dedicated `Executor` methods — subprocess spawns reduced from
~15 to ~7–8 per container. See backlog item #1 above for full details.

---

## Phase 4a — vm_update + remote_host_update (🔶 wired, retire pending)

Move `roles/vm_update/tasks/*` and `roles/remote_host_update/tasks/*` into `flows/vm.py` and
`flows/remote.py`. The per-VM `block/rescue/always` becomes Python `try/except/finally`.

### What was built

- **`proxmox_fleet/flows/vm.py`** — `run_vm_update(node, vmid, inventory_hostname, executor,
  node_executor, settings, *, dry_run, api_host) -> VmFlowOutcome`. Full `try/except/finally`:
  backup (vzdump / snapshot) → detect pkg mgr → upgrade → reboot check → health check → report /
  rescue (`qm rollback` via `node_executor` if snapshot taken; `rollback_done` set only when
  rollback succeeds AND `qm status` confirms "running") / always (delete snapshot).
  `pkg_count` extracted from upgrade stdout and stored on `VmRecord` for the briefing.
- **`proxmox_fleet/flows/remote.py`** — `run_remote_update(hostname, executor, settings, *,
  dry_run, pre_update_cmd) -> RemoteFlowOutcome`. Simpler — no snapshot, no always block.
  Rescue records plain `FAILED`.
- **`proxmox_fleet/changes.py`** — `vm_pkg_count(stdout, pkg_mgr)` added: extracts upgraded
  package count from apt (`N upgraded`), dnf (`Upgrade N Packages`), and apk (`Upgrading ` lines).
- **`proxmox_fleet/models/state.py`** — `VmRecord.pkg_count: Optional[int]` added.
- **`proxmox_fleet/models/settings.py`** — vm_* and remote_* fields added (matching role defaults).
- **`proxmox_fleet/inventory.py`** — `load_proxmox_vms()` and `load_remote_hosts()` added.
- **`proxmox_fleet/status.py`** — `vm_status()`, `vm_rescue_status()`, `vm_should_report()`,
  `remote_status()`, `remote_rescue_status()`, `remote_should_report()` added.
- **`proxmox_fleet/driver.py`** — `run_vm_phase()` and `run_remote_phase()` added. VM phase
  runs `pvesh get /cluster/resources` once per phase on the first available node to discover
  current VM locations — live cluster state wins over static `pve_node` inventory hint.
- **`proxmox_fleet/cli.py`** — `--use-vm-flow` (routes Phase 1b; sets `skip_phase_1b=true` +
  `fleet_vm_state_path`) and `--use-remote-flow` (routes Phase 0; sets `skip_phase_0=true` +
  `fleet_remote_state_path`) flags added.
- **`fleet-update.yml`** — four surgical edits: Phase 0 gated on `skip_phase_0`; "Merge Python
  remote state" play inserted after Phase 0; Phase 1b gated on `skip_phase_1b`; "Merge Python VM
  state" play inserted after Phase 1b.
- **`templates/discord_briefing.j2`** — LXC and VM items now grouped under `- LXC` / `- VM`
  sub-headings per node; VM format aligned with LXC style (`name (id) — status`); `pkg_count`
  shown on real runs (`UPDATED (3 upgraded)`), suppressed on dry-runs.
- **Tests** — `test_status_vm.py` (16), `test_status_remote.py` (12), `test_flow_vm.py` (14),
  `test_flow_remote.py` (13) added.

### Key pitfalls discovered

- **`&&`/`||` bash precedence bug** in package manager detection: `which apt-get && echo apt || which dnf && echo dnf`
  has equal-precedence operators — on a Debian system all three `echo` branches fire, `reversed()`
  scan returns `apk`, and `apk -s upgrade` fails silently. Fix: use `if/elif/else` syntax.
- **`node_executor` for `qm` commands**: `qm rollback` and `qm status` must run on the Proxmox
  node, not the VM guest. Using a single executor for both caused rollback to SSH into the VM and
  fail. The driver creates separate `vm_ex` and `node_ex` executors; flow tests assert `qm` commands
  never appear in `vm_ex.commands`.
- **`rollback_done` truthfulness**: only set `rollback_done=True` when `qm rollback` succeeds AND
  `qm status` confirms "running". Checking only the rollback command exit code produces false
  "FAILED + ROLLED BACK" entries when the VM is still coming up.
- **HA migration**: `pve_node` in inventory goes stale when HA migrates a VM. The driver calls
  `pvesh get /cluster/resources --type vm` once per phase to build a live `{vmid: (node, api_host)}`
  map. Falls back to inventory `pve_node` when pvesh is unavailable (standalone nodes).
- **`pkg_count` suppressed in dry-run**: `apt-get -s` reports "N upgraded" for pending packages,
  not actually-upgraded packages. Showing `(29 upgraded)` on `WOULD UPDATE` was misleading.
  `pkg_count` is only populated when `changed and not dry_run`.

### Retire step (after real-run parity is confirmed)

```bash
# Run with the flags and compare results against a flag-off run on the same target:
fleet-update --use-vm-flow --use-remote-flow --check -e fleet_dry_run=true
fleet-update --check -e fleet_dry_run=true
# Confirm fleet_vm_data, fleet_remote_data, fleet_changed, fleet_failed match.

# Then delete:
rm -rf roles/vm_update/tasks/ roles/vm_update/defaults/
rm -rf roles/remote_host_update/tasks/ roles/remote_host_update/defaults/
rm tests/unit/test_vm_report.py
# Flip --use-vm-flow / --use-remote-flow to unconditional defaults in cli.py and remove the flags.
```

---

## Phase 4b — node OS update + manager self-update (🔶 wired, retire pending)

Ports Phase 2 (node OS update, serial reboot loop) and Phase 3 (manager LXC self-update)
from `fleet-update.yml` into Python flows. Behind `--use-node-flow` until real-run parity
is confirmed.

### What was built

- **`proxmox_fleet/flows/node.py`** — `run_node_update(node, executor, settings, *, dry_run)`
  and `run_manager_update(executor, settings, *, dry_run)`. Full `try/except` mirroring
  `block/rescue`: is_manager check → apt upgrade (5 retries via `orchestration.retry`) →
  robust reboot check (reboot-required file OR kernel mismatch) → reboot (if not-manager,
  not dry-run) → `http.wait_for_port` (apt proxy) + 15 s settle → `node_status()` /
  `manager_status()`. Manager never reboots. `NodeFlowOutcome` dataclass.
- **`proxmox_fleet/status.py`** — `node_status()` (5 branches, parity with Phase 2 Jinja),
  `manager_status()` (3 branches, parity with Phase 3 ternary), `node_should_report()` (always
  True — nodes always appear in the briefing, no idle suppression).
- **`proxmox_fleet/models/settings.py`** — `manager_lxc_id`, `apt_proxy_ip`, `apt_proxy_port`,
  `node_dry_run`, `node_auto_reboot` fields added.
- **`proxmox_fleet/driver.py`** — `_fold_node_outcome()` + `run_node_phase()`: serial node loop
  with abort-on-first-failure (any_errors_fatal equivalent), then manager update runs
  unconditionally. Writes `/tmp/fleet_node_state.json`.
- **`proxmox_fleet/cli.py`** — `--use-node-flow` flag. Calls `driver.run_node_phase()`, sets
  `skip_phase_2=true`, `skip_phase_3=true`, `fleet_node_state_path` as extravars.
- **`fleet-update.yml`** — three surgical edits: (a) "Merge Python node state" play inserted
  between "Merge Python VM state" play and Phase 2; (b) Phase 2 "Node Maintenance Block" gated
  on `not (skip_phase_2 | default(false) | bool)`; (c) Phase 3 tasks wrapped in "Manager
  Self-Update Block" gated on `not (skip_phase_3 | default(false) | bool)`.
- **Tests** — `test_status_node.py` (15) + `test_flow_node.py` (16) + driver tests in
  `test_driver.py` (6 new: happy path, dry-run, abort-on-failure, empty inventory, state JSON keys)
  + settings tests in `test_settings.py` (2 new: defaults + YAML load for all 5 new fields).
  No molecule scenario needed — Phase 2/3 were inline in the playbook, not a role.
- **`reboot_host.yml`** — `check_mode: false` added to the reboot task (matching `run_shell.yml`).
  Python controls dry-run by choosing the command; Ansible check mode is bypassed at the primitive
  level. The node flow additionally guards `executor.reboot()` with `not dry_run` so it never fires
  during dry-run regardless.
- **`vm_apt_res` register-overwrite bug** found during parity testing: legacy `vm_update` role
  silently drops VMs from dry-run notifications because the skipped "Update VM packages" task
  overwrites `vm_apt_res` with `{skipped: true, changed: false}`. Real runs are unaffected.
  Python driver (`--use-vm-flow`) is immune. Documented in CLAUDE.md key non-obvious details.

### Key pitfalls

- **Retry uses `_sleep` injection**: `orchestration.retry` takes a `sleep` kwarg; `run_node_update`
  accepts `_sleep` and threads it through so tests never block on the 30 s retry delay.
- **Reboot detection uses stdout, not rc**: The robust reboot check outputs "reboot" or "ok" to
  stdout. `reboot_needed = "reboot" in res.stdout`. Manager reboot check uses
  `test -f /var/run/reboot-required && echo reboot || echo ok` — same stdout pattern.
- **Manager abort does NOT stop Phase 3**: the driver `break` stops only the node loop;
  `run_manager_update()` executes unconditionally after. Phase 3 is `ignore_errors` in Ansible
  and is independent of Phase 2 failures.
- **No new Ansible primitives**: node apt runs via existing `run_shell.yml`; reboot via
  `reboot_host.yml`. PVE nodes are always Debian/apt — no pkg_mgr detection step needed.

### Retire step (after real-run parity confirmed)

```bash
fleet-update --use-node-flow --check -e fleet_dry_run=true
fleet-update --check -e fleet_dry_run=true
# Confirm fleet_node_data, fleet_changed, fleet_failed match.

# Then flip --use-node-flow to the unconditional default in cli.py and remove the flag.
# No legacy role tasks to delete (Phase 2/3 were inline in the playbook).
# Gate skip_phase_2/skip_phase_3 permanently to true, then remove Phase 2/3 plays.
```

---

## Phase 5 — briefing / history / notifiers (🔶 wired, retire pending)

Phase 4 (the final briefing) is ported to Python behind `--use-notify-flow`. The notify
phase runs **after** the playbook because it consumes the *merged* fleet state, not a single
phase's output: when the flag is set, `cli.py` adds `skip_phase_4=true` +
`fleet_final_state_path` extravars; the playbook's Phase 4 then dumps the merged `fleet_*`
facts to JSON instead of briefing; after `ansible_runner.run()` returns, `cli.py` loads that
JSON into a `FleetState` and calls `driver.run_notify_phase()`.

### What was built

- **`proxmox_fleet/briefing.py`** — `render_briefing(state) -> str` (byte-parity port of
  `templates/discord_briefing.j2`), `prepare_body()` (`strip()` + `truncate(4000, False,
  '\n...')`), `briefing_title()` / `ntfy_title()` / `discord_color()` / `should_notify()`
  (ports of the Phase-4 `set_fact`). No trailing newline — Jinja's
  `keep_trailing_newline=False` strips the template's final `\n` (matched in the golden test).
- **`proxmox_fleet/history.py`** — `build_run_summary()` + `write_history()` (port of
  `tasks/persist-history.yml`). **New `briefing` field** records the exact rendered Discord
  body in `run-<ts>.json` + `latest.json`. `json.dump(indent=4, sort_keys=True)` ≈
  `to_nice_json`; prune by lexical timestamp sort.
- **`proxmox_fleet/notifiers.py`** — `resolve_notifiers()` (back-compat `discord_webhook`
  shim; `settings.notifiers` is `Optional` and defaults to `None` so an explicit `[]` is
  distinguishable from unset), `dispatch()` (Discord embed via `http.post_json`; ntfy via
  `http.request` with the same header logic as `notify.yml`), `ping_deadmans()`. All errors
  swallowed (mirrors `ignore_errors: yes`).
- **`proxmox_fleet/driver.py`** — `run_notify_phase(settings, state, *, check)`: renders the
  body once, dispatches when `should_notify`, writes history (carrying the body) when enabled,
  pings the dead-man. Body is rendered unconditionally so history records it even when
  notification is suppressed.
- **`proxmox_fleet/models/settings.py`** — Phase-4 fields added: `notifiers` (Optional),
  `discord_webhook`, `fleet_deadmans_url`, `fleet_history_enabled/dir/keep`, `force_notify`.
- **`proxmox_fleet/cli.py`** — `--use-notify-flow` flag; folds `-e force_notify=true` into
  settings; dumps/loads `/tmp/fleet_final_state.json`; notify runs regardless of playbook rc
  (a failure briefing must fire on failure), gated on the dump existing.
- **`fleet-update.yml`** — Phase 4 wrapped in a `when: not skip_phase_4` block + a
  `when: skip_phase_4` "Dump merged fleet state" task.
- **Tests** — `test_briefing.py` (behavioural + a **golden parity** test rendering the same
  fixtures through both the Jinja shim and `render_briefing()` byte-for-byte),
  `test_history.py`, `test_notifiers.py`, + `run_notify_phase` cases in `test_driver.py`.

### Retire step (after real-run parity is confirmed, and once the other four flows are default)

```bash
fleet-update --use-notify-flow --check -e fleet_dry_run=true -e force_notify=true
# Compare the posted Discord/ntfy body byte-for-byte against a flag-off run.

# Then:
# - Flip --use-notify-flow to the unconditional default in cli.py and remove the flag.
# - Delete templates/discord_briefing.j2, tasks/notify.yml, tasks/persist-history.yml.
# - Delete tests/conftest.py's Jinja shim + the parity tests that import it
#   (test_discord_briefing.py, test_notify.py, test_persist_history.py, and the remaining
#    test_*_report.py / regex shims listed per-phase above).
# - Port tests/integration/test_fleet_state_append_logic.py into tests/unit/test_state_model.py.
# - Collapse the Phase 4 plays out of fleet-update.yml (endgame monolith split).
```

### Split the monolith (endgame)

Per-phase playbooks the driver invokes in order, then the driver becomes the orchestrator end
to end and `fleet-update.yml` is removed. Do this only once all flows (incl. notify) are the
unconditional default.

---

## Post-migration backlog

Items discovered after the migration "completed". Listed roughly by value.
✅ = shipped; ⬜ = still open.

### ✅ 1. Execution primitives wired + subprocess consolidation (shipped `testing` branch, 2026-06-05)

All primitives are now invoked via `executor.py` — no more inline `run_shell` strings
for the LXC flow operations. Two new batched read primitives reduce subprocess spawns
from ~15 to ~7–8 per container.

**New primitives:**
- `ansible/primitives/lxc_introspect.yml` — batches `pct config` + `pct status` +
  `pct pull /usr/bin/update` + `cat` script content into **one** subprocess; returns
  `config_stdout`, `status_stdout`, `pull_rc`, `script_stdout` via `set_stats`.
- `ansible/primitives/lxc_post_update.yml` — batches dpkg/apk hash read + version file
  read after the update into **one** subprocess; returns `dpkg_hash_after`, `version_after`.

**New `Executor` protocol methods + `RunnerExecutor` implementations:**
`introspect()`, `vzdump()`, `lxc_os_update()`, `lxc_app_update()`, `post_update()`,
`pct_rollback()`, `pct_start()`, `pct_stop()`.

**`flows/lxc.py` rewrite:** introspect block, detect (pull/cat/rm-f), vzdump backup,
OS update, app update + resource scaling, post-update reads, rollback, and container
stop all converted to dedicated method calls. The remaining `run_shell` calls are:
`ver_before` (script name unknown until after detect), `dpkg_before` (runs between OS
and app update), and the post-start status re-check (conditional one-liner).

**Robustness & quality work shipped in the same batch:**
- `tests/unit/test_changes.py`, `test_cli.py`, `test_runner.py`, `test_executor.py`
  — new direct unit-test files covering previously untested modules; all edge cases
  for regex-based change detection (`changes.py`), CLI flag propagation (`cli.py`),
  runner event harvesting (`runner.py`), and executor extravars/fact-merge logic.
- `tests/unit/test_orchestration.py` augmented: `run_concurrent` falls back to serial
  when workers ≤ 1; `retry` with empty exceptions propagates immediately; retries=0
  means exactly 1 call.
- `tests/unit/test_flow_custom.py` augmented: `type=command` health-check branch
  (passes on rc=0, triggers rescue on rc=1).
- `tests/unit/test_settings.py` augmented: new timeout/retry field defaults verified.
- `orchestration.run_concurrent()` now accepts `timeout: Optional[float] = None`;
  `future.result(timeout=timeout)` prevents hung SSH from blocking the thread pool
  forever (default None = existing behaviour).
- `driver._discover_vm_locations()`: silent `except Exception: return {}` now prints
  a stderr warning before returning so the failure is visible without aborting.
- `GlobalSettings` gains 8 configurable timeout/retry fields:
  `apt_proxy_check_timeout`, `node_reboot_port_wait_timeout`, `snapshot_retries`,
  `snapshot_retry_delay`, `notifier_retries`, `deadmans_retries`, `node_apt_retries`,
  `node_apt_retry_delay`. All callers wired.
- CI: `unit-tests` job expanded to Python 3.10/3.11/3.12 matrix with
  `--cov=proxmox_fleet --cov-report=term-missing`; new `bandit -r proxmox_fleet/ -ll`
  security scan job.

### ✅ 2. `MaintenanceWindow` model wired
`inventory.py` now parses `maintenance_window` host_vars dicts into typed
`MaintenanceWindow` objects at load time — invalid keys raise `ValidationError`
immediately. `window.in_window` accepts `MaintenanceWindow` or plain `dict` (converts
to dict internally via `.model_dump()`). Tests updated in `test_inventory.py` and
`test_window.py`.

### ✅ 3. `lxc_parse.script_name_from_update()` wired
`flows/lxc.py` now `cat`s the pulled update script on the node and calls
`lxc_parse.script_name_from_update(content)` in Python, replacing the previous
`grep -oP 'ct/\K[^.]+(?=\.sh)'` node-side grep. Logic is in Python; no dependency
on Perl-regex grep availability. Test stubs in `test_flow_lxc.py` updated from
`"grep"` key to `"cat /tmp/ansible_update_"` key.

### ✅ 4. `changes.dpkg_hash_differs()` wired
`status.lxc_app_status()` now calls `changes.dpkg_hash_differs()` instead of
inlining the same comparison. One source of truth; no behaviour change.

### ✅ 5. Snapshot lock retry
`executor.snapshot_with_retry()` added as a module-level free function shared by
both `flows/lxc.py` and `flows/vm.py`. Wraps `orchestration.retry()` with
`retries=3, delay=15.0` and `until=changed` (create) / `until=not failed` (delete).
Returns a failed `PrimitiveResult` on exhaustion so existing warning/fallback paths
apply unchanged. `_sleep` is injectable for fast unit tests. Both snapshot create and
delete call sites in LXC and VM flows replaced. New tests: `test_snapshot_with_retry_*`
in `test_flow_lxc.py` (two cases) and `test_flow_vm.py` (one case).

### ✅ Minor: `window.in_window` tz-aware datetime
`window.in_window` now adds `else: now = now.astimezone(tz)` so a tz-aware `now` in
a foreign timezone is converted before day/time comparison. Previously only naive
datetimes were localised. New test: `test_tz_aware_now_in_different_tz_is_converted`.

### ✅ 6. Human-friendly CLI wrapper (`fleet-update.py`)
`fleet-update.py` added at the repo root as the recommended daily-driver interface.
Exposes `--dry-run` (sets both `check=True` and `fleet_dry_run=True`), `--force-notify`,
`--verbose`, and `--force-window` as proper flags instead of `-e KEY=VALUE` strings. Auto-bootstraps
into `.venv/bin/python` via `os.execv` so activation is not required. Keeps `-e KEY=VALUE` for
uncommon vars. Calls `driver.run_fleet()` directly (no double-parse through `cli.main`). Friendly
flags are applied last so they always win over any conflicting `-e` value.

Also fixed a latent bug in `cli.py`: `-e force_window=true` was not propagated into `settings`,
so `run_vm_phase` and `run_remote_phase` (which read `settings.force_window`) silently ignored
the flag. A fourth propagation block was added alongside the existing three.

CI additions: `ruff check fleet-update.py` added to the ruff job; new `lint-wrapper` job runs
`python3 -m py_compile fleet-update.py`. New test file `tests/unit/test_wrapper.py` (20 tests).

### ✅ Minor: `history.write_history` filename granularity
`_ts_now()` now uses `%Y%m%dT%H%M%S%fZ` (microsecond precision) instead of second
precision. Lexicographic sort order preserved; pruning unaffected. New test:
`test_ts_now_includes_microseconds`.

---

## Definition of done
- Python owns ALL logic (sequencing, gating, rescue/rollback, retries, serial, dependency order,
  window eval, status trees, change/outdated detection, briefing, config + state schemas);
  mypy-clean.
- Ansible is ONLY single-purpose execution primitives (+ inventory). No decision logic,
  `block/rescue`, or status `set_fact` remain.
- `tests/conftest.py` Jinja shim deleted; `discord_briefing.j2` deleted; tests import plain Python.
- Discord/ntfy briefing byte-identical (golden test + one verified real run).
- `fleet-update` (driver) is the entrypoint; the legacy monolith is removed.
- CI green: yamllint, ansible-lint, syntax-check, pytest, mypy, molecule (driving the flows),
  driver smoke test.

## Do-NOT list (carry forward)
- Do NOT leave decision logic in YAML. Primitives perform one action and return results.
- Do NOT re-introduce eager-templating coupling — step interpolation stays in `steps.py`.
- Do NOT change briefing bytes (golden-test gated).
- Rollback stays SNAPSHOT-ONLY — never a destructive vzdump restore; only roll back when the
  snapshot primitive reported `changed`.
- Manager-host node must never be rebooted mid-run.
- No heavy runtime deps beyond `pydantic` + `ansible-runner`.
- Keep each legacy path behind a flag until parity is proven on a real `--check` run.
