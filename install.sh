#!/usr/bin/env bash
# install.sh — one-shot installer for the proxmox-management fleet manager.
#
# Usage (as root, from the cloned repo):
#   ./install.sh              install: venv + deps, systemd scan timer, dashboard service
#                             (plus the hourly housekeeping timer when
#                              housekeeping_timer_enabled / housekeeping_enabled is true)
#   ./install.sh --update     git pull, reinstall deps, rewrite units, restart services
#   ./install.sh --uninstall  remove units + venv (prompts before deleting run history)
#   ./install.sh --help
#
# Everything is idempotent — re-running install is safe and acts as a repair.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_DIR/.venv"
PIP="$VENV/bin/pip"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"   # overridable for testing without touching the real systemd dir
# Fallback when the venv/vars.yml don't exist yet (e.g. --uninstall after a
# broken install); everywhere else the value comes from the package via
# resolve_setting fleet_history_dir, so a custom dir in vars.yml is honored.
HISTORY_DIR_DEFAULT="/var/log/fleet-update"
SCAN_SERVICE="fleet-scan.service"
SCAN_TIMER="fleet-scan.timer"
HOUSEKEEPING_SERVICE="fleet-housekeeping.service"
HOUSEKEEPING_TIMER="fleet-housekeeping.timer"
DASH_SERVICE="fleet-dashboard.service"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"   # overridable for testing without systemd
# The original invocation (flags/options), so --update can re-exec the freshly
# pulled installer with exactly the same arguments after install.sh itself lands.
SCRIPT_ARGS=("$@")

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Ask the installed package for a GlobalSettings value (single source of
# truth — vars.yml + model defaults). Omitting the fallback makes loading strict.
resolve_setting() {
    local field="$1" fallback="${2-}" value
    value=$(cd "$REPO_DIR" && "$VENV/bin/python" - "$field" <<'PYEOF' 2>/dev/null
import sys
from proxmox_fleet.models.settings import GlobalSettings
print(getattr(GlobalSettings.load(), sys.argv[1]))
PYEOF
    ) || {
        [ "$#" -ge 2 ] || die "Cannot load validated settings for $field"
        value=""
    }
    printf '%s' "${value:-$fallback}"
}

# True when an installed-settings string is a truthy boolean (true/1/yes).
# resolve_setting prints Python bools as "True"/"False".
setting_is_true() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        true|1|yes) return 0 ;;
        *)          return 1 ;;
    esac
}

