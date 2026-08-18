"""Read-only manual update checks for appliances the fleet cannot patch in place.

TrueNAS SCALE and OPNsense boxes apply updates through their own web UIs, so
the fleet only *reports* what is available and never installs anything. Each
adapter owns one fixed, sentinel-delimited, read-only SSH command — or, for
 the ``*_api`` adapters, two read-only REST API calls — and normalizes the
result into a common :class:`ManualUpdateResult` shape:

- no install/apply operation exists — commands/endpoints only query version
  state;
- adapters are validated *before* host contact: unknown adapter names,
  command-invariant violations, and missing API credentials are caught
  without touching the network;
- host unreachability — the ansible ``unreachable`` flag, a raised
  :class:`UnreachableHostError`, recognized SSH error text, or an API
  connection-level failure (``URLError``/``OSError``/``TimeoutError``) — is
  normalized to ``unreachable=True`` with ``error`` populated and the output
  parser skipped entirely;
- genuine command/parser failures and HTTP error statuses (4xx/5xx) stay
  ``unreachable=False``.

Lookup goes through :data:`MANUAL_UPDATE_REGISTRY`. Call
:func:`run_manual_update_check` with an adapter name, an :class:`Executor`
bound to the target host (SSH adapters only; API adapters take a
:class:`ManualUpdateApiConfig` instead), and the host string; the returned
:class:`ManualUpdateResult` is always full-shaped.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import time
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from proxmox_fleet import http
from proxmox_fleet.http import HttpResponse
from proxmox_fleet.executor import Executor
from proxmox_fleet.runner import PrimitiveResult, UnreachableHostError, is_unreachable_error

# Line that separates the version section from the check section inside the
# single remote command's stdout. Chosen so it cannot appear in either section.
_SEPARATOR = "@@MANUAL_UPDATE_SEPARATOR@@"


class ManualUpdateError(Exception):
    """Base class for manual-update adapter failures."""


class ManualUpdateAdapterError(ManualUpdateError):
    """An adapter invariant was violated (caught during validation, before host contact)."""


class ManualUpdateParseError(ManualUpdateError):
    """The remote command/API ran, but its output could not be normalized."""


class UnknownManualUpdateAdapterError(ManualUpdateError, ValueError):
    """The requested adapter name is not registered."""


@dataclass
class ManualUpdateResult:
    """The normalized outcome of one manual update check.

    ``error`` (and ``unreachable``) carry failures; the remaining fields are
    the parsed update state. Every field is always present so consumers can
    render a report row without inspecting which failure path was taken.
    """

    host: str
    display_name: str
    adapter: str
    current: str = ""
    latest: str = ""
    update_available: bool = False
    reboot_required: bool = False
    summary: str = ""
    details: List[str] = field(default_factory=list)
    apply_hint: str = ""
    unreachable: bool = False
    error: str = ""


@dataclass
class ManualUpdateApiConfig:
    """Per-host connection settings for the ``*_api`` manual-update adapters.

    Built by the scan wiring from the host's inventory vars; a blank
    ``api_url`` falls back to ``https://<host>`` inside the adapters.
    ``timeout`` is the per-request timeout in seconds; ``verify_ssl`` mirrors
    the per-host ``verify_ssl`` inventory var (default true).
    """

    api_url: str = ""
    api_key: str = ""
    api_secret: str = ""
    verify_ssl: bool = True
    timeout: float = 120.0


class ManualUpdateAdapter(Protocol):
    """Contract every manual-update adapter satisfies.

    ``validate`` runs before any host contact and raises
    :class:`ManualUpdateAdapterError` when the adapter is misconfigured;
    ``check`` performs the read-only check and never raises. ``transport``
    names the execution channel: ``"ssh"`` (uses *executor*) or ``"api"``
    (uses *config* instead).
    """

    name: str
    display_name: str
    apply_hint: str
    transport: str

    def validate(self, config: Optional[ManualUpdateApiConfig] = None) -> None:
        ...

    def check(
        self,
        executor: Optional[Executor],
        host: str,
        config: Optional[ManualUpdateApiConfig] = None,
    ) -> ManualUpdateResult:
        ...


class BaseManualUpdateAdapter:
    """Shared read-only check plumbing for manual-update adapters.

    Subclasses fix ``name``, ``display_name``, ``apply_hint``,
    ``check_command`` and ``forbidden_tokens``, and implement
    ``_validate_required_commands`` plus ``parse``. SSH-transport adapters
    keep ``transport = "ssh"``; API-transport adapters subclass
    :class:`BaseApiManualUpdateAdapter` instead.
    """

    name = ""
    display_name = ""
    apply_hint = ""
    transport = "ssh"
    check_command = ""
    forbidden_tokens: Tuple[str, ...] = ()

    def validate(self, config: Optional[ManualUpdateApiConfig] = None) -> None:
        """Check registry/command invariants. Runs before any host contact."""
        self._validate_identity()
        if not self.check_command:
            raise ManualUpdateAdapterError(
                f"manual update adapter {self.name!r} has no check command"
            )
        if _SEPARATOR not in self.check_command:
            raise ManualUpdateAdapterError(
                f"{self.name}: check command must be sentinel-delimited"
            )
        for token in self.forbidden_tokens:
            if token in self.check_command:
                raise ManualUpdateAdapterError(
                    f"{self.name}: check command must be read-only, found forbidden token {token!r}"
                )
        self._validate_required_commands()
        self._validate_api_config(config)

    def _validate_identity(self) -> None:
        """Registry-name invariants shared by both transports."""
        if not self.name:
            raise ManualUpdateAdapterError("manual update adapter is missing a registry name")
        if not self.display_name:
            raise ManualUpdateAdapterError(
                f"manual update adapter {self.name!r} is missing a display name"
            )

    def _validate_required_commands(self) -> None:
        """Subclass hook: assert the exact expected invocations are present."""

    def _validate_api_config(self, config: Optional[ManualUpdateApiConfig]) -> None:
        """Subclass hook: assert API credentials are present (API adapters only)."""

    def check(
        self,
        executor: Optional[Executor],
        host: str,
        config: Optional[ManualUpdateApiConfig] = None,
    ) -> ManualUpdateResult:
        """Run the read-only check and normalize the result. Never raises."""
        outcome = ManualUpdateResult(
            host=host,
            display_name=self.display_name,
            adapter=self.name,
            apply_hint=self.apply_hint,
        )
        if executor is None:
            outcome.error = f"{self.name}: SSH transport requires an executor"
            return outcome
        try:
            res = executor.run_shell(self.check_command, changed_when=False, ignore_errors=True)
        except UnreachableHostError as exc:
            outcome.unreachable = True
            outcome.error = f"Host unreachable: {exc}"
            return outcome
        except Exception as exc:  # noqa: BLE001 - a manual-update check never raises
            outcome.error = f"Manual update check failed: {exc}"
            return outcome

        # Unreachable always wins over parsing: the command never produced output.
        if res.unreachable or is_unreachable_error(res.stderr) or is_unreachable_error(res.stdout):
            detail = (res.stderr or res.stdout or "SSH connection failed").strip().replace("\n", " ")
            outcome.unreachable = True
            outcome.error = f"Host unreachable: {detail[-300:]}"
            return outcome

        try:
            return self.parse(res, outcome)
        except ManualUpdateParseError as exc:
            outcome.error = str(exc)
            return outcome
        except Exception as exc:  # noqa: BLE001 - parser failures are reported, not raised
            outcome.error = f"Failed to parse {self.name} output: {exc}"
            return outcome

    def parse(self, res: PrimitiveResult, outcome: ManualUpdateResult) -> ManualUpdateResult:
        """Normalize the executed command's rc/stdout/stderr into *outcome*."""
        raise NotImplementedError(f"{self.name} adapter must implement parse()")


