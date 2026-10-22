"""DhcpClient end to end against the in-memory fake server."""

from __future__ import annotations

import threading

import pytest

from netbox_windows_dhcp_plugin.windows.client import (
    DhcpClass,
    DhcpClient,
    ExclusionRange,
    FailoverRelationship,
    MacFilter,
    OptionDefinition,
    OptionKey,
    OptionKeyV6,
    Policy,
    PolicyIPRange,
    Reservation,
    ReservationV6,
    Scope,
    ScopeV6,
    Superscope,
)
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import NotFound, PSError
from netbox_windows_dhcp_plugin.windows.runner import Runner, limit


@pytest.fixture
def fake() -> FakeDhcpServer:
    return FakeDhcpServer()


@pytest.fixture
def c(fake: FakeDhcpServer) -> DhcpClient:
    return DhcpClient(fake)


def scope(start: str = "10.1.20.10", end: str = "10.1.20.250", mask: str = "255.255.255.0", **kw) -> Scope:
    return Scope(name=kw.pop("name", "Users"), start_range=start, end_range=end, subnet_mask=mask, **kw)


def test_fake_is_a_runner(fake: FakeDhcpServer) -> None:
    assert isinstance(fake, Runner)
    with pytest.raises(ValueError, match="op marker"):
        fake.run("Get-DhcpServerv4Scope", {})


def test_unknown_op(fake: FakeDhcpServer) -> None:
    from netbox_windows_dhcp_plugin.windows.psscript import Script

    with pytest.raises(PSError, match="unknown op"):
        DhcpClient(fake)._run(Script("nope.nothing", "$out = 1"))


def test_scope_lifecycle(c: DhcpClient, fake: FakeDhcpServer) -> None:
    c.check_available()
    s = c.add_scope(scope(start="10.1.21.10", end="10.1.21.200", mask="255.255.254.0", state="Inactive"))
    assert s.scope_id == "10.1.20.0"  # derived from start AND mask
    assert s.state == "Inactive"  # sent as InActive, read back as Inactive
    assert s.lease_duration == "8.00:00:00" and s.activate_policies is True
    assert fake.calls[-1].params["state"] == "InActive"
    with pytest.raises(PSError, match="already exists") as ei:
        c.add_scope(scope(start="10.1.20.50", end="10.1.20.60", mask="255.255.254.0"))
    assert not isinstance(ei.value, NotFound)
    assert [x.scope_id for x in c.list_scopes()] == ["10.1.20.0"]

    upd = c.set_scope(
        Scope(**{**s.to_dict(), "start_range": "10.1.20.20", "lease_duration": "1.00:00:00", "description": "d"})
    )
    assert upd.start_range == "10.1.20.20" and upd.lease_duration == "1.00:00:00" and upd.description == "d"
    assert upd.subnet_mask == "255.255.254.0"
    with pytest.raises(PSError, match="range"):
        c.set_scope(Scope(**{**s.to_dict(), "end_range": "10.1.30.1"}))

    c.remove_scope("10.1.20.0")
    with pytest.raises(NotFound):
        c.get_scope("10.1.20.0")
    with pytest.raises(NotFound):
        c.remove_scope("10.1.20.0")


def test_add_scope_with_mismatched_scope_id_reads_back_not_found(c: DhcpClient) -> None:
    with pytest.raises(NotFound):
        c.add_scope(scope(scope_id="10.9.9.0"))


def test_remove_scope_with_leases_needs_force(c: DhcpClient, fake: FakeDhcpServer) -> None:
    c.add_scope(scope())
    fake.add_lease("10.1.20.0", "10.1.20.11")
    assert c.list_leases("10.1.20.0")[0].ip_address == "10.1.20.11"
    with pytest.raises(PSError) as ei:
        c.remove_scope("10.1.20.0")
    assert ei.value.id.startswith("DHCP 20007,") and not ei.value.not_found
    c.remove_scope("10.1.20.0", force=True)
    assert fake.leases == {}


