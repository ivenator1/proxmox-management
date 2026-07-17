"""Cluster-qualified id helpers — single source of truth for id matching.

Two or more Proxmox clusters can each hold a container/VM with the same
vmid (e.g. LXC 101), so a bare id is not fleet-unique. Nodes carry an
optional ``cluster=`` inventory var (falling back to :data:`DEFAULT_CLUSTER`),
and id tokens in settings/--limit may be qualified as ``<cluster>/<vmid>``.
A bare token keeps the historical "matches in every cluster" behaviour.

Every id-matching decision in the fleet (settings exclude lists, --limit,
canary staging, kuma-map lookups) MUST go through the helpers below — never
re-implement `token in {str(x) for x in ...}` membership checks in a flow or
the driver, or qualified tokens silently stop working there.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

DEFAULT_CLUSTER = "default"


def split_qualified(token: str) -> Tuple[Optional[str], str]:
    """Split ``"alpha/101"`` → ``("alpha", "101")``; bare ``"101"`` → ``(None, "101")``.

    Splits on the first ``/`` only — the id part is everything after it.
    """
    text = str(token)
    if "/" in text:
        cluster, _, vmid = text.partition("/")
        return cluster, vmid
    return None, text


def token_is_id(token: str) -> bool:
    """True when *token* addresses an id: bare digits, or qualified with a
    digit id-part (``"101"``, ``"alpha/101"``). False for host/node-name
    tokens (``"pve-01"``) and malformed qualifiers (``"alpha/web-01"``).
    """
    _, vmid = split_qualified(token)
    return vmid.isdigit()


def id_matches(token: str, cluster: str, vmid: str) -> bool:
    """Does *token* (settings entry / --limit token / canary entry) select
    the container/VM ``vmid`` on ``cluster``?

    A bare token (``"101"``) matches that vmid in ANY cluster — the
    historical, back-compat behaviour. A qualified token (``"alpha/101"``)
    matches only when both the cluster AND the vmid agree.
    """
    tok_cluster, tok_vmid = split_qualified(token)
    if tok_vmid != str(vmid):
        return False
    return tok_cluster is None or tok_cluster == cluster


def matches_any(tokens: Iterable[Any], cluster: str, vmid: str) -> bool:
    """True when any entry in *tokens* selects ``(cluster, vmid)``.

    str-coerces every token first (settings lists may hold YAML ints via the
    ``_stringify_id_list`` validator, but callers may still pass raw values).
    Replaces the old ``lxc_id in {str(x) for x in tokens}`` membership check
    fleet-wide — that pattern ignores cluster qualification entirely.
    """
    vmid_s = str(vmid)
    return any(id_matches(str(t), cluster, vmid_s) for t in tokens)


def map_lookup(mapping: Dict[str, Any], cluster: str, vmid: str, default: Any = "") -> Any:
    """Look up ``vmid`` in a kuma-style ``{"id_or_cluster/id": monitor}`` map.

    An exact ``"cluster/vmid"`` key wins over a bare ``"vmid"`` key so a
    multi-cluster fleet can override one cluster's monitor while every other
    cluster still falls back to the shared bare-id mapping.
    """
    vmid_s = str(vmid)
    qualified_key = f"{cluster}/{vmid_s}"
    if qualified_key in mapping:
        return mapping[qualified_key]
    return mapping.get(vmid_s, default)


def limit_selects_id(limit: Iterable[Any], cluster: str, vmid: str) -> bool:
    """True when a ``--limit`` token set selects the container/VM ``(cluster, vmid)``.

    Same matching rules as :func:`matches_any` (a bare limit token matches
    the id in any cluster) — a distinct name from ``matches_any`` only to
    keep call sites self-documenting about which list is being consulted.
    """
    return matches_any(limit, cluster, vmid)
