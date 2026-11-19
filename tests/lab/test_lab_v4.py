"""IPv4 lab tests. Each one settles an ``# unverified:`` marker or a known question by a round trip on the real server.

A round trip writes an object through the client, reads it back and compares every field: a wrong output property
name shows up as an empty field. Raw property dumps go to .lab/probe/ (gitignored) to help fix what fails.
"""

from __future__ import annotations

import time

import pytest

from netbox_windows_dhcp_plugin.conf import DEFAULTS
from netbox_windows_dhcp_plugin.sync import windows_state
from netbox_windows_dhcp_plugin.sync.diff import Change
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import client as wc
from netbox_windows_dhcp_plugin.windows import normalize
from netbox_windows_dhcp_plugin.windows.psscript import PSError

from .conftest import save_probe
from .labkit import MAC_PREFIX, Lab, lab_env


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


def test_reservation_type_change_alone(lab: Lab, probe_out) -> None:
    """fake.py (#56): whether a new reservation type, with the same client ID, drops the reservation's option values."""
    s = lab.scope("nbwd-test-res56")
    base = s.scope_id.rsplit(".", 1)[0]
    r = lab.client.add_reservation(
        wc.Reservation(s.scope_id, f"{base}.20", MAC_PREFIX + "00-00-56", "nbwd-test-host56", "lab", "Dhcp")
    )
    k = wc.OptionKey(15, s.scope_id, reserved_ip=r.ip_address)
    lab.client.set_option_value(k, ["host.example"])
    r.type = "Both"
    assert lab.client.set_reservation(r).type == "Both"
    left = lab.client.list_option_values(scope_id=s.scope_id, reserved_ip=r.ip_address, all=True)
    save_probe(probe_out, "v4-reservation-type-change-options", [x.to_dict() for x in left])
    # Dropped, as for a new client ID (lab, Windows Server 2022); the push writer puts them back.
    assert left == []


def test_policy_set_without_criteria(lab: Lab, probe_out) -> None:
    """client.py (#56): whether Set-DhcpServerv4Policy leaves the conditions it is not given unchanged."""
    s = lab.scope("nbwd-test-polset")
    criteria = {"mac_address": ["EQ", "02-4e-42-ad*"], "fqdn": ["EQ", "*.example.test"]}
    p = lab.policy(wc.Policy(name="nbwd-test-polset", scope_id=s.scope_id, condition="AND", criteria=criteria))
    lab.raw(
        "probe.polset",
        "Set-DhcpServerv4Policy -Name $p.n -ScopeId $p.s -Description 'changed'; $out = $true",
        {"n": p.name, "s": s.scope_id},
    )
    after = lab.client.get_policy(p.name, s.scope_id)
    save_probe(probe_out, "v4-policy-set-without-criteria", after.to_dict())
    # Left as they were (lab, Windows Server 2022).
    assert after.description == "changed"
    assert after.criteria == {"mac_address": ["EQ", "02-4e-42-ad-*"], "fqdn": ["EQ", "*.example.test"]}
    assert after.condition == "AND"


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


def test_server_names(lab: Lab, probe_out) -> None:
    """client.py server.names (#57, #62): what the DHCP server reports as its own names, and that a failover partner
    naming it by its fully qualified or its short name finds it. On a domain member the DNS name is the FQDN; through
    -ComputerName the names are the DHCP server's, never the management host's."""
    env = lab_env()
    computer = env.get("WINDOWS_DHCP_LAB_COMPUTER_NAME", "")
    target = (computer or env["WINDOWS_DHCP_LAB_HOST"]).lower().rstrip(".")
    names = lab.client.get_server_names()
    save_probe(probe_out, "v4-server-names", {"target": target, "names": names})
    assert names
    if "." in target and not target.replace(".", "").isdigit():
        # Connected by DNS name: Windows must report that name back, or a partner given it would not match.
        assert target in names
        placeholder = "netbox-host.invalid"
        for spelling in (target, target.split(".", 1)[0]):
            assert windows_state.partner_matches(spelling, [(placeholder, names)]) == [placeholder], spelling
    if computer:
        management = env["WINDOWS_DHCP_LAB_HOST"].lower().split(".", 1)[0]
        assert management not in {n.split(".", 1)[0] for n in names}


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


