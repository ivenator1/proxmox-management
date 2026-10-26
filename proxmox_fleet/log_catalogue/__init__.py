"""Packaged application-log plugin manifests.

Each ``<service-id>.yml`` file is declarative data validated by
:mod:`proxmox_fleet.models.log_plugins` and loaded through
:func:`proxmox_fleet.log_plugins.load_log_catalogue`.  App manifests declare
their reusable dependency services through ``includes``; both are packaged
with the control plane so selection works without external downloads.
"""
