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
* **Smart Discovery:** Automatically identifies any LXC containing the Proxmox Helper Script update command.
* **Consolidated Reporting:** Aggregates results from every node and container into exactly one Discord notification every morning.

## 🛠 Prerequisites
* **Ansible Manager:** A dedicated LXC (e.g., Debian 12+, VMID 121) with a static IP.
* **SSH Trust:** Passwordless SSH keys distributed from the Manager to all Proxmox Nodes (`ssh-copy-id`).
* **API Token:** A Proxmox API Token for `root@pam` with "Privilege Separation" unchecked.
* **Uptime Kuma:** A Public Status Page (e.g., slug: `proxmox-sg1`) containing the monitors to be validated.

## 📂 Project Structure
```text
~/proxmox-management/
├── ansible.cfg              # Performance & connection settings
├── hosts.ini                # List of physical Proxmox nodes
├── vars.yml                 # Cluster-specific IDs, credentials, and mappings
├── fleet-update.yml         # The main 4-phase orchestrator playbook
└── update_individual_lxc.yml # The logic for individual container maintenance
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
* `lxc_kuma_map`: Links a Proxmox LXC ID to an Uptime Kuma Monitor ID. Ansible will wait 5 minutes for Kuma to report status: 1 (Up). If it fails, an automatic rollback is triggered.

### 🛡️ Management & Exclusions
* `manager_lxc_id`: The VMID of the Ansible Manager itself.
* `exclude_list`: IDs in this list are completely ignored (No updates, no snapshots).
* `os_update_exclude_list`: Only the specialized App update command is run. The standard `apt dist-upgrade` is skipped (Common for PBS).
* `snapshot_exclude_list`: Updates are performed, but snapshots are skipped. Use this for LXCs with Bind Mounts.
* **Note:** `fleet-update.yml` is configured with `any_errors_fatal: true` for the Node Update phase to ensure cluster integrity if a critical failure occurs.

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

# Install the Proxmox Ansible Collection
ansible-galaxy collection install community.proxmox
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
ansible-playbook -i hosts.ini fleet-update.yml -e "force_notify=true"
```
### Dry Run (Simulation)
```bash
ansible-playbook -i hosts.ini fleet-update.yml --check -e "force_notify=true"
```
### Automated Schedule (Cron)
Add this to the Manager LXC's `crontab -e` to run at 4:00 AM daily:
```cron
0 4 * * * cd /root/proxmox-management && /usr/bin/ansible-playbook fleet-update.yml >> /var/log/ansible-fleet-update.log 2>&1
```

## 📡 Discord Briefing Format
The orchestrator sends a consolidated message:
* **Node Status:** OK, UPDATED & REBOOTED, or MANUAL REBOOT REQ.
* **LXC Breakdown:** Shows specific status: APP: UPDATED, LXC: OK.
* **Error Log:** Dedicated section at the bottom for any failed tasks.