# TrueNAS update.check_available status values — shared by the midclt (SSH)
# and REST adapters so both transports normalize identically.
_TRUENAS_STATUS_AVAILABLE = "AVAILABLE"
_TRUENAS_STATUS_REBOOT = "REBOOT_REQUIRED"
_TRUENAS_CLEAN_STATUSES = ("CURRENT", "UNAVAILABLE")


class TrueNASScaleAdapter(BaseManualUpdateAdapter):
    """Read-only update check for TrueNAS SCALE via midclt.

    Runs exactly ``midclt call system.version`` and
    ``midclt call update.check_available`` in one sentinel-delimited command;
    the JSON is parsed here in Python (no jq/apt/updater on the box).

    Status mapping: ``AVAILABLE`` means an update is available, ``REBOOT_REQUIRED``
    means a reboot is pending, ``CURRENT``/``UNAVAILABLE`` are clean no-update
    states, and any other status is an error.
    """

    name = "truenas_scale"
    display_name = "TrueNAS SCALE"
    apply_hint = "TrueNAS GUI → System Settings → Update"
    check_command = (
        "midclt call system.version"
        " && printf '\\n@@MANUAL_UPDATE_SEPARATOR@@\\n'"
        " && midclt call update.check_available"
    )
    forbidden_tokens = ("apt", "jq", "updater", "apply", "upgrade")

    def _validate_required_commands(self) -> None:
        for required in ("midclt call system.version", "midclt call update.check_available"):
            if required not in self.check_command:
                raise ManualUpdateAdapterError(
                    f"{self.name}: expected {required!r} in check command"
                )

    def parse(self, res: PrimitiveResult, outcome: ManualUpdateResult) -> ManualUpdateResult:
        if res.rc != 0 or res.failed:
            detail = (res.stderr or res.stdout or "no output").strip().replace("\n", " ")
            raise ManualUpdateParseError(
                f"TrueNAS update check failed (rc={res.rc}): {detail[-300:]}"
            )
        version, payload_text = _split_sections(res, self.display_name)
        try:
            payload = json.loads(payload_text)
        except ValueError as exc:
            raise ManualUpdateParseError(
                f"Malformed TrueNAS update.check_available JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ManualUpdateParseError(
                "TrueNAS update.check_available returned a non-object value"
            )
        _apply_truenas_payload(outcome, version, payload)
        return outcome


