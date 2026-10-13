from __future__ import annotations

import ast
import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from netbox_windows_dhcp_plugin.windows import psscript
from netbox_windows_dhcp_plugin.windows.psscript import (
    BOOTSTRAP,
    NotFound,
    OutputError,
    PSError,
    PSNotFoundError,
    Requirement,
    Script,
    command,
    command_argv,
    decode_output,
    is_not_found,
    op_of,
    parse,
    render,
)

from .conftest import fixture_bytes


def test_wrap_and_op() -> None:
    s = Script("scope.get", "$out = 1").wrap()
    assert s.startswith("# netbox-windows-dhcp-plugin:scope.get\n")
    assert op_of(s) == "scope.get"
    assert "$out = 1" in s
    assert "ok = $true" in s and "ok = $false" in s
    assert "$null = . {" in s
    assert "-Depth 8 -Compress" in s
    # The preamble comes first once rendered, so the marker is no longer on line 1.
    assert op_of(render(s, None)) == ""
    assert op_of("no marker") == ""


def test_wrap_module_check() -> None:
    req = Requirement(module="DhcpServer", probe="Get-DhcpServerv4Scope", hint="Install it, it's easy.")
    s = Script("x", "$out = 1", req).wrap()
    assert "Get-Command -Name 'Get-DhcpServerv4Scope' -ErrorAction SilentlyContinue" in s
    # Constants inside single quotes are escaped too.
    assert "Install it, it''s easy." in s
    assert "Get-Command" not in Script("x", "$out = 1").wrap()


def test_render_preamble() -> None:
    out = render("# body", {"scope_id": "10.1.2.0"})
    lines = out.split("\n")
    assert lines[0] == "$ErrorActionPreference = 'Stop'"
    assert lines[1] == "$ProgressPreference = 'SilentlyContinue'"
    assert lines[2] == "$PSDefaultParameterValues['*:ErrorAction'] = 'Stop'"
    assert lines[3] == """$p = ConvertFrom-Json -InputObject '{"scope_id":"10.1.2.0"}'"""
    assert lines[4] == "$cn = @{}"
    assert lines[5] == "if ($p.computer_name) { $cn['ComputerName'] = $p.computer_name }"
    assert out.endswith("# body")


def test_render_escapes_quotes() -> None:
    out = render("# body", {"name": "it's \u2018quoted\u2019"})
    assert """$p = ConvertFrom-Json -InputObject '{"name":"it''s \u2018\u2018quoted\u2019\u2019"}'""" in out


@pytest.mark.parametrize("q", ["'", "\u2018", "\u2019", "\u201a", "\u201b"])
def test_render_doubles_every_single_quote_kind(q: str) -> None:
    out = render("", {"v": f"a{q}b"})
    line = out.split("\n")[3]
    literal = line[len("$p = ConvertFrom-Json -InputObject '") : -1]
    assert f"a{q}{q}b" in literal
    # No undoubled quote character anywhere inside the literal.
    stripped = literal
    for c in ["'", "\u2018", "\u2019", "\u201a", "\u201b"]:
        stripped = stripped.replace(c + c, "")
        assert c not in stripped


def test_render_injection_attempt_stays_in_literal() -> None:
    evil = "'; Remove-Item C:\\ -Recurse; '"
    out = render("", {"name": evil})
    line = out.split("\n")[3]
    assert line.startswith("$p = ConvertFrom-Json -InputObject '")
    assert line.endswith("'")
    assert "''; Remove-Item" in line


def test_render_rejects_unencodable() -> None:
    with pytest.raises(TypeError):
        render("", {"bad": object()})
    with pytest.raises(ValueError):
        render("", {"nan": float("nan")})


def test_command_encoding() -> None:
    cmd, stdin = command("Write-Output 'h\u00e9llo'")
    prefix = "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
    assert cmd.startswith(prefix)
    assert len(cmd) < 8000  # cmd.exe limit is 8191
    enc = cmd[len(prefix) :]
    assert base64.b64decode(enc).decode("utf-16-le") == BOOTSTRAP
    assert stdin.endswith("\r\n")
    assert base64.b64decode(stdin.strip()).decode("utf-8") == "Write-Output 'h\u00e9llo'"
    args, stdin2 = command_argv("x")
    assert args[-2] == "-EncodedCommand" and args[-1] == enc
    assert stdin2 == base64.b64encode(b"x").decode() + "\r\n"


def test_bootstrap_matches_go() -> None:
    assert '-join "`n"' in BOOTSTRAP
    assert "[Console]::In.ReadToEnd()" in BOOTSTRAP


