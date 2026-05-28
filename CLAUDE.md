# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Syntax check (no PVE infrastructure needed)
ansible-playbook fleet-update.yml --syntax-check

# Dry run — no changes, forces Discord notification
ansible-playbook fleet-update.yml --check -e "force_notify=true"

# Version comparison only (no updates applied)
ansible-playbook fleet-update.yml -e "lxc_dry_run=true force_notify=true"

# Full run against one node only
ansible-playbook fleet-update.yml --limit pve-01

# Full run with forced notification
ansible-playbook fleet-update.yml -e "force_notify=true"

# Install required collections
ansible-galaxy collection install community.proxmox community.general

# Jinja2 unit tests (no Ansible or PVE needed)
pip install -r tests/requirements.txt
pytest tests/unit/ -v
pytest tests/unit/test_report_tmp_app.py -v    # single file
pytest tests/unit/ -k "test_version_updated"   # single test

# Static analysis
yamllint .
ansible-lint fleet-update.yml

# Molecule scenario (runs against localhost via stub pct/vzdump scripts)
cd roles/lxc_update && molecule test -s lxc_update_normal
cd roles/lxc_update && molecule converge -s lxc_update_normal  # converge only, no verify/destroy
```

`hosts.ini` and `vars.yml` are gitignored (contain secrets/IPs). Copy from `.example` files to run locally.

## File Map

```
fleet-update.yml                        # Main playbook — 7 phases, entry point for everything
ansible.cfg                             # forks=20, pipelining=true, inventory=./hosts.ini
vars.yml / vars.yml.example             # Secrets + behaviour flags (gitignored; copy from .example)
hosts.ini / hosts.ini.example           # Inventory (gitignored; copy from .example)
.ansible-lint                           # profile: moderate; excludes role task files that use {{ role_path }} includes
.yamllint.yml                           # extends: default; line-length warning at 160
.github/workflows/ci.yml                # Five parallel jobs: yamllint, ansible-lint, syntax-check, unit-tests, molecule
tasks/
  fleet-state-append.yml                # Shared state accumulator — always use this, never inline set_fact+delegate
templates/
  discord_briefing.j2                   # Discord embed body — renders fleet_*_data lists into markdown
tests/
  requirements.txt                      # pytest, jinja2, pyyaml — all that's needed for unit tests
  conftest.py                           # Ansible-compatible Jinja2 env: regex_search (list return), bool, failed/search tests
  unit/                                 # 104 pytest tests; no Ansible or PVE required
  integration/
    test_fleet_state_append.yml         # Standalone ansible-playbook test for delegate_to+delegate_facts accumulation
roles/
  lxc_update/
    defaults/main.yml                   # Default values for all lxc_* vars
    tasks/
      main.yml                          # Orchestrator: introspect → block(detect→backup→dry_check|update→health_check→report) → rescue → always
      introspect.yml                    # pct config + pct status; starts stopped containers; sets lxc_name, lxc_os, lxc_is_running, lxc_was_stopped
      detect.yml                        # Pulls /usr/bin/update, extracts ct script name, fetches .sh from GitHub, parses resource requirements
      backup.yml                        # vzdump and/or snapshot (BEFORE_UPDATE_AUTO) based on lxc_backup_strategy
      dry_check.yml                     # Reads installed version + fetches latest GitHub release; sets dry_run_status
      update.yml                        # OS update first, dpkg hash before/after community script, ver before/after, reboot if needed
      health_check.yml                  # Polls Uptime Kuma for containers in lxc_kuma_map; only fires when something changed
      report.yml                        # Builds tmp_app/tmp_os strings; appends LXC record (skips idle containers)
    molecule/
      lxc_update_normal/                # Running container, vzdump backup, app+OS update
      lxc_update_template/              # pct config returns template:1 → all tasks skipped
      lxc_update_stopped/               # pct status:stopped → start → update → stop in always block
      lxc_update_dry_run/               # lxc_dry_run=true → only dry_check runs, no backup/update
      lxc_update_rescue/                # vzdump stub exits 1 → rescue block fires, fleet_failed=True
  vm_update/
    defaults/main.yml                   # Default values for vm_* vars
    tasks/
      main.yml                          # Orchestrator: block(snapshot→update→health_check→report) → rescue → always(delete snapshot)
      snapshot.yml                      # Creates BEFORE_UPDATE_AUTO snapshot via PVE API
      update.yml                        # apt/dnf/apk upgrade + reboot check
      health_check.yml                  # Polls Uptime Kuma (vm_kuma_map)
      report.yml                        # Appends VM record to fleet_vm_data
    molecule/default/                   # Runs role in check_mode against localhost; no PVE needed
  remote_host_update/
    defaults/main.yml                   # Default values for remote_* vars
    tasks/
      main.yml                          # Orchestrator: block(update→health_check→report) → rescue (no always block — no snapshots)
      update.yml                        # apt/dnf/apk upgrade + reboot check
      health_check.yml                  # Polls Uptime Kuma (remote_kuma_map)
      report.yml                        # Appends remote host record to fleet_remote_data
    molecule/default/                   # Runs role in check_mode against localhost; no PVE needed
