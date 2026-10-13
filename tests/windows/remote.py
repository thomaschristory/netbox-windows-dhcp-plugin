"""Test helper: play the Windows host's part of the bootstrap protocol with the fake server."""

from __future__ import annotations

import base64
import json

from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

_P_PREFIX = "$p = ConvertFrom-Json -InputObject '"
_QUOTES = ("'", "‘", "’", "‚", "‛")
_PREAMBLE_LINES = 6


def split_rendered(rendered: str) -> tuple[dict, str]:
    """Undo psscript.render: return (params, wrapped script)."""
    lines = rendered.split("\n")
    p_line = lines[3]
    assert p_line.startswith(_P_PREFIX) and p_line.endswith("'"), p_line
    literal = p_line[len(_P_PREFIX) : -1]
    for q in _QUOTES:
        literal = literal.replace(q + q, q)
    return json.loads(literal), "\n".join(lines[_PREAMBLE_LINES:])


def fake_host_response(server: FakeDhcpServer, stdin: str) -> tuple[bytes, bytes, int]:
    """What powershell.exe running the bootstrap would print, with ``server`` as the DHCP role."""
    rendered = base64.b64decode(stdin.strip()).decode("utf-8")
    params, wrapped = split_rendered(rendered)
    envelope = server.run(wrapped, params)
    return base64.b64encode(envelope), b"", 0