def _apply_truenas_payload(
    outcome: ManualUpdateResult, version: str, payload: Dict[str, Any]
) -> None:
    """Normalize a TrueNAS ``update.check_available`` payload into *outcome*.

    Shared by the midclt (SSH) and REST adapters so both transports produce
    identical current/latest/summary/detail fields. Raises
    :class:`ManualUpdateParseError` on unknown status values (fail closed).
    """
    changes = payload.get("changes")
    if not isinstance(changes, list):
        changes = []
    outcome.current = _component_version(version) or _first_component(changes, "old")
    if payload.get("REBOOT_REQUIRED"):
        outcome.reboot_required = True

    status = payload.get("status")
    train = str(payload.get("train") or "").strip()

    if status == _TRUENAS_STATUS_AVAILABLE:
        outcome.update_available = True
        latest = _component_version(payload.get("version")) or _first_component(changes, "new")
        outcome.latest = latest
        if outcome.current and latest:
            outcome.summary = f"TrueNAS update available: {outcome.current} -> {latest}"
        elif latest:
            outcome.summary = f"TrueNAS update available: {latest}"
        else:
            outcome.summary = "TrueNAS update available"
        if train:
            outcome.summary += f" (train {train})"
    elif status == _TRUENAS_STATUS_REBOOT:
        outcome.reboot_required = True
        outcome.summary = "TrueNAS reboot required to complete the pending update"
        if train:
            outcome.summary += f" (train {train})"
    elif status in _TRUENAS_CLEAN_STATUSES:
        outcome.summary = "TrueNAS is up to date"
        if train:
            outcome.summary += f" on train {train}"
    else:
        raise ManualUpdateParseError(f"Unknown TrueNAS update status: {status!r}")

    _append_changes(outcome, changes)
    _append_notice(outcome, payload)


def _body_excerpt(body: str) -> str:
    """One-line excerpt of an API response body for error messages."""
    return (body or "(empty)").strip().replace("\n", " ")[:300]


def _tls_verify_error(name: str, exc: ssl.SSLCertVerificationError) -> str:
    """Actionable error for a certificate verification failure.

    Classified as an error (not unreachable): a self-signed appliance cert is
    a per-host config problem — set ``verify_ssl=false`` or install a trusted
    certificate on the appliance.
    """
    return (
        f"{name}: HTTPS certificate verification failed — set verify_ssl=false "
        f"for this host (self-signed cert) or install a trusted certificate: {exc}"
    )


class BaseApiManualUpdateAdapter(BaseManualUpdateAdapter):
    """HTTP-API transport for a vendor's read-only update check.

    Subclasses fix ``transport = "api"`` and implement ``_check_api``; this
    class owns the never-raises contract and the failure classification:

    - connection-level failures (``URLError``/``OSError``/``TimeoutError``)
      normalize to ``unreachable=True`` — the host could not be reached;
    - TLS certificate verification failures are **errors, not unreachable** —
      a self-signed appliance cert is a config problem (``verify_ssl=false``
      or a trusted cert), not a reachability one, so they fail loudly;
    - HTTP error statuses (4xx/5xx, including 401/403) are genuine errors
      with ``unreachable=False`` — the host answered, the check failed;
    - malformed/unknown payloads fail closed via :class:`ManualUpdateParseError`.
    """

    transport = "api"

    def validate(self, config: Optional[ManualUpdateApiConfig] = None) -> None:
        """API adapters carry no SSH command — registry + credential checks only."""
        self._validate_identity()
        self._validate_api_config(config)

    def _validate_api_config(self, config: Optional[ManualUpdateApiConfig]) -> None:
        if config is None or not config.api_key:
            raise ManualUpdateAdapterError(
                f"{self.name}: api_key is required for the API transport — "
                "set it inline in hosts.ini or in host_vars/<host>.yml"
            )

    def check(
        self,
        executor: Optional[Executor],
        host: str,
        config: Optional[ManualUpdateApiConfig] = None,
    ) -> ManualUpdateResult:
        outcome = ManualUpdateResult(
            host=host,
            display_name=self.display_name,
            adapter=self.name,
            apply_hint=self.apply_hint,
        )
        try:
            return self._check_api(outcome, host, config)
        except ManualUpdateParseError as exc:
            outcome.error = str(exc)
            return outcome
        except urllib.error.HTTPError as exc:
            outcome.error = f"{self.name} API check failed ({exc})"
            return outcome
        except ssl.SSLCertVerificationError as exc:
            outcome.error = _tls_verify_error(self.name, exc)
            return outcome
        except urllib.error.URLError as exc:
            # urllib wraps TLS failures in URLError; surface them as errors too.
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                outcome.error = _tls_verify_error(self.name, exc.reason)
                return outcome
            outcome.unreachable = True
            outcome.error = f"Host unreachable: {exc}"
            return outcome
        except (OSError, TimeoutError) as exc:
            outcome.unreachable = True
            outcome.error = f"Host unreachable: {exc}"
            return outcome
        except Exception as exc:  # noqa: BLE001 - an API check never raises
            outcome.error = f"Manual update check failed: {exc}"
            return outcome

    def _api_request(self, url: str, fn: Callable[[], HttpResponse]) -> HttpResponse:
        """Run an API HTTP call, annotating connection errors with the URL.

        DNS/refused/timeout failures are diagnosed much faster when the error
        names the exact URL that failed (e.g. an ``api_url`` hostname that does
        not resolve). HTTP status errors pass through untouched so callers can
        inspect ``exc.code``; TLS verification failures keep their type so the
        caller classifies them as errors, not unreachable.
        """
        try:
            return fn()
        except urllib.error.HTTPError:
            raise
        except ssl.SSLCertVerificationError as exc:
            raise ssl.SSLCertVerificationError(f"{exc} (while fetching {url})") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise ssl.SSLCertVerificationError(
                    f"{exc.reason} (while fetching {url})"
                ) from exc
            raise urllib.error.URLError(f"{exc} (while fetching {url})") from exc
        except OSError as exc:
            raise OSError(f"{exc} (while fetching {url})") from exc

    def _get_json(self, url: str, **kw: Any) -> Any:
        try:
            return self._api_request(url, lambda: http.get_json(url, **kw))
        except ValueError as exc:
            # Empty or non-JSON body (e.g. an endpoint that 200s with nothing)
            # is diagnosed much faster when the failing URL is named.
            raise ManualUpdateParseError(
                f"Non-JSON/empty response from {url}: {exc}"
            ) from exc

    def _post_json(self, url: str, payload: Any, **kw: Any) -> HttpResponse:
        return self._api_request(url, lambda: http.post_json(url, payload, **kw))

    def _check_api(
        self,
        outcome: ManualUpdateResult,
        host: str,
        config: Optional[ManualUpdateApiConfig],
    ) -> ManualUpdateResult:
        """Run the vendor's read-only API calls and normalize *outcome*."""
        raise NotImplementedError(f"{self.name} adapter must implement _check_api()")


