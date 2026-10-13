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
    assert pol.description == "HP" and pol.condition == "AND" and pol.criteria == {"mac_address": ["EQ", "3c-d9-2b*"]}
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
