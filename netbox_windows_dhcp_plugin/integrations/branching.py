"""
Optional integration with netbox-branching and netbox-change-control.

Everything that imports those plugins lives here, so the plugin keeps working when they are not installed.
"""

import logging

from django.apps import apps

log = logging.getLogger(__name__)

# Bookkeeping models that must always live on main (see models/sync.py).
NON_BRANCHABLE = {"windowsserver", "syncstate", "syncrun", "importproposal", "importproposalitem"}


def branching_installed():
    return apps.is_installed("netbox_branching")


def change_control_installed():
    return apps.is_installed("netbox_change_control")


def _resolver(model):
    if model._meta.app_label == "netbox_windows_dhcp_plugin" and model._meta.model_name in NON_BRANCHABLE:
        return False
    return None


def register():
    if not branching_installed():
        return
    try:
        from netbox_branching.utilities import register_branching_resolver
    except ImportError:
        # A netbox-branching release without this hook: start anyway rather than take NetBox down.
        log.warning("netbox-branching has no register_branching_resolver; Windows DHCP bookkeeping will be branched")
        return
    register_branching_resolver(_resolver)