def test_scope_remove_cascades(c: DhcpClient, fake: FakeDhcpServer) -> None:
    c.add_scope(scope())
    c.add_scope(scope("10.1.30.10", "10.1.30.20", name="Voice"))
    c.add_reservation(Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="aa:bb:cc:dd:ee:ff"))
    c.add_exclusion_range(ExclusionRange("10.1.20.0", "10.1.20.1", "10.1.20.9"))
    c.set_option_value(OptionKey(3, scope_id="10.1.20.0"), ["10.1.20.1"])
    c.set_option_value(OptionKey(6, reserved_ip="10.1.20.5"), ["10.0.0.53"])
    c.set_option_value(OptionKey(6), ["10.0.0.53"])
    c.add_policy(Policy(name="p", scope_id="10.1.20.0", criteria={"mac_address": ["EQ", "aa-bb*"]}))
    c.add_failover(FailoverRelationship(name="fo", partner_server="dhcp02", scope_ids=["10.1.20.0", "10.1.30.0"]))
    c.add_superscope(Superscope("ss", ["10.1.20.0", "10.1.30.0"]))
    c.remove_scope("10.1.20.0")
    assert fake.reservations == {} and fake.exclusions == [] and fake.policies == {}
    assert list(fake.options) == [(6, "", "", "", "", "")]  # server-level option survives
    assert c.get_failover("fo").scope_ids == ["10.1.30.0"]
    assert c.get_superscope("ss").scope_ids == ["10.1.30.0"]


def test_reservations(c: DhcpClient) -> None:
    with pytest.raises(NotFound):
        c.get_reservation("10.1.20.0", "10.1.20.5")  # scope missing: DHCP 20005
    c.add_scope(scope())
    with pytest.raises(NotFound):
        c.get_reservation("10.1.20.0", "10.1.20.5")  # no match: $out = $null
    r = c.add_reservation(Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="AABB.CCDD.EEFF"))
    assert r.name == "10.1.20.5" and r.client_id == "aa-bb-cc-dd-ee-ff" and r.type == "Both"
    with pytest.raises(PSError, match="taken"):
        c.add_reservation(Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="00-11-22-33-44-55"))
    with pytest.raises(PSError, match="not in the scope"):
        c.add_reservation(Reservation(scope_id="10.1.20.0", ip_address="10.1.21.5", client_id="00-11-22-33-44-55"))
    r = c.set_reservation(
        Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="00:11:22:33:44:55", name="printer")
    )
    assert r.client_id == "00-11-22-33-44-55" and r.name == "printer" and r.description == ""
    assert [x.ip_address for x in c.list_reservations("10.1.20.0")] == ["10.1.20.5"]
    c.set_option_value(OptionKey(6, reserved_ip="10.1.20.5"), ["10.0.0.53"])
    c.remove_reservation("10.1.20.5")
    with pytest.raises(NotFound):
        c.remove_reservation("10.1.20.5")  # DHCP 20018
    with pytest.raises(NotFound):
        c.get_option_value(OptionKey(6, reserved_ip="10.1.20.5"))


def test_exclusions(c: DhcpClient) -> None:
    c.add_scope(scope())
    e = ExclusionRange("10.1.20.0", "10.1.20.1", "10.1.20.9")
    assert c.add_exclusion_range(e) == e
    with pytest.raises(PSError, match="already defined"):
        c.add_exclusion_range(e)
    with pytest.raises(PSError):
        c.add_exclusion_range(ExclusionRange("10.1.20.0", "10.1.20.9", "10.1.20.1"))
    assert c.list_exclusion_ranges("10.1.20.0") == [e]
    assert c.get_exclusion_range(e) == e
    c.remove_exclusion_range(e)
    with pytest.raises(NotFound):
        c.get_exclusion_range(e)


def test_option_values(c: DhcpClient) -> None:
    c.add_scope(scope())
    k = OptionKey(3, scope_id="10.1.20.0")
    with pytest.raises(NotFound):
        c.get_option_value(k)
    v = c.set_option_value(k, ["10.1.20.1"])
    assert v.name == "Router" and v.value == ["10.1.20.1"] and v.scope_id == "10.1.20.0"
    v = c.set_option_value(OptionKey(250, scope_id="10.1.20.0"), ["0x0A", "7"])
    assert v.value == ["10", "7"] and v.name == "Option 250"
    c.set_option_value(OptionKey(6, scope_id="10.1.20.0", user_class="phones"), ["10.0.0.53"])
    assert [o.option_id for o in c.list_option_values(scope_id="10.1.20.0")] == [3, 250]
    assert [o.option_id for o in c.list_option_values(scope_id="10.1.20.0", all=True)] == [3, 6, 250]
    with pytest.raises(NotFound):
        c.get_option_value(OptionKey(3, scope_id="10.9.9.0"))
    c.remove_option_value(k)
    with pytest.raises(NotFound):
        c.remove_option_value(k)  # DHCP 20010


