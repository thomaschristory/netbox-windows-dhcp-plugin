"""DHCPv6: records from Windows, push planning and the push writer, against the fake server."""

import pytest

from netbox_windows_dhcp_plugin.sync import push
from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.sync.diff import Record, diff
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import DhcpClass, OptionKeyV6, ReservationV6, ScopeV6
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError

PREFIX = "2001:db8:1::"
# Standard DHCPv6 codes used here: DNS servers (23) and the domain search list (24).
STANDARD6 = {21, 22, 23, 24, 31}
# Windows' default rebinding time (T2) for a new DHCPv6 scope: 6.4 days, as read back from Windows Server 2025 (#16).
WINDOWS_T2 = 6 * 86400 + 9 * 3600 + 36 * 60


@pytest.fixture
def fake():
    return FakeDhcpServer()


@pytest.fixture
def client(fake):
    return DhcpClient(fake)


def scope6(**over):
    fields = {
        "prefix": PREFIX,
        "prefix_length": 64,
        "name": "v6 users",
        "description": "office",
        "state": "active",
        "preference": 0,
        "preferred_lifetime": 8 * 86400,
        "valid_lifetime": 12 * 86400,
        "t1": 4 * 86400,
        "t2": WINDOWS_T2,
    }
    fields.update(over)
    return Record("scope6", ws.scope6_key(fields["prefix"]), fields)


def reservation6(ip="2001:db8:1::10", **over):
    fields = {
        "prefix": PREFIX,
        "ip_address": ip,
        "client_duid": "00-01-00-01-aa-bb-cc-dd",
        "iaid": 7,
        "name": "pc",
        "description": "desk",
    }
    fields.update(over)
    return Record("reservation6", ws.reservation6_key(fields["prefix"], fields["ip_address"]), fields)


def class6(name="Lab", **over):
    fields = {"name": name, "class_type": "user", "data": "LabComputers", "description": ""}
    fields.update(over)
    return Record("class6", ws.class6_key(name), fields)


def option6(parent, option_id, value, user_class=""):
    fields = {"parent": parent, "option_id": option_id, "value": value, "user_class": user_class}
    return Record("option6", ws.option6_key(parent, option_id, user_class), fields)


def wanted():
    return [
        class6(),
        scope6(),
        reservation6(),
        option6("server", 23, ["2001:db8::53"]),
        option6(PREFIX, 24, ["example.com"]),
        option6(PREFIX, 23, ["2001:db8::54", "2001:db8::55"], user_class="Lab"),
        option6(f"{PREFIX}/2001:db8:1::10", 23, ["2001:db8::56"]),
    ]


def records(client, codes=STANDARD6):
    state = ws.read(client)
    out, _ = ws.to_records(state, standard_codes=(), standard_codes6=codes)
    return [r for r in out if r.kind.endswith("6")]


def apply(client, source, managed=None):
    p = push.plan(
        source, records(client), managed or {}, hidden=ws.hidden_records(ws.read(client), standard_codes6=STANDARD6)
    )
    return p, push.execute(p, WindowsWriter(client))


def merged(managed, result):
    out = dict(managed)
    out.update(result.upserts)
    for k in result.removals:
        out.pop(k, None)
    return out


# -- records from Windows ------------------------------------------------------------------------------------------


def seed(client, fake):
    client.add_v6_class(DhcpClass(name="Lab", type="User", data="LabComputers"))
    fake.add_v6_class("Microsoft Windows Options", data="MSFT 5.0")
    client.add_v6_scope(ScopeV6(prefix="2001:DB8:1::", name="v6 users", description="office"))
    client.add_v6_reservation(
        ReservationV6(prefix=PREFIX, ip_address="2001:db8:1::10", client_duid="00:01:00:01:AA:BB:CC:DD", iaid=7)
    )
    client.set_v6_option_value(OptionKeyV6(23), ["2001:DB8::53"])
    client.set_v6_option_value(OptionKeyV6(24, prefix=PREFIX), ["example.com"])
    client.set_v6_option_value(OptionKeyV6(23, prefix=PREFIX, user_class="lab"), ["2001:db8::54"])
    client.set_v6_option_value(OptionKeyV6(23, prefix=PREFIX, reserved_ip="2001:db8:1::10"), ["2001:db8::56"])


