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
        # Milestone 5 kinds, appended so the positions above stay put.
        Record("class", "vendor/msft", {"name": "MSFT", "class_type": "vendor", "data": "MSFT 5.0", "description": ""}),
        Record(
            "optiondef",
            "msft/1",
            {
                "option_id": 1,
                "name": "Disable NetBIOS",
                "type": "DWord",
                "multi_valued": False,
                "default_value": [],
                "description": "",
                "vendor_class": "MSFT",
            },
        ),
        option("10.1.0.0", 1, ["0x1"], vendor_class="MSFT"),
        Record("superscope", "campus", {"name": "Campus", "scope_ids": ["10.1.0.0"]}),
        policy(),
        policy(
            scope_id="",
            name="Windows",
            criteria={"vendor_class": ["EQ", "MSFT"]},
            ip_ranges=[],
            processing_order=1,
            lease_seconds=None,
        ),
        option("10.1.0.0", 6, ["10.1.0.2", "a,b"], policy="Printers"),
        failover(),
        Record("macfilter", "00-15-5d-*", {"mac_address": "00-15-5d-*", "list": "deny", "description": "lab VMs"}),
        Record("filterlists", "server", {"allow": False, "deny": True}),
    ]


def policy(**over):
    fields = {
        "name": "Printers",
        "scope_id": "10.1.0.0",
        "description": "print",
        "enabled": True,
        "processing_order": 1,
        "condition": "or",
        "criteria": {"mac_address": ["EQ", "00-15-5d*"]},
        "lease_seconds": 3600,
        "ip_ranges": [["10.1.0.100", "10.1.0.120"]],
    }
    fields.update(over)
    return Record("policy", ws.policy_key(fields["scope_id"], fields["name"]), fields)


def failover(**over):
    fields = {
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
    }
    fields.update(over)
    return Record("failover", fields["name"].lower(), fields)


def option(parent, option_id, value, user_class="", vendor_class="", policy=""):
    fields = {"parent": parent, "option_id": option_id, "value": value, "vendor_class": vendor_class}
    fields["user_class"] = user_class
    if policy:
        fields["policy"] = policy
    return Record("option", ws.option_key(parent, option_id, vendor_class, user_class, policy), fields)


def windows_records(client):
    records, _ = ws.to_records(ws.read(client), standard_codes=())
    # The fake ships Windows' own built-in definitions, and the filter list switches always exist on Windows; they are
    # not part of these tests unless a test puts them there.
    return [r for r in records if r.kind != "optiondef" or r.key in ("200", "msft/1")]


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
    push(client, [r for r in wanted() if r.kind != "failover"])
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
        WindowsWriter(client).apply(Change("create", "nothing", "x", after={}))


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


def test_scope_with_leases_stays_in_its_failover_relationship(client, fake):
    push(client, wanted())
    fake.add_lease("10.1.0.0", "10.1.0.60")
    with pytest.raises(PushRefused, match="leases"):
        WindowsWriter(client).apply(Change("delete", "scope", "10.1.0.0", before=scope().fields))
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0"]


def test_deleted_scope_leaves_its_failover_relationship_first(client, fake):
    push(client, wanted())
    fake.scopes["10.2.0.0"] = {**fake.scopes["10.1.0.0"], "scope_id": "10.2.0.0"}
    fake.failovers["ha"]["scope_ids"].append("10.2.0.0")
    WindowsWriter(client).apply(Change("delete", "scope", "10.1.0.0", before=scope().fields))
    ops = [c.op for c in fake.calls]
    assert ops.index("failover.remove_scope") < ops.index("scope.remove")
    assert fake.failovers["ha"]["scope_ids"] == ["10.2.0.0"]


