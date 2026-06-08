"""Tests for proxmox_fleet.deps — mirrors test_custom_depends.py's contract
using plain Python (no Jinja shim).
"""

from proxmox_fleet.deps import dependency_failed, validate_depends_order


# --- validate_depends_order ---------------------------------------------------

def test_empty_hosts_no_problems():
    assert validate_depends_order([], {}) == []


def test_no_dependencies_no_problems():
    hosts = ["db-01", "app-01", "web-01"]
    assert validate_depends_order(hosts, {}) == []


def test_dep_listed_before_dependant_is_ok():
    hosts = ["db-01", "app-01"]
    dep_map = {"app-01": ["db-01"]}
    assert validate_depends_order(hosts, dep_map) == []


def test_dep_listed_after_dependant_is_problem():
    hosts = ["app-01", "db-01"]
    dep_map = {"app-01": ["db-01"]}
    problems = validate_depends_order(hosts, dep_map)
    assert len(problems) == 1
    assert "app-01" in problems[0]
    assert "db-01" in problems[0]
    assert "AFTER" in problems[0]


def test_missing_dep_host_is_problem():
    hosts = ["app-01"]
    dep_map = {"app-01": ["db-01"]}
    problems = validate_depends_order(hosts, dep_map)
    assert len(problems) == 1
    assert "missing custom host" in problems[0]
    assert "db-01" in problems[0]


def test_multiple_problems_all_reported():
    hosts = ["app-01", "web-01"]
    dep_map = {
        "app-01": ["missing-dep"],
        "web-01": ["app-01"],  # app-01 is listed BEFORE web-01, so this is OK
    }
    problems = validate_depends_order(hosts, dep_map)
    assert len(problems) == 1
    assert "missing-dep" in problems[0]


def test_ordering_problem_message_exact_format():
    hosts = ["app-01", "db-01"]
    dep_map = {"app-01": ["db-01"]}
    problems = validate_depends_order(hosts, dep_map)
    assert problems[0] == (
        "app-01 depends_on db-01 which is listed AFTER it; "
        "reorder [custom_hosts] so dependencies come first"
    )


def test_missing_dep_message_exact_format():
    hosts = ["app-01"]
    dep_map = {"app-01": ["no-such-host"]}
    problems = validate_depends_order(hosts, dep_map)
    assert problems[0] == "app-01 depends_on missing custom host no-such-host"


def test_multiple_deps_for_one_host():
    hosts = ["db-01", "cache-01", "app-01"]
    dep_map = {"app-01": ["db-01", "cache-01"]}
    assert validate_depends_order(hosts, dep_map) == []


def test_multiple_deps_one_missing():
    hosts = ["db-01", "app-01"]
    dep_map = {"app-01": ["db-01", "no-cache"]}
    problems = validate_depends_order(hosts, dep_map)
    assert len(problems) == 1
    assert "no-cache" in problems[0]


# --- dependency_failed --------------------------------------------------------

def test_no_deps_never_fails():
    assert dependency_failed("app-01", [], {"db-01"}) is False


def test_dep_not_failed():
    assert dependency_failed("app-01", ["db-01"], set()) is False


def test_dep_in_failed_set():
    assert dependency_failed("app-01", ["db-01"], {"db-01"}) is True


def test_one_of_many_deps_failed():
    assert dependency_failed("app-01", ["db-01", "cache-01"], {"cache-01"}) is True


def test_irrelevant_host_failed():
    assert dependency_failed("app-01", ["db-01"], {"other-host"}) is False


def test_host_not_in_its_own_deps():
    assert dependency_failed("app-01", ["db-01"], {"app-01"}) is False
