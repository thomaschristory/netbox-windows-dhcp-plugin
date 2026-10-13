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
from collections.abc import Sequence
from datetime import timedelta

__all__ = [
    "canonical_duid",
    "canonical_duration",
    "canonical_ipv6",
    "canonical_mac",
    "enum_of",
    "format_duration",
    "is_mac",
    "network_address",
    "option_values_equivalent",
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


def canonical_duration(value: str | int | timedelta) -> str:
    return format_duration(parse_duration(value))


_MAC_SEPARATORS = str.maketrans("", "", ":-. ")


def canonical_mac(value: str) -> str:
    """Canonicalise a MAC address or DHCP client identifier to ``aa-bb-cc-...``.

    Accepts ``:``, ``-``, ``.`` and space separators or bare hex, any even length
    (client identifiers can be longer than six bytes).
    """
    clean = str(value).strip().translate(_MAC_SEPARATORS)
    if not clean:
        raise ValueError(f"invalid client ID {value!r}: empty")
    if len(clean) % 2:
        raise ValueError(f"invalid client ID {value!r}: odd number of hex digits")
    try:
        raw = bytes.fromhex(clean)
    except ValueError:
        raise ValueError(f"invalid client ID {value!r}: not hexadecimal") from None
    return "-".join(f"{b:02x}" for b in raw)


def is_mac(value: str) -> bool:
    try:
        canonical_mac(value)
    except ValueError:
        return False
    return True


# DHCPv6 DUIDs are written the same way as client identifiers.
canonical_duid = canonical_mac


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


def option_values_equivalent(a: Sequence[str], b: Sequence[str]) -> bool:
    """Compare option value lists element by element.

    Elements are equal when they match ignoring case, or both parse as the same
    unsigned number in decimal or hex (``"0x0A" == "10"``). Order matters.
    """
    if len(a) != len(b):
        return False
    for x, y in zip(a, b, strict=True):
        if str(x).casefold() == str(y).casefold():
            continue
        try:
            if parse_option_number(x) != parse_option_number(y):
                return False
        except ValueError:
            return False
    return True
