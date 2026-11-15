"""Tests for the code quality audit fixes in the pure Windows layer (client, normalize, fake)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from netbox_windows_dhcp_plugin.windows import normalize as n
from netbox_windows_dhcp_plugin.windows.client import ALL_SCRIPTS, DhcpClient, MacFilter, Scope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import OutputError, PSError


class DataRunner:
    """Answers every script with one fixed ``data`` value."""

    def __init__(self, data: Any) -> None:
        self.data = data

    def run(self, script: str, params: Any = None) -> bytes:
        return json.dumps({"ok": True, "data": self.data}).encode()


# -- F72: helper functions are only sent with the scripts that call them ---------------------------------


@pytest.mark.parametrize("helper", ["ConvertTo-WdSeconds", "ConvertTo-WdTimeSpan"])
def test_a_script_that_calls_a_duration_helper_defines_it(helper: str) -> None:
    for s in ALL_SCRIPTS:
        defined = f"function {helper}(" in s.body
        called = len(re.findall(re.escape(helper), s.body)) > (1 if defined else 0)
        assert defined or not called, f"{s.op} calls {helper} without defining it"


def test_option_scripts_carry_no_duration_helpers() -> None:
    # The helpers travel together: a script that has them must use at least one of them.
    for s in ALL_SCRIPTS:
        if "function ConvertTo-WdSeconds(" in s.body:
            uses = len(re.findall("ConvertTo-WdSeconds|ConvertTo-WdTimeSpan", s.body))
            assert uses > 2, f"{s.op} carries the duration helpers without calling them"
    ops = {s.op for s in ALL_SCRIPTS if "ConvertTo-WdSeconds" in s.body}
    assert not {o for o in ops if o.startswith(("option.", "optiondef.", "v6option."))}
    assert {"failover.get", "policy.get", "v6scope.get"} <= ops


# -- F74: filter.set puts the old entry back when the add is refused ------------------------------------


def test_fake_filter_set_puts_the_old_entry_back_when_the_add_fails() -> None:
    fake = FakeDhcpServer()
    c = DhcpClient(fake)
    c.add_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Deny", "stolen"))
    fake.filter_add_fails = True
    with pytest.raises(PSError, match="Failed to add MAC address filter"):
        c.set_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Allow", "found"))
    assert c.get_filter("aa-bb-cc-dd-ee-ff") == MacFilter("aa-bb-cc-dd-ee-ff", "Deny", "stolen")
    fake.filter_add_fails = False
    assert c.set_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Allow", "found")).list == "Allow"


# -- F75: a new scope must match its range, mask and lease before anything is sent ----------------------


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"scope_id": "10.1.0.0", "start_range": "10.2.0.10", "end_range": "10.2.0.50"}, "not inside scope 10.1.0.0"),
        ({"scope_id": "10.1.0.0", "start_range": "10.1.0.10", "end_range": "10.1.1.50"}, "not inside scope 10.1.0.0"),
        ({"start_range": "10.1.0.10", "end_range": "10.1.1.50"}, "not inside scope 10.1.0.0"),
        ({"start_range": "10.1.0.50", "end_range": "10.1.0.10"}, "ends before it starts"),
        ({"start_range": "10.1.0.10", "end_range": "10.1.0.50", "lease_duration": ""}, "lease duration"),
    ],
)
def test_add_scope_refuses_inconsistent_values_before_any_call(kw: dict[str, str], match: str) -> None:
    fake = FakeDhcpServer()
    with pytest.raises(ValueError, match=match):
        DhcpClient(fake).add_scope(Scope(name="LAN", subnet_mask="255.255.255.0", **kw))
    assert fake.calls == [] and fake.scopes == {}


def test_add_scope_still_accepts_a_matching_scope_id() -> None:
    c = DhcpClient(FakeDhcpServer())
    s = c.add_scope(
        Scope(
            scope_id="10.1.0.0", name="LAN", start_range="10.1.0.10", end_range="10.1.0.50", subnet_mask="255.255.255.0"
        )
    )
    assert (s.scope_id, s.start_range, s.end_range) == ("10.1.0.0", "10.1.0.10", "10.1.0.50")


def test_set_scope_is_left_to_windows() -> None:
    # A Set needs no mask and Windows refuses a bad range itself, so the client check is on Add only.
    fake = FakeDhcpServer()
    c = DhcpClient(fake)
    c.add_scope(Scope(name="LAN", start_range="10.1.0.10", end_range="10.1.0.50", subnet_mask="255.255.255.0"))
    with pytest.raises(PSError, match="invalid"):
        c.set_scope(Scope(scope_id="10.1.0.0", name="LAN", start_range="10.2.0.10", end_range="10.2.0.50"))


# -- F76: malformed result shapes are an OutputError, not a default object or a bare AttributeError ------


@pytest.mark.parametrize(
    "data",
    [
        {"value": [{"scope_id": "10.1.0.0"}], "Count": 1},
        ["x"],
        "str",
        5,
        [None],
        [{"scope_id": "10.1.0.0"}, 3],
        True,
    ],
)
def test_list_refuses_malformed_shapes(data: Any) -> None:
    c = DhcpClient(DataRunner(data))
    with pytest.raises(OutputError, match="scope.list: expected a list of objects"):
        c.list_scopes()


def test_list_keeps_the_shapes_it_accepted_before() -> None:
    assert [s.scope_id for s in DhcpClient(DataRunner({"scope_id": "10.1.0.0"})).list_scopes()] == ["10.1.0.0"]
    assert DhcpClient(DataRunner([{}, {"scope_id": "10.1.0.0"}])).list_scopes()[0].scope_id == "10.1.0.0"
    assert DhcpClient(DataRunner([])).list_scopes() == []
    assert DhcpClient(DataRunner(None)).list_scopes() == []
    # An option value is a real object with a "value" field: only the exact value/Count pair is the array quirk.
    got = DhcpClient(DataRunner({"option_id": 6, "value": ["10.0.0.53"]})).list_option_values()
    assert [(o.option_id, o.value) for o in got] == [(6, ["10.0.0.53"])]


@pytest.mark.parametrize("data", [5, {"value": ["a"], "Count": 1}, True])
def test_server_names_refuses_malformed_shapes(data: Any) -> None:
    with pytest.raises(OutputError, match="server.names: expected a list of names"):
        DhcpClient(DataRunner(data)).get_server_names()


def test_server_names_keeps_the_shapes_it_accepted_before() -> None:
    assert DhcpClient(DataRunner("DHCP01.")).get_server_names() == ["dhcp01"]
    assert DhcpClient(DataRunner(["DHCP01", None, "dhcp01", "10.0.0.1"])).get_server_names() == ["dhcp01", "10.0.0.1"]


# -- F77: MAC addresses written without leading zeros ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "want"),
    [
        ("0:1b:21:a:b:c", "00-1b-21-0a-0b-0c"),
        ("1:2:3:4:5:6", "01-02-03-04-05-06"),
        ("a-b-c-d-e-f", "0a-0b-0c-0d-0e-0f"),
        ("0 1B 21 A B C", "00-1b-21-0a-0b-0c"),
        ("aabb.ccdd.eeff", "aa-bb-cc-dd-ee-ff"),
        ("aa-bb-cc-dd-ee-ff", "aa-bb-cc-dd-ee-ff"),
        ("aabbcc-ddeeff", "aa-bb-cc-dd-ee-ff"),
        ("aabbccddeeff", "aa-bb-cc-dd-ee-ff"),
        ("aa--bb::cc", "aa-bb-cc"),
    ],
)
def test_canonical_mac_pads_single_digit_groups(value: str, want: str) -> None:
    assert n.canonical_mac(value) == want
    assert n.canonical_duid(value) == want


@pytest.mark.parametrize("value", ["abc:de", "abc:def", "a:bcd", "aabb.c", "a", "abc"])
def test_canonical_mac_refuses_ambiguous_groups(value: str) -> None:
    with pytest.raises(ValueError):
        n.canonical_mac(value)
    with pytest.raises(ValueError):
        n.canonical_duid(value)


def test_reservation_client_id_is_padded_on_the_way_to_windows() -> None:
    from netbox_windows_dhcp_plugin.windows.client import Reservation

    fake = FakeDhcpServer()
    c = DhcpClient(fake)
    c.add_scope(Scope(name="LAN", start_range="10.1.0.10", end_range="10.1.0.50", subnet_mask="255.255.255.0"))
    r = c.add_reservation(Reservation(scope_id="10.1.0.0", ip_address="10.1.0.20", client_id="0:1b:21:a:b:c"))
    assert r.client_id == "00-1b-21-0a-0b-0c"


def test_mac_pattern_does_not_pad_half_a_byte() -> None:
    # In a wildcard pattern a single digit is half a byte, not a byte: these stay as written.
    assert n.canonical_mac_pattern("02-4e-42-a*") == "02-4e-42-a*"
    assert n.canonical_policy_criterion("mac_address", "02-4e-42-a*") == "02-4e-42-a*"
    assert n.canonical_mac_pattern("02-4e-42-ab*") == "02-4e-42-ab-*"
    assert n.canonical_mac_pattern("02:4E:42:AB:00:01") == "02-4e-42-ab-00-01"


# -- F78: the number rules the deleted option_values_equivalent test covered ----------------------------


def test_parse_option_number_hex_and_leading_zero() -> None:
    assert n.parse_option_number("0X0a") == n.parse_option_number("10") == n.parse_option_number("010") == 10
    for gone in ("canonical_duration", "is_mac", "option_values_equivalent"):
        assert not hasattr(n, gone) and gone not in n.__all__