def test_option_definitions(c: DhcpClient) -> None:
    assert c.get_option_definition(6).name == "DNS Servers"
    d = c.add_option_definition(OptionDefinition(252, "WPAD", "String", default_value=["http://wpad/wpad.dat"]))
    assert d.default_value == ["http://wpad/wpad.dat"]
    with pytest.raises(PSError, match="already exists"):
        c.add_option_definition(OptionDefinition(252, "WPAD", "String"))
    with pytest.raises(NotFound):
        c.add_option_definition(OptionDefinition(1, "x", "Byte", vendor_class="nope"))
    d = c.set_option_definition(OptionDefinition(252, "WPAD", "String", description="proxy"))
    assert d.description == "proxy"
    assert 252 in [x.option_id for x in c.list_option_definitions()]
    c.remove_option_definition(252)
    with pytest.raises(NotFound):
        c.get_option_definition(252)


def test_classes(c: DhcpClient) -> None:
    cl = c.add_class(DhcpClass("phones", "user", "voip", "IP phones"))
    assert cl.type == "User" and cl.data == "voip"
    with pytest.raises(PSError, match="already exists"):
        c.add_class(DhcpClass("PHONES", "User", "x"))
    c.add_class(DhcpClass("Contoso", "Vendor", "CONTOSO 1.0"))
    assert [x.name for x in c.list_classes()] == ["phones", "Contoso"]
    assert [x.name for x in c.list_classes("vendor")] == ["Contoso"]
    assert c.set_class(DhcpClass("phones", "User", "voip2")).data == "voip2"
    assert c.get_class("phones", "user").description == ""
    c.add_scope(scope())
    c.set_option_value(OptionKey(6, scope_id="10.1.20.0", user_class="phones"), ["10.0.0.53"])
    c.remove_class("phones", "User")
    with pytest.raises(NotFound):
        c.get_class("phones", "User")
    with pytest.raises(NotFound):
        c.remove_class("phones", "User")
    assert c.list_option_values(scope_id="10.1.20.0", all=True) == []


def test_failover(c: DhcpClient, fake: FakeDhcpServer) -> None:
    c.add_scope(scope())
    c.add_scope(scope("10.1.30.10", "10.1.30.20", name="Voice"))
    with pytest.raises(NotFound):
        c.get_failover("fo")
    with pytest.raises(NotFound):
        c.add_failover(FailoverRelationship(name="fo", partner_server="dhcp02", scope_ids=["10.9.9.0"]))
    f = c.add_failover(
        FailoverRelationship(
            name="fo", partner_server="dhcp02", mode="HotStandby", server_role="Active", scope_ids=["10.1.20.0"]
        ),
        shared_secret="s3cret",
    )
    assert f.mode == "HotStandby" and f.reserve_percent == 5 and f.enable_auth and f.state == "Normal"
    assert fake.failover_secrets == {"fo": "s3cret"}
    with pytest.raises(PSError, match="already part"):
        c.add_failover(FailoverRelationship(name="other", partner_server="dhcp03", scope_ids=["10.1.20.0"]))
    f = c.set_failover(FailoverRelationship(name="fo", reserve_percent=10, max_client_lead_time=7200))
    assert f.reserve_percent == 10 and f.max_client_lead_time == 7200
    assert c.add_failover_scopes("fo", ["10.1.30.0"]).scope_ids == ["10.1.20.0", "10.1.30.0"]
    assert c.remove_failover_scopes("fo", ["10.1.20.0"]).scope_ids == ["10.1.30.0"]
    with pytest.raises(PSError, match="last scope"):
        c.remove_failover_scopes("fo", ["10.1.30.0"])
    c.replicate_failover("fo")
    assert fake.replications == [{"name": "fo", "scope_ids": []}]
    assert [x.name for x in c.list_failovers()] == ["fo"]
    c.remove_failover("fo", force=True)
    assert c.list_failovers() == []


