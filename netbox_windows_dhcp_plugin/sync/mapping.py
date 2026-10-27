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
from netbox_dhcp.models import HostReservation, Option, OptionDefinition, Pool, SharedNetwork, Subnet

from ..conf import get_setting
from ..models import (
    ExclusionRange,
    FailoverRelationship,
    MacFilter,
    MacFilterLists,
    WindowsClass,
    WindowsOptionDefinition,
    WindowsOptionQualifier,
    WindowsPolicy,
    WindowsReservation,
    WindowsScope,
    WindowsServer,
    WindowsVendorOption,
    WindowsVendorOptionDefinition,
)
from ..windows import normalize
from . import netbox_state, windows_state
from .diff import CREATE, DELETE
from .push import DUPLICATE

__all__ = ("ApplyError", "Writer")

# The widest prefix import creates to cover a superscope's scopes.
MIN_SHARED_PREFIX_LENGTH = 8

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
        # Windows class names are not case sensitive.
        found = WindowsClass.objects.filter(
            dhcp_server=self.dhcp, family=4, class_type=class_type, name__iexact=name
        ).first()
        if found is None and required:
            raise ApplyError(f"{class_type} class {name!r} is not in NetBox yet: apply it first")
        return found

    def definition(self, option_id, vendor_class="", required=True):
        """
        The definition an option value uses: for a vendor class, its WindowsVendorOptionDefinition; otherwise the
        server's own netbox-plugin-dhcp definition of the code, else sys4's standard one.
        """
        if vendor_class:
            found = WindowsVendorOptionDefinition.objects.filter(
                vendor_class__dhcp_server=self.dhcp,
                vendor_class__family=4,
                vendor_class__name__iexact=vendor_class,
                code=option_id,
            ).first()
        else:
            qs = OptionDefinition.objects.filter(code=option_id, family=4)
            found = (
                qs.filter(dhcp_server=self.dhcp, standard=False).first()
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
        vendor, user, policy = fields["vendor_class"], fields["user_class"], fields.get("policy", "")
        base = windows_state.option_key(fields["parent"], fields["option_id"], vendor, user, policy)
        duplicate = re.fullmatch(rf"{re.escape(base)}#(\d+)", key)
        parent = self._parent(fields["parent"], required=False)
        if parent is None:
            return None
        where = {"assigned_object_type": ContentType.objects.get_for_model(parent), "assigned_object_id": parent.pk}
        if vendor:
            candidates = WindowsVendorOption.objects.filter(
                **where, definition__code=fields["option_id"], definition__vendor_class__name__iexact=vendor
            ).select_related("user_class", "policy")
            qualifiers = ((o, o) for o in candidates.order_by("pk"))
        else:
            candidates = Option.objects.filter(
                **where, definition__code=fields["option_id"], definition__family=4
            ).select_related(
                "netbox_windows_dhcp_plugin_windows_qualifier__user_class",
                "netbox_windows_dhcp_plugin_windows_qualifier__policy",
            )
            qualifiers = (
                (o, getattr(o, "netbox_windows_dhcp_plugin_windows_qualifier", None)) for o in candidates.order_by("pk")
            )
        if duplicate:
            return candidates.filter(pk=int(duplicate.group(1))).first()
        for o, q in qualifiers:
            found_user = q.user_class.name if q and q.user_class_id else ""
            found_policy = q.policy.name if q and q.policy_id else ""
            if (found_user.lower(), found_policy.lower()) == (user.lower(), policy.lower()):
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
        if fields["vendor_class"]:
            return self._apply_vendor_optiondef(change, fields)
        obj = self.definition(fields["option_id"], required=False)
        if obj is not None and obj.standard:
            obj = None  # never touch netbox-plugin-dhcp's own definitions
        if change.action == DELETE:
            return self._delete(obj)
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
            "windows_type": fields["type"],
            "default_value": list(fields["default_value"]),
        }
        if side is None:
            self._save(WindowsOptionDefinition(option_definition=obj, **side_values))
        else:
            self._update(side, **side_values)
        return obj

    def _apply_vendor_optiondef(self, change, fields):
        obj = self.definition(fields["option_id"], fields["vendor_class"], required=False)
        if change.action == DELETE:
            return self._delete(obj)
        values = {
            "name": fields["name"],
            "windows_type": fields["type"],
            "multi_valued": bool(fields["multi_valued"]),
            "default_value": list(fields["default_value"]),
            "description": fields["description"][:DESCRIPTION_LENGTH],
        }
        if obj is None:
            vendor = self.windows_class("vendor", fields["vendor_class"])
            return self._save(WindowsVendorOptionDefinition(vendor_class=vendor, code=fields["option_id"], **values))
        return self._update(obj, **values)

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

    def _policy(self, fields):
        """The policy an option value is for, at the level of its parent (server or scope)."""
        name = fields.get("policy", "")
        if not name:
            return None
        scope_id = fields["parent"].split("/", 1)[0] if fields["parent"] != "server" else ""
        policy = self.windows_policy(scope_id, name, required=True)
        return policy

    def _apply_option(self, change):
        fields = change.after or change.before
        obj = self.option(change.key, fields)
        if change.action == DELETE:
            return self._delete(obj)
        vendor = fields["vendor_class"]
        if vendor:
            if obj is not None:
                return self._update(obj, value=list(fields["value"]))
        else:
            data = windows_state.join_values(fields["value"])
            if obj is not None:
                return self._update(obj, data=data)
        parent = self._parent(fields["parent"])
        user = self.windows_class("user", fields["user_class"]) if fields["user_class"] else None
        policy = self._policy(fields)
        definition = self.definition(fields["option_id"], vendor)
        if vendor:
            return self._save(
                WindowsVendorOption(
                    definition=definition,
                    assigned_object=parent,
                    user_class=user,
                    policy=policy,
                    value=list(fields["value"]),
                )
            )
        obj = self._save(Option(definition=definition, assigned_object=parent, data=data))
        if user or policy:
            self._save(WindowsOptionQualifier(option=obj, user_class=user, policy=policy))
        return obj

    # -- policies, superscopes, failover, MAC filters ----------------------------------------------------------------

    @staticmethod
    def _marked(change, queryset):
        """
        The extra copy a delete of a NetBox duplicate names (two policies differing only in case, say), looked up by
        primary key within this server, so the lookup by name cannot pick the original instead.
        """
        pk = (change.before or {}).get(DUPLICATE) if change.action == DELETE else None
        return queryset.filter(pk=pk).first() if pk is not None else None

    def windows_policy(self, scope_id, name, required=True):
        subnet = self.subnet(scope_id, required=required) if scope_id else None
        if scope_id and subnet is None:
            return None
        found = WindowsPolicy.objects.filter(dhcp_server=self.dhcp, subnet=subnet, name__iexact=name).first()
        if found is None and required:
            raise ApplyError(f"policy {name!r} is not in NetBox yet: apply it first")
        return found

    def _apply_policy(self, change):
        fields = change.after or change.before
        obj = self._marked(change, WindowsPolicy.objects.filter(dhcp_server=self.dhcp)) or self.windows_policy(
            fields["scope_id"], fields["name"], required=False
        )
        if change.action == DELETE:
            return self._delete(obj)
        values = {
            "name": fields["name"],
            "description": fields["description"][:DESCRIPTION_LENGTH],
            "enabled": fields["enabled"],
            "processing_order": fields["processing_order"] or 1,
            "condition": fields["condition"],
            "criteria": fields["criteria"],
            "lease_duration": fields["lease_seconds"],
            "ip_ranges": fields["ip_ranges"],
        }
        if obj is None:
            subnet = self.subnet(fields["scope_id"]) if fields["scope_id"] else None
            return self._save(WindowsPolicy(dhcp_server=self.dhcp, subnet=subnet, **values))
        return self._update(obj, **values)

    def shared_network(self, name):
        """This server's shared network for a Windows superscope (matched on its description, then its name)."""
        networks = SharedNetwork.objects.filter(dhcp_server=self.dhcp)
        return (
            networks.filter(description__iexact=name).order_by("pk").first()
            or networks.filter(name__iexact=name).order_by("pk").first()
        )

    def _covering_prefix(self, network, subnets):
        """A prefix covering every member scope: netbox-plugin-dhcp requires one on a shared network."""
        nets = [ipaddress.ip_network(str(s.prefix.prefix)) for s in subnets]
        if network is not None and all(n.subnet_of(ipaddress.ip_network(str(network.prefix.prefix))) for n in nets):
            return network.prefix
        supernet = nets[0]
        while not all(n.subnet_of(supernet) for n in nets):
            supernet = supernet.supernet()
        if supernet.prefixlen < MIN_SHARED_PREFIX_LENGTH:
            # Superscopes often group unrelated subnets; a prefix as wide as 0.0.0.0/0 would only pollute IPAM.
            raise ApplyError(
                f"the scopes of this superscope are too far apart for one prefix (it would be {supernet}); "
                "netbox-plugin-dhcp needs a prefix covering a shared network's subnets"
            )
        return self._prefix(supernet)

    def _move_subnet(self, subnet, shared_network):
        """Put a subnet in a shared network, or (None) back directly under the server: a subnet has one parent."""
        if subnet.shared_network_id == (shared_network.pk if shared_network else None):
            return
        self._check(subnet, "change")
        subnet.snapshot()
        subnet.shared_network = shared_network
        subnet.dhcp_server = None if shared_network else self.dhcp
        self._save(subnet)

    def _apply_superscope(self, change):
        fields = change.after or change.before
        network = self._marked(change, SharedNetwork.objects.filter(dhcp_server=self.dhcp)) or self.shared_network(
            fields["name"]
        )
        if change.action == DELETE:
            if network is not None:
                # netbox-plugin-dhcp deletes a shared network's subnets with it; on Windows the scopes stay, so they
                # move back under the server first.
                for subnet in Subnet.objects.filter(shared_network=network):
                    self._move_subnet(subnet, None)
                self._delete(network)
            return None
        members = [self.subnet(sid) for sid in fields["scope_ids"]]
        prefix = self._covering_prefix(network, members)
        if network is None:
            network = self._save(
                SharedNetwork(
                    name=f"{self.dhcp.name}/{fields['name']}",
                    description=fields["name"][:DESCRIPTION_LENGTH],
                    dhcp_server=self.dhcp,
                    prefix=prefix,
                )
            )
        else:
            network = self._update(network, prefix=prefix, description=fields["name"][:DESCRIPTION_LENGTH])
        for subnet in Subnet.objects.filter(shared_network=network).exclude(pk__in=[s.pk for s in members]):
            self._move_subnet(subnet, None)
        for subnet in members:
            self._move_subnet(subnet, network)
        return network

    def _partner(self, host):
        """
        The DHCP server a Windows partner name points at, through its Windows server (the DHCP server itself, so
        computer_name when it connects through a management host). Only a Windows server will do: the push needs its
        host to check where the failover secret may go.
        """
        servers = list(WindowsServer.objects.exclude(dhcp_server=self.dhcp).select_related("dhcp_server"))
        hosts = windows_state.partner_matches(host, [(ws.dhcp_host, ws.reported_names) for ws in servers])
        found = [ws for ws in servers if windows_state.host_name(ws.dhcp_host) in hosts]
        if len(found) == 1:
            return found[0].dhcp_server
        if found:
            raise ApplyError(
                f"failover partner {host!r} matches more than one Windows server in NetBox "
                f"({', '.join(str(ws) for ws in found)}): give the partner's Windows server this name as host"
            )
        # Windows reports the partner as it was named when the relationship was created, and on the partner's side by
        # the other server's computer name, which a server records on each connection test, import and push.
        raise ApplyError(
            f"failover partner {host!r} matches no Windows server in NetBox: add the partner, or run Test connection "
            "on its Windows server so it records its computer name"
        )

    def failover(self, name):
        return FailoverRelationship.objects.filter(primary_server=self.dhcp, name__iexact=name).first()

    def _apply_failover(self, change):
        fields = change.after or change.before
        obj = self._marked(change, FailoverRelationship.objects.filter(primary_server=self.dhcp)) or self.failover(
            fields["name"]
        )
        if change.action == DELETE:
            return self._delete(obj)
        values = {
            "name": fields["name"],
            "partner_server": self._partner(fields["partner_server"]),
            "mode": fields["mode"],
            "max_client_lead_time": fields["max_client_lead_time"],
            "load_balance_percent": fields["load_balance_percent"],
            "reserve_percent": fields["reserve_percent"],
            "server_role": fields["server_role"],
            "state_switch_interval": fields["state_switch_interval"],
        }
        # Message authentication (fields["auth"]) needs a credential profile with the secret: Windows never reveals
        # it. Without a profile NetBox leaves authentication as Windows has it; set shared_secret_profile to manage it.
        subnets = [self.subnet(sid) for sid in fields["scope_ids"]]
        if obj is None:
            reverse = FailoverRelationship.objects.filter(
                primary_server=values["partner_server"], partner_server=self.dhcp, name__iexact=fields["name"]
            )
            if reverse.exists():
                # Both partners imported before either was applied: keep the first, synced from its primary.
                raise ApplyError(
                    f"failover relationship {fields['name']} is already in NetBox with {values['partner_server']} as "
                    "its primary; it is synced from that server"
                )
            obj = self._save(FailoverRelationship(primary_server=self.dhcp, **values))
        else:
            obj = self._update(obj, **values)
        if {s.pk for s in obj.subnets.all()} != {s.pk for s in subnets}:
            self._check(obj, "change")
            obj.snapshot()
            obj.subnets.set(subnets)
        return obj

    def _apply_macfilter(self, change):
        fields = change.after or change.before
        filters = MacFilter.objects.filter(dhcp_server=self.dhcp)
        obj = self._marked(change, filters) or filters.filter(mac_address__iexact=fields["mac_address"]).first()
        if change.action == DELETE:
            return self._delete(obj)
        values = {"list": fields["list"], "description": fields["description"][:DESCRIPTION_LENGTH]}
        if obj is None:
            return self._save(MacFilter(dhcp_server=self.dhcp, mac_address=fields["mac_address"], **values))
        return self._update(obj, **values)

    def _apply_filterlists(self, change):
        obj = MacFilterLists.objects.filter(dhcp_server=self.dhcp).first()
        if change.action == DELETE:
            return self._delete(obj)
        fields = change.after
        values = {"allow_list_enabled": fields["allow"], "deny_list_enabled": fields["deny"]}
        if obj is None:
            return self._save(MacFilterLists(dhcp_server=self.dhcp, **values))
        return self._update(obj, **values)

    # -- DHCPv6 ------------------------------------------------------------------------------------------------------

    def subnet6(self, prefix, required=True):
        """This server's IPv6 subnet for a scope prefix, or for a ``<prefix>#<pk>`` key naming one of several."""
        if (pk := _duplicate_pk(prefix)) is not None:
            return netbox_state.server_subnets6(self.dhcp).filter(pk=pk).first()
        prefix = windows_state.ipv6(prefix)
        for s in netbox_state.server_subnets6(self.dhcp).filter(prefix__prefix__net_contains_or_equals=prefix):
            if netbox_state.prefix_of(s) == prefix:
                return s
        if required:
            raise ApplyError(f"DHCPv6 scope {prefix} is not in NetBox yet: apply it first")
        return None

    def reservation6(self, prefix, ip_address, required=True):
        """A reservation in this server's IPv6 scope, or the one a ``<ip>#<pk>`` key names among several."""
        subnet = self.subnet6(prefix, required=required)
        found = None
        if subnet is not None and (pk := _duplicate_pk(ip_address)) is not None:
            found = HostReservation.objects.filter(subnet=subnet, pk=pk).first()
        elif subnet is not None and ip_address:
            found = (
                HostReservation.objects.filter(subnet=subnet, ipv6_addresses__address__net_host=ip_address)
                .order_by("pk")
                .first()
            )
        if found is None and required:
            raise ApplyError(f"DHCPv6 reservation {ip_address} is not in NetBox yet: apply it first")
        return found

    def windows_class6(self, name, required=True):
        found = WindowsClass.objects.filter(
            dhcp_server=self.dhcp, family=6, class_type="user", name__iexact=name
        ).first()
        if found is None and required:
            raise ApplyError(f"DHCPv6 user class {name!r} is not in NetBox yet: apply it first")
        return found

    def _parent6(self, parent, required=True):
        if parent == "server":
            return self.dhcp
        prefix, ip = windows_state.option6_target(parent)
        if ip:
            return self.reservation6(prefix, ip, required=required)
        return self.subnet6(prefix, required=required)

    def option6(self, key, fields):
        """The DHCPv6 Option for a record, or the extra copy a duplicate's ``<key>#<pk>`` key names."""
        user = fields["user_class"]
        base = windows_state.option6_key(fields["parent"], fields["option_id"], user)
        duplicate = re.fullmatch(rf"{re.escape(base)}#(\d+)", key)
        parent = self._parent6(fields["parent"], required=False)
        if parent is None:
            return None
        candidates = Option.objects.filter(
            assigned_object_type=ContentType.objects.get_for_model(parent),
            assigned_object_id=parent.pk,
            definition__code=fields["option_id"],
            definition__family=6,
        ).select_related("netbox_windows_dhcp_plugin_windows_qualifier__user_class")
        if duplicate:
            return candidates.filter(pk=int(duplicate.group(1))).first()
        for o in candidates.order_by("pk"):
            q = getattr(o, "netbox_windows_dhcp_plugin_windows_qualifier", None)
            found_user = q.user_class.name if q and q.user_class_id else ""
            if found_user.lower() == user.lower():
                return o
        return None

    def _apply_class6(self, change):
        fields = change.after or change.before
        obj = self.windows_class6(fields["name"], required=False)
        if change.action == DELETE:
            return self._delete(obj)
        values = {"data": fields["data"], "description": fields["description"][:DESCRIPTION_LENGTH]}
        if obj is None:
            return self._save(
                WindowsClass(dhcp_server=self.dhcp, family=6, class_type="user", name=fields["name"], **values)
            )
        return self._update(obj, **values)

    def _apply_scope6(self, change):
        fields = change.after or change.before
        key = change.key.removeprefix("v6/")
        subnet = self.subnet6(key, required=False)
        if change.action == DELETE:
            return self._delete(subnet)
        network = ipaddress.IPv6Network(f"{fields['prefix']}/{fields['prefix_length']}")
        keep = subnet is not None and str(subnet.prefix.prefix) == str(network)
        prefix = subnet.prefix if keep else self._prefix(network)
        name, _ = self._names(fields["prefix"])
        values = {
            "prefix": prefix,
            "description": fields["name"][:DESCRIPTION_LENGTH],
            "comments": fields["description"],
            "preferred_lifetime": fields["preferred_lifetime"],
            "valid_lifetime": fields["valid_lifetime"],
            "renew_timer": fields["t1"],
            "rebind_timer": fields["t2"],
        }
        if subnet is None:
            if self.subnet_ids is not None:
                values["subnet_id"] = next(self.subnet_ids)
            subnet = self._save(Subnet(name=name, dhcp_server=self.dhcp, **values))
        else:
            subnet = self._update(subnet, **values)
        scope_values = {"state": fields["state"], "preference": fields["preference"]}
        ws = WindowsScope.objects.filter(subnet=subnet).first()
        if ws is None:
            self._save(WindowsScope(subnet=subnet, **scope_values))
        else:
            self._update(ws, **scope_values)
        return subnet

    def _apply_reservation6(self, change):
        fields = change.after or change.before
        # The key, not the fields, so a "#<pk>" key reaches exactly the duplicate it names.
        prefix, _, ip = change.key.removeprefix("v6/").partition("/")
        obj = self.reservation6(prefix, ip, required=False)
        if change.action == DELETE:
            return self._delete(obj)
        subnet = self.subnet6(fields["prefix"])
        values = {
            "description": fields["name"][:DESCRIPTION_LENGTH],
            "comments": fields["description"],
            "duid": fields["client_duid"],
        }
        if obj is None:
            _, name = self._names(fields["prefix"], fields["ip_address"])
            obj = self._save(HostReservation(name=name, subnet=subnet, **values))
            address = self._ip_address(fields["ip_address"], subnet.prefix.prefix.prefixlen)
            obj.ipv6_addresses.add(address)
        else:
            obj = self._update(obj, **values)
        side = WindowsReservation.objects.filter(host_reservation=obj).first()
        if side is None:
            self._save(WindowsReservation(host_reservation=obj, iaid=fields["iaid"]))
        else:
            self._update(side, iaid=fields["iaid"])
        return obj

    def _apply_option6(self, change):
        fields = change.after or change.before
        obj = self.option6(change.key, fields)
        if change.action == DELETE:
            return self._delete(obj)
        data = windows_state.join_values(fields["value"])
        if obj is not None:
            return self._update(obj, data=data)
        parent = self._parent6(fields["parent"])
        user = self.windows_class6(fields["user_class"]) if fields["user_class"] else None
        definition = OptionDefinition.objects.filter(code=fields["option_id"], family=6, standard=True).first()
        if definition is None:
            raise ApplyError(f"netbox-plugin-dhcp has no standard DHCPv6 definition for option {fields['option_id']}")
        obj = self._save(Option(definition=definition, assigned_object=parent, data=data))
        if user:
            self._save(WindowsOptionQualifier(option=obj, user_class=user))
        return obj
