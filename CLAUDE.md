# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Commands

The Ansible→Python migration is **complete**: `fleet-update` (→ `driver.run_fleet()`) is the
only entrypoint. There is no `fleet-update.yml` playbook or `--use-*-flow` flags — Ansible
runs only as execution primitives in `ansible/primitives/*.yml`.

`fleet-update.py` (repo root) is the human-facing wrapper — auto-bootstraps `.venv`, friendly
flags. `fleet-update` (pip console command) is the programmatic/cron interface.

```bash
./fleet-update.py --dry-run --force-notify     # fleet-wide dry-run, forces Discord/ntfy notify
./fleet-update.py --force-notify               # full run, forced notification
./fleet-update.py --force-window               # bypass maintenance windows
./fleet-update.py -e custom_dry_run=true       # raw extra vars — only 5 keys are honoured
./fleet-update.py --history 5                  # table of the last 5 persisted runs (no fleet run)
./fleet-update.py --history-show latest        # replay a stored run's briefing (TS or 'latest')
./fleet-update.py --limit pve-01,105           # target host names and/or LXC/VM ids only
./fleet-update.py --phases lxc,vm              # run only these phases (pre-flight+notify always run)
./fleet-update.py --scan                       # read-only pending-updates scan → pending-*.json
                                              # (also runs manual_update checks + reminders)
fleet-update --check -e force_notify=true      # console command (needs active venv)

pip install -e '.[web]'              # fastapi + uvicorn for the dashboard
fleet-dashboard                      # web UI on dashboard_host:dashboard_port (default 0.0.0.0:8421);
                                     # run from the project root (the trigger subprocess needs CWD here)

pip install ansible-core 'proxmoxer>=2.3' requests   # into the venv — community.proxmox 2.x
                                            # needs proxmoxer >= 2.3 in ansible's interpreter;
                                            # requests = proxmoxer's undeclared HTTPS dep
ansible-galaxy collection install community.proxmox community.general
# ~/.ansible/collections is per-user and shared by every checkout on the box.

pip install -e '.[dev]'              # mypy, pytest, pydantic, types-PyYAML (+ web deps for test_web)
pytest tests/unit/ -v
pytest tests/unit/test_briefing.py -v
pytest tests/unit/ -k "run_fleet"

python -m mypy proxmox_fleet/
yamllint .
ansible-lint ansible/primitives/

# Molecule — drives Python flows via stub pct/vzdump scripts, against localhost
cd roles/lxc_update && molecule test -s lxc_update_normal      # normal | rollback | snapfail
cd roles/lxc_update && molecule converge -s lxc_update_normal  # converge only
cd roles/custom_update && molecule test -s custom_update_normal
```

`hosts.ini` and `vars.yml` are gitignored (secrets/IPs) — copy from `.example` files to run locally.

## Manager Setup (first time on Debian manager LXC)

Debian's system Python is externally managed (PEP 668) — use a venv:

```bash
apt install python3.13-venv     # match your Python version
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                # installs proxmox_fleet + fleet-update CLI
ansible-galaxy collection install community.proxmox community.general
```

Activate the venv at the start of each session: `source .venv/bin/activate`

## File Map

```
fleet-update.py            # wrapper: --dry-run/--force-notify/--verbose/--force-window/-e K=V; bootstraps .venv
install.sh                 # root installer: venv + deps + systemd units (fleet-dashboard.service,
                           # fleet-scan.timer every 6h); --update / --uninstall
ansible.cfg                # forks=20, pipelining=true, inventory=./hosts.ini
vars.yml / hosts.ini       # secrets + inventory (gitignored; copy from *.example)
.github/workflows/ci.yml   # lint, unit tests (3.10-3.12), mypy, ruff, bandit, molecule matrices
pyproject.toml             # package config; fleet-update entrypoint
proxmox_fleet/
  models/
    config.py              # CustomConfig pydantic schema (custom_update configs)
    state.py               # FleetState + per-type records; dump_for_ansible()
    settings.py            # GlobalSettings (vars.yml schema incl. timeouts/retries/kuma maps +
                           # scan-only manual_update_* settings)
  flows/
    _pkg.py                # shared: detect_pkg_mgr, upgrade_cmd (LC_ALL=C), kuma_healthy
    custom.py              # run_custom_update(): detect→backup→update→health→report
    lxc.py                 # run_lxc_update(): introspect→detect→backup→update→health→report
    vm.py                  # run_vm_update(): two-executor (VM SSH + node SSH for qm rollback/status)
    remote.py              # run_remote_update(): pre_update_cmd→detect→upgrade→reboot→health (no snapshot)
    node.py                # run_node_update() + run_manager_update(): Phase 2+3
  cluster.py               # multi-cluster helpers: DEFAULT_CLUSTER, split_qualified("alpha/101")
  deps.py                  # validate_depends_order() + dependency_failed()
  driver.py                # run_fleet() orchestrator + per-phase run_*_phase() helpers
  executor.py              # Executor protocol + RunnerExecutor; snapshot()/snapshot_with_retry(); 8 primitive methods
  http.py                  # get_json, poll_until, request, post_json
  inventory.py             # manual hosts.ini parsers + host_vars merge; MaintenanceWindow typing;
                           # [manual_update_hosts] loader + manual/auto overlap guard
  ledger.py                # hosts.json per-host accumulator (last-updated + os-upgrade events;
                           # manual systems observed for os_release/os-upgrade only)
  lxc_parse.py             # parse_pct_config/status, parse_ct_script, script_name_from_update
  orchestration.py         # run_serial(), run_concurrent(), retry()
  pkg_detail.py            # parse_upgraded()/pkg_mgr_for_ostype() — exact package list parsers
  runner.py                # invoke_primitive() — ansible-runner wrapper (project_dir=os.getcwd())
  steps.py                 # run_steps(): update_steps with per-step timeout + when gate
  status.py                # all status decision trees (custom/lxc/vm/remote/node/manager)
  changes.py               # change-detection helpers (lxc_os_changed, dpkg_hash_differs, ...)
  window.py                # in_window() — zoneinfo port of check-window.yml
  briefing.py              # render_briefing() byte-parity port of discord_briefing.j2
  history.py               # build_run_summary() + write_history(); history_summary()/read_run() readers
  notifiers.py             # resolve_notifiers(), dispatch() (discord/ntfy/webhook/telegram), ping_deadmans()
  manual_updates.py        # read-only manual-update adapter checks (TrueNAS midclt / OPNsense
                           # opnsense-update -c); registry + fail-closed parsers
  scan.py                  # --scan: read-only pending-updates walk → pending-*.json (next to history);
                           # runs manual_update adapter checks + reminder notifications
  scan_notifications.py    # manual-mapping scan notifications: fingerprint/state machine/render +
                           # run_manual_notifications() one-call (load → decide → dispatch → persist)
  lock.py                  # fleet-wide run lock (flock in fleet_history_dir); acquire_run_lock()/probe_lock()
  cli.py                   # fleet-update CLI: parses flags, calls driver.run_fleet()
  web/                     # fleet-dashboard ('.[web]' extra): app.py (FastAPI pages + SSE), runs.py
                           # (RunManager: detached CLI subprocess + log tail), main.py (entrypoint),
                           # templates/*.html + static/ (custom dashboard.css design system + dashboard.js)
config_templates/custom_system.yml.example   # full commented schema → copy to configs/<name>.yml
configs/                   # real configs/*.yml gitignored; commit *.yml.example only
ansible/primitives/        # thin single-purpose playbooks: run_shell, reboot_host, discover_lxcs,
                           # pct_config/status/start/stop/pull, snapshot, rollback, vzdump,
                           # lxc_os_update, lxc_app_update, lxc_introspect (batched read),
                           # lxc_post_update (batched read)
tests/unit/                # plain pytest, no Ansible/PVE; data/briefing_golden.json locks parity
roles/                     # molecule scenarios ONLY — drive Python flows via mol_run_flow.py
  lxc_update/molecule/{lxc_update_normal,lxc_update_rollback,lxc_update_snapfail}
  custom_update/molecule/{custom_update_normal,_noop,_rescue,_rollback,_dry_run,_uptodate,_per_step}
```