```

## Architecture

### Play order in `fleet-update.yml`

| Phase | Hosts | Purpose |
|---|---|---|
| Pre-Flight | localhost | Verify apt-cacher-ng proxy is reachable |
| Phase 0 | `remote_hosts` | Non-Proxmox hosts via `remote_host_update` role |
| Phase 1 | `proxmox_nodes` | Tag-filtered LXC discovery + `lxc_update` role per container |
| Phase 1b | `proxmox_vms` | QEMU VMs via `vm_update` role |
| Phase 2 | `proxmox_nodes` | PVE node OS update + sequential reboot (`serial: 1`, `any_errors_fatal: true`) |
| Phase 3 | localhost | Manager container self-update |
| Phase 4 | localhost | Send Discord embed via `templates/discord_briefing.j2` |

### State accumulation pattern

All fleet state lives as facts on `localhost` across plays. Every role and play appends to it via `tasks/fleet-state-append.yml` using `delegate_to: localhost` + `delegate_facts: true` + `check_mode: no`. The four state lists are `fleet_lxc_data`, `fleet_vm_data`, `fleet_remote_data`, and `fleet_node_data`. `fleet_changed`, `fleet_failed`, and `fleet_error_log` (a `list[{host, task, error}]`) are also maintained here.

Do not write `set_fact` + `delegate_to: localhost` blocks directly — always call `tasks/fleet-state-append.yml` instead.

### Role structure (`roles/lxc_update/`)

`tasks/main.yml` is the orchestrator:
- `introspect.yml` runs **outside** the block (fail loud if `pct config` fails)
- Inside the block: `detect.yml` → `backup.yml` → `dry_check.yml` or `update.yml` → `health_check.yml` → `report.yml`
- Rescue block captures `ansible_failed_task.name` and `ansible_failed_result.stderr` as the **first** `set_fact` before anything else (subsequent tasks reset these vars), then calls `fleet-state-append.yml`
- Always block: delete snapshot (only if `snap_res.changed` — skipped tasks still register with `changed=false`, so `is succeeded` is not sufficient), stop container if `lxc_was_stopped`

`vm_update` and `remote_host_update` follow the same block/rescue/always pattern. `remote_host_update` has no always block (no snapshots to clean up).

### `update.yml` task order and change detection

The task order matters for correct attribution:
1. Read `lxc_ver_before`
2. OS update (`apt dist-upgrade` / `apk upgrade`) — runs **first** so OS packages get credited to the OS line, not the app line
3. Read `dpkg_hash_before` — md5sum of `dpkg-query -W` (package→version pairs) after OS update, before community script
4. Scale up resources (if `needs_resource_scale`)
5. Community script (`app_update_res`)
6. Read `dpkg_hash_after` — same query; if hash matches `dpkg_hash_before`, nothing was installed
7. Read `lxc_ver_after`
8. Scale down → reboot check

**`tmp_app` decision tree in `report.yml`** (in priority order):
- Version files differ → `Updated: X → Y`
- Version files both non-empty and equal → `OK` (confirmed no app change)
- dpkg hash differs → `UPDATED` (packages changed, no version file)
- dpkg hash matches → `OK` (nothing installed, no version file)
- No hash data (non-apt OS) → `UPDATED` (fallback)
- `app_update_res.changed` is false → `OK`

**Why dpkg hash instead of stdout parsing:** `PHS_SILENT=1` (set by `lxc_unattended: true`) routes apt's stdout to `/dev/null` inside community scripts, so keywords like `0 upgraded, 0 newly installed` never appear in `app_update_res.stdout`. The dpkg hash is a direct query, immune to output suppression.

### `detect.yml` flow and version file convention

`detect.yml` does three things in sequence:
1. `pct pull {lxc_id} /usr/bin/update /tmp/ansible_update_{lxc_id}` — extracts the community-scripts update script from the container
2. Greps the script for `ct/NAME.sh` to get the ct script name (e.g. `sonarr`); sets `lxc_no_update_script: true` if not found
3. Fetches `https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/{name}.sh` (delegated to localhost) — parses `var_cpu`/`var_ram` for build resources, and `pct set $CTID -cores/-memory` for run resources

**Version files**: community-scripts store the installed app version at `~/.{scriptname}` inside the container (e.g. `~/.sonarr` contains `4.0.17.2952`). `update.yml` reads this before and after the update as `lxc_ver_before`/`lxc_ver_after`. `dry_check.yml` reads it as `installed_ver` and compares against the latest GitHub release.

**Resource scaling**: when `lxc_build_cpu > lxc_run_cpu`, `needs_resource_scale` is true — `update.yml` scales the container up before the update script runs and back down after.

### Uptime Kuma integration

`health_check.yml` (all three roles) polls `{kuma_url}/api/status-page/heartbeat/{kuma_slug}` and waits for the mapped monitor to show status `1`. It only fires when `lxc_id` (or equivalent) is in `lxc_kuma_map` (or `vm_kuma_map`/`remote_kuma_map`) **and** something actually changed. Kuma credentials and maps are in `vars.yml`.

### Key non-obvious details

- **Tag-based LXC discovery** (Phase 1): only LXCs tagged `community-script` or `proxmox-helper-scripts` in PVE are processed. Untagged containers are never touched. Tags must be set in PVE UI → Container → Options → Tags.
- **`include_role` not `import_role`**: roles are called in a loop; `import_role` is static (parse-time) and cannot be used inside loops.
- **URI calls in `detect.yml` are delegated to localhost**: PVE nodes may not have outbound HTTPS to GitHub. The Ansible manager always does.
- **`pve_node` inventory var** (VM inventory): must match the PVE node's **inventory hostname** (not its IP) — it is used as a key into `hostvars` for the snapshot API call: `hostvars[pve_node]['ansible_host']`.
- **PBS is transparent**: setting `lxc_backup_storage` to a PBS storage name routes `vzdump` to PBS automatically — no special code path.
- **`[proxmox_vms]` and `[remote_hosts]` must exist** in `hosts.ini` even if empty (just the group header). Ansible raises "no hosts matched" otherwise.
- **Node reboot is skipped** when `manager_lxc_id` runs on that node — rebooting the node would kill the manager mid-run.
- **Discord `check_mode: no`**: the URI task has this so `--check` runs still produce a notification when `force_notify=true`.
- **`lxc_backup_strategy`** is a four-value enum: `snapshot | vzdump | both | none` — not boolean flags.
- **`/tmp/.nc/clear` trick** in `update.yml`: overrides the `clear` shell command with a no-op so community-scripts update output isn't wiped from Ansible's stdout capture.
- **Snapshot name is fixed**: always `BEFORE_UPDATE_AUTO`. The `always:` cleanup hardcodes this name — changing it in `backup.yml` without also changing `main.yml` would leave orphaned snapshots.
- **`report.yml` skips idle containers**: the `when:` condition only appends a record when something changed or failed. Fully up-to-date containers with nothing to do produce no Discord entry.
- **`lxc_continue_on_error`**: when `true`, Phase 1 uses `ignore_errors: yes` on the LXC loop, so a single failing container doesn't abort the rest of the node's containers.

### Testing infrastructure

**Jinja2 unit tests** (`tests/unit/`) exercise the conditional logic inside role task files without Ansible or PVE. Each test file targets a specific expression:

| File | What it covers |
|---|---|
| `test_report_tmp_app.py` | 11-branch `tmp_app` decision tree in `report.yml` |
| `test_report_tmp_os.py` | `tmp_os` expression + the `None`-guard in the payload |
| `test_report_when_condition.py` | The `when:` gate that suppresses idle `OK`/`OK` containers |
| `test_dry_check_status.py` | `dry_run_status` branches in `dry_check.yml` |
| `test_detect_regex.py` | `regex_search` patterns in `detect.yml` and `needs_resource_scale` |
| `test_discord_briefing.py` | Full `discord_briefing.j2` template rendering |
| `test_fleet_state_append_logic.py` | `set_fact` expressions in `tasks/fleet-state-append.yml` |
| `test_introspect_regex.py` | `pct config` output parsing in `introspect.yml` |

`tests/conftest.py` implements Ansible-specific Jinja2 filters (`regex_search` with list-return for capture groups, `regex_replace`, `selectattr`) and tests (`failed`, `search`, `equalto`) so templates render identically to Ansible. When writing new tests, use `render()` for string output and `render_native()` (via `NativeEnvironment`) for Python objects.

**Molecule scenarios** (`roles/lxc_update/molecule/`) test role orchestration against localhost using stub `pct`/`vzdump` shell scripts placed in `/tmp/mol_stubs/` by `prepare.yml`. Each scenario's `molecule.yml` sets `PATH: "/tmp/mol_stubs:${PATH}"` at the provisioner level so stubs are found by all shell tasks inside included roles. Converge serialises `fleet_*` facts to `/tmp/mol_fleet_state.json`; verify loads it with `include_vars` (necessary because converge and verify run as separate `ansible-playbook` processes). Idempotency checking is disabled for all scenarios — backup and update operations are intentionally non-idempotent.

**CI** (`.github/workflows/ci.yml`) runs five parallel jobs: `yamllint`, `ansible-lint`, `syntax-check`, `unit-tests` (pytest), and `molecule-lxc-update`. The `ansible-lint` job excludes the six role task files that use `include_tasks: "{{ role_path }}/..."` via `exclude_paths` in `.ansible-lint` (the `load-failure[not-found]` rule is unskippable).

### Jinja2 / Ansible patterns

- **`regex_search` with capture groups**: always use `(value | regex_search('pattern', '\\1') or [''])[0]`. Never use `| first` — `regex_search` returns Python `None` (not Jinja2 `Undefined`) on no match, so `| default([])` does not help and `| first` on `None` crashes.
- **Empty `>-` block → Python `None`**: a `set_fact` using a `>-` YAML block scalar whose Jinja2 evaluates to an empty string stores `None`, not `""`. Guard downstream with `{{ '' if var is none else var | trim }}`. The `discord_briefing.j2` OS field and `report.yml` `os:` payload both use this pattern.
- **Explicit `{{ '\n' }}` for newlines in templates**: do not rely on template-source newlines when `{%- -%}` tags are present — they strip adjacent whitespace including newlines. Use `{{ '\n' }}` (or `{{ '\n\n' }}` for blank lines) as explicit output that cannot be stripped by control-tag whitespace rules.
- **Discord embed markdown**: embed descriptions support `**bold**`, `*italic*`, `` `code` ``, `- ` bullet lists, and `\n` newlines. They do **not** support `>` blockquotes or `#` headers (those work only in regular messages, not webhook embeds).
