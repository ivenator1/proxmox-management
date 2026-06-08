"""Notifier dispatch — the Python port of ``tasks/notify.yml`` plus the Phase-4
notifier-resolution shim from ``fleet-update.yml``.

The briefing body is rendered once by the caller and shared verbatim across all
notifiers; only the transport envelope differs (Discord embed vs ntfy headers).
All HTTP goes through ``proxmox_fleet.http`` (stdlib ``urllib``). Failures are
swallowed — notification must never abort the run (the legacy tasks use
``ignore_errors: yes``).
"""

from __future__ import annotations

from typing import Any, Dict, List

from proxmox_fleet import http
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.orchestration import retry


def resolve_notifiers(settings: GlobalSettings) -> List[Dict[str, Any]]:
    """Resolve the notifier list — explicit ``notifiers`` wins, else synth from
    ``discord_webhook`` (back-compat shim), else empty.

    ``settings.notifiers`` defaults to ``None`` so an unset value is
    distinguishable from an explicit empty list — matching the Ansible
    ``notifiers is defined`` check.
    """
    explicit = getattr(settings, "notifiers", None)
    if explicit is not None:
        return list(explicit)
    webhook = getattr(settings, "discord_webhook", "") or ""
    if webhook.strip():
        return [{"type": "discord", "enabled": True, "webhook": webhook}]
    return []


def _ntfy_headers(notifier: Dict[str, Any], *, ntfy_title: str, failed: bool) -> Dict[str, str]:
    """Build the ntfy HTTP headers — mirrors the ``combine`` expression in notify.yml."""
    headers: Dict[str, str] = {
        "Title": ntfy_title,
        "Priority": str(notifier.get("priority", 5 if failed else 3)),
        "Tags": str(notifier.get("tags", "warning" if failed else "white_check_mark")),
        "Markdown": "yes",
    }
    token = notifier.get("token")
    if token is not None and str(token).strip():
        headers["Authorization"] = f"Bearer {token}"
    return headers


def dispatch(
    notifiers: List[Dict[str, Any]],
    *,
    title: str,
    ntfy_title: str,
    body: str,
    color: int,
    failed: bool,
    retries: int = 15,
) -> None:
    """Send the briefing to every enabled notifier. Errors are swallowed per notifier."""
    for notifier in notifiers:
        if not notifier.get("enabled", True):
            continue
        ntype = notifier.get("type")
        try:
            if ntype == "discord":
                http.post_json(
                    notifier["webhook"],
                    {"embeds": [{"title": title, "description": body, "color": color}]},
                    retries=retries,
                    delay=10.0,
                    ok_statuses=(200, 204),
                )
            elif ntype == "ntfy":
                headers = _ntfy_headers(notifier, ntfy_title=ntfy_title, failed=failed)
                retry(
                    lambda: http.request(
                        notifier["url"],
                        method="POST",
                        headers=headers,
                        data=body.encode("utf-8"),
                    ),
                    retries=retries,
                    delay=10.0,
                    until=lambda r: r.status == 200,
                )
        except Exception:  # noqa: BLE001 - mirror ignore_errors: yes
            continue


def ping_deadmans(url: str, *, failed: bool, retries: int = 5) -> None:
    """Dead-man's-switch ping (``/fail`` suffix on failure). Errors swallowed."""
    if not url or not url.strip():
        return
    target = url + ("/fail" if failed else "")
    try:
        retry(
            lambda: http.request(target, method="GET"),
            retries=retries,
            delay=10.0,
            until=lambda r: r.status in (200, 201, 202, 204),
        )
    except Exception:  # noqa: BLE001 - mirror ignore_errors: yes
        pass
