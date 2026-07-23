# Observability Roadmap: package tracking, security awareness, per-host history

**Status: 📋 Planned (2026-07-05) — not yet implemented.**

Staged as **4 PRs to `testing`**. PR1 ∥ PR2 independent; PR3 depends on PR2;
PR4 depends on PR1.

## Context

Follow-up to "what else could the dashboard / underlying code track?", seeded by
"exact packages updated by apt etc.". Exploration found the raw material already flows
through the code and is discarded at exactly one point per flow: every flow captures the
upgrade stdout (`flows/lxc.py:310`, `vm.py:116`, `remote.py:73`, `node.py:98`) then keeps
only a bool/count. The scan already parses pending package *names* but drops versions and
the security archive field. Nothing tracks per-host "last updated" or OS release upgrades.

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
- LXC dry-run returns before the OS step (`lxc.py:242-272`) → LXC package detail exists
  only on real runs; simulate-parsing applies to vm/remote/node.
- All record models are `extra="allow"` → new fields auto-serialize into `run-<ts>.json`.
- `render_briefing()` never reads the new fields → briefing golden test untouched.
- `--scan` and runs share the fleet flock (`cli.py:125`) → accumulator writes never race.
- **Pre-existing bug #1**: `changes.vm_pkg_count` apk branch matches `^Upgrading ` and
  misses the `(i/n) ` prefix → apk counts are always 0. Fix in PR1.
- **Pre-existing bug #2**: `scan.scan_cmd` apk branch contains `'<'`, but `scan_lxc`
  wraps the command in `pct exec {id} -- {shell} -c '{...}'` — nested single quotes break
  Alpine LXC scans. Fix in PR2 (`"<"`).

---

## PR 1 — Exact package tracking (parse → store → prune → display)

**New module `proxmox_fleet/pkg_detail.py`** (pure functions, manager-side):

- `parse_upgraded(stdout: str, pkg_mgr: str) -> List[Dict[str, str]]` →
  `[{"name","from","to"}]`, `from` may be `""` (apt new installs, dnf real).
  Regexes (MULTILINE): apt real `^Unpacking (\S+?)(?::\S+)? \(([^)]+)\) over \(([^)]+)\)`
  (+ no-`over` form); apt simulate `^Inst` with/without `[old]`; dnf real tokens between
  `^Upgraded:$` and next section (`rsplit("-", 2)` after stripping `.arch`); dnf
  `--assumeno` `Upgrading:` table; apk `^(?:\(\d+/\d+\) )?Upgrading (\S+) \((\S+) -> (\S+)\)`.
  De-duplicate, preserve order.
