"""DHCPv6 lab tests. One scope only, 2001:db8:4e42::/64, so the tests in this module run one after the other."""

from __future__ import annotations

import pytest

from netbox_windows_dhcp_plugin.sync import push
from netbox_windows_dhcp_plugin.sync.diff import Change
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import client as wc
from netbox_windows_dhcp_plugin.windows import normalize
from netbox_windows_dhcp_plugin.windows.psscript import PSError

from .conftest import save_probe
from .labkit import V6_PREFIX, Lab

DUID = "00-01-00-01-4e-42-00-00-02-4e-42-00-00-01"


def test_v6_scope_defaults_and_lifetimes(lab: Lab, probe_out) -> None:
    """client.py: v6 scope output names; push.py: T1/T2 defaults and whether Set accepts them."""
    s = lab.v6_scope("nbwd-test-v6", description="lab")
    save_probe(probe_out, "v6-scope", lab.dump("probe.v6scope", "Get-DhcpServerv6Scope -Prefix $p.x", {"x": V6_PREFIX}))
    save_probe(probe_out, "v6-scope-client", s.to_dict())
    assert s.prefix == V6_PREFIX
    assert s.name == "nbwd-test-v6" and s.description == "lab"
    assert normalize.parse_duration(s.preferred_lifetime) == 8 * 86400
    assert normalize.parse_duration(s.valid_lifetime) == 12 * 86400
    # Windows' own defaults, which push.V6_LIFETIME_DEFAULTS sends for a new scope: T1 4 days, T2 6.4 days (#16).
    assert normalize.parse_duration(s.t1, allow_zero=True) == push.V6_LIFETIME_DEFAULTS["t1"] == 4 * 86400
    assert normalize.parse_duration(s.t2, allow_zero=True) == push.V6_LIFETIME_DEFAULTS["t2"] == 552960

    s.preference = 5
    s.t1 = "1.00:00:00"
    s.t2 = "2.00:00:00"
    s.preferred_lifetime = "3.00:00:00"
    s.valid_lifetime = "4.00:00:00"
    after = lab.client.set_v6_scope(s)
    assert after.preference == 5
    assert normalize.parse_duration(after.t1) == 86400
    assert normalize.parse_duration(after.t2) == 2 * 86400

    # Setting the defaults back explicitly is accepted.
    after.t1, after.t2 = "4.00:00:00", "6.09:36:00"
    after.preferred_lifetime, after.valid_lifetime = "8.00:00:00", "12.00:00:00"
    back = lab.client.set_v6_scope(after)
    assert normalize.parse_duration(back.t1) == 4 * 86400
    assert normalize.parse_duration(back.t2) == 552960


def test_v6_scope_timers_with_a_short_preferred_lifetime(lab: Lab, probe_out) -> None:
    """
    push.py: what Windows gives T1 and T2 for a scope created with a 2 day preferred lifetime and no timers (fixed
    4 and 6.4 days, or half and 80 % of it, or a refusal), and that the timers a push works out instead are accepted.
    """
    preferred = 2 * 86400
    try:
        s = lab.v6_scope("nbwd-test-v6short", preferred_lifetime="2.00:00:00", valid_lifetime="3.00:00:00")
    except PSError as exc:
        save_probe(probe_out, "v6-scope-short-preferred", {"refused": str(exc)})
        s = lab.v6_scope("nbwd-test-v6short")
    else:
        save_probe(probe_out, "v6-scope-short-preferred", s.to_dict())
    # Whatever Windows chose by itself, the timers a push sends when NetBox leaves them empty are accepted.
    timers = push.v6_timers(preferred)
    s.preferred_lifetime, s.valid_lifetime = "2.00:00:00", "3.00:00:00"
    s.t1, s.t2 = normalize.format_duration(timers["t1"]), normalize.format_duration(timers["t2"])
    after = lab.client.set_v6_scope(s)
    assert normalize.parse_duration(after.t1) == timers["t1"] == 86400
    assert normalize.parse_duration(after.t2) == timers["t2"]


