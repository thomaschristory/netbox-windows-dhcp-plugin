import pytest

from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.sync.diff import Record, diff
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import (
    DhcpClass,
    ExclusionRange,
    OptionDefinition,
    OptionKey,
    Reservation,
    Scope,
)
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer


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
    assert set(kinds) == {"class", "optiondef", "scope", "exclusion", "reservation", "option"}
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
    assert kinds["class"]["vendor/MSFT"] == {
        "name": "MSFT",
        "class_type": "vendor",
        "data": "MSFT 5.0",
        "description": "",
    }
    assert kinds["optiondef"]["200"]["name"] == "Custom"
    assert kinds["optiondef"]["MSFT/1"]["vendor_class"] == "MSFT"


def test_standard_definitions_are_left_to_sys4(client):
    records, _ = ws.to_records(ws.read(client), standard_codes={6, 200})
    keys = set(by_kind(records)["optiondef"])
    assert "6" not in keys and "200" not in keys
    # A vendor-class definition never matches a standard one.
    assert "MSFT/1" in keys


def test_option_records_and_skipped_ids(client):
    options = by_kind(ws.to_records(ws.read(client))[0])["option"]
    assert set(options) == {
        "server/6",
        "server/1;vendor=MSFT",
        "10.1.0.0/3",
        "10.1.0.0/10.1.0.50/15",
    }
    assert options["server/6"] == {
        "parent": "server",
        "option_id": 6,
        "value": ["10.0.0.1", "10.0.0.2"],
        "vendor_class": "",
        "user_class": "",
    }
    assert options["10.1.0.0/10.1.0.50/15"]["parent"] == "10.1.0.0/10.1.0.50"


def test_policy_options_are_skipped_with_a_note():
    state = ws.WindowsState(
        options=[ws.OptionValue(option_id=6, value=["1.1.1.1"], policy_name="printers", scope_id="10.1.0.0")]
    )
    records, notes = ws.to_records(state)
    assert records == []
    assert notes == ["skipped 1 policy option value(s): policies come in a later release"]


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
