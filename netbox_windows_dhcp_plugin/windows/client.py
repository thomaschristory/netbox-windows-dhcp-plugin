"""Typed client for the Windows DhcpServer PowerShell module.

Port of ``internal/dhcp`` (client.go, scope.go, reservation.go, exclusion.go,
option.go, lease.go) plus the objects the provider does not manage: option
definitions, user/vendor classes, failover, superscopes, MAC filters, policies and
DHCPv6 scopes, reservations and option values.

Rules, as in the Go code:

* only this module contains cmdlet text; everything above it calls one method per
  operation;
* every script reads its inputs from ``$p`` (never interpolated), splats ``@cn`` so
  ``computer_name`` becomes ``-ComputerName`` on every cmdlet, and assigns ``$out``;
* output helpers stringify every field with ``"$(...)"`` so enums serialise as names
  and IP addresses as dotted strings; durations go out as ``d.hh:mm:ss`` or seconds;
* add and set read the object back and return the server's view;
* getters that list and filter (``Where-Object``) return ``$out = $null`` when nothing
  matches, which ``_get`` turns into ``NotFound``. All getters for objects the Go code
  did not cover use that pattern, so they never rely on an unverified error code.

Cmdlet parameters or output properties not exercised by the Go provider and not
checked against a real server carry an ``# unverified:`` comment.

This module must not import Django or NetBox.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from . import normalize
from .psscript import NotFound, OutputError, PSError, Requirement, Script, is_not_found, parse
from .runner import Runner

__all__ = [
    "DHCP_MODULE",
    "POLICY_CRITERIA",
    "DhcpClass",
    "DhcpClient",
    "ExclusionRange",
    "FailoverRelationship",
    "FilterListState",
    "Lease",
    "MacFilter",
    "NotFound",
    "OptionDefinition",
    "OptionKey",
    "OptionKeyV6",
    "OptionValue",
    "OptionValueV6",
    "PSError",
    "Policy",
    "PolicyIPRange",
    "Reservation",
    "ReservationV6",
    "Scope",
    "ScopeV6",
    "Superscope",
    "is_not_found",
]

log = logging.getLogger(__name__)

DHCP_MODULE = Requirement(
    module="DhcpServer",
    probe="Get-DhcpServerv4Scope",
    hint="Install the DHCP Server role, or the RSAT DHCP tools on a management host "
    "(Install-WindowsFeature RSAT-DHCP).",
)

# Parameter names whose values must never be logged.
_SECRET_KEYS = frozenset({"shared_secret", "password"})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class _Model:
    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Self:
        names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)  # type: ignore[call-overload]


def _strs(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


@dataclass
class Scope(_Model):
    """An IPv4 scope. ``scope_id`` is derived by the server from start_range and mask."""

    scope_id: str = ""
    name: str = ""
    description: str = ""
    start_range: str = ""
    end_range: str = ""
    subnet_mask: str = ""
    state: str = "Active"  # Active | InActive (the server reports "Inactive")
    type: str = "Dhcp"  # Dhcp | Bootp | Both
    lease_duration: str = "8.00:00:00"  # d.hh:mm:ss
    # Optional settings; None means "do not send" on add/set.
    delay: int | None = None  # offer delay, milliseconds
    max_bootp_clients: int | None = None
    activate_policies: bool | None = None
    superscope_name: str = ""  # read only; managed through the superscope methods


@dataclass
class Reservation(_Model):
    scope_id: str = ""
    ip_address: str = ""
    client_id: str = ""  # aa-bb-cc-dd-ee-ff
    name: str = ""  # the server defaults it to the IP address
    description: str = ""
    type: str = "Both"  # Dhcp | Bootp | Both


@dataclass
class ExclusionRange(_Model):
    scope_id: str = ""
    start_range: str = ""
    end_range: str = ""


@dataclass
class OptionKey(_Model):
    """Where an IPv4 option value lives: server (no scope/reservation), scope or reservation.

    ``vendor_class``, ``user_class`` and ``policy_name`` further qualify it.
    """

    option_id: int = 0
    scope_id: str = ""
    reserved_ip: str = ""
    vendor_class: str = ""
    user_class: str = ""
    policy_name: str = ""


@dataclass
class OptionValue(_Model):
    option_id: int = 0
    name: str = ""
    value: list[str] = field(default_factory=list)
    vendor_class: str = ""
    user_class: str = ""
    policy_name: str = ""
    scope_id: str = ""  # echoed from the request
    reserved_ip: str = ""  # echoed from the request


@dataclass
class Lease(_Model):
    ip_address: str = ""
    scope_id: str = ""
    client_id: str = ""
    host_name: str = ""
    address_state: str = ""
    lease_expiry_time: str = ""  # RFC 3339 UTC, empty for reservations without a lease
    description: str = ""
    client_type: str = ""


@dataclass
class OptionDefinition(_Model):
    option_id: int = 0
    name: str = ""
    type: str = "String"  # Byte, Word, DWord, DWordDword, IPv4Address, String, BinaryData, EncapsulatedData, ...
    description: str = ""
    vendor_class: str = ""
    multi_valued: bool = False
    default_value: list[str] = field(default_factory=list)


@dataclass
class DhcpClass(_Model):
    """A user or vendor class."""

    name: str = ""
    type: str = "User"  # User | Vendor
    data: str = ""  # class data as ASCII text
    description: str = ""


@dataclass
class FailoverRelationship(_Model):
    name: str = ""
    partner_server: str = ""
    mode: str = "LoadBalance"  # LoadBalance | HotStandby
    scope_ids: list[str] = field(default_factory=list)
    server_role: str = ""  # HotStandby only: Active | Standby
    load_balance_percent: int | None = None  # LoadBalance only
    reserve_percent: int | None = None  # HotStandby only
    max_client_lead_time: int | None = None  # seconds
    state_switch_interval: int | None = None  # seconds; None disables automatic switch
    auto_state_transition: bool | None = None
    enable_auth: bool = False  # read only; set by giving a shared secret
    state: str = ""  # read only, e.g. Normal, CommunicationInterrupted


@dataclass
class Superscope(_Model):
    name: str = ""
    scope_ids: list[str] = field(default_factory=list)


@dataclass
class MacFilter(_Model):
    mac_address: str = ""  # aa-bb-cc-dd-ee-ff, or a wildcard pattern such as aa-bb-cc-*
    list: str = "Allow"  # Allow | Deny
    description: str = ""


@dataclass
class FilterListState(_Model):
    allow: bool = False
    deny: bool = False


# Policy criteria keys and the matching Add/Set-DhcpServerv4Policy parameter names.
# Each value is the cmdlet's string array, operator first: ["EQ", "aa-bb-cc*", ...].
POLICY_CRITERIA: dict[str, str] = {
    "vendor_class": "VendorClass",
    "user_class": "UserClass",
    "mac_address": "MacAddress",
    "client_id": "ClientId",
    "relay_agent": "RelayAgent",
    "circuit_id": "CircuitId",
    "remote_id": "RemoteId",
    "subscriber_id": "SubscriberId",
    "fqdn": "Fqdn",
}


@dataclass
class Policy(_Model):
    name: str = ""
    scope_id: str = ""  # empty: server-level policy
    description: str = ""
    enabled: bool = True
    processing_order: int | None = None
    condition: str = "OR"  # OR | AND
    criteria: dict[str, list[str]] = field(default_factory=dict)  # keys from POLICY_CRITERIA
    lease_duration: str = ""  # d.hh:mm:ss, empty: scope default


@dataclass
class PolicyIPRange(_Model):
    scope_id: str = ""
    policy_name: str = ""
    start_range: str = ""
    end_range: str = ""


@dataclass
class ScopeV6(_Model):
    prefix: str = ""  # e.g. 2001:db8:1:: (always a /64)
    name: str = ""
    description: str = ""
    state: str = "Active"
    preference: int = 0
    preferred_lifetime: str = "8.00:00:00"
    valid_lifetime: str = "12.00:00:00"
    t1: str = ""  # empty or 0.00:00:00: server computes it
    t2: str = ""


@dataclass
class ReservationV6(_Model):
    prefix: str = ""
    ip_address: str = ""
    client_duid: str = ""  # 00-01-00-01-...
    iaid: int = 0
    name: str = ""
    description: str = ""


@dataclass
class OptionKeyV6(_Model):
    option_id: int = 0
    prefix: str = ""
    reserved_ip: str = ""
    vendor_class: str = ""
    user_class: str = ""


@dataclass
class OptionValueV6(_Model):
    option_id: int = 0
    name: str = ""
    value: list[str] = field(default_factory=list)
    vendor_class: str = ""
    user_class: str = ""
    prefix: str = ""
    reserved_ip: str = ""


# ---------------------------------------------------------------------------
# PowerShell
# ---------------------------------------------------------------------------

# Helpers shared by several scripts.
# String arrays are built inline as @(<expr> | Where-Object { $null -ne $_ } | ForEach-Object { "$_" })
# rather than returned from a helper function: an array that went through a function's
# output can serialise as {"value": [...], "Count": n} in Windows PowerShell 5.1. The
# Where-Object drops $null (piping $null into ForEach-Object yields one empty string).
_HELPERS = r"""
function ConvertTo-WdSeconds($t) { if ($null -eq $t) { $null } else { [int64]([TimeSpan]$t).TotalSeconds } }
function ConvertTo-WdTimeSpan($t) { if ($null -eq $t) { '' } else { ([TimeSpan]$t).ToString('d\.hh\:mm\:ss') } }
"""

# ---- IPv4 scopes (scope.go) -------------------------------------------------

# unverified: Delay, MaxBootpClients, ActivatePolicies and SuperscopeName properties and the
# matching -Delay/-MaxBootpClients/-ActivatePolicies parameters (not used by the Go provider).
_SCOPE_FUNC = r"""
function ConvertTo-WdScope($s) {
    [ordered]@{
        scope_id          = "$($s.ScopeId)"
        name              = "$($s.Name)"
        description       = "$($s.Description)"
        start_range       = "$($s.StartRange)"
        end_range         = "$($s.EndRange)"
        subnet_mask       = "$($s.SubnetMask)"
        state             = "$($s.State)"
        type              = "$($s.Type)"
        lease_duration    = $s.LeaseDuration.ToString('d\.hh\:mm\:ss')
        delay             = [int]$s.Delay
        max_bootp_clients = [int64]$s.MaxBootpClients
        activate_policies = [bool]$s.ActivatePolicies
        superscope_name   = "$($s.SuperscopeName)"
    }
}
function Add-WdScopeExtras($a) {
    if ($null -ne $p.delay) { $a['Delay'] = [uint16]$p.delay }
    if ($null -ne $p.max_bootp_clients) { $a['MaxBootpClients'] = [uint32]$p.max_bootp_clients }
    if ($null -ne $p.activate_policies) { $a['ActivatePolicies'] = [bool]$p.activate_policies }
}
"""

S_AVAILABLE = Script("dhcp.available", "$out = $true")

S_SCOPE_GET = Script(
    "scope.get",
    _SCOPE_FUNC
    + r"""