def test_v6_reservation_exclusion_lease_and_options(lab: Lab, probe_out) -> None:
    """client.py: v6 reservation keyed by -IPAddress; exclusion and lease output names; -Prefix with -ReservedIP."""
    lab.v6_scope("nbwd-test-v6res")
    ip = "2001:db8:4e42::20"
    r = lab.client.add_v6_reservation(
        wc.ReservationV6(
            prefix=V6_PREFIX, ip_address=ip, client_duid=DUID, iaid=7, name="nbwd-test-v6host", description="lab"
        )
    )
    save_probe(
        probe_out,
        "v6-reservation",
        lab.dump("probe.v6res", "Get-DhcpServerv6Reservation -Prefix $p.x", {"x": V6_PREFIX}),
    )
    assert r.ip_address == ip and r.iaid == 7 and r.client_duid.lower() == DUID
    r.description = "changed"
    assert lab.client.set_v6_reservation(r).description == "changed"

    # Option on the reservation: Windows takes -ReservedIP alone on Set and Remove (issue #17); the prefix stays in
    # what the client returns.
    k = wc.OptionKeyV6(24, prefix=V6_PREFIX, reserved_ip=ip)
    ov = lab.client.set_v6_option_value(k, ["lab.example"])
    assert (ov.value, ov.prefix, ov.reserved_ip) == (["lab.example"], V6_PREFIX, ip)
    assert lab.client.get_v6_option_value(k).value == ["lab.example"]
    assert [x.value for x in lab.client.list_v6_option_values(prefix=V6_PREFIX, reserved_ip=ip)] == [["lab.example"]]
    lab.client.remove_v6_option_value(k)
    with pytest.raises(wc.NotFound):
        lab.client.get_v6_option_value(k)

    lab.raw(
        "probe.v6excl",
        "Add-DhcpServerv6ExclusionRange -Prefix $p.x -StartRange $p.a -EndRange $p.b",
        {"x": V6_PREFIX, "a": "2001:db8:4e42::100", "b": "2001:db8:4e42::110"},
    )
    save_probe(
        probe_out,
        "v6-exclusion",
        lab.dump("probe.v6excl", "Get-DhcpServerv6ExclusionRange -Prefix $p.x", {"x": V6_PREFIX}),
    )
    ex = lab.client.list_v6_exclusion_ranges(V6_PREFIX)
    assert [(e.prefix, e.start_range, e.end_range) for e in ex] == [
        (V6_PREFIX, "2001:db8:4e42::100", "2001:db8:4e42::110")
    ]

    lab.raw(
        "probe.v6lease",
        "Add-DhcpServerv6Lease -IPAddress $p.ip -ClientDuid $p.d -Iaid 9 -HostName 'nbwd-test-v6lease' "
        "-LeaseExpiryTime ((Get-Date).AddDays(1))",
        {"ip": "2001:db8:4e42::30", "d": DUID.replace("-01", "-02", 1)},
    )
    save_probe(probe_out, "v6-lease", lab.dump("probe.v6lease", "Get-DhcpServerv6Lease -Prefix $p.x", {"x": V6_PREFIX}))
    leases = [x for x in lab.client.list_v6_leases(V6_PREFIX) if x.ip_address == "2001:db8:4e42::30"]
    save_probe(probe_out, "v6-lease-client", [x.to_dict() for x in leases])
    assert len(leases) == 1
    lease = leases[0]
    assert lease.iaid == 9 and lease.client_duid
    assert lease.host_name == "nbwd-test-v6lease"
    # Windows reports no state for a DHCPv6 lease (issue #22), so the client keeps none.
    assert "address_state" not in lease.to_dict()
    assert lease.lease_expiry_time.endswith("Z")


