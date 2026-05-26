# Proxmox Cluster Orchestrator

A professional-grade, Ansible-based automation engine for maintaining a Proxmox VE High-Availability (HA) cluster. This project manages the simultaneous update of numerous LXC containers across multiple nodes while ensuring cluster quorum, service availability, and automated self-healing.

---

## 📡 Overview
The Proxmox Cluster Orchestrator moves maintenance from a manual process to a Tiered Recovery Model:

1. **Tier 1 (Disaster):** Integration with Proxmox Backup Server (PBS).
2. **Tier 2 (Rapid Recovery):** Automatic Pre-update Snapshots (with Bind Mount awareness).
3. **Tier 3 (Validation):** Real-world health validation via Uptime Kuma Status Page API.
4. **Tier 4 (Self-Healing):** Automatic Snapshot Rollback if an application fails to respond post-update.

## ✨ Key Features
* **Location-Aware Updates:** Dynamically detects which physical node is hosting the Ansible Manager LXC and skips that node's reboot.
* **Controlled Parallelism:** Updates LXCs across multiple nodes simultaneously to save time, while rebooting physical nodes sequentially to maintain Cluster Quorum and HA stability.
* **Apt-Proxy Awareness:** Optimized for environments using `apt-cacher-ng`; automatically waits for the proxy service to be online before allowing subsequent nodes to start updates.
* **Tag-Based Discovery:** Only processes LXCs tagged `community-script` or `proxmox-helper-scripts` in PVE — untagged containers are never touched.
* **Multi-Host Support:** Handles LXC containers, QEMU VMs, and non-Proxmox remote hosts in a single run.
* **Flexible Backup Strategy:** Choose per-run between lightweight snapshots, full `vzdump` backups (including PBS), both, or none.
* **Resource Scaling:** Automatically scales container CPU/RAM up during build-heavy app updates and back down afterward, matching the behaviour of the community-scripts bash installer.
* **Dry-Run Mode:** Compare installed vs. latest GitHub release versions without applying any changes.
* **Consolidated Reporting:** Aggregates results from every node and container into exactly one Discord notification, with a structured error log showing which host, which task, and what the error was.

## 🛠 Prerequisites
* **Ansible Manager:** A dedicated LXC (e.g., Debian 12+, VMID 121) with a static IP.
* **SSH Trust:** Passwordless SSH keys distributed from the Manager to all Proxmox Nodes (`ssh-copy-id`).
* **API Token:** A Proxmox API Token for `root@pam` with "Privilege Separation" unchecked.
* **Uptime Kuma:** A Public Status Page (e.g., slug: `proxmox-sg1`) containing the monitors to be validated.

## 📂 Project Structure
```text
~/proxmox-management/
├── ansible.cfg                      # Performance & connection settings
├── hosts.ini                        # List of nodes (gitignored — copy from .example)
├── vars.yml                         # Credentials and cluster config (gitignored — copy from .example)
├── fleet-update.yml                 # Main orchestrator playbook (7 phases)
├── tasks/
│   └── fleet-state-append.yml       # Shared state accumulator (used by all roles)
├── templates/
│   └── discord_briefing.j2          # Discord embed body template
└── roles/
    ├── lxc_update/                  # LXC container update logic
    ├── vm_update/                   # QEMU VM update logic
    └── remote_host_update/          # Non-Proxmox host update logic
```

## ⚙️ Configuration (vars.yml)
The `vars.yml` file is the central intelligence of the orchestrator.

### 🔑 Authentication & Notifications
* `pve_api_...`: Credentials for the Proxmox API. Required for snapshots and rollbacks.
* `discord_webhook`: Your unique Discord Webhook URL for the morning briefing.

### 🌐 Networking (Apt-Cacher NG)
* `apt_proxy_ip` / `apt_proxy_port`: If the node hosting your proxy reboots, Ansible will pause and wait for this IP/Port to respond.

### 🚥 Uptime Kuma Integration
* `kuma_url` / `kuma_slug`: Points to your Kuma instance and the specific Status Page slug.
* `lxc_kuma_map` / `vm_kuma_map` / `remote_kuma_map`: Map an inventory hostname or LXC ID to an Uptime Kuma Monitor ID. Ansible will wait up to 5×30 seconds for Kuma to report `status: 1`. The monitor ID is the integer visible in the Kuma URL when editing a monitor; `kuma_slug` is the status page slug, not a monitor slug.

### 🔄 Backup Strategy
* `lxc_backup_strategy`: `snapshot` (default) | `vzdump` | `both` | `none`
* `lxc_backup_storage`: PVE storage name for `vzdump`. Set to your PBS storage name (as shown in Datacenter → Storage) to route backups to PBS — no other change needed.

### 🏷️ LXC Tag Discovery
* `lxc_tags`: List of PVE tags that mark community-scripts containers (default: `community-script`, `proxmox-helper-scripts`). Set tags in PVE UI → Container → Options → Tags.
* `lxc_dry_run`: Set to `true` to compare installed vs. latest GitHub release versions without making any changes.
* `lxc_unattended`: Sets `PHS_SILENT=1` inside containers to suppress interactive prompts.
* `lxc_backup_strategy` / `lxc_auto_reboot` / `lxc_continue_on_error`: See `vars.yml.example` for defaults.

