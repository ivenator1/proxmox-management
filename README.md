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
* **Controlled Parallelism:** Updates LXCs across multiple nodes simultaneously to save time, while rebooting physical nodes sequentially to maintain Cluster Quorum and HA stability.
* **Apt-Proxy Awareness:** Optimized for environments using `apt-cacher-ng`; automatically waits for the proxy service to be online before allowing subsequent nodes to start updates.
* **Tag-Based Discovery:** Only processes LXCs tagged `community-script` or `proxmox-helper-scripts` in PVE — untagged containers are never touched.
* **Multi-Host Support:** Handles LXC containers, QEMU VMs, non-Proxmox remote hosts, and config-driven custom systems in a single run.
* **Flexible Backup Strategy:** Choose per-run between lightweight snapshots, full `vzdump` backups (including PBS), both, or none.
* **Snapshot-Lock Retry:** Transient Proxmox task locks (`CT is locked`) are retried automatically (up to 3 times, 15 s apart by default) before a snapshot failure is recorded as a non-fatal warning.
* **Configurable Timeouts & Retries:** All hardcoded timeouts and retry counts (snapshot, apt proxy wait, notifier, dead-man ping, node apt) are exposed as `vars.yml` fields — override per environment without touching code.
* **Resource Scaling:** Automatically scales container CPU/RAM up during build-heavy app updates and back down afterward, matching the behaviour of the community-scripts bash installer.
* **Dry-Run Mode:** Compare installed vs. latest GitHub release versions without applying any changes.
* **Accurate Change Detection:** OS packages are updated before the community script runs, so each line is correctly attributed. For apps without a version file, a `dpkg-query` package-state hash is taken before and after the community script — if it matches, the container is silent even if the script produced output. This prevents false-positive "UPDATED" reports caused by `PHS_SILENT=1` suppressing apt's stdout inside community scripts.
* **Consolidated Reporting:** Aggregates results from every node and container into exactly one Discord (or ntfy) notification, with a structured error log showing which host, which task, and what the error was.
* **Maintenance Windows:** Per-host time/day windows in `host_vars`; `force_window=true` bypasses. Invalid window config fails loud at load time.
* **Web Dashboard:** Optional `fleet-dashboard` web UI (`pip install -e '.[web]'`) — pending updates across the fleet (agentless, PatchMon-style, including community-script app versions), browsable run history with per-host drill-down, and a token-protected run trigger with live console output (SSE). Triggered runs launch the CLI as a detached subprocess under the shared fleet run lock, so dashboard, cron, and shell runs can never collide.

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
* **SSH Trust:** Passwordless SSH keys distributed from the Manager to all Proxmox Nodes (`ssh-copy-id`).
* **API Token:** A Proxmox API Token for `root@pam` with "Privilege Separation" unchecked.
* **Uptime Kuma:** A Public Status Page (e.g., slug: `proxmox-sg1`) containing the monitors to be validated.

