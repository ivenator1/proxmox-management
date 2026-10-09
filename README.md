# Proxmox Cluster Orchestrator

A professional-grade automation engine for maintaining a Proxmox VE High-Availability (HA) cluster. A typed-Python control plane (`proxmox_fleet/`) owns all decision logic, orchestration, and reporting; Ansible is reduced to a catalogue of single-purpose execution primitives. The `fleet-update` CLI drives everything — there is no monolithic playbook.

---

## 📡 Overview
The Proxmox Cluster Orchestrator moves maintenance from a manual process to a Tiered Recovery Model:

1. **Tier 1 (Disaster):** Integration with Proxmox Backup Server (PBS).
2. **Tier 2 (Rapid Recovery):** Automatic Pre-update Snapshots (with Bind Mount awareness).
3. **Tier 3 (Validation):** Real-world health validation via Uptime Kuma Status Page API.
4. **Tier 4 (Self-Healing):** Automatic Snapshot Rollback if an application fails to respond post-update.

## ✨ Key Features
* **Location-Aware Updates:** Dynamically detects which physical node is hosting the Manager LXC and skips that node's reboot.
* **NVIDIA Post-Upgrade Checks:** Nodes flagged `nvidia_host=true` get read-only GPU-driver diagnostics after their node update — loaded vs. next-boot module versions, target-kernel DKMS state, and an `nvidia-smi` probe. A module mismatch flags a reboot; an unprepared target kernel is a hard failure.
* **Controlled Parallelism:** Updates LXCs across multiple nodes simultaneously to save time, while rebooting physical nodes sequentially to maintain Cluster Quorum and HA stability.
* **Apt-Proxy Awareness:** Optimized for environments using `apt-cacher-ng`; automatically waits for the proxy service to be online before allowing subsequent nodes to start updates.
* **Tag-Based Discovery:** Processes LXCs tagged `community-script` or `proxmox-helper-scripts`, plus explicitly configured OS-only containers.
* **Automatic Alloy Compliance:** Optional desired-state enforcement installs Grafana Alloy from its official apt repository, validates/deploys the guest config, grants journal access, and repairs the service on every managed LXC/VM. `--alloy-only` audits or migrates guests without regular updates or snapshots.
* **Opt-In Log Housekeeping:** When enabled, managed LXCs get NPM/PBS file-log retention in Loki: a 48-hour (size-capped) local buffer, acknowledged import of all pre-existing file-log history *before* anything is pruned, scoped Alloy file sources, and recurring APT/Yarn/npm/pnpm cache cleanup. `--housekeeping-only` performs that maintenance with no updates, snapshots or reboots, and writes to its own history namespace.
* **Multi-Host Support:** Handles LXC containers, QEMU VMs, non-Proxmox remote hosts, and config-driven custom systems in a single run.
* **Flexible Backup Strategy:** Choose per-run between lightweight snapshots, full `vzdump` backups (including PBS), both, or none.
* **Snapshot-Lock Retry:** Transient Proxmox task locks (`CT is locked`) are retried automatically (up to 3 times, 15 s apart by default) before a snapshot failure is recorded as a non-fatal warning.
* **Configurable Timeouts & Retries:** All hardcoded timeouts and retry counts (snapshot, apt proxy wait, notifier, dead-man ping, node apt) are exposed as `vars.yml` fields — override per environment without touching code.
* **Resource Scaling:** Automatically scales container CPU/RAM up during build-heavy app updates and back down afterward, matching the behaviour of the community-scripts bash installer.
* **Dry-Run Mode:** Compare installed vs. latest GitHub release versions without applying any changes.
* **Accurate Change Detection:** OS packages are updated before the community script runs, so each line is correctly attributed. For apps without a version file, a `dpkg-query` package-state hash is taken before and after the community script — if it matches, the container is silent even if the script produced output. This prevents false-positive "UPDATED" reports caused by `PHS_SILENT=1` suppressing apt's stdout inside community scripts.
* **Consolidated Reporting:** Aggregates results from every node and container into exactly one Discord (or ntfy) notification, with a structured error log showing which host, which task, and what the error was.
* **Maintenance Windows:** Per-host time/day windows in `host_vars`; `force_window=true` bypasses. Invalid window config fails loud at load time.
* **Canary Staging:** Hosts flagged `canary=true` (or listed in `canary_hosts`) update first in the remote/LXC/VM phases; the rest run only if every canary succeeded and — after a configurable soak window — its Uptime Kuma monitor is healthy. A failed gate records the remainder as `SKIPPED (canary failed)`.
* **Targeted Runs:** `--phases lxc,vm` runs only the named phases; `--limit pve-01,105` restricts every phase to specific host names and/or LXC/VM ids — ideal for re-running a single failed container.
* **Pending-Updates Scan:** `fleet-update --scan` is a strictly read-only fleet walk (pending OS packages per host plus community-script app current → latest per LXC, plus read-only manual-update checks for `[manual_update_hosts]` appliances — see below) that feeds the dashboard's pending view. `install.sh` schedules it every 6 hours via `fleet-scan.timer`.
* **Manual-Update Monitoring:** TrueNAS SCALE and OPNsense appliances are *tracked, never auto-updated* — the six-hour `--scan` runs fixed read-only vendor checks, over SSH by default (`midclt` for TrueNAS, `opnsense-version` + `opnsense-update -c` for OPNsense) or optionally over the appliance REST API (`manual_adapter=opnsense_api` / `truenas_scale_api` with per-host `api_url`/`api_key`/`api_secret`/`verify_ssl`). Scans refresh dashboard/reminder state without notifying; due manual attention piggybacks on the next normal fleet briefing. Applying the update always stays a manual GUI action on the appliance.
* **Run History & Replay:** Every run persists a JSON record to `fleet_history_dir`; `--history [N]` tables recent runs and `--history-show latest` replays a stored briefing.
* **Fleet Run Lock:** A fleet-wide `flock` guarantees the dashboard trigger, the systemd timer, cron, and manual shell runs can never mutate the fleet concurrently.
* **Web Dashboard:** Optional `fleet-dashboard` web UI (`pip install -e '.[web]'`, or via `install.sh`) — session-based login (admin account, password set during install), pending updates across the fleet (agentless, PatchMon-style, including community-script app versions), browsable run history with per-host drill-down (plus a separate `/housekeeping` maintenance-history namespace), a run trigger with live console output (SSE), an inventory & enrollment page (add hosts to `hosts.ini`, generate/push/test SSH keys from the browser — no manual `ssh-copy-id` needed), and a comment-preserving `vars.yml` settings editor. Triggered runs launch the CLI as a detached subprocess under the shared fleet run lock.
* **One-Shot Installer:** `./install.sh` sets up the venv, all dependencies, and reboot-persistent systemd units for the dashboard, the scan timer, and (when enabled) the hourly housekeeping timer; `--update` and `--uninstall` round-trip it.