def test_superscopes(c: DhcpClient) -> None:
    for i in (20, 30, 40):
        c.add_scope(scope(f"10.1.{i}.10", f"10.1.{i}.20", name=f"s{i}"))
    ss = c.add_superscope(Superscope("A", ["10.1.20.0", "10.1.30.0"]))
    assert ss.scope_ids == ["10.1.20.0", "10.1.30.0"]
    assert c.get_scope("10.1.20.0").superscope_name == "A"
    assert c.set_superscope(Superscope("A", ["10.1.30.0", "10.1.40.0"])).scope_ids == ["10.1.30.0", "10.1.40.0"]
    assert c.get_scope("10.1.20.0").superscope_name == ""
    c.add_superscope(Superscope("B", ["10.1.40.0"]))  # moves the scope
    assert c.get_superscope("A").scope_ids == ["10.1.30.0"]
    assert c.rename_superscope("A", "C").name == "C"
    with pytest.raises(NotFound):
        c.get_superscope("A")
    assert [s.name for s in c.list_superscopes()] == ["B", "C"]
    c.remove_superscope("C")
    assert c.get_scope("10.1.30.0").superscope_name == ""
    with pytest.raises(NotFound):
        c.remove_superscope("C")


def test_filters(c: DhcpClient) -> None:
    f = c.add_filter(MacFilter("AA:BB:CC:DD:EE:FF", "deny", "stolen"))
    assert f == MacFilter("aa-bb-cc-dd-ee-ff", "Deny", "stolen")
    with pytest.raises(PSError):
        c.add_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Allow"))
    assert c.set_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Allow", "")).list == "Allow"
    assert c.get_filter("AABBCCDDEEFF").list == "Allow"
    c.add_filter(MacFilter("00-11-22-*", "Deny"))
    assert [x.mac_address for x in c.list_filters("Deny")] == ["00-11-22-*"]
    c.remove_filter("aa:bb:cc:dd:ee:ff")
    with pytest.raises(NotFound):
        c.get_filter("aa:bb:cc:dd:ee:ff")
    assert c.get_filter_lists().allow is False
    st = c.set_filter_lists(allow=False, deny=True)
    assert st.deny and not st.allow


def test_policies(c: DhcpClient) -> None:
    c.add_scope(scope())
    with pytest.raises(PSError, match="condition"):
        c.add_policy(Policy(name="empty", scope_id="10.1.20.0"))
    pol = c.add_policy(
        Policy(
            name="printers",
            scope_id="10.1.20.0",
            criteria={"mac_address": ["EQ", "3c-d9-2b*"]},
            lease_duration="1.00:00:00",
        )
    )
    assert pol.processing_order == 1 and pol.enabled and pol.lease_duration == "1.00:00:00"
    srv = c.add_policy(Policy(name="srv", criteria={"user_class": ["EQ", "phones"]}, enabled=False))
    assert srv.scope_id == "" and srv.enabled is False
    pol = c.set_policy(Policy(name="printers", scope_id="10.1.20.0", description="HP", condition="and"))
    # Windows stores a MAC prefix with a separator before the wildcard (#21).
    assert pol.description == "HP" and pol.condition == "AND" and pol.criteria == {"mac_address": ["EQ", "3c-d9-2b-*"]}
    assert [p.name for p in c.list_policies("10.1.20.0")] == ["printers"]
    assert [p.name for p in c.list_policies()] == ["srv"]
    r = c.add_policy_ip_range(PolicyIPRange("10.1.20.0", "printers", "10.1.20.100", "10.1.20.120"))
    assert r.start_range == "10.1.20.100"
    with pytest.raises(PSError):
        c.add_policy_ip_range(PolicyIPRange("10.1.20.0", "printers", "10.1.21.1", "10.1.21.2"))
    assert len(c.list_policy_ip_ranges("10.1.20.0", "printers")) == 1
    c.remove_policy_ip_range(r)
    assert c.list_policy_ip_ranges("10.1.20.0", "printers") == []
    c.remove_policy("printers", "10.1.20.0")
    with pytest.raises(NotFound):
        c.get_policy("printers", "10.1.20.0")
    with pytest.raises(NotFound):
        c.remove_policy("printers", "10.1.20.0")