$out = ConvertTo-WdScope (Get-DhcpServerv4Scope @cn -ScopeId $p.scope_id)
""",
)
S_SCOPE_LIST = Script(
    "scope.list",
    _SCOPE_FUNC
    + r"""
$out = @(Get-DhcpServerv4Scope @cn | ForEach-Object { ConvertTo-WdScope $_ })
""",
)
S_SCOPE_ADD = Script(
    "scope.add",
    _SCOPE_FUNC
    + r"""
$a = @{
    Name          = $p.name
    StartRange    = $p.start_range
    EndRange      = $p.end_range
    SubnetMask    = $p.subnet_mask
    State         = $p.state
    Type          = $p.type
    LeaseDuration = [TimeSpan]::FromSeconds($p.lease_seconds)
}
if ($p.description) { $a['Description'] = $p.description }
Add-WdScopeExtras $a
Add-DhcpServerv4Scope @cn @a
$out = ConvertTo-WdScope (Get-DhcpServerv4Scope @cn -ScopeId $p.scope_id)
""",
)
S_SCOPE_SET = Script(
    "scope.set",
    _SCOPE_FUNC
    + r"""
$a = @{
    ScopeId       = $p.scope_id
    Name          = $p.name
    Description   = "$($p.description)"
    StartRange    = $p.start_range
    EndRange      = $p.end_range
    State         = $p.state
    Type          = $p.type
    LeaseDuration = [TimeSpan]::FromSeconds($p.lease_seconds)
}
Add-WdScopeExtras $a
Set-DhcpServerv4Scope @cn @a
$out = ConvertTo-WdScope (Get-DhcpServerv4Scope @cn -ScopeId $p.scope_id)
""",
)
S_SCOPE_REMOVE = Script(
    "scope.remove",
    r"""
Remove-DhcpServerv4Scope @cn -ScopeId $p.scope_id -Force:([bool]$p.force) -Confirm:$false
""",
)

# ---- IPv4 reservations (reservation.go) -----------------------------------------

_RESERVATION_FUNC = r"""
function ConvertTo-WdReservation($r) {
    [ordered]@{
        scope_id    = "$($r.ScopeId)"
        ip_address  = "$($r.IPAddress)"
        client_id   = "$($r.ClientId)"
        name        = "$($r.Name)"
        description = "$($r.Description)"
        type        = "$($r.Type)"
    }
}
function Get-WdReservation {
    Get-DhcpServerv4Reservation @cn -ScopeId $p.scope_id |
        Where-Object { "$($_.IPAddress)" -eq $p.ip_address } |
        Select-Object -First 1
}
"""

S_RESERVATION_GET = Script(
    "reservation.get",
    _RESERVATION_FUNC
    + r"""
$r = Get-WdReservation
if ($r) { $out = ConvertTo-WdReservation $r }
""",
)
S_RESERVATION_LIST = Script(
    "reservation.list",
    _RESERVATION_FUNC
    + r"""
$out = @(Get-DhcpServerv4Reservation @cn -ScopeId $p.scope_id | ForEach-Object { ConvertTo-WdReservation $_ })
""",
)
S_RESERVATION_ADD = Script(
    "reservation.add",
    _RESERVATION_FUNC
    + r"""
$a = @{ ScopeId = $p.scope_id; IPAddress = $p.ip_address; ClientId = $p.client_id; Type = $p.type }
if ($p.name) { $a['Name'] = $p.name }
if ($p.description) { $a['Description'] = $p.description }
Add-DhcpServerv4Reservation @cn @a
$r = Get-WdReservation
if ($r) { $out = ConvertTo-WdReservation $r }
""",
)
# Set is keyed by IP only (no -ScopeId); an empty description clears it.
S_RESERVATION_SET = Script(
    "reservation.set",
    _RESERVATION_FUNC
    + r"""
$a = @{ IPAddress = $p.ip_address; ClientId = $p.client_id; Type = $p.type; Description = "$($p.description)" }
if ($p.name) { $a['Name'] = $p.name }
Set-DhcpServerv4Reservation @cn @a
$r = Get-WdReservation
if ($r) { $out = ConvertTo-WdReservation $r }
""",
)
S_RESERVATION_REMOVE = Script(
    "reservation.remove",
    r"""
Remove-DhcpServerv4Reservation @cn -IPAddress $p.ip_address -Confirm:$false
""",
)

# ---- IPv4 exclusion ranges (exclusion.go); there is no Set cmdlet -----------------

_EXCLUSION_FUNC = r"""
function ConvertTo-WdExclusion($e) {
    [ordered]@{ scope_id = "$($e.ScopeId)"; start_range = "$($e.StartRange)"; end_range = "$($e.EndRange)" }
}
function Get-WdExclusion {
    $e = Get-DhcpServerv4ExclusionRange @cn -ScopeId $p.scope_id |
        Where-Object { "$($_.StartRange)" -eq $p.start_range -and "$($_.EndRange)" -eq $p.end_range } |
        Select-Object -First 1
    if ($e) { ConvertTo-WdExclusion $e }
}
"""

S_EXCLUSION_GET = Script("exclusion.get", _EXCLUSION_FUNC + "\n$out = Get-WdExclusion\n")
S_EXCLUSION_LIST = Script(
    "exclusion.list",
    _EXCLUSION_FUNC
    + r"""
$out = @(Get-DhcpServerv4ExclusionRange @cn -ScopeId $p.scope_id | ForEach-Object { ConvertTo-WdExclusion $_ })
""",
)
S_EXCLUSION_ADD = Script(
    "exclusion.add",
    _EXCLUSION_FUNC
    + r"""
Add-DhcpServerv4ExclusionRange @cn -ScopeId $p.scope_id -StartRange $p.start_range -EndRange $p.end_range
$out = Get-WdExclusion
""",
)
S_EXCLUSION_REMOVE = Script(
    "exclusion.remove",
    r"""
$a = @{ ScopeId = $p.scope_id; StartRange = $p.start_range; EndRange = $p.end_range }
Remove-DhcpServerv4ExclusionRange @cn @a -Confirm:$false
""",
)

# ---- IPv4 option values (option.go) ------------------------------------------------

# unverified: -PolicyName on Get/Set/Remove-DhcpServerv4OptionValue and -All on Get (list).
_OPTION_FUNC = r"""
$sel = @{}
if ($p.scope_id) { $sel['ScopeId'] = $p.scope_id }
if ($p.reserved_ip) { $sel['ReservedIP'] = $p.reserved_ip }
if ($p.vendor_class) { $sel['VendorClass'] = $p.vendor_class }
if ($p.user_class) { $sel['UserClass'] = $p.user_class }
if ($p.policy_name) { $sel['PolicyName'] = $p.policy_name }
function ConvertTo-WdOption($o) {
    [ordered]@{
        option_id    = [int]$o.OptionId
        name         = "$($o.Name)"
        value        = @($o.Value | Where-Object { $null -ne $_ } | ForEach-Object { "$_" })
        vendor_class = "$($o.VendorClass)"
        user_class   = "$($o.UserClass)"
        policy_name  = "$($o.PolicyName)"
        scope_id     = "$($p.scope_id)"
        reserved_ip  = "$($p.reserved_ip)"
    }
}
function Get-WdOption {
    $o = Get-DhcpServerv4OptionValue @cn @sel |
        Where-Object { [int]$_.OptionId -eq [int]$p.option_id } |
        Select-Object -First 1
    if ($o) { ConvertTo-WdOption $o }
}
"""

S_OPTION_GET = Script("option.get", _HELPERS + _OPTION_FUNC + "\n$out = Get-WdOption\n")
S_OPTION_LIST = Script(
    "option.list",
    _HELPERS
    + _OPTION_FUNC
    + r"""
