"""
Write sync changes into NetBox: netbox-plugin-dhcp objects, the core IPAM objects they need, and our own models.

Every write goes through ``save()`` one object at a time (sys4 validates in save and numbers subnets as it goes), and
``snapshot()`` is taken before each update or delete so the change log, and a branch's conflict detection, know the
previous state.
"""

import ipaddress
import re

from dcim.models import MACAddress
from django.contrib.contenttypes.models import ContentType
from ipam.models import IPAddress, IPRange, Prefix
from netbox_dhcp.models import HostReservation, Option, OptionDefinition, Pool, Subnet

from ..conf import get_setting
from ..models import (
    ExclusionRange,
    WindowsClass,
    WindowsOptionDefinition,
    WindowsOptionQualifier,
    WindowsReservation,
    WindowsScope,
)
from ..windows import normalize
from . import netbox_state, windows_state
from .diff import CREATE, DELETE

__all__ = ("ApplyError", "Writer")

# PrimaryModel.description is limited to 200 characters.
DESCRIPTION_LENGTH = 200


class ApplyError(Exception):
    """A change that cannot be written, with a message for the person reviewing the import."""


def _duplicate_pk(key):
    """
    The primary key in a ``...#<pk>`` key that netbox_state gives extra copies of a scope, exclusion or reservation.
    These keys are built from addresses only, so a "#" cannot come from Windows text.
    """
    match = re.search(r"#(\d+)$", key)
    return int(match.group(1)) if match else None