## 📂 Project Structure
```text
~/proxmox-management/
├── fleet-update.py                  # Runnable wrapper — ./fleet-update.py [--dry-run|--force-notify|…]
├── ansible.cfg                      # Performance & connection settings
├── hosts.ini                        # List of nodes (gitignored — copy from .example)
├── vars.yml                         # Credentials and cluster config (gitignored — copy from .example)
├── pyproject.toml                   # Package config + the fleet-update console-command entrypoint
├── .ansible-lint                    # Lint profile and skip rules
├── .yamllint.yml                    # YAML style rules
├── .github/workflows/ci.yml         # CI: yamllint, ansible-lint, syntax-check, pytest, mypy, ruff, molecule
├── proxmox_fleet/                   # Python control plane (the "brain")
│   ├── cli.py / driver.py           # Entrypoint + run_fleet() orchestrator
│   ├── flows/                       # Per-host control flows (custom/lxc/vm/remote/node)
│   ├── executor.py                  # Executor protocol + RunnerExecutor + snapshot_with_retry()
│   ├── status.py / changes.py       # Decision trees + change detection
│   ├── briefing.py / notifiers.py / history.py   # Phase 4 (briefing/notify/history)
│   └── models/                      # Pydantic schemas (config, state, settings)
├── ansible/primitives/             # Single-purpose Ansible execution primitives (no logic); includes batched read primitives (lxc_introspect, lxc_post_update)
├── configs/                         # custom_update config files (gitignored; *.example committed)
├── tests/
│   ├── requirements.txt             # pytest, pyyaml
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
* `notifiers`: List of notifier configs (type `discord` or `ntfy`). If unset, `discord_webhook` is used as a back-compat single notifier; an explicit `[]` means no notifications.

### 🌐 Networking (Apt-Cacher NG)
* `apt_proxy_ip` / `apt_proxy_port`: If the node hosting your proxy reboots, the orchestrator will pause and wait for this IP/Port to respond before continuing.

### 🚥 Uptime Kuma Integration
* `kuma_url` / `kuma_slug`: Points to your Kuma instance and the specific Status Page slug.
* `lxc_kuma_map` / `vm_kuma_map` / `remote_kuma_map`: Map an inventory hostname or LXC ID to an Uptime Kuma Monitor ID. The orchestrator waits up to `kuma_health_check_retries` × `kuma_health_check_delay` seconds (default 5×30 s) for Kuma to report `status: 1`.

### 🔄 Backup Strategy
* `lxc_backup_strategy`: `snapshot` (default) | `vzdump` | `both` | `none`
* `lxc_backup_storage`: PVE storage name for `vzdump`. Set to your PBS storage name (as shown in Datacenter → Storage) to route backups to PBS — no other change needed.

### 🏷️ LXC Tag Discovery
* `lxc_tags`: List of PVE tags that mark community-scripts containers (default: `community-script`, `proxmox-helper-scripts`). Set tags in PVE UI → Container → Options → Tags.
* `lxc_dry_run`: Set to `true` to compare installed vs. latest GitHub release versions without making any changes.
* `lxc_unattended`: Sets `PHS_SILENT=1` inside containers to suppress interactive prompts.
* `lxc_backup_strategy` / `lxc_auto_reboot` / `lxc_continue_on_error`: See `vars.yml.example` for defaults.

### 🛡️ Management & Exclusions
* `manager_lxc_id`: The VMID of the Manager LXC itself. The node hosting this container is never rebooted automatically.
* `exclude_list`: LXC IDs completely skipped (no updates, no snapshots).
* `os_update_exclude_list`: Skip `apt dist-upgrade` / `apk upgrade` for these IDs (app update still runs).
* `app_update_exclude_list`: Skip the community-script app update for these tagged LXC IDs (the OS update still runs).
* `os_only_lxc_list`: Pull these *untagged* LXC IDs into discovery for OS-only management — they have no `/usr/bin/update`, so the app line reports `NO SCRIPT`.
* `snapshot_exclude_list`: Updates run but no snapshot is taken (use for LXCs with bind mounts).
* **Note:** Phase 2 (node OS updates) runs serially with abort-on-first-failure to protect cluster quorum.

### ⏱️ Timeouts & Retries
All previously-hardcoded timeouts and retry counts are overridable per environment in `vars.yml`
(see `vars.yml.example` / `GlobalSettings` for full defaults):
* `apt_proxy_check_timeout` / `node_reboot_port_wait_timeout`: How long to wait for the apt-cacher-ng proxy / a rebooted node's SSH port to come back.
* `snapshot_retries` / `snapshot_retry_delay`: Retry attempts and delay for transient `CT is locked` snapshot failures.
* `notifier_retries` / `deadmans_retries`: Retry attempts for notifier dispatch and dead-man's-switch pings.
* `node_apt_retries` / `node_apt_retry_delay`: Retry attempts and delay for the Phase 2 node OS update.

### 📨 Notifications, History & Dead-Man's Switch
* `force_notify`: Send a Discord/ntfy notification even if nothing changed (same as `--force-notify`).
* `fleet_history_enabled` / `fleet_history_dir` / `fleet_history_keep`: Each run writes `<dir>/run-<UTC-timestamp>.json` and overwrites `<dir>/latest.json`, pruned to the newest N (`0` = keep all).
* `fleet_deadmans_url`: Pinged at the end of every run (e.g. a [healthchecks.io](https://healthchecks.io)-style URL) so its *absence* alerts you if the orchestrator stops running; `<url>/fail` is pinged on failure.

## 🚀 Setup Instructions
To set up this project from scratch on a brand-new Manager LXC, follow these steps in order. This ensures all dependencies are met and SSH trust between your Manager and your Proxmox nodes is established correctly.

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

# Install required Ansible Collections (for the execution primitives)
ansible-galaxy collection install community.proxmox community.general
```

### 3. Establish SSH Trust (Passwordless Login)
The orchestrator SSHs into your Proxmox nodes to run primitives and gather state.
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
```
Populate `hosts.ini` and `vars.yml` with your cluster-specific configuration.

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

### All Flags
```
--dry-run / --check      Simulate everything — no changes, no reboots, no snapshots
--force-notify / --notify  Send a notification even if nothing changed
--verbose                Enable verbose LXC output
--force-window           Bypass per-host maintenance-window checks
-e KEY=VALUE             Pass a raw extra var (repeatable; e.g. -e custom_allow_reboot=false)
--inventory PATH         Inventory file (default: hosts.ini)
--vars-file PATH         Settings YAML (default: vars.yml)
```

### Automated Schedule (Cron)
Add this to the Manager LXC's `crontab -e` to run at 4:00 AM daily:
```cron
0 4 * * * cd /root/proxmox-management && /usr/bin/python3 fleet-update.py >> /var/log/fleet-update.log 2>&1
```

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
