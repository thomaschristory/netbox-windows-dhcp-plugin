"""IPv4 lab tests. Each one settles an ``# unverified:`` marker or a known question by a round trip on the real server.

A round trip writes an object through the client, reads it back and compares every field: a wrong output property
name shows up as an empty field. Raw property dumps go to .lab/probe/ (gitignored) to help fix what fails.
"""

from __future__ import annotations

import pytest

from netbox_windows_dhcp_plugin.sync import windows_state
from netbox_windows_dhcp_plugin.sync.diff import Change
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import client as wc
from netbox_windows_dhcp_plugin.windows import normalize
from netbox_windows_dhcp_plugin.windows.psscript import PSError

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
    """windows_state.py: how an unlimited lease is reported. Tries both spellings of "unlimited" and records each."""
    s = lab.scope("nbwd-test-unlimited")
    sid = {"s": s.scope_id}
    seen = {}
    for label, expr in (("maxvalue", "[TimeSpan]::MaxValue"), ("uint32", "[TimeSpan]::FromSeconds(4294967295)")):
        try:
            lab.raw("probe.unlimited", f"Set-DhcpServerv4Scope -ScopeId $p.s -LeaseDuration ({expr})", sid)
        except wc.PSError as exc:
            seen[label] = f"refused: {exc}"
            continue
        dump = lab.dump("probe.scope", "Get-DhcpServerv4Scope -ScopeId $p.s", sid)
        seen[label] = {"raw": dump[0]["LeaseDuration"], "client": lab.client.get_scope(s.scope_id).lease_duration}
    save_probe(probe_out, "v4-scope-unlimited", seen)
    assert any(isinstance(v, dict) for v in seen.values()), seen


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

    # An option on the reservation: Windows takes -ReservedIP alone on Set and Remove (issue #20); the scope stays in
    # what the client returns. A missing value is NotFound, not an empty one (issue #15).
    k = wc.OptionKey(15, s.scope_id, reserved_ip=r.ip_address)
    with pytest.raises(wc.NotFound):
        lab.client.get_option_value(k)
    ov = lab.client.set_option_value(k, ["host.example"])
    assert (ov.value, ov.scope_id, ov.reserved_ip) == (["host.example"], s.scope_id, r.ip_address)
    assert lab.client.get_option_value(k).value == ["host.example"]
    listed = lab.client.list_option_values(scope_id=s.scope_id, reserved_ip=r.ip_address, all=True)
    assert [(x.option_id, x.value) for x in listed] == [(15, ["host.example"])]
    lab.client.remove_option_value(k)
    with pytest.raises(wc.NotFound):
        lab.client.get_option_value(k)


def test_reservation_update_keeps_its_option_values(lab: Lab, probe_out) -> None:
    """Issue #42: client.py sends the client ID and type only on change; whether a new client ID drops the values."""
    s = lab.scope("nbwd-test-res42")
    base = s.scope_id.rsplit(".", 1)[0]
    r = lab.client.add_reservation(
        wc.Reservation(s.scope_id, f"{base}.20", MAC_PREFIX + "00-00-42", "nbwd-test-host42", "lab", "Dhcp")
    )
    k = wc.OptionKey(15, s.scope_id, reserved_ip=r.ip_address)
    lab.client.set_option_value(k, ["host.example"])
    r.description = "changed"
    assert lab.client.set_reservation(r).description == "changed"
    assert lab.client.get_option_value(k).value == ["host.example"]

    # A new client ID is sent to Windows; record whether the value survives, to settle the unverified note.
    r.client_id = MAC_PREFIX + "00-00-43"
    assert lab.client.set_reservation(r).client_id.lower() == MAC_PREFIX + "00-00-43"
    left = lab.client.list_option_values(scope_id=s.scope_id, reserved_ip=r.ip_address, all=True)
    save_probe(probe_out, "v4-reservation-identity-change-options", [x.to_dict() for x in left])

    # Whatever Windows does, the push writer puts the value back.
    lab.client.set_option_value(k, ["host.example"])
    before = {
        "scope_id": s.scope_id,
        "ip_address": r.ip_address,
        "client_id": r.client_id,
        "name": r.name,
        "description": "changed",
        "reservation_type": "dhcp",
    }
    change = Change("update", "reservation", r.ip_address, before=before, after={**before, "description": "again"})
    WindowsWriter(lab.client).apply(change)
    assert lab.client.get_option_value(k).value == ["host.example"]


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