def test_policy_conditions_are_stored_as_windows_stores_them(c: DhcpClient) -> None:
    """#21: Windows Server 2025 reads back "02-4e-42-ab*" as "02-4e-42-ab-*" and "*.Example.Test" in lowercase."""
    c.add_scope(scope())
    pol = c.add_policy(
        Policy(
            name="lab",
            scope_id="10.1.20.0",
            criteria={
                "mac_address": ["EQ", "02-4e-42-ab*"],
                "fqdn": ["EQ", "*.Example.Test"],
                "user_class": ["EQ", "Phones"],
            },
        )
    )
    assert pol.criteria == {
        "mac_address": ["EQ", "02-4e-42-ab-*"],
        "fqdn": ["EQ", "*.example.test"],
        "user_class": ["EQ", "Phones"],
    }
    pol = c.set_policy(
        Policy(name="lab", scope_id="10.1.20.0", criteria={"mac_address": ["NE", "AA-BB*", "cc-dd-ee*"]})
    )
    assert pol.criteria["mac_address"] == ["NE", "aa-bb-*", "cc-dd-ee-*"]


def test_policy_processing_order(c: DhcpClient) -> None:
    """#24: Windows numbers the policies of a level 1, 2, 3... without gaps (DHCP 20110 for one past the end)."""
    c.add_scope(scope())
    crit = {"mac_address": ["EQ", "aa-bb-*"]}

    def orders(scope_id: str = "10.1.20.0") -> list[tuple[str, int | None]]:
        return [(p.name, p.processing_order) for p in c.list_policies(scope_id)]

    with pytest.raises(PSError) as err:
        c.add_policy(Policy(name="b", scope_id="10.1.20.0", criteria=crit, processing_order=2))
    assert err.value.id.startswith("DHCP 20110") and err.value.category == "InvalidData"
    assert orders() == []
    c.add_policy(Policy(name="b", scope_id="10.1.20.0", criteria=crit, processing_order=1))
    c.add_policy(Policy(name="c", scope_id="10.1.20.0", criteria=crit, processing_order=2))
    # Inserting at a used number moves the others down the list.
    c.add_policy(Policy(name="a", scope_id="10.1.20.0", criteria=crit, processing_order=1))
    assert orders() == [("a", 1), ("b", 2), ("c", 3)]
    # No number: the end of the list.
    c.add_policy(Policy(name="d", scope_id="10.1.20.0", criteria=crit))
    assert orders()[-1] == ("d", 4)
    # Each level has its own list.
    c.add_policy(Policy(name="srv", criteria=crit, processing_order=1))
    assert orders("") == [("srv", 1)]
    # Moving one renumbers those in between.
    c.set_policy(Policy(name="d", scope_id="10.1.20.0", processing_order=2))
    assert orders() == [("a", 1), ("d", 2), ("b", 3), ("c", 4)]
    c.set_policy(Policy(name="d", scope_id="10.1.20.0", processing_order=4))
    assert orders() == [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    with pytest.raises(PSError) as err:
        c.set_policy(Policy(name="d", scope_id="10.1.20.0", processing_order=5))
    assert err.value.id.startswith("DHCP 20110")
    # Removing one closes the gap.
    c.remove_policy("b", "10.1.20.0")
    assert orders() == [("a", 1), ("c", 2), ("d", 3)]


def test_v6(c: DhcpClient) -> None:
    s = c.add_v6_scope(ScopeV6(prefix="2001:DB8:1::", name="v6", state="inactive", valid_lifetime="1.00:00:00"))
    assert s.prefix == "2001:db8:1::" and s.state == "Inactive" and s.valid_lifetime == "1.00:00:00"
    with pytest.raises(PSError):
        c.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="dup"))
    assert c.set_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6b", preference=5)).preference == 5
    r = c.add_v6_reservation(
        ReservationV6(prefix="2001:db8:1::", ip_address="2001:db8:1::10", client_duid="00:01:00:01:AB:CD", iaid=1)
    )
    assert r.client_duid == "00-01-00-01-ab-cd" and r.name == "2001:db8:1::10"
    with pytest.raises(PSError, match="not in the scope"):
        c.add_v6_reservation(
            ReservationV6(prefix="2001:db8:1::", ip_address="2001:db8:2::10", client_duid="01", iaid=1)
        )
    assert c.set_v6_reservation(ReservationV6("2001:db8:1::", "2001:db8:1::10", "0102", 2, "h")).iaid == 2
    assert len(c.list_v6_reservations("2001:db8:1::")) == 1
    k = OptionKeyV6(23, prefix="2001:db8:1::")
    assert c.set_v6_option_value(k, ["2001:db8::53"]).value == ["2001:db8::53"]
    assert len(c.list_v6_option_values(prefix="2001:db8:1::")) == 1
    c.remove_v6_reservation("2001:db8:1::10")
    with pytest.raises(NotFound):
        c.get_v6_reservation("2001:db8:1::", "2001:db8:1::10")
    c.remove_v6_scope("2001:db8:1::")
    assert c.list_v6_scopes() == [] and c.list_v6_option_values() == []
    with pytest.raises(NotFound):
        c.get_v6_option_value(k)