def test_reads_every_v6_kind(client, fake):
    seed(client, fake)
    out, notes = ws.to_records(ws.read(client), standard_codes=(), standard_codes6=STANDARD6)
    keys = {(r.kind, r.key) for r in out if r.kind.endswith("6")}
    assert keys == {
        ("class6", "v6/user/lab"),
        ("scope6", "v6/2001:db8:1::"),
        ("reservation6", "v6/2001:db8:1::/2001:db8:1::10"),
        ("option6", "v6/server/23"),
        ("option6", "v6/2001:db8:1::/24"),
        ("option6", "v6/2001:db8:1::/23;user=lab"),
        ("option6", "v6/2001:db8:1::/2001:db8:1::10/23"),
    }
    by_key = {r.key: r.fields for r in out}
    assert by_key["v6/2001:db8:1::"] == scope6().fields
    assert by_key["v6/2001:db8:1::/2001:db8:1::10"]["client_duid"] == "00-01-00-01-aa-bb-cc-dd"
    # Windows prints IPv6 addresses in its own form; values keep what Windows says.
    assert by_key["v6/server/23"]["value"] == ["2001:DB8::53"]
    assert notes == ["skipped 1 DHCPv6 vendor class(es): the plugin syncs DHCPv6 user classes only"]


def test_v6_keys_never_meet_v4_keys():
    assert ws.scope6_key("2001:DB8:1:0::") == "v6/2001:db8:1::"
    assert ws.option6_key("server", 23) != ws.option_key("server", 23)
    assert ws.class6_key("Lab") == "v6/user/lab"


def test_unsupported_v6_objects_are_skipped_and_hidden(client, fake):
    seed(client, fake)
    fake.add_v6_exclusion(PREFIX, "2001:db8:1::1", "2001:db8:1::ff")
    # A vendor class value, a value of a code without a standard definition, and one with a trailing space (kept).
    fake.v6_options[(17, PREFIX, "", "Microsoft Windows Options", "")] = {
        "option_id": 17,
        "name": "vendor",
        "value": ["1"],
        "vendor_class": "Microsoft Windows Options",
        "user_class": "",
        "prefix": PREFIX,
        "reserved_ip": "",
    }
    client.set_v6_option_value(OptionKeyV6(99, prefix=PREFIX), ["x"])
    client.set_v6_option_value(OptionKeyV6(21, prefix=PREFIX, reserved_ip="2001:db8:1::10"), ["a "])
    state = ws.read(client)
    out, notes = ws.to_records(state, standard_codes=(), standard_codes6=STANDARD6)
    assert not [r for r in out if r.kind == "option6" and r.fields["option_id"] in (17, 99)]
    assert [r.fields["value"] for r in out if r.kind == "option6" and r.fields["option_id"] == 21] == [["a "]]
    assert sorted(notes) == sorted(
        [
            "skipped 1 DHCPv6 vendor class(es): the plugin syncs DHCPv6 user classes only",
            f"skipped 1 {ws.V6_VENDOR_VALUES}",
            f"skipped 1 {ws.V6_UNDEFINED}",
            f"skipped 1 {ws.V6_EXCLUSIONS}",
        ]
    )
    hidden = {(r.kind, r.key) for r in ws.hidden_records(state, standard_codes6=STANDARD6)}
    assert ("exclusion6", "v6/2001:db8:1::/2001:db8:1::1-2001:db8:1::ff") in hidden
    assert ("option6", "v6/2001:db8:1::/17;vendor=microsoft windows options") in hidden
    assert ("option6", "v6/2001:db8:1::/99") in hidden
    assert ("option6", "v6/2001:db8:1::/2001:db8:1::10/21") not in hidden


def test_ipv6_option_values_compare_in_any_spelling():
    assert ws.equal("option6", "value", ["2001:DB8:0::53"], ["2001:db8::53"])
    assert not ws.equal("option6", "value", ["2001:db8::53"], ["2001:db8::54"])
    a = option6("server", 23, ["2001:DB8:0::53"])
    b = option6("server", 23, ["2001:db8::53"])
    assert push.fingerprint(a) == push.fingerprint(b)


