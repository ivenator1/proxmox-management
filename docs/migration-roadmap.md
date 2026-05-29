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
  calls `run_shell` (target), `run_local` (manager), `reboot`. `RunnerExecutor` invokes
  primitives; tests inject a scripted fake. New flows take an `Executor`.
- **Primitives** return values via `ansible.builtin.set_stats: { data: {...}, aggregate: false }`;
  `runner._harvest` reads `res['ansible_stats']['data']` into `PrimitiveResult.facts`.
- **Status parity**: every `tmp_*` status string is reproduced in `status.py` and locked by a
  test that mirrors the old Jinja test case-for-case (e.g. `test_status_custom.py` ↔
  `test_custom_report.py`). Do the same for lxc/vm/node.
- **Eager-templating fix** lives in `steps.py`: command `{{ steps.NAME }}` refs are resolved
  in Python; all other `{{ }}` pass through to Ansible. Do NOT render full commands in Python.
- **Tests import plain Python** (no Jinja shim). Flows are tested with a fake executor +
  monkeypatched `http`. Keep the old Jinja-shim tests until the matching logic is the default,
  then delete them with `conftest.py` in Phase 5.

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
| 2-wire | Driver runs the custom flow as default; molecule reworked; role YAML retired | ⬜ **next** |
| 3 | `lxc_update` → `flows/lxc.py` + primitives; `tmp_app`/`tmp_os` ports | ⬜ |
| 4 | `vm_update`/`remote_host_update`/node + manager; serial reboot loop; window eval | ⬜ |
| 5 | Briefing/history/notifiers in Python (byte-parity); split monolith; retire `conftest.py` + delete `.j2` | ⬜ |

What exists now: `proxmox_fleet/{__init__,cli,__main__,runner,orchestration,http,steps,executor,status,changes}.py`,
`proxmox_fleet/models/{config,state}.py`, `proxmox_fleet/flows/custom.py`,
`ansible/primitives/{run_shell,reboot_host}.yml`, and tests
`tests/unit/test_{config_model,state_model,orchestration,http,status_custom,steps,flow_custom}.py`.
278 tests green, mypy clean. **The legacy roles + `fleet-update.yml` are untouched and still run.**

---

## Phase 2-wire — make the custom flow the default (NEXT)

Goal: route Phase 0b through `flows/custom.py` instead of the `custom_update` role, behind a
flag, then retire the role.

1. **`proxmox_fleet/driver.py`** — add a `run_custom_phase(settings, inventory)`:
   - Read `[custom_hosts]` + per-host `custom_config`, `custom_overrides`, `depends_on`,
     `maintenance_window` from inventory (parse `hosts.ini`/host_vars, or have a small
     `inventory` primitive dump `hostvars` to JSON the driver reads).
   - Validate Phase-0a dependency order via `deps.validate_depends_order()` (see Phase 4/5 —
     port `_dep_problems` from `fleet-update.yml`); abort on problems.
   - For each host **in dependency order** (serial — matches `serial: 1`): load+merge config
     → `CustomConfig`; skip if outside `window.in_window(...)`; compute `dep_failed` from prior
     failures; build `RunnerExecutor(host)`; call `run_custom_update(...)`; fold the
     `CustomFlowOutcome` into a `FleetState` (record, changed, failed, error, warnings).
   - Pass `kuma_url`, `kuma_retries`, `kuma_delay`, `custom_allow_reboot` from `vars.yml`
     (introduce a `GlobalSettings` pydantic model for `vars.yml`).
2. **Config loading** — load `configs/<name>.yml`, deep-merge `custom_overrides`
   (`combine(recursive=true)` semantics — but in Python this is just a recursive dict merge;
   note: list values REPLACE, matching the documented Ansible behaviour), then
   `CustomConfig.model_validate`.
