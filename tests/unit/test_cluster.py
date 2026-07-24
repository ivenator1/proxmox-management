"""Tests for proxmox_fleet.cluster — qualified-id helpers."""

from proxmox_fleet.cluster import (
    DEFAULT_CLUSTER,
    api_creds,
    id_matches,
    limit_selects_id,
    map_lookup,
    matches_any,
    split_qualified,
    token_is_id,
)
from proxmox_fleet.models.settings import GlobalSettings


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


# --- token_is_id -------------------------------------------------------------------


def test_token_is_id_bare_digits():
    assert token_is_id("101") is True


def test_token_is_id_qualified_digits():
    assert token_is_id("alpha/101") is True


def test_token_is_id_host_name():
    assert token_is_id("pve-01") is False


def test_token_is_id_qualified_non_digit():
    assert token_is_id("alpha/web-01") is False


def test_token_is_id_empty_string():
    assert token_is_id("") is False


# --- id_matches --------------------------------------------------------------------


def test_id_matches_bare_token_matches_any_cluster():
    assert id_matches("101", "alpha", "101") is True
    assert id_matches("101", "beta", "101") is True


def test_id_matches_qualified_token_matches_only_its_cluster():
    assert id_matches("alpha/101", "alpha", "101") is True
    assert id_matches("alpha/101", "beta", "101") is False


def test_id_matches_vmid_mismatch():
    assert id_matches("101", "alpha", "102") is False
    assert id_matches("alpha/101", "alpha", "102") is False


def test_id_matches_int_vmid_arg():
    assert id_matches("101", "alpha", 101) is True  # type: ignore[arg-type]


# --- matches_any -------------------------------------------------------------------


def test_matches_any_bare_token_matches_both_clusters():
    tokens = ["101"]
    assert matches_any(tokens, "alpha", "101") is True
    assert matches_any(tokens, "beta", "101") is True


def test_matches_any_qualified_token_matches_only_its_cluster():
    tokens = ["alpha/101"]
    assert matches_any(tokens, "alpha", "101") is True
    assert matches_any(tokens, "beta", "101") is False


def test_matches_any_str_coerces_int_tokens():
    tokens = [101, "102"]
    assert matches_any(tokens, "alpha", 101) is True  # type: ignore[arg-type]
    assert matches_any(tokens, "alpha", "102") is True


def test_matches_any_empty_tokens():
    assert matches_any([], "alpha", "101") is False


def test_matches_any_no_match():
    assert matches_any(["999"], "alpha", "101") is False


# --- map_lookup --------------------------------------------------------------------


def test_map_lookup_bare_key():
    mapping = {"101": "5"}
    assert map_lookup(mapping, "alpha", "101") == "5"
    assert map_lookup(mapping, "beta", "101") == "5"


def test_map_lookup_exact_qualified_key_wins_over_bare():
    mapping = {"101": "5", "alpha/101": "9"}
    assert map_lookup(mapping, "alpha", "101") == "9"
    assert map_lookup(mapping, "beta", "101") == "5"


def test_map_lookup_default_when_absent():
    assert map_lookup({}, "alpha", "101") == ""
    assert map_lookup({}, "alpha", "101", default="none") == "none"


def test_map_lookup_int_vmid_arg():
    mapping = {"101": "5"}
    assert map_lookup(mapping, "alpha", 101) == "5"  # type: ignore[arg-type]


# --- limit_selects_id ----------------------------------------------------------------


def test_limit_selects_id_bare_token():
    assert limit_selects_id({"101"}, "alpha", "101") is True
    assert limit_selects_id({"101"}, "beta", "101") is True


def test_limit_selects_id_qualified_token_scopes_to_cluster():
    assert limit_selects_id({"alpha/101"}, "alpha", "101") is True
    assert limit_selects_id({"alpha/101"}, "beta", "101") is False


def test_limit_selects_id_host_name_token_never_matches():
    assert limit_selects_id({"pve-01"}, "alpha", "101") is False


def test_limit_selects_id_empty_limit():
    assert limit_selects_id(set(), "alpha", "101") is False


# --- api_creds -----------------------------------------------------------------------


def test_api_creds_no_pve_clusters_returns_globals():
    s = GlobalSettings(
        pve_api_user="root@pam",
        pve_api_token_id="ansible",
        pve_api_token_secret="global-secret",
    )
    assert api_creds(s, "alpha") == {
        "api_user": "root@pam",
        "api_token_id": "ansible",
        "api_token_secret": "global-secret",
    }


def test_api_creds_unknown_cluster_falls_back_to_globals():
    s = GlobalSettings.model_validate({
        "pve_api_user": "root@pam",
        "pve_api_token_id": "ansible",
        "pve_api_token_secret": "global-secret",
        "pve_clusters": {
            "beta": {"pve_api_token_secret": "beta-secret"},
        },
    })
    assert api_creds(s, "gamma") == {
        "api_user": "root@pam",
        "api_token_id": "ansible",
        "api_token_secret": "global-secret",
    }


def test_api_creds_full_cluster_override():
    s = GlobalSettings.model_validate({
        "pve_api_user": "root@pam",
        "pve_api_token_id": "ansible",
        "pve_api_token_secret": "global-secret",
        "pve_clusters": {
            "alpha": {
                "pve_api_user": "alpha-user@pve",
                "pve_api_token_id": "alpha-token",
                "pve_api_token_secret": "alpha-secret",
            },
        },
    })
    assert api_creds(s, "alpha") == {
        "api_user": "alpha-user@pve",
        "api_token_id": "alpha-token",
        "api_token_secret": "alpha-secret",
    }


def test_api_creds_per_field_fallback():
    # beta only overrides the token secret — user/token_id inherit globals.
    s = GlobalSettings.model_validate({
        "pve_api_user": "root@pam",
        "pve_api_token_id": "ansible",
        "pve_api_token_secret": "global-secret",
        "pve_clusters": {
            "beta": {"pve_api_token_secret": "beta-secret"},
        },
    })
    assert api_creds(s, "beta") == {
        "api_user": "root@pam",
        "api_token_id": "ansible",
        "api_token_secret": "beta-secret",
    }


def test_api_creds_empty_override_string_falls_back_to_global():
    s = GlobalSettings.model_validate({
        "pve_api_user": "root@pam",
        "pve_api_token_id": "ansible",
        "pve_api_token_secret": "global-secret",
        "pve_clusters": {
            "beta": {"pve_api_user": "", "pve_api_token_secret": "beta-secret"},
        },
    })
    assert api_creds(s, "beta")["api_user"] == "root@pam"
