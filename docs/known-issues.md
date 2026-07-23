# Known issues

Small, deliberate rough edges — each is understood, judged non-urgent, and
recorded here so it is not rediscovered from scratch. Anything actively harmful
belongs in a fix, not in this file.

---

## `run_fleet_scan`'s error path omits the health keys

**Where:** `proxmox_fleet/scan.py`, the fallback in `run_fleet_scan()` used when
`run_concurrent` returns no result for a container.

**What:** `scan_lxc()` returns a dict carrying `disk_percent`, `os` and
`os_mismatch` alongside `id`/`node`/`name`/`os_pending*`/`app`/`error`. When the
scan of a container raises instead, `run_fleet_scan` synthesises a stand-in dict
that has `id` but **not** the three health keys:

```python
if result is None:
    result = {"node": node_name, "id": str(lxc_id), "name": str(lxc_id),
              "skipped": None, "os_pending_count": 0, "os_pending": [],
              "app": None, "error": str(run_err)[:300]}
```

**Impact:** benign but slightly misleading. Every consumer reads the keys
defensively — `pending_summary()` uses `(c.get("disk_percent") or 0)` and
`c.get("os_mismatch")`, and `pending.html` renders `—` when `disk_percent` is
`None` — so nothing breaks. But a container whose scan *errored* is presented
identically to one that was scanned and found healthy: blank Disk and OS cells,
and no contribution to the `low_disk` / `os_mismatch` counters. "We could not
tell" and "nothing to report" look the same.

Only the erroring container is affected; the rest of the scan is unaffected, and
its `error` field is rendered, so the failure itself is visible.

**Origin:** not introduced by the multi-cluster merge (#45). The health keys were
added to `scan_lxc()` in #41 without a matching update to this fallback; #38 had
independently added `id` to both. The merge simply preserved the asymmetry.

**Fix when convenient:** give the two dicts one constructor so they cannot drift
again — e.g. a module-level `_empty_lxc_scan(node, lxc_id)` returning the full
shape, which `scan_lxc()` and this fallback both start from. That also removes
the chance of the next added key repeating this. If the distinction between
"unknown" and "nothing to report" matters on the dashboard, it needs a real
signal (`disk_percent: None` plus a rendered `error`) rather than an absent key.
