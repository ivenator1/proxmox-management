"""Exact OS package detail — pure, manager-side parsers (PR1 observability).

Every update flow captures the upgrade stdout and, until now, kept only a
bool/count. This module turns that already-flowing output into the exact
package list (``[{"name", "from", "to"}]``) that run records carry in their
``packages`` field. All parsing happens on the manager against ``LC_ALL=C``
pinned command output — the primitives are untouched.

Supported output shapes (per package manager):

- apt real:    ``Unpacking pkg[:arch] (NEW) over (OLD)`` plus the new-install
               ``Unpacking pkg (NEW)`` form (``from`` empty).
- apt simulate: ``Inst pkg[:arch] [OLD] (NEW repo ...)`` and the no-old form.
- dnf real:    tokens between ``Upgraded:`` and the next section header,
               ``name-version-release.arch`` tokens (multi-column, no old
               version) — ``from`` empty.
- dnf assumeno: the ``Upgrading:`` table (Package/Arch/Version columns).
- apk:         ``(i/n) Upgrading pkg (old -> new)`` — the ``(i/n)`` prefix is
               optional so simulate (``apk -s``) lines parse too.
- unknown/garbage: ``[]`` (a flow never fails because one line drifted).
"""

from __future__ import annotations

import re
from typing import Dict, List

# ostype (pct config) → package manager, for containers. Only apk differs
# meaningfully (alpine); every other supported LXC OS is apt or dnf.
_OSTYPE_PKG_MGR = {
    "debian": "apt",
    "ubuntu": "apt",
    "devuan": "apt",
    "alpine": "apk",
}

# dnf real-run package tokens carry a trailing `.arch`; strip it before the
# `name-version[-release]` split so `foo-1.0.x86_64` → name "foo", version "1.0".
_ARCH_SUFFIX_RE = re.compile(
    r"\.(?:x86_64|noarch|aarch64|i686|i386|ppc64le|s390x|armv7hl|armv7hnl|src)$"
)

# apt real-run: "Unpacking libssl3:amd64 (3.0.13-1) over (3.0.11-1) ..."
# (with or without the `over (OLD)` clause — new installs lack it).
_APT_REAL_RE = re.compile(
    r"^Unpacking (\S+?)(?::\S+)? \(([^)]+)\)(?: over \(([^)]+)\))?",
    re.MULTILINE,
)

# apt simulate: "Inst curl [8.5.0-1] (8.5.0-2 Debian:...)" — version first
# token inside the parens; optional [OLD] bracket; optional :arch suffix.
_APT_INST_OLD_RE = re.compile(
    r"^Inst (\S+?)(?::\S+)? \[([^]]+)\] \(([^)\s]+)", re.MULTILINE
)
_APT_INST_NEW_RE = re.compile(
    r"^Inst (\S+?)(?::\S+)? \(([^)\s]+)", re.MULTILINE
)

# apk: "(1/2) Upgrading musl (1.2.4-r0 -> 1.2.5-r0)" — the (i/n) prefix is
# optional so `apk -s` simulate lines (which print it too, but may not) parse.
_APK_RE = re.compile(
    r"^(?:\(\d+/\d+\) )?Upgrading (\S+) \((\S+) -> (\S+)\)", re.MULTILINE
)

# Section headers that terminate a dnf block: "Upgraded:" (real post-transaction
# summary) and "Upgrading:" (assumeno table) are each followed by sibling
# sections ("Installed:", "Removed:") or "Complete!" / "Transaction Summary".
_DNF_SECTION_END_RE = re.compile(r"^[A-Za-z][A-Za-z ]+:$")


def pkg_mgr_for_ostype(os_type: str) -> str:
    """Map a container's ``ostype`` (pct config) to its package manager.

    Only apk differs meaningfully (alpine); every other supported LXC OS is
    apt or dnf. Falls back to ``dnf`` for unknown/unsupported OS types (fedora,
    arch) so the caller always has a manager to try. Single home for the map
    scan.py previously kept as ``_OSTYPE_PKG_MGR``.
    """
    return _OSTYPE_PKG_MGR.get(os_type, "dnf")