if ($p.all) { $sel['All'] = $true }
$out = @(Get-DhcpServerv4OptionValue @cn @sel | ForEach-Object { ConvertTo-WdOption $_ })
""",
)
# Set-DhcpServerv4OptionValue is create-or-overwrite, so create == update.
S_OPTION_SET = Script(
    "option.set",
    _HELPERS
    + _OPTION_FUNC
    + r"""
Set-DhcpServerv4OptionValue @cn @sel -OptionId $p.option_id -Value ([string[]]@($p.value))
$out = Get-WdOption
""",
)
S_OPTION_REMOVE = Script(
    "option.remove",
    _HELPERS
    + _OPTION_FUNC
    + r"""
Remove-DhcpServerv4OptionValue @cn @sel -OptionId $p.option_id -Confirm:$false
""",
)

# ---- IPv4 leases (lease.go), read only -------------------------------------------

S_LEASE_LIST = Script(
    "lease.list",
    r"""
$a = @{ ScopeId = $p.scope_id }
if ($p.all_leases) { $a['AllLeases'] = $true }
$out = @(Get-DhcpServerv4Lease @cn @a | ForEach-Object {
    $exp = ''
    if ($_.LeaseExpiryTime) { $exp = $_.LeaseExpiryTime.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'") }
    [ordered]@{
        ip_address        = "$($_.IPAddress)"
        scope_id          = "$($_.ScopeId)"
        client_id         = "$($_.ClientId)"
        host_name         = "$($_.HostName)"
        address_state     = "$($_.AddressState)"
        lease_expiry_time = $exp
        description       = "$($_.Description)"
        client_type       = "$($_.ClientType)"
    }
})
""",
)

# ---- IPv4 option definitions ----------------------------------------------------------

# unverified: -All on Get (all vendor classes); Set accepting -Name/-Description/-DefaultValue
# (type and multi-valued cannot be changed in place: remove and add instead).
_OPTIONDEF_FUNC = r"""
function ConvertTo-WdOptionDef($d) {
    [ordered]@{
        option_id     = [int]$d.OptionId
        name          = "$($d.Name)"
        type          = "$($d.Type)"
        description   = "$($d.Description)"
        vendor_class  = "$($d.VendorClass)"
        multi_valued  = [bool]$d.MultiValued
        default_value = @($d.DefaultValue | Where-Object { $null -ne $_ } | ForEach-Object { "$_" })
    }
}
$vc = @{}
if ($p.vendor_class) { $vc['VendorClass'] = $p.vendor_class }
function Get-WdOptionDef {
    $d = Get-DhcpServerv4OptionDefinition @cn @vc |
        Where-Object { [int]$_.OptionId -eq [int]$p.option_id -and "$($_.VendorClass)" -eq "$($p.vendor_class)" } |
        Select-Object -First 1
    if ($d) { ConvertTo-WdOptionDef $d }
}
"""

S_OPTIONDEF_GET = Script("optiondef.get", _HELPERS + _OPTIONDEF_FUNC + "\n$out = Get-WdOptionDef\n")
S_OPTIONDEF_LIST = Script(
    "optiondef.list",
    _HELPERS
    + _OPTIONDEF_FUNC
    + r"""
$out = @(Get-DhcpServerv4OptionDefinition @cn -All | ForEach-Object { ConvertTo-WdOptionDef $_ })
""",
)
S_OPTIONDEF_ADD = Script(
    "optiondef.add",
    _HELPERS
    + _OPTIONDEF_FUNC
    + r"""
$a = @{ OptionId = $p.option_id; Name = $p.name; Type = $p.type }
if ($p.description) { $a['Description'] = $p.description }
if ($p.multi_valued) { $a['MultiValued'] = $true }
if (@($p.default_value).Count -gt 0) { $a['DefaultValue'] = [string[]]@($p.default_value) }
Add-DhcpServerv4OptionDefinition @cn @vc @a
$out = Get-WdOptionDef
""",
)
S_OPTIONDEF_SET = Script(
    "optiondef.set",
    _HELPERS
    + _OPTIONDEF_FUNC
    + r"""
$a = @{ OptionId = $p.option_id; Name = $p.name; Description = "$($p.description)" }
if (@($p.default_value).Count -gt 0) { $a['DefaultValue'] = [string[]]@($p.default_value) }
Set-DhcpServerv4OptionDefinition @cn @vc @a
$out = Get-WdOptionDef
""",
)
S_OPTIONDEF_REMOVE = Script(
    "optiondef.remove",
    _OPTIONDEF_FUNC
    + r"""
Remove-DhcpServerv4OptionDefinition @cn @vc -OptionId $p.option_id -Confirm:$false
""",
)

# ---- IPv4 user and vendor classes -------------------------------------------------------

# unverified: whether the ASCII class data is in .AsciiData (and .Data holds hex) or in .Data.
_CLASS_FUNC = r"""
function ConvertTo-WdClass($c) {
    $data = "$($c.AsciiData)"
    if (-not $data) { $data = "$($c.Data)" }
    [ordered]@{ name = "$($c.Name)"; type = "$($c.Type)"; data = $data; description = "$($c.Description)" }
}
function Get-WdClass {
    $c = Get-DhcpServerv4Class @cn |
        Where-Object { "$($_.Name)" -eq $p.name -and "$($_.Type)" -eq $p.type } |
        Select-Object -First 1
    if ($c) { ConvertTo-WdClass $c }
}
"""

S_CLASS_GET = Script("class.get", _CLASS_FUNC + "\n$out = Get-WdClass\n")
S_CLASS_LIST = Script(
    "class.list",
    _CLASS_FUNC
    + r"""
$out = @(Get-DhcpServerv4Class @cn | Where-Object { -not $p.type -or "$($_.Type)" -eq $p.type } |
    ForEach-Object { ConvertTo-WdClass $_ })
""",
)
S_CLASS_ADD = Script(
    "class.add",
    _CLASS_FUNC
    + r"""
$a = @{ Name = $p.name; Type = $p.type; Data = $p.data }
if ($p.description) { $a['Description'] = $p.description }
Add-DhcpServerv4Class @cn @a
$out = Get-WdClass
""",
)
S_CLASS_SET = Script(
    "class.set",
    _CLASS_FUNC
    + r"""
Set-DhcpServerv4Class @cn -Name $p.name -Type $p.type -Data $p.data -Description "$($p.description)"
$out = Get-WdClass
""",
)
S_CLASS_REMOVE = Script(
    "class.remove",
    r"""
Remove-DhcpServerv4Class @cn -Name $p.name -Type $p.type -Confirm:$false
""",
)

# ---- IPv4 failover --------------------------------------------------------------------

# unverified: output property names (ServerRole, EnableAuth, StateSwitchInterval as TimeSpan),
# Set-DhcpServerv4Failover accepting -Mode/-ServerRole, and -Force on Invoke-...Replication
# suppressing the confirmation prompt. Writes go to one partner; call replicate_failover
# afterwards to copy scope configuration to the other.
_FAILOVER_FUNC = r"""
function ConvertTo-WdFailover($f) {
    [ordered]@{
        name                  = "$($f.Name)"
        partner_server        = "$($f.PartnerServer)"
        mode                  = "$($f.Mode)"
        scope_ids             = @($f.ScopeId | Where-Object { $null -ne $_ } | ForEach-Object { "$_" })
        server_role           = "$($f.ServerRole)"
        load_balance_percent  = if ($null -ne $f.LoadBalancePercent) { [int]$f.LoadBalancePercent } else { $null }
        reserve_percent       = if ($null -ne $f.ReservePercent) { [int]$f.ReservePercent } else { $null }
        max_client_lead_time  = ConvertTo-WdSeconds $f.MaxClientLeadTime
        state_switch_interval = ConvertTo-WdSeconds $f.StateSwitchInterval
        auto_state_transition = [bool]$f.AutoStateTransition
        enable_auth           = [bool]$f.EnableAuth
        state                 = "$($f.State)"
    }
}
function Get-WdFailover {
    $f = Get-DhcpServerv4Failover @cn | Where-Object { "$($_.Name)" -eq $p.name } | Select-Object -First 1
    if ($f) { ConvertTo-WdFailover $f }
}
function Add-WdFailoverSettings($a) {
    if ($null -ne $p.load_balance_percent) { $a['LoadBalancePercent'] = [uint32]$p.load_balance_percent }
    if ($null -ne $p.reserve_percent) { $a['ReservePercent'] = [uint32]$p.reserve_percent }
    if ($null -ne $p.max_client_lead_time) {
        $a['MaxClientLeadTime'] = [TimeSpan]::FromSeconds($p.max_client_lead_time)
    }
    if ($null -ne $p.state_switch_interval) {
        $a['StateSwitchInterval'] = [TimeSpan]::FromSeconds($p.state_switch_interval)
    }
    if ($null -ne $p.auto_state_transition) { $a['AutoStateTransition'] = [bool]$p.auto_state_transition }
    if ($p.shared_secret) { $a['SharedSecret'] = $p.shared_secret }
}
"""

S_FAILOVER_GET = Script("failover.get", _HELPERS + _FAILOVER_FUNC + "\n$out = Get-WdFailover\n")
S_FAILOVER_LIST = Script(
    "failover.list",
    _HELPERS
    + _FAILOVER_FUNC
    + r"""
$out = @(Get-DhcpServerv4Failover @cn | ForEach-Object { ConvertTo-WdFailover $_ })
""",
)
S_FAILOVER_ADD = Script(
    "failover.add",
    _HELPERS
    + _FAILOVER_FUNC
    + r"""
$a = @{ Name = $p.name; PartnerServer = $p.partner_server; ScopeId = [string[]]@($p.scope_ids) }
if ($p.mode -eq 'HotStandby') { $a['ServerRole'] = $p.server_role }
Add-WdFailoverSettings $a
Add-DhcpServerv4Failover @cn @a
$out = Get-WdFailover
""",
)
S_FAILOVER_SET = Script(
    "failover.set",
    _HELPERS
    + _FAILOVER_FUNC
    + r"""
$a = @{ Name = $p.name }
if ($p.mode) { $a['Mode'] = $p.mode }
if ($p.server_role) { $a['ServerRole'] = $p.server_role }
Add-WdFailoverSettings $a
Set-DhcpServerv4Failover @cn @a -Force
$out = Get-WdFailover
""",
)
S_FAILOVER_REMOVE = Script(
    "failover.remove",
    r"""
Remove-DhcpServerv4Failover @cn -Name $p.name -Force:([bool]$p.force) -Confirm:$false
""",
)
S_FAILOVER_ADD_SCOPE = Script(
    "failover.add_scope",
    _HELPERS
    + _FAILOVER_FUNC
    + r"""
Add-DhcpServerv4FailoverScope @cn -Name $p.name -ScopeId ([string[]]@($p.scope_ids))
$out = Get-WdFailover
""",
)
S_FAILOVER_REMOVE_SCOPE = Script(
    "failover.remove_scope",
    _HELPERS
    + _FAILOVER_FUNC
    + r"""
$ids = [string[]]@($p.scope_ids)
Remove-DhcpServerv4FailoverScope @cn -Name $p.name -ScopeId $ids -Force:([bool]$p.force) -Confirm:$false
$out = Get-WdFailover
""",
)
S_FAILOVER_REPLICATE = Script(
    "failover.replicate",
    r"""
$a = @{}
if ($p.name) { $a['Name'] = $p.name }
if (@($p.scope_ids).Count -gt 0) { $a['ScopeId'] = [string[]]@($p.scope_ids) }
$null = Invoke-DhcpServerv4FailoverReplication @cn @a -Force
""",
)

# ---- IPv4 superscopes -----------------------------------------------------------------

# Get-DhcpServerv4Superscope output is grouped by name here, so it works whether the cmdlet
# returns one object per superscope (with a ScopeId array) or one per member scope.
# unverified: the exact output shape, and that Remove-DhcpServerv4Superscope leaves the
# member scopes in place (documented behaviour). Removing members rebuilds the superscope.
_SUPERSCOPE_FUNC = r"""
function Get-WdSuperscopes {
    $m = [ordered]@{}
    foreach ($s in @(Get-DhcpServerv4Superscope @cn)) {
        $n = "$($s.SuperscopeName)"
        if (-not $n) { continue }
        if (-not $m.Contains($n)) { $m[$n] = [System.Collections.ArrayList]::new() }
        foreach ($id in @($s.ScopeId)) { if ($null -ne $id) { [void]$m[$n].Add("$id") } }
    }
    foreach ($k in $m.Keys) { [ordered]@{ name = $k; scope_ids = [string[]]$m[$k].ToArray() } }
}
function Get-WdSuperscope { Get-WdSuperscopes | Where-Object { $_.name -eq $p.name } | Select-Object -First 1 }
"""

S_SUPERSCOPE_GET = Script("superscope.get", _SUPERSCOPE_FUNC + "\n$out = Get-WdSuperscope\n")
S_SUPERSCOPE_LIST = Script("superscope.list", _SUPERSCOPE_FUNC + "\n$out = @(Get-WdSuperscopes)\n")
S_SUPERSCOPE_ADD = Script(
    "superscope.add",
    _SUPERSCOPE_FUNC
    + r"""
Add-DhcpServerv4Superscope @cn -SuperscopeName $p.name -ScopeId ([string[]]@($p.scope_ids))
$out = Get-WdSuperscope
""",
)
S_SUPERSCOPE_SET = Script(
    "superscope.set",
    _SUPERSCOPE_FUNC
    + r"""
$want = [string[]]@($p.scope_ids)
$cur = Get-WdSuperscope
$have = if ($cur) { [string[]]@($cur.scope_ids) } else { [string[]]@() }
$gone = @($have | Where-Object { $want -notcontains $_ })
if ($gone.Count -gt 0) {
    Remove-DhcpServerv4Superscope @cn -SuperscopeName $p.name -Confirm:$false
    $add = $want
} else {
    $add = [string[]]@($want | Where-Object { $have -notcontains $_ })
}
if ($add.Count -gt 0) { Add-DhcpServerv4Superscope @cn -SuperscopeName $p.name -ScopeId $add }
$out = Get-WdSuperscope
""",
)
S_SUPERSCOPE_RENAME = Script(
    "superscope.rename",
    _SUPERSCOPE_FUNC
    + r"""
Rename-DhcpServerv4Superscope @cn -SuperscopeName $p.name -NewName $p.new_name
$out = Get-WdSuperscopes | Where-Object { $_.name -eq $p.new_name } | Select-Object -First 1
""",
)
S_SUPERSCOPE_REMOVE = Script(
    "superscope.remove",
    r"""
Remove-DhcpServerv4Superscope @cn -SuperscopeName $p.name -Confirm:$false
""",
)

# ---- IPv4 MAC filters -----------------------------------------------------------------

# There is no Set-DhcpServerv4Filter: set removes and re-adds in one script.
# unverified: Remove-DhcpServerv4Filter accepting -MacAddress without -List.
_FILTER_FUNC = r"""
function ConvertTo-WdFilter($f) {
    [ordered]@{ mac_address = "$($f.MacAddress)"; list = "$($f.List)"; description = "$($f.Description)" }
}
function Get-WdFilter {
    $f = Get-DhcpServerv4Filter @cn | Where-Object { "$($_.MacAddress)" -eq $p.mac_address } | Select-Object -First 1
    if ($f) { ConvertTo-WdFilter $f }
}
function Add-WdFilter {
    $a = @{ List = $p.list; MacAddress = $p.mac_address }
    if ($p.description) { $a['Description'] = $p.description }
    Add-DhcpServerv4Filter @cn @a
}
"""

S_FILTER_GET = Script("filter.get", _FILTER_FUNC + "\n$out = Get-WdFilter\n")
S_FILTER_LIST = Script(
    "filter.list",
    _FILTER_FUNC
    + r"""
$a = @{}
if ($p.list) { $a['List'] = $p.list }
$out = @(Get-DhcpServerv4Filter @cn @a | ForEach-Object { ConvertTo-WdFilter $_ })
""",
)
S_FILTER_ADD = Script("filter.add", _FILTER_FUNC + "\nAdd-WdFilter\n$out = Get-WdFilter\n")
S_FILTER_SET = Script(
    "filter.set",
    _FILTER_FUNC
    + r"""
Remove-DhcpServerv4Filter @cn -MacAddress $p.mac_address -Confirm:$false
Add-WdFilter
$out = Get-WdFilter
""",
)
S_FILTER_REMOVE = Script(
    "filter.remove",
    r"""
Remove-DhcpServerv4Filter @cn -MacAddress $p.mac_address -Confirm:$false
""",
)
S_FILTERLIST_GET = Script(
    "filterlist.get",
    r"""
$l = Get-DhcpServerv4FilterList @cn
$out = [ordered]@{ allow = [bool]$l.Allow; deny = [bool]$l.Deny }
""",
)
S_FILTERLIST_SET = Script(
    "filterlist.set",
    r"""
Set-DhcpServerv4FilterList @cn -Allow ([bool]$p.allow) -Deny ([bool]$p.deny)
$l = Get-DhcpServerv4FilterList @cn
$out = [ordered]@{ allow = [bool]$l.Allow; deny = [bool]$l.Deny }
""",
)

# ---- IPv4 policies ----------------------------------------------------------------------

# Criteria parameter names come from this fixed table, never from $p.
# unverified: output property names (Condition, ProcessingOrder, LeaseDuration and the
# criteria arrays, operator first), and that Set leaves unspecified criteria unchanged
# (clearing a criterion is not supported here: remove and re-add the policy).
_POLICY_FUNC = (
    "$crit = [ordered]@{ "
    + "; ".join(f"{k} = '{v}'" for k, v in POLICY_CRITERIA.items())
    + r""" }
