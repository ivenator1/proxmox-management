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
  fake. New flows take an `Executor`. Phase 3 added `snapshot(lxc_id, *, snap_state, api_host, …)`;
  `api_host` must be the node's `ansible_host` IP, not the inventory name.
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
  alias map). There are now two merge plays in `fleet-update.yml`: (a) "Merge Python custom state"
  between Phase 0b and Phase 1, (b) "Merge Python lxc state" between Phase 1 and Phase 1b. Both
  gate on `is defined` for their respective `fleet_*_state_path` extravar. Each must stay in its
  correct position — loading lxc state after Phase 1b would OR-join against already-false flags.
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
| 3 | `lxc_update` → `flows/lxc.py` + primitives; `tmp_app`/`tmp_os` ports; molecule reworked | 🔶 wired (`6a30716`) |
| 4a | `vm_update` + `remote_host_update` → `flows/vm.py` + `flows/remote.py`; `--use-vm-flow` / `--use-remote-flow` flags | 🔶 wired |
| 4b | node OS update + manager self-update; serial reboot loop; `--use-node-flow` | ⬜ **next** |
| 5 | Briefing/history/notifiers in Python (byte-parity); split monolith; retire `conftest.py` + delete `.j2` | ⬜ |

What exists now: `proxmox_fleet/{__init__,cli,__main__,runner,orchestration,http,steps,executor,status,changes,deps,driver,inventory,window,lxc_parse}.py`,
`proxmox_fleet/models/{config,state,settings}.py`, `proxmox_fleet/flows/{custom,lxc,vm,remote}.py`,
`ansible/primitives/{run_shell,reboot_host,discover_lxcs,pct_config,pct_status,pct_start,pct_stop,pct_pull,snapshot,rollback,vzdump,lxc_os_update,lxc_app_update}.yml`,
`roles/{custom_update,lxc_update}/molecule/mol_run_flow.py`,
and tests `tests/unit/test_{config_model,state_model,orchestration,http,status_custom,steps,flow_custom,settings,deps,window,inventory,driver,status_lxc,flow_lxc}.py`.
442 tests green, mypy clean. `--use-custom-flow` routes Phase 0b and `--use-lxc-flow` routes Phase 1
through the Python driver; both flags keep the legacy Ansible role as the default until real-run parity is confirmed.

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

---

## Phase 4 — vm / remote / node / manager

Already done (landed in Phase 2-wire): `window.py.in_window()` ← `tasks/check-window.yml`;
`deps.py` — `validate_depends_order()` + `dependency_failed()`. Both have plain-Python parity
tests (`test_window.py`, `test_deps.py`) and are wired into `run_custom_phase()` already.

Remaining:
- **`flows/vm.py`** ← `roles/vm_update` (qm snapshot→update→health→rescue→delete snapshot).
  Status → `status.py.vm_status()` + rescue rollback string (mirror `test_vm_report.py`).
  Primitives: `qm_snapshot.yml`, `qm_rollback.yml`, `apt_upgrade.yml`.
- **`flows/remote.py`** ← `roles/remote_host_update` (apt/dnf/apk + reboot-check; no snapshot,
  no `always`). `status.py.remote_status()`.
- **`flows/node.py`** ← Phase 2 of `fleet-update.yml`: serial node OS update + reboot. Replace
  `serial: 1`/`any_errors_fatal` with `orchestration.run_serial(abort_on_error=True)`. Port
  `node_status_str` → `status.py.node_status()`; port the manager-host skip
  (`manager_lxc_id`) and manager self-update (Phase 3 playbook). Use `http.wait_for_port` for
  the apt-proxy + post-reboot waits.

For each flow, follow the Phase 3 pattern exactly: `try/except/finally`, `LxcFlowOutcome`-style
dataclass outcome, `ScriptedExecutor` tests, molecule `mol_run_flow.py`, parity test vs. the
existing Jinja test, flag behind `--use-vm-flow` / `--use-remote-flow` / `--use-node-flow` until
real-run parity confirmed.

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