def test_fqdn_policy_round_trip(lab: Lab, probe_out) -> None:
    """client.py: policy output property names with an FQDN condition, created, updated and read back.

    Windows takes a wildcard only at the start or the end of a condition ("Host*.Example.Test" is refused, issue #18)
    and lowercases an FQDN condition on read (issue #21).
    """
    s = lab.scope("nbwd-test-fqdnpol")
    base = s.scope_id.rsplit(".", 1)[0]
    p = lab.policy(
        wc.Policy(
            name="nbwd-test-fqdn-policy",
            scope_id=s.scope_id,
            description="lab",
            enabled=True,
            condition="OR",
            criteria={"fqdn": ["EQ", "*.Example.Test"]},
            lease_duration="1.00:00:00",
        )
    )
    save_probe(
        probe_out,
        "v4-policy-fqdn",
        lab.dump("probe.pol", "Get-DhcpServerv4Policy -ScopeId $p.s -Name $p.n", {"s": s.scope_id, "n": p.name}),
    )
    save_probe(probe_out, "v4-policy-fqdn-client", p.to_dict())
    assert p.description == "lab"
    assert p.condition == "OR"
    assert p.processing_order == 1
    assert normalize.parse_duration(p.lease_duration) == 86400
    assert p.criteria["fqdn"][0] == "EQ"
    # Windows writes host names back in lowercase (#21); the sync must see that as no change.
    assert p.criteria["fqdn"] == ["EQ", "*.example.test"]
    assert windows_state.equal("policy", "criteria", {"fqdn": ["EQ", "*.Example.Test"]}, p.criteria)
    assert lab.client.get_policy(p.name, s.scope_id).criteria == p.criteria

    p.enabled = False
    p.description = "changed"
    after = lab.client.set_policy(p)
    assert after.enabled is False and after.description == "changed"

    # Windows refuses IP ranges (DHCP 20135) and ordinary option values (DHCP 20136) on an FQDN policy: that is a
    # Windows rule, not a transport failure. Ranges and values are covered by the MAC policy test below.
    with pytest.raises(wc.PSError) as ei:
        lab.client.add_policy_ip_range(wc.PolicyIPRange(s.scope_id, p.name, f"{base}.50", f"{base}.60"))
    assert ei.value.id.startswith("DHCP 20135"), ei.value.to_dict()
    with pytest.raises(wc.PSError) as ei:
        lab.client.set_option_value(wc.OptionKey(15, s.scope_id, policy_name=p.name), ["policy.example"])
    assert ei.value.id.startswith("DHCP 20136"), ei.value.to_dict()


def test_policy_embedded_wildcard_is_refused(lab: Lab) -> None:
    """The fake refuses "Host*.Example.Test" because Windows does (issue #18): keep the two in step."""
    s = lab.scope("nbwd-test-badpol")
    name = "nbwd-test-bad-policy"
    with pytest.raises(wc.PSError, match="Wildcard is valid either at the beginning or at the end"):
        lab.policy(wc.Policy(name=name, scope_id=s.scope_id, criteria={"fqdn": ["EQ", "Host*.Example.Test"]}))
    with pytest.raises(wc.NotFound):
        lab.client.get_policy(name, s.scope_id)