## 🐍 Python Control Plane
A typed-Python "brain" (`proxmox_fleet/`) owns all decision logic, config/state
schemas, orchestration, and reporting. Ansible is reduced to a catalogue of
single-purpose execution primitives (`ansible/primitives/*.yml`) invoked through
`ansible-runner` — no logical branching inside any primitive. `driver.run_fleet()`
is the sole entrypoint, driven by either the `fleet-update.py` wrapper (recommended)
or the `fleet-update` console command.

```bash
# On the Manager LXC (Debian — system Python is externally managed):
apt install python3.13-venv          # match your Python version
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                     # installs fleet-update console command + deps

# fleet-update.py auto-bootstraps into .venv — no activation needed:
./fleet-update.py --dry-run --force-notify   # fleet-wide dry-run with notification
./fleet-update.py                            # full run
./fleet-update.py --help                     # full flag reference + examples

# Dev / CI:
pip install -e '.[dev]'
python -m mypy proxmox_fleet/
pytest tests/unit/ -v
```

`driver.run_fleet()` runs the pre-flight apt-proxy check then every phase in order
(remote → custom → lxc → vm → node + manager → briefing), threading one `FleetState`
through and dispatching the Discord/ntfy briefing at the end. The decision logic
lives in `status.py`/`changes.py`/`deps.py`/`window.py`, the per-host flows in
`flows/*.py`, manager-local IO in `http.py`, and the byte-parity briefing in
`briefing.py`. See `docs/migration-roadmap.md` for the completed migration history.

## 🛠 Prerequisites
* **Manager LXC:** A dedicated LXC (e.g., Debian 12+, VMID 121) with a static IP.
* **SSH Trust:** Passwordless SSH keys distributed from the Manager to all Proxmox Nodes — via `ssh-copy-id` or the dashboard's Inventory & enrollment page (see step 3).
* **API Token:** A Proxmox API Token for `root@pam` with "Privilege Separation" unchecked.
* **proxmoxer ≥ 2.3:** required by the `community.proxmox` collection (2.x); installed into the project venv together with `ansible-core` (step 2 / `install.sh`).
* **Uptime Kuma:** A Public Status Page (e.g. slug: `proxmox-sg1`) containing the monitors to be validated.
* **Guest `acl` (only for log housekeeping):** the targeted NPM/PBS guests need the `acl` package installed **once** during initial setup so scoped read/traverse ACLs can be granted to the `alloy` user. Recurring maintenance never installs packages — if `setfacl`/`getfacl` is missing or access cannot be repaired, that profile is reported `Blocked` and its file logs are retained.

## 📂 Project Structure
```text
~/proxmox-management/
├── fleet-update.py                  # Runnable wrapper — ./fleet-update.py [--dry-run|--scan|--limit|…]
├── install.sh                       # Root installer: venv + deps + systemd units (dashboard service, 6h scan timer, hourly housekeeping timer when enabled)
├── ansible.cfg                      # Performance & connection settings
├── hosts.ini                        # List of nodes (gitignored — copy from .example)
├── vars.yml                         # Credentials and cluster config (gitignored — copy from .example)
├── pyproject.toml                   # Package config + fleet-update / fleet-dashboard console entrypoints
├── .ansible-lint                    # Lint profile and skip rules
├── .yamllint.yml                    # YAML style rules
├── .github/workflows/ci.yml         # CI: yamllint, ansible-lint, syntax-check, pytest, mypy, ruff, molecule
├── proxmox_fleet/                   # Python control plane (the "brain")
│   ├── cli.py / driver.py           # Entrypoint + run_fleet() orchestrator
│   ├── flows/                       # Per-host control flows (custom/lxc/vm/remote/node)
│   ├── executor.py / alloy.py       # Execution adapters + shared Alloy compliance policy
│   ├── housekeeping.py             # LXC maintenance sequencing and logging preparation
│   ├── housekeeping_stream.py      # Bounded transport, decompression and log framing
│   ├── housekeeping_sources.py     # Source lineage, frozen-prefix completion and current coverage
│   ├── housekeeping_native.py      # Guarded native policy writes and logrotate parsing
│   ├── housekeeping_retention.py   # Delivery verification and centralized deletion authorization
│   ├── housekeeping_import.py / housekeeping_checkpoint.py   # Loki acknowledgement, capture and resume
│   ├── housekeeping_cache.py       # Fixed native cache profiles and successful-clean cadence
│   ├── status.py / changes.py       # Decision trees + change detection
│   ├── briefing.py / notifiers.py / history.py   # Phase 4 (briefing/notify/history)
│   ├── scan.py / lock.py            # Read-only pending-updates scan (incl. manual-update state refresh) + fleet-wide run lock
│   ├── scan_notifications.py        # Manual scan state + fleet-briefing first/change/daily-reminder selection
│   ├── manual_updates.py            # Read-only adapter checks: SSH (midclt / opnsense-update -c) + REST-API (*_api) variants
│   ├── web/                         # fleet-dashboard FastAPI app ('.[web]' extra): pages, run trigger,
│   │                                # inventory enrollment, SSH key setup, vars.yml settings editor
│   └── models/                      # Pydantic schemas (config, state, settings)
├── ansible/primitives/             # Single-purpose Ansible execution primitives (no logic); includes batched read primitives (lxc_introspect, lxc_post_update)
├── configs/                         # custom_update config files (gitignored; *.example committed)
├── config_templates/                # Full commented custom_update schema (custom_system.yml.example)
├── docs/                            # migration-roadmap.md, FEATURE_ROADMAP.md
├── tests/
│   ├── requirements.txt             # pytest, pyyaml, web deps for test_web
│   └── unit/                        # pytest tests — no Ansible or PVE required
└── roles/
    ├── lxc_update/molecule/        # Molecule scenarios driving flows/lxc.py
    └── custom_update/molecule/     # Molecule scenarios driving flows/custom.py
```

## ⚙️ Configuration (vars.yml)
The `vars.yml` file is the central intelligence of the orchestrator.

### 🔑 Authentication & Notifications
* `pve_api_...`: Credentials for the Proxmox API. Required for snapshots and rollbacks.
* `discord_webhook`: Your unique Discord Webhook URL for the morning briefing.
* `notifiers`: List of notifier configs (types: `discord`, `ntfy`, `webhook`, `telegram`) — the same briefing body is fanned out to every enabled target. If unset, `discord_webhook` is used as a back-compat single notifier; an explicit `[]` means no notifications.