## Architecture

### Phase order in `driver.run_fleet()`

Threads one in-memory `FleetState` through every phase (`_merge_state()` folds each phase's
returned state in):

| Phase | Target | Purpose |
|---|---|---|
| Pre-Flight | manager | `http.wait_for_port()` on apt-cacher-ng proxy; `SystemExit(1)` if unreachable |
| Phase 0 | `remote_hosts` | `run_remote_phase()` → `flows/remote.py` (concurrent, `remote_forks`) |
| Phase 0a/0b | `custom_hosts` | `run_custom_phase()` — validates `depends_on` order, then `flows/custom.py` serially |
| Phase 1 | `proxmox_nodes` | `run_lxc_phase()` — tag-filtered discovery + `flows/lxc.py` (concurrent, `lxc_forks`) |
| Phase 1b | `proxmox_vms` | `run_vm_phase()` — pvesh HA discovery + `flows/vm.py` (concurrent, `vm_forks`) |
| Phase 2 | `proxmox_nodes` | `run_node_phase()` — serial OS update + reboot (abort-on-first-failure) |
| Phase 3 | manager | manager self-update (runs even after a node failure) |
| Phase 4 | manager | `run_notify_phase()` — render briefing → dispatch notifiers → write history → dead-man ping |

Returns exit code 1 if any phase recorded a failure. Each phase's dry-run flag is
`check or fleet_dry_run or <phase>_dry_run`; `fleet_dry_run` also forces a notification.

**Targeting**: `run_fleet(phases=...)` (`--phases`) selects phases by name
(`driver.PHASE_NAMES`: remote/custom/lxc/vm/node/manager — node and manager map onto
`run_node_phase(include_nodes=, include_manager=)`); pre-flight and Phase 4 always run; unknown
names `SystemExit`. `run_fleet(limit=...)` (`--limit`) restricts every phase to the named
hosts/ids — silently omitted like window skips. LXC limit mixes node names (whole node) and bare
ids (that container anywhere; nodes are skipped pre-discovery only when the limit holds no ids);
VM limit matches name or vmid; custom dep *validation* still covers the full inventory. The
manager self-update obeys limit via the tokens `manager`/`localhost`/`Ansible-Manager`.

**Canary staging** (remote/lxc/vm phases): hosts flagged `canary=true` (inventory/host_vars,
remote+vm) or listed in `canary_hosts` (names and/or LXC/VM ids, str-coerced) form wave 1; the
rest run only if no canary failed AND `driver._soak_canaries()` passes (sleep
`canary_soak_minutes`, then poll Kuma for each canary with a `*_kuma_map` entry — injectable
`_sleep`). On a failed gate the remainder get `SKIPPED (canary failed)` records
(`driver.CANARY_SKIP_STATUS`) and the phase is failed. The LXC phase discovers all nodes
up-front so the canary wave spans nodes; a discovery error never trips the gate (per-wave
failure tracking, not `state.failed`). Soak is skipped in dry-run; no canaries → single wave,
identical to pre-canary behaviour. Custom (dependency-ordered) and node (serial,
abort-on-first-failure) phases stage themselves already.

### State accumulation

Each `flows/*` call returns a per-host outcome → `run_*_phase()` folds it into a `FleetState`
via `_fold_outcome(state, outcome, bucket)` → `run_fleet()` `_merge_state()`s the per-phase
states. Lists are `lxc`/`vm`/`remote`/`node`/`custom`, each with `changed`/`failed` flags and
`errors`/`warnings` (`models/state.py`). `run_*_phase()` can `dump_for_ansible()` a phase's
state to JSON given a `state_output_path` (used by molecule); `run_fleet()` passes `None` and
merges purely in-memory.

