# Feature Enhancement Roadmap

Proposed enhancements for the fleet orchestrator, sized small / medium / large. Each fills a
confirmed gap: no host targeting, run history is write-only, the custom flow has no snapshots,
phases update everything in one wave, and there is no UI or pending-update visibility.

---

## Small

### S1. `--history` CLI — view past runs

**Status: ✅ Implemented.**

**Gap**: `history.py` writes `run-<ts>.json` + `latest.json` to `fleet_history_dir`, but nothing
ever reads them back; users must `cat` JSON manually.

**What**: `fleet-update --history [N]` prints a table of the last N runs (timestamp,
changed/failed, per-type counts); `--history-show <ts|latest>` prints one run's stored
`briefing` text (already rendered and stored in every history record).

**How**: new `history_summary()` reader in `proxmox_fleet/history.py` (it already owns the file
naming/pruning conventions); flag parsing in `proxmox_fleet/cli.py` plus a pass-through alias in
`fleet-update.py`, early-exiting before `driver.run_fleet()`. Tests in
`tests/unit/test_history.py` and `test_cli.py`.

**Effort**: ~½ day. No new dependencies.

### S2. Generic webhook + Telegram notifiers

**Status: ✅ Implemented.**

**Gap**: `notifiers.dispatch()` supports only `discord` and `ntfy`; anything else needs code.

**What**: two new notifier types in the existing `notifiers:` list:

- `type: webhook` — POSTs `{title, body, failed, timestamp}` JSON to an arbitrary URL with
  optional headers (covers Slack/Mattermost/Home Assistant/n8n via their generic intakes).
- `type: telegram` — `sendMessage` via the Bot API (`bot_token`, `chat_id`).

**How**: extend the type dispatch in `proxmox_fleet/notifiers.py`, reusing `http.post_json` and
the existing per-notifier error-swallowing + `notifier_retries`. Schema additions in
`models/settings.py`; document in `vars.yml.example`. Tests in `test_notifiers.py`.

**Effort**: ~½ day each. No new dependencies (urllib already used).

---

## Medium

### M1. Host/phase targeting: `--limit` and `--phases`

**Status: ✅ Implemented.**

**Gap**: every run is fleet-wide; there is no way to re-run one failed host or test a single LXC
without editing `hosts.ini` or waiting through all phases.

**What**:

- `--limit host1,host2,105` — restrict the run to named inventory hosts and/or LXC/VM IDs.
- `--phases lxc,vm` — run only the listed phases (`remote|custom|lxc|vm|node|manager`);
  Pre-Flight and Phase 4 (notify/history) always run.

**How**: parse in `cli.py`/`fleet-update.py`; thread `limit: set[str] | None` and
`phases: set[str] | None` into `driver.run_fleet()`. Each `run_*_phase()` filters its host list;
the LXC phase additionally filters discovered container IDs (same place `exclude_list` is
applied). Hosts skipped by limit are silently omitted, mirroring the maintenance-window skip
behavior (`window.in_window`). Guard: `--limit` with Phase 2 node updates still honors the
serial / abort-on-first-failure semantics. Tests extend `test_driver.py` and `test_wrapper.py`.

**Effort**: ~2–3 days incl. tests. Touches `cli.py`, `fleet-update.py`, `driver.py`, and each
phase helper.

### M2. Snapshot/rollback for the `custom_update` flow (v2)

**Status: ✅ Implemented.**

**Gap**: `flows/custom.py` is explicitly "v1, no snapshot" — rescue runs an opaque
`rollback_command` and hopes. Yet most `[custom_hosts]` are LXCs/VMs on PVE, and all snapshot
machinery already exists (`executor.snapshot()`, `snapshot_with_retry()`, the
`BEFORE_UPDATE_AUTO` convention, `ansible/primitives/snapshot.yml`/`rollback.yml`).

**What**: optional per-config keys `pve_vmid` + `pve_node` (or host_vars) enabling
snapshot-before / rollback-on-failure / delete-in-finally, exactly mirroring the LXC/VM flow
shape (`try/except/finally`). `rollback_command` remains the fallback when no vmid is set.
Status strings gain `FAILED + ROLLED BACK` / `FAILED (NO SNAPSHOT)` parity via `status.py`.

**How**: extend `models/config.py` (`CustomConfig`), `flows/custom.py`, and
`status.custom_status()`; reuse `snapshot_with_retry` verbatim. New molecule scenario
`custom_update_rollback` alongside the existing six; unit tests in `test_flow_custom.py` /
`test_status_custom.py`.

**Effort**: ~3–4 days. Highest-value reliability win per line of code.

