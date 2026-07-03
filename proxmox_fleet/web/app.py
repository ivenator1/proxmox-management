"""The fleet dashboard FastAPI app — server-rendered, no JS build step.

Pages: overview (``/``), pending updates (``/pending``), run history
(``/history`` + per-run detail), per-host drill-down (``/hosts/{name}``), and
the run trigger (``/trigger`` → ``POST /runs`` → live console with SSE).

Every route except ``/login``, ``/static`` and the FastAPI-Users ``/auth``
router hangs off a single auth-protected APIRouter (session cookie login) —
new pages are protected by default. All fleet data comes from
``fleet_history_dir`` via the readers in :mod:`proxmox_fleet.history` and
:mod:`proxmox_fleet.scan` — the dashboard never runs fleet operations itself;
updates go through the CLI subprocess, and the only direct host contact is
the SSH-enrollment helpers (``/ssh/push``, ``/ssh/test``).
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from proxmox_fleet import history as history_mod
from proxmox_fleet import inventory_edit, vars_edit
from proxmox_fleet import scan as scan_mod
from proxmox_fleet.inventory_edit import InventoryEditError
from proxmox_fleet.lock import probe_lock
from proxmox_fleet.models.config import MaintenanceWindow
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.vars_edit import VarsEditError
from proxmox_fleet.web import auth, sshsetup
from proxmox_fleet.web.auth import auth_backend, current_active_user, fastapi_users
from proxmox_fleet.web.runs import RunActive, RunManager
from proxmox_fleet.web.sshsetup import SshSetupError

_PACKAGE_DIR = Path(__file__).parent


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that forbids heuristic freshness caching.

    Plain StaticFiles sends ETag/Last-Modified but no Cache-Control, so
    browsers guess a freshness lifetime and keep serving stale CSS/JS after
    a deploy (missing features, broken styling) until a hard refresh.
    ``no-cache`` means "revalidate every time" — with the ETag already
    there, an unchanged file is a cheap 304, not a re-download.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

# Phase names accepted by `fleet-update --phases` (mirrors driver.PHASE_NAMES,
# which is not imported here — the driver needs ansible-runner installed).
PHASE_NAMES = ("remote", "custom", "lxc", "vm", "node", "manager")

# --limit / --phases tokens are passed to a subprocess: allow only inventory
# name / vmid shaped tokens, nothing shell-meaningful. Shared with the
# inventory editor so the two layers can never drift apart.
_TOKEN_RE = inventory_edit._NAME_RE

# Per-bucket table columns for the run-detail and host drill-down pages.
BUCKET_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "lxc": ("node", "id", "name", "os", "app"),
    "vm": ("node", "vmid", "name", "status", "pkg_count"),
    "remote": ("host", "status"),
    "node": ("node", "status"),
    "custom": ("host", "name", "app"),
    "errors": ("host", "task", "error"),
    "warnings": ("host", "task", "warning"),
}

# Record keys that identify the host a record belongs to (drill-down matching).
_HOST_KEYS = ("host", "name", "node", "id", "vmid")

_COUNT_KEYS = ("lxc", "vm", "remote", "node", "custom", "errors", "warnings")


def _parse_ts(ts: str) -> Optional[datetime]:
    """Parse a history timestamp (``%Y%m%dT%H%M%S%fZ``); None if malformed."""
    try:
        return datetime.strptime(str(ts).rstrip("Z"), "%Y%m%dT%H%M%S%f").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def ts_human(ts: str) -> str:
    """Jinja filter: ``20260102T000030000000Z`` → ``2026-01-02 00:00 UTC``.

    Display text only — links keep the raw timestamp as the URL ref."""
    dt = _parse_ts(ts)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else str(ts)


def ts_iso(ts: str) -> str:
    """Jinja filter: history timestamp → ISO-8601 (for ``data-ts`` relative
    times rendered client-side); empty string if malformed."""
    dt = _parse_ts(ts)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def ts_span(ts: str) -> Markup:
    """Jinja filter: human timestamp wrapped for client-side localization.

    dashboard.js rewrites every ``.ts[data-utc]`` span into the viewer's
    browser timezone; the server-rendered UTC text is the no-JS fallback.
    Malformed timestamps degrade to plain (escaped) text."""
    human = ts_human(ts)
    iso = ts_iso(ts)
    if not iso:
        return escape(human)
    # Markup(literal).format() escapes its arguments (bandit B704-clean)
    return Markup('<span class="ts" data-utc="{}">{}</span>').format(iso, human)


def spark_points(values: Sequence[Any], w: int = 120, h: int = 28,
                 lo: Optional[float] = None, hi: Optional[float] = None) -> str:
    """Jinja filter: numeric series (oldest→newest) → SVG polyline ``points``.

    *lo*/*hi* pin the Y scale so several series can share one axis (the
    combined trend chart); left unset, the series scales to its own range.
    """
    pad = 2.0
    try:
        vals = [float(v or 0) for v in values]
    except (TypeError, ValueError):
        return ""
    if not vals:
        return ""
    if len(vals) == 1:
        vals = vals * 2
    top = float(hi) if hi is not None else max(vals)
    bot = float(lo) if lo is not None else min(vals)
    span = (top - bot) or 1.0
    step = (w - 2 * pad) / (len(vals) - 1)
    return " ".join(
        f"{pad + i * step:.1f},{pad + (top - v) / span * (h - 2 * pad):.1f}"
        for i, v in enumerate(vals)
    )


def _endpoint_counts(inventory_path: str,
                     latest_pending: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Currently known update endpoints for the overview card.

    VM/PVE-host/remote/custom counts come from the inventory groups (what the
    fleet is configured to update); LXCs have no inventory entry — they are
    tag-discovered — so their count comes from the latest pending scan.
    """
    groups = inventory_edit.list_hosts(inventory_path)
    return [
        {"label": "LXCs", "value": len((latest_pending or {}).get("lxc") or {})},
        {"label": "VMs", "value": len(groups.get("proxmox_vms") or [])},
        {"label": "PVE hosts", "value": len(groups.get("proxmox_nodes") or [])},
        {"label": "remote hosts", "value": len(groups.get("remote_hosts") or [])},
        {"label": "custom systems", "value": len(groups.get("custom_hosts") or [])},
    ]


def _health_score(latest_run: Optional[Mapping[str, Any]],
                  pending_row: Optional[Mapping[str, Any]]) -> int:
    """Fleet health 0–100 for the overview gauge: start at 100; −25 if the
    latest run failed, −5 per error, −2 per warning, −1 per outdated app."""
    score = 100
    if latest_run:
        counts = latest_run.get("counts") or {}
        if latest_run.get("failed"):
            score -= 25
        score -= 5 * int(counts.get("errors", 0) or 0)
        score -= 2 * int(counts.get("warnings", 0) or 0)
    if pending_row:
        score -= int(pending_row.get("outdated_apps", 0) or 0)
    return max(0, min(100, score))


def _activity_weeks(rows: Sequence[Mapping[str, Any]], weeks: int = 17,
                    today: Optional[date] = None) -> List[List[Dict[str, Any]]]:
    """GitHub-style activity grid from history rows: *weeks* columns of 7 day
    cells (Monday-first, oldest column first, last column contains today).
    Each cell: date / count / failed / level (0–3) / future / month label."""
    today = today or datetime.now(timezone.utc).date()
    per_day: Dict[date, Dict[str, Any]] = {}
    for row in rows:
        dt = _parse_ts(str(row.get("timestamp", "")))
        if dt is None:
            continue
        ent = per_day.setdefault(dt.date(), {"count": 0, "failed": False})
        ent["count"] += 1
        ent["failed"] = ent["failed"] or bool(row.get("failed"))

    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(weeks=weeks - 1)
    grid: List[List[Dict[str, Any]]] = []
    prev_month = ""
    for w in range(weeks):
        week_start = start + timedelta(weeks=w)
        month = week_start.strftime("%b")
        col_label = month if month != prev_month else ""
        prev_month = month
        col: List[Dict[str, Any]] = []
        for d in range(7):
            day = week_start + timedelta(days=d)
            ent = per_day.get(day, {"count": 0, "failed": False})
            count = int(ent["count"])
            level = 0 if count == 0 else 1 if count == 1 else 2 if count <= 3 else 3
            col.append({
                "date": day.isoformat(),
                "count": count,
                "failed": bool(ent["failed"]),
                "level": level,
                "future": day > today,
                "month": col_label if d == 0 else "",
            })
        grid.append(col)
    return grid


# Settings-page form: fields whose names smell like credentials render as
# password inputs (blank = keep current). Over-matching (pve_api_token_id)
# only costs a masked input, never a leak.
_SECRET_FIELD_RE = re.compile(r"(token|secret|pass|webhook|deadmans)", re.IGNORECASE)
# Structured editing can't represent these faithfully — raw editor only.
_RAW_ONLY_FIELDS = {"notifiers"}


def settings_form_fields(file_data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """One row per GlobalSettings field for the /settings form: name, kind
    (bool/int/float/str/list/map), current value + its text rendering, whether
    vars.yml sets it explicitly, and the secret flag. Unknown extra keys in
    vars.yml (extra="allow") are raw-editor-only."""
    defaults = GlobalSettings()
    rows: List[Dict[str, Any]] = []
    for name in GlobalSettings.model_fields:
        if name in _RAW_ONLY_FIELDS:
            continue
        default_val = getattr(defaults, name)
        if isinstance(default_val, bool):
            kind = "bool"
        elif isinstance(default_val, int):
            kind = "int"
        elif isinstance(default_val, float):
            kind = "float"
        elif isinstance(default_val, list):
            kind = "list"
        elif isinstance(default_val, dict):
            kind = "map"
        else:
            kind = "str"
        value = file_data.get(name, default_val)
        secret = bool(_SECRET_FIELD_RE.search(name))
        if kind == "list":
            text = "\n".join(str(v) for v in (value or []))
        elif kind == "map":
            text = "\n".join(f"{k}={v}" for k, v in (value or {}).items())
        elif kind == "bool":
            text = "true" if value else "false"
        else:
            text = "" if value is None else str(value)
        rows.append({
            "name": name, "kind": kind, "value": value,
            "text": "" if secret else text,
            "set_in_file": name in file_data, "secret": secret,
        })
    return rows


def parse_settings_form(form: Mapping[str, Any],
                        file_data: Mapping[str, Any]) -> Dict[str, Any]:
    """Diff the submitted /settings form against the file: only fields whose
    parsed value differs are returned (so an untouched form writes nothing).
    Blank secret fields mean "keep current". Raises VarsEditError on bad input."""
    getlist = getattr(form, "getlist", lambda k: [form[k]] if k in form else [])
    changes: Dict[str, Any] = {}
    for row in settings_form_fields(file_data):
        name, kind = row["name"], row["kind"]
        submitted = getlist(name)
        if not submitted:
            continue
        raw = str(submitted[-1])
        if row["secret"] and raw == "":
            continue
        new: Any
        try:
            if kind == "bool":
                new = raw.strip().lower() in ("true", "1", "yes", "on")
            elif kind == "int":
                new = int(raw.strip())
            elif kind == "float":
                new = float(raw.strip())
            elif kind == "list":
                new = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            elif kind == "map":
                new = {}
                for ln in raw.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    if "=" not in ln:
                        raise ValueError(f"{name}: expected key=value, got {ln!r}")
                    k, v = ln.split("=", 1)
                    new[k.strip()] = v.strip()
            else:
                new = raw
        except ValueError as exc:
            raise VarsEditError(f"invalid value for {name}: {exc}")
        cur = row["value"]
        if kind == "list":
            if [str(v) for v in new] != [str(v) for v in (cur or [])]:
                changes[name] = new
        elif kind == "map":
            if {str(k): str(v) for k, v in new.items()} != \
                    {str(k): str(v) for k, v in (cur or {}).items()}:
                changes[name] = new
        elif kind in ("int", "float", "bool"):
            if new != cur:
                changes[name] = new
        else:
            if str(new) != ("" if cur is None else str(cur)):
                changes[name] = new
    return changes


def _truthy(value: Any) -> bool:
    return str(value or "").lower() in ("true", "1", "yes", "on")


def _csv_tokens(value: str) -> List[str]:
    """Split a comma-separated form field into validated argv-safe tokens."""
    tokens = [t.strip() for t in value.split(",") if t.strip()]
    for token in tokens:
        if not _TOKEN_RE.match(token):
            raise ValueError(f"invalid token {token!r} (letters/digits/._- only)")
    return tokens


def build_run_args(form: Mapping[str, Any]) -> List[str]:
    """Compose the `fleet-update` argv tail from validated trigger fields.

    Only structured flags are ever emitted — free-text fields (`limit`,
    `phases`) are tokenized and validated, never passed through raw.
    Raises ValueError on any invalid input.
    """
    args: List[str] = []
    scan = _truthy(form.get("scan"))
    if scan:
        args.append("--scan")
    else:
        if _truthy(form.get("dry_run")):
            args.append("--check")
        if _truthy(form.get("force_notify")):
            args += ["-e", "force_notify=true"]
        if _truthy(form.get("force_window")):
            args += ["-e", "force_window=true"]
        phases = _csv_tokens(str(form.get("phases") or ""))
        unknown = [p for p in phases if p not in PHASE_NAMES]
        if unknown:
            raise ValueError(f"unknown phase(s): {', '.join(unknown)}")
        if phases:
            args += ["--phases", ",".join(phases)]
    limit = _csv_tokens(str(form.get("limit") or ""))
    if limit:
        args += ["--limit", ",".join(limit)]
    return args



def _lxc_sort_key(item: Tuple[str, Dict[str, Any]]) -> Tuple[str, int, str]:
    key, entry = item
    return (str(entry.get("node", "")), int(key) if key.isdigit() else 0, key)


def _history_rows_with_deltas(history_dir: str) -> List[Dict[str, Any]]:
    """history_summary() rows (newest first) plus a human `delta` vs the
    previous (older) run, e.g. "errors +1, lxc -2"."""
    rows = history_mod.history_summary(history_dir, limit=0)
    for i, row in enumerate(rows):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        if prev is None:
            row["delta"] = ""
            continue
        parts = []
        for key in _COUNT_KEYS:
            diff = int((row.get("counts") or {}).get(key, 0)) - int((prev.get("counts") or {}).get(key, 0))
            if diff:
                parts.append(f"{key} {diff:+d}")
        row["delta"] = ", ".join(parts)
    return rows


def _host_records(history_dir: str, name: str) -> List[Dict[str, Any]]:
    """A host's records across all persisted runs, newest run first."""
    out: List[Dict[str, Any]] = []
    for row in history_mod.history_summary(history_dir, limit=0):
        try:
            run = history_mod.read_run(history_dir, str(row["timestamp"]))
        except (OSError, ValueError):
            continue
        for bucket, columns in BUCKET_COLUMNS.items():
            for record in run.get(bucket, []) or []:
                if any(str(record.get(k, "")) == name for k in _HOST_KEYS):
                    out.append({
                        "timestamp": row["timestamp"],
                        "bucket": bucket,
                        "record": record,
                        "columns": columns,
                    })
    return out


def create_app(
    settings: Optional[GlobalSettings] = None,
    *,
    run_manager: Optional[RunManager] = None,
    vars_path: str = "vars.yml",
    inventory_path: str = "hosts.ini",
) -> FastAPI:
    """Build the dashboard app. *run_manager* is injectable for tests;
    *vars_path*/*inventory_path* are the files the enrollment and settings
    pages edit (main.py passes its ``--vars-file``)."""
    settings = settings or GlobalSettings.load()
    manager = run_manager or RunManager(settings.fleet_history_dir)
    history_dir = settings.fleet_history_dir

    # Configure FastAPI-Users authentication
    auth.configure(auth.user_db_path(history_dir))

    app = FastAPI(title="Fleet Dashboard", docs_url=None, redoc_url=None,
                  on_startup=[auth.create_db_and_tables])

    # Mount FastAPI-Users routers for authentication
    app.include_router(
        fastapi_users.get_auth_router(auth_backend),
        prefix="/auth",
        tags=["auth"],
    )

    # Everything except /login, /static and the /auth router hangs off this
    # router, so authentication is default-on: a new page added without
    # thinking about auth is protected, not silently public.
    protected = APIRouter(dependencies=[Depends(current_active_user)])

    app.mount("/static", RevalidatingStaticFiles(directory=str(_PACKAGE_DIR / "static")),
              name="static")
    templates = Jinja2Templates(directory=str(_PACKAGE_DIR / "templates"))
    templates.env.filters["ts_human"] = ts_human
    templates.env.filters["ts_iso"] = ts_iso
    templates.env.filters["ts_span"] = ts_span
    templates.env.filters["spark_points"] = spark_points

    # Exception handler: redirect 401 (unauthenticated) to login for HTML requests
    @app.exception_handler(HTTPException)
    async def http_exception_handler_override(request: Request, exc: HTTPException):  # type: ignore
        if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url="/login", status_code=303)
        return await http_exception_handler(request, exc)

    def _read_run_or_none(ref: str) -> Optional[Dict[str, Any]]:
        try:
            return history_mod.read_run(history_dir, ref)
        except (OSError, ValueError):
            return None

    def _read_pending_or_none(ref: str) -> Optional[Dict[str, Any]]:
        try:
            return scan_mod.read_pending(history_dir, ref)
        except (OSError, ValueError):
            return None

    def _build_palette_items() -> List[Dict[str, str]]:
        """Command-palette entries (pages, hosts, recent runs), rendered into
        a JSON blob in base.html on every page."""
        items: List[Dict[str, str]] = [
            {"kind": "page", "label": "Overview", "url": "/"},
            {"kind": "page", "label": "Pending updates", "url": "/pending"},
            {"kind": "page", "label": "Run history", "url": "/history"},
            {"kind": "page", "label": "Trigger a run", "url": "/trigger"},
            {"kind": "page", "label": "Inventory & enrollment", "url": "/inventory"},
            {"kind": "page", "label": "Settings (vars.yml)", "url": "/settings"},
        ]
        seen: set = set()

        def _add_host(name: str) -> None:
            if name and name not in seen:
                seen.add(name)
                items.append({"kind": "host", "label": name, "url": f"/hosts/{name}"})

        latest = _read_run_or_none("latest")
        if latest:
            for bucket in BUCKET_COLUMNS:
                for record in latest.get(bucket, []) or []:
                    for key in ("host", "name"):
                        _add_host(str(record.get(key) or ""))
        pending_snapshot = _read_pending_or_none("latest")
        if pending_snapshot:
            for name in pending_snapshot.get("hosts") or {}:
                _add_host(str(name))
            for lxc_id, entry in (pending_snapshot.get("lxc") or {}).items():
                _add_host(str(entry.get("name") or lxc_id))
        for row in history_mod.history_summary(history_dir, limit=8):
            ts = str(row.get("timestamp", ""))
            items.append({"kind": "run", "label": f"Run {ts_human(ts)}",
                          "url": f"/history/{ts}"})
        return items

    # base.html calls palette_items() on every page render; rebuilding it
    # means re-reading latest.json + the pending snapshot + the history
    # summaries each hit, so cache it briefly (staleness is invisible at
    # human browsing speed; races just rebuild twice).
    _palette_cache: Tuple[float, List[Dict[str, str]]] = (0.0, [])
    _PALETTE_TTL = 5.0

    def _palette_items() -> List[Dict[str, str]]:
        nonlocal _palette_cache
        expires, items = _palette_cache
        if time.monotonic() >= expires:
            items = _build_palette_items()
            _palette_cache = (time.monotonic() + _PALETTE_TTL, items)
        return items

    templates.env.globals["palette_items"] = _palette_items

    @app.get("/login")
    def login_page(request: Request) -> Any:
        """Login form (FastAPI-Users will handle the actual /auth/login POST)."""
        return templates.TemplateResponse(request, "login.html", {})

    @protected.get("/")
    def index(request: Request) -> Any:
        latest_run = _read_run_or_none("latest")
        latest_pending = _read_pending_or_none("latest")
        pending_rows = scan_mod.pending_summary(history_dir, limit=1)
        pending_row = pending_rows[0] if pending_rows else None
        # newest first from history_summary; reversed → oldest first for the
        # pulse strip / sparkline (time flows left → right)
        recent = list(reversed(history_mod.history_summary(history_dir, limit=30)))
        # combined trend chart: one point per run, all three series on one
        # shared count axis (max across every series, floor 1 so a flat-zero
        # history still draws a baseline)
        trend = [
            {
                "ts": row["timestamp"],
                "label": ts_human(row["timestamp"]),
                "iso": ts_iso(row["timestamp"]),
                "os": (row.get("updates") or {}).get("os", 0),
                "app": (row.get("updates") or {}).get("app", 0),
                "err": (row.get("counts") or {}).get("errors", 0),
            }
            for row in recent
        ]
        trend_max = max(
            (max(p["os"], p["app"], p["err"]) for p in trend), default=0,
        ) or 1
        return templates.TemplateResponse(request, "index.html", {
            "latest_run": latest_run,
            "latest_pending": latest_pending,
            "pending_row": pending_row,
            "lock_holder": probe_lock(history_dir),
            "active_run": manager.active_run(),
            "endpoints": _endpoint_counts(inventory_path, latest_pending),
            "recent": recent,
            "trend": trend,
            "trend_max": trend_max,
            "totals": history_mod.read_totals(history_dir),
            "health": _health_score(latest_run, pending_row),
        })

    @protected.get("/pending")
    def pending(request: Request, ref: str = "latest") -> Any:
        snapshot = _read_pending_or_none(ref)
        if snapshot is None and ref != "latest":
            raise HTTPException(404, f"no pending snapshot {ref!r}")
        hosts = sorted((snapshot.get("hosts") or {}).items()) if snapshot else []
        lxc = sorted((snapshot.get("lxc") or {}).items(), key=_lxc_sort_key) if snapshot else []
        return templates.TemplateResponse(request, "pending.html", {
            "ref": ref,
            "snapshot": snapshot,
            "hosts": hosts,
            "lxc": lxc,
            "scans": scan_mod.pending_summary(history_dir, limit=0),
        })

    @protected.get("/history")
    def history(request: Request) -> Any:
        rows = _history_rows_with_deltas(history_dir)
        return templates.TemplateResponse(request, "history.html", {
            "rows": rows,
            "count_keys": _COUNT_KEYS,
            "heatmap": _activity_weeks(rows),
        })

    @protected.get("/history/{ref}")
    def history_show(request: Request, ref: str) -> Any:
        run = _read_run_or_none(ref)
        if run is None:
            raise HTTPException(404, f"no history record {ref!r}")
        buckets = [
            (bucket, columns, run.get(bucket, []) or [])
            for bucket, columns in BUCKET_COLUMNS.items()
        ]
        return templates.TemplateResponse(request, "run_detail.html", {
            "ref": ref,
            "run": run,
            "buckets": buckets,
        })

    @protected.get("/hosts/{name}")
    def host_detail(request: Request, name: str) -> Any:
        return templates.TemplateResponse(request, "host.html", {
            "name": name,
            "records": _host_records(history_dir, name),
        })

    @protected.get("/trigger")
    def trigger(request: Request) -> Any:
        return templates.TemplateResponse(request, "trigger.html", {
            "phase_names": PHASE_NAMES,
            "lock_holder": probe_lock(history_dir),
            "runs": manager.list_runs(),
        })

    # ----- inventory enrollment + settings + ssh setup ----------------- #

    def _redirect(url: str, *, ok: str = "", err: str = "") -> RedirectResponse:
        if ok:
            url += f"?ok={quote(ok)}"
        elif err:
            url += f"?err={quote(err)}"
        return RedirectResponse(url=url, status_code=303)

    def _inventory_context(request: Request,
                           ssh_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Context shared by GET /inventory and the ssh POSTs (which re-render
        the page with the action's output instead of redirecting)."""
        exists = Path(inventory_path).exists()
        live = GlobalSettings.load(vars_path)   # fresh — edits land on the file
        canary_ids = {str(c) for c in live.canary_hosts}
        host_vars_dir = Path(settings.host_vars_dir)
        groups: List[Dict[str, Any]] = []
        if exists:
            for group, hosts in inventory_edit.list_hosts(inventory_path).items():
                rows = []
                for host in hosts:
                    name = str(host["name"])
                    hvars: Dict[str, str] = dict(host["vars"])
                    vmid = hvars.get("vmid", "")
                    kuma = ""
                    if group == "proxmox_vms":
                        kuma = str(live.vm_kuma_map.get(vmid)
                                   or live.vm_kuma_map.get(name) or "")
                    elif group == "remote_hosts":
                        kuma = str(live.remote_kuma_map.get(name) or "")
                    rows.append({
                        "name": name,
                        "vars": hvars,
                        "canary": (_truthy(hvars.get("canary"))
                                   or name in canary_ids or vmid in canary_ids),
                        "kuma": kuma,
                        "has_host_vars": (host_vars_dir / f"{name}.yml").is_file(),
                    })
                groups.append({"group": group, "hosts": rows})
        return {
            "inventory_exists": exists,
            "inventory_path": inventory_path,
            "groups": groups,
            "lxc_lists": {
                "canary_hosts": live.canary_hosts,
                "os_only_lxc_list": live.os_only_lxc_list,
                "exclude_list": live.exclude_list,
                "app_update_exclude_list": live.app_update_exclude_list,
                "lxc_kuma_map": live.lxc_kuma_map,
            },
            "public_keys": sshsetup.list_public_keys(),
            "ssh_result": ssh_result,
            "group_names": inventory_edit.GROUPS,
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        }

    @protected.get("/inventory")
    def inventory_page(request: Request) -> Any:
        return templates.TemplateResponse(
            request, "inventory.html", _inventory_context(request))

    @protected.post("/inventory/create")
    async def inventory_create(request: Request) -> Any:
        created = inventory_edit.ensure_inventory(inventory_path)
        return _redirect("/inventory",
                         ok="hosts.ini created" if created else "hosts.ini already exists")

    @protected.post("/inventory/add")
    async def inventory_add(request: Request) -> Any:
        form = await request.form()
        group = str(form.get("group") or "")
        name = str(form.get("name") or "").strip()
        ansible_host = str(form.get("ansible_host") or "").strip()
        try:
            if group not in inventory_edit.GROUPS:
                raise InventoryEditError(f"unknown group {group!r}")
            inline: Dict[str, str] = {}
            if ansible_host:
                inline["ansible_host"] = ansible_host
            elif group != "proxmox_nodes":   # nodes fall back to their name
                raise InventoryEditError("ansible_host is required")
            if group == "proxmox_vms":
                vmid = str(form.get("vmid") or "").strip()
                pve_node = str(form.get("pve_node") or "").strip()
                if not vmid.isdigit():
                    raise InventoryEditError("vmid is required (digits)")
                if not pve_node:
                    raise InventoryEditError("pve_node is required")
                inline["vmid"], inline["pve_node"] = vmid, pve_node
            if group == "custom_hosts":
                custom_config = str(form.get("custom_config") or "").strip()
                if not custom_config:
                    raise InventoryEditError("custom_config is required")
                inline["custom_config"] = custom_config
            if _truthy(form.get("canary")) and group in ("proxmox_vms", "remote_hosts"):
                inline["canary"] = "true"

            inventory_edit.ensure_inventory(inventory_path)
            inventory_edit.add_host(inventory_path, group, name, inline)

            # vars.yml extras: Uptime-Kuma monitor mapping for this host
            kuma_id = str(form.get("kuma_id") or "").strip()
            if kuma_id:
                if not _TOKEN_RE.match(kuma_id):
                    raise VarsEditError(f"invalid kuma monitor id {kuma_id!r}")
                if group == "proxmox_vms":
                    vars_edit.apply_changes(
                        vars_path, map_set={"vm_kuma_map": {inline["vmid"]: kuma_id}})
                elif group == "remote_hosts":
                    vars_edit.apply_changes(
                        vars_path, map_set={"remote_kuma_map": {name: kuma_id}})

            # host_vars/<name>.yml: pre_update_cmd + maintenance window
            host_vars: Dict[str, Any] = {}
            pre_cmd = str(form.get("pre_update_cmd") or "").strip()
            if pre_cmd and group == "remote_hosts":
                host_vars["pre_update_cmd"] = pre_cmd
            mw_fields = {k: str(form.get(f"mw_{k}") or "").strip()
                         for k in ("days", "start", "end", "tz")}
            if any(mw_fields.values()) and group != "proxmox_nodes":
                mw: Dict[str, Any] = {}
                days = [d.strip() for d in mw_fields["days"].split(",") if d.strip()]
                if days:
                    mw["days"] = days
                for k in ("start", "end", "tz"):
                    if mw_fields[k]:
                        mw[k] = mw_fields[k]
                MaintenanceWindow(**mw)   # fail loud before writing
                host_vars["maintenance_window"] = mw
            if host_vars:
                vars_edit.host_vars_write(settings.host_vars_dir, name, host_vars)
        except (InventoryEditError, VarsEditError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return _redirect("/inventory", ok=f"enrolled {name} in [{group}]")

    @protected.post("/inventory/remove")
    async def inventory_remove(request: Request) -> Any:
        form = await request.form()
        group = str(form.get("group") or "")
        name = str(form.get("name") or "").strip()
        cleanup = _truthy(form.get("cleanup"))
        try:
            vmid = ""
            for host in inventory_edit.list_hosts(inventory_path).get(group, []):
                if host["name"] == name:
                    vmid = dict(host["vars"]).get("vmid", "")
                    break
            inventory_edit.remove_host(inventory_path, group, name)
            if cleanup:
                ids = [name] + ([vmid] if vmid else [])
                vars_edit.apply_changes(
                    vars_path,
                    list_remove={key: ids for key in (
                        "canary_hosts", "os_only_lxc_list", "exclude_list",
                        "os_update_exclude_list", "app_update_exclude_list",
                        "snapshot_exclude_list")},
                    map_remove={key: ids for key in (
                        "lxc_kuma_map", "vm_kuma_map", "remote_kuma_map")},
                )
                vars_edit.host_vars_delete(settings.host_vars_dir, name)
        except (InventoryEditError, VarsEditError) as exc:
            raise HTTPException(400, str(exc))
        suffix = " (vars.yml + host_vars cleaned)" if cleanup else ""
        return _redirect("/inventory", ok=f"removed {name} from [{group}]{suffix}")

    @protected.get("/settings")
    def settings_page(request: Request) -> Any:
        file_data = dict(vars_edit.load_data(vars_path))
        known = set(GlobalSettings.model_fields)
        return templates.TemplateResponse(request, "settings.html", {
            "fields": settings_form_fields(file_data),
            "raw": vars_edit.read_raw(vars_path),
            "vars_path": vars_path,
            "extra_keys": sorted(k for k in file_data if k not in known),
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        })

    @protected.post("/settings")
    async def settings_save(request: Request) -> Any:
        form = await request.form()
        file_data = dict(vars_edit.load_data(vars_path))
        try:
            changes = parse_settings_form(form, file_data)
            if changes:
                vars_edit.apply_changes(vars_path, set_keys=changes)
        except VarsEditError as exc:
            raise HTTPException(400, str(exc))
        msg = (f"updated {len(changes)} setting(s): " + ", ".join(sorted(changes))
               if changes else "no changes")
        return _redirect("/settings", ok=msg)

    @protected.post("/settings/raw")
    async def settings_save_raw(request: Request) -> Any:
        form = await request.form()
        try:
            vars_edit.replace_raw(vars_path, str(form.get("content") or ""))
        except VarsEditError as exc:
            raise HTTPException(400, str(exc))
        return _redirect("/settings", ok=f"{vars_path} replaced (backup written)")

    def _ssh_action(request: Request, action: str,
                    call: Callable[[], Tuple[bool, str]]) -> Any:
        """Shared tail of the three ssh POSTs: run the action, map bad input
        to 400, re-render the inventory page with the action's output."""
        try:
            ok, output = call()
        except SshSetupError as exc:
            raise HTTPException(400, str(exc))
        return templates.TemplateResponse(request, "inventory.html", _inventory_context(
            request, ssh_result={"action": action, "ok": ok, "output": output}))

    @protected.post("/ssh/generate")
    async def ssh_generate(request: Request) -> Any:
        return _ssh_action(request, "generate", sshsetup.generate_key)

    @protected.post("/ssh/push")
    async def ssh_push(request: Request) -> Any:
        form = await request.form()
        return _ssh_action(request, "push", lambda: sshsetup.push_key(
            str(form.get("host") or ""), str(form.get("user") or "root"),
            str(form.get("password") or ""), port=form.get("port") or 22,
            pubkey_path=str(form.get("pubkey") or sshsetup.DEFAULT_KEY + ".pub"),
        ))

    @protected.post("/ssh/test")
    async def ssh_test(request: Request) -> Any:
        form = await request.form()
        return _ssh_action(request, "test", lambda: sshsetup.test_key(
            str(form.get("host") or ""), str(form.get("user") or "root"),
            port=form.get("port") or 22,
        ))

    @protected.post("/runs")
    async def start_run(request: Request) -> Any:
        form = await request.form()
        try:
            args = build_run_args(form)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            run_id = manager.start(args)
        except RunActive as exc:
            raise HTTPException(409, str(exc))
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @protected.get("/runs/{run_id}")
    def run_console(request: Request, run_id: str) -> Any:
        try:
            meta = manager.read_meta(run_id)
        except (OSError, ValueError):
            raise HTTPException(404, f"no dashboard run {run_id!r}")
        return templates.TemplateResponse(request, "console.html", {"run": meta})

    @protected.get("/runs/{run_id}/log")
    def run_log(run_id: str) -> Any:
        try:
            manager.read_meta(run_id)
        except (OSError, ValueError):
            raise HTTPException(404, f"no dashboard run {run_id!r}")
        return PlainTextResponse(manager.read_log(run_id))

    @protected.get("/runs/{run_id}/stream")
    async def run_stream(run_id: str) -> Any:
        try:
            manager.read_meta(run_id)
        except (OSError, ValueError):
            raise HTTPException(404, f"no dashboard run {run_id!r}")

        # async tail: a sync generator here would pin one threadpool worker
        # per open console tab for the whole run.
        async def _sse() -> AsyncIterator[str]:
            async for event in manager.astream(run_id):
                if event["event"] == "done":
                    yield f"event: done\ndata: {event['data']}\n\n"
                else:
                    yield f"data: {event['data']}\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    app.include_router(protected)
    return app