### Phase 4 subsystems (`driver.run_notify_phase()`)

- **Notifiers**: `briefing.prepare_body()` renders the body once; `notifiers.dispatch()` fans
  it to a `notifiers` list (`discord`/`ntfy`/`webhook`/`telegram`). Back-compat
  `resolve_notifiers()`: if `settings.notifiers` is unset (`None`) but `discord_webhook` is set,
  synthesize one Discord notifier; an explicit `[]` means "none". All types reuse the same body,
  different envelope: ntfy headers, generic-webhook `{title, body, failed, timestamp}` JSON,
  Telegram `sendMessage` (plain text by default — briefing markdown is Discord-flavoured).
- **Run history** (`history.py`): `write_history()` writes `run-<UTC-ts>.json` + `latest.json`
  to `fleet_history_dir`, pruned to `fleet_history_keep`; gated on `fleet_history_enabled`.
  `write_history(keep_detail=…)` also strips per-record `packages` detail from runs older
  than `fleet_package_detail_keep` (see "Observability layer"). Read back via
  `history_summary()`/`read_run()` — surfaced as `--history [N]` /
  `--history-show <ts|latest>` (`cli.history_main()`, shared by both entry points, early-exits
  before any driver import).
- **Dead-man's switch**: `notifiers.ping_deadmans()` pings `fleet_deadmans_url` (`/fail` on
  failure) so its absence alerts when the orchestrator stops running.
- **Pending-updates scan** (`scan.py`, `--scan`): strictly read-only fleet walk — pending OS
  packages per host (apt `-s dist-upgrade` / `dnf check-update` rc-100-tolerant / `apk version
  -l "<"`, parsed by `parse_pending()`) plus per-LXC app current→latest (the lxc dry-check
  detect chain, reused). One sentinel-delimited command per host also reports the security
  subset, reboot-required flag and `/etc/os-release` (see "Observability layer"). Stopped
  CTs/templates are skipped, never started. Writes `pending-<ts>.json` + `pending-latest.json`
  to `fleet_history_dir` (pruned to `scan_history_keep`); obeys `--limit`; exits 1 for genuine
  check/parser errors, while SSH-unreachable targets are recorded and skipped. Bypasses
  `run_fleet()` entirely (no phases, no run briefing). It does dispatch
  *manual-update* notifications (see "Manual-update monitoring" below): when
  `manual_update_notifications` is enabled, pending/errored `[manual_update_hosts]` entries fan
  out through the same `notifiers.dispatch` on a first/change/daily-reminder state machine — that
  is the scan's only notify path.

### Web dashboard (`web/`, `fleet-dashboard`)

Optional `.[web]` extra (fastapi + uvicorn + python-multipart + fastapi-users + aiosqlite).
`web/app.py` serves six server-rendered pages (overview / pending / history+run detail /
per-host drill-down / package search / trigger) purely from `fleet_history_dir` via the
`history.py`/`scan.py`/`ledger.py` readers — the dashboard never runs fleet operations
itself (its only direct host contact is
the SSH-enrollment helpers `/ssh/push`/`/ssh/test`). Every route except `/login`, `/static`
and `/auth/*` hangs off one auth-protected `APIRouter` (session cookie via fastapi-users:
`CookieTransport` + `JWTStrategy`, single `admin@fleet.lan` user in
`<fleet_history_dir>/.fleet-users.db` — path owned by `auth.user_db_path()`, account created
by `install.sh`/`python -m proxmox_fleet.web.init_db`). `POST /runs` launches
`python -u -m proxmox_fleet.cli <flags>` via `web/runs.py`'s `RunManager`: detached
(`start_new_session`), stdout → `dashboard-runs/<id>.log` + `<id>.json` meta, so a dashboard
restart never kills a run (orphaned metas are finalized on read with `rc: null`). SSE
(`/runs/{id}/stream`) re-tails the log from byte 0 each connect. Single-flight is double-checked:
`RunManager.start()` probes the fleet lock + its own children, and the CLI child re-acquires the
same `lock.py` flock. Trigger args are composed only from validated tokens
(`build_run_args()` — never raw strings into argv). Start the dashboard from the project root
(the subprocess inherits CWD; `invoke_primitive` needs it). Tests: `test_web.py`
(fastapi.testclient + stub subprocess commands).

### Observability layer (package detail · scan metadata · per-host ledger · search)

Spans `pkg_detail.py`, `scan.py`, `ledger.py`, `history.py` and `web/app.py` (landed as one
changeset; see `docs/observability-roadmap.md`).

- **Exact package detail (records)**: `packages: Optional[List[{name, from, to}]]` on
  lxc/vm/remote/node records (`None` → key absent, so idle/dry/old records stay key-free).
  `pkg_detail.parse_upgraded()` parses manager-side `LC_ALL=C` output — apt real
  `Unpacking … over …` + simulate `Inst …`, dnf real `Upgraded:` block + `--assumeno`
  table, apk `(i/n) Upgrading … (old -> new)` (prefix optional); garbage/drift → `[]`,
  never an exception. Captured on success records only; LXC detail is dropped when the OS
  step failed; vm/remote/node keep simulated dry-run output as would-update detail, but
  `pkg_count` (a field remote/node records also gained) stays real-run-only so cumulative
  totals stay factual. `changes.vm_pkg_count`'s apk branch delegates to `parse_upgraded`
  (the old `^Upgrading` regex missed the `(i/n)` prefix and always counted 0).
- **Retention / totals independence**: `fleet_package_detail_keep` (default 7; ≤0 → never
  strip) keeps package detail only on the newest N run files. `write_history(keep_detail=…)`
  strips `packages` keys in place from older timestamped runs
  (`history._strip_package_detail`, idempotent) and never touches `latest.json` or
  `totals.json` — `count_packages()` reads status strings / `pkg_count`, so totals are
  independent of the stripped detail.
