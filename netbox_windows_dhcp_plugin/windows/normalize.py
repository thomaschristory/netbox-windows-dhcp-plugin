"""Canonical forms for values exchanged with the Windows DHCP server.

Port of ``internal/normalize/normalize.go`` (DHCP parts), ``normalize.EnumOf`` and the
option value comparison from ``internal/resources/dhcp/option_value.go``.

The server echoes values back in its own spelling (``Inactive`` for ``InActive``,
``10`` for ``0x0A``, lowercase-dash MACs, ``d.hh:mm:ss`` lease durations); comparing in
canonical form keeps the sync from seeing drift that is not there.

This module must not import Django or NetBox.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import timedelta

__all__ = [
    "canonical_duid",
    "canonical_ipv6",
    "canonical_mac",
    "canonical_mac_pattern",
    "mac_condition_problem",
    "mac_pattern_refused",
    "canonical_policy_criterion",
    "enum_of",
    "format_duration",
    "network_address",
    "parse_duration",
    "parse_ipv4",
    "parse_mask",
    "parse_option_number",
]

_TIMESPAN_RE = re.compile(r"^(?:(\d+)\.)?(\d{1,2}):(\d{2}):(\d{2})$")
_MAX_DAYS = 100_000


def parse_duration(value: str | int | timedelta, *, allow_zero: bool = False) -> int:
    """Parse a lease duration and return it in whole seconds.

    Accepts the .NET TimeSpan form ``d.hh:mm:ss`` (``8.00:00:00``), ``hh:mm:ss``
    (``08:00:00``), a number of seconds (``28800`` or ``"28800"``) or a ``timedelta``.
    The duration must be positive (or zero with ``allow_zero``) and a whole number of
    seconds.
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid duration {value!r}")
    if isinstance(value, timedelta):
        total = value.total_seconds()
        if total != int(total):
            raise ValueError(f"invalid duration {value!r}: must be a whole number of seconds")
        seconds = int(total)
    elif isinstance(value, int):
        seconds = value
    else:
        s = str(value).strip()
        m = _TIMESPAN_RE.match(s)
        if m:
            days = int(m.group(1) or 0)
            if days > _MAX_DAYS:
                raise ValueError(f"invalid duration {value!r}: too many days")
            h, mi, sec = int(m.group(2)), int(m.group(3)), int(m.group(4))
            if h > 23 or mi > 59 or sec > 59:
                raise ValueError(f"invalid duration {value!r}: hours must be < 24, minutes and seconds < 60")
            seconds = days * 86400 + h * 3600 + mi * 60 + sec
        elif s.isdigit():
            seconds = int(s)
        else:
            raise ValueError(
                f"invalid duration {value!r}: use d.hh:mm:ss (e.g. 8.00:00:00), hh:mm:ss or a number of seconds"
            )
    if seconds < 0 or (seconds == 0 and not allow_zero):
        raise ValueError(f"invalid duration {value!r}: must be positive")
    if seconds > _MAX_DAYS * 86400:
        raise ValueError(f"invalid duration {value!r}: too many days")
    return seconds


def format_duration(seconds: int | timedelta) -> str:
    """Format seconds as ``d.hh:mm:ss``; the day part is always present."""
    if isinstance(seconds, timedelta):
        seconds = int(seconds.total_seconds())
    days, rest = divmod(int(seconds), 86400)
    return f"{days}.{rest // 3600:02d}:{(rest % 3600) // 60:02d}:{rest % 60:02d}"


_MAC_SEPARATORS = str.maketrans("", "", ":-. ")
_MAC_SEPARATOR_RE = re.compile(r"[:\-. ]+")


