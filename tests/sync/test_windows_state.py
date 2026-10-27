import pytest

from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.sync.diff import Record, diff
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import (
    DhcpClass,
    ExclusionRange,
    FailoverRelationship,
    MacFilter,
    OptionDefinition,
    OptionKey,
    Policy,
    PolicyIPRange,
    Reservation,
    Scope,
    ScopeV6,
    Superscope,
)
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import NotFound


@pytest.fixture
def client():
    fake = FakeDhcpServer()
    c = DhcpClient(fake)
    c.add_scope(
        Scope(
            name="LAN",
            description="office",
            start_range="10.1.0.10",
            end_range="10.1.0.200",
            subnet_mask="255.255.255.0",
            lease_duration="1.00:00:00",
            state="InActive",
        )
    )
    c.add_exclusion_range(ExclusionRange(scope_id="10.1.0.0", start_range="10.1.0.10", end_range="10.1.0.19"))
    c.add_reservation(
        Reservation(scope_id="10.1.0.0", ip_address="10.1.0.50", client_id="AA:BB:CC:00:11:22", name="pc")
    )
    c.add_class(DhcpClass(name="MSFT", type="Vendor", data="MSFT 5.0"))
    c.add_option_definition(OptionDefinition(option_id=200, name="Custom", type="String"))
    c.add_option_definition(OptionDefinition(option_id=1, name="Vendor opt", type="DWord", vendor_class="MSFT"))
    c.set_option_value(OptionKey(option_id=6), ["10.0.0.1", "10.0.0.2"])
    c.set_option_value(OptionKey(option_id=3, scope_id="10.1.0.0"), ["10.1.0.1"])
    c.set_option_value(OptionKey(option_id=51, scope_id="10.1.0.0"), ["86400"])
    c.set_option_value(OptionKey(option_id=1, vendor_class="MSFT"), ["1"])
    c.set_option_value(OptionKey(option_id=15, scope_id="10.1.0.0", reserved_ip="10.1.0.50"), ["pc.example"])
    return c


def by_kind(records):
    out = {}
    for r in records:
        out.setdefault(r.kind, {})[r.key] = r.fields
    return out


def test_reads_every_kind(client):
    records, notes = ws.to_records(ws.read(client))
    kinds = by_kind(records)
    assert set(kinds) == {"class", "optiondef", "scope", "exclusion", "reservation", "option", "filterlists"}
    assert notes == []


def test_scope_record(client):
    scope = by_kind(ws.to_records(ws.read(client))[0])["scope"]["10.1.0.0"]
    assert scope == {
        "name": "LAN",
        "description": "office",
        "start_range": "10.1.0.10",
        "end_range": "10.1.0.200",
        "subnet_mask": "255.255.255.0",
        "state": "inactive",
        "scope_type": "dhcp",
        "lease_seconds": 86400,
        "delay_ms": 0,
        "max_bootp_clients": None,
        "activate_policies": True,
    }


def test_exclusion_and_reservation_records(client):
    kinds = by_kind(ws.to_records(ws.read(client))[0])
    assert kinds["exclusion"] == {
        "10.1.0.0/10.1.0.10-10.1.0.19": {
            "scope_id": "10.1.0.0",
            "start_range": "10.1.0.10",
            "end_range": "10.1.0.19",
        }
    }
    assert kinds["reservation"]["10.1.0.0/10.1.0.50"] == {
        "scope_id": "10.1.0.0",
        "ip_address": "10.1.0.50",
        "client_id": "aa-bb-cc-00-11-22",
        "name": "pc",
        "description": "",
        "reservation_type": "both",
    }


def test_class_and_definition_records(client):
    kinds = by_kind(ws.to_records(ws.read(client))[0])
    assert kinds["class"]["vendor/msft"] == {
        "name": "MSFT",
        "class_type": "vendor",
        "data": "MSFT 5.0",
        "description": "",
    }
    assert kinds["optiondef"]["200"]["name"] == "Custom"
    # Vendor class definitions are keyed by the lowercase class name, and code 1 is not skipped for them.
    assert kinds["optiondef"]["msft/1"]["vendor_class"] == "MSFT"