def _dedupe(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Drop duplicate (name, from, to) triples, preserving first-seen order."""
    seen = set()
    out: List[Dict[str, str]] = []
    for item in items:
        key = (item["name"], item["from"], item["to"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _dnf_real(stdout: str) -> List[Dict[str, str]]:
    """Parse the real-run ``Upgraded:`` block: ``name-version-release.arch``
    tokens (multi-column), up to the next section header / ``Complete!``."""
    out: List[Dict[str, str]] = []
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "Upgraded:":
            continue
        for j in range(i + 1, len(lines)):
            cur = lines[j].strip()
            if not cur or _DNF_SECTION_END_RE.match(cur) or cur.startswith("Complete!"):
                break
            for tok in cur.split():
                if not re.search(r"\d", tok):
                    continue  # separators/decorative rows (e.g. a bare "---")
                base = _ARCH_SUFFIX_RE.sub("", tok)
                parts = base.rsplit("-", 2)
                if len(parts) < 2:
                    continue
                # dnf may print either name-version-release.arch or
                # name.arch-version-release depending on command/version.
                name = re.sub(r"\.(?:x86_64|noarch|aarch64|i686|i386|ppc64le|s390x|armv7hl|armv7hnl|src)$", "", parts[0])
                out.append({"name": name, "from": "",
                            "to": "-".join(parts[1:])})
        break  # one Upgraded: section per run
    return out


def _dnf_assumeno(stdout: str) -> List[Dict[str, str]]:
    """Parse the dry-run ``Upgrading:`` table (Package/Arch/Version/... rows)."""
    out: List[Dict[str, str]] = []
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "Upgrading:":
            continue
        for j in range(i + 1, len(lines)):
            cur = lines[j].strip()
            if not cur or _DNF_SECTION_END_RE.match(cur) or cur.startswith("Transaction Summary"):
                break
            toks = cur.split()
            if len(toks) < 3 or toks[0] == "Package":
                continue  # header row, or a malformed row
            out.append({"name": toks[0], "from": "", "to": toks[2]})
        break
    return out


def _parse_dnf(stdout: str) -> List[Dict[str, str]]:
    """Real-run output wins when both shapes could be present."""
    if re.search(r"^Upgraded:$", stdout, re.MULTILINE):
        return _dnf_real(stdout)
    return _dnf_assumeno(stdout)


def parse_upgraded(stdout: str, pkg_mgr: str) -> List[Dict[str, str]]:
    """Extract the exact packages an upgrade would/did install.

    Returns ``[{"name", "from", "to"}]`` in output order, de-duplicated.
    ``from`` is ``""`` when the output carries no old version (apt new
    installs, dnf real runs). Unknown package managers and unparseable output
    (dnf5 drift, garbage) yield ``[]`` — never an exception, so a flow never
    fails because one line drifted.
    """
    if not stdout:
        return []

    if pkg_mgr == "apt":
        out: List[Dict[str, str]] = []
        for m in _APT_REAL_RE.finditer(stdout):
            out.append({"name": m.group(1),
                        "to": m.group(2),
                        "from": m.group(3) or ""})
        for m in _APT_INST_OLD_RE.finditer(stdout):
            out.append({"name": m.group(1), "from": m.group(2), "to": m.group(3)})
        for m in _APT_INST_NEW_RE.finditer(stdout):
            out.append({"name": m.group(1), "from": "", "to": m.group(2)})
        return _dedupe(out)

    if pkg_mgr == "dnf":
        return _dedupe(_parse_dnf(stdout))

    if pkg_mgr == "apk":
        out = [{"name": m.group(1), "from": m.group(2), "to": m.group(3)}
               for m in _APK_RE.finditer(stdout)]
        return _dedupe(out)

    return []