def canonical_mac(value: str) -> str:
    """Canonicalise a MAC address or DHCP client identifier to ``aa-bb-cc-...``.

    Accepts ``:``, ``-``, ``.`` and space separators or bare hex, any even length
    (client identifiers can be longer than six bytes).

    Some systems print a MAC address without leading zeros (``0:1b:21:a:b:c``). When the
    value has separators and every group is one or two digits, each group is one byte and
    is padded to two digits. Groups of even width (``aabb.ccdd.eeff``) are read as written.
    Any other mix is refused, because joining the digits would give a different, shorter
    client ID without any warning.
    """
    text = str(value).strip()
    groups = [g for g in _MAC_SEPARATOR_RE.split(text) if g]
    if len(groups) > 1 and any(len(g) % 2 for g in groups):
        if any(len(g) > 2 for g in groups):
            raise ValueError(f"invalid client ID {value!r}: ambiguous groups, write every byte as two hex digits")
        text = "-".join(g.zfill(2) for g in groups)
    clean = text.translate(_MAC_SEPARATORS)
    if not clean:
        raise ValueError(f"invalid client ID {value!r}: empty")
    if len(clean) % 2:
        raise ValueError(f"invalid client ID {value!r}: odd number of hex digits")
    try:
        raw = bytes.fromhex(clean)
    except ValueError:
        raise ValueError(f"invalid client ID {value!r}: not hexadecimal") from None
    return "-".join(f"{b:02x}" for b in raw)


# DHCPv6 DUIDs are written the same way as client identifiers.
canonical_duid = canonical_mac


def canonical_mac_pattern(value: str) -> str:
    """Return a MAC address condition of a policy the way Windows stores it.

    Seen on Windows Server 2025 (#110): Windows ignores the dashes, joins the hex digits and
    lowercases them. A full address (12 digits) reads back as ``aa-bb-cc-dd-ee-ff``. A prefix
    with a trailing wildcard reads back as whole bytes, then ``-*``; an odd number of digits is
    padded with a zero on the LEFT, so ``02-4e-42-a*`` becomes ``00-24-e4-2a-*`` and
    ``02-4-42-ab*`` becomes ``00-24-42-ab-*`` (not what the writer meant: NetBox refuses such
    values on save). A value Windows refuses (see ``mac_pattern_refused``) or a wildcard
    anywhere but the end is returned as written.
    """
    s = str(value).strip()
    if mac_pattern_refused(s) or "*" in s[:-1]:
        return s
    wildcard = s.endswith("*")
    digits = (s[:-1] if wildcard else s).replace("-", "").lower()
    if not wildcard:
        return "-".join(digits[i : i + 2] for i in range(0, 12, 2))
    # unverified: a prefix of 12 digits or more before the wildcard (read here like a shorter one).
    if not digits:
        return s
    if len(digits) % 2:
        digits = "0" + digits
    return "-".join(digits[i : i + 2] for i in range(0, len(digits), 2)) + "-*"


_MAC_PATTERN_RE = re.compile(r"[0-9A-Fa-f-]*\*?")


def mac_pattern_refused(value: str) -> bool:
    """Whether Windows refuses this MAC address condition of a policy ("not in valid format").

    Seen on Windows Server 2025 (#110): colons are refused (``02:4e:42:ab*``), and a value
    without a wildcard needs all 12 digits (``2-4e-42-a-b-c`` is refused). A wildcard before
    the last character is not judged here: Windows refuses one in the middle (#18, the fake
    checks that) and how it takes a leading one on a MAC condition is not known.
    """
    s = str(value).strip()
    if "*" in s[:-1]:
        return False
    # unverified: dots, spaces and other characters (refused here, like colons).
    if not _MAC_PATTERN_RE.fullmatch(s):
        return True
    if not s.endswith("*"):
        return len(s.replace("-", "")) != 12
    return False