# -- push ----------------------------------------------------------------------------------------------------------


def test_push_creates_everything_and_a_second_push_changes_nothing(client, fake):
    p, r = apply(client, wanted())
    assert not r.failed and not p.skipped, (r.failed, p.skipped)
    assert diff(wanted(), records(client), equal=ws.equal) == []
    assert fake.v6_reservations["2001:db8:1::10"]["iaid"] == 7
    managed = merged({}, r)
    p2, r2 = apply(client, wanted(), managed)
    assert p2.changes == [] and not r2.failed
    assert merged(managed, r2) == managed


def test_push_updates(client, fake):
    _, r = apply(client, wanted())
    managed = merged({}, r)
    source = wanted()
    source[1] = scope6(name="renamed", state="inactive", preference=3, valid_lifetime=86400)
    source[2] = reservation6(client_duid="00-01-02", iaid=9, name="pc2")
    source[3] = option6("server", 23, ["2001:db8::99"])
    _, r2 = apply(client, source, managed)
    assert not r2.failed, r2.failed
    assert diff(source, records(client), equal=ws.equal) == []
    assert fake.v6_scopes[PREFIX]["state"] == "Inactive"


def test_empty_lifetimes_keep_what_windows_has(client, fake):
    apply(client, wanted())
    fake.v6_scopes[PREFIX]["t1"] = "1.00:00:00"
    source = wanted()
    source[1] = scope6(preferred_lifetime=None, valid_lifetime=None, t1=None, t2=None)
    p, _ = apply(client, source)
    assert [c for c in p.changes if c.kind == "scope6"] == []


def test_new_scope_without_lifetimes_gets_windows_defaults(client, fake):
    source = [scope6(preferred_lifetime=None, valid_lifetime=None, t1=None, t2=None, name="")]
    _, r = apply(client, source)
    assert not r.failed
    assert fake.v6_scopes[PREFIX]["valid_lifetime"] == "12.00:00:00"
    # Windows requires a name: the prefix stands in, as for IPv4 scopes.
    assert fake.v6_scopes[PREFIX]["name"] == PREFIX
    p, _ = apply(client, source, merged({}, r))
    assert p.changes == []


def test_lifetime_defaults_are_windows_own(client, fake):
    """#16: a scope pushed without T1/T2 must get what Windows gives one made without them, not 6 days 10 hours."""
    assert push.V6_LIFETIME_DEFAULTS == {
        "preferred_lifetime": 8 * 86400,
        "valid_lifetime": 12 * 86400,
        "t1": 4 * 86400,
        "t2": WINDOWS_T2,
    }
    # A scope made on Windows without lifetimes (by hand) and one pushed without them look the same.
    client.add_v6_scope(ScopeV6(prefix="2001:db8:9::", name="by hand"))
    assert fake.v6_scopes["2001:db8:9::"]["t2"] == "6.09:36:00"
    _, r = apply(client, [scope6(t1=None, t2=None)])
    assert not r.failed
    assert fake.v6_scopes[PREFIX]["t1"] == "4.00:00:00"
    assert fake.v6_scopes[PREFIX]["t2"] == "6.09:36:00"


def test_default_timers_are_half_and_80_percent_of_the_default_preferred_lifetime():
    defaults = push.V6_LIFETIME_DEFAULTS
    assert push.v6_timers(defaults["preferred_lifetime"]) == {"t1": defaults["t1"], "t2": defaults["t2"]}


def test_new_scope_with_a_short_preferred_lifetime_gets_timers_below_it(client, fake):
    """T1 and T2 left empty follow the preferred lifetime, never 4 and 6.4 days past it (T1 < T2 < preferred)."""
    source = [scope6(preferred_lifetime=2 * 86400, valid_lifetime=3 * 86400, t1=None, t2=None)]
    _, r = apply(client, source)
    assert not r.failed, r.failed
    assert fake.v6_scopes[PREFIX]["t1"] == "1.00:00:00"
    assert fake.v6_scopes[PREFIX]["t2"] == "1.14:24:00"  # 80 % of 2 days
    p, _ = apply(client, source, merged({}, r))
    assert p.changes == []


