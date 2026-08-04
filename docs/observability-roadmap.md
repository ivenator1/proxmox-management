# Observability Roadmap: package tracking, security awareness, per-host history

**Status: ✅ Implemented (2026-08-04); pending merge and deployment acceptance.**

Designed as four review stages for `testing` (PR1 ∥ PR2 independent; PR3 depends on PR2;
PR4 depends on PR1). **Not yet deployed; no deployment-acceptance has been performed** —
the on-box verification steps at the bottom remain outstanding.

## Context

Follow-up to "what else could the dashboard / underlying code track?", seeded by
"exact packages updated by apt etc.". Exploration found the raw material already flows
through the code and was discarded at exactly one point per flow: every flow captures the
upgrade stdout (`flows/lxc.py`, `vm.py`, `remote.py`, `node.py`) then kept only a
bool/count. The scan already parsed pending package *names* but dropped versions and
the security archive field. Nothing tracked per-host "last updated" or OS release upgrades.

Confirmed scope:

1. Exact package tracking — **OS updates only** (LXC community-script app step stays
   dpkg-hash based; its stdout is PHS_SILENT-suppressed), with **short retention** —
   package-level detail from weeks ago is noise, not signal.
2. Security-update awareness in the pending scan.
3. Reboot-required tracking.
4. Host page: pending timeline + package search.
5. Per-host "last updated" tracking (survives run pruning).
6. OS release upgrade detection (e.g. Debian 12→13).

## Verified facts the design rests on

- apt simulate `Inst pkg [old] (new REPO)` lines carry the archive
  (`Debian-Security:...-security`) — security detection needs no extra command.
- apt real-run detail is in `Unpacking pkg[:arch] (NEW) over (OLD)` lines; dnf real has an
  `Upgraded:` section (multi-column, **no old version**); apk prints
  `(1/2) Upgrading pkg (old -> new)`.
- LXC dry-run returns before the OS step (`flows/lxc.py`) → LXC package detail exists
  only on real runs; simulate-parsing applies to vm/remote/node.
- All record models are `extra="allow"` → new fields auto-serialize into `run-<ts>.json`.
- `render_briefing()` never reads the new fields → briefing golden test untouched.
- `--scan` and runs share the fleet flock (`cli.py`) → accumulator writes never race.
- **Bug #1 (fixed in PR1)**: `changes.vm_pkg_count` apk branch matched `^Upgrading` and
  missed the `(i/n)` prefix → apk counts were always 0.
- **Bug #2 (fixed in PR2)**: `scan.scan_cmd` apk branch contained `'<'`, but `scan_lxc`
  wraps the command in `pct exec {id} -- {shell} -c '{...}'` — nested single quotes
  broke Alpine LXC scans. Fixed by using `"<"`.

---

## PR 1 — Exact package tracking (parse → store → prune → display) — ✅ implemented

**New module `proxmox_fleet/pkg_detail.py`** (pure functions, manager-side):

- `parse_upgraded(stdout: str, pkg_mgr: str) -> List[Dict[str, str]]` →
  `[{"name","from","to"}]`, `from` may be `""` (apt new installs, dnf real).
  Regexes (MULTILINE): apt real `^Unpacking (\S+?)(?::\S+)? \(([^)]+)\) over \(([^)]+)\)`
  (+ no-`over` form); apt simulate `^Inst` with/without `[old]`; dnf real tokens between
  `^Upgraded:$` and next section (`rsplit("-", 2)` after stripping `.arch`); dnf
  `--assumeno` `Upgrading:` table; apk `^(?:\(\d+/\d+\) )?Upgrading (\S+) \((\S+) -> (\S+)\)`
  (the `(i/n)` prefix is optional so `apk -s` simulate lines parse too). De-duplicated,
  order preserved; unknown managers and garbage yield `[]` — never an exception.
- `pkg_mgr_for_ostype(os_type: str) -> str` — single home for scan's ostype→mgr map
  (only alpine differs; unknown/unsupported OS types fall back to `"dnf"`).

**Schema** (`models/state.py`): `packages: Optional[List[Dict[str, str]]]` on
LxcRecord/VmRecord/RemoteRecord/NodeRecord, excluded when `None` (idle/dry/old records
stay key-free).

**Capture sites** — success records only:

- `flows/lxc.py` report: `os_packages` parsed from `os_res_stdout` **only when the OS
  step did not fail** (partial output is discarded; empty parse → `None`).
- `flows/vm.py`, `flows/remote.py`, `flows/node.py` (`run_node_update` **and**
  `run_manager_update`): parsed when changed. Simulated dry-run output is intentionally
  retained as would-update detail; `pkg_count` (new on RemoteRecord/NodeRecord, pre-existing
  on VmRecord) stays **real-run-only** so cumulative totals stay factual.