def test_secret_stays_out_of_the_script_block(lab: Lab, probe_out) -> None:
    """psscript.py BOOTSTRAP (#104): a secret reaches $p on Windows PowerShell 5.1 but not the compiled script.

    When script block logging is on, the event log is searched for the marker too. When it is off, that part checks
    nothing: the probe file records ``logging`` and whether the log could be read.

    The marker has no quote character: render() doubles quotes, so a marker with one would not be found in the
    script text even if a regression put it there.
    """
    import secrets as pysecrets

    marker = "nbwd-" + pysecrets.token_hex(8) + ".:"
    got = lab.raw(
        "probe.secret",
        r"""
$sbl = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell\ScriptBlockLogging' `
    -ErrorAction SilentlyContinue
$readable = $true
try { $null = Get-WinEvent -LogName 'Microsoft-Windows-PowerShell/Operational' -MaxEvents 1 }
catch { $readable = $false }
$logged = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-PowerShell/Operational'; Id = 4104;
    StartTime = (Get-Date).AddMinutes(-5) } -ErrorAction SilentlyContinue |
    Where-Object { $_.Message.Contains($p.shared_secret) }).Count
$out = @{ secret = $p.shared_secret; in_src = $src.Contains($p.shared_secret);
    ps = "$($PSVersionTable.PSVersion)"; logging = [bool]($sbl -and $sbl.EnableScriptBlockLogging); logged = $logged;
    log_readable = $readable }
""",
        {"shared_secret": marker},
    )
    save_probe(probe_out, "secret-logging", {k: v for k, v in got.items() if k != "secret"})
    assert got["secret"] == marker and got["in_src"] is False, got
    assert got["logged"] == 0, got


def _values(values: list[wc.OptionValue]) -> list[tuple]:
    return sorted((o.option_id, tuple(o.value), o.vendor_class, o.user_class, o.policy_name) for o in values)


def _error_of(fn) -> dict:
    """The exact error a call raises (id, category, command), recorded so NOT_FOUND_IDS can be checked against it."""
    try:
        fn()
    except PSError as e:
        return {**e.to_dict(), "not_found": e.not_found}
    except wc.NotFound as e:
        return {"message": str(e), "not_found": True}
    return {"raised": False}