function ConvertTo-WdPolicy($x) {
    $c = [ordered]@{}
    foreach ($k in $crit.Keys) {
        $v = @($x.($crit[$k]) | Where-Object { $null -ne $_ } | ForEach-Object { "$_" })
        if ($v.Count -gt 0) { $c[$k] = $v }
    }
    [ordered]@{
        name             = "$($x.Name)"
        scope_id         = "$($p.scope_id)"
        description      = "$($x.Description)"
        enabled          = [bool]$x.Enabled
        processing_order = if ($null -ne $x.ProcessingOrder) { [int]$x.ProcessingOrder } else { $null }
        condition        = "$($x.Condition)"
        criteria         = $c
        lease_duration   = ConvertTo-WdTimeSpan $x.LeaseDuration
    }
}
$sel = @{}
if ($p.scope_id) { $sel['ScopeId'] = $p.scope_id }
function Get-WdPolicy {
    $x = Get-DhcpServerv4Policy @cn @sel | Where-Object { "$($_.Name)" -eq $p.name } | Select-Object -First 1
    if ($x) { ConvertTo-WdPolicy $x }
}
function Add-WdPolicySettings($a) {
    if ($null -ne $p.enabled) { $a['Enabled'] = [bool]$p.enabled }
    if ($null -ne $p.processing_order) { $a['ProcessingOrder'] = [uint16]$p.processing_order }
    if ($p.condition) { $a['Condition'] = $p.condition }
    if ($p.lease_seconds) { $a['LeaseDuration'] = [TimeSpan]::FromSeconds($p.lease_seconds) }
    if ($p.criteria) {
        foreach ($k in $crit.Keys) {
            $v = $p.criteria.$k
            if ($null -ne $v) { $a[$crit[$k]] = [string[]]@($v) }
        }
    }
}
"""
)

S_POLICY_GET = Script("policy.get", _HELPERS + _POLICY_FUNC + "\n$out = Get-WdPolicy\n")
S_POLICY_LIST = Script(
    "policy.list",
    _HELPERS
    + _POLICY_FUNC
    + r"""