def test_shortened_preferred_lifetime_moves_empty_timers_below_it(client, fake):
    _, r = apply(client, [scope6(t1=None, t2=None)])
    managed = merged({}, r)
    # Windows keeps 4 and 6.4 days while they still fit below the preferred lifetime...
    source = [scope6(preferred_lifetime=7 * 86400, t1=None, t2=None)]
    _, r2 = apply(client, source, managed)
    assert not r2.failed, r2.failed
    assert (fake.v6_scopes[PREFIX]["t1"], fake.v6_scopes[PREFIX]["t2"]) == ("4.00:00:00", "6.09:36:00")
    # ...and they follow it down once they would not.
    source = [scope6(preferred_lifetime=2 * 86400, valid_lifetime=3 * 86400, t1=None, t2=None)]
    _, r3 = apply(client, source, merged(managed, r2))
    assert not r3.failed, r3.failed
    assert (fake.v6_scopes[PREFIX]["t1"], fake.v6_scopes[PREFIX]["t2"]) == ("1.00:00:00", "1.14:24:00")


@pytest.mark.parametrize(
    ("t1", "t2", "expected"),
    [
        # Only T2 set, below the default T1 of 4 days: T1 goes to 5/8 of T2 (15 hours).
        (None, 86400, ("0.15:00:00", "1.00:00:00")),
        # Only T1 set, past the default T2 of 6.4 days: T2 goes halfway to the preferred lifetime of 8 days.
        (7 * 86400, None, ("7.00:00:00", "7.12:00:00")),
        # Only one set, and the default for the other still fits: the default is kept.
        (None, 5 * 86400, ("4.00:00:00", "5.00:00:00")),
        (86400, None, ("1.00:00:00", "6.09:36:00")),
    ],
)
def test_new_scope_with_one_timer_set_gets_the_other_on_the_right_side_of_it(client, fake, t1, t2, expected):
    source = [scope6(t1=t1, t2=t2)]
    _, r = apply(client, source)
    assert not r.failed, r.failed
    assert (fake.v6_scopes[PREFIX]["t1"], fake.v6_scopes[PREFIX]["t2"]) == expected
    p, _ = apply(client, source, merged({}, r))
    assert p.changes == []


def test_one_timer_set_below_the_windows_partner_moves_the_partner(client, fake):
    _, r = apply(client, [scope6(t1=None, t2=None)])
    managed = merged({}, r)
    # Windows has 4 and 6.4 days; NetBox now sets T2 to 1 day and leaves T1 empty: 4 days would break T1 < T2.
    source = [scope6(t1=None, t2=86400)]
    _, r2 = apply(client, source, managed)
    assert not r2.failed, r2.failed
    assert (fake.v6_scopes[PREFIX]["t1"], fake.v6_scopes[PREFIX]["t2"]) == ("0.15:00:00", "1.00:00:00")
    # A Windows T1 that still fits is kept.
    fake.v6_scopes[PREFIX]["t1"] = "0.12:00:00"
    p, _ = apply(client, source, merged(managed, r2))
    assert p.changes == []


def test_fake_refuses_timers_past_the_preferred_lifetime(client, fake):
    with pytest.raises(PSError):
        client.add_v6_scope(ScopeV6(prefix=PREFIX, name="v6", preferred_lifetime="2.00:00:00"))
    assert PREFIX not in fake.v6_scopes


def test_deletes_children_first(client, fake):
    _, r = apply(client, wanted())
    _, r2 = apply(client, [], merged({}, r))
    assert not r2.failed, r2.failed
    assert fake.v6_scopes == {} and fake.v6_options == {} and fake.v6_classes == {}


def test_scope_with_a_hidden_exclusion_is_not_deleted(client, fake):
    _, r = apply(client, wanted())
    fake.add_v6_exclusion(PREFIX, "2001:db8:1::1", "2001:db8:1::ff")
    p, _ = apply(client, [], merged({}, r))
    assert PREFIX in fake.v6_scopes
    assert any(s.change.kind == "scope6" and "exclusion6" in s.reason for s in p.skipped)