def test_batched_reads_match_single_reads(lab: Lab, probe_out) -> None:
    """
    client.py (issue #100): option.list_reserved and policy.list_contents, their output shape on Windows PowerShell
    5.1, -Brief (the values must match the reads without it, class names included) and the not-found errors.
    """
    s = lab.scope("nbwd-test-batch")
    sid = s.scope_id
    base = sid.rsplit(".", 1)[0]
    ips = [f"{base}.20", f"{base}.21"]
    for i, ip in enumerate(ips):
        lab.client.add_reservation(
            wc.Reservation(scope_id=sid, ip_address=ip, client_id=MAC_PREFIX + f"0b-00-{i:02x}", name=f"nbwd-test-b{i}")
        )
    uc = lab.user_class("nbwd-test-batch-user", "nbwd-batch")
    lab.client.set_option_value(wc.OptionKey(15, sid, reserved_ip=ips[0]), ["batch.example"])
    lab.client.set_option_value(wc.OptionKey(15, sid, reserved_ip=ips[0], user_class=uc.name), ["user.example"])
    p = lab.policy(wc.Policy(name="nbwd-test-batchpol", scope_id=sid, criteria={"mac_address": ["EQ", "02-4e-42-ba*"]}))
    lab.client.add_policy_ip_range(wc.PolicyIPRange(sid, p.name, f"{base}.50", f"{base}.60"))
    lab.client.set_option_value(wc.OptionKey(15, sid, policy_name=p.name), ["policy.example"])
    empty = lab.policy(
        wc.Policy(name="nbwd-test-batchempty", scope_id=sid, criteria={"mac_address": ["EQ", "02-4e-42-bc*"]})
    )
    srv = lab.policy(
        wc.Policy(name="nbwd-test-batchsrv", enabled=False, criteria={"mac_address": ["EQ", "02-4e-42-bb*"]})
    )
    lab.client.set_option_value(wc.OptionKey(15, policy_name=srv.name), ["server-policy.example"])

    # Raw shapes, including one item with no values and no ranges (how Windows PowerShell 5.1 prints @() there).
    raw = {
        "reserved": lab.client._run(wc.S_OPTION_LIST_RESERVED, {"ips": ips})[1],
        "reserved_one_empty": lab.client._run(wc.S_OPTION_LIST_RESERVED, {"ips": [ips[1]]})[1],
        "policy": lab.client._run(wc.S_POLICY_LIST_CONTENTS, {"scope_id": sid, "names": [p.name]})[1],
        "policy_one_empty": lab.client._run(wc.S_POLICY_LIST_CONTENTS, {"scope_id": sid, "names": [empty.name]})[1],
        "server_policy": lab.client._run(wc.S_POLICY_LIST_CONTENTS, {"scope_id": "", "names": [srv.name]})[1],
    }
    save_probe(probe_out, "v4-batched-reads", raw)

    got = lab.client.list_reservation_option_values(sid, ips)
    assert list(got) == ips
    for ip in ips:
        # The batch reads with -Brief, the single read without it: same values, same classes.
        assert _values(got[ip]) == _values(lab.client.list_option_values(scope_id=sid, reserved_ip=ip, all=True))
    assert sorted((o.user_class, o.value) for o in got[ips[0]] if o.option_id == 15) == [
        ("", ["batch.example"]),
        (uc.name, ["user.example"]),
    ]
    assert got[ips[1]] == []
    assert lab.client.list_reservation_option_values(sid, [ips[1]]) == {ips[1]: []}

    c = lab.client.list_policy_contents([p.name, empty.name], sid)
    assert c[p.name].ip_ranges == lab.client.list_policy_ip_ranges(sid, p.name)
    assert _values(c[p.name].options) == _values(
        lab.client.list_option_values(scope_id=sid, policy_name=p.name, all=True)
    )
    assert ["policy.example"] in [o.value for o in c[p.name].options if o.option_id == 15]
    assert c[empty.name].ip_ranges == []
    assert _values(c[empty.name].options) == _values(
        lab.client.list_option_values(scope_id=sid, policy_name=empty.name, all=True)
    )
    c = lab.client.list_policy_contents([srv.name])[srv.name]
    assert c.ip_ranges == []
    assert _values(c.options) == _values(lab.client.list_option_values(policy_name=srv.name, all=True))
    assert ["server-policy.example"] in [o.value for o in c.options]

    # The exact errors for things that are not there, recorded before any assertion so the probe always has them.
    gone_ip, gone_policy = f"{base}.99", "nbwd-test-nopolicy"
    errors = {
        "reserved_batch": _error_of(lambda: lab.client.list_reservation_option_values(sid, [ips[0], gone_ip])),
        "reserved_single": _error_of(
            lambda: lab.client.list_option_values(scope_id=sid, reserved_ip=gone_ip, all=True)
        ),
        "scope_policy_batch": _error_of(lambda: lab.client.list_policy_contents([gone_policy], sid)),
        "scope_policy_options": _error_of(
            lambda: lab.client.list_option_values(scope_id=sid, policy_name=gone_policy, all=True)
        ),
        "scope_policy_ranges": _error_of(lambda: lab.client.list_policy_ip_ranges(sid, gone_policy)),
        "server_policy_batch": _error_of(lambda: lab.client.list_policy_contents([gone_policy])),
        "server_policy_options": _error_of(lambda: lab.client.list_option_values(policy_name=gone_policy, all=True)),
    }
    save_probe(probe_out, "v4-batched-gone", errors)
    for name, err in errors.items():
        print(f"{name}: id={err.get('id')!r} category={err.get('category')!r} not_found={err.get('not_found')}")
    # A gone item must read as gone, never as "no option values": windows_state then reads the scope again. Windows
    # Server 2025 answers an IP that is not reserved with DHCP 20016 (InvalidType), which the reservation option reads
    # alone take as not found, and a missing policy with DHCP 20111 (ObjectNotFound) on both cmdlets.
    for k in ("reserved_batch", "reserved_single"):
        assert errors[k].get("not_found") and errors[k]["id"].startswith("DHCP 20016,"), errors
    for k in errors:
        if "policy" in k:
            assert errors[k].get("not_found") and errors[k]["id"].startswith("DHCP 20111,"), errors


