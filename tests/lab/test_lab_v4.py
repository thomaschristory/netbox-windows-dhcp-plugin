"""IPv4 lab tests. Each one settles an ``# unverified:`` marker or a known question by a round trip on the real server.

A round trip writes an object through the client, reads it back and compares every field: a wrong output property
name shows up as an empty field. Raw property dumps go to .lab/probe/ (gitignored) to help fix what fails.
"""

from __future__ import annotations

import pytest

from netbox_windows_dhcp_plugin.windows import client as wc
from netbox_windows_dhcp_plugin.windows import normalize

from .conftest import save_probe
from .labkit import MAC_PREFIX, Lab


def test_scope_round_trip_with_extras(lab: Lab, probe_out) -> None:
    """client.py: Delay, MaxBootpClients, ActivatePolicies and SuperscopeName on scopes."""
    s = lab.scope(
        "nbwd-test-scope",
        description="lab",
        lease_duration="2.03:04:05",
        delay=250,
        max_bootp_clients=17,
        activate_policies=False,
        type="Both",
    )
    save_probe(probe_out, "v4-scope", lab.dump("probe.scope", "Get-DhcpServerv4Scope -ScopeId $p.s", {"s": s.scope_id}))
    got = lab.client.get_scope(s.scope_id)
    assert got.name == "nbwd-test-scope"
    assert got.description == "lab"
    assert normalize.parse_duration(got.lease_duration) == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert got.delay == 250
    assert got.max_bootp_clients == 17
    assert got.activate_policies is False
    assert got.type == "Both"
    assert got.superscope_name == ""

    got.state = "InActive"
    got.delay = 0
    got.activate_policies = True
    after = lab.client.set_scope(got)
    assert after.state.lower() == "inactive"
    assert after.delay == 0
    assert after.activate_policies is True


def test_unlimited_lease(lab: Lab, probe_out) -> None:
    """windows_state.py: how an unlimited lease is reported."""
    s = lab.scope("nbwd-test-unlimited")
    # The DHCP console's "Unlimited" is 0xFFFFFFFF seconds.
    lab.raw(
        "probe.unlimited",
        "Set-DhcpServerv4Scope -ScopeId $p.s -LeaseDuration ([TimeSpan]::FromSeconds(4294967295))",
        {"s": s.scope_id},
    )
    save_probe(
        probe_out,
        "v4-scope-unlimited",
        lab.dump("probe.scope", "Get-DhcpServerv4Scope -ScopeId $p.s", {"s": s.scope_id}),
    )
    got = lab.client.get_scope(s.scope_id)
    save_probe(probe_out, "v4-scope-unlimited-client", got.to_dict())
    assert got.lease_duration, "an unlimited lease must still read back as something"


def test_superscope_lifecycle(lab: Lab, probe_out) -> None:
    """client.py: superscope output shape; Remove -ScopeId removing some members; Remove leaves the scopes."""
    a = lab.scope("nbwd-test-ss-a")
    b = lab.scope("nbwd-test-ss-b")
    ss = lab.superscope(wc.Superscope(name="nbwd-test-super", scope_ids=[a.scope_id, b.scope_id]))
    save_probe(
        probe_out,
        "v4-superscope",
        lab.dump("probe.ss", "Get-DhcpServerv4Superscope -SuperscopeName $p.n", {"n": ss.name}),
    )
    assert sorted(ss.scope_ids) == sorted([a.scope_id, b.scope_id])
    assert lab.client.get_scope(a.scope_id).superscope_name == "nbwd-test-super"

    # Drop one member.
    after = lab.client.set_superscope(wc.Superscope(name=ss.name, scope_ids=[a.scope_id]))
    assert after.scope_ids == [a.scope_id]
    assert lab.client.get_scope(b.scope_id).superscope_name == ""

    renamed = lab.client.rename_superscope(ss.name, "nbwd-test-super2")
    assert renamed.name == "nbwd-test-super2"
    lab.on_cleanup("superscope nbwd-test-super2", lambda: lab.remove_superscope("nbwd-test-super2"))

    lab.remove_superscope("nbwd-test-super2")
    assert all(s.name != "nbwd-test-super2" for s in lab.client.list_superscopes())
    # The scopes survive their superscope.
    assert lab.client.get_scope(a.scope_id).name == "nbwd-test-ss-a"


