"""Parity tests for node_status(), manager_status(), node_should_report().

These mirror the Phase 2 / Phase 3 Jinja decision trees in fleet-update.yml
lines 317–322 (node_status_str) and line 383 (manager status ternary).
Plain Python — no Jinja shim needed.
"""


from proxmox_fleet.status import manager_status, node_should_report, node_status


# ---------------------------------------------------------------------------
# node_status() — 5 branches
# ---------------------------------------------------------------------------

class TestNodeStatus:
    def test_rebooted(self):
        assert node_status(True, True, True, False) == "UPDATED & REBOOTED"

    def test_rebooted_wins_over_manager(self):
        # rebooted=True takes priority even if is_manager=True (edge case)
        assert node_status(True, True, True, True) == "UPDATED & REBOOTED"

    def test_manager_reboot_needed(self):
        assert node_status(True, True, False, True) == "UPDATED (MANUAL REBOOT REQ)"

    def test_manager_reboot_needed_apt_unchanged(self):
        # reboot needed (kernel mismatch), no apt change, is_manager
        assert node_status(False, True, False, True) == "UPDATED (MANUAL REBOOT REQ)"

    def test_reboot_failed(self):
        # Logically unreachable in normal flow (failed reboot → except → FAILED),
        # but kept for parity with the Jinja template.
        assert node_status(True, True, False, False) == "REBOOT FAILED"

    def test_reboot_failed_no_apt_change(self):
        assert node_status(False, True, False, False) == "REBOOT FAILED"

    def test_manual_reboot_when_auto_reboot_disabled(self):
        assert (
            node_status(False, True, False, False, reboot_disabled=True)
            == "UPDATED (MANUAL REBOOT REQ)"
        )

    def test_reboot_failed_reserved_for_enabled_policy(self):
        assert node_status(True, True, False, False, reboot_disabled=False) == "REBOOT FAILED"

    def test_updated_no_reboot(self):
        assert node_status(True, False, False, False) == "UPDATED"

    def test_updated_no_reboot_manager(self):
        # apt changed, no reboot needed, is_manager — just UPDATED
        assert node_status(True, False, False, True) == "UPDATED"

    def test_ok_no_changes(self):
        assert node_status(False, False, False, False) == "OK"

    def test_ok_manager_no_changes(self):
        assert node_status(False, False, False, True) == "OK"


# ---------------------------------------------------------------------------
# manager_status() — 3 branches
# ---------------------------------------------------------------------------

class TestManagerStatus:
    def test_reboot_needed(self):
        assert manager_status(True, True) == "UPDATED (MANUAL REBOOT REQ)"

    def test_reboot_needed_no_apt_change(self):
        # Kernel mismatch but apt changed nothing
        assert manager_status(False, True) == "UPDATED (MANUAL REBOOT REQ)"

    def test_updated(self):
        assert manager_status(True, False) == "UPDATED"

    def test_ok(self):
        assert manager_status(False, False) == "OK"


# ---------------------------------------------------------------------------
# node_should_report() — always True (no idle suppression for nodes)
# ---------------------------------------------------------------------------

class TestNodeShouldReport:
    def test_always_true(self):
        assert node_should_report() is True