def test_batched_read_time(lab: Lab, probe_out) -> None:
    """
    client.py (issue #100): the time per reservation in a batch, with and without -Brief. Over SSH a batch must end
    within command_timeout (a silence timeout), so READ_BATCH items must take at most half of it.
    """
    s = lab.scope("nbwd-test-batchtime")
    sid = s.scope_id
    base = sid.rsplit(".", 1)[0]
    ips = [f"{base}.{20 + i}" for i in range(30)]
    for i, ip in enumerate(ips):
        lab.client.add_reservation(
            wc.Reservation(scope_id=sid, ip_address=ip, client_id=MAC_PREFIX + f"0c-00-{i:02x}", name=f"nbwd-test-t{i}")
        )
        lab.client.set_option_value(wc.OptionKey(15, sid, reserved_ip=ip), [f"t{i}.example"])

    def timed(fn) -> float:
        start = time.monotonic()
        fn()
        return time.monotonic() - start

    def per_item(fn) -> float:
        # A batch of one against a batch of all: the difference leaves out PowerShell's own start-up.
        return max(timed(lambda: fn(ips)) - timed(lambda: fn(ips[:1])), 0.0) / (len(ips) - 1)

    def without_brief(batch: list[str]) -> None:
        lab.raw(
            "probe.nobrief",
            "$out = @(foreach ($ip in @($p.ips)) { @(Get-DhcpServerv4OptionValue @cn -ReservedIP $ip -All).Count })",
            {"ips": batch},
        )

    brief = per_item(lambda batch: lab.client.list_reservation_option_values(sid, batch))
    plain = per_item(without_brief)
    got = lab.client.list_reservation_option_values(sid, ips)
    single = timed(lambda: [lab.client.list_option_values(scope_id=sid, reserved_ip=ip, all=True) for ip in ips])
    budget = DEFAULTS["command_timeout"] / 2
    result = {
        "reservations": len(ips),
        "per_item_brief_s": brief,
        "per_item_without_brief_s": plain,
        "single_reads_s": single,
        "read_batch": wc.READ_BATCH,
        "batch_estimate_s": wc.READ_BATCH * brief,
        "budget_s": budget,
    }
    save_probe(probe_out, "v4-batched-time", result)
    print(result)
    assert all([o.value for o in got[ip] if o.option_id == 15] == [[f"t{i}.example"]] for i, ip in enumerate(ips))
    assert wc.READ_BATCH * brief <= budget, result


# -- issue #108: what the push leaves out because Windows would refuse it ---------------------------------------------


def _attempt(fn) -> str:
    """'accepted', or 'refused: <Windows message>'."""
    try:
        fn()
    except wc.PSError as exc:
        return f"refused: {exc}"
    return "accepted"


def test_range_with_network_or_broadcast_address(lab: Lab, probe_out) -> None:
    """push.py (#108): Windows refuses a scope range holding the network or the broadcast address (Server 2025 lab
    run), so the push leaves such a scope out."""
    seen = {}
    for label, first, last in (("network", 0, 200), ("broadcast", 10, 255)):
        net = lab.new_v4_subnet()
        base = net.rsplit(".", 1)[0]
        lab.on_cleanup(f"scope {net}", lambda net=net: lab.remove_scope(net))
        scope = wc.Scope(
            name=f"nbwd-test-edge-{label}",
            start_range=f"{base}.{first}",
            end_range=f"{base}.{last}",
            subnet_mask="255.255.255.0",
            scope_id=net,
            lease_duration="1.00:00:00",
        )
        seen[label] = _attempt(lambda scope=scope: lab.client.add_scope(scope))
    save_probe(probe_out, "v4-scope-network-broadcast", seen)
    assert all(v.startswith("refused") for v in seen.values()), seen