- **Bug #1 fix**: `changes.vm_pkg_count` apk branch now delegates to
  `len(parse_upgraded(stdout, "apk"))`.

**Retention** — strip-in-place (no sidecar: single-file read path, `write_history`
already owns pruning):

- `models/settings.py`: `fleet_package_detail_keep: int = 7` (≤0 → never strip).
- `history.py`: `_strip_package_detail(directory, keep_detail)` — removes `packages` keys
  in place from run files older than the newest N; idempotent (rewrites only when at least
  one key was removed); never touches `latest.json` (it mirrors the newest run, always
  retained) and never touches `totals.json` — package totals come from status strings /
  `pkg_count` (`count_packages()` is unchanged), so **totals are independent of the
  stripped detail**. `write_history()` gained a `keep_detail: int = 0` kwarg;
  `driver.run_notify_phase()` passes the setting.

**Dashboard**: `packages` appended to `BUCKET_COLUMNS` (app.py); run_detail.html +
host.html render it as a `<details>` disclosure (`N pkgs` summary, mono
`name from → to` lines) — the pending.html pattern, no JS.

**Tests**: new `test_pkg_detail.py` (realistic fixtures per manager incl. `(1/2)` apk
prefix, garbage input); one scripted-executor case per `test_flow_*.py` asserting
`record.packages`; `test_history.py` strip test (3 runs, keep_detail=1 → older two lose
the key, second call no-op, totals unchanged); `test_web.py` disclosure assertions;
briefing golden as tripwire. Molecule: no stub changes needed.

**Risks (accepted)**: dnf5 output drift → empty list (count fallback still works — the
real-host verification at deploy remains outstanding); pacman LXCs parse to `[]` (fine).

## PR 2 — Scan: security + reboot-required + os-release capture — ✅ implemented

**`scan.py scan_cmd()`** emits sentinel-delimited sections in the *same single command*
(scan roundtrips are whole ansible-runner subprocesses — no new calls):

- `__FLEET_SEC__` (dnf only: `check-update --security`; its rc is deliberately discarded)
  and `__FLEET_META__` (reboot flag from `/var/run/reboot-required` + `/etc/os-release`
  capture), followed by `exit $rc` so the scan section's real exit code survives while the
  metadata tail stays best-effort. No single quotes anywhere (bug #2 fixed — apk uses
  `"<"`); apk hosts get no reboot/security sections (Alpine has neither concept).
- apt security needs no extra command: `_apt_security_names()` is the subset of pending
  `Inst` lines whose archive field matches `*-security`.

**New parsers** (next to `parse_pending`, reused unchanged):
`parse_scan_output(stdout, pkg_mgr) -> {"pending", "security", "reboot_required",
"os_release"}` and `parse_os_release(text) -> {"id","version_id","pretty_name"}`
(extends the lxc_parse helper with `pretty_name`). Missing sections degrade to defaults
(whole output = pending, `security=[]`, no reboot, empty os_release) — never an exception.

**Snapshot schema** (`pending-<ts>.json`; lxc fields named `os_security*` to match
`os_pending*`):

- hosts entries: `security_count`, `security` (names), `reboot_required` (bool),
  `os_release` ({id, version_id, pretty_name}).
- lxc entries: `os_security_count`, `os_security`, `reboot_required`, `os_release` —
  keyed **`node/id`** (multi-cluster-safe).
- Old snapshots lack the keys → readers use `.get()`. `pending_summary()` rows gain
  `security_pending` + `reboot_hosts` (alongside the existing low_disk/os_mismatch/
  unreachable/errors).

**Dashboard**: pending.html Security column (fail pill >0) + reboot pill for both the
hosts and lxc tables; overview pending card gains `security pkgs` / `reboot required`
stats; `_health_score()` (app.py) adds `-min(20, 2·security_pending)` and
`-min(10, 2·reboot_hosts)`. Notifications deliberately skipped (scan bypasses Phase 4;
briefing is golden-locked).

**Tests**: `test_scan.py` — section parsing per manager, sections-missing defaults,
os-release quoting, regression test that `scan_cmd("apk")` contains no `'`;
`test_web.py` — pills, stats, health-score cases incl. legacy rows.

## PR 3 — Per-host ledger: last-updated + OS-upgrade events — ✅ implemented

**New module `proxmox_fleet/ledger.py`** (`hosts.json`, totals.json-style accumulator
next to the run files — survives run pruning; separate module so scan.py doesn't import
history.py):

- `read_ledger(history_dir) -> {"hosts": {...}, "events": [...]}` — missing, unreadable,
  or corrupt (syntactically or structurally) → fresh empty ledger; the ledger never fails
  a run or scan, and the observe functions swallow write errors.