### 🌐 Networking (Apt-Cacher NG)
* `apt_proxy_ip` / `apt_proxy_port`: If the node hosting your proxy reboots, the orchestrator will pause and wait for this IP/Port to respond before continuing.

### 🚥 Uptime Kuma Integration
* `kuma_url` / `kuma_slug`: Points to your Kuma instance and the specific Status Page slug.
* `lxc_kuma_map` / `vm_kuma_map` / `remote_kuma_map`: Map an inventory hostname or LXC ID to an Uptime Kuma Monitor ID. The orchestrator waits up to `kuma_health_check_retries` × `kuma_health_check_delay` seconds (default 5×30 s) for Kuma to report `status: 1`.

### 📈 Grafana Alloy Guest Compliance
Alloy enforcement is opt-in and applies only to guests already managed by the LXC/VM phases.

```bash
cp configs/guest.alloy.example configs/guest.alloy
# Edit configs/guest.alloy and replace REPLACE_WITH_LOKI_ENDPOINT.
```

Then set `alloy_enabled: true` in `vars.yml`. Add any log receiver or guest with a purpose-specific Alloy config to `lxc_alloy_exclude_list`; use bare IDs fleet-wide or `cluster/ID` for one cluster. `vm_alloy_exclude_list` uses VM inventory names. The example's journal `max_age` defaults to `48h` and can be aligned by the housekeeping retention drop-in; do not add a second journal reader. When log housekeeping is enabled, the per-guest desired config is extended with fleet-managed file sources (the base journal pipeline and stored positions are preserved); `--alloy-only` prepares those sources but runs no cache cleanup or file pruning.

Audit first, then migrate:

```bash
./fleet-update.py --dry-run --alloy-only
./fleet-update.py --alloy-only --limit 101,media-vm
./fleet-update.py --alloy-only
```

Alloy-only mode honors limits, exclusions, VM maintenance windows, and `--force-window`, but takes no snapshots and skips package/app updates, reboots, and Kuma workload checks. A missing/placeholder desired config or unresolved guest drift produces an amber attention notification while the run remains successful (`rc=0`, successful dead-man ping). Once every guest reports compliant, retire the separate `install-alloy-guests.yml` rollout.

### 🗄️ Log Housekeeping for Managed LXCs (opt-in)
An opt-in LXC maintenance feature that retains Nginx Proxy Manager (NPM) and Proxmox Backup Server (PBS) file logs in Loki, keeps a short local buffer, and runs recurring cache cleanup. Ordinary LXC runs opt in with `housekeeping_enabled: true` (housekeeping runs before disk warnings, snapshots, and updates so cache reclamation can prevent a storage refusal); `--housekeeping-only` requests the same feature with no update work at all. Enabled settings require `housekeeping_loki_url`: an absolute `http(s)` base with no credentials, query, or fragment. Explicit maintenance without a configured endpoint can still clean eligible caches, but reports blocked logging and never authorizes log deletion. The manager appends `/loki/api/v1/...` itself.

```bash
# Audit intended actions first, then apply — limited to the log guests:
./fleet-update.py --housekeeping-only --check --limit 120,123
./fleet-update.py --housekeeping-only --limit 120,123
./fleet-update.py --housekeeping-only --check      # fleet-wide audit
./fleet-update.py --housekeeping-only              # fleet-wide apply
```

`--housekeeping-only` is incompatible with `--scan`, `--alloy-only`, explicit `--phases`, and `--history`/`--history-show`. It touches only managed LXCs — stopped and template guests are skipped (never started), and an Alloy-excluded guest that is explicitly ID-targeted gets an explanatory warning with no retention/file-pruning actions (ordinary exclusions are skipped silently). It uses a single worker to bound importer disk/network use.

Policy (`vars.yml` → `housekeeping_*`):

* **48 h local retention:** files older than `housekeeping_local_retention_hours` are pruned only after their full current content has acknowledged Loki coverage, their identity/digest is unchanged, no writer/task is active, current permissions and owned policy hashes match, Alloy is healthy with the expected configuration, and Loki readiness currently passes against the verified delivery endpoint. A batch acknowledgement is not frozen-prefix completion; frozen-prefix completion is not current-file coverage. Every initial file prefix must be acknowledged before any file-log pruning begins; until then the run reports `Backfill pending` and resumes on later runs within `housekeeping_backfill_budget_mb` per guest per run.
* **Hard journald caps:** a fleet-owned drop-in sets `SystemMaxUse`/`SystemKeepFree`/`MaxRetentionSec` from the settings and restarts journald only on drift. `.journal` files are never deleted by hand.
* **Cache cleanup** runs at most every `housekeeping_cache_interval_hours` (24 h), independently of Loki/backfill health.

**Cache cleanup** uses only fixed allowlisted roots and each native tool's own command — `apt-get clean`, `yarn cache clean`, `npm cache clean --force`, `pnpm store prune` — for APT `/var/cache/apt/archives`; Yarn `/usr/local/share/.cache/yarn` or `/root/.cache/yarn`; npm `/root/.npm`; pnpm `/root/.local/share/pnpm/store`. It never runs autoremove, never deletes browser caches, application dependencies/databases, unsupported or configured roots, and never touches PBS datastores. Busy package/build activity or a failed command leaves that tool's successful cadence unchanged; missing cache directories are simply idle.

**Deployment prerequisites (initial only, never recurring):** the affected NPM/PBS guests need the `acl` package so scoped read/traverse ACLs can be granted to the `alloy` user during initial setup — install it once per targeted guest while APT is idle. Recurring maintenance never installs packages: if `setfacl`/`getfacl` is missing, or log access cannot be repaired, that profile reports `Blocked` (and, in standalone mode, the run fails) while its file logs are retained.

Permission verification checks effective access as the `alloy` user and separately requires an inherited named-user ACL with an effective read/traverse mask on every managed log directory. A successful permission-repair command alone never authorizes archival or retention.

Native policy reads are bounded and JSON-framed so transport trimming cannot change line endings, file identity hashes, or the original-rule backup. Logrotate cutover retains the existing writer mechanisms and unrelated policy bytes.

