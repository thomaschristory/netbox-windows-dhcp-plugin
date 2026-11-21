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


def test_parse_option_number() -> None:
    assert n.parse_option_number("0xff") == 255
    assert n.parse_option_number("42") == 42
    for bad in ["", "-1", "+1", "0x", "1e3", "²"]:
        with pytest.raises(ValueError):
            n.parse_option_number(bad)


@pytest.mark.parametrize(
    ("value", "want"),
    [
        # What Windows Server 2025 reads back for a MAC prefix condition (#21).
        ("02-4e-42-ab*", "02-4e-42-ab-*"),
        ("02-4e-42-ab-*", "02-4e-42-ab-*"),
        ("02-4E-42-AB*", "02-4e-42-ab-*"),
        ("024e42ab*", "02-4e-42-ab-*"),
        ("02-4e-42-ab-cd-ef", "02-4e-42-ab-cd-ef"),
        (" 02-4E-42-AB-CD-EF ", "02-4e-42-ab-cd-ef"),
        ("024e420a0b0c", "02-4e-42-0a-0b-0c"),
        # Windows joins the digits of a prefix and pads an odd count on the LEFT (lab, Windows Server 2025, #110).
        ("02-4e-42-a*", "00-24-e4-2a-*"),
        ("02-4-42-ab*", "00-24-42-ab-*"),
        ("2-4e-42-ab*", "02-4e-42-ab-*"),
        ("02-4e-4*", "00-24-e4-*"),
        # Refused by Windows (colons, a full address without 12 digits) or a wildcard elsewhere: left as written.
        ("02:4e:42:ab*", "02:4e:42:ab*"),
        ("02:4e:42:0a:0b:0c", "02:4e:42:0a:0b:0c"),
        ("2-4e-42-a-b-c", "2-4e-42-a-b-c"),
        ("02-*-42", "02-*-42"),
        ("*", "*"),
    ],
)
def test_canonical_mac_pattern(value: str, want: str) -> None:
    assert n.canonical_mac_pattern(value) == want


@pytest.mark.parametrize(
    ("value", "refused"),
    [
        # What Windows Server 2025 answers "not in valid format" to, and what it takes (#110).
        ("02:4e:42:ab*", True),
        ("02:4e:42:0a:0b:0c", True),
        ("2:4e:42:a:b:c", True),
        ("2-4e-42-a-b-c", True),
        ("02.4e.42.0a.0b.0c", True),
        ("zz-4e-42-0a-0b-0c", True),
        ("02-4e-42-0a-0b-0c", False),
        ("024e420a0b0c", False),
        ("02-4e-42-a*", False),
        ("02-4e-42-ab-*", False),
        ("024e42*", False),
        ("02-*-42", False),
    ],
)
def test_mac_pattern_refused(value: str, refused: bool) -> None:
    assert n.mac_pattern_refused(value) is refused


def test_canonical_policy_criterion() -> None:
    assert n.canonical_policy_criterion("mac_address", "02-4e-42-ab*") == "02-4e-42-ab-*"
    # Host names are not case sensitive (#21).
    assert n.canonical_policy_criterion("fqdn", "*.Example.Test") == "*.example.test"
    # Other conditions are compared exactly until a real server shows otherwise.
    for name in ("vendor_class", "user_class", "client_id", "relay_agent", "circuit_id", "remote_id", "subscriber_id"):
        assert n.canonical_policy_criterion(name, "AbC*") == "AbC*"


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("02-4e-42-ab-cd-ef", None),
        ("024e420a0b0c", None),
        ("02-4E-42-AB*", None),
        ("02-4e-42-ab-*", None),
        ("024e42*", None),
        ("*", None),
        ("*.example.test", None),  # a wildcard elsewhere is Windows' own rule (#18)
        (" 02-4e-42-ab* ", "spaces"),
        ("02:4e:42:ab*", "characters"),
        ("02.4e.42.0a.0b.0c", "characters"),
        ("zz-4e-42-*", "characters"),
        ("02-4-42-ab*", "groups"),
        ("2-4e-42-ab*", "groups"),
        ("02-4e-42-a*", "groups"),
        ("02--4e-42-ab*", "groups"),
        ("02-4e-42-0a-0b-0c-", "groups"),
        ("2-4e-42-a-b-c", "length"),
        ("024-e42-0a0b-0c", "groups"),
        ("-*", "groups"),
        ("------*", "groups"),
        ("02-4e-42-0a-0b", "length"),
        ("024e420a0b0c0d", "length"),
        ("024e4*", "half"),
    ],
)
def test_mac_condition_problem(value: str, problem: str | None) -> None:
    """#110: what NetBox refuses on save and the push names as not pushed."""
    assert n.mac_condition_problem(value) == problem
