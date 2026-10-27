"""
Write sync changes into NetBox: the plugin's DHCP models and the core IPAM objects scopes and reservations point at
(prefix, IP range, IP address, MAC address).

Every write goes through ``full_clean()`` and ``save()`` one object at a time, and ``snapshot()`` is taken before each
update or delete so the change log, and a branch's conflict detection, know the previous state.
"""

import ipaddress
import re

from dcim.models import MACAddress
from ipam.models import IPAddress, IPRange, Prefix

from ..models import (
    ExclusionRange,
    FailoverRelationship,
    MacFilter,
    MacFilterLists,
    OptionDefinition,
    OptionValue,
    Reservation,
    Scope,
    Superscope,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
)
from ..standard_options import STANDARD_V4, STANDARD_V6
from ..windows import normalize
from . import netbox_state, windows_state
from .diff import CREATE, DELETE
from .push import DUPLICATE

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


def _name(obj):
    return obj.name if obj is not None else ""


class Writer:
    """Applies changes for one Windows server. Under activate_branch every read and write goes to the branch."""

    def __init__(self, windows_server, user=None):
        self.server = windows_server
        self.vrf = windows_server.import_vrf
        # When set (a reviewer applying proposal items), every object written is checked against this user's
        # permissions, constraints included, like NetBox's own edit views do. Imports run by a job leave it unset.
        self.user = user

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

    # -- lookups -----------------------------------------------------------------------------------------------------

    def scope(self, scope_id, required=True):
        """This server's IPv4 scope for a scope ID, or for a ``<scope ID>#<pk>`` key naming one of several."""
        scopes = netbox_state.server_scopes(self.server)
        if (pk := _duplicate_pk(scope_id)) is not None:
            return scopes.filter(pk=pk).first()
        for s in scopes.filter(prefix__prefix__net_contains_or_equals=scope_id):
            if netbox_state.scope_id_of(s) == scope_id:
                return s
        if required:
            raise ApplyError(f"scope {scope_id} is not in NetBox yet: apply it first")
        return None

    def reservation(self, scope_id, ip_address, required=True):
        """A reservation in this server's scope, or the one a ``<ip>#<pk>`` key names among several."""
        scope = self.scope(scope_id, required=required)
        return self._find_reservation(scope, ip_address, required, "reservation")

    @staticmethod
    def _find_reservation(scope, ip_address, required, label):
        found = None
        if scope is not None and (pk := _duplicate_pk(ip_address)) is not None:
            found = Reservation.objects.filter(scope=scope, pk=pk).first()
        elif scope is not None and ip_address:
            found = (
                Reservation.objects.filter(scope=scope, ip_address__address__net_host=ip_address).order_by("pk").first()
            )
        if found is None and required:
            raise ApplyError(f"{label} {ip_address} is not in NetBox yet: apply it first")
        return found

    def windows_class(self, class_type, name, required=True):
        # Windows class names are not case sensitive.
        found = WindowsClass.objects.filter(
            server=self.server, family=4, class_type=class_type, name__iexact=name
        ).first()
        if found is None and required:
            raise ApplyError(f"{class_type} class {name!r} is not in NetBox yet: apply it first")
        return found

    def definition(self, option_id, vendor_class="", required=True):
        """This server's IPv4 definition of an option code, for a vendor class or (``""``) for every client."""
        qs = OptionDefinition.objects.filter(server=self.server, family=4, code=option_id)
        if vendor_class:
            found = qs.filter(vendor_class__name__iexact=vendor_class).first()
        else:
            found = qs.filter(vendor_class__isnull=True).first()
        if found is None and required:
            label = f"{vendor_class}/{option_id}" if vendor_class else str(option_id)
            raise ApplyError(f"option definition {label} is not in NetBox yet: apply it first")
        return found

    def _target(self, parent, required=True):
        """
        Where an IPv4 option value lives: (scope, reservation), (None, None) at server level, or None when its scope
        or reservation is not in NetBox.
        """
        if parent == "server":
            return None, None
        if "/" in parent:
            reservation = self.reservation(*parent.split("/", 1), required=required)
            return (reservation.scope, reservation) if reservation is not None else None
        scope = self.scope(parent, required=required)
        return (scope, None) if scope is not None else None

    def _find_option(self, key, base, family, target, fields, vendor, user, policy):
        """
        The OptionValue for a record, or the extra copy a duplicate's ``<base key>#<pk>`` key names. Keys contain
        Windows class names, which may themselves contain "#": a key only names a duplicate when it is exactly the
        record's own key plus "#<pk>", and that pk must be a value of the same parent and code.
        """
        if target is None:
            return None
        scope, reservation = target
        candidates = OptionValue.objects.filter(
            server=self.server, family=family, scope=scope, reservation=reservation, code=fields["option_id"]
        ).select_related("vendor_class", "user_class", "policy")
        if duplicate := re.fullmatch(rf"{re.escape(base)}#(\d+)", key):
            return candidates.filter(pk=int(duplicate.group(1))).first()
        wanted = (vendor.lower(), user.lower(), policy.lower())
        for o in candidates.order_by("pk"):
            if (_name(o.vendor_class).lower(), _name(o.user_class).lower(), _name(o.policy).lower()) == wanted:
                return o
        return None

    def option(self, key, fields):
        """The IPv4 OptionValue for a record, or the extra copy a duplicate's ``<key>#<pk>`` key names."""
        vendor, user, policy = fields["vendor_class"], fields["user_class"], fields.get("policy", "")
        base = windows_state.option_key(fields["parent"], fields["option_id"], vendor, user, policy)
        target = self._target(fields["parent"], required=False)
        return self._find_option(key, base, 4, target, fields, vendor, user, policy)

    # -- core IPAM objects -------------------------------------------------------------------------------------------

    def _prefix(self, network):
        found = Prefix.objects.filter(prefix=str(network), vrf=self.vrf).first()
        return found or self._save(Prefix(prefix=str(network), vrf=self.vrf, description="Windows DHCP scope"))

    def _ip_range(self, start, end, network, scope=None):
        """
        The IP range a scope hands out. ``scope`` is the scope being updated, whose current range is moved when no
        other scope needs it where it is.
        """
        current = scope.ip_range if scope is not None else None
        vrf = current.vrf if current is not None else self.vrf
        found = IPRange.objects.filter(start_address__net_host=start, end_address__net_host=end, vrf=vrf).first()
        if found:
            return found
        prefixlen = network.prefixlen
        others = current.netbox_windows_dhcp_plugin_scopes.exclude(pk=scope.pk) if current is not None else None
        if current is not None and all(str(s.prefix.prefix) == str(network) for s in others.select_related("prefix")):
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
                    server=self.server, family=4, class_type=fields["class_type"], name=fields["name"], **values
                )
            )
        return self._update(obj, **values)

    def _apply_optiondef(self, change):
        fields = change.after or change.before
        obj = self.definition(fields["option_id"], fields["vendor_class"], required=False)
        if change.action == DELETE:
            return self._delete(obj)
        values = {
            "name": fields["name"],
            "windows_type": fields["type"],
            "multi_valued": bool(fields["multi_valued"]),
            "default_value": list(fields["default_value"]),
            "description": fields["description"],
        }
        if obj is None:
            vendor = self.windows_class("vendor", fields["vendor_class"]) if fields["vendor_class"] else None
            return self._save(
                OptionDefinition(server=self.server, family=4, vendor_class=vendor, code=fields["option_id"], **values)
            )
        return self._update(obj, **values)

    def _apply_scope(self, change):
        fields = change.after or change.before
        scope = self.scope(change.key, required=False)
        if change.action == DELETE:
            return self._delete(scope)
        prefixlen = normalize.parse_mask(fields["subnet_mask"])
        network = ipaddress.IPv4Network(f"{change.key}/{prefixlen}")
        # Keep the prefix the scope already has (it may sit in another VRF) unless the mask changed.
        keep = scope is not None and str(scope.prefix.prefix) == str(network)
        values = {
            "prefix": scope.prefix if keep else self._prefix(network),
            "ip_range": self._ip_range(fields["start_range"], fields["end_range"], network, scope),
            "name": fields["name"],
            "description": fields["description"],
            "state": fields["state"],
            "scope_type": fields["scope_type"],
            "lease_duration": fields["lease_seconds"],
            "delay_ms": fields["delay_ms"],
            "max_bootp_clients": fields["max_bootp_clients"],
            "activate_policies": fields["activate_policies"],
        }
        if scope is None:
            return self._save(Scope(server=self.server, **values))
        return self._update(scope, **values)

    def _apply_exclusion(self, change):
        fields = change.after or change.before
        scope = self.scope(fields["scope_id"], required=change.action == CREATE)
        if change.action == DELETE:
            if scope is not None:
                matches = ExclusionRange.objects.filter(scope=scope)
                if (pk := _duplicate_pk(change.key)) is not None:
                    matches = matches.filter(pk=pk)
                else:
                    matches = matches.filter(start_address=fields["start_range"], end_address=fields["end_range"])
                self._delete(matches.order_by("pk").first())
            return None
        # The key holds every field, so an exclusion is only ever created or deleted.
        return self._save(
            ExclusionRange(scope=scope, start_address=fields["start_range"], end_address=fields["end_range"])
        )

    def _apply_reservation(self, change):
        fields = change.after or change.before
        # The key, not the fields, so a "#<pk>" key reaches exactly the duplicate it names.
        obj = self.reservation(*change.key.split("/", 1), required=False)
        if change.action == DELETE:
            return self._delete(obj)
        mac = self._mac(fields["client_id"])
        values = {
            "name": fields["name"],
            "description": fields["description"],
            "mac_address": mac,
            "client_id": "" if mac else fields["client_id"],
            "reservation_type": fields["reservation_type"],
        }
        if obj is None:
            scope = self.scope(fields["scope_id"])
            address = self._ip_address(fields["ip_address"], scope.prefix.prefix.prefixlen)
            return self._save(Reservation(scope=scope, ip_address=address, **values))
        return self._update(obj, **values)

    def _policy(self, fields):
        """The policy an option value is for, at the level of its parent (server or scope)."""
        name = fields.get("policy", "")
        if not name:
            return None
        scope_id = fields["parent"].split("/", 1)[0] if fields["parent"] != "server" else ""
        return self.windows_policy(scope_id, name, required=True)

    def _apply_option(self, change):
        fields = change.after or change.before
        obj = self.option(change.key, fields)
        if change.action == DELETE:
            return self._delete(obj)
        value = list(fields["value"])
        if obj is not None:
            return self._update(obj, value=value)
        scope, reservation = self._target(fields["parent"])
        vendor = fields["vendor_class"]
        # A value of a code Windows does not define could not be written back: the definition comes first.
        if vendor or fields["option_id"] not in STANDARD_V4:
            self.definition(fields["option_id"], vendor)
        return self._save(
            OptionValue(
                server=self.server,
                family=4,
                scope=scope,
                reservation=reservation,
                code=fields["option_id"],
                vendor_class=self.windows_class("vendor", vendor) if vendor else None,
                user_class=self.windows_class("user", fields["user_class"]) if fields["user_class"] else None,
                policy=self._policy(fields),
                value=value,
            )
        )

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
        scope = self.scope(scope_id, required=required) if scope_id else None
        if scope_id and scope is None:
            return None
        found = WindowsPolicy.objects.filter(server=self.server, scope=scope, name__iexact=name).first()
        if found is None and required:
            raise ApplyError(f"policy {name!r} is not in NetBox yet: apply it first")
        return found

    def _apply_policy(self, change):
        fields = change.after or change.before
        obj = self._marked(change, WindowsPolicy.objects.filter(server=self.server)) or self.windows_policy(
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
            scope = self.scope(fields["scope_id"]) if fields["scope_id"] else None
            return self._save(WindowsPolicy(server=self.server, scope=scope, **values))
        return self._update(obj, **values)

    def superscope(self, name):
        """This server's superscope of that Windows name (not case sensitive, as on Windows)."""
        return Superscope.objects.filter(server=self.server, name__iexact=name).order_by("pk").first()

    def _move_scope(self, scope, superscope):
        """Put a scope in a superscope, or (None) take it out."""
        if scope.superscope_id == (superscope.pk if superscope else None):
            return
        self._check(scope, "change")
        scope.snapshot()
        scope.superscope = superscope
        self._save(scope)

    def _apply_superscope(self, change):
        fields = change.after or change.before
        superscope = self._marked(change, Superscope.objects.filter(server=self.server)) or self.superscope(
            fields["name"]
        )
        if change.action == DELETE:
            # Its scopes stay, outside any superscope, as on Windows.
            return self._delete(superscope)
        members = [self.scope(sid) for sid in fields["scope_ids"]]
        if superscope is None:
            superscope = self._save(Superscope(server=self.server, name=fields["name"]))
        else:
            superscope = self._update(superscope, name=fields["name"])
        for scope in Scope.objects.filter(superscope=superscope).exclude(pk__in=[s.pk for s in members]):
            self._move_scope(scope, None)
        for scope in members:
            self._move_scope(scope, superscope)
        return superscope

    def _partner(self, host):
        """
        The Windows server a Windows partner name points at (the DHCP server itself, so computer_name when it
        connects through a management host). Only a Windows server will do: the push needs its host to check where
        the failover secret may go.
        """
        servers = list(WindowsServer.objects.exclude(pk=self.server.pk))
        hosts = windows_state.partner_matches(host, [(ws.dhcp_host, ws.reported_names) for ws in servers])
        found = [ws for ws in servers if windows_state.host_name(ws.dhcp_host) in hosts]
        if len(found) == 1:
            return found[0]
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
        return FailoverRelationship.objects.filter(primary_server=self.server, name__iexact=name).first()

    def _apply_failover(self, change):
        fields = change.after or change.before
        obj = self._marked(change, FailoverRelationship.objects.filter(primary_server=self.server)) or self.failover(
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
        scopes = [self.scope(sid) for sid in fields["scope_ids"]]
        if obj is None:
            reverse = FailoverRelationship.objects.filter(
                primary_server=values["partner_server"], partner_server=self.server, name__iexact=fields["name"]
            )
            if reverse.exists():
                # Both partners imported before either was applied: keep the first, synced from its primary.
                raise ApplyError(
                    f"failover relationship {fields['name']} is already in NetBox with {values['partner_server']} as "
                    "its primary; it is synced from that server"
                )
            obj = self._save(FailoverRelationship(primary_server=self.server, **values))
        else:
            obj = self._update(obj, **values)
        if {s.pk for s in obj.scopes.all()} != {s.pk for s in scopes}:
            self._check(obj, "change")
            obj.snapshot()
            obj.scopes.set(scopes)
        return obj

    def mac_filter(self, mac_address):
        return MacFilter.objects.filter(server=self.server, mac_address__iexact=mac_address).first()

    def _apply_macfilter(self, change):
        fields = change.after or change.before
        obj = self._marked(change, MacFilter.objects.filter(server=self.server)) or self.mac_filter(
            fields["mac_address"]
        )
        if change.action == DELETE:
            return self._delete(obj)
        values = {"list": fields["list"], "description": fields["description"][:DESCRIPTION_LENGTH]}
        if obj is None:
            return self._save(MacFilter(server=self.server, mac_address=fields["mac_address"], **values))
        return self._update(obj, **values)

    def filter_lists(self):
        return MacFilterLists.objects.filter(server=self.server).first()

    def _apply_filterlists(self, change):
        obj = self.filter_lists()
        if change.action == DELETE:
            return self._delete(obj)
        fields = change.after
        values = {"allow_list_enabled": fields["allow"], "deny_list_enabled": fields["deny"]}
        if obj is None:
            return self._save(MacFilterLists(server=self.server, **values))
        return self._update(obj, **values)

    # -- DHCPv6 ------------------------------------------------------------------------------------------------------

    def scope6(self, prefix, required=True):
        """This server's DHCPv6 scope for a scope prefix, or for a ``<prefix>#<pk>`` key naming one of several."""
        scopes = netbox_state.server_scopes6(self.server)
        if (pk := _duplicate_pk(prefix)) is not None:
            return scopes.filter(pk=pk).first()
        prefix = windows_state.ipv6(prefix)
        for s in scopes.filter(prefix__prefix__net_contains_or_equals=prefix):
            if netbox_state.prefix_of(s) == prefix:
                return s
        if required:
            raise ApplyError(f"DHCPv6 scope {prefix} is not in NetBox yet: apply it first")
        return None

    def reservation6(self, prefix, ip_address, required=True):
        """A reservation in this server's DHCPv6 scope, or the one a ``<ip>#<pk>`` key names among several."""
        scope = self.scope6(prefix, required=required)
        return self._find_reservation(scope, ip_address, required, "DHCPv6 reservation")

    def windows_class6(self, name, required=True):
        found = WindowsClass.objects.filter(server=self.server, family=6, class_type="user", name__iexact=name).first()
        if found is None and required:
            raise ApplyError(f"DHCPv6 user class {name!r} is not in NetBox yet: apply it first")
        return found

    def _target6(self, parent, required=True):
        """As ``_target``, for a DHCPv6 option value."""
        if parent == "server":
            return None, None
        prefix, ip = windows_state.option6_target(parent)
        if ip:
            reservation = self.reservation6(prefix, ip, required=required)
            return (reservation.scope, reservation) if reservation is not None else None
        scope = self.scope6(prefix, required=required)
        return (scope, None) if scope is not None else None

    def option6(self, key, fields):
        """The DHCPv6 OptionValue for a record, or the extra copy a duplicate's ``<key>#<pk>`` key names."""
        user = fields["user_class"]
        base = windows_state.option6_key(fields["parent"], fields["option_id"], user)
        target = self._target6(fields["parent"], required=False)
        return self._find_option(key, base, 6, target, fields, "", user, fields.get("policy", ""))

    def _apply_class6(self, change):
        fields = change.after or change.before
        obj = self.windows_class6(fields["name"], required=False)
        if change.action == DELETE:
            return self._delete(obj)
        values = {"data": fields["data"], "description": fields["description"][:DESCRIPTION_LENGTH]}
        if obj is None:
            return self._save(
                WindowsClass(server=self.server, family=6, class_type="user", name=fields["name"], **values)
            )
        return self._update(obj, **values)

    def _apply_scope6(self, change):
        fields = change.after or change.before
        scope = self.scope6(change.key.removeprefix("v6/"), required=False)
        if change.action == DELETE:
            return self._delete(scope)
        network = ipaddress.IPv6Network(f"{fields['prefix']}/{fields['prefix_length']}")
        keep = scope is not None and str(scope.prefix.prefix) == str(network)
        values = {
            "prefix": scope.prefix if keep else self._prefix(network),
            "name": fields["name"],
            "description": fields["description"],
            "state": fields["state"],
            "preference": fields["preference"],
            "preferred_lifetime": fields["preferred_lifetime"],
            "valid_lifetime": fields["valid_lifetime"],
            "renew_timer": fields["t1"],
            "rebind_timer": fields["t2"],
        }
        if scope is None:
            return self._save(Scope(server=self.server, **values))
        return self._update(scope, **values)

    def _apply_reservation6(self, change):
        fields = change.after or change.before
        # The key, not the fields, so a "#<pk>" key reaches exactly the duplicate it names.
        prefix, _, ip = change.key.removeprefix("v6/").partition("/")
        obj = self.reservation6(prefix, ip, required=False)
        if change.action == DELETE:
            return self._delete(obj)
        values = {
            "name": fields["name"],
            "description": fields["description"],
            "duid": fields["client_duid"],
            "iaid": fields["iaid"],
        }
        if obj is None:
            scope = self.scope6(fields["prefix"])
            address = self._ip_address(fields["ip_address"], scope.prefix.prefix.prefixlen)
            return self._save(Reservation(scope=scope, ip_address=address, **values))
        return self._update(obj, **values)

    def _apply_option6(self, change):
        fields = change.after or change.before
        obj = self.option6(change.key, fields)
        if change.action == DELETE:
            return self._delete(obj)
        value = list(fields["value"])
        if obj is not None:
            return self._update(obj, value=value)
        if fields["option_id"] not in STANDARD_V6:
            # DHCPv6 option definitions are not synced: only standard codes have a known meaning on both sides.
            raise ApplyError(f"option {fields['option_id']} has no standard DHCPv6 definition")
        scope, reservation = self._target6(fields["parent"])
        return self._save(
            OptionValue(
                server=self.server,
                family=6,
                scope=scope,
                reservation=reservation,
                code=fields["option_id"],
                user_class=self.windows_class6(fields["user_class"]) if fields["user_class"] else None,
                value=value,
            )
        )
