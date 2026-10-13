from __future__ import annotations

from datetime import timedelta

import pytest

from netbox_windows_dhcp_plugin.windows import normalize as n


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("8.00:00:00", 8 * 86400),
        ("0.08:00:00", 8 * 3600),
        ("08:00:00", 8 * 3600),
        ("1:30:00", 90 * 60),
        (" 3.12:00:00 ", 3 * 86400 + 12 * 3600),
        ("49710.06:28:15", 49710 * 86400 + 6 * 3600 + 28 * 60 + 15),
        ("28800", 28800),
        (28800, 28800),
        (timedelta(hours=8), 28800),
    ],
)
def test_parse_duration(value: object, seconds: int) -> None:
    assert n.parse_duration(value) == seconds  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    ["", "abc", "0", 0, -5, "-1", "8.24:00:00", "00:60:00", "00:00:60", "1:2:3", "999999999.00:00:00", "8d", "8h",
     "1.5", timedelta(milliseconds=1500), True],
)  # fmt: skip
def test_parse_duration_rejects(value: object) -> None:
    with pytest.raises(ValueError):
        n.parse_duration(value)  # type: ignore[arg-type]


def test_parse_duration_allow_zero() -> None:
    assert n.parse_duration("0.00:00:00", allow_zero=True) == 0
    assert n.parse_duration(0, allow_zero=True) == 0
    with pytest.raises(ValueError):
        n.parse_duration(-1, allow_zero=True)


def test_format_duration() -> None:
    assert n.format_duration(8 * 86400) == "8.00:00:00"
    assert n.format_duration(8 * 3600) == "0.08:00:00"
    assert n.format_duration(90 * 60 + 5) == "0.01:30:05"
    assert n.format_duration(timedelta(days=1)) == "1.00:00:00"
    assert n.canonical_duration("08:00:00") == "0.08:00:00"
    assert n.canonical_duration(691200) == "8.00:00:00"


@pytest.mark.parametrize(
    "value",
    ["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aabbccddeeff", "aabb.ccdd.eeff", " AABBCCDDEEFF ", "AA BB CC DD EE FF"],
)
def test_canonical_mac(value: str) -> None:
    assert n.canonical_mac(value) == "aa-bb-cc-dd-ee-ff"


def test_canonical_mac_client_identifier() -> None:
    assert n.canonical_mac("01-AA-BB-CC-DD-EE-FF") == "01-aa-bb-cc-dd-ee-ff"
    assert n.canonical_duid("00:01:00:01:2A:BC") == "00-01-00-01-2a-bc"


@pytest.mark.parametrize("value", ["", "abc", "zz:bb:cc:dd:ee:ff", "::", "aa-bb-cc-*"])
def test_canonical_mac_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        n.canonical_mac(value)
    assert not n.is_mac(value)


def test_ipv4_helpers() -> None:
    assert str(n.parse_ipv4("10.1.2.3")) == "10.1.2.3"
    for bad in ["", "10.1.2", "::1", "10.1.2.256", "10.1.2.03"]:
        with pytest.raises(ValueError):
            n.parse_ipv4(bad)
    for mask, bits in {
        "255.255.255.0": 24,
        "255.255.254.0": 23,
        "255.255.255.252": 30,
        "0.0.0.0": 0,
        "255.255.255.255": 32,
    }.items():
        assert n.parse_mask(mask) == bits
    for bad in ["255.0.255.0", "255.255.255.1", "foo"]:
        with pytest.raises(ValueError):
            n.parse_mask(bad)
    assert n.network_address("10.1.2.10", "255.255.254.0") == "10.1.2.0"
    assert n.network_address("10.1.3.10", "255.255.254.0") == "10.1.2.0"
    with pytest.raises(ValueError):
        n.network_address("10.1.3.10", "bad")


def test_canonical_ipv6() -> None:
    assert n.canonical_ipv6("2001:0DB8:0001:0000::") == "2001:db8:1::"
    with pytest.raises(ValueError):
        n.canonical_ipv6("10.0.0.1")


def test_enum_of() -> None:
    assert n.enum_of("Inactive", *n.SCOPE_STATES) == "InActive"
    assert n.enum_of("active", *n.SCOPE_STATES) == "Active"
    assert n.enum_of("both", *n.SCOPE_TYPES) == "Both"
    assert n.enum_of("weird", *n.SCOPE_STATES) == "weird"


@pytest.mark.parametrize(
    ("a", "b", "want"),
    [
        (["10"], ["0x0A"], True),
        (["0X0a"], ["10"], True),
        (["010"], ["10"], True),  # leading zero is not octal
        (["contoso.COM"], ["contoso.com"], True),
        (["10.0.0.53", "10.0.1.53"], ["10.0.0.53", "10.0.1.53"], True),
        (["10.0.0.53", "10.0.1.53"], ["10.0.1.53", "10.0.0.53"], False),  # order matters
        (["1"], ["1", "2"], False),
        (["0x"], ["0"], False),
        (["abc"], ["0xabc"], False),
        ([], [], True),
    ],
)
def test_option_values_equivalent(a: list[str], b: list[str], want: bool) -> None:
    assert n.option_values_equivalent(a, b) is want


def test_parse_option_number() -> None:
    assert n.parse_option_number("0xff") == 255
    assert n.parse_option_number("42") == 42
    for bad in ["", "-1", "+1", "0x", "1e3", "²"]:
        with pytest.raises(ValueError):
            n.parse_option_number(bad)
