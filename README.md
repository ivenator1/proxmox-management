🚀 Proxmox Cluster Orchestrator

A professional-grade, Ansible-based automation engine for maintaining a Proxmox
VE High-Availability (HA) cluster. This project manages the simultaneous update
of numerous LXC containers across multiple nodes while ensuring cluster quorum,
service availability, and automated self-healing.

📡 Overview

The Proxmox Cluster Orchestrator moves maintenance from a manual process to a
Tiered Recovery Model:

1.  Tier 1 (Disaster): Integration with Proxmox Backup Server (PBS).
2.  Tier 2 (Rapid Recovery): Automatic Pre-update Snapshots (with Bind Mount
    awareness).
3.  Tier 3 (Validation): Real-world health validation via Uptime Kuma Status
    Page API.
4.  Tier 4 (Self-Healing): Automatic Snapshot Rollback if an application fails
    to respond post-update.

✨ Key Features

  - Location-Aware Updates: Dynamically detects which physical node is hosting
    the Ansible Manager LXC and skips that node's reboot to prevent script
    termination mid-run.
  - Controlled Parallelism: Updates LXCs across multiple nodes simultaneously to
    save time, while rebooting physical nodes sequentially to maintain Cluster
    Quorum and HA stability.
  - Apt-Proxy Awareness: Optimized for environments using apt-cacher-ng;
    automatically waits for the proxy service to be online before allowing
    subsequent nodes to start updates.
  - Smart Discovery: Automatically identifies any LXC containing the Proxmox
    Helper Script update command.
  - Consolidated Reporting: Aggregates results from every node and container
    into exactly one Discord notification every morning.

🛠 Prerequisites

  - Ansible Manager: A dedicated LXC (e.g., Debian 13, VMID 121) with a static
    IP.
  - SSH Trust: Passwordless SSH keys distributed from the Manager to all Proxmox
    Nodes (ssh-copy-id).
  - API Token: A Proxmox API Token for root@pam with "Privilege Separation"
    unchecked.
  - Uptime Kuma: A Public Status Page (e.g., slug: proxmox-sg1) containing the
    monitors to be validated.

📂 Project Structure

~/proxmox-management/
├── ansible.cfg              # Performance & connection settings
├── hosts.ini                # List of physical Proxmox nodes
├── vars.yml                 # Cluster-specific IDs, credentials, and mappings
├── fleet-update.yml         # The main 4-phase orchestrator playbook
└── update_individual_lxc.yml # The logic for individual container maintenance

⚙️ Configuration (vars.yml)

The vars.yml file is the central intelligence of the orchestrator. Below is an
explanation of its sections:

🔑 Authentication & Notifications

  - pve_api_...: Credentials for the Proxmox API. Required for snapshots and
    rollbacks.
  - discord_webhook: Your unique Discord Webhook URL for the morning briefing.

🌐 Networking (Apt-Cacher NG)

  - apt_proxy_ip / apt_proxy_port: If the node hosting your proxy reboots,
    Ansible will pause and wait for this IP/Port to respond before allowing
    other nodes to attempt updates.

🚥 Uptime Kuma Integration

  - kuma_url / kuma_slug: Points to your Kuma instance and the specific Status
    Page slug.
  - lxc_kuma_map: Links a Proxmox LXC ID to an Uptime Kuma Monitor ID.
      - Ansible will wait 5 minutes for Kuma to report status: 1 (Up). If it
        fails, an automatic rollback is triggered.

🛡️ Management & Exclusions

  - manager_lxc_id: The VMID of the Ansible Manager itself. Whichever host holds
    this ID will skip its automated reboot to ensure the briefing is sent.
  - exclude_list: IDs in this list are completely ignored (No updates, no
    snapshots).
  - os_update_exclude_list: Only the specialized App update command is run. The
    standard apt dist-upgrade is skipped (Common for PBS).
  - snapshot_exclude_list: Updates are performed, but snapshots are skipped. Use
    this for LXCs with Bind Mounts, as Proxmox cannot snapshot them.

🚀 Setup Instructions

1.  Prepare the Manager LXC (Debian 12+):
    apt update && apt install -y git python3-venv
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install ansible proxmoxer

2.  Install Ansible Collections:
    ansible-galaxy collection install community.proxmox

3.  Configure:
    Edit `hosts.ini` to add your PVE nodes and `vars.yml` to populate credentials.

4.  Run the Fleet Update:
    ansible-playbook -i hosts.ini fleet-update.yml

🏃 Usage

Manual Fleet Run (With Notification)

ansible-playbook -i hosts.ini fleet-update.yml -e "force_notify=true"

Dry Run (Simulation)

ansible-playbook -i hosts.ini fleet-update.yml --check -e "force_notify=true"

Automated Schedule (Cron)

Add this to the Manager LXC's crontab -e to run at 4:00 AM daily:

0 4 * * * cd /root/proxmox-management && /usr/bin/ansible-playbook fleet-update.yml >> /var/log/ansible-fleet-update.log 2>&1

📡 Discord Briefing Format

The orchestrator sends a consolidated message:

  - Node Status: OK, UPDATED & REBOOTED, or MANUAL REBOOT REQ.
  - LXC Breakdown: Shows specific status: APP: UPDATED, LXC: OK.
  - Error Log: Dedicated section at the bottom for any failed tasks.