def test_fail_injection_and_calls(fake: FakeDhcpServer) -> None:
    c = DhcpClient(fake, computer_name="dhcp02.example.local")
    fake.fail = lambda op, p: (
        PSError(message="Access is denied.", category="PermissionDenied", id="WIN32 5", command="Get-DhcpServerv4Scope")
        if op == "scope.list"
        else None
    )
    with pytest.raises(PSError, match="Access is denied") as ei:
        c.list_scopes()
    assert not ei.value.not_found
    c.add_scope(scope())
    assert fake.ops() == ["scope.list", "scope.add"]
    assert all(call.params["computer_name"] == "dhcp02.example.local" for call in fake.calls)


def test_concurrent_calls_through_limiter(fake: FakeDhcpServer) -> None:
    c = DhcpClient(limit(fake, 3))
    errors: list[Exception] = []

    def work(i: int) -> None:
        try:
            c.add_scope(scope(f"10.2.{i}.10", f"10.2.{i}.20", name=f"s{i}"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(c.list_scopes()) == 20


def test_client_close_delegates_to_runner() -> None:
    closed = []

    class R:
        def run(self, script, params=None):  # pragma: no cover - not called
            raise AssertionError

        def close(self) -> None:
            closed.append(True)

    DhcpClient(R()).close()
    DhcpClient(object()).close()  # runners without close() are fine
    assert closed == [True]


def test_scope_set_without_lease_keeps_it():
    from netbox_windows_dhcp_plugin.windows.client import Scope

    c = DhcpClient(FakeDhcpServer())
    s = c.add_scope(
        Scope(
            name="a",
            start_range="10.5.0.10",
            end_range="10.5.0.20",
            subnet_mask="255.255.255.0",
            lease_duration="2.00:00:00",
        )
    )
    s.lease_duration = ""
    s.name = "b"
    got = c.set_scope(s)
    assert (got.name, got.lease_duration) == ("b", "2.00:00:00")


def test_v6_classes(c: DhcpClient) -> None:
    c.add_v6_class(DhcpClass(name="Lab", type="User", data="LabComputers", description="x"))
    with pytest.raises(PSError):
        c.add_v6_class(DhcpClass(name="lab", type="User", data="dup"))
    assert c.set_v6_class(DhcpClass(name="lab", type="User", data="Lab2")).data == "Lab2"
    assert [x.name for x in c.list_v6_classes()] == ["Lab"]
    # The v4 and v6 class lists are separate on Windows.
    assert c.list_classes() == []
    c.remove_v6_class("LAB", "User")
    assert c.list_v6_classes() == []
    with pytest.raises(NotFound):
        c.remove_v6_class("lab", "User")


def test_v6_option_list_all_returns_every_class(c: DhcpClient) -> None:
    c.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6"))
    c.add_v6_class(DhcpClass(name="lab", type="User", data="Lab"))
    c.set_v6_option_value(OptionKeyV6(23, prefix="2001:db8:1::"), ["2001:db8::53"])
    c.set_v6_option_value(OptionKeyV6(23, prefix="2001:db8:1::", user_class="lab"), ["2001:db8::54"])
    assert len(c.list_v6_option_values(prefix="2001:db8:1::")) == 1
    got = c.list_v6_option_values(prefix="2001:db8:1::", all=True)
    assert sorted(o.user_class for o in got) == ["", "lab"]
    # Server level values are not scope values.
    assert c.list_v6_option_values(all=True) == []


def test_v6_exclusions_and_leases(c: DhcpClient, fake: FakeDhcpServer) -> None:
    c.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6"))
    fake.add_v6_exclusion("2001:db8:1::", "2001:db8:1::1", "2001:db8:1::ff")
    fake.add_v6_lease("2001:db8:1::", "2001:db8:1::1:10", iaid=5)
    (e,) = c.list_v6_exclusion_ranges("2001:db8:1::")
    assert (e.start_range, e.end_range) == ("2001:db8:1::1", "2001:db8:1::ff")
    (lease,) = c.list_v6_leases("2001:db8:1::")
    assert lease.ip_address == "2001:db8:1::1:10" and lease.iaid == 5
    with pytest.raises(PSError, match="used by a client"):
        c.remove_v6_scope("2001:db8:1::")
    with pytest.raises(NotFound):
        c.list_v6_leases("2001:db8:2::")


# ---- issue #15: the shape a Windows PowerShell 5.1 server printed for a missing object ----

MISSING = {
    "scope": lambda c: c.get_scope("10.1.99.0"),
    "reservation": lambda c: c.get_reservation("10.1.20.0", "10.1.20.77"),
    "exclusion": lambda c: c.get_exclusion_range(ExclusionRange("10.1.20.0", "10.1.20.1", "10.1.20.2")),
    "option": lambda c: c.get_option_value(OptionKey(15, scope_id="10.1.20.0")),
    "reservation option": lambda c: c.get_option_value(OptionKey(15, "10.1.20.0", reserved_ip="10.1.20.5")),
    "option definition": lambda c: c.get_option_definition(252),
    "class": lambda c: c.get_class("nope", "User"),
    "failover": lambda c: c.get_failover("nope"),
    "superscope": lambda c: c.get_superscope("nope"),
    "filter": lambda c: c.get_filter("aa-bb-cc-dd-ee-01"),
    "policy": lambda c: c.get_policy("nope", "10.1.20.0"),
    "v6 scope": lambda c: c.get_v6_scope("2001:db8:9::"),
    "v6 reservation": lambda c: c.get_v6_reservation("2001:db8:1::", "2001:db8:1::99"),
    "v6 option": lambda c: c.get_v6_option_value(OptionKeyV6(23, prefix="2001:db8:1::")),
}


@pytest.mark.parametrize("empty_as_object", [False, True], ids=["null", "empty-object"])
@pytest.mark.parametrize("getter", MISSING.values(), ids=MISSING.keys())
def test_missing_objects_raise_not_found(fake: FakeDhcpServer, getter, empty_as_object: bool) -> None:
    c = DhcpClient(fake)
    c.add_scope(scope())
    c.add_reservation(Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="aa-bb-cc-dd-ee-ff"))
    c.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6"))
    fake.empty_result_as_object = empty_as_object
    with pytest.raises(NotFound):
        getter(c)


def test_empty_result_as_object_shape(fake: FakeDhcpServer) -> None:
    fake.empty_result_as_object = True
    raw = fake.run("# netbox-windows-dhcp-plugin:filter.get\n", {"mac_address": "aa-bb-cc-dd-ee-01"})
    assert raw == b'{"ok": true, "data": {}}'


# ---- issues #20 and #17: options on a reservation ----


def test_reservation_options_round_trip(c: DhcpClient, fake: FakeDhcpServer) -> None:
    c.add_scope(scope())
    c.add_reservation(Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="aa-bb-cc-dd-ee-ff"))
    k = OptionKey(15, scope_id="10.1.20.0", reserved_ip="10.1.20.5")
    v = c.set_option_value(k, ["host.example"])
    assert (v.value, v.scope_id, v.reserved_ip) == (["host.example"], "10.1.20.0", "10.1.20.5")
    assert c.get_option_value(k).value == ["host.example"]
    (listed,) = c.list_option_values(scope_id="10.1.20.0", reserved_ip="10.1.20.5", all=True)
    assert (listed.scope_id, listed.reserved_ip) == ("10.1.20.0", "10.1.20.5")
    # Not a scope value.
    assert c.list_option_values(scope_id="10.1.20.0", all=True) == []
    c.remove_option_value(k)
    with pytest.raises(NotFound):
        c.get_option_value(k)
    # Windows refuses -ScopeId with -ReservedIP on Set and Remove; so does the fake.
    for op in ("option.set", "option.remove"):
        raw = fake.run(
            f"# netbox-windows-dhcp-plugin:{op}\n",
            {"option_id": 15, "scope_id": "10.1.20.0", "reserved_ip": "10.1.20.5", "value": ["x"]},
        )
        assert b"ScopeId and ReservedIP cannot be specified together" in raw and b"InvalidArgument" in raw


