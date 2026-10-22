"""
Live leases of a scope, read from Windows in a job (never in a request) and kept in the job's data.

Nothing is cached in a model: the scope page shows the last read, with the time it was taken, and a button to read
again.
"""

import ipaddress

from .models import WindowsServer

__all__ = ("LEASE_LIMIT", "rows", "scope_of", "windows_server_for")

# The most leases kept from one read. A large scope can hold tens of thousands, which would make the job's data (and
# the page showing it) heavy; the rest are counted, not kept.
LEASE_LIMIT = 5000


def windows_server_for(subnet):
    """The Windows server holding this subnet's scope (directly, or through its shared network), or None."""
    dhcp = subnet.dhcp_server if subnet.dhcp_server_id else getattr(subnet.shared_network, "dhcp_server", None)
    if dhcp is None:
        return None
    return WindowsServer.objects.filter(dhcp_server=dhcp).first()


def scope_of(subnet):
    """(family, Windows scope ID or prefix) of a subnet: the network address, as Windows names its scopes."""
    network = ipaddress.ip_network(str(subnet.prefix.prefix), strict=False)
    return network.version, str(network.network_address)


def _sort_key(row):
    try:
        address = ipaddress.ip_address(row["ip_address"])
    except ValueError:
        return (1, 0, row["ip_address"])  # unreadable or empty: last, as Windows printed them
    return (0, int(address), "")


def rows(leases, family):
    """Leases as plain dicts, in address order, at most LEASE_LIMIT of them. Returns (rows, total)."""
    out = []
    for lease in leases:
        row = {
            "ip_address": lease.ip_address,
            "client": lease.client_duid if family == 6 else lease.client_id,
            "host_name": lease.host_name,
            "lease_expiry_time": lease.lease_expiry_time,
            "description": lease.description,
        }
        if family == 6:
            # Windows reports no state for a DHCPv6 lease (issue #22): the panel shows "n/a" for it.
            row["iaid"] = lease.iaid
        else:
            row["address_state"] = lease.address_state
        out.append(row)
    out.sort(key=_sort_key)
    return out[:LEASE_LIMIT], len(out)