def test_reservation_and_exclusion(lab: Lab) -> None:
    s = lab.scope("nbwd-test-res")
    base = s.scope_id.rsplit(".", 1)[0]
    r = lab.client.add_reservation(
        wc.Reservation(
            scope_id=s.scope_id,
            ip_address=f"{base}.20",
            client_id=MAC_PREFIX + "00-00-01",
            name="nbwd-test-host",
            description="lab",
            type="Dhcp",
        )
    )
    assert r.client_id.lower() == MAC_PREFIX + "00-00-01"
    assert r.name == "nbwd-test-host" and r.type == "Dhcp"
    r.description = "changed"
    assert lab.client.set_reservation(r).description == "changed"

    e = lab.client.add_exclusion_range(wc.ExclusionRange(s.scope_id, f"{base}.100", f"{base}.110"))
    assert (e.start_range, e.end_range) == (f"{base}.100", f"{base}.110")
    assert lab.client.list_exclusion_ranges(s.scope_id) == [e]


def test_classes_and_class_data(lab: Lab, probe_out) -> None:
    """client.py: whether the ASCII class data is in .AsciiData or .Data."""
    u = lab.user_class("nbwd-test-user", "nbwd-user-data", "lab user class")
    v = lab.vendor_class("nbwd-test-vendor", "nbwd-vendor-data")
    save_probe(
        probe_out,
        "v4-classes",
        lab.dump("probe.class", "Get-DhcpServerv4Class | Where-Object { $_.Name -like 'nbwd-test-*' }"),
    )
    assert u.data == "nbwd-user-data" and u.description == "lab user class"
    assert v.data == "nbwd-vendor-data" and v.type == "Vendor"
    u.description = "changed"
    assert lab.client.set_class(u).description == "changed"


def test_vendor_option_definition_and_values(lab: Lab, probe_out) -> None:
    """client.py: vendor option definitions (-All on Get, Set with Name/Description/DefaultValue)."""
    v = lab.vendor_class("nbwd-test-vdef", "nbwd-vdef")
    oid = lab.free_option_id(v.name)
    d = lab.option_definition(
        wc.OptionDefinition(
            option_id=oid,
            name="nbwd-test-vopt",
            type="String",
            description="lab",
            vendor_class=v.name,
            default_value=["x"],
        )
    )
    assert d.vendor_class == v.name and d.default_value == ["x"]
    assert any(x.option_id == oid and x.vendor_class == v.name for x in lab.client.list_option_definitions())
    d.description = "changed"
    d.default_value = ["y"]
    after = lab.client.set_option_definition(d)
    assert after.description == "changed" and after.default_value == ["y"]
    save_probe(
        probe_out,
        "v4-optiondefs-vendor",
        lab.dump("probe.od", "Get-DhcpServerv4OptionDefinition -VendorClass $p.v", {"v": v.name}),
    )


def test_option_values_all_returns_class_values(lab: Lab, probe_out) -> None:
    """windows_state.py: does -All return the values of every user and vendor class?"""
    s = lab.scope("nbwd-test-all")
    u = lab.user_class("nbwd-test-all-user", "nbwd-all-user")
    v = lab.vendor_class("nbwd-test-all-vendor", "nbwd-all-vendor")
    oid = lab.free_option_id(v.name)
    lab.option_definition(
        wc.OptionDefinition(option_id=oid, name="nbwd-test-all-vopt", type="String", vendor_class=v.name)
    )

    lab.client.set_option_value(wc.OptionKey(15, s.scope_id), ["plain.example"])
    lab.client.set_option_value(wc.OptionKey(15, s.scope_id, user_class=u.name), ["user.example"])
    lab.client.set_option_value(wc.OptionKey(oid, s.scope_id, vendor_class=v.name), ["vendor"])

    values = lab.client.list_option_values(scope_id=s.scope_id, all=True)
    save_probe(probe_out, "v4-options-all", [x.to_dict() for x in values])
    seen = {(x.option_id, x.user_class, x.vendor_class): x.value for x in values}
    assert seen.get((15, "", "")) == ["plain.example"]
    assert seen.get((15, u.name, "")) == ["user.example"]
    assert seen.get((oid, "", v.name)) == ["vendor"]