def test_reservation_outside_the_scope_range(lab: Lab, probe_out) -> None:
    """push.py (#108): Windows takes a reservation inside the subnet but outside the scope's range (Server 2025 lab
    run), whatever Microsoft KB 2005980 says, so the push only asks for the prefix."""
    s = lab.scope("nbwd-test-res-out")
    base = s.scope_id.rsplit(".", 1)[0]
    outside = wc.Reservation(scope_id=s.scope_id, ip_address=f"{base}.5", client_id=MAC_PREFIX + "00-01-08")
    seen = {"outside": _attempt(lambda: lab.client.add_reservation(outside))}
    save_probe(probe_out, "v4-reservation-outside-range", seen)
    assert seen["outside"] == "accepted", seen


def test_overlapping_scopes(lab: Lab, probe_out) -> None:
    """push.py (#108): Windows refuses a scope sharing addresses with another scope of the server, a /25 inside a
    /24 (Server 2025 lab run: ResourceExists)."""
    s = lab.scope("nbwd-test-scope-overlap")
    base = s.scope_id.rsplit(".", 1)[0]
    inner = f"{base}.128"
    lab.on_cleanup(f"scope {inner}", lambda: lab.remove_scope(inner))
    scope = wc.Scope(
        name="nbwd-test-scope-overlap-inner",
        start_range=f"{base}.130",
        end_range=f"{base}.140",
        subnet_mask="255.255.255.128",
        scope_id=inner,
        lease_duration="1.00:00:00",
    )
    seen = {"inner": _attempt(lambda: lab.client.add_scope(scope))}
    seen["listed"] = sorted(x.scope_id for x in lab.client.list_scopes() if x.scope_id.startswith(f"{base}."))
    save_probe(probe_out, "v4-scope-overlap", seen)
    assert seen["inner"].startswith("refused"), seen


def test_overlapping_exclusion_ranges(lab: Lab, probe_out) -> None:
    """push.py (#108): Windows refuses exclusion ranges of one scope sharing addresses (overlapping or nested), and
    takes one reaching past the scope's range inside the subnet (Server 2025 lab run)."""
    s = lab.scope("nbwd-test-excl-overlap")
    base = s.scope_id.rsplit(".", 1)[0]
    lab.client.add_exclusion_range(wc.ExclusionRange(s.scope_id, f"{base}.20", f"{base}.40"))
    attempts = {
        "overlap": (30, 50),
        "inside": (25, 35),
        # Inside the subnet but reaching past the scope's range (.10-.200).
        "outside_range": (2, 12),
    }
    seen = {
        label: _attempt(
            lambda a=a, b=b: lab.client.add_exclusion_range(wc.ExclusionRange(s.scope_id, f"{base}.{a}", f"{base}.{b}"))
        )
        for label, (a, b) in attempts.items()
    }
    seen["listed"] = [(e.start_range, e.end_range) for e in lab.client.list_exclusion_ranges(s.scope_id)]
    save_probe(probe_out, "v4-exclusion-overlap", seen)
    assert seen["overlap"].startswith("refused") and seen["inside"].startswith("refused"), seen
    assert seen["outside_range"] == "accepted", seen


def test_two_reservations_for_one_client(lab: Lab, probe_out) -> None:
    """push.py (#108): Windows refuses a second reservation for one client in a scope (Server 2025 lab run:
    ResourceExists)."""
    s = lab.scope("nbwd-test-res-twice")
    base = s.scope_id.rsplit(".", 1)[0]
    mac = MAC_PREFIX + "00-01-09"
    lab.client.add_reservation(wc.Reservation(scope_id=s.scope_id, ip_address=f"{base}.20", client_id=mac))
    second = wc.Reservation(scope_id=s.scope_id, ip_address=f"{base}.21", client_id=mac)
    seen = {"second": _attempt(lambda: lab.client.add_reservation(second))}
    seen["listed"] = [r.ip_address for r in lab.client.list_reservations(s.scope_id)]
    save_probe(probe_out, "v4-reservation-same-client", seen)
    assert seen["second"].startswith("refused"), seen