3. **Wire fleet-update** — Phase 0b chooses driver-flow vs legacy role on a flag
   (`use_custom_flow`, default false → flip to true once molecule passes). Simplest seam: the
   `fleet-update` CLI runs the custom phase in Python and tells the playbook to skip its
   Phase 0b (e.g. an extravar the play's `when:` honours), or the playbook calls out — pick
   the lower-friction option when implementing.
4. **Molecule** — rework `roles/custom_update/molecule/{normal,noop,rescue,dry_run,uptodate,per_step}`
   to drive `flows/custom.py` with a fake/stubbed `run_shell` (or real shell stubs on
   localhost). Keep the same six scenarios + assertions. Update `.github/workflows/ci.yml`
   `molecule-custom-update` matrix accordingly (and `pip install -e . ansible-runner`).
5. **Retire** — delete `roles/custom_update/tasks/*` and the role once molecule + a real
   `--check` run match. Delete `tests/unit/test_custom_report.py` / `test_run_step.py` /
   `test_custom_depends.py` (now covered by `test_status_custom.py`/`test_steps.py`/new deps test).
6. **Rollback for this step**: keep the role + flag-off path until parity is proven.

---

## Phase 3 — `lxc_update`

Move `roles/lxc_update/tasks/*` into `flows/lxc.py`. The per-container `block/rescue/always`
becomes Python `try/except/finally`.

- **Status → `status.py`**: `lxc_app_status()` (the 11-branch `tmp_app`), `lxc_os_status()`
  (`tmp_os`), and the rescue app-string (`FAILED + ROLLED BACK` / `FAILED (NO SNAPSHOT)` /
  `FAILED`). Parity tests mirroring `test_report_tmp_app.py`, `test_report_tmp_os.py`,
  `test_report_when_condition.py`, `test_dry_check_status.py`.
- **Parsing → `status.py`/helpers**: `introspect.yml` (`pct config`/`status`, template/running
  detection — mirror `test_introspect_regex.py`), `detect.yml` resource regexes
  (mirror `test_detect_regex.py`), the dpkg-hash compare in `update.yml`.
- **Primitives**: `discover_lxcs.yml` (tag-filter discovery shell), `pct_config.yml`,
  `pct_status.yml`, `pct_start.yml`, `pct_stop.yml`, `snapshot.yml`
  (`community.proxmox.proxmox_snap` create + delete), `rollback.yml` (`pct rollback`),
  `pct_pull.yml`, `vzdump.yml`, `lxc_os_update.yml`, `lxc_app_update.yml` (community-script
  run incl. the `/tmp/.nc/clear` trick + resource scale up/down).
- **Control flow in Python**: start-if-stopped (and stop again in `finally` unless rollback
  restored it), backup strategy enum (`snapshot|vzdump|both|none`), **snapshot-only rollback**
  (call `rollback.yml` ONLY when the snapshot primitive reported `changed`), health-gate (run
  only if changed; Kuma via `http.py`), `always` snapshot-delete.
- **Driver**: Phase 1 loops nodes; per node, discover LXCs, run containers with
  `orchestration.run_concurrent(max_workers=…)` (replaces `forks`/`serial: 2`);
  `lxc_continue_on_error` → don't abort siblings (already the default of `run_concurrent`).
- **Molecule**: rework `roles/lxc_update/molecule/{normal,rollback,snapfail}` to drive
  `flows/lxc.py` with stub `pct`/`vzdump`. Parity gate against the old Jinja before deleting.

Footgun: Kuma map + the `pve_node` → `hostvars[...]['ansible_host']` lookup for the snapshot
API call; manager-host node must never be rebooted.

---

## Phase 4 — vm / remote / node / manager

- **`flows/vm.py`** ← `roles/vm_update` (qm snapshot→update→health→rescue→delete snapshot).
  Status → `status.py.vm_status()` + rescue rollback string (mirror `test_vm_report.py`).
  Primitives: `qm_snapshot.yml`, `qm_rollback.yml`, `apt_upgrade.yml`.
- **`flows/remote.py`** ← `roles/remote_host_update` (apt/dnf/apk + reboot-check; no snapshot,
  no `always`). `status.py.remote_status()`.
- **`flows/node.py`** ← Phase 2 of `fleet-update.yml`: serial node OS update + reboot. Replace
  `serial: 1`/`any_errors_fatal` with `orchestration.run_serial(abort_on_error=True)`. Port
  `node_status_str` → `status.py.node_status()`; port the manager-host skip
  (`manager_lxc_id`) and manager self-update (Phase 3). Use `http.wait_for_port` for the
  apt-proxy + post-reboot waits.
- **`window.py.in_window(now, window)`** ← `tasks/check-window.yml` (use Python
  `datetime`/`zoneinfo`; no `date` shell-out). Mirror `test_check_window.py`. Driver evaluates
  the window before invoking a host's flow.
- **`deps.py`** ← Phase 0a `_dep_problems` validator + `main.yml` `_dep_failed`
  (`validate_depends_order()`, `dependency_failed()`). Mirror `test_custom_depends.py`.

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