**Delivery gates and outage behaviour:** import sends at most a 512 KiB push body per request and only HTTP 204 acknowledges a batch; timeouts/429/5xx are retried with bounded backoff, other 4xx fail immediately, and a failed batch reports `Blocked` with progress intact. A readiness/query failure pauses fleet-managed file deletion even for already-acknowledged files. Applications' own native rotation limits and journald's hard caps remain finite buffers during an outage — this feature does not promise infinite outage retention. PBS `task-log-max-days` is not shortened to two days; existing native limits still apply independently. PBS task index/control files and active task logs are never pruned.

**Backfill and spool:** pre-existing history is frozen once into a root-only (`0700`/`0600`) node spool under `/var/tmp/fleet-log-import/<sha256(cluster/node/id)>/<capture_id>/` (requiring node free space ≥ captured bytes + 2 GiB) so native rotation cannot erase history mid-import, then streamed oldest-first. Each captured blob is released only after every one of its bytes is acknowledged, even if other frozen prefixes still need backfill. Releases use bounded shell-command batches; an interruption preserves the remaining blobs and resumes from durable per-blob progress without re-archiving acknowledged content. The checkpoint database lives at `<fleet_history_dir>/housekeeping.sqlite3`; a missing DB starts an import, a corrupt/unwritable one blocks file-log deletion rather than guessing.

Frozen inputs share bounded multi-range snapshot fetches: at most 64 ranges and 32 MiB raw data per tar, with a 64 KiB manifest limit. Every entry is validated against its exact source request before reading; each source retains its own raw digest and HTTP acknowledgement. Prefetched bytes are not archive coverage. Live-source prefix verification remains separate.

**Quarantine and recovery:** approved closed files retain their source-relative path beneath a same-root `.fleet-housekeeping-quarantine` directory. Missing nested parents (including PBS task hex fanouts and nested NPM archives) are created with `0700` permissions through pinned directory descriptors; symlinked/non-directory ancestors are rejected. A failed prune transport keeps its durable intents for revalidation and recovery on a later maintenance run. Initial archive completion is not proof that file cleanup succeeded.

Reclaimed file-log space is the observed decrease in allocated filesystem usage around deletion, clamped to zero—not a guaranteed sum of the removed files' allocated blocks. Directory metadata and concurrent filesystem activity can affect the measurement; deletion completion is reported separately as `files_pruned`.

Primitive execution uses transient Ansible Runner directories, removed after results are harvested on success or failure so large manifests cannot accumulate on a manager's `/tmp` tmpfs. API callers that explicitly provide `private_data_dir` retain their diagnostic artifacts. Durable housekeeping checkpoints and frozen-source spools are separate and are never removed by this cleanup. A Runner failure without task stderr reports its status and return code instead of an unrelated manifest tail.

Capture creation and resume reject symlinked, foreign-owned, or group/other-accessible spool directories. An unsafe spool blocks import rather than resetting checkpoints or removing retained history.

**Querying live vs. archive:** each file source emits two labelled streams — the live Alloy reader (`delivery="live"`, immediate visibility) and the acknowledged importer (`delivery="archive"`, the authoritative retained history). Query them separately to avoid double-counting. Lines are packed JSON, so `| unpack` restores the original log line from `_entry` and exposes `filename`:

```logql
# Live NPM application logs, original log line restored:
{job="lxc-file", delivery="live", app="nginxproxymanager"} | unpack | line_format "{{.filename}}: {{._entry}}"

# Archived history for one container (survives rotation):
{job="lxc-file", delivery="archive", guest_id="123", app="nginxproxymanager"} | unpack | filename=~"/data/logs/.*"
```

### 🔄 Backup Strategy
* `lxc_backup_strategy`: `snapshot` (default) | `vzdump` | `both` | `none`
* `lxc_backup_storage`: PVE storage name for `vzdump`. Set to your PBS storage name (as shown in Datacenter → Storage) to route backups to PBS — no other change needed.

### 🏷️ LXC Tag Discovery
* `lxc_tags`: List of PVE tags that mark community-scripts containers (default: `community-script`, `proxmox-helper-scripts`). Set tags in PVE UI → Container → Options → Tags.
* `lxc_dry_run`: Set to `true` to compare installed vs. latest GitHub release versions without making any changes.
* `lxc_unattended`: Sets `PHS_SILENT=1` inside containers to suppress interactive prompts.
* `lxc_disk_warn_percent` / `lxc_disk_min_free_gb`: Disk safety uses both utilization and absolute free space (defaults: 75% and 10 GiB). Large roots with ample free space may pass the community-script 80% guard; genuinely constrained roots remain blocked.
* `lxc_backup_strategy` / `lxc_auto_reboot` / `lxc_continue_on_error`: See `vars.yml.example` for defaults.

### 🛡️ Management & Exclusions
* `manager_lxc_id`: The VMID of the Manager LXC itself. The node hosting this container is never rebooted automatically.
* `exclude_list`: LXC IDs completely skipped (no updates, no snapshots).
* `os_update_exclude_list`: Skip `apt dist-upgrade` / `apk upgrade` for these IDs (app update still runs). Successful real APT upgrades finish with `apt-get clean`; dry runs and failed upgrades do not remove cached archives.
* `app_update_exclude_list`: Skip the community-script app update for these tagged LXC IDs (the OS update still runs).
* `os_only_lxc_list`: Pull these *untagged* LXC IDs into discovery for OS-only management — they have no `/usr/bin/update`, so the app line reports `NO SCRIPT`.
* `snapshot_exclude_list`: Updates run but no snapshot is taken (use for LXCs with bind mounts).
* **Note:** Phase 2 (node OS updates) runs serially with abort-on-first-failure to protect cluster quorum.

### 📟 Manual-Update Monitoring (scan-only host checks)
Appliances the fleet must never auto-update — TrueNAS SCALE, OPNsense firewalls, vendor-managed boxes — are **tracked, not updated**. The existing six-hour `fleet-update --scan` runs read-only checks per `[manual_update_hosts]` host, records the results in the pending snapshot's `manual` bucket, and refreshes reminder state without dispatching. Due first/change/daily reminders are included in the next ordinary fleet-run briefing. Manual host contact remains part of the scan walk, **not** a `run_fleet()` phase; the fleet run only reads persisted scan state. Applying the update always stays a manual GUI action on the appliance.

* `manual_update_notifications` (default `true`): Record manual attention during scans and include due reminders in fleet briefings.
* `manual_update_reminder_hours` (default `24`): Reminder cadence while a host stays pending — the first notice fires immediately, then again daily until the update is applied or the state changes.
* `manual_update_forks` (default `2`): Parallel adapter checks during the scan.
* `manual_update_api_timeout` (default `120`): Per-request timeout (seconds) for the HTTP-API manual checks (`manual_adapter=*_api`); OPNsense's firmware/status check hits update mirrors synchronously and can exceed the default 30s.