- **Pending scan metadata (schema)**: `scan_cmd()` emits sentinel-delimited sections in one
  command — `__FLEET_SEC__` (dnf only: `check-update --security`) and `__FLEET_META__`
  (reboot flag + `/etc/os-release`), then `exit $rc`. No single quotes anywhere (apk uses
  `"<"` — a `'` breaks `pct exec … -c '…'`). `parse_scan_output()` →
  `{pending, security, reboot_required, os_release}`; `parse_os_release()` →
  `{id, version_id, pretty_name}`; apt security = the subset of `Inst` lines whose archive
  matches `*-security` (no extra command). Snapshot keys: hosts
  `security_count`/`security`/`reboot_required`/`os_release`; lxc
  `os_security_count`/`os_security`/`reboot_required`/`os_release` (named to match
  `os_pending*`), lxc entries keyed `node/id`. Old snapshots lack the keys → `.get()`;
  `pending_summary()` adds `security_pending`/`reboot_hosts`.
- **Per-host ledger (`hosts.json`, survives run pruning)**: `ledger.read_ledger()` →
  `{hosts: {key: {last_run_ts, last_status, last_changed_ts, os_release}},
  events: [...]}`. Identities are multi-cluster-safe: **lxc → `node/id`** (bare vmids are
  not fleet-unique), vm → `name`, remote → `host`, node/manager → `node`; custom excluded;
  manual → the stable inventory hostname (release-only — see "Manual-update monitoring").
  Scan lxc entries normalise to `node/id` (`_scan_lxc_key`; pre-PR3 bare-id keys
  normalised, no-node entries skipped). `last_changed_ts` only for an **applied** OS update
  (shared `history._UPDATED_RE`, not dry-run — NodeRecord persists a `dry_run` marker,
  since node status strings have no dry-run variant). `observe_run` is called from
  `write_history()` after totals; `observe_scan` from `scan.write_pending()` before
  pruning. OS-upgrade events (`{type, host, from, to, ts}`, newest first, cap 100) come
  from scans only — the first observation is a baseline; a `version_id` (fallback
  `pretty_name`) change emits an event. **Corrupt recovery**: any read problem
  (missing/unreadable/invalid JSON — syntactic or structural) yields a fresh empty ledger;
  the observe functions swallow write errors; the ledger never fails a run or scan. Dead
  hosts linger — `rm hosts.json` rebuilds it observationally.
- **Package search + host timeline (routes)**: `GET /packages?q=` → `packages.html`
  (`_search_packages`): case-insensitive substring over `packages` (name/from/to) in
  timestamped `run-*.json` only (never `latest.json`), de-duplicated, newest run first,
  grouped by run with `/history/{ts}` and `/hosts/{node}/{id}` links; the retention note
  reads `fleet_package_detail_keep`. The host page timeline (`_host_timeline`) merges run
  records, per-snapshot pending entries (`_host_pending_entries`, `pending-latest.json`
  excluded) and ledger os-upgrade events — same timestamp shape, one lexical sort; pending
  items render counts / reboot pill / package `<details>`.

### Manual-update monitoring (scan-only)

Appliances the fleet must never auto-update (TrueNAS SCALE, OPNsense, vendor-managed boxes) are
**tracked, not updated**. The six-hour `--scan` runs read-only adapter checks per
`[manual_update_hosts]` host and folds the normalized results into the pending snapshot's
top-level `manual` mapping (keyed by the stable inventory hostname; fields: `adapter`, `current`,
`latest`, `update_available`, `reboot_required`, `summary`, `details`, `apply_hint`,
`unreachable`, `error`). This is scan-only — `run_fleet()` never touches these hosts, and the
`manual` bucket never enters `FleetState`, so a pending manual action cannot change
`changed`/`failed` run totals, the run briefing, or run history.

- **Inventory** (`inventory.py`): `[manual_update_hosts]` entries need a non-blank
  `manual_adapter` (inline or host_vars) — fails loud at load time, before host contact.
  `validate_manual_update_overlap()` rejects a hostname that also appears in `[remote_hosts]`,
  `[proxmox_vms]`, `[custom_hosts]` or `[proxmox_nodes]`; it runs in **both** the scan pre-flight
  (`scan.run_fleet_scan`) and the `run_fleet()` pre-flight, so a misplaced TrueNAS/OPNsense entry
  can never reach apt/pkg or a custom updater. Migrating a box off auto-update = remove it from
  every auto-update group first, then add it here with `manual_adapter=` (own commit, so a stale
  checkout never runs both paths for the same host).
- **Adapters** (`manual_updates.py`): each adapter owns one fixed, sentinel-delimited, read-only
  command and never an install/apply operation. TrueNAS: `midclt call system.version` +
  `midclt call update.check_available` (JSON parsed in Python; forbidden tokens apt/jq/updater/
  apply/upgrade). OPNsense: `opnsense-version` + `opnsense-update -c` only (a bare
  `opnsense-update`, or `pkg`/`upgrade`/`apply`, is a validation error). Adapters validate command
  invariants *before* host contact; unknown adapter names and invalid configs become error results
  without touching the network. Unreachable (ansible flag, raised error, or recognized SSH text) →
  `unreachable=true` + error, parser skipped.
- **Fail closed**: TrueNAS status outside `AVAILABLE`/`REBOOT_REQUIRED`/`CURRENT`/`UNAVAILABLE`,
  malformed JSON, OPNsense rc=1, or rc=0 with unrecognized output → error result, never a guess.
  If a future OPNsense release changes `opnsense-update -c` wording, the scan errors — capture the
  local check-only output and confirm the parser fixture
  (`tests/unit/data/manual_updates/opnsense_*.txt`) before relying on it.
