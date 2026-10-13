"""Build the PowerShell text sent to the Windows host and parse the JSON answer.

Port of ``internal/psscript/psscript.go`` from terraform-provider-windowsddi.

The script contract (docs/DESIGN.md section 5):

* every script runs inside ``try``/``catch`` and prints exactly one JSON envelope,
  ``{"ok": true, "data": ...}`` or ``{"ok": false, "error": {...}}``;
* user values only enter the script as a single-quoted JSON literal assigned to ``$p``,
  with every single-quote character (ASCII and typographic) doubled. Nothing is ever
  interpolated into PowerShell source;
* ``$PSDefaultParameterValues['*:ErrorAction'] = 'Stop'`` is set because the CDXML
  DhcpServer cmdlets ignore ``$ErrorActionPreference``; without it their errors are
  non-terminating and a failed call looks like a success;
* the rendered script travels base64-encoded on stdin to a fixed bootstrap passed with
  ``powershell.exe -EncodedCommand``. That avoids the cmd.exe 8191-character limit and
  console code page mangling.

Layering: ``client`` owns the cmdlet bodies and asks this module to wrap them; a
``runner`` renders, encodes and transports them; ``client`` parses the envelope.

This module must not import Django or NetBox.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BOOTSTRAP",
    "NOT_FOUND_IDS",
    "OP_PREFIX",
    "NotFound",
    "OutputError",
    "PSError",
    "PSNotFoundError",
    "Requirement",
    "Script",
    "WindowsLayerError",
    "command",
    "command_argv",
    "decode_output",
    "is_not_found",
    "op_of",
    "parse",
    "render",
]

# First line of every wrapped script. Fakes dispatch on it (see fake.py).
OP_PREFIX = "# netbox-windows-dhcp-plugin:"

# FullyQualifiedErrorId prefixes that mean "the object does not exist".
# Matched as "<id>," or the exact id, so "DHCP 200050" does not match "DHCP 20005".
# DHCP 20007 (scope in use, cannot be removed) is deliberately NOT here: it is a real error.
NOT_FOUND_IDS: tuple[str, ...] = (
    "DHCP 20005",  # ERROR_DHCP_SUBNET_NOT_PRESENT
    "DHCP 20010",  # ERROR_DHCP_OPTION_NOT_PRESENT
    "DHCP 20018",  # ERROR_DHCP_NOT_RESERVED_CLIENT
)

# Characters that end a PowerShell single-quoted string. Inside such a string any of
# them is escaped by doubling it. PowerShell treats the typographic quotes exactly like
# the ASCII apostrophe, so they must be doubled too or they would close the literal.
_SINGLE_QUOTES = ("'", "‘", "’", "‚", "‛")


class WindowsLayerError(Exception):
    """Base class of every error raised by the Windows layer."""


class OutputError(WindowsLayerError, ValueError):
    """The host's output was not what the script contract promises."""


class NotFound(WindowsLayerError):  # noqa: N818 - mirrors the Go ErrNotFound sentinel
    """The requested object does not exist on the DHCP server.

    Raised by ``parse`` (as ``PSNotFoundError``) for not-found script errors, and by
    the client when a getter's script succeeds with ``$out = $null``.
    """


@dataclass(frozen=True)
class Requirement:
    """A PowerShell module a script needs, checked before the body runs."""

    module: str
    probe: str  # a command that exists when the module is available
    hint: str  # how to install it


@dataclass(frozen=True)
class Script:
    """One operation's PowerShell body.

    The body reads parameters from ``$p``, splats ``@cn`` (``-ComputerName`` when set)
    on every DhcpServer cmdlet, and assigns its result to ``$out``.
    """

    op: str
    body: str
    requires: Requirement | None = None

    def with_requirement(self, req: Requirement | None) -> Script:
        return Script(op=self.op, body=self.body, requires=req)

    def wrap(self) -> str:
        """Return the body inside the op marker, module check and envelope.

        ``$null = . { body }`` dot-sources the body (so ``$out`` assigned inside is
        visible) and discards anything it writes to the pipeline, so only the one JSON
        document reaches stdout. The module check throws with a generic category, so a
        missing module is never mistaken for not-found.
        """
        parts = [OP_PREFIX + self.op + "\n", "try {\n    $out = $null\n"]
        if self.requires is not None:
            r = self.requires
            parts.append(
                "    if (-not (Get-Command -Name '"
                + _ps_quote_body(r.probe)
                + "' -ErrorAction SilentlyContinue)) { throw 'The "
                + _ps_quote_body(r.module)
                + " PowerShell module is not available on this host. "
                + _ps_quote_body(r.hint)
                + "' }\n"
            )
        parts.append("    $null = . {\n")
        parts.append(self.body)
        parts.append(
            "\n"
            "    }\n"
            "    ConvertTo-Json -InputObject @{ ok = $true; data = $out } -Depth 8 -Compress\n"
            "} catch {\n"
            "    $e = $_\n"
            "    ConvertTo-Json -InputObject @{ ok = $false; error = @{\n"
            '        message  = "$($e.Exception.Message)"\n'
            '        category = "$($e.CategoryInfo.Category)"\n'
            '        id       = "$($e.FullyQualifiedErrorId)"\n'
            '        command  = "$($e.InvocationInfo.MyCommand.Name)"\n'
            "    } } -Compress\n"
            "}\n"
        )
        return "".join(parts)