### 🛡️ Management & Exclusions
* `manager_lxc_id`: The VMID of the Ansible Manager itself. The node hosting this container is never rebooted automatically.
* `exclude_list`: LXC IDs completely skipped (no updates, no snapshots).
* `os_update_exclude_list`: Skip `apt dist-upgrade` / `apk upgrade` for these IDs (app update still runs).
* `snapshot_exclude_list`: Updates run but no snapshot is taken (use for LXCs with bind mounts).
* **Note:** Phase 2 (node OS updates) uses `any_errors_fatal: true` and `serial: 1` to protect cluster quorum.

## 🚀 Setup Instructions
To set up this project from scratch on a brand-new Ansible Manager LXC, follow these steps in order. This ensures all dependencies are met and the "trust" between your manager and your Proxmox nodes is established correctly.

### 1. Create the Manager LXC
* **OS:** Debian 13 (highly recommended) or Ubuntu 22.04/24.04.
* **Resources:** 1 CPU, 1GB RAM, 8GB Disk.
* **Network:** Assign a Static IP. (If the IP changes, your SSH keys might be flagged).

### 2. Install Core Software
Log into your new Manager LXC and run these commands to install Ansible and the libraries required to talk to the Proxmox API:
```bash
# Update the OS
apt update && apt upgrade -y

# Install Ansible, Git, and the Proxmox Python library
# We use the 'python3-proxmoxer' apt package to avoid pip library conflicts
apt install -y ansible python3-pip python3-proxmoxer python3-jmespath git

# Install required Ansible Collections
ansible-galaxy collection install community.proxmox community.general
```

### 3. Establish SSH Trust (Passwordless Login)
Ansible needs to log into your Proxmox nodes without a password.
1. **Generate your SSH Key:**
   ```bash
   ssh-keygen -t ed25519 -C "ansible-manager"
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
   # It should let you in without a password. Type 'exit' to return.
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
Populate these new files (`hosts.ini`, `vars.yml`) with your cluster-specific configuration.

### 5. Final Initial Configuration
Once the files are created, tell Ansible to silence the purple "Python interpreter" warnings by adding this to your `hosts.ini` (under `[proxmox_nodes:vars]`):
```ini
[proxmox_nodes:vars]
ansible_user=root
ansible_python_interpreter=/usr/bin/python3
```

### 6. Verify the Whole Setup
Run this "Ping" test to make sure Ansible can talk to all your nodes at once:
```bash
ansible all -i hosts.ini -m ping
```
If everything comes back GREEN:
You are ready to run your first real update!
```bash
ansible-playbook -i hosts.ini fleet-update.yml -e "force_notify=true"
```

**Summary of what you just did:**
* Installed Ansible as the engine.
* Installed Proxmoxer as the bridge to the Proxmox API.
* Set up SSH Keys as the secure doorway to your nodes.
* Built the Folder Structure to hold your SG-1 Fleet intelligence.

## 🏃 Usage
### Manual Fleet Run (With Notification)
```bash
ansible-playbook fleet-update.yml -e "force_notify=true"
```
### Check Mode (No Changes, Forces Notification)
```bash
ansible-playbook fleet-update.yml --check -e "force_notify=true"
```
### Version Comparison Dry Run (No Updates Applied)
```bash
ansible-playbook fleet-update.yml -e "lxc_dry_run=true force_notify=true"
```
### Single Node
```bash
ansible-playbook fleet-update.yml --limit pve-01
```
### Automated Schedule (Cron)
Add this to the Manager LXC's `crontab -e` to run at 4:00 AM daily:
```cron
0 4 * * * cd /root/proxmox-management && /usr/bin/ansible-playbook fleet-update.yml >> /var/log/ansible-fleet-update.log 2>&1
```

## 🗒️ TODO

- **Tier 4 — Automatic Snapshot Rollback**: `health_check.yml` polls Uptime Kuma after updates but takes no action on failure (`ignore_errors: yes`). Implement: if Kuma doesn't return `status: 1` within the retry window, roll back to the `BEFORE_UPDATE_AUTO` snapshot via `community.proxmox.proxmox_snap` and record the rollback in the fleet state / Discord embed.

## 📡 Discord Briefing Format
The orchestrator sends one consolidated embed per run:
* **Per-Node sections:** Node status (OK / UPDATED & REBOOTED / UPDATED (MANUAL REBOOT REQ) / FAILED), followed by each changed LXC and VM.
* **Remote Hosts section:** Listed separately (not tied to a PVE node).
* **Error Log:** Structured entries showing which host failed, which task failed, and the first 300 characters of stderr.
* Only containers where something actually happened (UPDATED, FAILED, or dry-run results) appear in the embed — already-up-to-date containers are silent.