class TrueNASScaleApiAdapter(BaseApiManualUpdateAdapter):
    """Read-only update check for TrueNAS SCALE via the REST API.

    Two calls mirroring the SSH adapter's midclt commands:
    ``GET /api/v2.0/system/version`` and ``POST /api/v2.0/update/check_available``
    with ``Authorization: Bearer <api_key>``. The payload normalizes through
    the same :func:`_apply_truenas_payload` as the SSH adapter. Read-only:
    no update/apply endpoint is ever invoked.
    """

    name = "truenas_scale_api"
    display_name = "TrueNAS SCALE"
    apply_hint = "TrueNAS GUI → System Settings → Update"
    transport = "api"

    _VERSION_PATH = "/api/v2.0/system/version"
    _CHECK_PATH = "/api/v2.0/update/check_available"
    _API_RETRIES = 1
    _API_RETRY_DELAY = 10.0

    def _check_api(
        self,
        outcome: ManualUpdateResult,
        host: str,
        config: Optional[ManualUpdateApiConfig],
    ) -> ManualUpdateResult:
        if config is None:
            raise ManualUpdateParseError(f"{self.name}: missing API config")
        base_url = (config.api_url or f"https://{host}").rstrip("/")
        headers = {"Authorization": f"Bearer {config.api_key}"}
        timeout = config.timeout
        verify = config.verify_ssl

        version_resp = self._api_request(
            f"{base_url}{self._VERSION_PATH}",
            lambda: http.request(
                f"{base_url}{self._VERSION_PATH}",
                method="GET",
                headers=headers,
                timeout=timeout,
                verify=verify,
            ),
        )
        check_resp = self._post_json(
            f"{base_url}{self._CHECK_PATH}",
            {},
            headers=headers,
            retries=self._API_RETRIES,
            delay=self._API_RETRY_DELAY,
            timeout=timeout,
            verify=verify,
        )
        if check_resp.status not in (200, 201, 202):
            raise ManualUpdateParseError(
                f"TrueNAS update.check_available failed (HTTP {check_resp.status}): "
                f"{_body_excerpt(check_resp.body)}"
            )
        try:
            payload = check_resp.json()
        except ValueError as exc:
            raise ManualUpdateParseError(
                f"Malformed TrueNAS update.check_available JSON: {exc}"
            ) from exc
        # Some middleware versions wrap the payload in a single-element list.
        if isinstance(payload, list) and len(payload) == 1:
            payload = payload[0]
        if not isinstance(payload, dict):
            raise ManualUpdateParseError(
                "TrueNAS update.check_available returned a non-object value"
            )
        _apply_truenas_payload(outcome, version_resp.body, payload)
        return outcome