def test_standard_definitions_are_left_to_sys4(client):
    records, _ = ws.to_records(ws.read(client), standard_codes={6, 200})
    keys = set(by_kind(records)["optiondef"])
    assert "6" not in keys and "200" not in keys


def test_option_records_and_skipped_ids(client):
    options = by_kind(ws.to_records(ws.read(client))[0])["option"]
    assert set(options) == {
        "server/6",
        "10.1.0.0/3",
        "10.1.0.0/10.1.0.50/15",
        "server/1;vendor=msft",
    }
    assert options["server/6"] == {
        "parent": "server",
        "option_id": 6,
        "value": ["10.0.0.1", "10.0.0.2"],
        "vendor_class": "",
        "user_class": "",
    }
    assert options["10.1.0.0/10.1.0.50/15"]["parent"] == "10.1.0.0/10.1.0.50"


def test_policy_option_records():
    state = ws.WindowsState(
        options=[ws.OptionValue(option_id=6, value=["1.1.1.1"], policy_name="Printers", scope_id="10.1.0.0")]
    )
    (record,), notes = ws.to_records(state)
    assert notes == []
    assert record.key == "10.1.0.0/6;policy=printers"
    assert record.fields["policy"] == "Printers"


def test_values_netbox_cannot_store_are_skipped():
    state = ws.WindowsState(
        options=[
            ws.OptionValue(option_id=15, value=["a,b"]),
            ws.OptionValue(option_id=15, value=[" x"], scope_id="s"),
            # Vendor class values keep their list, so surrounding spaces are fine there.
            ws.OptionValue(option_id=2, value=[" y"], vendor_class="MSFT"),
        ]
    )
    records, notes = ws.to_records(state)
    assert [r.key for r in records] == ["server/15", "server/2;vendor=msft"]
    assert notes == [f"skipped 1 {ws.UNSTORABLE_VALUES}"]
    assert [r.key for r in ws.hidden_records(state)] == ["s/15"]


@pytest.mark.parametrize(
    "values",
    [[], ["a"], ["a", "b"], ["a,b", "c"], ["back\\slash", "x,"], ["10.0.0.1", "10.0.0.2"]],
)
def test_values_survive_the_netbox_string(values):
    assert ws.split_values(ws.join_values(values)) == values


def test_netbox_strings_are_split_like_before():
    assert ws.split_values("10.0.0.1, 10.0.0.2") == ["10.0.0.1", "10.0.0.2"]
    assert ws.split_values("  ") == []
    assert ws.join_values(["a,b", "c"]) == "a\\,b, c"


def test_long_names_are_cut_like_netbox_cuts_them(client):
    client.set_scope(Scope(scope_id="10.1.0.0", name="n" * 300, start_range="10.1.0.10", end_range="10.1.0.200"))
    scope = by_kind(ws.to_records(ws.read(client))[0])["scope"]["10.1.0.0"]
    assert scope["name"] == "n" * 200


def test_text_option_values_compare_exactly():
    assert not ws.equal("option", "value", ["BootX64.efi"], ["bootx64.efi"])
    assert ws.equal("option", "value", ["0x0A"], ["10"])
    assert not ws.equal("option", "value", ["1"], ["1", "2"])


def test_option_values_compare_hex_and_decimal():
    a = [Record("option", "server/46", {"value": ["0x8"]})]
    b = [Record("option", "server/46", {"value": ["8"]})]
    assert diff(a, b, equal=ws.equal) == []


def test_unparseable_lease_duration_reads_as_none():
    state = ws.WindowsState(
        scopes=[Scope(scope_id="10.9.0.0", start_range="10.9.0.1", end_range="10.9.0.9", subnet_mask="255.255.255.0")]
    )
    state.scopes[0].lease_duration = "Unlimited"
    (scope,) = ws.to_records(state)[0]
    assert scope.fields["lease_seconds"] is None


