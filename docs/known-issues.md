# Known issues

Small, deliberate rough edges — each is understood, judged non-urgent, and
recorded here so it is not rediscovered from scratch. Anything actively harmful
belongs in a fix, not in this file.

Resolved entries are kept at the bottom rather than deleted, so a reader who
remembers the symptom can find out what happened to it.

---

*(No open entries.)*

---

## Resolved

### `run_fleet_scan`'s error path omitted the health keys

`scan_lxc()` returned `disk_percent` / `os` / `os_mismatch`, but the stand-in
dict `run_fleet_scan()` built when a container's scan raised did not carry them.
Nothing broke — every consumer read the keys defensively — but a container whose
scan *errored* rendered identically to one scanned and found healthy: blank Disk
and OS cells, no contribution to the counters. "We could not tell" and "nothing
to report" looked the same.

The keys had been added to `scan_lxc()` without a matching update to the
fallback, and a later merge preserved the asymmetry.

**Fixed** by `scan._empty_lxc_entry(node, lxc_id)`, the single constructor both
`scan_lxc()` and every `run_fleet_scan()` fallback now start from, so a key
added in one place cannot reach only the other. `test_scan.py::
test_empty_lxc_entry_has_the_same_shape_as_a_real_scan` compares the two key
sets directly and fails if they drift again.
