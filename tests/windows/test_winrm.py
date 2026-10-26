"""WinRM transport with pypsrp replaced by in-memory fakes."""

from __future__ import annotations

import base64
import json
import sys
import types
from typing import Any

import pytest

from netbox_windows_dhcp_plugin.windows import psscript
from netbox_windows_dhcp_plugin.windows.client import DhcpClient, Scope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.runner import RunnerError
from netbox_windows_dhcp_plugin.windows.winrm import WinRMConfig, WinRMRunner, kerberos_user

from .remote import fake_host_response


class FakePypsrp:
    """Stands in for pypsrp.wsman.WSMan, pypsrp.shell.WinRS and pypsrp.shell.Process."""

    def __init__(self, respond) -> None:
        self.respond = respond  # (argv, stdin bytes) -> (stdout, stderr, rc)
        self.wsman_kwargs: list[dict[str, Any]] = []
        self.closed = 0
        self.shells_closed = 0
        self.signals: list[str] = []
        self.sent: list[tuple[bytes, bool]] = []
        fake = self

        class WSMan:
            def __init__(self, **kwargs: Any) -> None:
                fake.wsman_kwargs.append(kwargs)

            def __enter__(self) -> WSMan:
                return self

            def __exit__(self, *exc: object) -> None:
                fake.closed += 1

        class WinRS:
            def __init__(self, wsman: WSMan) -> None:
                self.wsman = wsman

            def __enter__(self) -> WinRS:
                return self

            def __exit__(self, *exc: object) -> None:
                fake.shells_closed += 1

        class Process:
            def __init__(self, shell: WinRS, executable: str, arguments: list[str] | None = None) -> None:
                self.argv = [executable, *(arguments or [])]
                self.stdin = b""
                self.stdout = b""
                self.stderr = b""
                self.rc: int | None = None

            def begin_invoke(self) -> None:
                pass

            def send(self, data: bytes, end: bool = True) -> None:
                fake.sent.append((data, end))
                self.stdin += data

            def end_invoke(self) -> None:
                self.stdout, self.stderr, self.rc = fake.respond(self.argv, self.stdin)

            def signal(self, code: str) -> None:
                fake.signals.append(code)

        class SignalCode:
            TERMINATE = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/signal/Terminate"

        self.wsman_mod = types.ModuleType("pypsrp.wsman")
        self.wsman_mod.WSMan = WSMan  # type: ignore[attr-defined]
        self.shell_mod = types.ModuleType("pypsrp.shell")
        self.shell_mod.WinRS = WinRS  # type: ignore[attr-defined]
        self.shell_mod.Process = Process  # type: ignore[attr-defined]
        self.shell_mod.SignalCode = SignalCode  # type: ignore[attr-defined]


@pytest.fixture
def pypsrp(monkeypatch: pytest.MonkeyPatch):
    def install(respond) -> FakePypsrp:
        f = FakePypsrp(respond)
        pkg = types.ModuleType("pypsrp")
        monkeypatch.setitem(sys.modules, "pypsrp", pkg)
        monkeypatch.setitem(sys.modules, "pypsrp.wsman", f.wsman_mod)
        monkeypatch.setitem(sys.modules, "pypsrp.shell", f.shell_mod)
        return f

    return install


def cfg(**kw: Any) -> WinRMConfig:
    base: dict[str, Any] = {"host": "dhcp01.example.local", "username": "EXAMPLE\\svc", "password": "pw"}
    base.update(kw)
    return WinRMConfig(**base)


def test_defaults_ntlm_https() -> None:
    k = WinRMRunner(cfg()).wsman_kwargs()
    assert k["server"] == "dhcp01.example.local" and k["port"] == 5986 and k["ssl"] is True
    assert k["auth"] == "ntlm" and k["cert_validation"] is True and k["encryption"] == "auto"
    assert k["username"] == "EXAMPLE\\svc" and k["operation_timeout"] == 60 and k["read_timeout"] > 60


def test_http_port_and_cert_toggle() -> None:
    k = WinRMRunner(cfg(https=False, cert_validation=False)).wsman_kwargs()
    assert k["port"] == 5985 and k["ssl"] is False and k["encryption"] == "auto"  # NTLM seals over HTTP
    assert k["cert_validation"] is False
    assert WinRMRunner(cfg(port=15986)).wsman_kwargs()["port"] == 15986


def test_basic_over_http_is_unencrypted() -> None:
    assert WinRMRunner(cfg(auth="basic", https=False)).wsman_kwargs()["encryption"] == "never"
    assert WinRMRunner(cfg(auth="BASIC")).wsman_kwargs()["encryption"] == "auto"