def test_policy_superscope_failover_and_filter_records(client):

    client.add_scope(Scope(name="B", start_range="10.2.0.10", end_range="10.2.0.20", subnet_mask="255.255.255.0"))
    client.add_superscope(Superscope(name="Campus", scope_ids=["10.2.0.0", "10.1.0.0"]))
    client.add_policy(
        Policy(name="Printers", scope_id="10.1.0.0", criteria={"mac_address": ["eq", "00-15-5d*"]}, lease_duration="")
    )
    client.add_policy_ip_range(PolicyIPRange("10.1.0.0", "Printers", "10.1.0.150", "10.1.0.160"))
    client.add_policy_ip_range(PolicyIPRange("10.1.0.0", "Printers", "10.1.0.100", "10.1.0.120"))
    client.add_failover(FailoverRelationship(name="HA", partner_server="DHCP2", scope_ids=["10.2.0.0"]))
    client.add_filter(MacFilter(mac_address="00-15-5D-*", list="Deny"))
    kinds = by_kind(ws.to_records(ws.read(client))[0])
    assert kinds["superscope"] == {"campus": {"name": "Campus", "scope_ids": ["10.1.0.0", "10.2.0.0"]}}
    assert kinds["policy"]["10.1.0.0/printers"] == {
        "name": "Printers",
        "scope_id": "10.1.0.0",
        "description": "",
        "enabled": True,
        "processing_order": 1,
        "condition": "or",
        # As Windows stores the "00-15-5d*" it was given (#21).
        "criteria": {"mac_address": ["EQ", "00-15-5d-*"]},
        "lease_seconds": None,
        "ip_ranges": [["10.1.0.100", "10.1.0.120"], ["10.1.0.150", "10.1.0.160"]],
    }
    assert kinds["failover"]["ha"] == {
        "name": "HA",
        "partner_server": "dhcp2",
        "mode": "loadbalance",
        "max_client_lead_time": 3600,
        "load_balance_percent": 50,
        "reserve_percent": None,
        "server_role": "",
        "state_switch_interval": None,
        "auth": False,
        "scope_ids": ["10.2.0.0"],
    }
    assert kinds["macfilter"] == {
        "00-15-5d-*": {"mac_address": "00-15-5d-*", "list": "deny", "description": ""},
    }
    records, notes = ws.to_records(ws.read(client), partner_failovers={"HA"})
    assert "failover" not in by_kind(records)
    assert notes == [f"skipped 1 {ws.PARTNER_FAILOVERS}"]


def test_policy_criteria_compare_as_windows_stores_them():
    """#21: Windows rewrites a MAC prefix and lowercases a host name; both must read back as no change."""
    sent = Policy(
        name="Lab",
        scope_id="10.1.0.0",
        processing_order=1,
        criteria={"mac_address": ["EQ", "02-4e-42-ab*"], "fqdn": ["EQ", "*.Example.Test"]},
    )
    read = Policy(
        name="Lab",
        scope_id="10.1.0.0",
        processing_order=1,
        criteria={"mac_address": ["EQ", "02-4e-42-ab-*"], "fqdn": ["EQ", "*.example.test"]},
    )
    assert diff([ws.policy_record(sent, [])], [ws.policy_record(read, [])], equal=ws.equal) == []
    assert ws.equal("policy", "criteria", {"mac_address": ["eq", "AA-BB*"]}, {"mac_address": ["EQ", "aa-bb-*"]})
    # A different prefix, a different operator, or a case change in a class name is still a change.
    assert not ws.equal("policy", "criteria", {"mac_address": ["EQ", "aa-bb*"]}, {"mac_address": ["EQ", "aa-bc-*"]})
    assert not ws.equal("policy", "criteria", {"fqdn": ["EQ", "*.a.test"]}, {"fqdn": ["NE", "*.a.test"]})
    assert not ws.equal("policy", "criteria", {"user_class": ["EQ", "Phones"]}, {"user_class": ["EQ", "phones"]})
    assert not ws.equal("policy", "criteria", {"fqdn": ["EQ", "a"]}, {"fqdn": ["EQ", "a"], "user_class": ["EQ", "x"]})


def test_names_compare_without_case():
    assert ws.equal("policy", "name", "Printers", "PRINTERS")
    assert ws.equal("option", "policy", "a", "A")
    assert not ws.equal("policy", "description", "a", "A")


