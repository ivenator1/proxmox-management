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
```

`hosts.ini` and `vars.yml` are gitignored (contain secrets/IPs). Copy from `.example` files to run locally.

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
- Always block: delete snapshot (only if `snap_res` is defined and succeeded), stop container if `lxc_was_stopped`

`vm_update` and `remote_host_update` follow the same block/rescue/always pattern. `remote_host_update` has no always block (no snapshots to clean up).

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

### Jinja2 / Ansible patterns

- **`regex_search` with capture groups**: always use `(value | regex_search('pattern', '\\1') or [''])[0]`. Never use `| first` — `regex_search` returns Python `None` (not Jinja2 `Undefined`) on no match, so `| default([])` does not help and `| first` on `None` crashes.
- **Empty `>-` block → Python `None`**: a `set_fact` using a `>-` YAML block scalar whose Jinja2 evaluates to an empty string stores `None`, not `""`. Guard downstream with `{{ '' if var is none else var | trim }}`. The `discord_briefing.j2` OS field and `report.yml` `os:` payload both use this pattern.
- **Explicit `{{ '\n' }}` for newlines in templates**: do not rely on template-source newlines when `{%- -%}` tags are present — they strip adjacent whitespace including newlines. Use `{{ '\n' }}` (or `{{ '\n\n' }}` for blank lines) as explicit output that cannot be stripped by control-tag whitespace rules.
- **Discord embed markdown**: embed descriptions support `**bold**`, `*italic*`, `` `code` ``, `- ` bullet lists, and `\n` newlines. They do **not** support `>` blockquotes or `#` headers (those work only in regular messages, not webhook embeds).