usage() {
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
    # community.proxmox 2.x hard-fails without proxmoxer >= 2.3 in ansible's
    # interpreter; proxmoxer's HTTPS backend needs requests but doesn't declare it.
    "$PIP" install --quiet 'proxmoxer>=2.3' requests
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

init_admin_user() {
    # init_db resolves the DB location itself (fleet_history_dir from
    # vars.yml, same as the running dashboard) — only probe it here to skip
    # the password prompt on re-runs.
    local db_path="$1/.fleet-users.db"

    # Check if database already exists
    if [ -f "$db_path" ]; then
        info "Dashboard database already exists — skipping password setup"
        return 0
    fi

    # Prompt for admin password
    printf '%s' "Dashboard admin password: "
    read -rs password1
    printf '\n'

    printf '%s' "Confirm password: "
    read -rs password2
    printf '\n'

    if [ "$password1" != "$password2" ]; then
        die "Passwords do not match"
    fi

    if [ -z "$password1" ]; then
        die "Password cannot be empty"
    fi

    # Initialize the database and create admin user. The password travels in
    # the environment, not on argv (argv is world-readable in `ps`); init_db
    # resolves the DB path from vars.yml itself.
    info "Initializing dashboard database"
    (cd "$REPO_DIR" && FLEET_ADMIN_PASSWORD="$password1" \
        "$VENV/bin/python" -m proxmox_fleet.web.init_db) \
        || die "Failed to initialize database"
}

write_units() {
    info "Writing systemd units to $UNIT_DIR"

    # Scheduled maintenance targets (empty = legacy whole-fleet schedule). The
    # value is a validated bare/cluster-qualified token list, so it needs no
    # quoting and carries no `%`/`$` for systemd to expand as a specifier or
    # environment variable.
    local hk_limit hk_exec
    hk_limit=$(resolve_setting housekeeping_timer_limit)
    hk_exec="$VENV/bin/fleet-update --housekeeping-only"
    if [ -n "$hk_limit" ]; then
        hk_exec="$hk_exec --limit $hk_limit"
    fi

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

    cat > "$UNIT_DIR/$HOUSEKEEPING_SERVICE" <<EOF
[Unit]
Description=Fleet log housekeeping (fleet-update --housekeeping-only)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$hk_exec
Environment=PYTHONUNBUFFERED=1
TimeoutStartSec=3600
CPUWeight=10
IOWeight=10
EOF

    cat > "$UNIT_DIR/$HOUSEKEEPING_TIMER" <<EOF
[Unit]
Description=Run fleet log housekeeping hourly

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
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

# The housekeeping service/timer are written on every install but enabled only
# when the resolved housekeeping timer decision is true. That decision
# (housekeeping_timer_effective_enabled) is explicit housekeeping_timer_enabled,
# falling back to housekeeping_enabled when unset. Install and --update both
# reconcile the enabled state (never an unconditional enable), so flipping the
# setting in vars.yml off and re-running the installer stops the timer again.
reconcile_housekeeping_timer() {
    local enabled
    enabled=$(resolve_setting housekeeping_timer_effective_enabled)
    if setting_is_true "$enabled"; then
        info "Enabling and starting $HOUSEKEEPING_TIMER (housekeeping schedule enabled)"
        "$SYSTEMCTL" enable --now "$HOUSEKEEPING_TIMER"
        if [ "$("$SYSTEMCTL" is-enabled "$HOUSEKEEPING_TIMER")" != "enabled" ] \
            || [ "$("$SYSTEMCTL" is-active "$HOUSEKEEPING_TIMER")" != "active" ]; then
            die "$HOUSEKEEPING_TIMER was not observed enabled and active"
        fi
    else
        info "Housekeeping schedule disabled in settings — leaving $HOUSEKEEPING_TIMER disabled"
        "$SYSTEMCTL" disable --now "$HOUSEKEEPING_TIMER"
        local enable_state active_state
        enable_state=$("$SYSTEMCTL" is-enabled "$HOUSEKEEPING_TIMER") || true
        active_state=$("$SYSTEMCTL" is-active "$HOUSEKEEPING_TIMER") || true
        if [ "$enable_state" != "disabled" ] || [ "$active_state" != "inactive" ]; then
            die "$HOUSEKEEPING_TIMER was not observed disabled and inactive"
        fi
    fi
}

print_summary() {
    local history_dir="$1"
    local port host_ip hk_line hk_targets hk_enabled
    hk_enabled=$(resolve_setting housekeeping_timer_effective_enabled)
    if setting_is_true "$hk_enabled"; then
        hk_targets=$(resolve_setting housekeeping_timer_limit)
        if [ -n "$hk_targets" ]; then
            hk_line="hourly ($HOUSEKEEPING_TIMER -> fleet-update --housekeeping-only --limit $hk_targets)"
        else
            hk_line="hourly ($HOUSEKEEPING_TIMER -> fleet-update --housekeeping-only)"
        fi
    else
        hk_line="disabled (set housekeeping_timer_enabled=true in vars.yml, then re-run install)"
    fi
    port=$(resolve_setting dashboard_port 8421)
    host_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    cat <<EOF

Install complete.

  Dashboard:   http://${host_ip:-<this-host>}:${port:-8421}  ($DASH_SERVICE, login required)
  Scan timer:  every 6 hours ($SCAN_TIMER -> fleet-update --scan)
  Housekeep:   $hk_line
  History:     $history_dir
  Enabled units persist across reboots.

Next steps:
  1. Access the dashboard at http://${host_ip:-<this-host>}:${port:-8421} (admin@fleet.lan, password set during install)
  2. From the "Inventory & enrollment" page in the dashboard, add hosts and set up SSH keys
  3. Or: Edit $REPO_DIR/hosts.ini manually and set up SSH trust per the README

Until hosts.ini/vars.yml point at real hosts, scans will report errors — that's expected.
Maintenance: ./install.sh --update | ./install.sh --uninstall
EOF
}

do_install() {
    ensure_system_packages
    install_python_deps
    seed_config
    local history_dir
    history_dir=$(resolve_setting fleet_history_dir "$HISTORY_DIR_DEFAULT")
    mkdir -p "$history_dir"
    init_admin_user "$history_dir"
    write_units
    enable_units
    reconcile_housekeeping_timer
    print_summary "$history_dir"
}

# --- update ------------------------------------------------------------------

do_update() {
    # The installer is executed as a whole file, so shell functions are parsed
    # before `git pull` runs: an install.sh updated by the pull would otherwise
    # be ignored until the *next* invocation (the observed bug where new unit
    # logic, e.g. the scoped --limit, never reached the installed service).
    # Detect a content change in install.sh across the pull and re-exec the
    # freshly pulled script with the original arguments, so the new logic runs
    # in this very invocation. FLEET_INSTALLER_REEXEC guards against a re-exec
    # loop: the re-executed script sees its own now-unchanged content and
    # proceeds with a normal single-pass update.
    local installer_before installer_after
    installer_before=$(sha256sum "$REPO_DIR/install.sh" 2>/dev/null | awk '{print $1}') || true

    info "Pulling latest changes ($(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD))"
    # A failed pull must not continue into dependency/unit work against a
    # half-updated tree — abort and leave the existing install untouched.
    if ! git -C "$REPO_DIR" pull --ff-only; then
        die "git pull failed — aborting update before installing anything"
    fi

    installer_after=$(sha256sum "$REPO_DIR/install.sh" 2>/dev/null | awk '{print $1}') || true
    if [ "${FLEET_INSTALLER_REEXEC:-0}" != "1" ] \
        && [ -n "$installer_before" ] && [ "$installer_before" != "$installer_after" ]; then
        info "Install script changed during the pull — re-executing the updated installer"
        FLEET_INSTALLER_REEXEC=1 exec "$REPO_DIR/install.sh" "${SCRIPT_ARGS[@]}"
    fi

    install_python_deps
    write_units
    "$SYSTEMCTL" daemon-reload
    info "Restarting services"
    "$SYSTEMCTL" enable --now "$SCAN_TIMER"
    "$SYSTEMCTL" restart "$DASH_SERVICE"
    reconcile_housekeeping_timer
    info "Update complete."
}

# --- uninstall ---------------------------------------------------------------

do_uninstall() {
    # Resolve while the venv still exists; falls back to the default dir.
    local history_dir
    history_dir=$(resolve_setting fleet_history_dir "$HISTORY_DIR_DEFAULT")

    info "Stopping and disabling units"
    "$SYSTEMCTL" disable --now "$SCAN_TIMER" "$DASH_SERVICE" "$SCAN_SERVICE" \
        "$HOUSEKEEPING_TIMER" "$HOUSEKEEPING_SERVICE" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SCAN_SERVICE" "$UNIT_DIR/$SCAN_TIMER" "$UNIT_DIR/$DASH_SERVICE" \
        "$UNIT_DIR/$HOUSEKEEPING_SERVICE" "$UNIT_DIR/$HOUSEKEEPING_TIMER"
    "$SYSTEMCTL" daemon-reload
    "$SYSTEMCTL" reset-failed 2>/dev/null || true

    info "Removing virtualenv $VENV"
    rm -rf "$VENV"

    if [ -d "$history_dir" ]; then
        read -r -p "Delete run/scan history in $history_dir? [y/N] " answer || answer=n
        case "$answer" in
            [yY]*) rm -rf "$history_dir"; info "Removed $history_dir" ;;
            *)     info "Kept $history_dir" ;;
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