def test_v6_classes(lab: Lab, probe_out) -> None:
    """client.py: v6 class data in .AsciiData or .Data; Set-DhcpServerv6Class parameters."""
    c = lab.user_class("nbwd-test-v6user", "nbwd-v6-data", "lab", v6=True)
    save_probe(
        probe_out,
        "v6-classes",
        lab.dump("probe.v6class", "Get-DhcpServerv6Class | Where-Object { $_.Name -like 'nbwd-test-*' }"),
    )
    assert c.data == "nbwd-v6-data" and c.description == "lab"
    c.description = "changed"
    c.data = "nbwd-v6-data2"
    after = lab.client.set_v6_class(c)
    assert after.description == "changed" and after.data == "nbwd-v6-data2"


def test_v6_option_values_all(lab: Lab, probe_out) -> None:
    """windows_state.py: -All on Get-DhcpServerv6OptionValue returns user class values."""
    lab.v6_scope("nbwd-test-v6all")
    c = lab.user_class("nbwd-test-v6all-user", "nbwd-v6all", v6=True)
    lab.client.set_v6_option_value(wc.OptionKeyV6(24, prefix=V6_PREFIX), ["plain.example"])
    lab.client.set_v6_option_value(wc.OptionKeyV6(24, prefix=V6_PREFIX, user_class=c.name), ["user.example"])
    values = lab.client.list_v6_option_values(prefix=V6_PREFIX, all=True)
    save_probe(probe_out, "v6-options-all", [x.to_dict() for x in values])
    seen = {(x.option_id, x.user_class): x.value for x in values}
    assert seen.get((24, "")) == ["plain.example"]
    assert seen.get((24, c.name)) == ["user.example"]


def test_v6_reservation_update_keeps_its_option_values(lab: Lab, probe_out) -> None:
    """Issue #42: client.py sends the DUID and IAID only on change; whether a new IAID drops the values."""
    lab.v6_scope("nbwd-test-v6res42")
    ip = "2001:db8:4e42::21"
    r = lab.client.add_v6_reservation(wc.ReservationV6(V6_PREFIX, ip, DUID, 7, "nbwd-test-v6host42", "lab"))
    k = wc.OptionKeyV6(24, prefix=V6_PREFIX, reserved_ip=ip)
    lab.client.set_v6_option_value(k, ["host.example"])
    r.description = "changed"
    assert lab.client.set_v6_reservation(r).description == "changed"
    assert lab.client.get_v6_option_value(k).value == ["host.example"]

    r.iaid = 8
    assert lab.client.set_v6_reservation(r).iaid == 8
    left = lab.client.list_v6_option_values(prefix=V6_PREFIX, reserved_ip=ip, all=True)
    save_probe(probe_out, "v6-reservation-identity-change-options", [x.to_dict() for x in left])

    lab.client.set_v6_option_value(k, ["host.example"])
    before = {
        "prefix": V6_PREFIX,
        "ip_address": ip,
        "client_duid": DUID,
        "iaid": 8,
        "name": r.name,
        "description": "changed",
    }
    change = Change("update", "reservation6", ip, before=before, after={**before, "description": "again"})
    WindowsWriter(lab.client).apply(change)
    assert lab.client.get_v6_option_value(k).value == ["host.example"]


def test_v6_scope_refuses_timers_out_of_order(lab: Lab, probe_out) -> None:
    """fake.py: Windows refuses T1 < T2 < preferred lifetime not holding, rather than storing the timers."""
    lab.v6_scope("nbwd-test-v6timers")
    seen: dict[str, str] = {}
    for label, t1, t2 in (("t1_above_t2", 3, 2), ("t2_above_preferred", 4, 9)):
        try:
            lab.raw(
                "probe.v6timers",
                "Set-DhcpServerv6Scope -Prefix $p.x -T1 ([TimeSpan]::FromDays($p.a)) -T2 ([TimeSpan]::FromDays($p.b))",
                {"x": V6_PREFIX, "a": t1, "b": t2},
            )
            got = lab.client.get_v6_scope(V6_PREFIX)
            seen[label] = f"stored: t1={got.t1} t2={got.t2}"
        except PSError as exc:
            seen[label] = f"refused: {exc.id}: {exc}"
    save_probe(probe_out, "v6-scope-timers-out-of-order", seen)
    assert all(v.startswith("refused") for v in seen.values()), seen
