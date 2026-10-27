"""DhcpClient against recorded JSON envelopes (port of internal/dhcp/client_test.go)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from netbox_windows_dhcp_plugin.windows import client as wc
from netbox_windows_dhcp_plugin.windows.client import (
    ALL_SCRIPTS,
    S_SUPERSCOPE_RENAME,
    S_V6LEASE_LIST,
    DhcpClass,
    DhcpClient,
    ExclusionRange,
    FailoverRelationship,
    MacFilter,
    OptionDefinition,
    OptionKey,
    OptionKeyV6,
    Policy,
    Reservation,
    ReservationV6,
    Scope,
    ScopeV6,
    Superscope,
)
from netbox_windows_dhcp_plugin.windows.psscript import NotFound, PSError, op_of
from netbox_windows_dhcp_plugin.windows.runner import RunnerError

from .conftest import fixture_bytes


class FixtureRunner:
    def __init__(self, fixture: str = "ok_null.json", err: Exception | None = None) -> None:
        self.fixture = fixture
        self.err = err
        self.op = ""
        self.script = ""
        self.params: dict[str, Any] = {}

    def run(self, script: str, params: Any = None) -> bytes:
        self.op = op_of(script)
        self.script = script
        self.params = dict(params or {})
        if self.err is not None:
            raise self.err
        return fixture_bytes(self.fixture)


def new_client(fixture: str = "ok_null.json", computer_name: str = "") -> tuple[DhcpClient, FixtureRunner]:
    r = FixtureRunner(fixture)
    return DhcpClient(r, computer_name), r


USERS = Scope(
    name="Users VLAN 120",
    start_range="10.1.20.10",
    end_range="10.1.20.250",
    subnet_mask="255.255.255.0",
    state="Active",
    type="Dhcp",
    lease_duration="0.08:00:00",
)


def test_get_scope() -> None:
    c, r = new_client("scope_get.json")
    s = c.get_scope("10.1.20.0")
    assert s == Scope(
        scope_id="10.1.20.0",
        name="Users VLAN 120",
        start_range="10.1.20.10",
        end_range="10.1.20.250",
        subnet_mask="255.255.255.0",
        state="Active",
        type="Dhcp",
        lease_duration="8.00:00:00",
    )
    assert r.op == "scope.get" and r.params == {"scope_id": "10.1.20.0"}
    assert "computer_name" not in r.params
    assert "Get-DhcpServerv4Scope @cn -ScopeId $p.scope_id" in r.script
    assert "Get-Command -Name 'Get-DhcpServerv4Scope'" in r.script  # module check


def test_computer_name() -> None:
    c, r = new_client("scope_get.json", computer_name="dhcp02.example.local")
    c.get_scope("10.1.20.0")
    assert r.params["computer_name"] == "dhcp02.example.local"


def test_get_scope_not_found() -> None:
    c, _ = new_client("scope_not_found.json")
    with pytest.raises(NotFound, match="Get-DhcpServerv4Scope"):
        c.get_scope("10.9.9.0")


def test_runner_error_is_not_not_found() -> None:
    r = FixtureRunner(err=RunnerError("ssh: connection refused"))
    with pytest.raises(RunnerError) as ei:
        DhcpClient(r).get_scope("10.1.20.0")
    assert not isinstance(ei.value, NotFound)


def test_list_scopes() -> None:
    c, r = new_client("scope_list.json")
    got = c.list_scopes()
    assert len(got) == 2 and got[1].state == "Inactive" and got[1].lease_duration == "0.08:00:00"
    assert r.op == "scope.list"
    for fx in ("scope_list_empty.json", "ok_null.json"):
        assert new_client(fx)[0].list_scopes() == []


def test_add_set_scope_params() -> None:
    want = {
        "scope_id": "10.1.20.0",
        "name": "Users VLAN 120",
        "description": "",
        "start_range": "10.1.20.10",
        "end_range": "10.1.20.250",
        "subnet_mask": "255.255.255.0",
        "state": "Active",
        "type": "Dhcp",
        "lease_seconds": 28800,
        "delay": None,
        "max_bootp_clients": None,
        "activate_policies": None,
    }
    c, r = new_client("scope_get.json")
    c.add_scope(USERS)  # scope ID derived from start + mask
    assert r.op == "scope.add" and r.params == want
    assert "Add-DhcpServerv4Scope @cn @a" in r.script
    c.set_scope(USERS)
    assert r.op == "scope.set" and r.params == want
    c2, _ = new_client("ok_null.json")
    with pytest.raises(NotFound):
        c2.add_scope(USERS)


def test_scope_enum_casing_normalised() -> None:
    c, r = new_client("scope_get.json")
    c.set_scope(Scope(**{**USERS.to_dict(), "state": "Inactive", "type": "both", "delay": 100}))
    assert r.params["state"] == "InActive" and r.params["type"] == "Both" and r.params["delay"] == 100


def test_remove_scope() -> None:
    c, r = new_client("ok_null.json")
    c.remove_scope("10.1.20.0", force=True)
    assert r.op == "scope.remove" and r.params["force"] is True
    assert "-Force:([bool]$p.force) -Confirm:$false" in r.script
    c, _ = new_client("remove_with_leases.json")
    with pytest.raises(PSError, match="Remove-DhcpServerv4Scope") as ei:
        c.remove_scope("10.1.20.0")
    assert not isinstance(ei.value, NotFound)


def test_reservation() -> None:
    c, r = new_client("reservation_get.json")
    got = c.get_reservation("10.1.20.0", "10.1.20.5")
    assert got.client_id == "aa-bb-cc-dd-ee-ff" and got.name == "printer-01" and got.type == "Both"
    assert r.op == "reservation.get" and r.params == {"scope_id": "10.1.20.0", "ip_address": "10.1.20.5"}
    res = Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="AA:BB:CC:DD:EE:FF", type="both")
    c.add_reservation(res)
    assert r.op == "reservation.add" and r.params["client_id"] == "aa-bb-cc-dd-ee-ff" and r.params["type"] == "Both"
    c.set_reservation(res)
    assert r.op == "reservation.set" and "Set-DhcpServerv4Reservation @cn @a" in r.script
    # Issue #42: the current reservation is read first, so the client ID and type are only sent when they change.
    assert r.script.index("$cur = Get-WdReservation") < r.script.index("Set-DhcpServerv4Reservation @cn @a")
    assert r.params["client_id"] == "aa-bb-cc-dd-ee-ff"
    c.set_v6_reservation(ReservationV6("2001:db8:1::", "2001:db8:1::10", "00-01", 7))
    assert r.op == "v6reservation.set"
    assert r.script.index("$cur = Get-WdReservationV6") < r.script.index("Set-DhcpServerv6Reservation @cn @a")
    c.remove_reservation("10.1.20.5")
    assert r.op == "reservation.remove" and r.params == {"ip_address": "10.1.20.5"}
    c, _ = new_client("ok_null.json")
    with pytest.raises(NotFound):
        c.get_reservation("10.1.20.0", "10.1.20.5")
    with pytest.raises(ValueError):
        c.add_reservation(Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="nope"))


def test_exclusion_range() -> None:
    e = ExclusionRange("10.1.20.0", "10.1.20.1", "10.1.20.9")
    c, r = new_client("exclusion_get.json")
    assert c.get_exclusion_range(e) == e
    assert r.op == "exclusion.get" and r.params == e.to_dict()
    assert c.add_exclusion_range(e) == e and r.op == "exclusion.add"
    c.remove_exclusion_range(e)
    assert r.op == "exclusion.remove"
    c, _ = new_client("ok_null.json")
    with pytest.raises(NotFound):
        c.get_exclusion_range(e)


def test_option_value() -> None:
    c, r = new_client("option_get.json")
    k = OptionKey(option_id=6, scope_id="10.1.20.0")
    got = c.get_option_value(k)
    assert got.name == "DNS Servers" and got.value == ["10.0.0.53", "10.0.1.53"]
    assert r.op == "option.get"
    assert r.params == {
        "option_id": 6,
        "scope_id": "10.1.20.0",
        "reserved_ip": "",
        "vendor_class": "",
        "user_class": "",
        "policy_name": "",
    }
    c.set_option_value(k, ["10.0.0.53", "10.0.1.53"])
    assert r.op == "option.set" and r.params["value"] == ["10.0.0.53", "10.0.1.53"]
    assert "-Value ([string[]]@($p.value))" in r.script
    c.remove_option_value(OptionKey(option_id=6, reserved_ip="10.1.20.5", vendor_class="MSFT"))
    assert r.op == "option.remove" and r.params["reserved_ip"] == "10.1.20.5" and r.params["vendor_class"] == "MSFT"
    c, _ = new_client("err_option_not_present.json")
    with pytest.raises(NotFound):
        c.get_option_value(k)


def test_list_leases() -> None:
    c, r = new_client("lease_list.json")
    got = c.list_leases("10.1.20.0", all_leases=True)
    assert len(got) == 1 and got[0].host_name == "pc01.example.local"
    assert got[0].lease_expiry_time == "2026-10-09T08:00:00Z"
    assert r.op == "lease.list" and r.params == {"scope_id": "10.1.20.0", "all_leases": True}


def test_failover() -> None:
    c, r = new_client("failover_get.json")
    f = c.get_failover("dhcp01-dhcp02")
    assert f.mode == "HotStandby" and f.scope_ids == ["10.1.20.0", "10.1.30.0"] and f.enable_auth
    assert f.load_balance_percent is None and f.max_client_lead_time == 3600
    fr = FailoverRelationship(
        name="x", partner_server="dhcp02", mode="hotstandby", server_role="standby", scope_ids=["10.1.20.0"]
    )
    c.add_failover(fr, shared_secret="s3cret")
    assert r.op == "failover.add"
    assert r.params["mode"] == "HotStandby" and r.params["server_role"] == "Standby"
    assert r.params["shared_secret"] == "s3cret"
    assert "s3cret" not in r.script  # secrets travel in $p only
    with pytest.raises(ValueError, match="server_role"):
        c.add_failover(FailoverRelationship(name="x", partner_server="p", mode="HotStandby", scope_ids=["10.1.20.0"]))
    c.replicate_failover("x")
    assert r.op == "failover.replicate" and "Invoke-DhcpServerv4FailoverReplication @cn @a -Force" in r.script
    assert r.params == {"name": "x", "scope_ids": []}
    c.replicate_failover("x", ["10.1.20.0"])
    assert r.params == {"name": "", "scope_ids": ["10.1.20.0"]}
    # A single relationship serialised without its array wrapper still lists as one.
    c, _ = new_client("failover_list_single.json")
    got = c.list_failovers()
    assert len(got) == 1 and got[0].load_balance_percent == 50


def test_failover_load_balance_from_windows() -> None:
    # Scrubbed from a lab run on Windows Server 2022: in load balance mode ServerRole and ReservePercent are null,
    # and StateSwitchInterval is null when automatic switching is off.
    c, _ = new_client("failover_get_loadbalance.json")
    f = c.get_failover("dhcp01-dhcp02")
    assert f.mode == "LoadBalance" and f.server_role == "" and f.reserve_percent is None
    assert f.load_balance_percent == 60 and f.max_client_lead_time == 1800 and f.state_switch_interval is None


def test_failover_force_only_where_safe() -> None:
    # Add and Set ask for confirmation without -Force, which fails over WinRM. The removes do not ask in the Normal
    # state, and with the partner unreachable -Force would remove on this server only (lab, Windows Server 2022).
    def line(script):
        return next(x for x in script.body.splitlines() if "-DhcpServerv4Failover" in x and "Get-" not in x)

    for script in (wc.S_FAILOVER_ADD, wc.S_FAILOVER_SET):
        assert line(script).rstrip().endswith("-Force"), line(script)
    assert "-Force" in wc.S_FAILOVER_REPLICATE.body
    for script in (wc.S_FAILOVER_REMOVE, wc.S_FAILOVER_REMOVE_SCOPE):
        assert "-Force" not in line(script), line(script)


def test_server_names() -> None:
    # Lowercase, without the trailing dot of a fully qualified name; one name may come without its array.
    c, r = new_client("server_names.json")
    assert c.get_server_names() == ["dhcp1", "dhcp1.corp.example"]
    assert r.op == "server.names"
    c, r = new_client("ok_null.json", computer_name="dhcp9")
    assert c.get_server_names() == []
    assert r.params == {"computer_name": "dhcp9"}


def test_classes_optiondefs_superscopes_filters() -> None:
    c, r = new_client("class_list.json")
    classes = c.list_classes("vendor")
    assert r.params == {"type": "Vendor"} and classes[0].data == "MSFT 5.0"
    c, r = new_client("optiondef_list.json")
    defs = c.list_option_definitions()
    assert defs[0].multi_valued and defs[1].default_value == ["http://wpad/wpad.dat"]
    assert "-All" in r.script
    r.fixture = "ok_object.json"
    c.add_option_definition(OptionDefinition(option_id=252, name="WPAD", type="String"))
    assert r.op == "optiondef.add" and r.params["default_value"] == []
    c, r = new_client("superscope_list.json")
    assert c.list_superscopes() == [Superscope("Building A", ["10.1.20.0", "10.1.30.0"])]
    c, r = new_client("filter_list.json")
    assert c.list_filters("deny")[0].list == "Deny" and r.params == {"list": "Deny"}
    r.fixture = "ok_object.json"
    c.add_filter(MacFilter("AA:BB:CC:DD:EE:FF", "deny"))
    assert r.params["mac_address"] == "aa-bb-cc-dd-ee-ff"
    c.add_filter(MacFilter("AA-BB-CC-*", "Deny"))
    assert r.params["mac_address"] == "AA-BB-CC-*"  # wildcards pass through


def test_policy() -> None:
    c, r = new_client("policy_get.json")
    pol = c.get_policy("printers", "10.1.20.0")
    # Windows' own spelling of a MAC prefix (#21).
    assert pol.criteria == {"mac_address": ["EQ", "3c-d9-2b-*"]} and pol.processing_order == 1
    c.add_policy(Policy(name="p", scope_id="10.1.20.0", condition="or", criteria={"fqdn": ["EQ", "*.lab"]}))
    assert r.params["condition"] == "OR" and r.params["criteria"] == {"fqdn": ["EQ", "*.lab"]}
    with pytest.raises(ValueError, match="unknown criteria"):
        c.add_policy(Policy(name="p", criteria={"bogus": ["EQ", "x"]}))


def test_v6() -> None:
    c, r = new_client("v6scope_get.json")
    s = c.get_v6_scope("2001:DB8:1:0::")
    assert r.params == {"prefix": "2001:db8:1::"} and s.valid_lifetime == "12.00:00:00"
    c.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="x", t1="0.00:00:00"))
    assert r.params["preferred_seconds"] == 8 * 86400 and r.params["t1_seconds"] == 0
    c.add_v6_reservation(
        ReservationV6(prefix="2001:db8:1::", ip_address="2001:db8:1::10", client_duid="00:01:00:01:AB", iaid=7)
    )
    assert r.params["client_duid"] == "00-01-00-01-ab" and r.params["iaid"] == 7
    c.set_v6_option_value(OptionKeyV6(option_id=23, prefix="2001:db8:1::"), ["2001:db8::53"])
    assert r.op == "v6option.set" and "Set-DhcpServerv6OptionValue @cn @sel" in r.script


def test_every_script_splats_cn_and_has_unique_op() -> None:
    ops = [s.op for s in ALL_SCRIPTS]
    assert len(ops) == len(set(ops))
    cmdlet = re.compile(r"\b(Get|Set|Add|Remove|Invoke|Rename)-DhcpServer\w+")
    for s in ALL_SCRIPTS:
        for line in s.body.splitlines():
            if cmdlet.search(line):
                assert "@cn" in line, f"{s.op}: cmdlet without @cn: {line.strip()}"
            # User data only ever comes from $p, never from Python string formatting.
            assert "{p" not in line


def test_v6_classes() -> None:
    c, r = new_client("v6class_list.json")
    got = c.list_v6_classes()
    assert [(x.name, x.type) for x in got] == [("Microsoft Windows Options", "Vendor"), ("lab", "User")]
    assert r.op == "v6class.list"
    c, r = new_client("ok_object.json")
    c.add_v6_class(DhcpClass(name="lab", type="user", data="LabComputers"))
    assert r.op == "v6class.add" and r.params == {
        "name": "lab",
        "type": "User",
        "data": "LabComputers",
        "description": "",
    }
    assert "Add-DhcpServerv6Class @cn @a" in r.script
    c.set_v6_class(DhcpClass(name="lab", type="User", data="Lab2", description="d"))
    assert r.op == "v6class.set" and r.params["data"] == "Lab2"
    c.remove_v6_class("lab", "user")
    assert r.op == "v6class.remove" and r.params == {"name": "lab", "type": "User"}
    # A vendor class needs an enterprise number, which the plugin does not keep: only user classes are written.
    with pytest.raises(ValueError, match="user classes"):
        c.add_v6_class(DhcpClass(name="v", type="Vendor", data="x"))


def test_v6_exclusions_and_leases() -> None:
    c, r = new_client("v6exclusion_list.json")
    (e,) = c.list_v6_exclusion_ranges("2001:DB8:1::")
    assert e.start_range == "2001:db8:1::1" and r.params == {"prefix": "2001:db8:1::"}
    c, r = new_client("v6lease_list.json")
    (lease,) = c.list_v6_leases("2001:db8:1::")
    assert lease.iaid == 33554432 and lease.client_duid.startswith("00-01-00-01")
    assert lease.lease_expiry_time == "2026-10-20T08:00:00Z"
    assert r.op == "v6lease.list" and r.params == {"prefix": "2001:db8:1::"}


def test_v6_option_list_all() -> None:
    c, r = new_client("ok_null.json")
    c.list_v6_option_values(prefix="2001:db8:1::", all=True)
    assert r.op == "v6option.list" and r.params["all"] is True


# ---- issue #15: Windows PowerShell 5.1 printed {} for a missing object ----

GETTERS = {
    "scope": lambda c: c.get_scope("10.1.20.0"),
    "reservation": lambda c: c.get_reservation("10.1.20.0", "10.1.20.5"),
    "exclusion": lambda c: c.get_exclusion_range(ExclusionRange("10.1.20.0", "10.1.20.1", "10.1.20.9")),
    "option": lambda c: c.get_option_value(OptionKey(15, scope_id="10.1.20.0")),
    "reservation option": lambda c: c.get_option_value(OptionKey(15, "10.1.20.0", reserved_ip="10.1.20.5")),
    "option definition": lambda c: c.get_option_definition(252),
    "class": lambda c: c.get_class("phones", "User"),
    "failover": lambda c: c.get_failover("fo"),
    "superscope": lambda c: c.get_superscope("A"),
    "filter": lambda c: c.get_filter("aa-bb-cc-dd-ee-ff"),
    "filter lists": lambda c: c.get_filter_lists(),
    "policy": lambda c: c.get_policy("printers", "10.1.20.0"),
    "v6 scope": lambda c: c.get_v6_scope("2001:db8:1::"),
    "v6 reservation": lambda c: c.get_v6_reservation("2001:db8:1::", "2001:db8:1::10"),
    "v6 option": lambda c: c.get_v6_option_value(OptionKeyV6(23, prefix="2001:db8:1::")),
    "add read-back": lambda c: c.add_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Deny")),
    "set read-back": lambda c: c.set_class(DhcpClass("phones", "User", "voip")),
    "rename read-back": lambda c: c.rename_superscope("A", "B"),
}


@pytest.mark.parametrize("getter", GETTERS.values(), ids=GETTERS.keys())
def test_empty_object_is_not_found(getter) -> None:
    # The live shape of a missing object from a server before the envelope fix (ok_empty_object.json).
    c, _ = new_client("ok_empty_object.json")
    with pytest.raises(NotFound):
        getter(c)


def test_empty_object_is_not_a_list_item() -> None:
    c, _ = new_client("ok_empty_object.json")
    assert c.list_filters() == [] and c.list_superscopes() == []


# ---- issues #20 and #17: options on a reservation are named by the reserved IP alone ----


def test_reservation_option_sends_the_reserved_ip_alone() -> None:
    c, r = new_client("option_get.json")
    k = OptionKey(option_id=6, scope_id="10.1.20.0", reserved_ip="10.1.20.5")
    got = c.get_option_value(k)
    assert r.params["reserved_ip"] == "10.1.20.5" and r.params["scope_id"] == ""
    assert (got.scope_id, got.reserved_ip) == ("10.1.20.0", "10.1.20.5")  # kept as context
    got = c.set_option_value(k, ["10.0.0.53"])
    assert r.op == "option.set" and r.params["scope_id"] == "" and r.params["reserved_ip"] == "10.1.20.5"
    assert (got.scope_id, got.reserved_ip) == ("10.1.20.0", "10.1.20.5")
    c.remove_option_value(k)
    assert r.op == "option.remove" and r.params["scope_id"] == "" and r.params["reserved_ip"] == "10.1.20.5"
    c, r = new_client("option_get.json")
    (listed,) = c.list_option_values(scope_id="10.1.20.0", reserved_ip="10.1.20.5", all=True)
    assert r.op == "option.list" and r.params["scope_id"] == "" and r.params["all"] is True
    assert (listed.scope_id, listed.reserved_ip) == ("10.1.20.0", "10.1.20.5")
    # A scope value still names its scope.
    c.set_option_value(OptionKey(option_id=6, scope_id="10.1.20.0"), ["10.0.0.53"])
    assert r.params["scope_id"] == "10.1.20.0" and r.params["reserved_ip"] == ""
    # Even if both were passed, the script itself picks ReservedIP over ScopeId.
    assert "if ($p.reserved_ip) { $sel['ReservedIP'] = $p.reserved_ip }\nelseif ($p.scope_id)" in r.script


def test_v6_reservation_option_sends_the_reserved_ip_alone() -> None:
    c, r = new_client("ok_object.json")
    k = OptionKeyV6(option_id=23, prefix="2001:DB8:1::", reserved_ip="2001:db8:1::10")
    got = c.set_v6_option_value(k, ["2001:db8::53"])
    assert r.params["prefix"] == "" and r.params["reserved_ip"] == "2001:db8:1::10"
    assert (got.prefix, got.reserved_ip) == ("2001:db8:1::", "2001:db8:1::10")
    got = c.get_v6_option_value(k)
    assert r.op == "v6option.get" and r.params["prefix"] == ""
    assert (got.prefix, got.reserved_ip) == ("2001:db8:1::", "2001:db8:1::10")
    c.remove_v6_option_value(k)
    assert r.op == "v6option.remove" and r.params["prefix"] == "" and r.params["reserved_ip"] == "2001:db8:1::10"
    c.set_v6_option_value(OptionKeyV6(option_id=23, prefix="2001:db8:1::"), ["2001:db8::53"])
    assert r.params["prefix"] == "2001:db8:1::"
    assert "if ($p.reserved_ip) { $sel['ReservedIP'] = $p.reserved_ip }\nelseif ($p.prefix)" in r.script


# ---- issue #14 and #22 ----


def test_superscope_rename_uses_name_and_new_name() -> None:
    assert "Rename-DhcpServerv4Superscope @cn -Name $p.name -NewName $p.new_name" in S_SUPERSCOPE_RENAME.body
    assert "-SuperscopeName" not in S_SUPERSCOPE_RENAME.body.split("Rename-DhcpServerv4Superscope", 1)[1]


def test_v6_leases_have_no_state() -> None:
    # Get-DhcpServerv6Lease has no AddressState on Windows Server 2025; AddressType is not a state.
    assert "AddressState" not in S_V6LEASE_LIST.body and "AddressType" not in S_V6LEASE_LIST.body
    c, _ = new_client("v6lease_list.json")
    (lease,) = c.list_v6_leases("2001:db8:1::")
    assert "address_state" not in lease.to_dict()
