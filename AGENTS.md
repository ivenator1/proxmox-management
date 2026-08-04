# AGENTS.md

## Project overview

This repository is a typed-Python control plane for Proxmox fleet maintenance. Python owns orchestration, policy, parsing, state, reporting, and local I/O. Ansible is only the remote execution layer, implemented as thin single-purpose playbooks in `ansible/primitives/`.

`proxmox_fleet.driver.run_fleet()` is the sole fleet orchestrator. It is exposed through:

- `fleet-update.py` — human-facing wrapper that bootstraps `.venv` and provides friendly flags.
- `fleet-update` — package console command for programmatic and cron use.

Do not reintroduce a monolithic update playbook or legacy `--use-*-flow` flags. Read `CLAUDE.md` for the full operational model and uncommon edge cases. Treat old roadmap documents as historical; when documentation conflicts, verify behavior against current source and tests.

## Repository map

- `proxmox_fleet/driver.py` — phase orchestration, targeting, canary waves, state merging, and notification phase.
- `proxmox_fleet/flows/` — per-target flows: remote, custom, LXC, VM, node, and manager.
- `proxmox_fleet/models/` — Pydantic settings, custom configuration, and fleet-state schemas.
- `proxmox_fleet/executor.py` / `runner.py` — typed execution boundary and Ansible Runner adapter.
- `proxmox_fleet/status.py`, `changes.py`, `deps.py`, `window.py` — policy and decision trees.
- `proxmox_fleet/lxc_parse.py`, `pkg_detail.py` — manager-side parsing and change-detail extraction.
- `proxmox_fleet/history.py`, `scan.py`, `ledger.py`, `briefing.py`, `notifiers.py` — observability and reporting.
- `proxmox_fleet/inventory.py`, `inventory_edit.py`, `vars_edit.py` — inventory/settings loading and dashboard edits.
- `proxmox_fleet/web/` — optional FastAPI dashboard, authentication, enrollment, and detached run management.
- `ansible/primitives/` — single-purpose execution playbooks; no orchestration or business policy.
- `tests/unit/` — plain pytest tests using scripted executors; no live PVE or Ansible required.
- `roles/*/molecule/` — Molecule harnesses that drive Python flows with stub commands.
- `config_templates/` — committed configuration examples. Real `configs/*.yml` files are ignored.

## Setup