def mac_condition_problem(value: str) -> str | None:
    """Which rule a policy MAC condition breaks, so it would not reach Windows as meant, or None.

    Stricter than ``mac_pattern_refused``: it also catches what Windows takes but reads as other
    devices (#110). Windows ignores the dashes of a prefix and pads an odd number of digits on
    the left, so every byte must be two hex digits. One of ``spaces`` (around the value),
    ``characters`` (anything but hex digits and dashes), ``groups`` (a group that is not two
    digits), ``length`` (a full address without 12 digits) or ``half`` (half a byte before the
    wildcard). ``models/dhcp.py`` holds a copy (models must not import this module).
    """
    text = str(value)
    # unverified: how Windows takes spaces around the value (refused here: say what is meant).
    if text != text.strip():
        return "spaces"
    if "*" in text[:-1]:
        return None  # a wildcard elsewhere is Windows' own rule (#18)
    wildcard = text.endswith("*")
    body = text[:-1].removesuffix("-") if wildcard else text
    if not re.fullmatch(r"[0-9A-Fa-f-]*", body):
        return "characters"
    groups = body.split("-")
    digits = "".join(groups)
    if not wildcard and len(digits) != 12:
        return "length"
    # unverified: how Windows stores a full address of 12 digits grouped otherwise (024-e42-0a0b-0c): refused here.
    # A prefix whose first group alone has one digit is read as meant: Windows pads it on the left (2-4e-42-ab* is
    # stored as 02-4e-42-ab-*).
    lead = groups[1:] if wildcard and len(groups) > 1 and len(groups[0]) == 1 else groups
    if (len(groups) > 1 and any(len(g) != 2 for g in lead)) or (wildcard and text != "*" and not digits):
        return "groups"
    if wildcard and len(digits) % 2 and lead is groups:
        return "half"
    return None


def canonical_policy_criterion(criterion: str, value: str) -> str:
    """Return one value of a policy condition in the form Windows stores it, for comparison.

    ``criterion`` is a key of ``client.POLICY_CRITERIA``. A MAC address goes through
    ``canonical_mac_pattern``; a host name (``fqdn``) is lowercased, as Windows does
    (``*.Example.Test`` reads back ``*.example.test``) and as DNS names compare. The other
    conditions (classes, client ID, relay agent and its sub-options) are compared exactly:
    no real server has shown Windows rewriting them.
    """
    if criterion == "mac_address":
        return canonical_mac_pattern(value)
    if criterion == "fqdn":
        return str(value).lower()
    return value


def parse_ipv4(value: str) -> ipaddress.IPv4Address:
    """Parse a dotted IPv4 address (no leading zeros, no IPv6)."""
    try:
        return ipaddress.IPv4Address(str(value).strip())
    except ValueError:
        raise ValueError(f"invalid IPv4 address {value!r}") from None


def parse_mask(value: str) -> int:
    """Return the prefix length of a dotted, contiguous IPv4 subnet mask."""
    try:
        m = int(parse_ipv4(value))
    except ValueError:
        raise ValueError(f"invalid subnet mask {value!r}") from None
    ones = 0
    while m & 0x80000000:
        ones += 1
        m = (m << 1) & 0xFFFFFFFF
    if m:
        raise ValueError(f"invalid subnet mask {value!r}: not contiguous")
    return ones


def network_address(ip: str, mask: str) -> str:
    """Return the scope ID (network address) for an address and mask."""
    addr = parse_ipv4(ip)
    bits = parse_mask(mask)
    return str(ipaddress.IPv4Network(f"{addr}/{bits}", strict=False).network_address)


def canonical_ipv6(value: str) -> str:
    """Return the compressed lowercase form Windows prints for an IPv6 address."""
    try:
        return str(ipaddress.IPv6Address(str(value).strip()))
    except ValueError:
        raise ValueError(f"invalid IPv6 address {value!r}") from None


def enum_of(value: str, *allowed: str) -> str:
    """Return the allowed spelling matching ``value`` case-insensitively, else ``value``.

    The server reports a scope state as ``Inactive`` but ``Set-DhcpServerv4Scope -State``
    wants ``InActive``: ``enum_of("Inactive", "Active", "InActive") == "InActive"``.
    """
    folded = value.casefold()
    for a in allowed:
        if a.casefold() == folded:
            return a
    return value


SCOPE_STATES = ("Active", "InActive")
SCOPE_TYPES = ("Dhcp", "Bootp", "Both")


def parse_option_number(value: str) -> int:
    """Read a number as option values are written: ``0x``/``0X`` is hex, else decimal.

    A leading zero does not mean octal (``010`` is ten).
    """
    s = str(value).strip()
    if s.lower().startswith("0x"):
        digits = s[2:]
        if not digits or not all(c in "0123456789abcdefABCDEF" for c in digits):
            raise ValueError(f"not a number: {value!r}")
        return int(digits, 16)
    if not s.isascii() or not s.isdigit():
        raise ValueError(f"not a number: {value!r}")
    return int(s, 10)