def test_v6_reservation_options_round_trip(c: DhcpClient, fake: FakeDhcpServer) -> None:
    c.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6"))
    c.add_v6_reservation(ReservationV6("2001:db8:1::", "2001:db8:1::10", "00-01", 1))
    k = OptionKeyV6(24, prefix="2001:db8:1::", reserved_ip="2001:db8:1::10")
    v = c.set_v6_option_value(k, ["host.example"])
    assert (v.value, v.prefix, v.reserved_ip) == (["host.example"], "2001:db8:1::", "2001:db8:1::10")
    assert c.get_v6_option_value(k).value == ["host.example"]
    (listed,) = c.list_v6_option_values(prefix="2001:db8:1::", reserved_ip="2001:db8:1::10", all=True)
    assert (listed.prefix, listed.reserved_ip) == ("2001:db8:1::", "2001:db8:1::10")
    assert c.list_v6_option_values(prefix="2001:db8:1::", all=True) == []
    for op in ("v6option.set", "v6option.remove"):
        raw = fake.run(
            f"# netbox-windows-dhcp-plugin:{op}\n",
            {"option_id": 24, "prefix": "2001:db8:1::", "reserved_ip": "2001:db8:1::10", "value": ["x"]},
        )
        assert b"Prefix and ReservedIP cannot be specified together" in raw
    # Removing the scope takes the reservation and its options with it.
    c.remove_v6_scope("2001:db8:1::")
    assert fake.v6_options == {}