- **Settings** (`models/settings.py`): `manual_update_notifications` (default true),
  `manual_update_reminder_hours` (24), `manual_update_forks` (2). Scan-only, read by
  `run_fleet_scan`; deliberately **not** accepted as `-e` extra vars (see the `-e` bullet below).
- **Notifications** (`scan_notifications.py`): when enabled, `run_manual_notifications()`
  (load state → decide → dispatch → persist) runs after the pending snapshot is written. Per-host
  state is `manual-notify-state.json` (`{host: {fingerprint, last_notified}}`); a host notifies on
  **first** (no entry), **change** (semantic `fingerprint` differs — whitespace-normalized, no
  timestamps), or **reminder** (`now >= last_notified + reminder_hours`). Unreachable → skipped,
  state untouched; clean → own entry cleared; hosts absent from a `--limit` scan keep their state.
  Dispatch goes through the existing `notifiers.dispatch` fan-out (Discord/ntfy/webhook/Telegram,
  same body, `notifier_retries`); genuine check errors drive failure severity (red,
  `❌ Scan: Manual Check Errors`), pending drives warning (amber, `⚠️ Scan: Manual Updates
  Available`). A dispatch attempt advances `last_notified` even with zero targets, so a failed
  dispatch never re-spams the next scan. Body ≤4000 chars, no trailing newline.
- **Dashboard/snapshot** (`web/app.py` + `pending.html`): the snapshot `manual` bucket renders as
  the "Manual systems" table on `/pending` (platform, current → available, action pills, details,
  apply hint, notes) with per-host `/hosts/<name>` links; `pending_summary()` counts
  `manual_updates`/`manual_reboots` for the per-scan columns. The ledger observes manual entries
  for OS release/upgrade events only — a manual update is an admin action, never a fleet-applied
  change. The dashboard health score deducts 1 per pending manual update/reboot (admin action).

### Cross-cutting subsystems

- **Snapshot-only rollback**: LXC/VM flows roll back via `pct/qm rollback BEFORE_UPDATE_AUTO`
  only when `snap_taken` (snapshot primitive returned `changed=True`). A failed snapshot is a
  non-fatal warning; rescue status is `FAILED (NO SNAPSHOT)` / `FAILED + ROLLED BACK` / `FAILED`.
  `lxc_backup_strategy: both` / `vm_backup_strategy: both` also takes a vzdump (never used to restore).
- **Fleet-wide dry-run**: `-e fleet_dry_run=true` (or `--check`, or `<phase>_dry_run`) puts every
  flow in simulate mode (`apt-get -s` / `dnf --assumeno` / `apk -s`) reporting `WOULD UPDATE`/`OK`,
  and forces a notification.
- **Maintenance windows** (`window.in_window`): remote/vm/custom hosts with a `maintenance_window`
  in `host_vars` are silently skipped outside it; `force_window=true` bypasses. Parsed into a typed
  `MaintenanceWindow` at inventory load (invalid keys fail loud); tz-aware `now` is `astimezone()`-converted.

### Flow structure (`flows/lxc.py` and friends)

`run_lxc_update()`'s `try/except/finally` reproduces the old `block/rescue/always`:

- **Introspect runs outside `try`** (fail loud on bad `pct config`): parse name/os_type/template,
  read status, start the container if stopped.
- **`try`**: detect (pull `/usr/bin/update`, parse ct script, fetch GitHub script) → dry-check
  (version compare) → backup (vzdump and/or snapshot) → update → health check → report.
- **`except`**: capture failing step → `pct rollback BEFORE_UPDATE_AUTO` if `snap_taken` → poll
  until running → `rollback_done` → record `FAILED + ROLLED BACK` / `FAILED (NO SNAPSHOT)` / `FAILED`.
- **`finally`**: delete snapshot if `snap_taken`; stop the container if `was_stopped` and
  `not rollback_done` (a rollback already restores it).

`run_vm_update()`/`run_remote_update()` follow the same shape; remote has no `finally` (no
snapshots). Outcomes are folded by `driver._fold_outcome()`.

### `custom_update` flow (`flows/custom.py` + `driver.run_custom_phase`)

- Config load is **outside** the flow (fail loud): `driver._load_config()` reads
  `configs/<name>.yml` (`settings.configs_dir`), deep-merges `custom_overrides` from host_vars,
  validates via `CustomConfig`.
- Flow body: detect (`version_command` + latest lookup) → backup (`backup_command`, then a PVE
  snapshot when enabled) → `steps.run_steps()` → change detection → reboot → health → report.
- **PVE snapshot/rollback (v2)**: a config with `pve_vmid` + `pve_node` (`pve_type: lxc|vm`)
  gets the lxc/vm-style safety net — `BEFORE_UPDATE_AUTO` snapshot before the steps,
  `pct/qm rollback` on the node in rescue (status `FAILED + ROLLED BACK` /
  `FAILED (NO SNAPSHOT)` via `status.custom_rescue_status()`), delete in `finally`.
  `run_custom_phase()` resolves `pve_node` → `ansible_host` (unknown node fails loud) and
  passes `node_executor` + `api_params`; without them the flow never snapshots.
- `except` (no `pve_vmid`, legacy): run `rollback_command` (errors ignored) → record `FAILED`.
  When a snapshot was taken, the snapshot rollback wins and `rollback_command` is NOT run.
- Each `[custom_hosts]` host needs `custom_config=<name>`; optional `custom_overrides: {...}`.
- `configs/*.yml` is gitignored — commit `*.yml.example`. Schema: `config_templates/custom_system.yml.example`.

`custom_status()` decision tree: `dry_run` → `dry-run: X → Y`; `update_only_if_outdated` &
current → `OK (up to date)`; `changed_when.type == always` → `Updated [+ Rebooted]`;
`== command` → exit 0 ⇒ `Updated [+ Rebooted]`, else `OK`; `== version` (default) → differ ⇒
`Updated: X → Y [+ Rebooted]`, same ⇒ `OK`; no version data → `Updated [+ Rebooted]` (fallback).