def test_policy_update_sets_and_moves_ip_ranges(client, fake):
    push(client, wanted())
    before = policy().fields
    after = policy(ip_ranges=[["10.1.0.150", "10.1.0.160"]], enabled=False, condition="and").fields
    WindowsWriter(client).apply(Change("update", "policy", "10.1.0.0/printers", before=before, after=after))
    assert fake.policy_ranges[("10.1.0.0", "printers")] == [
        {"scope_id": "10.1.0.0", "policy_name": "Printers", "start_range": "10.1.0.150", "end_range": "10.1.0.160"}
    ]
    assert fake.policies[("10.1.0.0", "printers")]["enabled"] is False
    assert fake.policies[("10.1.0.0", "printers")]["condition"] == "AND"


def test_clearing_a_policy_condition_is_refused(client):
    push(client, wanted())
    before = policy(criteria={"mac_address": ["EQ", "00-15-5d*"], "fqdn": ["EQ", "*.lab"]}).fields
    with pytest.raises(PushRefused, match="fqdn"):
        WindowsWriter(client).apply(
            Change("update", "policy", "10.1.0.0/printers", before=before, after=policy().fields)
        )


def test_superscope_membership_and_rename(client, fake):
    push(client, wanted())
    fake.scopes["10.2.0.0"] = {**fake.scopes["10.1.0.0"], "scope_id": "10.2.0.0"}
    before = {"name": "Campus", "scope_ids": ["10.1.0.0"]}
    after = {"name": "CAMPUS", "scope_ids": ["10.2.0.0"]}
    WindowsWriter(client).apply(Change("update", "superscope", "campus", before=before, after=after))
    assert fake.superscopes == {"CAMPUS": ["10.2.0.0"]}
    WindowsWriter(client).apply(Change("delete", "superscope", "campus", before=after))
    assert fake.superscopes == {}
    assert "10.2.0.0" in fake.scopes


def test_failover_secret_comes_from_the_callback_and_is_not_in_the_change(client, fake):
    secrets = {"HA": "s3cret"}
    change = Change("create", "failover", "ha", after=failover(auth=True).fields)
    push(client, [r for r in wanted() if r.kind != "failover"])
    WindowsWriter(client, failover_secret=secrets.get).apply(change)
    assert fake.failover_secrets == {"ha": "s3cret"}
    assert "s3cret" not in repr(change)
    assert all("s3cret" not in repr(c.params) or c.op == "failover.add" for c in fake.calls)


def test_failover_without_a_secret_is_refused(client):
    push(client, [r for r in wanted() if r.kind != "failover"])
    with pytest.raises(PushRefused, match="shared secret"):
        WindowsWriter(client).apply(Change("create", "failover", "ha", after=failover(auth=True).fields))


def test_failover_update_adds_and_removes_scopes_and_refuses_a_new_partner(client, fake):
    push(client, wanted())
    fake.scopes["10.2.0.0"] = {**fake.scopes["10.1.0.0"], "scope_id": "10.2.0.0"}
    before = failover().fields
    after = failover(scope_ids=["10.2.0.0"], load_balance_percent=70).fields
    WindowsWriter(client).apply(Change("update", "failover", "ha", before=before, after=after))
    assert fake.failovers["ha"]["scope_ids"] == ["10.2.0.0"]
    assert fake.failovers["ha"]["load_balance_percent"] == 70
    with pytest.raises(PushRefused, match="partner"):
        WindowsWriter(client).apply(
            Change("update", "failover", "ha", before=after, after={**after, "partner_server": "other"})
        )


def test_mac_filter_update_and_filter_list_delete(client, fake):
    push(client, wanted())
    before = wanted()[-2].fields
    WindowsWriter(client).apply(
        Change("update", "macfilter", "00-15-5d-*", before=before, after={**before, "list": "allow"})
    )
    assert fake.filters["00-15-5d-*"]["list"] == "Allow"
    WindowsWriter(client).apply(Change("delete", "filterlists", "server", before={"allow": False, "deny": True}))
    assert fake.filter_lists == {"allow": False, "deny": True}