# ---- issue #18: Windows only accepts a wildcard at the start or the end of a policy condition ----


@pytest.mark.parametrize("value", ["*.example.test", "host*", "02-4e-42-ab*"])
def test_policy_wildcard_at_either_end_is_accepted(c: DhcpClient, value: str) -> None:
    c.add_scope(scope())
    c.add_policy(Policy(name="p", scope_id="10.1.20.0", criteria={"fqdn": ["EQ", value]}))


def test_policy_embedded_wildcard_is_refused(c: DhcpClient) -> None:
    c.add_scope(scope())
    bad = {"fqdn": ["EQ", "Host*.Example.Test"]}
    with pytest.raises(PSError, match="Wildcard is valid either at the beginning or at the end") as ei:
        c.add_policy(Policy(name="p", scope_id="10.1.20.0", criteria=bad))
    assert ei.value.category == "InvalidArgument"
    c.add_policy(Policy(name="p", scope_id="10.1.20.0", criteria={"fqdn": ["EQ", "*.example.test"]}))
    with pytest.raises(PSError, match="Wildcard"):
        c.set_policy(Policy(name="p", scope_id="10.1.20.0", criteria=bad))
    assert c.get_policy("p", "10.1.20.0").criteria == {"fqdn": ["EQ", "*.example.test"]}


# ---- issue #22: DHCPv6 leases have no state ----


def test_v6_leases_have_no_state(c: DhcpClient, fake: FakeDhcpServer) -> None:
    c.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6"))
    fake.add_v6_lease("2001:db8:1::", "2001:db8:1::1:10")
    (lease,) = c.list_v6_leases("2001:db8:1::")
    assert "address_state" not in lease.to_dict()
    assert "address_state" not in fake.v6_leases["2001:db8:1::"][0]