### LXC update sequence (`flows/lxc.py`) — order matters for correct attribution

1. Read `ver_before` (`cat ~/.<script>`)
2. OS update (`_os_update_cmd`, `LC_ALL=C`) — **first**, so OS packages credit the OS line
3. Read `dpkg_before` (`LC_ALL=C dpkg-query -W | sort | md5sum`)
4. Scale up resources if `needs_resource_scale`
5. App update (`/usr/bin/update`, `/tmp/.nc/clear` trick + `PHS_SILENT` when `lxc_unattended`)
6. Read `dpkg_after` — equal to `dpkg_before` ⇒ nothing installed
7. Read `ver_after`
8. Scale down → reboot check

`lxc_app_status()` priority order: version files differ → `Updated: X → Y`; equal & non-empty →
`OK`; dpkg hash differs → `UPDATED`; matches → `OK`; no hash data (non-apt) → `UPDATED`
(fallback); `app_changed` false → `OK`.

**Why dpkg hash, not stdout parsing**: `PHS_SILENT=1` routes apt's stdout to `/dev/null` inside
community scripts, so `0 upgraded, 0 newly installed` never appears. The dpkg hash is a direct
query, immune to suppression (`LC_ALL=C` keeps it locale-independent).

### Detect flow & version-file convention (`flows/lxc.py`)

1. `pct pull {id} /usr/bin/update /tmp/ansible_update_{id}` — `pull` failing ⇒ `lxc_no_update_script=True`.
2. `cat` the file **on the node**, then `lxc_parse.script_name_from_update()` **in Python** on the
   manager extracts the ct script name (avoids depending on `grep -P` on the node).
3. Fetch `https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/{name}.sh` via
   `http.request` **on the manager** (PVE nodes may lack outbound HTTPS); `parse_ct_script()`
   reads `var_cpu`/`var_ram` (build) and `pct set -cores/-memory` (run resources).

Version files live at `~/.{scriptname}` inside the container (e.g. `~/.sonarr` → `4.0.17.2952`);
`_read_version()` reads before/after, and the dry-check compares against the latest GitHub tag.
Resource scaling: when build CPU > run CPU, the flow scales up before the script and back down after.

### Uptime Kuma integration

All flows poll `{kuma_url}/api/status-page/heartbeat/{kuma_slug}` via `http.poll_until` +
`_pkg.kuma_healthy`, waiting for status `1`. Fires only when the host id is in
`lxc_kuma_map`/`vm_kuma_map`/`remote_kuma_map` **and** something changed. A timeout raises into
rescue (and rolls back if snapshotted). Retries/delay: `kuma_health_check_retries` (default 5) /
`kuma_health_check_delay` (default 30s); map keys are coerced to `str`.

### Key non-obvious details

- **Tag-based LXC discovery**: LXCs tagged `community-script` or `proxmox-helper-scripts`
  in PVE are processed (set in PVE UI → Container → Options → Tags), **plus** any IDs in
  `os_only_lxc_list` (pulled in for OS updates only — they lack `/usr/bin/update`, so the
  flow naturally reports `app="NO SCRIPT"`). `app_update_exclude_list` is the inverse for
  already-tagged LXCs: keep the OS update, skip the community-script app update (`SKIPPED`).
- **GitHub HTTPS runs on the manager** (`http.request`/`get_json`, `urllib`) — never the PVE node.
- **`pve_node` is a fallback hint only**: `run_vm_phase()` discovers the live VM→node map via
  `pvesh get /cluster/resources` (HA-aware); `pve_node` is used only when discovery fails.
- **Multi-cluster**: vmids are NOT fleet-unique — nodes carry a `cluster=` inventory var
  (default `default`), VM discovery queries one reachable node **per cluster** and keys by
  `(cluster, vmid)`, and `--scan` keys LXC entries by `node/id`. A VM whose vmid exists in
  several clusters must set `cluster=` or `pve_node=` (ambiguity fails loud, never guesses).
  Node names must be unique across all clusters (pre-flight `validate_node_uniqueness()`).
- **PBS is transparent**: `lxc_backup_storage` set to a PBS storage name routes `vzdump` to PBS automatically.
- **`[proxmox_vms]`/`[remote_hosts]`/`[custom_hosts]` must exist** in `hosts.ini` even if empty
  (header only) — Ansible raises "no hosts matched" otherwise.
- **`custom_config` is required per-host** for `[custom_hosts]` — fails loud (include_vars) if missing.
- **Node reboot is skipped** when `manager_lxc_id` runs on that node (would kill the manager mid-run).
- **Unreachable nodes are tolerated while the cluster is quorate**: LXC discovery
  (`UnreachableHostError`, from `PrimitiveResult.unreachable` / `runner_on_unreachable`) and
  the Phase-2 node loop (`_error_is_unreachable()` text match) convert an SSH-unreachable node
  into a warning + `SKIPPED (unreachable)` record instead of failing the run — gated on
  `driver._cluster_quorate()` (`pvesh get /cluster/status` on a surviving node; standalone
  node counts as quorate). Without quorum it stays a hard error (pmxcfs goes read-only →
  snapshots fail fleet-wide). VM guests stay hard errors — HA should have migrated them.
- **`lxc_backup_strategy`** is a 4-value enum: `snapshot | vzdump | both | none` — not booleans.
- **`/tmp/.nc/clear` trick**: overrides `clear` with a no-op so update-script output isn't wiped from capture.
- **Snapshot name is fixed** at `BEFORE_UPDATE_AUTO` — the `finally` cleanup hardcodes it; changing
  the create site without the cleanup/rollback site leaves orphaned snapshots.
- **Idle containers are suppressed** (`lxc_should_report`) — a record is appended only on change/failure.
- **`lxc_continue_on_error`**: Phase 1 runs containers concurrently per node; one failure becomes
  a `FAILED` record without aborting the others.