These settings are **scan-only** — read only by the `--scan` path, never by `run_fleet()`, and deliberately **not** accepted as `-e` extra vars (the five-key `-e` allowlist is unchanged). Set them in `vars.yml`.

#### Inventory: `[manual_update_hosts]`
```ini
[manual_update_hosts]
truenas-01  ansible_host=10.10.10.60  manual_adapter=truenas_scale
opnsense-01 ansible_host=10.10.10.61  manual_adapter=opnsense

[manual_update_hosts:vars]
ansible_user=root
ansible_ssh_private_key_file=~/.ssh/id_ed25519
ansible_python_interpreter=/usr/bin/python3
```
Every host requires `manual_adapter=truenas_scale|opnsense|truenas_scale_api|opnsense_api|truenas_scale_ws`; a missing or blank value fails loudly at inventory load time, before any host is contacted. Optional per-host `display_name` (report/reminder label) and `apply_hint` (free-form GUI note) go in `host_vars/<name>.yml`.

**Overlap safety & migration:** a hostname in `[manual_update_hosts]` must NOT also appear in `[remote_hosts]`, `[proxmox_vms]`, `[custom_hosts]`, or `[proxmox_nodes]` — that overlap is rejected loudly (naming the host and both groups) both in the scan and in the `run_fleet()` pre-flight, so no machine is ever both manually and automatically updated. To move a TrueNAS/OPNsense box off auto-update: **remove** its line from *every* auto-update group first, then add it here with `manual_adapter=` — do it in its own commit so a stale checkout can never run both paths for the same host.

**Transport:** manual hosts use the same SSH/Ansible transport as the rest of the fleet by default — passwordless root SSH from the manager (the `:vars` block above carries the standard defaults; on OPNsense enable root SSH under System → Settings → Administration → Secure Shell). Alternatively, `manual_adapter=opnsense_api` / `truenas_scale_api` runs the same checks manager-side over HTTPS against the appliance REST API (TrueNAS `/api/v2.0` bearer token; OPNsense `/api/core` HTTP Basic with `api_key`:`api_secret`) — per-host `api_url` (blank → defaults to `https://<ansible_host>`), `api_key`, `api_secret` (OPNsense only), and `verify_ssl` (default `true`; set `false` for self-signed appliance certs), inline in `hosts.ini` or in `host_vars/<name>.yml` (both gitignored). Both transports are strictly read-only — version/status endpoints only, never apply/update endpoints — and share the same result shape; SSH remains the default. Connection failures (DNS/timeout/refused) and HTTP 4xx/5xx errors are reported distinctly, and a **TLS certificate verification failure is an error, not "unreachable"** — it means `verify_ssl=false` for that host (or a trusted cert on the appliance), so the scan surfaces it loudly instead of silently skipping.

#### Getting appliance API keys (for `*_api` adapters)
- **OPNsense** — create a **dedicated user** (System → Access → Users → add, e.g. `fleet-ro`) and give it exactly one privilege: **System: Firmware**. There is no read-only ACL variant — that page privilege is the minimum that covers `GET /api/core/firmware/info` and `GET /api/core/firmware/status`, and it also grants the page's write endpoints, so never use the `root`/admin account's key; the dedicated user is the boundary. Then open the user → **API keys** → **+** and copy the generated **key + secret** (shown once) into `api_key` / `api_secret`. No SSH access is needed for API hosts.
- **TrueNAS SCALE** — log in to the web UI, click the user icon (top-right) → **API Keys** → **Add**, name it (e.g. `fleet-manager`), and **copy the token immediately** — it is displayed only once. The key inherits the account's permissions, so generate it from a restricted account if you do not want full admin reach. Put it in `api_key` (sent as `Authorization: Bearer`).

#### Read-only check commands (never install anything)
The scan runs one fixed, sentinel-delimited read-only command per adapter (or fixed read-only API calls for the `*_api` adapters) and parses the output manager-side. There is **no install/apply operation anywhere** — each adapter validates its command invariants before host contact and refuses to run if a forbidden token appears:

| Adapter | Read-only command | Forbidden |
|---|---|---|
| `truenas_scale` | `midclt call system.version` + `midclt call update.check_available` (JSON parsed in Python) | `apt`, `jq`, `updater`, `apply`, `upgrade` |
| `opnsense` | `opnsense-version` + `opnsense-update -c` (check mode only) | bare `pkg`, `upgrade`, `apply`, `updater` |
| `truenas_scale_api` | `GET /api/v2.0/system/version` + `POST /api/v2.0/update/check_available` (Bearer token) | — (GET/POST-check only; never update/apply endpoints) |
| `truenas_scale_ws` | WebSocket JSON-RPC `wss://<host>/api/current`: `auth.login_with_api_key` → `system.version` → `update.check_available` (falls back to `update.available_versions` + `update.status` on TrueNAS 25.10+, where the method was removed) | — (read methods only; never update/apply methods) |
| `opnsense_api` | `GET /api/core/firmware/info` → `POST /api/core/firmware/check` → poll `GET /api/core/firmware/upgradestatus` → `GET /api/core/firmware/status` (HTTP Basic `api_key`:`api_secret`) | — (check endpoint only; never update/apply endpoints) |

TrueNAS status mapping: `AVAILABLE` → update pending, `REBOOT_REQUIRED` → reboot pending, `CURRENT`/`UNAVAILABLE` → up to date, anything else → check error. OPNsense classification uses rc + output for SSH: `A newer version is available` (or rc=2) → update pending, `Nothing to do.` / `Currently up to date.` → up to date, rc=1 or unrecognized output → **fails closed** with an error — the scan never guesses. The API adapter maps the machine statuses (`update`/`update_available`/`update_available_major` → pending, `none`/`nothing_to_do` → up to date, `reboot_required` → reboot, `error`/`connection_failure` → check error) and **triggers a fresh check first** — since OPNsense ~22.7 the status endpoint only reports the *last* check, so the adapter POSTs `/api/core/firmware/check` and polls `upgradestatus` until the check finishes before reading the result, keeping the monitor from going stale.

> **Before first use on OPNsense:** run `opnsense-update -c` locally on the firewall (check-only, safe) and confirm its output matches one of the fixtures under `tests/unit/data/manual_updates/opnsense_*.txt`. If a future OPNsense release changes the wording, the scan reports an error instead of misclassifying — capture the local check-only output and confirm the parser fixture before the box goes quiet.

