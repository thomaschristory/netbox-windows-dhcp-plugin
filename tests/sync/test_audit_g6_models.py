"""Audit F32: an option definition's Windows type is compared without case, like Windows reads it."""

import pytest

from netbox_windows_dhcp_plugin.sync import push
from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.sync.diff import UPDATE, Change, Record, diff
from netbox_windows_dhcp_plugin.sync.windows_writer import PushRefused, WindowsWriter
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import OptionDefinition
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer


def definition(**over):
    fields = {
        "option_id": 200,
        "name": "Custom",
        "type": "String",
        "multi_valued": False,
        "default_value": [],
        "description": "",
        "vendor_class": "",
    }
    fields.update(over)
    return fields


@pytest.mark.parametrize(
    ("typed", "windows"), [("string", "String"), ("Dword", "DWord"), ("IPV4ADDRESS", "IPv4Address")]
)
def test_a_type_typed_in_another_case_is_the_same_definition(typed, windows):
    assert ws.records_equal("optiondef", definition(type=typed), definition(type=windows))
    source = [Record("optiondef", "200", definition(type=typed))]
    target = [Record("optiondef", "200", definition(type=windows))]
    assert diff(source, target, equal_records=ws.records_equal) == []


def test_another_type_is_still_a_difference():
    assert not ws.records_equal("optiondef", definition(type="String"), definition(type="DWord"))


def test_the_type_case_does_not_change_the_fingerprints_already_stored():
    # Stored fingerprints keep the spelling: lowercasing the type there would make every recorded definition look
    # changed on Windows after an upgrade.
    assert ("optiondef", "type") not in ws.NAME_FIELDS
    typed = push.fingerprint(Record("optiondef", "200", definition(type="string")))
    assert typed != push.fingerprint(Record("optiondef", "200", definition(type="String")))
    assert typed == "8ea4ee3d69c3497eff2fd409d1f4c02cbd89712e0239063276b6646df1794299"


# What 0.2.0 recorded in SyncState for these objects: each digest below was computed by running the 0.2.0 code
# (`git show v0.2.0:netbox_windows_dhcp_plugin/sync/push.py`) on the record next to it. A push only deletes a Windows
# object whose fingerprint still matches the stored one, so a digest that moves here means every delete made in NetBox
# is skipped as "changed on Windows" after an upgrade. Change one only together with a way to adopt the old form, like
# `option_value_version` in push.fingerprint.
RECORDED_BY_0_2_0 = [
    # Plain kind: nothing special in the hash.
    (
        Record(
            "scope",
            "10.1.0.0",
            {
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
            },
        ),
        "c8739ef32f177d81bdf7adaa3cbe7e127b6a33be816b5ce482a8f862ab624c8a",
    ),
    # Option definition: the type keeps its case, a number default hashes in decimal.
    (Record("optiondef", "200", definition()), "bcfa51012146c73b34e8055c191da46647eed2c5c047076f9f85d45991f4a3eb"),
    (
        Record("optiondef", "201", definition(option_id=201, name="Flags", type="DWord", default_value=["0x10"])),
        "3e8a7b517ae72fdf17aa0a31aa21f5e78068d6ef0b96a1f412b555b34d95d92d",
    ),
    # Option value of a standard number option (46, NetBIOS node type), typed in hex, with a class name in capitals.
    (
        Record(
            "option",
            "10.1.0.0/46//Phones",
            {"parent": "10.1.0.0", "option_id": 46, "value": ["0x8"], "vendor_class": "", "user_class": "Phones"},
        ),
        "ff658a516f398042c29f60ad5c71b0d7fd2f24189e6747fb71555e5dcdd9122c",
    ),
    # Policy: the processing order is left out, the name is lowercased, the MAC prefix is hashed as Windows stores it.
    (
        Record(
            "policy",
            "10.1.0.0/printers",
            {
                "name": "Printers",
                "scope_id": "10.1.0.0",
                "description": "print",
                "enabled": True,
                "processing_order": 3,
                "condition": "or",
                "criteria": {"mac_address": ["EQ", "00-15-5D*"]},
                "lease_seconds": 3600,
                "ip_ranges": [["10.1.0.100", "10.1.0.120"]],
            },
        ),
        "e02d5626c327f5044d3d01dc747cf28e0d2d17690c2284a234371fccb9181c6c",
    ),
    # Names Windows reads without case.
    (
        Record("class", "user/phones", {"name": "Phones", "class_type": "user", "data": "phone", "description": "IP"}),
        "df65d54b25baa6c206345b139918c2ffe91e180659deed2e01bde5d7cbdf7046",
    ),
    (
        Record(
            "failover",
            "ha",
            {
                "name": "HA",
                "partner_server": "dhcp2.example.com",
                "mode": "loadbalance",
                "max_client_lead_time": 3600,
                "load_balance_percent": 60,
                "reserve_percent": None,
                "server_role": "",
                "state_switch_interval": None,
                "auth": False,
                "scope_ids": ["10.1.0.0"],
            },
        ),
        "c3cb2f10524fe6a0be0ff4c39f7356217d8d354a2b60c01733bf8c911d1ec975",
    ),
]


@pytest.mark.parametrize(("record", "recorded"), RECORDED_BY_0_2_0, ids=lambda v: getattr(v, "key", None))
def test_a_fingerprint_recorded_by_0_2_0_still_matches(record, recorded):
    assert push.fingerprint(record) == recorded
    assert push.unchanged(recorded, record)


@pytest.fixture
def client():
    client = DhcpClient(FakeDhcpServer())
    client.add_option_definition(OptionDefinition(option_id=200, name="Custom", type="String"))
    return client


def test_another_change_is_pushed_when_the_type_differs_only_in_case(client):
    change = Change(
        UPDATE,
        "optiondef",
        "200",
        before=definition(type="String"),
        after=definition(type="string", description="now described"),
    )
    WindowsWriter(client).apply(change)
    assert client.get_option_definition(200).description == "now described"


def test_a_real_type_change_is_still_refused(client):
    change = Change(UPDATE, "optiondef", "200", before=definition(type="String"), after=definition(type="DWord"))
    with pytest.raises(PushRefused, match="the type cannot change"):
        WindowsWriter(client).apply(change)
    assert client.get_option_definition(200).type == "String"