- **Rollback only fires when `snap_taken`** — `strategy: none` or a failed snapshot just records
  `FAILED` (the latter `FAILED (NO SNAPSHOT)`); **vzdump alone never auto-restores**.
- **A non-zero OS/app update does not raise** (the flow carries on so the other line still gets
  reported) but it *is* a failed run: each one appends an `ErrorEntry` (task `OS update` /
  `app update`) to `LxcFlowOutcome.errors` carrying `_failure_detail()` — the failing command's
  stderr, else stdout, ANSI-stripped, collapsed to one line and tail-capped at 400 chars (the
  community scripts colourise even under `TERM=dumb`/`PHS_SILENT`, and their *last* line can be a
  misleading fallthrough with the real cause two lines above) — and sets
  `outcome.failed`. Without that the record reads `FAILED` while `state.failed` stays false, so
  the exit code, history and dashboard all claim success with no reason recorded anywhere.
  `outcome.errors` (plural, lxc-only, both lines can fail) is separate from `outcome.error` (the
  single rescue-path exception); `driver._fold_outcome()` reads it defensively via `getattr`.
  These failures do **not** enter rescue, so **they do not roll back** — the container is left
  half-updated by design.
- **Pre-emptive health warnings** (`flows/lxc.py`: `disk_warning()` / `os_mismatch_warning()`):
  the `lxc_introspect` primitive also returns `df_stdout` + `os_release_stdout` (batched — no
  extra subprocess), and the flow emits a `WarningEntry` when the rootfs is at/over
  `lxc_disk_warn_percent` (default 75, below the community scripts' own >80% `exit 114`) or the
  container OS is behind the ct script's `var_os`/`var_version` (the `exit 203` trap). Disk is
  checked for **every** container (plain apt runs out of space too); OS only when a ct script
  exists. Both fire **outside** `try` / before the dry-run return, so `--dry-run` reports them
  ahead of a window — that is the point. `scan.py` surfaces the same two signals as
  `disk_percent`/`os`/`os_mismatch` per container, counted by `pending_summary(disk_threshold=)`
  as `low_disk`/`os_mismatch`, and the dashboard's `/pending` page renders both (Disk + OS
  columns, plus per-scan counters) — `web/app.py` passes `settings.lxc_disk_warn_percent` so the
  page and the briefing agree on "low". Warnings only: nothing is skipped or failed.
- **Introspect precedes `pct_start`**, so `df_stdout`/`os_release_stdout` are empty for a
  container that was stopped (a failed `pct exec` gives rc≠0 + empty stdout, absorbed by
  `failed_when: false`). The parsers return `None`/`""` there and no warning fires — an accepted
  gap, not a false negative to "fix" by moving the reads after the start.
- **`os_version_matches()` mirrors `check_container_os_guard`**: exact match or a prefix on a
  dot boundary (alpine `3.22.1` satisfies a script targeting `3.22`), and missing data on either
  side counts as a match, so an unparseable read never invents a warning.
- **`parse_ct_script` must handle `var_x="${var_x:-N}"`**: current community scripts write that
  form so the environment can override, and the bare `var_cpu="2"` patterns silently stopped
  matching. Every field now goes through the `_var()` helper, which accepts both forms.
- **Resource scaling is opt-in and re-sourced** (`lxc_parse.resource_scale_plan`): `pct set $CTID
  -cores N` is gone from every current ct script *and* from `build.func`/`install.func` — upstream
  dropped build-time scaling, so `parse_ct_script` no longer returns `run_cpu`/`run_ram`/
  `needs_resource_scale`. The run side is now the container's live allocation from `pct config`
  (`cores:`/`memory:`, **anchored with `^…` + MULTILINE** — the `description:` field is a blob of
  URL-encoded HTML that an unanchored pattern matches inside). Targets are `max(script, current)`
  so an over-provisioned container is never shrunk, and the restore target is the live pre-scale
  value. The plan is always computed (visible under `--verbose`) but only executed when
  `lxc_resource_scaling: true` — default **false**, because turning it on adds `pct set` calls the
  scripts themselves no longer make. It can only fire on hand-provisioned containers; one created
  by its own script already matches its spec.
- **Custom-config commands are opaque strings**: `CustomConfig` validates as literals, never
  renders. `steps.run_steps()` resolves only `{{ steps.NAME }}` in Python at run time; everything
  else is left for the shell. `register` stashes a step's stdout for a later `when:`.
- **`invoke_primitive` requires CWD = project root**: `ansible_runner.run()` passes
  `project_dir=os.getcwd()`. Without it, ansible-runner uses a fresh tempdir and the playbook path
  never resolves. `mol_run_flow.py` calls `os.chdir(_project_root)` at module load for molecule.
- **State merge is in-memory** (`driver._merge_state`): OR-joins `changed`/`failed`, concatenates
  records. The `dump_for_ansible()` JSON is only for tooling/molecule `verify.yml`.