- **Identities are multi-cluster-safe**: **lxc → `node/id`** (a bare vmid is not
  fleet-unique — two clusters can each have a 101), vm → `name`, remote → `host`,
  node/manager → `node`; custom records are excluded. Pending-scan lxc entries normalise
  to `node/id` too (`_scan_lxc_key`: the entry's explicit `node`/`id` fields win over the
  snapshot key; pre-PR3 bare-id keys are normalised; an old bare-keyed entry without a
  node is skipped rather than inventing `id/id`).
- `observe_run(history_dir, summary)` — per record sets `last_run_ts`/`last_status`, and
  `last_changed_ts` only when the record's OS status matches the shared
  `history._UPDATED_RE` predicate **and** it is not dry-run (NodeRecord persists an
  explicit `dry_run` marker — node/manager status strings have no dry-run variant, so
  without it simulation would look applied). Called from `write_history()` after the
  totals accumulator.
- `observe_scan(history_dir, scan)` — per entry with `os_release`, compares
  `version_id` (fallback `pretty_name`) to the stored value; the first observation is a
  baseline (stored, no event); a change prepends
  `{"type": "os-upgrade", "host", "from", "to", "ts"}` (newest first), events capped at
  100. Called from `scan.write_pending()` before pruning.
- os-release is captured **scan-only** — 6h timer resolution is ample for release
  upgrades; zero flow/primitive/molecule changes. Trade-off documented: an upgrade is
  logged at the next scan, not the moment it happens.

**Dashboard**: host page header — "last updated … · last run … · Debian 12 (bookworm)"
from the ledger (reusing the `data-ts` localization); OS-upgrade events as timeline
items; a small "Recent OS upgrades" list on the overview (newest 8 events) when any
exist.

**Tests**: `test_ledger.py` (per-bucket key derivation, UPDATED-gating incl. dry-run,
first-observation vs change, cap, corrupt file); writer wiring in
`test_history.py`/`test_scan.py`; `test_web.py` header + event rendering.

**Gotcha**: dead hosts linger in hosts.json — acceptable, `rm` rebuilds it
observationally.

## PR 4 — Host pending timeline + package search — ✅ implemented

- `web/app.py _host_pending_entries(history_dir, name)` — this host's entry from each
  retained timestamped pending snapshot (`pending-latest.json` excluded — it duplicates
  the newest timestamped file); lxc entries match by `node/id` (or bare name/id, or the
  legacy bare-id key shape). `_host_timeline()` merges these (tagged
  `bucket == "pending"`) with run records and ledger os-upgrade events (`kind == "event"`)
  — all three carry the same timestamp shape, so one lexical sort interleaves them.
  host.html renders `bucket == 'pending'` items with pending/security counts, reboot pill,
  and the package `<details>` (reusing PR1/PR2 rendering).
- `_search_packages(history_dir, q)` — case-insensitive substring over `packages`
  (matching name, `from`, and `to`) in timestamped `run-*.json` files only (never
  `latest.json`); de-duplicated, corrupt files skipped, newest run first. New route
  `GET /packages?q=` → `packages.html` groups hits by run with `/history/{ts}` and
  `/hosts/{node}/{id}` links; empty state + a retention hint ("package detail is kept on
  the newest N run(s)", driven by `fleet_package_detail_keep`). Command-palette entry
  added.
- Tests: `test_web.py` — pending timeline items, search hit/miss/blank, version-only
  queries, legacy records, retention-note setting.

## Cross-cutting

- `status.py` decision trees and all primitives unchanged; parsing stays manager-side
  with `LC_ALL=C` pinned commands.
- Briefing golden (`tests/unit/data/briefing_golden.json`) must pass untouched — run
  `pytest tests/unit/test_briefing.py` as a tripwire.
- mypy/ruff/bandit clean; new modules fully typed (3.10-compatible `List`/`Dict`).

## Verification

Performed in this changeset:

1. Full unit suite plus the explicit briefing golden tripwire.
2. mypy, ruff, Bandit, JavaScript syntax, and diff checks.
3. Seeded dashboard smoke pass in headless Chromium: overview, pending, run detail, host,
   and package-search pages in both themes and with JavaScript disabled (85 DOM/behavior
   assertions; screenshots generated, but no manual pixel-level review).

Outstanding — do not claim as done:

1. PR → CI green (incl. molecule) → merge to `testing`.
2. On the test box: `./fleet-update.py --scan` (verify security/reboot/os_release land in
   `pending-latest.json`) and a real limited run (`--limit <one LXC>`) to verify
   `packages` appears in `run-*.json` and renders; after PR1, confirm stripping by
   inspecting a run file older than `fleet_package_detail_keep`.