def test_policy_round_trip(lab: Lab, probe_out) -> None:
    """client.py: policy output property names; does Windows change the case of criteria?"""
    s = lab.scope("nbwd-test-pol")
    base = s.scope_id.rsplit(".", 1)[0]
    u = lab.user_class("nbwd-test-pol-user", "nbwd-pol")
    p = lab.policy(
        wc.Policy(
            name="nbwd-test-policy",
            scope_id=s.scope_id,
            description="lab",
            enabled=True,
            condition="AND",
            criteria={
                "mac_address": ["EQ", "02-4e-42-ab*"],
                "user_class": ["EQ", u.name],
                "fqdn": ["EQ", "Host*.Example.Test"],
            },
            lease_duration="1.00:00:00",
        )
    )
    save_probe(
        probe_out,
        "v4-policy",
        lab.dump("probe.pol", "Get-DhcpServerv4Policy -ScopeId $p.s -Name $p.n", {"s": s.scope_id, "n": p.name}),
    )
    save_probe(probe_out, "v4-policy-client", p.to_dict())
    assert p.description == "lab"
    assert p.condition == "AND"
    assert p.processing_order == 1
    assert normalize.parse_duration(p.lease_duration) == 86400
    assert p.criteria["user_class"] == ["EQ", u.name]
    assert p.criteria["mac_address"][0] == "EQ"
    assert p.criteria["mac_address"][1].lower() == "02-4e-42-ab*"
    assert p.criteria["fqdn"][1].lower() == "host*.example.test"

    r = lab.client.add_policy_ip_range(wc.PolicyIPRange(s.scope_id, p.name, f"{base}.50", f"{base}.60"))
    assert (r.start_range, r.end_range) == (f"{base}.50", f"{base}.60")

    # -PolicyName on option values.
    ov = lab.client.set_option_value(wc.OptionKey(15, s.scope_id, policy_name=p.name), ["policy.example"])
    assert ov.value == ["policy.example"] and ov.policy_name == p.name
    assert [x.value for x in lab.client.list_option_values(scope_id=s.scope_id, policy_name=p.name)] == [
        ["policy.example"]
    ]
    lab.client.remove_option_value(wc.OptionKey(15, s.scope_id, policy_name=p.name))

    p.enabled = False
    p.description = "changed"
    after = lab.client.set_policy(p)
    assert after.enabled is False and after.description == "changed"


def test_server_level_policy_is_listed(lab: Lab) -> None:
    p = lab.policy(wc.Policy(name="nbwd-test-srvpol", enabled=False, criteria={"mac_address": ["EQ", "02-4e-42-cd*"]}))
    assert p.scope_id == ""
    assert any(x.name == p.name for x in lab.client.list_policies())


def test_mac_filter(lab: Lab, probe_out) -> None:
    """client.py: Remove-DhcpServerv4Filter accepting -MacAddress without -List."""
    mac = MAC_PREFIX + "00-00-99"
    f = lab.mac_filter(wc.MacFilter(mac_address=mac, list="Deny", description="nbwd-test-filter"))
    assert f.mac_address.lower() == mac and f.list == "Deny"
    f.list = "Allow"
    f.description = "nbwd-test-changed"
    after = lab.client.set_filter(f)
    assert after.list == "Allow" and after.description == "nbwd-test-changed"
    lab.remove_filter(mac)
    with pytest.raises(wc.NotFound):
        lab.client.get_filter(mac)
    # Lists: write back what is there, never toggle (that is server-wide).
    state = lab.client.get_filter_lists()
    assert lab.client.set_filter_lists(state.allow, state.deny) == state


def test_leases(lab: Lab, probe_out) -> None:
    s = lab.scope("nbwd-test-lease")
    base = s.scope_id.rsplit(".", 1)[0]
    lab.raw(
        "probe.lease",
        "Add-DhcpServerv4Lease -ScopeId $p.s -IPAddress $p.ip -ClientId $p.mac -HostName 'nbwd-test-lease' "
        "-LeaseExpiryTime ((Get-Date).AddDays(1))",
        {"s": s.scope_id, "ip": f"{base}.30", "mac": MAC_PREFIX + "00-00-30"},
    )
    leases = lab.client.list_leases(s.scope_id)
    save_probe(probe_out, "v4-leases", [x.to_dict() for x in leases])
    assert len(leases) == 1
    lease = leases[0]
    assert lease.ip_address == f"{base}.30"
    assert lease.client_id.lower() == MAC_PREFIX + "00-00-30"
    assert lease.host_name == "nbwd-test-lease"
    assert lease.address_state.startswith("Active")
    assert lease.lease_expiry_time.endswith("Z")


def test_failover_read_only(lab: Lab, probe_out) -> None:
    """Single-server lab: only check that the list call works. Writes need a partner."""
    rels = lab.client.list_failovers()
    save_probe(probe_out, "v4-failover", [r.to_dict() for r in rels])
