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

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Package skeleton, runner, orchestration, http, cli, CI (mypy + pip install -e .) | ✅ done (`0dad3b8`) |
| 1 | pydantic `CustomConfig` + `FleetState` schemas | ✅ done (`0dad3b8`) |
| 2-logic | `custom_update` decision trees → `status.py`/`changes.py` (+ parity tests) | ✅ done (`57ade9f`) |
| 2-flow | `custom_update` orchestration → `flows/custom.py` + `steps.py` + `executor.py` + primitives | ✅ done (`d7d6308`) |
| 2-wire | Driver runs the custom flow behind `--use-custom-flow`; molecule reworked; retire pending real-run parity | 🔶 wired (`759f3ad`) |
| 3 | `lxc_update` → `flows/lxc.py` + primitives; `tmp_app`/`tmp_os` ports; molecule reworked | 🔶 live-tested (`testing` branch) |
| 3-retire | Delete legacy `lxc_update` role tasks/defaults; flip `--use-lxc-flow` to default | ⬜ pending speed parity |
| 4 | `vm_update`/`remote_host_update`/node + manager; serial reboot loop; window eval | ⬜ **next** |
| 5 | Briefing/history/notifiers in Python (byte-parity); split monolith; retire `conftest.py` + delete `.j2` | ⬜ |

What exists now: `proxmox_fleet/{__init__,cli,__main__,runner,orchestration,http,steps,executor,status,changes,deps,driver,inventory,window,lxc_parse}.py`,
`proxmox_fleet/models/{config,state,settings}.py`, `proxmox_fleet/flows/{custom,lxc,vm,remote}.py`,
`ansible/primitives/{run_shell,reboot_host,discover_lxcs,pct_config,pct_status,pct_start,pct_stop,pct_pull,snapshot,rollback,vzdump,lxc_os_update,lxc_app_update}.yml`,
`roles/{custom_update,lxc_update}/molecule/mol_run_flow.py`,
and tests `tests/unit/test_{config_model,state_model,orchestration,http,status_custom,steps,flow_custom,settings,deps,window,inventory,driver,status_lxc,flow_lxc,status_vm,status_remote,flow_vm,flow_remote}.py`.
~500 tests green, mypy clean. `--use-custom-flow` routes Phase 0b, `--use-lxc-flow` routes Phase 1,
`--use-vm-flow` routes Phase 1b, and `--use-remote-flow` routes Phase 0 through the Python driver;
all flags keep the legacy Ansible role as the default until real-run parity is confirmed.

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

**Future TODO — speed optimisation:**
The per-`run_shell()` subprocess overhead is the primary bottleneck. Recommended approach: consolidate the ~15 individual primitive calls per container into 4–5 purpose-built multi-read primitives (e.g., one `introspect` primitive returning pct config + status + version file; one `post-update` primitive returning dpkg hash + version file). This keeps all decision logic in Python, maintains clean error attribution, and cuts subprocess spawns from ~15 to ~4–5 per container. Do NOT batch decision logic into shell scripts — that moves branching out of Python. Do NOT use direct SSH (loses Ansible inventory/SSH config integration). See memory `project_speed_todo.md` for full notes.

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

## Phase 4b — node OS update + manager self-update (⬜ next)

Remaining from Phase 4:
- **`flows/node.py`** ← Phase 2 of `fleet-update.yml`: serial node OS update + reboot. Replace
  `serial: 1`/`any_errors_fatal` with `orchestration.run_serial(abort_on_error=True)`. Port
  `node_status_str` → `status.py.node_status()`; port the manager-host skip
  (`manager_lxc_id`) and manager self-update (Phase 3 playbook). Use `http.wait_for_port` for
  the apt-proxy + post-reboot waits.

Follow the Phase 3 pattern: `try/except/finally`, outcome dataclass, `ScriptedExecutor` tests,
`mol_run_flow.py`, flag behind `--use-node-flow` until real-run parity confirmed.

---

## Phase 5 — briefing / history / notifiers; retire the shim

- **`briefing.py.render_briefing(state: FleetState) -> str`** ← `templates/discord_briefing.j2`.
  **BYTE-FOR-BYTE PARITY IS NON-NEGOTIABLE**: reproduce `\n`/`\n\n` separators, `**bold**`,
  `*(no snap)*`, `— ` em-dashes, section order, the `OS:` `None`-guard, the idle
  `*No container changes.*` line. Keep `| trim | truncate(4000, False, '\n...')` applied by the
  driver. Golden test seeded from the CURRENT render (capture via `test_discord_briefing.py`'s
  shim BEFORE deleting it). No spacing/markdown change without explicit approval.
- **History** ← `tasks/persist-history.yml`: driver writes `run-<UTC-ts>.json` + `latest.json`
  and prunes to `fleet_history_keep` with stdlib `json.dump`/`pathlib`. No copy/file/shell.
- **`notifiers.py`** ← `tasks/notify.yml` + Phase-4 `set_fact` (title/colour/`_ntfy_title`/
  `_should_notify` + back-compat `discord_webhook` shim). Discord/ntfy POST + dead-man ping via
  `http.post_json`/`http.request` (manager-local). Mirror `test_notify.py`,
  `test_persist_history.py`.
- **Split the monolith** (the Phase-0 seam, do it here if not earlier): per-phase playbooks the
  driver invokes in order, then the driver becomes the orchestrator end to end and
  `fleet-update.yml` is removed.
- **Retire the shim**: delete `tests/conftest.py`'s Ansible-Jinja re-implementation + parity
  tests; delete `discord_briefing.j2`. Port `test_fleet_state_append_logic.py` (state assembly →
  `models/state.py`).

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
