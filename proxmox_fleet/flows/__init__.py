"""Per-entity control flows — the Python decomposition of the Ansible roles.

Each flow owns the sequencing, gating, and rescue/rollback decisions for one
entity (custom host, LXC, VM, …) and uses an Executor to make Ansible perform
the actual remote work.
"""