def test_failover_partner_names_match_short_and_qualified():
    assert ws.same_host("DHCP2.corp.example.", "dhcp2.corp.example")
    assert ws.same_host("dhcp2", "dhcp2.corp.example")
    assert not ws.same_host("dhcp2.a.example", "dhcp2.b.example")
    assert not ws.same_host("dhcp2", "dhcp3")
    assert ws.equal("failover", "partner_server", "dhcp2", "dhcp2.corp.example")


def test_backslashes_are_kept():
    # A PXE boot path, stored before or after commas were escaped.
    assert ws.split_values("boot\\x64\\wdsnbp.com") == ["boot\\x64\\wdsnbp.com"]
    assert ws.split_values(ws.join_values(["\\\\srv\\share", "a\\,b"])) == ["\\\\srv\\share", "a\\,b"]


def test_values_ending_in_a_backslash_are_not_stored():
    state = ws.WindowsState(options=[ws.OptionValue(option_id=67, value=["boot\\"])])
    assert ws.to_records(state)[0] == []


@pytest.mark.parametrize("value", ["10675199.02:48:05", ""])
def test_unlimited_lease_records_no_lease_time(value):
    # An unlimited lease reads back as TimeSpan.MaxValue, printed by client.py as 10675199.02:48:05 (lab, Windows
    # Server 2025): too long for parse_duration, so no lease time, and a push keeps the scope's own.
    assert ws._lease_seconds(value) is None
    assert ws._lease_seconds("8.00:00:00") == 8 * 86400


def test_a_scope_deleted_during_the_read_is_left_out(client):
    # Seen in the lab: removing a failover relationship deletes its scopes on the partner, which a read already
    # under way had listed. Its per-scope reads then fail with ObjectNotFound; that scope is gone, the rest is fine.
    c = client
    c.add_scope(Scope(name="Gone", start_range="10.2.0.10", end_range="10.2.0.20", subnet_mask="255.255.255.0"))
    c.add_v6_scope(ScopeV6(prefix="2001:db8:9::", name="Gone6"))
    listed, listed6 = c.list_scopes(), c.list_v6_scopes()
    c.remove_scope("10.2.0.0", force=True)
    c.remove_v6_scope("2001:db8:9::", force=True)
    c.list_scopes = lambda: listed
    c.list_v6_scopes = lambda: listed6
    state = ws.read(c)
    assert [s.scope_id for s in state.scopes] == ["10.1.0.0"]
    assert state.v6_scopes == []
    assert [r.ip_address for r in state.reservations] == ["10.1.0.50"]
    # #58: say so, through the notes the import and push jobs log and keep in the run summary.
    gone = [
        "scope 10.2.0.0 left out: deleted on Windows while it was read",
        "DHCPv6 scope 2001:db8:9:: left out: deleted on Windows while it was read",
    ]
    assert state.notes == gone
    assert ws.to_records(state)[1][:2] == gone


def test_a_full_read_has_no_notes(client):
    assert ws.read(client).notes == []


def test_a_reservation_deleted_during_the_read_keeps_its_scope(client):
    # Review of #55: a NotFound for something inside the scope (here a reservation deleted between the list and the
    # read of its options) must not make the whole scope look deleted. The scope is read again.
    c = client
    real = c.list_option_values
    calls = {"n": 0}

    def flaky(**kw):
        if kw.get("reserved_ip") == "10.1.0.50" and calls["n"] == 0:
            calls["n"] += 1
            raise NotFound("Get-DhcpServerv4OptionValue: Failed to get reservation 10.1.0.50 (ObjectNotFound)")
        return real(**kw)

    c.list_option_values = flaky
    state = ws.read(c)
    assert [s.scope_id for s in state.scopes] == ["10.1.0.0"]
    assert [r.ip_address for r in state.reservations] == ["10.1.0.50"]


def test_a_scope_that_keeps_failing_to_read_fails_the_read(client):
    c = client

    def broken(**kw):
        if kw.get("reserved_ip"):
            raise NotFound("Get-DhcpServerv4OptionValue: (ObjectNotFound)")
        return []

    c.list_option_values = broken
    with pytest.raises(NotFound):
        ws.read(c)
