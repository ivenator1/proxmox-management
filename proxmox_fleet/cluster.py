"""Cluster-qualified id helpers — single source of truth for id matching.

Two or more Proxmox clusters can each hold a container/VM with the same
vmid (e.g. LXC 101), so a bare id is not fleet-unique. Nodes carry an
optional ``cluster=`` inventory var (falling back to :data:`DEFAULT_CLUSTER`),
and id tokens in settings/--limit may be qualified as ``<cluster>/<vmid>``.
A bare token keeps the historical "matches in every cluster" behaviour.
"""
from __future__ import annotations

from typing import Optional, Tuple

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