- `pkg_mgr_for_ostype(os_type: str) -> str` (single home for scan's ostype→mgr map).

**Schema** (`models/state.py`): `packages: Optional[List[Dict[str, str]]] = None` on
LxcRecord/VmRecord/RemoteRecord/NodeRecord (`None` so idle/dry/old records stay key-free).

**Capture sites**: success records only — `lxc.py` report (~448) from `os_res_stdout`;
`vm.py` (~168) from `pkg_res.stdout`; `remote.py` (~78) plus a free
`pkg_count=vm_pkg_count(...)` (`count_packages()` already reads it); `node.py` both
`run_node_update` and `run_manager_update`, plus `pkg_count`. Fix `vm_pkg_count` apk
branch (delegate to `len(parse_upgraded(stdout, "apk"))`).

**Retention** — strip-in-place (no sidecar: single-file read path, `write_history`
already owns pruning):

- `models/settings.py`: `fleet_package_detail_keep: int = 7` (≤0 → never strip).
- `history.py`: `_strip_package_detail(directory, keep_detail)` — remove `packages` keys
  from run files older than the newest N; idempotent (rewrite only when something
  removed); never touches `latest.json`. `write_history()` gains `keep_detail: int = 0`
  kwarg; `driver.run_notify_phase()` passes the setting. `count_packages()` unchanged
  (status-string/pkg_count based → totals.json unaffected by stripping).

**Dashboard**: append `"packages"` to `BUCKET_COLUMNS` (app.py:74); run_detail.html +
host.html special-case the column as a `<details>` disclosure (`N pkgs` summary, mono
`name from → to` lines) — the pending.html pattern, no JS.

**Tests**: new `test_pkg_detail.py` (realistic fixtures per manager incl. `(1/2)` apk
prefix, garbage input); one scripted-executor case per `test_flow_*.py` asserting
`record.packages`; `test_history.py` strip test (3 runs, keep_detail=1 → older two lose
the key, second call no-op, totals unchanged); `test_web.py` disclosure assertions;
briefing golden as tripwire. Molecule: no stub changes needed.

**Risks**: dnf5 output drift → empty list (count fallback still works — verify on the
real host at deploy); pacman LXCs parse to `[]` (fine).

## PR 2 — Scan: security + reboot-required + os-release capture

**`scan.py scan_cmd()`** grows sentinel-delimited sections in the *same single command*
(scan roundtrips are whole ansible-runner subprocesses — no new calls). No single quotes
anywhere (fixes bug #2):

- apt: simulate; `echo __FLEET_META__; test -f /var/run/reboot-required && echo
  reboot_required; cat /etc/os-release 2>/dev/null; exit $rc` (preserve real rc).
- dnf: `check-update; echo __FLEET_SEC__; check-update --security; echo __FLEET_META__;
  ...` (stays rc-100-tolerant).
- apk: version list + META section (no reboot flag — no such concept; `security` = []).

**New parsers** (next to `parse_pending`, which is reused unchanged):
`parse_scan_output(stdout, pkg_mgr) -> {"pending", "security", "reboot_required",
"os_release"}` and `parse_os_release(text) -> {"id","version_id","pretty_name"}`.
apt security = subset of Inst lines whose archive field matches `-security`.

**Snapshot schema** (hosts + lxc entries, lxc named `os_security*` to match
`os_pending*`): `security_count`, `security` (names), `reboot_required` (bool),
`os_release` ({id, version_id, pretty_name}). Old snapshots lack keys → readers use
`.get()`. `pending_summary()` rows gain `security_pending` + `reboot_hosts`.

**Dashboard**: pending.html Security column (fail pill >0) + reboot pill; overview
pending card gains `security pkgs` / `reboot required` stats; `_health_score()`
(app.py:173) adds `-min(20, 2·security_pending)` and `-min(10, 2·reboot_hosts)`.
Notifications deliberately skipped (scan bypasses Phase 4; briefing is golden-locked).

**Tests**: `test_scan.py` — section parsing per manager, sections-missing defaults,
os-release quoting, regression test that `scan_cmd("apk")` contains no `'`;
`test_web.py` — pills, stats, health-score cases incl. legacy rows.

## PR 3 — Per-host ledger: last-updated + OS-upgrade events

**New module `proxmox_fleet/ledger.py`** (`hosts.json`, totals.json-style accumulator —
survives run pruning; separate module so scan.py doesn't import history.py):

- `read_ledger(history_dir)` → `{"hosts": {...}, "events": [...]}` (corrupt → fresh).
- `observe_run(history_dir, summary)` — per record derive host key (lxc→name, vm→name,
  remote→host, node→node); set `last_run_ts`, `last_status`, and `last_changed_ts` when
  status matches `history._UPDATED_RE`. Called from `write_history()` after totals.
- `observe_scan(history_dir, scan)` — per entry with `os_release`, compare `version_id`
  (fallback pretty_name) to stored; on change append
  `{"type": "os-upgrade", "host", "from", "to", "ts"}` (events capped at 100) and update
  stored value. Called from `scan.write_pending()`.

os-release is captured **scan-only** — 6h timer resolution is ample for release
upgrades; zero flow/primitive/molecule changes. Trade-off documented: an upgrade is
logged at the next scan, not the moment it happens.

**Dashboard**: host page header — "last updated 3d ago · last run … · Debian 12
(bookworm)" (reuse `data-ts` localization); OS-upgrade events as timeline items; small
"Recent OS upgrades" list on the overview when events exist.

**Tests**: `test_ledger.py` (per-bucket key derivation, UPDATED-gating, first-observation
vs change, cap, corrupt file); writer wiring in `test_history.py`/`test_scan.py`;
`test_web.py` header + event rendering.

**Gotcha**: LXC ledger keys use container name (matches `/hosts/{name}` nav); dead hosts
linger in hosts.json — acceptable, `rm` rebuilds it observationally.

## PR 4 — Host pending timeline + package search

- `web/app.py _host_pending_entries(history_dir, name)` — this host's entry from each
  retained pending snapshot; merged with run records in `host_detail()` (lexical
  timestamp sort works). host.html renders `bucket == 'pending'` items with
  pending/security counts, reboot pill, package `<details>` (reusing PR1/PR2 rendering).
- `_search_packages(history_dir, q)` — case-insensitive substring over `packages` in all
  retained runs; new route `GET /packages?q=` → `packages.html` (form + results linking
  to `/history/{ts}` and `/hosts/{host}`); empty state + hint line spell out the
  short-retention window ("searching the last N runs with package detail"). Add a
  command-palette entry.
- Tests: `test_web.py` — pending timeline items, search hit/miss/blank.

## Cross-cutting

- `status.py` decision trees and all primitives unchanged; parsing stays manager-side
  with `LC_ALL=C` pinned commands.
- Briefing golden (`tests/unit/data/briefing_golden.json`) must pass untouched in every
  PR — run `pytest tests/unit/test_briefing.py` as a tripwire.
- mypy/ruff/bandit clean; new modules fully typed (3.10-compatible `List`/`Dict`).

## Verification (per PR)

1. Local: `pytest tests/unit/ -q`, `python -m mypy proxmox_fleet/`, `ruff check`,
   `bandit -q -ll -r proxmox_fleet/`.
2. Dashboard PRs (1, 2, 4): seeded demo dashboard + headless-chromium CDP screenshots,
   both themes + no-JS fallback.
3. PR → CI green (incl. molecule) → merge → deploy to the test manager LXC and restart
   `fleet-dashboard.service`.
4. On the test box: `./fleet-update.py --scan` (verify security/reboot/os_release land in
   `pending-latest.json`) and a real limited run (`--limit <one LXC>`) to verify
   `packages` appears in `run-*.json` and renders; after PR1, confirm stripping by
   inspecting a run file older than `fleet_package_detail_keep`.