#### Notifications (first → change → daily reminder)
When `manual_update_notifications` is enabled, each scan refreshes `manual-notify-state.json` but sends nothing. The next fleet run selects each host on **first** observation, **change** (semantic fingerprint differs), then a **reminder** every `manual_update_reminder_hours` (default 24 h) while unchanged. Due entries force the normal Phase 4 notifier fan-out and appear in one node-like subsection; every host detail is nested beneath it. **Unreachable hosts are skipped** with state left untouched, a clean host clears its own entry, and a `--limit` scan never wipes hosts it did not observe.

Sample subsection (identical across notifier types):

```text
**Manual Systems: (ATTENTION REQUIRED)**
- Updates / reboots
  - **truenas** — 25.04.0.2 → 25.10.0 (reboot required)
    - upgrade: 25.04.0.2 -> 25.10.0
    - GUI apply: TrueNAS GUI → System Settings → Update
  - **firewall** — 24.7.11 → 25.1
    - Target release: 25.1
    - GUI apply: OPNsense GUI → System → Firmware → Status
```

**Run totals stay untouched:** persisted manual entries never enter `FleetState`; they cannot change `changed`/`failed`, package totals, the run exit code, or the dead-man failure signal. The combined briefing text is retained in run history, while the structured manual data stays in scan snapshots/state. A manual-only briefing is amber, not a fleet failure. The scan's own exit code still turns 1 when a manual check errored (like any host scan error), and the dashboard health score applies a small per-pending-action deduction.

#### Dashboard & snapshots
Pending snapshots carry a top-level `manual` mapping (keyed by the stable inventory hostname) that feeds the dashboard's **/pending** page — the "Manual systems" table (platform, current → available, action pills, details, apply hint, notes) and the per-scan "Manual updates" / "Manual reboots" columns. Each manual entry links to its `/hosts/<name>` page, and the per-host ledger observes manual systems for OS release/upgrade events only — a manual update is an admin action, never a fleet-applied change.

### 🎮 NVIDIA Node Post-Upgrade Checks
Proxmox nodes with NVIDIA GPUs must opt in with `nvidia_host=true`, either inline in `hosts.ini`:

```ini
[proxmox_nodes]
gpu-node ansible_host=10.10.10.10 nvidia_host=true
```

or in host vars:

```yaml
# host_vars/gpu-node.yml
nvidia_host: true
```

Keep the flag absent or `false` on non-NVIDIA nodes. After an opted-in node's OS update it gets read-only driver diagnostics, executed from the Manager LXC via Ansible SSH on the Proxmox node itself (never inside a workload LXC). The control plane classifies the results:

* **Running vs. target kernel** — the configured Proxmox next-boot pin, boot-loader override, or automatic kernel selection determines the kernel that will be validated; the newest `/boot` image alone does not override an explicit selection.
* **Target vs. loaded module mismatch** — a newer target-kernel driver than the running kernel has loaded flags the node as requiring a reboot.
* **Missing target modules or installed DKMS state** — a hard failure; the node reports `FAILED` rather than being rebooted. The old running kernel's on-disk module may be absent while its module remains loaded; a transient userspace mismatch is cleared by the pending reboot.
* **`nvidia-smi` faults** — non-fatal before a pending reboot caused by a driver mismatch, but fatal during the strict post-reboot validation.

Reboot policy is unchanged and covers all nodes: `node_auto_reboot` defaults to `true`. Set it to `false` for controlled/manual maintenance — nodes that need a reboot report `UPDATED (MANUAL REBOOT REQ)` and are never rebooted automatically. Manager-host protection remains: the node hosting the Manager LXC is never rebooted automatically.

### ⏱️ Timeouts & Retries
All previously-hardcoded timeouts and retry counts are overridable per environment in `vars.yml`
(see `vars.yml.example` / `GlobalSettings` for full defaults):
* `apt_proxy_check_timeout` / `node_reboot_port_wait_timeout`: How long to wait for the apt-cacher-ng proxy / a rebooted node's SSH port to come back.
* `snapshot_retries` / `snapshot_retry_delay`: Retry attempts and delay for transient `CT is locked` snapshot failures.
* `snapshot_timeout` / `snapshot_api_timeout`: Overall snapshot-task wait (default 600s) and per-request Proxmox API timeout (default 30s), sized for large disks and slow storage.
* `notifier_retries` / `deadmans_retries`: Retry attempts for notifier dispatch and dead-man's-switch pings.
* `node_apt_retries` / `node_apt_retry_delay`: Retry attempts and delay for the Phase 2 node OS update.

### 🐤 Canary Staging
* `canary_hosts`: Inventory names and/or LXC/VM ids that form wave 1 of the remote/LXC/VM phases; remote hosts and VMs can also be flagged `canary=true` in `hosts.ini`/`host_vars`.
* `canary_soak_minutes`: How long to wait after the canary wave before checking each Kuma-monitored canary; the remaining hosts run only if no canary failed and every monitored one is healthy.