def test_kerberos() -> None:
    k = WinRMRunner(cfg(auth="kerberos", kerberos_realm="example.local")).wsman_kwargs()
    assert k["auth"] == "kerberos" and k["username"] == "svc@EXAMPLE.LOCAL"
    assert "negotiate_service" not in k
    k = WinRMRunner(
        cfg(auth="kerberos", kerberos_realm="EXAMPLE.LOCAL", kerberos_spn="HTTP/dhcp01.example.local:5986")
    ).wsman_kwargs()
    assert k["negotiate_service"] == "HTTP" and k["negotiate_hostname_override"] == "dhcp01.example.local"
    with pytest.raises(ValueError, match="kerberos_realm"):
        WinRMRunner(cfg(auth="kerberos"))
    # user@REALM is enough on its own
    assert WinRMRunner(cfg(auth="kerberos", username="svc@EXAMPLE.LOCAL")).wsman_kwargs()["username"] == "svc"


def test_credssp() -> None:
    """CredSSP (needed for failover writes, see winrm.py) is sent as pypsrp's own auth name, encrypted over HTTP."""
    k = WinRMRunner(cfg(auth="CredSSP", https=False)).wsman_kwargs()
    assert k["auth"] == "credssp" and k["encryption"] == "auto" and k["username"] == "EXAMPLE\\svc"


def test_credssp_without_requests_credssp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "requests_credssp", None)
    with pytest.raises(RunnerError, match="requests-credssp"):
        WinRMRunner(cfg(auth="credssp")).run("x")


def test_kerberos_user() -> None:
    assert kerberos_user("EXAMPLE\\svc") == "svc"
    assert kerberos_user("svc@example.local") == "svc"
    assert kerberos_user("svc", "example.local") == "svc@EXAMPLE.LOCAL"


@pytest.mark.parametrize(
    ("kw", "match"),
    [({"host": ""}, "required"), ({"password": ""}, "required"), ({"auth": "negotiate"}, "unsupported auth")],
)
def test_validation(kw: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        WinRMRunner(cfg(**kw))


def test_run_through_fake_host(pypsrp) -> None:
    server = FakeDhcpServer()
    seen: list[list[str]] = []

    def respond(argv: list[str], stdin: bytes) -> tuple[bytes, bytes, int]:
        seen.append(argv)
        return fake_host_response(server, stdin.decode())

    f = pypsrp(respond)
    c = DhcpClient(WinRMRunner(cfg()), computer_name="dhcp02")
    s = c.add_scope(Scope(name="Users", start_range="10.1.20.10", end_range="10.1.20.250", subnet_mask="255.255.255.0"))
    assert s.scope_id == "10.1.20.0"
    assert [x.scope_id for x in c.list_scopes()] == ["10.1.20.0"]
    assert seen[0][0] == "powershell.exe" and seen[0][-2] == "-EncodedCommand"
    assert len(f.wsman_kwargs) == 2 and f.closed == 2 and f.shells_closed == 2  # a new connection per call
    assert f.signals and all(end for _, end in f.sent[-1:])
    assert server.calls[0].params["computer_name"] == "dhcp02"


def test_large_stdin_is_chunked(pypsrp) -> None:
    f = pypsrp(lambda argv, stdin: (base64.b64encode(b'{"ok":true,"data":null}'), b"", 0))
    WinRMRunner(cfg()).run("# x", {"blob": "x" * 200_000})
    assert len(f.sent) > 1
    assert [end for _, end in f.sent] == [False] * (len(f.sent) - 1) + [True]
    assert all(len(d) <= 64 * 1024 for d, _ in f.sent)


def test_nonzero_exit_and_clixml(pypsrp) -> None:
    clixml = b'#< CLIXML\r\n<Objs Version="1.1.0.1"><S S="Error">The term is not recognized_x000D__x000A_</S></Objs>'
    pypsrp(lambda argv, stdin: (b"", clixml, 1))
    with pytest.raises(RunnerError, match="winrm: PowerShell exited with code 1: The term is not recognized"):
        WinRMRunner(cfg()).run("# x", {})


def test_transport_exception_is_runner_error(pypsrp) -> None:
    def boom(argv: list[str], stdin: bytes) -> tuple[bytes, bytes, int]:
        raise ConnectionError("401 Unauthorized")

    pypsrp(boom)
    with pytest.raises(RunnerError, match="winrm: running PowerShell: 401 Unauthorized"):
        WinRMRunner(cfg()).run("# x", {})


def test_envelope_error_passes_through(pypsrp) -> None:
    env = {"ok": False, "error": {"message": "nope", "category": "ObjectNotFound", "id": "x", "command": "Get-X"}}
    pypsrp(lambda argv, stdin: (base64.b64encode(json.dumps(env).encode()), b"", 0))
    raw = WinRMRunner(cfg()).run("# x", {})
    with pytest.raises(psscript.NotFound):
        psscript.parse(raw)
