"""The fleet dashboard FastAPI app — server-rendered, no JS build step.

Pages: overview (``/``), pending updates (``/pending``), run history
(``/history`` + per-run detail), per-host drill-down (``/hosts/{name}``), and
the run trigger (``/trigger`` → ``POST /runs`` → live console with SSE).

Read-only pages are open on the LAN; ``POST /runs`` requires the
``dashboard_token`` bearer token when one is configured. All data comes from
``fleet_history_dir`` via the readers in :mod:`proxmox_fleet.history` and
:mod:`proxmox_fleet.scan` — the dashboard never reaches the fleet itself; it
only launches the CLI.
"""

from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from proxmox_fleet import history as history_mod
from proxmox_fleet import scan as scan_mod
from proxmox_fleet.lock import probe_lock
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.web.runs import RunActive, RunManager

_PACKAGE_DIR = Path(__file__).parent

# Phase names accepted by `fleet-update --phases` (mirrors driver.PHASE_NAMES,
# which is not imported here — the driver needs ansible-runner installed).
PHASE_NAMES = ("remote", "custom", "lxc", "vm", "node", "manager")

# --limit / --phases tokens are passed to a subprocess: allow only inventory
# name / vmid shaped tokens, nothing shell-meaningful.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")

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


def spark_points(values: Sequence[Any], w: int = 120, h: int = 28) -> str:
    """Jinja filter: numeric series (oldest→newest) → SVG polyline ``points``."""
    pad = 2.0
    try:
        vals = [float(v or 0) for v in values]
    except (TypeError, ValueError):
        return ""
    if not vals:
        return ""
    if len(vals) == 1:
        vals = vals * 2
    top, bot = max(vals), min(vals)
    span = (top - bot) or 1.0
    step = (w - 2 * pad) / (len(vals) - 1)
    return " ".join(
        f"{pad + i * step:.1f},{pad + (top - v) / span * (h - 2 * pad):.1f}"
        for i, v in enumerate(vals)
    )


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


def _token_ok(settings: GlobalSettings, request: Request, form: Mapping[str, Any]) -> bool:
    """Bearer-token check for the run trigger. Empty configured token = open
    (the settings field documents this); accepts the Authorization header or
    a `token` form field (HTML forms can't set headers without JS)."""
    token = settings.dashboard_token
    if not token:
        return True
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and secrets.compare_digest(header[7:], token):
        return True
    supplied = str(form.get("token") or "")
    return bool(supplied) and secrets.compare_digest(supplied, token)


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
) -> FastAPI:
    """Build the dashboard app. *run_manager* is injectable for tests."""
    settings = settings or GlobalSettings.load()
    manager = run_manager or RunManager(settings.fleet_history_dir)
    history_dir = settings.fleet_history_dir

    app = FastAPI(title="Fleet Dashboard", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_PACKAGE_DIR / "static")), name="static")
    templates = Jinja2Templates(directory=str(_PACKAGE_DIR / "templates"))
    templates.env.filters["ts_human"] = ts_human
    templates.env.filters["ts_iso"] = ts_iso
    templates.env.filters["spark_points"] = spark_points

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

    def _palette_items() -> List[Dict[str, str]]:
        """Command-palette entries (pages, hosts, recent runs), rendered into
        a JSON blob in base.html on every page. Called at render time so it
        always reflects the current history dir."""
        items: List[Dict[str, str]] = [
            {"kind": "page", "label": "Overview", "url": "/"},
            {"kind": "page", "label": "Pending updates", "url": "/pending"},
            {"kind": "page", "label": "Run history", "url": "/history"},
            {"kind": "page", "label": "Trigger a run", "url": "/trigger"},
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

    templates.env.globals["palette_items"] = _palette_items

    @app.get("/")
    def index(request: Request) -> Any:
        latest_run = _read_run_or_none("latest")
        latest_pending = _read_pending_or_none("latest")
        pending_rows = scan_mod.pending_summary(history_dir, limit=1)
        pending_row = pending_rows[0] if pending_rows else None
        # newest first from history_summary; reversed → oldest first for the
        # pulse strip / sparkline (time flows left → right)
        recent = list(reversed(history_mod.history_summary(history_dir, limit=30)))
        return templates.TemplateResponse(request, "index.html", {
            "latest_run": latest_run,
            "latest_pending": latest_pending,
            "pending_row": pending_row,
            "lock_holder": probe_lock(history_dir),
            "active_run": manager.active_run(),
            "count_keys": _COUNT_KEYS,
            "recent": recent,
            "health": _health_score(latest_run, pending_row),
        })

    @app.get("/pending")
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

    @app.get("/history")
    def history(request: Request) -> Any:
        rows = _history_rows_with_deltas(history_dir)
        return templates.TemplateResponse(request, "history.html", {
            "rows": rows,
            "count_keys": _COUNT_KEYS,
            "heatmap": _activity_weeks(rows),
        })

    @app.get("/history/{ref}")
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

    @app.get("/hosts/{name}")
    def host_detail(request: Request, name: str) -> Any:
        return templates.TemplateResponse(request, "host.html", {
            "name": name,
            "records": _host_records(history_dir, name),
        })

    @app.get("/trigger")
    def trigger(request: Request) -> Any:
        return templates.TemplateResponse(request, "trigger.html", {
            "phase_names": PHASE_NAMES,
            "token_required": bool(settings.dashboard_token),
            "lock_holder": probe_lock(history_dir),
            "runs": manager.list_runs(),
        })

    @app.post("/runs")
    async def start_run(request: Request) -> Any:
        form = await request.form()
        if not _token_ok(settings, request, form):
            raise HTTPException(401, "missing or invalid dashboard token")
        try:
            args = build_run_args(form)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            run_id = manager.start(args)
        except RunActive as exc:
            raise HTTPException(409, str(exc))
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}")
    def run_console(request: Request, run_id: str) -> Any:
        try:
            meta = manager.read_meta(run_id)
        except (OSError, ValueError):
            raise HTTPException(404, f"no dashboard run {run_id!r}")
        return templates.TemplateResponse(request, "console.html", {"run": meta})

    @app.get("/runs/{run_id}/log")
    def run_log(run_id: str) -> Any:
        try:
            manager.read_meta(run_id)
        except (OSError, ValueError):
            raise HTTPException(404, f"no dashboard run {run_id!r}")
        return PlainTextResponse(manager.read_log(run_id))

    @app.get("/runs/{run_id}/stream")
    def run_stream(run_id: str) -> Any:
        try:
            manager.read_meta(run_id)
        except (OSError, ValueError):
            raise HTTPException(404, f"no dashboard run {run_id!r}")

        def _sse() -> Iterator[str]:
            for event in manager.stream(run_id):
                if event["event"] == "done":
                    yield f"event: done\ndata: {event['data']}\n\n"
                else:
                    yield f"data: {event['data']}\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    return app