### 📨 Notifications, History & Dead-Man's Switch
* `force_notify`: Send a notification even if nothing changed (same as `--force-notify`).
* `fleet_history_enabled` / `fleet_history_dir` / `fleet_history_keep`: Each run writes `<dir>/run-<UTC-timestamp>.json` and overwrites `<dir>/latest.json`, pruned to the newest N (`0` = keep all). Read back with `--history` / `--history-show`.
* `scan_history_keep`: How many `pending-*.json` scan snapshots (`--scan`) to keep in the same directory.
* `fleet_deadmans_url`: Pinged at the end of every ordinary run (e.g. a [healthchecks.io](https://healthchecks.io)-style URL) so its *absence* alerts you if the orchestrator stops running; `<url>/fail` is pinged on failure.
* **Housekeeping history:** hourly `--housekeeping-only` runs write their own `run-*.json`/`latest.json` into `<fleet_history_dir>/housekeeping/`, with `fleet_history_keep` applied separately in that directory and no package-detail stripping. The main `latest.json`, normal `run-*.json` retention, package-detail window, cumulative update totals and update dead-man state are all unaffected. Read it with `--history --history-kind housekeeping` / `--history-show <ref> --history-kind housekeeping`, or the dashboard's `/housekeeping` pages. Maintenance runs still notify and persist history, but they do **not** ping `fleet_deadmans_url` — hourly cleanup must never clear or mask a failed scheduled update.

### 🖥️ Web Dashboard
* `dashboard_host` / `dashboard_port`: Bind address and port for `fleet-dashboard` (default `0.0.0.0:8421`).
* **Housekeeping history:** protected `/housekeeping` and `/housekeeping/{ref}` pages show the hourly maintenance runs (own namespace, `history_base="/housekeeping"`); the existing `/history` pages remain the update history. The run trigger has a **Housekeeping only** checkbox (`--housekeeping-only`, mutually exclusive with scan/alloy-only/phases).
* **Authentication:** All pages require login. Single `admin` account with password set during `install.sh`. Session-based auth via HTTP-only cookies (SQLite database in `fleet_history_dir/.fleet-users.db`). No configuration needed — password is prompted during installation.

## 🚀 Setup Instructions
To set up this project from scratch on a brand-new Manager LXC, follow these steps in order. This ensures all dependencies are met and SSH trust between your Manager and your Proxmox nodes is established correctly.

### ⚡ Quick Install (install.sh)
On a fresh Manager LXC, `install.sh` automates steps 2 and 6 below and wires everything into
systemd — it prompts for a dashboard admin password, creates the `.venv`, installs the
package (with the web-dashboard extras), ansible-core and the Ansible collections, seeds
`vars.yml`/`hosts.ini` from the `.example` templates if missing, initializes the login
database, and installs + enables the units that persist across reboots:
`fleet-dashboard.service` (the web UI on port 8421, login required) and
`fleet-scan.timer` (`fleet-update --scan` every 6 hours), plus
`fleet-housekeeping.timer` (`fleet-update --housekeeping-only` hourly) which is
written on every install but enabled only when `housekeeping_enabled: true`.

```bash
git clone https://github.com/ivenator1/proxmox-management.git
cd proxmox-management
./install.sh                  # as root

./install.sh --update         # later: git pull + reinstall deps + restart services
./install.sh --uninstall      # remove units + venv (keeps vars.yml/hosts.ini; asks about history)
```

You still need step 1 (create the LXC) first, and afterwards your inventory and SSH trust —
both of which can be done entirely from the dashboard's **Inventory & enrollment** page
(generate a key, push it to each host with its password once, test the login) after signing
in with the admin account created during install. The manual steps below remain valid if you
prefer to set things up by hand.

### 1. Create the Manager LXC
* **OS:** Debian 12+ (recommended) or Ubuntu 22.04/24.04.
* **Resources:** 1 CPU, 1 GB RAM, 8 GB Disk.
* **Network:** Assign a Static IP.

### 2. Install Core Software
Log into your new Manager LXC and install the Python toolchain, Git, and the Ansible collections used as execution primitives:
```bash
# Update the OS
apt update && apt upgrade -y

# Install Python venv support and Git
apt install -y python3-venv git

# Create a virtualenv and install the orchestrator + all dependencies
python3 -m venv .venv
source .venv/bin/activate
# (run after cloning — see step 4)
pip install -e .

# Ansible itself + proxmoxer live in the venv too — community.proxmox 2.x
# needs proxmoxer >= 2.3 importable by the interpreter Ansible runs under
# (requests is proxmoxer's undeclared HTTPS-backend dependency).
pip install ansible-core 'proxmoxer>=2.3' requests

# Install required Ansible Collections (for the execution primitives)
ansible-galaxy collection install community.proxmox community.general
```

### 3. Establish SSH Trust (Passwordless Login)
The orchestrator SSHs into your Proxmox nodes to run primitives and gather state. The same passwordless root SSH is required for every remote host, VM, and `[manual_update_hosts]` appliance (TrueNAS/OPNsense) the scan checks — see "Manual-Update Monitoring" below.

> **GUI alternative:** if the dashboard is running (e.g. via `install.sh`), the
> **Inventory & enrollment** page does all of this from the browser — generate an
> ed25519 key, push it to each host (the host's password is asked once and never
> stored), and test the passwordless login. Skip to step 4 if you use it.

1. **Generate your SSH Key:**
   ```bash
   ssh-keygen -t ed25519 -C "fleet-manager"
   # Press Enter for all prompts (no passphrase)
   ```
2. **Copy the key to every Proxmox Node:**
   Run this for every node in your cluster:
   ```bash
   ssh-copy-id root@<NODE_IP>
   ```
   *You will be asked for the root password of each node one last time.*
3. **Test the connection:**
   ```bash
   ssh root@<NODE_IP>
   # Should log in without a password. Type 'exit' to return.
   ```

### 4. Clone and Configure
Clone the project repository:
```bash
git clone https://github.com/ivenator1/proxmox-management.git ~/proxmox-management
cd ~/proxmox-management
```
Copy the example files to create your own configuration:
```bash
cp vars.yml.example vars.yml
cp hosts.ini.example hosts.ini
cp configs/guest.alloy.example configs/guest.alloy  # only when enabling Alloy
```
Populate `hosts.ini` and `vars.yml` with your cluster-specific configuration. Before enabling Alloy, replace the endpoint placeholder in the ignored `configs/guest.alloy` file and exclude any log receiver or guest with a purpose-specific config.

### 5. Configure Ansible Hosts
Ensure `hosts.ini` sets the correct interpreter for each group so Ansible's executor primitives use the right Python:
```ini
[proxmox_nodes:vars]
ansible_user=root
ansible_python_interpreter=/usr/bin/python3
```

### 6. Install the Package and Verify
With the virtualenv active, install the package and run the connectivity test:
```bash
source .venv/bin/activate
pip install -e .
ansible-galaxy collection install community.proxmox community.general

# Verify Ansible can reach all nodes
ansible all -i hosts.ini -m ping
```
If everything comes back green, run your first dry-run:
```bash
./fleet-update.py --dry-run --force-notify
```

`fleet-update.py` detects `.venv/bin/python` at the repo root and re-execs into it
automatically, so you do not need to activate the venv before running it.

## 🏃 Usage

`fleet-update.py` is the recommended interface — no venv activation needed, friendly
flags, and a built-in `--help`. Run it from the project root.

### Dry Run (No Changes, Forces Notification)
```bash
./fleet-update.py --dry-run --force-notify
```

### Full Live Run
```bash
./fleet-update.py
```

### Full Run with Maintenance-Window Bypass
```bash
./fleet-update.py --force-window
```

### Targeted Runs
```bash
./fleet-update.py --dry-run --phases lxc --limit 105   # re-check one container, no changes
./fleet-update.py --phases vm --limit media-vm         # only the VM phase, one VM
```

### Alloy Audit / Migration Only
```bash
./fleet-update.py --dry-run --alloy-only             # probe only
./fleet-update.py --alloy-only --limit 101,media-vm  # limited repair
./fleet-update.py --alloy-only                       # all managed LXC/VM guests
```
`--alloy-only` cannot be combined with `--scan` or `--phases`.

### Log Housekeeping Only
```bash
./fleet-update.py --housekeeping-only --check            # audit log/cache maintenance, no changes
./fleet-update.py --housekeeping-only --check --limit 120,123
./fleet-update.py --housekeeping-only --limit 120,123    # apply for just these guests
./fleet-update.py --housekeeping-only                    # all eligible managed LXCs
```
Log import requires `housekeeping_loki_url`; the feature is normally switched on
with `housekeeping_enabled: true`. No updates, snapshots, reboots or stopped-container
starts are performed. Cache cleanup runs independently of import health, while
file pruning waits for full acknowledged coverage and verified live delivery.

### Pending-Updates Scan & Run History
```bash
./fleet-update.py --scan                 # read-only: what *would* update, fleet-wide
./fleet-update.py --history 5            # table of the last 5 persisted runs
./fleet-update.py --history-show latest  # replay a stored run's briefing
./fleet-update.py --history 5 --history-kind housekeeping         # hourly maintenance history
./fleet-update.py --history-show latest --history-kind housekeeping
```
`--history-kind` selects the parent `fleet` history (default) or the separate
`housekeeping` namespace the hourly timer writes into; it is only valid together
with `--history`/`--history-show`.

### All Flags
```
--dry-run / --check        Simulate everything — no changes, no reboots, no snapshots
--force-notify / --notify  Send a notification even if nothing changed
--verbose                  Enable verbose LXC output
--force-window             Bypass per-host maintenance-window checks
--limit HOST,ID,...        Restrict the run to these host names and/or LXC/VM ids
--phases P1,P2             Run only these phases (remote,custom,lxc,vm,node,manager)
--scan                     Read-only pending-updates scan → pending-*.json (no fleet run; refreshes manual-update state)
--alloy-only               Audit/repair Alloy on managed LXC/VM guests; skip regular updates and snapshots
--housekeeping-only        Log/cache housekeeping on managed LXCs only; no updates, snapshots or reboots
--history [N]              Show the last N persisted runs and exit (default: 10)
--history-show TS|latest   Print one persisted run's briefing and exit
--history-kind KIND        History namespace for --history/--history-show: fleet (default) or housekeeping
-e KEY=VALUE               Raw extra var (repeatable). Only fleet_dry_run, lxc_verbose,
                           force_notify, force_window, custom_dry_run are honoured; any
                           other key is accepted and silently ignored.
--inventory PATH           Inventory file (default: hosts.ini)
--vars-file PATH           Settings YAML (default: vars.yml)
```

### Automated Schedule
`install.sh` already schedules the read-only scan every 6 hours (`fleet-scan.timer`).
When `housekeeping_enabled: true`, it also writes and enables the hourly
`fleet-housekeeping.timer` → `fleet-housekeeping.service`
(`fleet-update --housekeeping-only`, oneshot, 1 h start timeout, low CPU/IO
weight, `RandomizedDelaySec=300`, `Persistent=true`). The units are written on
every install but the timer is **enabled only when the setting resolves true** —
install and `--update` reconcile the enabled state, so flipping
`housekeeping_enabled` off and re-running the installer stops the timer again. A
timer tick that meets a running fleet job refuses and exits; the next tick retries.

For unattended *update* runs, add a cron entry on the Manager LXC (`crontab -e`),
e.g. 4:00 AM daily:
```cron
0 4 * * * cd /root/proxmox-management && /usr/bin/python3 fleet-update.py >> /var/log/fleet-update.log 2>&1
```
The fleet-wide run lock means a cron run, a dashboard-triggered run, and a manual
shell run can never collide — late starters exit immediately with a clear message.

## 🧪 Development & Testing

No Proxmox infrastructure is needed to run the tests.

```bash
# Python unit tests (decision logic, briefing byte-parity, state, flows) with coverage
pip install -e '.[dev]'
pytest tests/unit/ -v --cov=proxmox_fleet --cov-report=term-missing
python -m mypy proxmox_fleet/

# Static analysis
ruff check proxmox_fleet/ tests/ fleet-update.py
python3 -m py_compile fleet-update.py
yamllint .
ansible-lint ansible/primitives/

# Security scan (medium+ severity)
bandit -r proxmox_fleet/ -ll

# Molecule (drives the Python flows via stub pct/vzdump scripts, against localhost)
cd roles/lxc_update && molecule test -s lxc_update_normal
cd roles/custom_update && molecule test -s custom_update_normal
```

CI runs automatically on push/PR via GitHub Actions (`.github/workflows/ci.yml`),
covering yamllint, ansible-lint, syntax-check, pytest (Python 3.10/3.11/3.12 matrix with
coverage), mypy, ruff, bandit security scan, and molecule scenarios.

## 📡 Discord Briefing Format
The orchestrator sends one consolidated embed per run:
* **Per-Node sections:** Node status (`OK` / `UPDATED & REBOOTED` / `UPDATED (MANUAL REBOOT REQ)` / `FAILED`), followed by each changed LXC and VM.
* **App status per container:** `Updated: X → Y` (version changed), `UPDATED` (packages changed, no version file), `OK` (nothing changed), `NO SCRIPT` (no community-script update binary), `FAILED`.
* **OS status per container:** `Updated (N upgraded)` (with package count), `OK`, `SKIPPED` (in `os_update_exclude_list`), `FAILED`.
* **Alloy status per guest:** `Installed`, `Configured`, or `Service repaired` appears only when remediation changed the guest; compliant guests stay silent.
* **Housekeeping line per guest:** `Housekeeping: <status> (<reclaimed> reclaimed; <archived> archived)` appears only when a housekeeping summary exists (statuses `Configured`, `Cleaned`, `Backfill pending`, `Blocked`, `Audit`). A housekeeping failure never changes OS/app update status or update counts; standalone maintenance still fails on blocked operations.
* **Remote Hosts section:** Listed separately (not tied to a PVE node).
* **Error Log:** Structured entries showing which host failed, which task failed, and the first 300 characters of stderr.
* Containers where nothing changed produce no embed entry — they are absorbed into `*No container changes.*` for that node.

## 📜 License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0)**. See the [`LICENSE`](LICENSE) file for the full text.

You are free to use, modify, and distribute this software under the terms of the GPL-3.0. Any modified versions distributed to others must also be released under the GPL-3.0.

## 📣 Notices

This project includes code derived from [community-scripts/ProxmoxVE](https://github.com/community-scripts/ProxmoxVE), licensed under the MIT License:

> MIT License
>
> Copyright (c) 2021-2026 tteck | community-scripts ORG
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

See the [`NOTICES`](NOTICES) file for complete third-party attribution.