def test_scope_with_leases_is_not_forced(client, fake):
    source = [scope6()]
    _, r = apply(client, source)
    fake.add_v6_lease(PREFIX, "2001:db8:1::1:10")
    _, r2 = apply(client, [], merged({}, r))
    assert PREFIX in fake.v6_scopes
    assert r2.failed and "used by a client" in r2.failed[0][1]


def test_unpushable_v6_records(client):
    source = [
        scope6(prefix="2001:db8:2::", prefix_length=48),
        reservation6(client_duid=""),
        reservation6(ip="2001:db8:1::11", iaid=None),
        option6(PREFIX, 23, []),
        scope6(),
    ]
    p = push.plan(source, [], {})
    reasons = {s.change.key: s.reason for s in p.skipped}
    assert "/64" in reasons["v6/2001:db8:2::"]
    assert "DUID" in reasons["v6/2001:db8:1::/2001:db8:1::10"]
    assert "IAID" in reasons["v6/2001:db8:1::/2001:db8:1::11"]
    assert "no value" in reasons["v6/2001:db8:1::/23"]


def test_v6_parents():
    assert push.parents("reservation6", reservation6().fields) == [("scope6", "v6/2001:db8:1::")]
    assert push.parents("option6", option6(f"{PREFIX}/2001:db8:1::10", 23, ["x"], user_class="Lab").fields) == [
        ("scope6", "v6/2001:db8:1::"),
        ("reservation6", "v6/2001:db8:1::/2001:db8:1::10"),
        ("class6", "v6/user/lab"),
    ]
    assert push.parents("option6", option6("server", 23, ["x"]).fields) == []


def test_only_user_classes_are_pushed():
    p = push.plan([class6(name="V", class_type="vendor")], [], {})
    assert p.changes == [] and "user classes" in p.skipped[0].reason


def test_policy_values_are_not_pushed():
    record = option6(PREFIX, 23, ["2001:db8::53"])
    record = Record(record.kind, record.key, {**record.fields, "policy": "printers"})
    p = push.plan([scope6(), record], [], {})
    assert [s.change.key for s in p.skipped] == [record.key]
    assert "policies" in p.skipped[0].reason


def test_user_class_names_are_not_case_sensitive_on_windows(client):
    client.add_v6_scope(ScopeV6(prefix=PREFIX, name="v6"))
    client.add_v6_class(DhcpClass(name="Lab", type="User", data="Lab"))
    client.set_v6_option_value(OptionKeyV6(23, prefix=PREFIX, user_class="Lab"), ["2001:db8::1"])
    client.set_v6_option_value(OptionKeyV6(23, prefix=PREFIX, user_class="lab"), ["2001:db8::2"])
    (value,) = client.list_v6_option_values(prefix=PREFIX, all=True)
    assert value.value == ["2001:db8::2"]


def test_hidden_records_keeps_its_positional_parameters(client):
    # standard_codes6 is keyword only, so existing positional callers keep their meaning.
    assert ws.hidden_records(ws.read(client), ws.SKIPPED_OPTION_IDS) == []


def test_reservation_description_change_keeps_its_option_values(client, fake):
    """Issue #42, DHCPv6: the update only shows the reservation, and its option 24 survives."""
    source = [*wanted(), option6(f"{PREFIX}/2001:db8:1::10", 24, ["host.example"])]
    _, first = apply(client, source)
    managed = merged({}, first)
    fake.reservation_set_drops_options = True
    source[2] = reservation6(description="changed")
    p, result = apply(client, source, managed)
    assert [(c.action, c.kind) for c in p.changes] == [("update", "reservation6")]
    assert len(result.applied) == 1 and not result.failed
    values = {k[0]: v["value"] for k, v in fake.v6_options.items() if k[2] == "2001:db8:1::10"}
    assert values == {23: ["2001:db8::56"], 24: ["host.example"]}
    assert apply(client, source, merged(managed, result))[0].changes == []