- **Inventory parser avoids `configparser`** (it mis-splits `hostname key=val key=val …` lines on
  the first `=`) — `inventory._iter_section()` is a manual regex parser; merges `host_vars/<host>.yml`
  for every group, including `proxmox_nodes` (so a node's `ansible_host` can live in host_vars).
- **Package/locale commands pin `LC_ALL=C`** (`_pkg.upgrade_cmd`, `lxc._os_update_cmd`/`_dpkg_hash_cmd`)
  — change detection greps English summary lines. `window.in_window` likewise uses a fixed weekday list.
- **Shared pkg helpers** (`detect_pkg_mgr`, `upgrade_cmd`, `kuma_healthy`) live in `flows/_pkg.py` —
  used by vm/remote/lxc/custom/node flows; don't re-copy them.
- **Alpine uses `ash`**: `lxc._read_version` and the OS-update command pick `ash` for `ostype: alpine`, else `bash`.
- **Briefing byte-parity — no trailing newline**: `render_briefing()` must not emit one (golden
  fixture has none). `prepare_body()` is `.strip()` + a port of Jinja's
  `truncate(4000, killwords=False, end='\n...', leeway=5)` — match the algorithm exactly
  (unchanged when `len <= 4005`).
- **`settings.notifiers` defaults to `None`** (not `[]`): preserves the `notifiers is defined`
  distinction — explicit `[]` means "none" and must not trigger the `discord_webhook` back-compat shim.
- **`-e KEY=VALUE` honours only five keys** — `fleet_dry_run`, `lxc_verbose`, `force_notify`,
  `force_window` (folded onto settings by `cli.apply_extravar_overrides` via the
  `_SETTINGS_EXTRAVARS` allowlist) and `custom_dry_run` (read straight from the dict in
  `run_custom_phase`). Every other key is parsed by `_parse_extra_vars`, carried in the
  extravars dict, and **never read**: `-e discord_webhook=` or `-e fleet_history_dir=/tmp/x`
  looks accepted and silently does nothing. Values are booleans only (`_is_true`: true/1/yes),
  so `-e` cannot set a string setting at all — use `vars.yml` or `--vars-file`. The example
  previously advertised in the README and `--help` (`-e custom_allow_reboot=false`) was itself
  a no-op: `custom_allow_reboot` is only ever read from settings.
- **`manual_update_*` settings are scan-only** — read by `run_fleet_scan` only, and deliberately
  not part of the `-e` allowlist: like any other non-listed key, `-e manual_update_reminder_hours=…`
  is parsed and silently ignored (set them in `vars.yml`).
- **`run_shell.yml`/`reboot_host.yml` have `check_mode: false`** — commands always execute; Python
  controls dry-run by choosing a simulate vs. real command. The node flow additionally guards
  reboot with `not dry_run` in Python.
- **`run_node_update` retry uses injectable `_sleep`** (`orchestration.retry(..., sleep=_sleep)`);
  tests pass `lambda s: None`. `run_node_phase()` uses real `time.sleep` — tests calling through it
  must monkeypatch `time.sleep`. `steps.run_steps()` has the same injectable `sleep`.
- **`executor.snapshot(vmid, *, snap_state, api_host, ...)`**: `api_host` must be the node's
  `ansible_host` IP, not the inventory name; `vmid` (not `lxc_id`) covers both LXC and QEMU.
- **`snapshot_with_retry`**: free function wrapping `orchestration.retry()`; used by lxc & vm flows
  for create (`until=changed`) and delete (`until=not failed`); treats "CT is locked" as transient.
  It preserves the final primitive result so non-fatal warnings include the actual module error.
  `snapshot_timeout` (default 600s) and `snapshot_api_timeout` (default 30s) override the
  community.proxmox defaults, which are too short for large disks or slow storage.
- **`run_concurrent(timeout=...)`**: each `future.result(timeout=...)` raising `TimeoutError` is
  caught by `except BaseException` and becomes a per-item failure rather than hanging forever.
- **`_discover_vm_locations()`** prints a `[vm phase] WARNING: ...` to stderr on failure before
  falling back to `pve_node` hints — surfaces the error without aborting.
- **Two-executor pattern for VMs**: `executor` (VM guest SSH, package upgrades) vs.
  `node_executor` (Proxmox node SSH, `qm rollback`/`status`) — swapping them fails silently.
- **Package manager detection uses `if/elif/else`**, never `&&`/`||` chains (equal precedence
  causes every branch to fire on Debian).
- **`lxc_parse.py` owns all regex extraction** — parity locked by `test_status_lxc.py`; update both together.

### Testing infrastructure

Plain Python (`tests/unit/`) — no Ansible, no PVE, no Jinja shim. Each flow has its own
`Scripted*Executor` (queued `PrimitiveResult`s matched by command substring, records `.commands`,
stubs `snapshot()`/`reboot()`); status/parse/helper functions are tested directly. There is no
shared `conftest.py`. `ruff`/`mypy` run clean over `proxmox_fleet/` (and `ruff` over `tests/`).

Coverage by area: `test_{config_model,state_model,settings,changes,deps,window,inventory,pkg,
orchestration,http,steps,history,notifiers,runner,executor,cli}.py` plus `test_pkg_detail.py`
and `test_ledger.py` for the model/helper layer;
`test_status_{custom,lxc,vm,remote,node}.py` for decision trees; `test_flow_{custom,lxc,vm,
remote,node}.py` for end-to-end flows via scripted executors; `test_driver.py` for phase
orchestration (dep-abort, window skip, dry-run, abort-on-first-failure); `test_briefing.py`
includes the **golden byte-parity** test against `tests/unit/data/briefing_golden.json`;
`test_wrapper.py` covers `fleet-update.py`'s flags/aliases/propagation/exit codes.

**Molecule** drives the Python flows (not roles): `roles/lxc_update/molecule/` has
`normal`/`rollback`/`snapfail`; `roles/custom_update/molecule/` has `normal`/`noop`/`rescue`/
`rollback`/`dry_run`/`uptodate`/`per_step`. Each `converge.yml` runs `mol_run_flow.py` → a stub executor →
the flow function → `verify.yml` `include_vars`s the dumped JSON. Idempotency is disabled
(backup/update are intentionally non-idempotent). `roles/` contains **only** these harnesses.

**CI** (`.github/workflows/ci.yml`): yamllint, ansible-lint + syntax-check (primitives), pytest
(3.10–3.12 matrix + coverage), mypy, ruff, bandit (`-ll`), plus the two molecule matrices.

### Briefing output constraints (`briefing.py`)

Rendered in Python now, but **Discord embed markdown** constraints still apply: descriptions
support `**bold**`, `*italic*`, `` `code` ``, `- ` bullets, `\n`; they do **not** support `>`
blockquotes or `#` headers (regular messages only, not webhook embeds). Byte-parity with the
retired template is locked by the golden test (see "Briefing byte-parity" above).
