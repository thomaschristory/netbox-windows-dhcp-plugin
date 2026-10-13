import pytest

from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.sync.diff import Change, Record, diff
from netbox_windows_dhcp_plugin.sync.windows_writer import PushRefused, WindowsWriter
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError


@pytest.fixture
def fake():
    return FakeDhcpServer()


@pytest.fixture
def client(fake):
    return DhcpClient(fake)


def scope(**over):
    fields = {
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
    fields.update(over)
    return Record("scope", "10.1.0.0", fields)


def wanted():
    """What NetBox would hold for a small server, in the records' own spelling."""
    return [
        Record("class", "user/phones", {"name": "Phones", "class_type": "user", "data": "phone", "description": "IP"}),
        Record(
            "optiondef",
            "200",
            {
                "option_id": 200,
                "name": "Custom",
                "type": "String",
                "multi_valued": False,
                "default_value": [],
                "description": "",
                "vendor_class": "",
            },
        ),
        scope(),
        Record(
            "exclusion",
            "10.1.0.0/10.1.0.10-10.1.0.19",
            {"scope_id": "10.1.0.0", "start_range": "10.1.0.10", "end_range": "10.1.0.19"},
        ),
        Record(
            "reservation",
            "10.1.0.0/10.1.0.50",
            {
                "scope_id": "10.1.0.0",
                "ip_address": "10.1.0.50",
                "client_id": "aa-bb-cc-00-11-22",
                "name": "pc",
                "description": "desk",
                "reservation_type": "both",
            },
        ),
        option("server", 6, ["10.0.0.1", "10.0.0.2"]),
        option("10.1.0.0", 3, ["10.1.0.1"]),
        option("10.1.0.0", 200, ["hello"], user_class="Phones"),
        option("10.1.0.0/10.1.0.50", 15, ["pc.example"]),
    ]


def option(parent, option_id, value, user_class=""):
    return Record(
        "option",
        ws.option_key(parent, option_id, "", user_class),
        {"parent": parent, "option_id": option_id, "value": value, "vendor_class": "", "user_class": user_class},
    )


def windows_records(client):
    records, _ = ws.to_records(ws.read(client), standard_codes=())
    # The fake ships Windows' own built-in definitions; they are not part of these tests.
    return [r for r in records if r.kind != "optiondef" or r.key == "200"]


def push(client, source):
    writer = WindowsWriter(client)
    for change in diff(source, windows_records(client), equal=ws.equal):
        writer.apply(change)


def test_creates_everything_and_reads_back_the_same(client):
    push(client, wanted())
    assert diff(wanted(), windows_records(client), equal=ws.equal) == []


def test_scope_state_and_lease_are_sent_in_windows_spelling(client, fake):
    push(client, wanted())
    assert fake.scopes["10.1.0.0"]["state"] == "Inactive"
    assert fake.scopes["10.1.0.0"]["lease_duration"] == "1.00:00:00"
    assert fake.scopes["10.1.0.0"]["type"] == "Dhcp"


def test_updates(client, fake):
    push(client, wanted())
    source = wanted()
    source[2] = scope(name="LAN 2", state="active", lease_seconds=3600, max_bootp_clients=10)
    res = source[4]
    source[4] = Record(res.kind, res.key, {**res.fields, "client_id": "aa-bb-cc-00-11-33", "name": "pc2"})
    source[5] = option("server", 6, ["10.0.0.9"])
    push(client, source)
    assert diff(source, windows_records(client), equal=ws.equal) == []
    assert fake.scopes["10.1.0.0"]["state"] == "Active"
    assert fake.scopes["10.1.0.0"]["max_bootp_clients"] == 10


def test_no_bootp_limit_is_sent_as_the_maximum(client, fake):
    push(client, wanted())
    fake.scopes["10.1.0.0"]["max_bootp_clients"] = 10
    push(client, wanted())
    assert fake.scopes["10.1.0.0"]["max_bootp_clients"] == ws.NO_BOOTP_LIMIT


def test_deletes_children_first(client, fake):
    push(client, wanted())
    push(client, [])
    assert fake.scopes == {}
    assert fake.classes == {}
    removed = [c.op for c in fake.calls if c.op.endswith(".remove")]
    assert removed.index("option.remove") < removed.index("reservation.remove") < removed.index("scope.remove")


def test_exclusion_update_is_remove_and_add(client, fake):
    push(client, wanted())
    writer = WindowsWriter(client)
    writer.apply(
        Change(
            "update",
            "exclusion",
            "10.1.0.0/10.1.0.10-10.1.0.19",
            before={"scope_id": "10.1.0.0", "start_range": "10.1.0.10", "end_range": "10.1.0.19"},
            after={"scope_id": "10.1.0.0", "start_range": "10.1.0.10", "end_range": "10.1.0.29"},
        )
    )
    assert [(e["start_range"], e["end_range"]) for e in fake.exclusions] == [("10.1.0.10", "10.1.0.29")]


def test_subnet_mask_change_is_refused(client, fake):
    push(client, wanted())
    change = Change("update", "scope", "10.1.0.0", before=scope().fields, after=scope(subnet_mask="255.255.0.0").fields)
    with pytest.raises(PushRefused, match="subnet mask"):
        WindowsWriter(client).apply(change)
    assert fake.scopes["10.1.0.0"]["subnet_mask"] == "255.255.255.0"


def test_option_definition_type_change_is_refused(client):
    push(client, wanted())
    before = wanted()[1].fields
    change = Change("update", "optiondef", "200", before=before, after={**before, "type": "DWord"})
    with pytest.raises(PushRefused, match="type"):
        WindowsWriter(client).apply(change)


def test_scope_with_leases_is_not_forced(client, fake):
    push(client, wanted())
    fake.add_lease("10.1.0.0", "10.1.0.60")
    with pytest.raises(PSError) as err:
        WindowsWriter(client).apply(Change("delete", "scope", "10.1.0.0", before=scope().fields))
    assert err.value.id.startswith("DHCP 20007")
    assert "10.1.0.0" in fake.scopes


def test_unlimited_lease_keeps_the_windows_value(client, fake):
    push(client, wanted())
    change = Change(
        "update", "scope", "10.1.0.0", before=scope().fields, after=scope(lease_seconds=None, name="x").fields
    )
    WindowsWriter(client).apply(change)
    assert fake.scopes["10.1.0.0"]["lease_duration"] == "1.00:00:00"
    assert fake.scopes["10.1.0.0"]["name"] == "x"


def test_unknown_kind_is_refused(client):
    with pytest.raises(PushRefused):
        WindowsWriter(client).apply(Change("create", "policy", "x", after={}))


def test_lease_unknown_on_both_sides_is_left_alone(client, fake):
    push(client, wanted())
    before = scope(lease_seconds=None).fields
    WindowsWriter(client).apply(
        Change("update", "scope", "10.1.0.0", before=before, after=scope(lease_seconds=None, name="y").fields)
    )
    set_call = [c for c in fake.calls if c.op == "scope.set"][-1]
    assert set_call.params["lease_seconds"] is None
    assert fake.scopes["10.1.0.0"]["lease_duration"] == "1.00:00:00"


def test_clearing_a_default_value_is_refused(client):
    push(client, wanted())
    before = {**wanted()[1].fields, "default_value": ["x"]}
    with pytest.raises(PushRefused, match="default value"):
        WindowsWriter(client).apply(
            Change("update", "optiondef", "200", before=before, after={**before, "default_value": []})
        )
