#!/usr/bin/env bash
# install.sh — one-shot installer for the proxmox-management fleet manager.
#
# Usage (as root, from the cloned repo):
#   ./install.sh              install: venv + deps, systemd scan timer, dashboard service
#   ./install.sh --update     git pull, reinstall deps, rewrite units, restart services
#   ./install.sh --uninstall  remove units + venv (prompts before deleting run history)
#   ./install.sh --help
#
# Everything is idempotent — re-running install is safe and acts as a repair.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_DIR/.venv"
PIP="$VENV/bin/pip"
UNIT_DIR="/etc/systemd/system"
HISTORY_DIR="/var/log/fleet-update"
SCAN_SERVICE="fleet-scan.service"
SCAN_TIMER="fleet-scan.timer"
DASH_SERVICE="fleet-dashboard.service"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"   # overridable for testing without systemd

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "this script must be run as root (try: sudo ./install.sh $*)"
}

# --- install steps -----------------------------------------------------------

ensure_system_packages() {
    if ! python3 -m venv --help >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 \
            || die "python3-venv and/or git are missing and apt-get is unavailable — install them manually"
        info "Installing system packages (python3-venv, git)"
        apt-get update -qq
        apt-get install -y -qq python3-venv git
    fi
}

install_python_deps() {
    if [ ! -x "$VENV/bin/python" ]; then
        info "Creating virtualenv at $VENV"
        python3 -m venv "$VENV"
    fi
    info "Installing proxmox-fleet (with web dashboard extras)"
    "$PIP" install --quiet --upgrade pip
    "$PIP" install --quiet -e "${REPO_DIR}[web]"
    # ansible-runner needs the ansible-playbook binary but does not depend on it.
    "$PIP" install --quiet ansible-core
    info "Installing Ansible collections (community.proxmox, community.general)"
    "$VENV/bin/ansible-galaxy" collection install community.proxmox community.general >/dev/null
}

seed_config() {
    local seeded=0
    if [ ! -f "$REPO_DIR/vars.yml" ]; then
        cp "$REPO_DIR/vars.yml.example" "$REPO_DIR/vars.yml"
        seeded=1
    fi
    if [ ! -f "$REPO_DIR/hosts.ini" ]; then
        cp "$REPO_DIR/hosts.ini.example" "$REPO_DIR/hosts.ini"
        seeded=1
    fi
    if [ "$seeded" -eq 1 ]; then
        warn "vars.yml / hosts.ini were seeded from the .example templates —"
        warn "edit them with your real inventory and settings before relying on scans or runs."
    fi
}

write_units() {
    info "Writing systemd units to $UNIT_DIR"

    cat > "$UNIT_DIR/$SCAN_SERVICE" <<EOF
[Unit]
Description=Fleet pending-updates scan (fleet-update --scan, read-only)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$VENV/bin/fleet-update --scan
Environment=PYTHONUNBUFFERED=1
EOF

    cat > "$UNIT_DIR/$SCAN_TIMER" <<EOF
[Unit]
Description=Run the fleet pending-updates scan every 6 hours

[Timer]
OnCalendar=00/6:00:00
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
EOF

    cat > "$UNIT_DIR/$DASH_SERVICE" <<EOF
[Unit]
Description=Fleet web dashboard (fleet-dashboard)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$REPO_DIR
ExecStart=$VENV/bin/fleet-dashboard
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
}

enable_units() {
    "$SYSTEMCTL" daemon-reload
    info "Enabling and starting $SCAN_TIMER and $DASH_SERVICE"
    "$SYSTEMCTL" enable --now "$SCAN_TIMER" "$DASH_SERVICE"
}

print_summary() {
    local port host_ip
    port=$(sed -n 's/^dashboard_port:[[:space:]]*\([0-9]*\).*/\1/p' "$REPO_DIR/vars.yml" | head -1)
    host_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    cat <<EOF

Install complete.

  Dashboard:   http://${host_ip:-<this-host>}:${port:-8421}  ($DASH_SERVICE)
  Scan timer:  every 6 hours ($SCAN_TIMER -> fleet-update --scan)
  History:     $HISTORY_DIR
  Both units are enabled and persist across reboots.

Next steps:
  1. Edit $REPO_DIR/hosts.ini with your real inventory.
  2. Edit $REPO_DIR/vars.yml (set dashboard_token — the run trigger is unauthenticated without it).
  3. Set up SSH trust to your nodes (see README "Establish SSH Trust").
  4. systemctl restart $DASH_SERVICE after changing vars.yml.

Until hosts.ini/vars.yml point at real hosts, scans will report errors — that's expected.
Maintenance: ./install.sh --update | ./install.sh --uninstall
EOF
}

do_install() {
    ensure_system_packages
    install_python_deps
    seed_config
    mkdir -p "$HISTORY_DIR"
    write_units
    enable_units
    print_summary
}

# --- update ------------------------------------------------------------------

do_update() {
    info "Pulling latest changes ($(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD))"
    git -C "$REPO_DIR" pull --ff-only
    install_python_deps
    write_units
    "$SYSTEMCTL" daemon-reload
    info "Restarting services"
    "$SYSTEMCTL" enable --now "$SCAN_TIMER"
    "$SYSTEMCTL" restart "$DASH_SERVICE"
    info "Update complete."
}

# --- uninstall ---------------------------------------------------------------

do_uninstall() {
    info "Stopping and disabling units"
    "$SYSTEMCTL" disable --now "$SCAN_TIMER" "$DASH_SERVICE" "$SCAN_SERVICE" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SCAN_SERVICE" "$UNIT_DIR/$SCAN_TIMER" "$UNIT_DIR/$DASH_SERVICE"
    "$SYSTEMCTL" daemon-reload
    "$SYSTEMCTL" reset-failed 2>/dev/null || true

    info "Removing virtualenv $VENV"
    rm -rf "$VENV"

    if [ -d "$HISTORY_DIR" ]; then
        read -r -p "Delete run/scan history in $HISTORY_DIR? [y/N] " answer || answer=n
        case "$answer" in
            [yY]*) rm -rf "$HISTORY_DIR"; info "Removed $HISTORY_DIR" ;;
            *)     info "Kept $HISTORY_DIR" ;;
        esac
    fi

    cat <<EOF

Uninstall complete. Kept (delete manually if desired):
  $REPO_DIR/vars.yml and $REPO_DIR/hosts.ini  (your settings/inventory)
  $REPO_DIR                                    (rm -rf it to remove the clone)
EOF
}

# --- main --------------------------------------------------------------------

case "${1:-}" in
    "")           require_root;             do_install ;;
    --update)     require_root --update;    do_update ;;
    --uninstall)  require_root --uninstall; do_uninstall ;;
    --help|-h)    usage ;;
    *)            usage; die "unknown flag: $1" ;;
esac