---

## Large

### L1. Canary / staged rollout

**Status: ✅ Implemented.**

**Gap**: Phases 0/1/1b update everything concurrently in one wave; a bad upstream package can
take out the whole fleet between two runs.

**What**: opt-in staged execution per phase. Hosts tagged `canary=true` (host_vars) or listed in
`canary_hosts` update first; the run then **soaks** (`canary_soak_minutes`, polling the existing
Uptime Kuma maps via `_pkg.kuma_healthy`) and proceeds to the remaining hosts only if no canary
failed and all monitored canaries are healthy. On canary failure: abort the wave, record the
remaining hosts as `SKIPPED (canary failed)`, notify.

**How**: split each phase's host list into waves inside `run_*_phase()` (driver-level, flows
untouched); the soak loop reuses `http.poll_until` + an injectable sleep (same pattern as
`orchestration.retry`). New settings in `models/settings.py`; the briefing gains a canary
section in `briefing.py` (mind the byte-parity golden test — add a new golden fixture variant).
Tests: `test_driver.py` wave/abort/soak cases with monkeypatched sleep.

**Effort**: ~1–2 weeks. Big blast-radius reduction.

### L2. Fleet web dashboard: pending updates (PatchMon-style) + run history + run trigger

**Status: 🚧 Partially implemented — `--scan` (pillar 1's data source) is done; the FastAPI app remains.**

**Gap**: the only visibility is a Discord/ntfy message and raw JSON files; no way to see what is
*pending* across the fleet, browse past runs, or kick off a run remotely. PatchMon covers the
pending-OS-packages slice but needs an agent per host and knows nothing about community-script
app versions, snapshots/rollbacks, or this orchestrator's runs — so build the combined view here.

**What** — one FastAPI app (`proxmox_fleet/web/`, console entrypoint `fleet-dashboard`) with
three pillars:

1. **Pending updates (agentless PatchMon analog)** — prerequisite piece: a new
   `fleet-update --scan` mode that walks the fleet read-only over the existing SSH/pct executor
   (no agents — the orchestrator already reaches every host) and writes
   `pending-<ts>.json`/`pending-latest.json` to `fleet_history_dir`:
   - OS packages per host: reuse `_pkg.detect_pkg_mgr` + the simulate commands already used for
     dry-run (`apt-get -s dist-upgrade` / `dnf check-update` / `apk version -l '<'`), parsing
     package lists with `LC_ALL=C`.
   - **App versions for LXCs** (the part PatchMon can't do): reuse the existing detect flow —
     `lxc._read_version()` (`~/.{scriptname}`) vs. the latest GitHub tag fetched on the manager
     (`lxc_parse.script_name_from_update` + `http.get_json`) → "sonarr 4.0.17 → 4.0.18 pending".
   - Dashboard renders: per-host pending counts, package lists, app current→latest,
     last-scan age.
2. **Run history** — latest run status, per-phase counts, error/warning log, run list with
   run-over-run diffs, per-host drill-down across runs. All read from `history.py` output
   (`run-*.json`/`latest.json`) — the data already exists; share the reader built for S1.
3. **Run trigger via the CLI** — `POST /runs` (token-auth) launches `fleet-update` **as a
   subprocess** (not in-process): the web app composes flags (`--dry-run`, `--force-notify`,
   `--limit`/`--phases` from M1, or `--scan`) and streams the child's stdout to the browser via
   SSE. Subprocess isolation keeps the web app responsive, reuses all CLI/venv behavior, and a
   file lock prevents overlapping runs (the CLI should honor the same lock so cron and web
   can't collide).

**How**: optional dependency group `pip install -e '.[web]'` (fastapi + uvicorn); Jinja/static
HTML, no JS build step. `--scan` lives in `cli.py`/`driver.py` as a read-only phase variant
reusing each flow's detect helpers — worth landing as its own PR before the web app. New
settings: `dashboard_bind`, `dashboard_token`, `scan_history_keep`. Tests: scan parsing in
`tests/unit` (scripted executors, like `test_flow_lxc.py`), web via `fastapi.testclient` against
fixture history/pending dirs; the subprocess trigger tested with a stub `fleet-update` script.

**Effort**: ~3–4 weeks (scan ~1 week, web app ~2–3). Depends on S1 (history reader) and benefits
from M1 (`--limit` exposed in the trigger UI).

---

## Suggested ordering

S1 → S2 → M1 → M2 → L1 → L2. S1's history reader and M1's `--limit` are direct prerequisites for
L2's dashboard (and within L2, the `--scan` CLI mode lands before the web app); M2 is independent
and can go any time.