class Writer:
    """Applies changes for one Windows server. Under activate_branch every read and write goes to the branch."""

    def __init__(self, windows_server, user=None, subnet_ids=None):
        self.server = windows_server
        self.dhcp = windows_server.dhcp_server
        self.vrf = windows_server.import_vrf
        # When set (a reviewer applying proposal items), every object written is checked against this user's
        # permissions, constraints included, like NetBox's own edit views do. Imports run by a job leave it unset.
        self.user = user
        # Subnet IDs to give new subnets (branch imports), instead of letting sys4 count from the current schema.
        self.subnet_ids = subnet_ids

    # -- writes ------------------------------------------------------------------------------------------------------

    def _check(self, obj, action):
        if self.user is None:
            return
        model = type(obj)
        perm = f"{model._meta.app_label}.{action}_{model._meta.model_name}"
        if not self.user.has_perm(perm) or not model.objects.restrict(self.user, action).filter(pk=obj.pk).exists():
            raise ApplyError(f"permission {perm} does not allow this change to {model._meta.verbose_name} {obj}")

    def _save(self, obj):
        adding = obj._state.adding
        obj.full_clean()
        obj.save()
        self._check(obj, "add" if adding else "change")
        return obj

    def _update(self, obj, **values):
        """Set fields and save only when something changed, so unchanged objects leave no change log entry."""
        changed = {k: v for k, v in values.items() if getattr(obj, k) != v}
        if not changed:
            return obj
        self._check(obj, "change")
        obj.snapshot()
        for k, v in changed.items():
            setattr(obj, k, v)
        return self._save(obj)

    def _delete(self, obj):
        if obj is not None:
            self._check(obj, "delete")
            obj.snapshot()
            obj.delete()

    # -- naming ------------------------------------------------------------------------------------------------------

    def _names(self, scope_id, ip_address=""):
        values = {"server": self.dhcp.name, "scope_id": scope_id, "ip_address": ip_address}
        return (
            get_setting("subnet_name_template").format(**values),
            get_setting("reservation_name_template").format(**values),
        )

    # -- lookups -----------------------------------------------------------------------------------------------------

    def subnet(self, scope_id, required=True):
        """This server's subnet for a scope ID, or for a ``<scope ID>#<pk>`` key naming one of several."""
        if (pk := _duplicate_pk(scope_id)) is not None:
            return netbox_state.server_subnets(self.dhcp).filter(pk=pk).first()
        for s in netbox_state.server_subnets(self.dhcp).filter(prefix__prefix__net_contains_or_equals=scope_id):
            if netbox_state.scope_id_of(s) == scope_id:
                return s
        if required:
            raise ApplyError(f"scope {scope_id} is not in NetBox yet: apply it first")
        return None

    def reservation(self, scope_id, ip_address, required=True):
        """A reservation in this server's scope, or the one a ``<ip>#<pk>`` key names among several."""
        subnet = self.subnet(scope_id, required=required)
        found = None
        if subnet is not None and (pk := _duplicate_pk(ip_address)) is not None:
            found = HostReservation.objects.filter(subnet=subnet, pk=pk).first()
        elif subnet is not None and ip_address:
            found = (
                HostReservation.objects.filter(subnet=subnet, ipv4_address__address__net_host=ip_address)
                .order_by("pk")
                .first()
            )
        if found is None and required:
            raise ApplyError(f"reservation {ip_address} is not in NetBox yet: apply it first")
        return found

    def windows_class(self, class_type, name, required=True):
        found = WindowsClass.objects.filter(dhcp_server=self.dhcp, family=4, class_type=class_type, name=name).first()
        if found is None and required:
            raise ApplyError(f"{class_type} class {name!r} is not in NetBox yet: apply it first")
        return found

    def definition(self, option_id, vendor_class="", required=True):
        """The definition an option value uses: a server one for a vendor class or a custom code, else sys4's own."""
        qs = OptionDefinition.objects.filter(code=option_id, family=4)
        custom = qs.filter(dhcp_server=self.dhcp, standard=False)
        if vendor_class:
            found = custom.filter(
                netbox_windows_dhcp_plugin_windows_option_definition__vendor_class__name=vendor_class
            ).first()
        else:
            found = (
                custom.exclude(netbox_windows_dhcp_plugin_windows_option_definition__vendor_class__isnull=False).first()
                or qs.filter(standard=True, space="dhcp4").first()
            )
        if found is None and required:
            label = f"{vendor_class}/{option_id}" if vendor_class else str(option_id)
            raise ApplyError(f"option definition {label} is not in NetBox yet: apply it first")
        return found

    def _parent(self, parent, required=True):
        if parent == "server":
            return self.dhcp
        if "/" in parent:
            return self.reservation(*parent.split("/", 1), required=required)
        return self.subnet(parent, required=required)

    def option(self, key, fields):
        """The Option for a record, or the extra copy a duplicate's ``<key>#<pk>`` key names."""
        # Keys contain Windows class names, which may themselves contain "#". A key only names a duplicate when it
        # is exactly the record's own key plus "#<pk>", and that pk must be an option of the same parent and code.
        base = windows_state.option_key(
            fields["parent"], fields["option_id"], fields["vendor_class"], fields["user_class"]
        )
        duplicate = re.fullmatch(rf"{re.escape(base)}#(\d+)", key)
        parent = self._parent(fields["parent"], required=False)
        if parent is None:
            return None
        candidates = Option.objects.filter(
            assigned_object_type=ContentType.objects.get_for_model(parent),
            assigned_object_id=parent.pk,
            definition__code=fields["option_id"],
            definition__family=4,
        ).select_related(
            "netbox_windows_dhcp_plugin_windows_qualifier__user_class",
            "netbox_windows_dhcp_plugin_windows_qualifier__vendor_class",
        )
        if duplicate:
            return candidates.filter(pk=int(duplicate.group(1))).first()
        for o in candidates.order_by("pk"):
            q = getattr(o, "netbox_windows_dhcp_plugin_windows_qualifier", None)
            if q and q.policy_id:
                continue
            vendor = q.vendor_class.name if q and q.vendor_class_id else ""
            user = q.user_class.name if q and q.user_class_id else ""
            if (vendor, user) == (fields["vendor_class"], fields["user_class"]):
                return o
        return None

    # -- core IPAM objects -------------------------------------------------------------------------------------------

    def _prefix(self, network):
        found = Prefix.objects.filter(prefix=str(network), vrf=self.vrf).first()
        return found or self._save(Prefix(prefix=str(network), vrf=self.vrf, description="Windows DHCP scope"))

    def _ip_range(self, start, end, network, current=None):
        vrf = current.vrf if current is not None else self.vrf
        found = IPRange.objects.filter(start_address__net_host=start, end_address__net_host=end, vrf=vrf).first()
        if found:
            return found
        prefixlen = network.prefixlen
        if current is not None and all(
            str(pool.subnet.prefix.prefix) == str(network)
            for pool in current.netbox_dhcp_pools.select_related("subnet__prefix")
        ):
            # The range only serves this scope (on this server, or also on its failover partner, which holds the same
            # scope): move it. A second range would overlap it, which NetBox refuses.
            return self._update(current, start_address=f"{start}/{prefixlen}", end_address=f"{end}/{prefixlen}")
        return self._save(IPRange(start_address=f"{start}/{prefixlen}", end_address=f"{end}/{prefixlen}", vrf=vrf))

    def _ip_address(self, ip, prefixlen):
        found = IPAddress.objects.filter(address__net_host=ip, vrf=self.vrf).first()
        return found or self._save(
            IPAddress(address=f"{ip}/{prefixlen}", vrf=self.vrf, description="Windows DHCP reservation")
        )

    def _mac(self, client_id):
        """A MACAddress for a six-byte client ID; longer client IDs stay a plain string on the reservation."""
        try:
            raw = normalize.canonical_mac(client_id)
        except ValueError:
            return None
        if raw.count("-") != 5:
            return None
        mac = raw.replace("-", ":").upper()
        return MACAddress.objects.filter(mac_address=mac).first() or self._save(MACAddress(mac_address=mac))

    # -- apply -------------------------------------------------------------------------------------------------------

    def apply(self, change):
        """Apply one change and return the NetBox object it created or updated (None for a delete)."""
        handler = getattr(self, f"_apply_{change.kind}", None)
        if handler is None:
            raise ApplyError(f"cannot import {change.kind} objects yet")
        return handler(change)

    def _apply_class(self, change):
        fields = change.after or change.before
        obj = self.windows_class(fields["class_type"], fields["name"], required=False)
        if change.action == DELETE:
            return self._delete(obj)
        values = {"data": fields["data"], "description": fields["description"][:DESCRIPTION_LENGTH]}
        if obj is None:
            return self._save(
                WindowsClass(
                    dhcp_server=self.dhcp, family=4, class_type=fields["class_type"], name=fields["name"], **values
                )
            )
        return self._update(obj, **values)

    def _apply_optiondef(self, change):
        fields = change.after or change.before
        obj = self.definition(fields["option_id"], fields["vendor_class"], required=False)
        if obj is not None and obj.standard:
            obj = None  # never touch netbox-plugin-dhcp's own definitions
        if change.action == DELETE:
            return self._delete(obj)
        vendor = self.windows_class("vendor", fields["vendor_class"]) if fields["vendor_class"] else None
        values = {
            "name": fields["name"],
            "type": netbox_state.SYS4_TYPE.get(fields["type"], "binary"),
            "array": bool(fields["multi_valued"]),
            "description": fields["description"][:DESCRIPTION_LENGTH],
        }
        if obj is None:
            obj = self._save(
                OptionDefinition(dhcp_server=self.dhcp, family=4, space="dhcp4", code=fields["option_id"], **values)
            )
        else:
            obj = self._update(obj, **values)
        side = getattr(obj, "netbox_windows_dhcp_plugin_windows_option_definition", None)
        side_values = {
            "vendor_class": vendor,
            "windows_type": fields["type"],
            "default_value": list(fields["default_value"]),
        }
        if side is None:
            self._save(WindowsOptionDefinition(option_definition=obj, **side_values))
        else:
            self._update(side, **side_values)
        return obj

    def _apply_scope(self, change):
        fields = change.after or change.before
        subnet = self.subnet(change.key, required=False)
        if change.action == DELETE:
            return self._delete(subnet)
        prefixlen = normalize.parse_mask(fields["subnet_mask"])
        network = ipaddress.IPv4Network(f"{change.key}/{prefixlen}")
        # Keep the prefix the subnet already has (it may sit in another VRF) unless the mask changed.
        keep = subnet is not None and str(subnet.prefix.prefix) == str(network)
        prefix = subnet.prefix if keep else self._prefix(network)
        name, _ = self._names(change.key)
        values = {
            "prefix": prefix,
            "description": fields["name"][:DESCRIPTION_LENGTH],
            "comments": fields["description"],
            "valid_lifetime": fields["lease_seconds"],
        }
        if subnet is None:
            if self.subnet_ids is not None:
                values["subnet_id"] = next(self.subnet_ids)
            subnet = self._save(Subnet(name=name, dhcp_server=self.dhcp, **values))
            pool = None
        else:
            subnet = self._update(subnet, **values)
            pool = subnet.child_pools.order_by("pk").first()
        ip_range = self._ip_range(fields["start_range"], fields["end_range"], network, pool.ip_range if pool else None)
        if pool is None:
            self._save(Pool(name=name, subnet=subnet, ip_range=ip_range))
        else:
            self._update(pool, ip_range=ip_range)
        scope_values = {
            "state": fields["state"],
            "scope_type": fields["scope_type"],
            "delay_ms": fields["delay_ms"],
            "max_bootp_clients": fields["max_bootp_clients"],
            "activate_policies": fields["activate_policies"],
        }
        ws = WindowsScope.objects.filter(subnet=subnet).first()
        if ws is None:
            self._save(WindowsScope(subnet=subnet, **scope_values))
        else:
            self._update(ws, **scope_values)
        return subnet

    def _apply_exclusion(self, change):
        fields = change.after or change.before
        subnet = self.subnet(fields["scope_id"], required=change.action == CREATE)
        if change.action == DELETE:
            if subnet is not None:
                matches = ExclusionRange.objects.filter(subnet=subnet)
                if (pk := _duplicate_pk(change.key)) is not None:
                    matches = matches.filter(pk=pk)
                else:
                    matches = matches.filter(start_address=fields["start_range"], end_address=fields["end_range"])
                self._delete(matches.order_by("pk").first())
            return None
        # The key holds every field, so an exclusion is only ever created or deleted.
        return self._save(
            ExclusionRange(subnet=subnet, start_address=fields["start_range"], end_address=fields["end_range"])
        )

    def _apply_reservation(self, change):
        fields = change.after or change.before
        # The key, not the fields, so a "#<pk>" key reaches exactly the duplicate it names.
        obj = self.reservation(*change.key.split("/", 1), required=False)
        if change.action == DELETE:
            return self._delete(obj)
        subnet = self.subnet(fields["scope_id"])
        prefixlen = subnet.prefix.prefix.prefixlen
        mac = self._mac(fields["client_id"])
        values = {
            "description": fields["name"][:DESCRIPTION_LENGTH],
            "comments": fields["description"],
            "hw_address": mac,
            "client_id": None if mac else fields["client_id"],
        }
        if obj is None:
            _, name = self._names(fields["scope_id"], fields["ip_address"])
            obj = self._save(
                HostReservation(
                    name=name, subnet=subnet, ipv4_address=self._ip_address(fields["ip_address"], prefixlen), **values
                )
            )
        else:
            obj = self._update(obj, **values)
        side = WindowsReservation.objects.filter(host_reservation=obj).first()
        if side is None:
            self._save(WindowsReservation(host_reservation=obj, reservation_type=fields["reservation_type"]))
        else:
            self._update(side, reservation_type=fields["reservation_type"])
        return obj

    def _apply_option(self, change):
        fields = change.after or change.before
        obj = self.option(change.key, fields)
        if change.action == DELETE:
            return self._delete(obj)
        data = ", ".join(fields["value"])
        if obj is not None:
            return self._update(obj, data=data)
        parent = self._parent(fields["parent"])
        obj = self._save(
            Option(
                definition=self.definition(fields["option_id"], fields["vendor_class"]),
                assigned_object=parent,
                data=data,
            )
        )
        if fields["vendor_class"] or fields["user_class"]:
            self._save(
                WindowsOptionQualifier(
                    option=obj,
                    vendor_class=self.windows_class("vendor", fields["vendor_class"])
                    if fields["vendor_class"]
                    else None,
                    user_class=self.windows_class("user", fields["user_class"]) if fields["user_class"] else None,
                )
            )
        return obj