class OpnsenseAdapter(BaseManualUpdateAdapter):
    """Read-only update check for OPNsense.

    One fixed command: ``LC_ALL=C opnsense-version`` followed by
    ``LC_ALL=C opnsense-update -c`` (check mode only — never a bare update,
    never an apply). rc and output are both preserved for classification:

    - rc=2 → an update is available (opnsense-update convention);
    - rc=0 with ``A newer version is available`` → update available;
    - rc=0 with ``Nothing to do.`` / ``Currently up to date.`` → no update;
    - rc=1 (or any other nonzero rc) → the check itself failed;
    - rc=0 with unrecognized output → error (fail closed).

    The output parser is the primary signal, so the message-driven classic
    behavior and the exit-code convention both normalize correctly.
    """

    name = "opnsense"
    display_name = "OPNsense"
    apply_hint = "OPNsense GUI → System → Firmware → Status"
    check_command = (
        "LC_ALL=C opnsense-version 2>&1"
        " && printf '\\n@@MANUAL_UPDATE_SEPARATOR@@\\n'"
        " && LC_ALL=C opnsense-update -c 2>&1"
    )
    forbidden_tokens = ("apt", "apply", "upgrade", "pkg", "jq", "updater")

    _AVAILABLE_MARKER = "A newer version is available"
    _NO_UPDATE_MARKERS = ("Nothing to do.", "Currently up to date.")
    _RC_UPDATE_AVAILABLE = 2

    def _validate_required_commands(self) -> None:
        if "opnsense-version" not in self.check_command:
            raise ManualUpdateAdapterError("opnsense: expected 'opnsense-version' in check command")
        # opnsense-update may only ever appear in its -c (check) form.
        if re.search(r"opnsense-update(?!\s-c\b)", self.check_command):
            raise ManualUpdateAdapterError(
                "opnsense: opnsense-update may only be invoked as 'opnsense-update -c'"
            )

    def parse(self, res: PrimitiveResult, outcome: ManualUpdateResult) -> ManualUpdateResult:
        banner, check_text = _split_sections(res, self.display_name)
        current = _parse_opnsense_release(banner)
        if not current:
            raise ManualUpdateParseError(
                f"Could not determine OPNsense release from version output: {banner or '(empty)'}"
            )
        outcome.current = current
        outcome.details.append(banner)

        available = False
        if self._AVAILABLE_MARKER in check_text:
            available = True
            target = _extract_target_release(check_text)
            if target:
                outcome.latest = target
        elif any(marker in check_text for marker in self._NO_UPDATE_MARKERS):
            available = False
        elif res.rc == self._RC_UPDATE_AVAILABLE:
            # rc=2 is the opnsense-update "update available" convention; it must
            # not fall through into the generic nonzero → failure branch.
            available = True
        elif res.rc != 0:
            detail = (res.stderr or check_text or res.stdout or "no output").strip().replace("\n", " ")
            raise ManualUpdateParseError(
                f"OPNsense update check failed (rc={res.rc}): {detail[-300:]}"
            )
        else:
            raise ManualUpdateParseError(
                f"Unrecognized opnsense-update -c output (rc=0): {check_text or '(empty)'}"
            )

        if available:
            outcome.update_available = True
            if outcome.latest:
                outcome.summary = f"OPNsense update available: {current} -> {outcome.latest}"
                # OPNsense releases ship base system, kernel, and packages together.
                outcome.details.append(f"Target release: {outcome.latest}")
                outcome.details.append("Components: base system, kernel, packages")
            else:
                outcome.summary = f"OPNsense update available (current {current})"
            outcome.details.append(check_text)
        else:
            outcome.summary = f"OPNsense is up to date ({current})"
            outcome.details.append(check_text)
        return outcome