def test_decode_output() -> None:
    enc = base64.b64encode(b'{"ok":true}').decode()
    assert decode_output((enc + "\r\n").encode()) == b'{"ok":true}'
    with pytest.raises(OutputError, match="not base64"):
        decode_output(b"Windows PowerShell banner noise!")


def test_parse_ok_object() -> None:
    present, data = parse(fixture_bytes("ok_object.json"))
    assert present and data["scope_id"] == "10.1.2.0"


def test_parse_ok_null() -> None:
    assert parse(fixture_bytes("ok_null.json")) == (False, None)


def test_parse_not_found_by_category() -> None:
    with pytest.raises(NotFound) as ei:
        parse(fixture_bytes("err_not_found.json"))
    e = ei.value
    assert isinstance(e, PSError) and isinstance(e, PSNotFoundError) and e.not_found
    assert str(e) == (
        "Get-DhcpServerv4Scope: Failed to get information for scope 10.9.9.0 on DHCP server DHCP01. (ObjectNotFound)"
    )
    assert is_not_found(e)


def test_parse_not_found_by_dhcp_code() -> None:
    with pytest.raises(NotFound) as ei:
        parse(fixture_bytes("err_option_not_present.json"))
    # Category NotSpecified is left out of the message.
    assert str(ei.value) == "Get-DhcpServerv4OptionValue: Failed to get option value 6 on DHCP server DHCP01."


@pytest.mark.parametrize(
    ("err_id", "category", "want"),
    [
        ("DHCP 20005,Get-DhcpServerv4Scope", "NotSpecified", True),
        ("DHCP 20010,Get-DhcpServerv4OptionValue", "NotSpecified", True),
        ("DHCP 20018,Set-DhcpServerv4Reservation", "NotSpecified", True),
        ("DHCP 20005", "NotSpecified", True),
        ("DHCP 200050,Get-DhcpServerv4Scope", "NotSpecified", False),
        ("DHCP 20007,Remove-DhcpServerv4Scope", "NotSpecified", False),
        ("DHCP 20007,Remove-DhcpServerv4Scope", "PermissionDenied", False),
        ("DHCP 20052,Add-DhcpServerv4Scope", "InvalidArgument", False),
        ("Whatever", "ObjectNotFound", True),
        ("", "", False),
    ],
)
def test_not_found_rules(err_id: str, category: str, want: bool) -> None:
    raw = json.dumps({"ok": False, "error": {"message": "m", "category": category, "id": err_id, "command": "c"}})
    with pytest.raises(PSError) as ei:
        parse(raw)
    assert ei.value.not_found is want
    assert isinstance(ei.value, NotFound) is want


def test_dhcp_20007_is_a_real_error() -> None:
    with pytest.raises(PSError) as ei:
        parse(fixture_bytes("remove_with_leases.json"))
    assert not isinstance(ei.value, NotFound)
    assert "Remove-DhcpServerv4Scope" in str(ei.value)


def test_parse_other_error() -> None:
    with pytest.raises(PSError) as ei:
        parse(fixture_bytes("err_invalid.json"))
    e = ei.value
    assert not e.not_found and e.command == "Add-DhcpServerv4Scope" and "already exists" in str(e)
    assert str(e).endswith("(InvalidArgument)")


def test_parse_garbage_and_missing_details() -> None:
    with pytest.raises(OutputError):
        parse(b"WARNING: x")
    with pytest.raises(OutputError):
        parse(b'{"ok":false}')
    with pytest.raises(OutputError):
        parse(b"[1,2]")


def test_error_without_command() -> None:
    assert str(PSError(message=" boom ")) == "PowerShell: boom"


def test_layer_does_not_import_django_or_netbox() -> None:
    # Whatever the parent package imports (it guards its NetBox imports), importing the
    # Windows layer itself must not add Django, NetBox or the transport libraries.
    code = (
        "import sys\n"
        "import netbox_windows_dhcp_plugin\n"
        "before = set(sys.modules)\n"
        "import netbox_windows_dhcp_plugin.windows as w\n"
        "from netbox_windows_dhcp_plugin.windows import client, fake, normalize, psscript, runner, ssh, winrm\n"
        "new = set(sys.modules) - before\n"
        "bad = [m for m in new if m.split('.')[0] in ('django', 'netbox', 'pypsrp', 'paramiko')]\n"
        "print(','.join(sorted(bad)))\n"
    )
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=root)
    assert out.stdout.strip() == "", f"unexpected imports: {out.stdout}"


def test_layer_source_has_no_django_or_netbox_imports() -> None:
    pkg = Path(psscript.__file__).parent
    for path in pkg.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for n in names:
                assert n.split(".")[0] not in ("django", "netbox"), f"{path.name} imports {n}"


def test_public_names() -> None:
    for name in psscript.__all__:
        assert hasattr(psscript, name)