$out = @(Get-DhcpServerv4Policy @cn @sel | ForEach-Object { ConvertTo-WdPolicy $_ })
""",
)
S_POLICY_ADD = Script(
    "policy.add",
    _HELPERS
    + _POLICY_FUNC
    + r"""
$a = @{ Name = $p.name }
if ($p.description) { $a['Description'] = $p.description }
Add-WdPolicySettings $a
Add-DhcpServerv4Policy @cn @sel @a
$out = Get-WdPolicy
""",
)
S_POLICY_SET = Script(
    "policy.set",
    _HELPERS
    + _POLICY_FUNC
    + r"""
$a = @{ Name = $p.name; Description = "$($p.description)" }
Add-WdPolicySettings $a
Set-DhcpServerv4Policy @cn @sel @a
$out = Get-WdPolicy
""",
)
S_POLICY_REMOVE = Script(
    "policy.remove",
    r"""
$sel = @{}
if ($p.scope_id) { $sel['ScopeId'] = $p.scope_id }
Remove-DhcpServerv4Policy @cn @sel -Name $p.name -Confirm:$false
""",
)

_POLICYRANGE_FUNC = r"""
function ConvertTo-WdPolicyRange($r) {
    [ordered]@{
        scope_id    = "$($p.scope_id)"
        policy_name = "$($p.policy_name)"
        start_range = "$($r.StartRange)"
        end_range   = "$($r.EndRange)"
    }
}
function Get-WdPolicyRanges {
    @(Get-DhcpServerv4PolicyIPRange @cn -ScopeId $p.scope_id -Name $p.policy_name |
        ForEach-Object { ConvertTo-WdPolicyRange $_ })
}
"""

S_POLICYRANGE_LIST = Script("policyrange.list", _POLICYRANGE_FUNC + "\n$out = @(Get-WdPolicyRanges)\n")
S_POLICYRANGE_ADD = Script(
    "policyrange.add",
    _POLICYRANGE_FUNC
    + r"""
$a = @{ ScopeId = $p.scope_id; Name = $p.policy_name; StartRange = $p.start_range; EndRange = $p.end_range }
Add-DhcpServerv4PolicyIPRange @cn @a
$out = Get-WdPolicyRanges | Where-Object { $_.start_range -eq $p.start_range -and $_.end_range -eq $p.end_range } |
    Select-Object -First 1
""",
)
S_POLICYRANGE_REMOVE = Script(
    "policyrange.remove",
    r"""
$a = @{ ScopeId = $p.scope_id; Name = $p.policy_name; StartRange = $p.start_range; EndRange = $p.end_range }
Remove-DhcpServerv4PolicyIPRange @cn @a -Confirm:$false
""",
)

# ---- DHCPv6 scopes ----------------------------------------------------------------------

# unverified: v6 scope output property names (PreferredLifetime, ValidLifetime, T1, T2,
# Preference) and the -PreferredLifeTime/-ValidLifeTime/-T1/-T2 parameter names.
_V6SCOPE_FUNC = r"""
function ConvertTo-WdScopeV6($s) {
    [ordered]@{
        prefix             = "$($s.Prefix)"
        name               = "$($s.Name)"
        description        = "$($s.Description)"
        state              = "$($s.State)"
        preference         = [int]$s.Preference
        preferred_lifetime = ConvertTo-WdTimeSpan $s.PreferredLifetime
        valid_lifetime     = ConvertTo-WdTimeSpan $s.ValidLifetime
        t1                 = ConvertTo-WdTimeSpan $s.T1
        t2                 = ConvertTo-WdTimeSpan $s.T2
    }
}
function Get-WdScopeV6 {
    $s = Get-DhcpServerv6Scope @cn | Where-Object { "$($_.Prefix)" -eq $p.prefix } | Select-Object -First 1
    if ($s) { ConvertTo-WdScopeV6 $s }
}
function Add-WdScopeV6Settings($a) {
    if ($p.state) { $a['State'] = $p.state }
    if ($null -ne $p.preference) { $a['Preference'] = [uint16]$p.preference }
    if ($p.preferred_seconds) { $a['PreferredLifeTime'] = [TimeSpan]::FromSeconds($p.preferred_seconds) }
    if ($p.valid_seconds) { $a['ValidLifeTime'] = [TimeSpan]::FromSeconds($p.valid_seconds) }
    if ($p.t1_seconds) { $a['T1'] = [TimeSpan]::FromSeconds($p.t1_seconds) }
    if ($p.t2_seconds) { $a['T2'] = [TimeSpan]::FromSeconds($p.t2_seconds) }
}
"""

S_V6SCOPE_GET = Script("v6scope.get", _HELPERS + _V6SCOPE_FUNC + "\n$out = Get-WdScopeV6\n")
S_V6SCOPE_LIST = Script(
    "v6scope.list",
    _HELPERS
    + _V6SCOPE_FUNC
    + r"""
$out = @(Get-DhcpServerv6Scope @cn | ForEach-Object { ConvertTo-WdScopeV6 $_ })
""",
)
S_V6SCOPE_ADD = Script(
    "v6scope.add",
    _HELPERS
    + _V6SCOPE_FUNC
    + r"""
$a = @{ Prefix = $p.prefix; Name = $p.name }
if ($p.description) { $a['Description'] = $p.description }
Add-WdScopeV6Settings $a
Add-DhcpServerv6Scope @cn @a
$out = Get-WdScopeV6
""",
)
S_V6SCOPE_SET = Script(
    "v6scope.set",
    _HELPERS
    + _V6SCOPE_FUNC
    + r"""
$a = @{ Prefix = $p.prefix; Name = $p.name; Description = "$($p.description)" }
Add-WdScopeV6Settings $a
Set-DhcpServerv6Scope @cn @a
$out = Get-WdScopeV6
""",
)
S_V6SCOPE_REMOVE = Script(
    "v6scope.remove",
    r"""
Remove-DhcpServerv6Scope @cn -Prefix $p.prefix -Force:([bool]$p.force) -Confirm:$false
""",
)

# ---- DHCPv6 reservations ----------------------------------------------------------------

# unverified: Set/Remove-DhcpServerv6Reservation keyed by -IPAddress alone, like v4.
_V6RES_FUNC = r"""
function ConvertTo-WdReservationV6($r) {
    [ordered]@{
        prefix      = "$($r.Prefix)"
        ip_address  = "$($r.IPAddress)"
        client_duid = "$($r.ClientDuid)"
        iaid        = [int64]$r.Iaid
        name        = "$($r.Name)"
        description = "$($r.Description)"
    }
}
function Get-WdReservationV6 {
    $r = Get-DhcpServerv6Reservation @cn -Prefix $p.prefix |
        Where-Object { "$($_.IPAddress)" -eq $p.ip_address } | Select-Object -First 1
    if ($r) { ConvertTo-WdReservationV6 $r }
}
"""

S_V6RES_GET = Script("v6reservation.get", _V6RES_FUNC + "\n$out = Get-WdReservationV6\n")
S_V6RES_LIST = Script(
    "v6reservation.list",
    _V6RES_FUNC
    + r"""
$out = @(Get-DhcpServerv6Reservation @cn -Prefix $p.prefix | ForEach-Object { ConvertTo-WdReservationV6 $_ })
""",
)
S_V6RES_ADD = Script(
    "v6reservation.add",
    _V6RES_FUNC
    + r"""