def test_mac_policy_ranges_and_options(lab: Lab, probe_out) -> None:
    """client.py: policy IP ranges and -PolicyName on option values, on a MAC-only policy (issue #18)."""
    s = lab.scope("nbwd-test-pol")
    base = s.scope_id.rsplit(".", 1)[0]
    p = lab.policy(
        wc.Policy(
            name="nbwd-test-policy",
            scope_id=s.scope_id,
            description="lab",
            condition="OR",
            criteria={"mac_address": ["EQ", "02-4e-42-ab*"]},
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
    assert p.processing_order == 1
    assert normalize.parse_duration(p.lease_duration) == 86400
    # Windows stores conditions in its own spelling (#21), which the sync must compare as no change.
    assert p.criteria["mac_address"] == ["EQ", "02-4e-42-ab-*"]
    assert windows_state.equal("policy", "criteria", {"mac_address": ["EQ", "02-4e-42-ab*"]}, p.criteria)

    r = lab.client.add_policy_ip_range(wc.PolicyIPRange(s.scope_id, p.name, f"{base}.50", f"{base}.60"))
    assert (r.start_range, r.end_range) == (f"{base}.50", f"{base}.60")
    assert lab.client.list_policy_ip_ranges(s.scope_id, p.name) == [r]

    # -PolicyName on option values.
    k = wc.OptionKey(15, s.scope_id, policy_name=p.name)
    ov = lab.client.set_option_value(k, ["policy.example"])
    assert ov.value == ["policy.example"] and ov.policy_name == p.name
    # Windows also lists the policy's lease time as option 51 (issue #29), which the sync skips.
    values = lab.client.list_option_values(scope_id=s.scope_id, policy_name=p.name)
    listed = {x.option_id: x.value for x in values}
    assert len(listed) == len(values)  # one entry per option
    assert listed.pop(15) == ["policy.example"]
    assert set(listed) <= {51}
    if 51 in listed:
        assert listed[51] == ["86400"]
    lab.client.remove_option_value(k)
    with pytest.raises(wc.NotFound):
        lab.client.get_option_value(k)
    assert 15 not in {x.option_id for x in lab.client.list_option_values(scope_id=s.scope_id, policy_name=p.name)}

    lab.client.remove_policy_ip_range(r)
    assert lab.client.list_policy_ip_ranges(s.scope_id, p.name) == []

    p.enabled = False
    p.description = "changed"
    after = lab.client.set_policy(p)
    assert after.enabled is False and after.description == "changed"


def test_user_class_policy_criteria(lab: Lab) -> None:
    """client.py: the user class condition's property name."""
    s = lab.scope("nbwd-test-ucpol")
    u = lab.user_class("nbwd-test-pol-user", "nbwd-pol")
    p = lab.policy(
        wc.Policy(
            name="nbwd-test-uc-policy",
            scope_id=s.scope_id,
            condition="AND",
            criteria={"mac_address": ["EQ", "02-4e-42-ac*"], "user_class": ["EQ", u.name]},
        )
    )
    assert p.condition == "AND"
    assert p.criteria["user_class"] == ["EQ", u.name]


def test_policy_processing_order(lab: Lab) -> None:
    """push.py and fake.py: policies of a level are numbered 1, 2, 3... (#24); settles Set and removal renumbering."""
    s = lab.scope("nbwd-test-polord")
    crit = {"mac_address": ["EQ", "02-4e-42-ef*"]}

    def add(name: str, order: int) -> wc.Policy:
        return lab.policy(wc.Policy(name=name, scope_id=s.scope_id, criteria=crit, processing_order=order))

    def orders() -> list[tuple[str, int | None]]:
        return [(p.name, p.processing_order) for p in lab.client.list_policies(s.scope_id)]

    with pytest.raises(PSError) as err:
        add("nbwd-test-pol-b", 2)
    assert err.value.id.startswith("DHCP 20110")
    add("nbwd-test-pol-b", 1)
    add("nbwd-test-pol-c", 2)
    add("nbwd-test-pol-a", 1)
    assert orders() == [("nbwd-test-pol-a", 1), ("nbwd-test-pol-b", 2), ("nbwd-test-pol-c", 3)]
    # unverified in fake.py: Set moves a policy and renumbers those in between, and refuses one past the end.
    lab.client.set_policy(wc.Policy(name="nbwd-test-pol-a", scope_id=s.scope_id, processing_order=3))
    assert orders() == [("nbwd-test-pol-b", 1), ("nbwd-test-pol-c", 2), ("nbwd-test-pol-a", 3)]
    with pytest.raises(PSError):
        lab.client.set_policy(wc.Policy(name="nbwd-test-pol-a", scope_id=s.scope_id, processing_order=4))
    # unverified in fake.py: removing a policy closes the gap.
    lab.client.remove_policy("nbwd-test-pol-b", s.scope_id)
    assert orders() == [("nbwd-test-pol-c", 1), ("nbwd-test-pol-a", 2)]


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


def test_scope_range_shifts(lab: Lab, probe_out) -> None:
    """client.py (#30): range changes that widen one end and narrow the other, in both directions, and a move."""
    s = lab.scope("nbwd-test-shift")
    base = s.scope_id.rsplit(".", 1)[0]
    sid = {"s": s.scope_id, "a": f"{base}.5", "b": f"{base}.150"}
    # One plain Set that widens the start and narrows the end: record whether Windows refuses it (DHCP 20023).
    seen: dict[str, str] = {}
    try:
        lab.raw("probe.shift", "Set-DhcpServerv4Scope -ScopeId $p.s -StartRange $p.a -EndRange $p.b", sid)
        seen["start_widens_end_narrows"] = "accepted"
    except PSError as exc:
        seen["start_widens_end_narrows"] = f"refused: {exc.id}"
    save_probe(probe_out, "v4-scope-range-shift", seen)

    def shift(start: str, end: str) -> None:
        got = lab.client.get_scope(s.scope_id)
        got.start_range, got.end_range = f"{base}.{start}", f"{base}.{end}"
        after = lab.client.set_scope(got)
        assert (after.start_range, after.end_range) == (f"{base}.{start}", f"{base}.{end}")

    shift("10", "200")  # back to the start, whatever the probe did
    shift("5", "150")  # start widens, end narrows
    shift("20", "220")  # start narrows, end widens
    shift("230", "250")  # no overlap with the old range


def test_policy_mac_prefix_case(lab: Lab, probe_out) -> None:
    """fake.py: Windows reads an uppercase MAC prefix back in lowercase."""
    s = lab.scope("nbwd-test-maccase")
    p = lab.policy(
        wc.Policy(name="nbwd-test-maccase", scope_id=s.scope_id, criteria={"mac_address": ["EQ", "02-4E-42-AB*"]})
    )
    save_probe(probe_out, "v4-policy-mac-case", p.to_dict())
    assert p.criteria["mac_address"] == ["EQ", "02-4e-42-ab-*"]