class OpnsenseApiAdapter(BaseApiManualUpdateAdapter):
    """Read-only update check for OPNsense via the REST API.

    ``GET /api/core/firmware/info`` (current version), then a fresh check is
    triggered with ``POST /api/core/firmware/check`` and ``GET
    /api/core/firmware/upgradestatus`` is polled until it finishes — since
    OPNsense ~22.7 ``GET /api/core/firmware/status`` only reports the *last*
    check, so without the trigger the monitor would go stale. Finally ``GET
    /api/core/firmware/status`` is read, authenticated with HTTP Basic
    (``api_key``:``api_secret`` — the canonical OPNsense API method; the
    X-API-Key/X-API-Secret header variant is not honored consistently on
    modern releases). Status mapping covers both the
    modern machine values and the legacy ``opnsense-update`` strings:
    ``update``/``update_available``/``update_available_major`` → update
    available (target from ``upgrade_version``/``upgrade_major_version``/
    ``status_msg`` when present), ``none``/``nothing_to_do`` → up to date,
    ``reboot_required`` → reboot flag, ``error``/``connection_failure`` →
    check error, anything else → fail closed. Read-only: the check endpoint
    only probes for updates; no update/apply endpoint is ever invoked.
    """

    name = "opnsense_api"
    display_name = "OPNsense"
    apply_hint = "OPNsense GUI → System → Firmware → Status"
    transport = "api"

    _INFO_PATH = "/api/core/firmware/info"
    _CHECK_PATH = "/api/core/firmware/check"
    _UPGRADE_STATUS_PATH = "/api/core/firmware/upgradestatus"
    _STATUS_PATH = "/api/core/firmware/status"
    _API_RETRIES = 1
    _API_RETRY_DELAY = 10.0
    # Budget for the background check to finish: 13 × 10s ≈ 2 minutes, in line
    # with the synchronous SSH check's mirror-contact time.
    _POLL_ATTEMPTS = 13
    _POLL_DELAY = 10.0

    _AVAILABLE_STATUSES = ("update", "update_available", "update_available_major")
    _NO_UPDATE_STATUSES = ("none", "nothing_to_do")
    _REBOOT_STATUS = "reboot_required"
    _CONNECTION_FAILURE_STATUS = "connection_failure"
    _ERROR_STATUS = "error"
    _IN_PROGRESS_STATUSES = ("running",)
    # Terminal upgradestatus values — the check finished (or never registered).
    _UPGRADE_TERMINAL_STATUSES = ("done", "reboot", "error")

    def _validate_api_config(self, config: Optional[ManualUpdateApiConfig]) -> None:
        super()._validate_api_config(config)
        if config is None or not config.api_secret:
            raise ManualUpdateAdapterError(
                f"{self.name}: api_secret is required for the OPNsense API transport "
                "(key/secret pair from System → Access → Users → API keys)"
            )

    def _check_api(
        self,
        outcome: ManualUpdateResult,
        host: str,
        config: Optional[ManualUpdateApiConfig],
    ) -> ManualUpdateResult:
        if config is None:
            raise ManualUpdateParseError(f"{self.name}: missing API config")
        base_url = (config.api_url or f"https://{host}").rstrip("/")
        # OPNsense's canonical API auth is HTTP Basic with key:secret (the
        # X-API-Key/X-API-Secret header variant is not honored consistently on
        # modern releases and can 200 with an empty body).
        token = base64.b64encode(f"{config.api_key}:{config.api_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        timeout = config.timeout
        verify = config.verify_ssl

        info = self._get_json(
            f"{base_url}{self._INFO_PATH}",
            headers=headers,
            retries=self._API_RETRIES,
            delay=self._API_RETRY_DELAY,
            timeout=timeout,
            verify=verify,
        )
        current = _opnsense_version_from_info(info)
        if not current:
            raise ManualUpdateParseError(
                f"Could not determine OPNsense release from firmware info: "
                f"{_body_excerpt(str(info))}"
            )
        outcome.current = current
        outcome.details.append(f"Installed: {current}")

        # Since OPNsense ~22.7 the status endpoint only reports the last
        # check's result — trigger a fresh background check and wait for it so
        # the scan never reports stale state.
        check_resp = self._post_json(
            f"{base_url}{self._CHECK_PATH}",
            {},
            headers=headers,
            retries=self._API_RETRIES,
            delay=self._API_RETRY_DELAY,
            timeout=timeout,
            verify=verify,
        )
        if check_resp.status not in (200, 201, 202, 204):
            raise ManualUpdateParseError(
                f"OPNsense update check could not be started (HTTP {check_resp.status}): "
                f"{_body_excerpt(check_resp.body)}"
            )
        self._wait_for_check(base_url, headers, timeout, verify)

        status = self._get_json(
            f"{base_url}{self._STATUS_PATH}",
            headers=headers,
            retries=self._API_RETRIES,
            delay=self._API_RETRY_DELAY,
            timeout=timeout,
            verify=verify,
        )
        if not isinstance(status, dict):
            raise ManualUpdateParseError(
                f"Unrecognized OPNsense firmware status response: "
                f"{_body_excerpt(str(status))}"
            )
        state = str(status.get("status") or "").strip()
        if not state:
            raise ManualUpdateParseError(
                "OPNsense firmware status response has no 'status' field"
            )
        if state in self._IN_PROGRESS_STATUSES:
            raise ManualUpdateParseError(
                f"OPNsense update check still running (firmware status {state!r})"
            )

        if state in self._AVAILABLE_STATUSES:
            outcome.update_available = True
            latest = _opnsense_target_from_status(status) or _opnsense_upgrade_target(status)
            outcome.latest = latest
            if latest:
                outcome.summary = f"OPNsense update available: {outcome.current} -> {latest}"
                # OPNsense releases ship base system, kernel, and packages together.
                outcome.details.append(f"Target release: {latest}")
                outcome.details.append("Components: base system, kernel, packages")
            else:
                outcome.summary = f"OPNsense update available (current {outcome.current})"
        elif state in self._NO_UPDATE_STATUSES:
            outcome.summary = f"OPNsense is up to date ({outcome.current})"
        elif state == self._REBOOT_STATUS:
            outcome.reboot_required = True
            outcome.summary = "OPNsense reboot required to complete the pending update"
        elif state in (self._ERROR_STATUS, self._CONNECTION_FAILURE_STATUS):
            status_msg = str(status.get("status_msg") or "").strip()
            detail = f": {status_msg}" if status_msg else ""
            raise ManualUpdateParseError(f"OPNsense update check failed ({state}){detail}")
        else:
            raise ManualUpdateParseError(f"Unknown OPNsense update status: {state!r}")

        # Modern OPNsense flags a pending reboot in the status payload
        # (``status_reboot``/``needs_reboot`` == "1") even when an update is
        # available — surface it rather than waiting for a separate scan.
        if str(status.get("status_reboot") or status.get("needs_reboot") or "").strip() in ("1", "true"):
            outcome.reboot_required = True
            if outcome.update_available:
                outcome.summary += "; reboot required to complete"
            else:
                outcome.summary = "OPNsense reboot required to complete the pending update"

        status_msg = str(status.get("status_msg") or "").strip()
        if status_msg and status_msg not in outcome.details:
            outcome.details.append(status_msg)
        outcome.details.append(f"Firmware check status: {state}")
        return outcome

    def _wait_for_check(
        self, base_url: str, headers: Dict[str, str], timeout: float, verify: bool
    ) -> None:
        """Block until the background firmware check finishes, or raise.

        Polls ``/api/core/firmware/upgradestatus`` until it reports a terminal
        state (``done``/``reboot``/``error``). A 404 means no task is registered
        (the check already finished). An empty/non-JSON body — a 200-with-nothing
        race right after the check starts, before the configd task registers —
        carries no progress info, so the loop keeps polling rather than reading
        a stale status. Exhausting the budget fails closed.
        """
        state = ""
        for attempt in range(self._POLL_ATTEMPTS):
            try:
                progress = self._get_json(
                    f"{base_url}{self._UPGRADE_STATUS_PATH}",
                    headers=headers,
                    retries=self._API_RETRIES,
                    delay=self._API_RETRY_DELAY,
                    timeout=timeout,
                    verify=verify,
                )
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                return
            except ManualUpdateParseError:
                progress = None
            if isinstance(progress, dict):
                state = str(progress.get("status") or "").strip()
                if state in self._UPGRADE_TERMINAL_STATUSES:
                    return
            else:
                state = ""
            # "running" or no progress info yet → keep polling (bounded); an
            # unknown value is left for the status endpoint to fail closed on.
            if state in self._IN_PROGRESS_STATUSES or not state:
                if attempt < self._POLL_ATTEMPTS - 1:
                    time.sleep(self._POLL_DELAY)
                continue
            return
        raise ManualUpdateParseError(
            f"OPNsense update check still running after "
            f"{self._POLL_ATTEMPTS * self._POLL_DELAY:.0f}s (upgradestatus {state!r})"
        )


def _split_sections(res: PrimitiveResult, label: str) -> Tuple[str, str]:
    """Split the sentinel-delimited stdout into (version_section, check_section)."""
    parts = res.stdout.split(_SEPARATOR)
    if len(parts) < 2:
        raise ManualUpdateParseError(
            f"{label} check output is missing its check section (rc={res.rc})"
        )
    return parts[0].strip(), parts[1].strip()


def _truenas_version(value: str) -> str:
    """Return the release token from TrueNAS' raw or JSON-string version."""
    text = value.strip()
    if text.startswith('"'):
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = text
        if isinstance(decoded, str):
            text = decoded.strip()
    match = re.fullmatch(r"TrueNAS(?:-SCALE)?-(.+)", text, re.IGNORECASE)
    return match.group(1) if match else text


def _component_version(item: Any) -> str:
    """Normalize a TrueNAS component version (old/new may be str or dict)."""
    if isinstance(item, str):
        return _truenas_version(item)
    if isinstance(item, dict):
        for key in ("version", "id", "name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return _truenas_version(value)
    return ""


def _first_component(changes: List[Any], key: str) -> str:
    """First non-empty version under *key* across the changes list."""
    for change in changes:
        if isinstance(change, dict):
            value = _component_version(change.get(key))
            if value:
                return value
    return ""


def _append_changes(outcome: ManualUpdateResult, changes: List[Any]) -> None:
    """Add concise per-component change lines to the result's details."""
    for change in changes:
        if not isinstance(change, dict):
            continue
        operation = str(change.get("operation", "")).strip().lower()
        old = _component_version(change.get("old"))
        new = _component_version(change.get("new"))
        if operation and old and new:
            outcome.details.append(f"{operation}: {old} -> {new}")
        elif operation and new:
            outcome.details.append(f"{operation}: {new}")


def _append_notice(outcome: ManualUpdateResult, payload: Dict[str, Any]) -> None:
    """Surface the TrueNAS notice/notes field when present (key varies by version)."""
    notice = payload.get("notice") or payload.get("notes")
    if isinstance(notice, str) and notice.strip():
        outcome.details.append(notice.strip())


_BANNER_RELEASE_RE = re.compile(r"\bOPNsense\s+([0-9][0-9A-Za-z._-]*)")


def _parse_opnsense_release(banner: str) -> str:
    """Extract the release token from the ``opnsense-version`` banner."""
    match = _BANNER_RELEASE_RE.search(banner)
    return match.group(1) if match else ""


_TARGET_RELEASE_RE = re.compile(
    r"A newer version is available:?\s*(?:OPNsense\s+)?([0-9][0-9A-Za-z._-]*)"
)


def _extract_target_release(check_text: str) -> str:
    """Extract the advertised target release from the check output, if present."""
    match = _TARGET_RELEASE_RE.search(check_text)
    return match.group(1) if match else ""


_RELEASE_TOKEN_RE = re.compile(r"^([0-9][0-9A-Za-z._-]*)")


def _release_token(text: str) -> str:
    """Leading release token of a bare version string ("24.1.10_3" → "24.1.10_3")."""
    match = _RELEASE_TOKEN_RE.match(text.strip())
    return match.group(1) if match else ""


def _opnsense_version_from_info(info: Any) -> str:
    """Extract the release token from the firmware/info response (defensive)."""
    candidates: List[str] = []
    if isinstance(info, dict):
        version = info.get("version")
        if isinstance(version, str) and version.strip():
            candidates.append(version)
        product = info.get("product")
        if isinstance(product, dict):
            for key in ("product_version", "version"):
                value = product.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value)
    for candidate in candidates:
        token = _parse_opnsense_release(f"OPNsense {candidate}") or _release_token(candidate)
        if token:
            return token
    return ""


def _opnsense_upgrade_target(status: Dict[str, Any]) -> str:
    """Target release from the firmware/status response, if advertised."""
    for key in ("upgrade_version", "upgrade_major_version"):
        value = status.get(key)
        if isinstance(value, str) and value.strip():
            token = _release_token(value)
            if token:
                return token
    # Modern OPNsense advertises the release inside status_msg, e.g.
    # "A new version of OPNsense 25.1.6 is available." — scan it defensively.
    message = status.get("status_msg")
    if isinstance(message, str):
        token = _parse_opnsense_release(message)
        if token:
            return token
    return ""


def _opnsense_target_from_status(status: Dict[str, Any]) -> str:
    """Target release from the modern status payload (OPNsense ~23+).

    The modern response carries the target under ``all_packages.opnsense.new``
    (or ``upgrade_packages``' opnsense entry's ``new_version``) and
    ``product_latest`` — checked before falling back to the legacy fields.
    """
    for container_key in ("all_packages", "upgrade_packages"):
        container = status.get(container_key)
        if isinstance(container, dict):
            entry = container.get("opnsense")
            if isinstance(entry, dict):
                for key in ("new", "new_version"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        token = _release_token(value)
                        if token:
                            return token
        elif isinstance(container, list):
            for entry in container:
                if isinstance(entry, dict) and entry.get("name") == "opnsense":
                    for key in ("new_version", "new"):
                        value = entry.get(key)
                        if isinstance(value, str) and value.strip():
                            token = _release_token(value)
                            if token:
                                return token
    latest = status.get("product_latest")
    if isinstance(latest, str) and latest.strip():
        token = _release_token(latest)
        if token:
            return token
    return ""


class ManualUpdateRegistry:
    """Name → adapter registry; the only entry point for adapter lookup."""

    def __init__(self) -> None:
        self._adapters: Dict[str, ManualUpdateAdapter] = {}

    def register(self, adapter: ManualUpdateAdapter) -> None:
        if not adapter.name:
            raise ManualUpdateAdapterError("cannot register a manual update adapter without a name")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ManualUpdateAdapter:
        if name not in self._adapters:
            available = ", ".join(self.names()) or "none"
            raise UnknownManualUpdateAdapterError(
                f"Unknown manual update adapter: {name!r} (registered: {available})"
            ) from None
        return self._adapters[name]

    def names(self) -> List[str]:
        return sorted(self._adapters)

    def __contains__(self, name: object) -> bool:
        return name in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)


# The default registry ships the two appliance adapters in both transports:
# SSH (midclt / opnsense-update -c) and REST API (truenas_scale_api /
# opnsense_api). Hosts pick one per entry via manual_adapter=.
MANUAL_UPDATE_REGISTRY = ManualUpdateRegistry()
MANUAL_UPDATE_REGISTRY.register(TrueNASScaleAdapter())
MANUAL_UPDATE_REGISTRY.register(OpnsenseAdapter())
MANUAL_UPDATE_REGISTRY.register(TrueNASScaleApiAdapter())
MANUAL_UPDATE_REGISTRY.register(OpnsenseApiAdapter())


def run_manual_update_check(
    adapter_name: str,
    executor: Optional[Executor] = None,
    host: str = "",
    *,
    config: Optional[ManualUpdateApiConfig] = None,
    registry: ManualUpdateRegistry = MANUAL_UPDATE_REGISTRY,
) -> ManualUpdateResult:
    """Run the named adapter's read-only update check against *host*.

    SSH-transport adapters use *executor*; API-transport adapters use *config*
    instead and ignore the executor. Never raises and never returns a partial
    shape: unknown adapter names and validation failures produce an error
    result *before* any host contact, unreachable hosts are normalized to
    ``unreachable=True`` without invoking the output parser, and genuine
    command/parser failures are reported with ``unreachable=False``.
    """
    try:
        adapter = registry.get(adapter_name)
    except UnknownManualUpdateAdapterError as exc:
        return ManualUpdateResult(
            host=host,
            display_name=adapter_name,
            adapter=adapter_name,
            error=str(exc),
        )

    try:
        adapter.validate(config)
    except ManualUpdateAdapterError as exc:
        return ManualUpdateResult(
            host=host,
            display_name=adapter.display_name,
            adapter=adapter.name,
            apply_hint=adapter.apply_hint,
            error=str(exc),
        )

    return adapter.check(executor, host, config=config)