$a = @{ Prefix = $p.prefix; IPAddress = $p.ip_address; ClientDuid = $p.client_duid; Iaid = [uint32]$p.iaid }
if ($p.name) { $a['Name'] = $p.name }
if ($p.description) { $a['Description'] = $p.description }
Add-DhcpServerv6Reservation @cn @a
$out = Get-WdReservationV6
""",
)
S_V6RES_SET = Script(
    "v6reservation.set",
    _V6RES_FUNC
    + r"""
$a = @{ IPAddress = $p.ip_address; ClientDuid = $p.client_duid; Iaid = [uint32]$p.iaid }
$a['Description'] = "$($p.description)"
if ($p.name) { $a['Name'] = $p.name }
Set-DhcpServerv6Reservation @cn @a
$out = Get-WdReservationV6
""",
)
S_V6RES_REMOVE = Script(
    "v6reservation.remove",
    r"""
Remove-DhcpServerv6Reservation @cn -IPAddress $p.ip_address -Confirm:$false
""",
)

# ---- DHCPv6 option values -----------------------------------------------------------------

_V6OPTION_FUNC = r"""
$sel = @{}
if ($p.prefix) { $sel['Prefix'] = $p.prefix }
if ($p.reserved_ip) { $sel['ReservedIP'] = $p.reserved_ip }
if ($p.vendor_class) { $sel['VendorClass'] = $p.vendor_class }
if ($p.user_class) { $sel['UserClass'] = $p.user_class }
function ConvertTo-WdOptionV6($o) {
    [ordered]@{
        option_id    = [int]$o.OptionId
        name         = "$($o.Name)"
        value        = @($o.Value | Where-Object { $null -ne $_ } | ForEach-Object { "$_" })
        vendor_class = "$($o.VendorClass)"
        user_class   = "$($o.UserClass)"
        prefix       = "$($p.prefix)"
        reserved_ip  = "$($p.reserved_ip)"
    }
}
function Get-WdOptionV6 {
    $o = Get-DhcpServerv6OptionValue @cn @sel |
        Where-Object { [int]$_.OptionId -eq [int]$p.option_id } | Select-Object -First 1
    if ($o) { ConvertTo-WdOptionV6 $o }
}
"""

S_V6OPTION_GET = Script("v6option.get", _HELPERS + _V6OPTION_FUNC + "\n$out = Get-WdOptionV6\n")
S_V6OPTION_LIST = Script(
    "v6option.list",
    _HELPERS
    + _V6OPTION_FUNC
    + r"""
$out = @(Get-DhcpServerv6OptionValue @cn @sel | ForEach-Object { ConvertTo-WdOptionV6 $_ })
""",
)
S_V6OPTION_SET = Script(
    "v6option.set",
    _HELPERS
    + _V6OPTION_FUNC
    + r"""
Set-DhcpServerv6OptionValue @cn @sel -OptionId $p.option_id -Value ([string[]]@($p.value))
$out = Get-WdOptionV6
""",
)
S_V6OPTION_REMOVE = Script(
    "v6option.remove",
    _HELPERS
    + _V6OPTION_FUNC
    + r"""
