"""Tests for proxmox_fleet.cluster — qualified-id helpers."""

from proxmox_fleet.cluster import DEFAULT_CLUSTER, split_qualified


def test_default_cluster_constant():
    assert DEFAULT_CLUSTER == "default"


def test_split_qualified_bare_id():
    assert split_qualified("101") == (None, "101")


def test_split_qualified_cluster_id():
    assert split_qualified("alpha/101") == ("alpha", "101")


def test_split_qualified_splits_on_first_slash_only():
    assert split_qualified("alpha/101/extra") == ("alpha", "101/extra")


def test_split_qualified_bare_name_token():
    # canary_hosts mixes host names with ids — names pass through unqualified.
    assert split_qualified("pve-01") == (None, "pve-01")


def test_split_qualified_coerces_non_str():
    # settings lists may carry YAML ints.
    assert split_qualified(101) == (None, "101")  # type: ignore[arg-type]