def _ps_quote_body(s: str) -> str:
    """Escape a constant for use inside a single-quoted PowerShell string."""
    for q in _SINGLE_QUOTES:
        s = s.replace(q, q + q)
    return s


def op_of(script: str) -> str:
    """Return the op name from a wrapped script's first line, or "" if it has none."""
    first = script.split("\n", 1)[0]
    if first.startswith(OP_PREFIX):
        return first[len(OP_PREFIX) :].strip()
    return ""


def _params_literal(params: Mapping[str, Any] | None) -> str:
    raw = json.dumps(dict(params or {}), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return _ps_quote_body(raw)


def render(script: str, params: Mapping[str, Any] | None = None) -> str:
    """Prepend the preamble that defines ``$p`` and ``$cn`` to a wrapped script.

    This is the injection boundary: ``params`` are JSON-encoded and only ever appear
    inside one single-quoted literal. Raises ``TypeError``/``ValueError`` when the
    params cannot be encoded as JSON.
    """
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$PSDefaultParameterValues['*:ErrorAction'] = 'Stop'\n"
        "$p = ConvertFrom-Json -InputObject '" + _params_literal(params) + "'\n"
        "$cn = @{}\n"
        "if ($p.computer_name) { $cn['ComputerName'] = $p.computer_name }\n" + script
    )


# Fixed bootstrap passed with -EncodedCommand. It reads the base64 UTF-8 script from
# stdin, runs it, joins its output with "`n" and writes it back base64 UTF-8 encoded,
# so the transport only ever carries ASCII.
BOOTSTRAP = (
    "$ProgressPreference = 'SilentlyContinue'\n"
    "$in = [Console]::In.ReadToEnd()\n"
    "$src = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($in.Trim()))\n"
    '$res = (& ([scriptblock]::Create($src))) -join "`n"\n'
    "[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($res)))\n"
)

POWERSHELL_EXE = "powershell.exe"
POWERSHELL_ARGS: tuple[str, ...] = ("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass")


def _encoded_bootstrap() -> str:
    return base64.b64encode(BOOTSTRAP.encode("utf-16-le")).decode("ascii")


def command_argv(rendered: str) -> tuple[list[str], str]:
    """Like ``command`` but return the argument vector (without the executable)."""
    args = [*POWERSHELL_ARGS, "-EncodedCommand", _encoded_bootstrap()]
    stdin = base64.b64encode(rendered.encode("utf-8")).decode("ascii") + "\r\n"
    return args, stdin


def command(rendered: str) -> tuple[str, str]:
    """Return the powershell.exe command line and the stdin payload for a rendered script."""
    args, stdin = command_argv(rendered)
    return " ".join([POWERSHELL_EXE, *args]), stdin


def _truncate(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n] + "..."


def decode_output(stdout: bytes | str) -> bytes:
    """Decode the bootstrap's base64 stdout. Anything else is banner or profile noise."""
    s = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else stdout
    s = s.strip()
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise OutputError(f"unexpected PowerShell output (not base64): {_truncate(s)!r}") from None


class PSError(WindowsLayerError):
    """A PowerShell error reported in the envelope (``ok = false``)."""

    def __init__(self, message: str = "", category: str = "", id: str = "", command: str = "") -> None:  # noqa: A002
        self.message = message
        self.category = category
        self.id = id
        self.command = command
        super().__init__(str(self))

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> PSError:
        """Build the right subclass: ``PSNotFoundError`` when the error means not-found."""
        kwargs = {k: str(d.get(k) or "") for k in ("message", "category", "id", "command")}
        if _is_not_found(kwargs["category"], kwargs["id"]):
            return PSNotFoundError(**kwargs)
        return PSError(**kwargs)

    def to_dict(self) -> dict[str, str]:
        return {"message": self.message, "category": self.category, "id": self.id, "command": self.command}

    @property
    def not_found(self) -> bool:
        return _is_not_found(self.category, self.id)

    def __str__(self) -> str:
        cmd = self.command or "PowerShell"
        msg = self.message.strip()
        if self.category and self.category != "NotSpecified":
            return f"{cmd}: {msg} ({self.category})"
        return f"{cmd}: {msg}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.to_dict()!r})"


class PSNotFoundError(PSError, NotFound):
    """A PowerShell error that means the object does not exist. Catch it as ``NotFound``."""


def _is_not_found(category: str, err_id: str) -> bool:
    if category == "ObjectNotFound":
        return True
    return any(err_id == i or err_id.startswith(i + ",") for i in NOT_FOUND_IDS)


def is_not_found(exc: BaseException | None) -> bool:
    """True when ``exc`` means the object does not exist."""
    return isinstance(exc, NotFound)


def parse(out: bytes | str) -> tuple[bool, Any]:
    """Parse a decoded envelope.

    Returns ``(present, data)``; ``present`` is False when ``data`` is null. Raises
    ``PSError`` (``PSNotFoundError`` for not-found) when the script failed, and
    ``OutputError`` when the output is not an envelope.
    """
    text = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out
    try:
        env = json.loads(text)
    except json.JSONDecodeError as e:
        raise OutputError(f"decoding PowerShell result: {e} (output: {_truncate(text)!r})") from None
    if not isinstance(env, dict):
        raise OutputError(f"decoding PowerShell result: not an envelope (output: {_truncate(text)!r})")
    if not env.get("ok"):
        err = env.get("error")
        if not isinstance(err, dict):
            raise OutputError("PowerShell reported failure without error details")
        raise PSError.from_dict(err)
    data = env.get("data")
    if data is None:
        return False, None
    return True, data