Remove-DhcpServerv6OptionValue @cn @sel -OptionId $p.option_id -Confirm:$false
""",
)

ALL_SCRIPTS: tuple[Script, ...] = tuple(v for k, v in list(globals().items()) if k.startswith("S_"))


# ---------------------------------------------------------------------------
# Parameter builders
# ---------------------------------------------------------------------------


def _ipv4(s: str) -> str:
    return str(normalize.parse_ipv4(s)) if s else ""


def _ipv6(s: str) -> str:
    return normalize.canonical_ipv6(s) if s else ""


def _mac_or_raw(s: str) -> str:
    """Canonical MAC when it parses, else the value unchanged (wildcard filters)."""
    try:
        return normalize.canonical_mac(s)
    except ValueError:
        return s.strip()


def _seconds_or_none(s: str | int | None) -> int | None:
    if s is None or s == "":
        return None
    return normalize.parse_duration(s)


def _optional_seconds(s: str | int | None) -> int:
    """TimeSpan string or seconds; empty or zero means "let the server decide" (0)."""
    if s is None or s == "":
        return 0
    return normalize.parse_duration(s, allow_zero=True)


def _scope_params(s: Scope) -> dict[str, Any]:
    start, mask = _ipv4(s.start_range), _ipv4(s.subnet_mask)
    scope_id = _ipv4(s.scope_id) if s.scope_id else normalize.network_address(start, mask)
    return {
        "scope_id": scope_id,
        "name": s.name,
        "description": s.description,
        "start_range": start,
        "end_range": _ipv4(s.end_range),
        "subnet_mask": mask,
        "state": normalize.enum_of(s.state, *normalize.SCOPE_STATES),
        "type": normalize.enum_of(s.type, *normalize.SCOPE_TYPES),
        "lease_seconds": normalize.parse_duration(s.lease_duration),
        "delay": s.delay,
        "max_bootp_clients": s.max_bootp_clients,
        "activate_policies": s.activate_policies,
    }


def _reservation_params(r: Reservation) -> dict[str, Any]:
    return {
        "scope_id": _ipv4(r.scope_id),
        "ip_address": _ipv4(r.ip_address),
        "client_id": normalize.canonical_mac(r.client_id),
        "name": r.name,
        "description": r.description,
        "type": normalize.enum_of(r.type, *normalize.SCOPE_TYPES),
    }


def _exclusion_params(e: ExclusionRange) -> dict[str, Any]:
    return {"scope_id": _ipv4(e.scope_id), "start_range": _ipv4(e.start_range), "end_range": _ipv4(e.end_range)}


def _option_params(k: OptionKey) -> dict[str, Any]:
    return {
        "option_id": int(k.option_id),
        "scope_id": _ipv4(k.scope_id),
        "reserved_ip": _ipv4(k.reserved_ip),
        "vendor_class": k.vendor_class,
        "user_class": k.user_class,
        "policy_name": k.policy_name,
    }


def _v6option_params(k: OptionKeyV6) -> dict[str, Any]:
    return {
        "option_id": int(k.option_id),
        "prefix": _ipv6(k.prefix),
        "reserved_ip": _ipv6(k.reserved_ip),
        "vendor_class": k.vendor_class,
        "user_class": k.user_class,
    }


def _redact(params: Mapping[str, Any]) -> dict[str, Any]:
    return {k: ("***" if k in _SECRET_KEYS and v else v) for k, v in params.items()}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class DhcpClient:
    """One method per DhcpServer cmdlet operation.

    ``computer_name`` targets another server through ``-ComputerName`` (RSAT on a
    management host). With WinRM/Kerberos/NTLM the credentials are not delegated to
    the second hop, so connecting directly to the DHCP server is preferred.

    Errors: ``NotFound`` (also ``PSNotFoundError``) when an object does not exist,
    ``PSError`` for other PowerShell errors, ``RunnerError`` for transport failures.
    """

    module: ClassVar[Requirement] = DHCP_MODULE

    def __init__(self, runner: Runner, computer_name: str = "") -> None:
        self.runner = runner
        self.computer_name = computer_name

    # -- plumbing -------------------------------------------------------------

    def _run(self, script: Script, params: Mapping[str, Any] | None = None) -> tuple[bool, Any]:
        p = dict(params or {})
        if self.computer_name:
            p["computer_name"] = self.computer_name
        log.debug("running DHCP script %s %s", script.op, _redact(p))
        raw = self.runner.run(script.with_requirement(self.module).wrap(), p)
        try:
            return parse(raw)
        except PSError as e:
            log.debug("DHCP script %s failed: %s", script.op, e)
            raise

    def _get[M: _Model](self, script: Script, params: Mapping[str, Any] | None, cls: type[M]) -> M:
        present, data = self._run(script, params)
        if not present:
            raise NotFound(f"{script.op}: object not found")
        if not isinstance(data, dict):
            raise OutputError(f"{script.op}: expected an object, got {type(data).__name__}")
        return cls.from_dict(data)

    def _list[M: _Model](self, script: Script, params: Mapping[str, Any] | None, cls: type[M]) -> list[M]:
        present, data = self._run(script, params)
        if not present:
            return []
        if isinstance(data, dict):  # a single object that lost its array wrapper
            data = [data]
        return [cls.from_dict(d) for d in data]

    def _do(self, script: Script, params: Mapping[str, Any] | None = None) -> None:
        self._run(script, params)

    def check_available(self) -> None:
        """Raise unless the host is reachable and has the DhcpServer module."""
        self._do(S_AVAILABLE)

    # -- IPv4 scopes ---------------------------------------------------------

    def get_scope(self, scope_id: str) -> Scope:
        return self._get(S_SCOPE_GET, {"scope_id": _ipv4(scope_id)}, Scope)

    def list_scopes(self) -> list[Scope]:
        return self._list(S_SCOPE_LIST, None, Scope)

    def add_scope(self, scope: Scope) -> Scope:
        """Create a scope. The server derives the scope ID from start_range and subnet_mask."""
        return self._get(S_SCOPE_ADD, _scope_params(scope), Scope)

    def set_scope(self, scope: Scope) -> Scope:
        """Update a scope. The subnet mask cannot change (remove and re-add instead)."""
        return self._get(S_SCOPE_SET, _scope_params(scope), Scope)

    def remove_scope(self, scope_id: str, force: bool = False) -> None:
        """Remove a scope. Without ``force`` a scope with leases fails with DHCP 20007."""
        self._do(S_SCOPE_REMOVE, {"scope_id": _ipv4(scope_id), "force": bool(force)})

    # -- IPv4 reservations ---------------------------------------------------

    def get_reservation(self, scope_id: str, ip_address: str) -> Reservation:
        return self._get(S_RESERVATION_GET, {"scope_id": _ipv4(scope_id), "ip_address": _ipv4(ip_address)}, Reservation)

    def list_reservations(self, scope_id: str) -> list[Reservation]:
        return self._list(S_RESERVATION_LIST, {"scope_id": _ipv4(scope_id)}, Reservation)

    def add_reservation(self, r: Reservation) -> Reservation:
        return self._get(S_RESERVATION_ADD, _reservation_params(r), Reservation)

    def set_reservation(self, r: Reservation) -> Reservation:
        return self._get(S_RESERVATION_SET, _reservation_params(r), Reservation)

    def remove_reservation(self, ip_address: str) -> None:
        """Remove a reservation by IP (unique server-wide)."""
        self._do(S_RESERVATION_REMOVE, {"ip_address": _ipv4(ip_address)})

    # -- IPv4 exclusion ranges ---------------------------------------------------

    def get_exclusion_range(self, e: ExclusionRange) -> ExclusionRange:
        return self._get(S_EXCLUSION_GET, _exclusion_params(e), ExclusionRange)

    def list_exclusion_ranges(self, scope_id: str) -> list[ExclusionRange]:
        return self._list(S_EXCLUSION_LIST, {"scope_id": _ipv4(scope_id)}, ExclusionRange)

    def add_exclusion_range(self, e: ExclusionRange) -> ExclusionRange:
        return self._get(S_EXCLUSION_ADD, _exclusion_params(e), ExclusionRange)

    def remove_exclusion_range(self, e: ExclusionRange) -> None:
        self._do(S_EXCLUSION_REMOVE, _exclusion_params(e))

    # -- IPv4 option values ------------------------------------------------------

    def get_option_value(self, key: OptionKey) -> OptionValue:
        return self._get(S_OPTION_GET, _option_params(key), OptionValue)

    def list_option_values(
        self,
        scope_id: str = "",
        reserved_ip: str = "",
        vendor_class: str = "",
        user_class: str = "",
        policy_name: str = "",
        all: bool = False,  # noqa: A002 - mirrors the -All switch
    ) -> list[OptionValue]:
        p = _option_params(OptionKey(0, scope_id, reserved_ip, vendor_class, user_class, policy_name))
        p["all"] = bool(all)
        return self._list(S_OPTION_LIST, p, OptionValue)

    def set_option_value(self, key: OptionKey, value: Sequence[str]) -> OptionValue:
        """Create or overwrite an option value (Set-DhcpServerv4OptionValue is an upsert)."""
        p = _option_params(key)
        p["value"] = _strs(value)
        return self._get(S_OPTION_SET, p, OptionValue)

    def remove_option_value(self, key: OptionKey) -> None:
        self._do(S_OPTION_REMOVE, _option_params(key))

    # -- IPv4 leases (read only) ------------------------------------------------

    def list_leases(self, scope_id: str, all_leases: bool = False) -> list[Lease]:
        return self._list(S_LEASE_LIST, {"scope_id": _ipv4(scope_id), "all_leases": bool(all_leases)}, Lease)

    # -- IPv4 option definitions -----------------------------------------------

    def get_option_definition(self, option_id: int, vendor_class: str = "") -> OptionDefinition:
        return self._get(S_OPTIONDEF_GET, {"option_id": int(option_id), "vendor_class": vendor_class}, OptionDefinition)

    def list_option_definitions(self) -> list[OptionDefinition]:
        return self._list(S_OPTIONDEF_LIST, None, OptionDefinition)

    def _optiondef_params(self, d: OptionDefinition) -> dict[str, Any]:
        return {
            "option_id": int(d.option_id),
            "name": d.name,
            "type": d.type,
            "description": d.description,
            "vendor_class": d.vendor_class,
            "multi_valued": bool(d.multi_valued),
            "default_value": _strs(d.default_value),
        }

    def add_option_definition(self, d: OptionDefinition) -> OptionDefinition:
        return self._get(S_OPTIONDEF_ADD, self._optiondef_params(d), OptionDefinition)

    def set_option_definition(self, d: OptionDefinition) -> OptionDefinition:
        """Update name, description and default value. Type and multi_valued need remove + add."""
        return self._get(S_OPTIONDEF_SET, self._optiondef_params(d), OptionDefinition)

    def remove_option_definition(self, option_id: int, vendor_class: str = "") -> None:
        self._do(S_OPTIONDEF_REMOVE, {"option_id": int(option_id), "vendor_class": vendor_class})

    # -- IPv4 user and vendor classes ------------------------------------------

    @staticmethod
    def _class_type(t: str) -> str:
        return normalize.enum_of(t, "User", "Vendor")

    def get_class(self, name: str, type: str) -> DhcpClass:  # noqa: A002
        return self._get(S_CLASS_GET, {"name": name, "type": self._class_type(type)}, DhcpClass)

    def list_classes(self, type: str = "") -> list[DhcpClass]:  # noqa: A002
        return self._list(S_CLASS_LIST, {"type": self._class_type(type) if type else ""}, DhcpClass)

    def _class_params(self, c: DhcpClass) -> dict[str, Any]:
        return {"name": c.name, "type": self._class_type(c.type), "data": c.data, "description": c.description}

    def add_class(self, c: DhcpClass) -> DhcpClass:
        return self._get(S_CLASS_ADD, self._class_params(c), DhcpClass)

    def set_class(self, c: DhcpClass) -> DhcpClass:
        return self._get(S_CLASS_SET, self._class_params(c), DhcpClass)

    def remove_class(self, name: str, type: str) -> None:  # noqa: A002
        self._do(S_CLASS_REMOVE, {"name": name, "type": self._class_type(type)})

    # -- IPv4 failover -----------------------------------------------------------

    def get_failover(self, name: str) -> FailoverRelationship:
        return self._get(S_FAILOVER_GET, {"name": name}, FailoverRelationship)

    def list_failovers(self) -> list[FailoverRelationship]:
        return self._list(S_FAILOVER_LIST, None, FailoverRelationship)

    @staticmethod
    def _failover_params(f: FailoverRelationship, shared_secret: str) -> dict[str, Any]:
        mode = normalize.enum_of(f.mode, "LoadBalance", "HotStandby") if f.mode else ""
        return {
            "name": f.name,
            "partner_server": f.partner_server,
            "mode": mode,
            "scope_ids": [_ipv4(s) for s in f.scope_ids],
            "server_role": normalize.enum_of(f.server_role, "Active", "Standby") if f.server_role else "",
            "load_balance_percent": f.load_balance_percent,
            "reserve_percent": f.reserve_percent,
            "max_client_lead_time": f.max_client_lead_time,
            "state_switch_interval": f.state_switch_interval,
            "auto_state_transition": f.auto_state_transition,
            "shared_secret": shared_secret,
        }

    def add_failover(self, f: FailoverRelationship, shared_secret: str = "") -> FailoverRelationship:
        """Create a relationship on this server (and, through it, on the partner)."""
        if normalize.enum_of(f.mode, "LoadBalance", "HotStandby") == "HotStandby" and not f.server_role:
            raise ValueError("failover: server_role (Active or Standby) is required in HotStandby mode")
        return self._get(S_FAILOVER_ADD, self._failover_params(f, shared_secret), FailoverRelationship)

    def set_failover(self, f: FailoverRelationship, shared_secret: str = "") -> FailoverRelationship:
        """Update settings. Scope membership changes go through add/remove_failover_scopes."""
        return self._get(S_FAILOVER_SET, self._failover_params(f, shared_secret), FailoverRelationship)

    def remove_failover(self, name: str, force: bool = False) -> None:
        self._do(S_FAILOVER_REMOVE, {"name": name, "force": bool(force)})

    def add_failover_scopes(self, name: str, scope_ids: Sequence[str]) -> FailoverRelationship:
        return self._get(
            S_FAILOVER_ADD_SCOPE, {"name": name, "scope_ids": [_ipv4(s) for s in scope_ids]}, FailoverRelationship
        )

    def remove_failover_scopes(self, name: str, scope_ids: Sequence[str], force: bool = False) -> FailoverRelationship:
        return self._get(
            S_FAILOVER_REMOVE_SCOPE,
            {"name": name, "scope_ids": [_ipv4(s) for s in scope_ids], "force": bool(force)},
            FailoverRelationship,
        )

    def replicate_failover(self, name: str = "", scope_ids: Sequence[str] = ()) -> None:
        """Copy scope configuration to the partner (Invoke-DhcpServerv4FailoverReplication)."""
        self._do(S_FAILOVER_REPLICATE, {"name": name, "scope_ids": [_ipv4(s) for s in scope_ids]})

    # -- IPv4 superscopes --------------------------------------------------------

    def get_superscope(self, name: str) -> Superscope:
        return self._get(S_SUPERSCOPE_GET, {"name": name}, Superscope)

    def list_superscopes(self) -> list[Superscope]:
        return self._list(S_SUPERSCOPE_LIST, None, Superscope)

    def add_superscope(self, s: Superscope) -> Superscope:
        """Create a superscope, or add scopes to an existing one."""
        return self._get(S_SUPERSCOPE_ADD, {"name": s.name, "scope_ids": [_ipv4(x) for x in s.scope_ids]}, Superscope)

    def set_superscope(self, s: Superscope) -> Superscope:
        """Make the superscope's members exactly ``s.scope_ids``."""
        return self._get(S_SUPERSCOPE_SET, {"name": s.name, "scope_ids": [_ipv4(x) for x in s.scope_ids]}, Superscope)

    def rename_superscope(self, name: str, new_name: str) -> Superscope:
        return self._get(S_SUPERSCOPE_RENAME, {"name": name, "new_name": new_name}, Superscope)

    def remove_superscope(self, name: str) -> None:
        """Remove the superscope; its member scopes stay."""
        self._do(S_SUPERSCOPE_REMOVE, {"name": name})

    # -- IPv4 MAC filters ----------------------------------------------------------

    @staticmethod
    def _filter_params(f: MacFilter) -> dict[str, Any]:
        return {
            "mac_address": _mac_or_raw(f.mac_address),
            "list": normalize.enum_of(f.list, "Allow", "Deny"),
            "description": f.description,
        }

    def get_filter(self, mac_address: str) -> MacFilter:
        return self._get(S_FILTER_GET, {"mac_address": _mac_or_raw(mac_address)}, MacFilter)

    def list_filters(self, list_name: str = "") -> list[MacFilter]:
        """All MAC filters, or those of one list (Allow or Deny)."""
        name = normalize.enum_of(list_name, "Allow", "Deny") if list_name else ""
        return self._list(S_FILTER_LIST, {"list": name}, MacFilter)

    def add_filter(self, f: MacFilter) -> MacFilter:
        return self._get(S_FILTER_ADD, self._filter_params(f), MacFilter)

    def set_filter(self, f: MacFilter) -> MacFilter:
        """Change list or description (remove and re-add in one script)."""
        return self._get(S_FILTER_SET, self._filter_params(f), MacFilter)

    def remove_filter(self, mac_address: str) -> None:
        self._do(S_FILTER_REMOVE, {"mac_address": _mac_or_raw(mac_address)})

    def get_filter_lists(self) -> FilterListState:
        return self._get(S_FILTERLIST_GET, None, FilterListState)

    def set_filter_lists(self, allow: bool, deny: bool) -> FilterListState:
        """Enable or disable the allow and deny lists."""
        return self._get(S_FILTERLIST_SET, {"allow": bool(allow), "deny": bool(deny)}, FilterListState)

    # -- IPv4 policies ---------------------------------------------------------------

    @staticmethod
    def _policy_params(pol: Policy) -> dict[str, Any]:
        unknown = set(pol.criteria) - set(POLICY_CRITERIA)
        if unknown:
            raise ValueError(f"policy: unknown criteria {sorted(unknown)}; want {sorted(POLICY_CRITERIA)}")
        return {
            "name": pol.name,
            "scope_id": _ipv4(pol.scope_id),
            "description": pol.description,
            "enabled": pol.enabled,
            "processing_order": pol.processing_order,
            "condition": normalize.enum_of(pol.condition, "OR", "AND") if pol.condition else "",
            "criteria": {k: _strs(v) for k, v in pol.criteria.items()},
            "lease_seconds": _seconds_or_none(pol.lease_duration) or 0,
        }

    def get_policy(self, name: str, scope_id: str = "") -> Policy:
        return self._get(S_POLICY_GET, {"name": name, "scope_id": _ipv4(scope_id)}, Policy)

    def list_policies(self, scope_id: str = "") -> list[Policy]:
        """Server-level policies, or the policies of one scope."""
        return self._list(S_POLICY_LIST, {"scope_id": _ipv4(scope_id)}, Policy)

    def add_policy(self, pol: Policy) -> Policy:
        return self._get(S_POLICY_ADD, self._policy_params(pol), Policy)

    def set_policy(self, pol: Policy) -> Policy:
        return self._get(S_POLICY_SET, self._policy_params(pol), Policy)

    def remove_policy(self, name: str, scope_id: str = "") -> None:
        self._do(S_POLICY_REMOVE, {"name": name, "scope_id": _ipv4(scope_id)})

    def list_policy_ip_ranges(self, scope_id: str, policy_name: str) -> list[PolicyIPRange]:
        return self._list(S_POLICYRANGE_LIST, {"scope_id": _ipv4(scope_id), "policy_name": policy_name}, PolicyIPRange)

    def add_policy_ip_range(self, r: PolicyIPRange) -> PolicyIPRange:
        return self._get(S_POLICYRANGE_ADD, self._policy_range_params(r), PolicyIPRange)

    def remove_policy_ip_range(self, r: PolicyIPRange) -> None:
        self._do(S_POLICYRANGE_REMOVE, self._policy_range_params(r))

    @staticmethod
    def _policy_range_params(r: PolicyIPRange) -> dict[str, Any]:
        return {
            "scope_id": _ipv4(r.scope_id),
            "policy_name": r.policy_name,
            "start_range": _ipv4(r.start_range),
            "end_range": _ipv4(r.end_range),
        }

    # -- DHCPv6 scopes -------------------------------------------------------------

    @staticmethod
    def _v6scope_params(s: ScopeV6) -> dict[str, Any]:
        return {
            "prefix": _ipv6(s.prefix),
            "name": s.name,
            "description": s.description,
            "state": normalize.enum_of(s.state, *normalize.SCOPE_STATES) if s.state else "",
            "preference": s.preference,
            "preferred_seconds": _optional_seconds(s.preferred_lifetime),
            "valid_seconds": _optional_seconds(s.valid_lifetime),
            "t1_seconds": _optional_seconds(s.t1),
            "t2_seconds": _optional_seconds(s.t2),
        }

    def get_v6_scope(self, prefix: str) -> ScopeV6:
        return self._get(S_V6SCOPE_GET, {"prefix": _ipv6(prefix)}, ScopeV6)

    def list_v6_scopes(self) -> list[ScopeV6]:
        return self._list(S_V6SCOPE_LIST, None, ScopeV6)

    def add_v6_scope(self, s: ScopeV6) -> ScopeV6:
        return self._get(S_V6SCOPE_ADD, self._v6scope_params(s), ScopeV6)

    def set_v6_scope(self, s: ScopeV6) -> ScopeV6:
        return self._get(S_V6SCOPE_SET, self._v6scope_params(s), ScopeV6)

    def remove_v6_scope(self, prefix: str, force: bool = False) -> None:
        self._do(S_V6SCOPE_REMOVE, {"prefix": _ipv6(prefix), "force": bool(force)})

    # -- DHCPv6 reservations ---------------------------------------------------------

    @staticmethod
    def _v6res_params(r: ReservationV6) -> dict[str, Any]:
        return {
            "prefix": _ipv6(r.prefix),
            "ip_address": _ipv6(r.ip_address),
            "client_duid": normalize.canonical_duid(r.client_duid),
            "iaid": int(r.iaid),
            "name": r.name,
            "description": r.description,
        }

    def get_v6_reservation(self, prefix: str, ip_address: str) -> ReservationV6:
        return self._get(S_V6RES_GET, {"prefix": _ipv6(prefix), "ip_address": _ipv6(ip_address)}, ReservationV6)

    def list_v6_reservations(self, prefix: str) -> list[ReservationV6]:
        return self._list(S_V6RES_LIST, {"prefix": _ipv6(prefix)}, ReservationV6)

    def add_v6_reservation(self, r: ReservationV6) -> ReservationV6:
        return self._get(S_V6RES_ADD, self._v6res_params(r), ReservationV6)

    def set_v6_reservation(self, r: ReservationV6) -> ReservationV6:
        return self._get(S_V6RES_SET, self._v6res_params(r), ReservationV6)

    def remove_v6_reservation(self, ip_address: str) -> None:
        self._do(S_V6RES_REMOVE, {"ip_address": _ipv6(ip_address)})

    # -- DHCPv6 option values ----------------------------------------------------------

    def get_v6_option_value(self, key: OptionKeyV6) -> OptionValueV6:
        return self._get(S_V6OPTION_GET, _v6option_params(key), OptionValueV6)

    def list_v6_option_values(
        self, prefix: str = "", reserved_ip: str = "", vendor_class: str = "", user_class: str = ""
    ) -> list[OptionValueV6]:
        p = _v6option_params(OptionKeyV6(0, prefix, reserved_ip, vendor_class, user_class))
        return self._list(S_V6OPTION_LIST, p, OptionValueV6)

    def set_v6_option_value(self, key: OptionKeyV6, value: Sequence[str]) -> OptionValueV6:
        p = _v6option_params(key)
        p["value"] = _strs(value)
        return self._get(S_V6OPTION_SET, p, OptionValueV6)

    def remove_v6_option_value(self, key: OptionKeyV6) -> None:
        self._do(S_V6OPTION_REMOVE, _v6option_params(key))