Debian system Python is externally managed, so use a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ansible-galaxy collection install community.proxmox community.general
```

For a manager installation, also ensure the venv contains `ansible-core`, `proxmoxer>=2.3`, and `requests`.

`hosts.ini`, `vars.yml`, and real `configs/*.yml` are gitignored and may contain secrets. Copy from example files for local use; never commit real credentials or environment-specific inventory.

## Verification

Run focused tests first, then checks appropriate to the change:

```bash
pytest tests/unit/test_<area>.py -v
pytest tests/unit/ -v
python -m mypy proxmox_fleet/
ruff check proxmox_fleet/ tests/ fleet-update.py
python -m py_compile fleet-update.py
yamllint .
ansible-lint ansible/primitives/
bandit -r proxmox_fleet/ -ll
```

Syntax-check primitives after YAML or execution-layer changes:

```bash
for f in ansible/primitives/*.yml; do
  ansible-playbook "$f" --syntax-check -i localhost,
done
```

Molecule examples:

```bash
cd roles/lxc_update && molecule test -s lxc_update_normal
cd roles/custom_update && molecule test -s custom_update_normal
```

CI tests Python 3.10–3.12. Package metadata permits Python 3.9, Ruff targets 3.9, and mypy checks with Python 3.10 semantics. Keep public behavior typed; `disallow_untyped_defs` is enabled.

## Architecture boundaries

- `driver.run_fleet()` executes pre-flight, remote, custom, LXC, VM, node, manager, and notification/history work in order. Pre-flight and notification handling remain cross-cutting even for selected phases.
- Per-host flows follow detect → dry-check → backup → update → health → report, with rescue/rollback and cleanup where applicable.
- Python performs sequencing, dry-run selection, retries, rollback decisions, parsing, HTTP calls, file writes, and status decisions.
- Ansible primitives perform one remote or privileged action and return facts. Do not move business logic into YAML.
- Flows depend on the `Executor` protocol so tests can inject scripted fakes.
- VM flows use two executors: guest SSH for package operations and Proxmox-node SSH for `qm` status/rollback.
- The dashboard reads persisted history/scans and launches the CLI as a detached subprocess. It must not execute fleet operations in-process.
- `invoke_primitive()` requires the repository root as the current working directory because Ansible Runner resolves primitive paths from `os.getcwd()`. Start the dashboard and Molecule harnesses from their documented locations.

## Critical invariants

- Dry-run behavior is controlled in Python by choosing simulation commands. `run_shell.yml` and `reboot_host.yml` use `check_mode: false` and will execute the command they receive.
- Pin package and locale-sensitive commands to `LC_ALL=C`; parsers depend on stable English output.
- Reuse `flows/_pkg.py` for package-manager detection, upgrade commands, and Kuma health checks. Use `if`/`elif`/`else`, not shell `&&`/`||` detection chains.
- Snapshot creation, rollback, and cleanup share the fixed name `BEFORE_UPDATE_AUTO`. Rollback is allowed only when a snapshot was actually created (`snap_taken`). A `vzdump` is never an automatic restore source.
- Snapshot `api_host` is the node's `ansible_host` address, not its inventory name.
- VMIDs are not globally unique across clusters. Preserve cluster-aware identity; LXC scan/ledger identities use `node/id`. Ambiguity must fail loudly rather than guess.
- The manager's host node must not reboot when it would stop the manager LXC. Unreachable PVE nodes are skippable only while the cluster remains quorate; VM guest failures remain hard failures.
- Use the manual inventory parser in `inventory.py`; `configparser` misparses Ansible inventory lines.
- `settings.notifiers` defaults to `None`. An explicit `[]` means no notifiers and must not activate the legacy `discord_webhook` fallback.
- `-e KEY=VALUE` accepts booleans but only five keys affect behavior: `fleet_dry_run`, `lxc_verbose`, `force_notify`, `force_window`, and `custom_dry_run`. String settings belong in `vars.yml` or `--vars-file`.
- Custom configuration commands are opaque shell strings. Only `{{ steps.NAME }}` references are resolved by `steps.run_steps()`.
- LXC update command failures can be recorded without raising so both OS and app work can finish. They must still populate errors, mark the outcome/state failed, and preserve useful failure detail; this path intentionally does not roll back.
- `briefing.render_briefing()` must not add a trailing newline. Preserve the golden fixture in `tests/unit/data/briefing_golden.json` and Discord embed markdown constraints.
- Persisted run/scan schemas evolve. Readers and dashboard routes must tolerate missing legacy keys with defensive access.

## Coding and testing conventions

- Add regression tests for changes to flows, policy/status decisions, parsing, state, history, scans, or web routes.
- Flow tests use local `Scripted*Executor` implementations with queued `PrimitiveResult` values. Keep unit tests independent of live hosts and Ansible.
- Keep regex extraction in `lxc_parse.py` and update parser/status tests together.
- Shared retry and step helpers accept injectable sleep functions; use them to keep tests fast. `run_node_phase()` uses real `time.sleep`, so monkeypatch it in tests.
- Preserve the VM guest/node executor distinction in tests and implementation.
- Preserve package detail semantics: absent/idle/old detail remains optional, and retention must not affect cumulative totals.
- Idle LXC records are intentionally suppressed. Do not turn no-op containers into report noise.
- Keep manager-side outbound HTTP (for example, GitHub community-script lookups) out of PVE-node primitives.

## Operational safety

Do not run a live fleet update, modify real inventory/secrets, push SSH keys, or change notification credentials unless explicitly requested. Prefer `--dry-run`, `--limit`, and `--phases` when validating operational behavior. Use unit tests and scripted executors for normal development.
