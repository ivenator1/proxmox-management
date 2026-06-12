"""Tests for the init_db CLI tool."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from proxmox_fleet.web.init_db import main


class TestInitDbCli:
    """Tests for the init_db command-line interface."""

    def test_init_db_main_success(self):
        """Test that main() successfully initializes the database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            password = "test-admin-password-123"

            # Run main with arguments
            exit_code = main(
                argv=["--password", password, "--db-path", str(db_path)]
            )

            assert exit_code == 0
            assert db_path.exists()

    def test_init_db_main_missing_password(self):
        """Test that main() fails without password."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            exit_code = main(argv=["--db-path", str(db_path)])
            # Should fail due to missing --password
            assert exit_code == 2  # argparse error code

    def test_init_db_main_missing_db_path(self):
        """Test that main() fails without db-path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            password = "test-password"

            exit_code = main(argv=["--password", password])
            # Should fail due to missing --db-path
            assert exit_code == 2  # argparse error code

    def test_init_db_main_creates_db_dir(self):
        """Test that main() creates parent directories if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "nested" / "dir" / "test.db"
            password = "test-admin-password"

            exit_code = main(
                argv=["--password", password, "--db-path", str(db_path)]
            )

            assert exit_code == 0
            assert db_path.exists()
            assert db_path.parent.exists()

    def test_init_db_main_empty_password(self):
        """Test that main() handles empty password gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Empty password might be accepted by argparse but could fail in create_admin_user
            exit_code = main(argv=["--password", "", "--db-path", str(db_path)])
            # Could be 0 or 1 depending on implementation
            assert exit_code in [0, 1]
