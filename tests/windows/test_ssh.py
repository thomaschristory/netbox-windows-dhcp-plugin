"""SSH transport against an in-process paramiko SSH server (port of the Go runner tests).

The server plays a Windows OpenSSH host: it accepts the exec request, reads the base64
script from stdin and answers with the fake DHCP server's envelope.
"""

from __future__ import annotations

import base64
import io
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

paramiko = pytest.importorskip("paramiko")

from netbox_windows_dhcp_plugin.windows import runner as runner_mod  # noqa: E402
from netbox_windows_dhcp_plugin.windows import ssh as ssh_mod  # noqa: E402
from netbox_windows_dhcp_plugin.windows.client import DhcpClient, Scope  # noqa: E402
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer  # noqa: E402
from netbox_windows_dhcp_plugin.windows.runner import RunnerError  # noqa: E402
from netbox_windows_dhcp_plugin.windows.ssh import SSHConfig, SSHRunner, key_algorithms  # noqa: E402

from .remote import fake_host_response  # noqa: E402

USER, PASSWORD = "EXAMPLE\\svc", "Pa'ss w0rd"

_KEYS: dict[str, Any] = {}


def key(name: str):
    """Generate test keys once per session (RSA generation is slow-ish)."""
    if name not in _KEYS:
        _KEYS[name] = paramiko.RSAKey.generate(1024) if name.startswith("rsa") else paramiko.ECDSAKey.generate()
    return _KEYS[name]


def authorized_line(k) -> str:
    return f"{k.get_name()} {k.get_base64()} test"


def private_pem(k, password: str | None = None) -> str:
    buf = io.StringIO()
    k.write_private_key(buf, password=password)
    return buf.getvalue()


@dataclass
class Behaviour:
    auths: str = "publickey,password,keyboard-interactive"
    client_key: Any = None
    respond: Callable[[str, str], tuple[bytes, bytes, int]] | None = None
    # Sessions the server accepts at once on a connection (sshd MaxSessions); None: no limit.
    max_sessions: int | None = None


@dataclass
class SSHServer:
    port: int
    host_keys: list
    behaviour: Behaviour
    fake: FakeDhcpServer
    connections: int = 0
    commands: list[str] = field(default_factory=list)
    transports: list = field(default_factory=list)
    open_sessions: int = 0
    refused: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def drop_all(self) -> None:
        for t in self.transports:
            t.close()


class _Iface(paramiko.ServerInterface):
    def __init__(self, srv: SSHServer) -> None:
        self.srv = srv

    def get_allowed_auths(self, username: str) -> str:
        return self.srv.behaviour.auths

    def _allowed(self, m: str) -> bool:
        return m in self.srv.behaviour.auths.split(",")

    def check_auth_password(self, username: str, password: str) -> int:
        ok = self._allowed("password") and username == USER and password == PASSWORD
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, k) -> int:
        ck = self.srv.behaviour.client_key
        ok = self._allowed("publickey") and ck is not None and k.asbytes() == ck.asbytes()
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_interactive(self, username: str, submethods: str):
        if not self._allowed("keyboard-interactive"):
            return paramiko.AUTH_FAILED
        return paramiko.InteractiveQuery("", "", ("Password: ", False))

    def check_auth_interactive_response(self, responses) -> int:
        return paramiko.AUTH_SUCCESSFUL if list(responses) == [PASSWORD] else paramiko.AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind != "session":
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        with self.srv.lock:
            limit = self.srv.behaviour.max_sessions
            if limit is not None and self.srv.open_sessions >= limit:
                self.srv.refused += 1
                return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            self.srv.open_sessions += 1
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command: bytes) -> bool:
        cmd = command.decode()
        self.srv.commands.append(cmd)
        threading.Thread(target=self._serve, args=(channel, cmd), daemon=True).start()
        return True

    def _serve(self, channel, cmd: str) -> None:
        data = bytearray()
        while True:
            b = channel.recv(65536)
            if not b:
                break
            data.extend(b)
        respond = self.srv.behaviour.respond or (lambda c, s: fake_host_response(self.srv.fake, s))
        stdout, stderr, code = respond(cmd, data.decode())
        if stdout:
            channel.sendall(stdout)
        if stderr:
            channel.sendall_stderr(stderr)
        channel.send_exit_status(code)
        channel.close()
        with self.srv.lock:
            self.srv.open_sessions -= 1


@pytest.fixture
def ssh_server() -> Iterator[Callable[..., SSHServer]]:
    socks: list[socket.socket] = []
    servers: list[SSHServer] = []

    def start(host_keys: list | None = None, behaviour: Behaviour | None = None) -> SSHServer:
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(10)
        socks.append(lsock)
        srv = SSHServer(
            lsock.getsockname()[1], host_keys or [key("ecdsa-host")], behaviour or Behaviour(), FakeDhcpServer()
        )
        servers.append(srv)

        def accept() -> None:
            while True:
                try:
                    conn, _ = lsock.accept()
                except OSError:
                    return
                srv.connections += 1
                t = paramiko.Transport(conn)
                for hk in srv.host_keys:
                    t.add_server_key(hk)
                srv.transports.append(t)
                try:
                    t.start_server(server=_Iface(srv))
                except Exception:  # noqa: BLE001 - client gave up (e.g. host key mismatch)
                    t.close()

        threading.Thread(target=accept, daemon=True).start()
        return srv

    yield start
    for srv in servers:
        srv.drop_all()
    for s in socks:
        s.close()


def runner(srv: SSHServer, **kw: Any) -> SSHRunner:
    base: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": srv.port,
        "username": USER,
        "password": PASSWORD,
        "host_key": authorized_line(srv.host_keys[0]),
        "timeout": 5,
        "command_timeout": 10,
    }
    base.update(kw)
    return SSHRunner(SSHConfig(**base))


SCOPE = Scope(name="Users", start_range="10.1.20.10", end_range="10.1.20.250", subnet_mask="255.255.255.0")


def test_run_pinned_host_key_password(ssh_server) -> None:
    srv = ssh_server()
    r = runner(srv)
    try:
        c = DhcpClient(r)
        assert c.add_scope(SCOPE).scope_id == "10.1.20.0"
        assert [s.name for s in c.list_scopes()] == ["Users"]
    finally:
        r.close()
    assert srv.connections == 1  # one connection, one channel per script
    assert len(srv.commands) == 2
    assert srv.commands[0].startswith("powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ")


def test_host_key_mismatch(ssh_server) -> None:
    srv = ssh_server()
    r = runner(srv, host_key=authorized_line(key("ecdsa-other")))
    with pytest.raises(RunnerError, match="host key mismatch"):
        r.run("# x", {})


def test_pinned_rsa_key_restricts_algorithms(ssh_server) -> None:
    # The server also has an ECDSA key; pinning only its RSA key must still succeed.
    srv = ssh_server(host_keys=[key("rsa-host"), key("ecdsa-host")])
    r = runner(srv, host_key=authorized_line(key("rsa-host")))
    assert r.host_key_algorithms() == ["rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"]
    try:
        DhcpClient(r).list_scopes()
    finally:
        r.close()


def test_known_hosts(ssh_server, tmp_path: Path) -> None:
    srv = ssh_server()
    kh = tmp_path / "known_hosts"
    hk = key("ecdsa-host")
    kh.write_text(f"[127.0.0.1]:{srv.port} {hk.get_name()} {hk.get_base64()}\n")
    r = runner(srv, host_key="", known_hosts_file=str(kh))
    assert r.host_key_algorithms() == [hk.get_name()]
    try:
        DhcpClient(r).list_scopes()
    finally:
        r.close()
    kh.write_text(f"otherhost {hk.get_name()} {hk.get_base64()}\n")
    with pytest.raises(RunnerError, match="is not in"):
        runner(srv, host_key="", known_hosts_file=str(kh)).run("# x", {})
    other = key("ecdsa-other")
    kh.write_text(f"[127.0.0.1]:{srv.port} {other.get_name()} {other.get_base64()}\n")
    with pytest.raises(RunnerError, match="host key mismatch"):
        runner(srv, host_key="", known_hosts_file=str(kh)).run("# x", {})
    with pytest.raises(ValueError, match="known_hosts_file"):
        runner(srv, host_key="", known_hosts_file=str(tmp_path / "missing"))


def test_insecure_skips_verification(ssh_server) -> None:
    srv = ssh_server()
    r = runner(srv, host_key="", insecure=True, known_hosts_file="/nonexistent")
    try:
        DhcpClient(r).list_scopes()
    finally:
        r.close()


def test_keyboard_interactive_only(ssh_server) -> None:
    srv = ssh_server(behaviour=Behaviour(auths="keyboard-interactive"))
    r = runner(srv)
    try:
        DhcpClient(r).list_scopes()
    finally:
        r.close()


@pytest.mark.parametrize("passphrase", [None, PASSWORD])
def test_public_key(ssh_server, passphrase: str | None) -> None:
    ck = key("ecdsa-client")
    srv = ssh_server(behaviour=Behaviour(auths="publickey", client_key=ck))
    r = runner(srv, private_key=private_pem(ck, passphrase), password=passphrase or "")
    try:
        DhcpClient(r).list_scopes()
    finally:
        r.close()


def test_bad_password(ssh_server) -> None:
    srv = ssh_server(behaviour=Behaviour(auths="password,keyboard-interactive"))
    with pytest.raises(RunnerError, match="authentication failed"):
        runner(srv, password="wrong").run("# x", {})


def test_nonzero_exit_is_a_result(ssh_server) -> None:
    clixml = b'#< CLIXML\r\n<Objs><S S="Error">powershell.exe : boom &amp; bust_x000D__x000A_</S></Objs>'
    srv = ssh_server(behaviour=Behaviour(respond=lambda cmd, stdin: (b"", clixml, 3)))
    r = runner(srv)
    try:
        with pytest.raises(RunnerError, match="ssh: PowerShell exited with code 3: powershell.exe : boom & bust"):
            r.run("# x", {})
        # The connection survives a failed script.
        with pytest.raises(RunnerError, match="code 3"):
            r.run("# x", {})
    finally:
        r.close()
    assert srv.connections == 1


def test_reconnects_after_drop(ssh_server) -> None:
    srv = ssh_server()
    r = runner(srv)
    try:
        c = DhcpClient(r)
        c.list_scopes()
        srv.drop_all()
        c.list_scopes()
    finally:
        r.close()
    assert srv.connections == 2


def test_concurrent_scripts_share_connection(ssh_server) -> None:
    srv = ssh_server()
    r = runner(srv)
    errors: list[Exception] = []
    c = DhcpClient(r)

    def work(i: int) -> None:
        try:
            c.add_scope(
                Scope(name=f"s{i}", start_range=f"10.3.{i}.10", end_range=f"10.3.{i}.20", subnet_mask="255.255.255.0")
            )
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        r.close()
    assert errors == [] and srv.connections == 1 and len(srv.fake.scopes) == 6


def slow_fake(srv_ref: list, seconds: float) -> Callable[[str, str], tuple[bytes, bytes, int]]:
    def respond(cmd: str, stdin: str) -> tuple[bytes, bytes, int]:
        time.sleep(seconds)
        return fake_host_response(srv_ref[0].fake, stdin)

    return respond


def test_a_session_limit_waits_for_a_free_session_on_the_same_connection(ssh_server, monkeypatch) -> None:
    # sshd MaxSessions below the number of commands at once (max_concurrency): the refused ones wait for a running
    # one to finish, on the same connection, and every command succeeds.
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_DELAYS", (0.1,) * 30)
    ref: list = []
    srv = ssh_server(behaviour=Behaviour(max_sessions=2))
    ref.append(srv)
    srv.behaviour.respond = slow_fake(ref, 0.3)
    r = runner(srv)
    errors: list[Exception] = []
    c = DhcpClient(r)

    def work() -> None:
        try:
            c.list_scopes()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(6)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        r.close()
    assert errors == []
    assert srv.refused > 0
    assert srv.connections == 1


def test_a_session_still_refused_fails_that_command_only(ssh_server, monkeypatch) -> None:
    # The refusal must not drop the connection: the command already running on it goes on and succeeds.
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_DELAYS", (0.05, 0.05))
    ref: list = []
    srv = ssh_server(behaviour=Behaviour(max_sessions=1))
    ref.append(srv)
    srv.behaviour.respond = slow_fake(ref, 1.0)
    r = runner(srv)
    c = DhcpClient(r)
    first: list[Any] = []
    t = threading.Thread(target=lambda: first.append(c.list_scopes()))
    try:
        t.start()
        time.sleep(0.3)
        with pytest.raises(RunnerError, match="MaxSessions.*max_concurrency|max_concurrency.*MaxSessions"):
            c.list_scopes()
        t.join()
    finally:
        r.close()
    assert first == [[]]
    assert srv.connections == 1


class _FakeTransport:
    def __init__(self, active: bool, refuse: int, error: Exception | None = None) -> None:
        self.active, self.refuse, self.error = active, refuse, error
        self.closed = False
        self.opened = 0

    def is_active(self) -> bool:
        return self.active and not self.closed

    def open_session(self, timeout: float | None = None) -> str:
        if self.error is not None:
            raise self.error
        if self.refuse:
            self.refuse -= 1
            raise paramiko.ChannelException(1, "Administratively prohibited")
        self.opened += 1
        return "channel"

    def close(self) -> None:
        self.closed = True


def _with_transports(monkeypatch, *transports: _FakeTransport) -> SSHRunner:
    r = SSHRunner(SSHConfig(host="dhcp.example.test", username=USER, password=PASSWORD, insecure=True))
    queue = list(transports)

    def get():
        with r._lock:
            if r._transport is None or not r._transport.is_active():
                r._transport = queue.pop(0)
            return r._transport

    monkeypatch.setattr(r, "_get_transport", get)
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_DELAYS", (0.01, 0.01))
    return r


def test_a_refused_session_while_others_run_is_tried_again_on_the_same_connection(monkeypatch) -> None:
    live = _FakeTransport(active=True, refuse=2)
    r = _with_transports(monkeypatch, live)
    r._sessions = 1  # another command of ours is running on the connection
    assert r._open_channel() == "channel"
    assert not live.closed and live.opened == 1


def test_a_refusal_with_nothing_else_open_fails_at_once_with_the_reason_first(monkeypatch) -> None:
    # Nothing of ours will free a session (MaxSessions 0, or a server that refuses sessions): no waiting, and the
    # message gives the server's reason before naming the likely cause.
    live = _FakeTransport(active=True, refuse=99)
    r = _with_transports(monkeypatch, live)
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_DELAYS", (5.0,) * 3)
    started = time.monotonic()
    with pytest.raises(RunnerError) as err:
        r._open_channel()
    assert time.monotonic() - started < 1
    assert str(err.value).startswith("ssh: dhcp.example.test refused to open a session: ")
    assert "Administratively prohibited" in str(err.value) and "If it allows fewer sessions" in str(err.value)
    assert not live.closed and live.refuse == 98


def test_a_refusal_taken_by_another_thread_still_keeps_the_connection(monkeypatch) -> None:
    # paramiko keeps one refusal per connection: a thread whose refusal another took gets this plain error.
    live = _FakeTransport(active=True, refuse=0, error=paramiko.SSHException("Unable to open channel."))
    r = _with_transports(monkeypatch, live)
    r._sessions = 1
    with pytest.raises(RunnerError, match="MaxSessions"):
        r._open_channel()
    assert not live.closed


def test_a_refused_session_on_a_dead_connection_reconnects(monkeypatch) -> None:
    dead, fresh = _FakeTransport(active=False, refuse=1), _FakeTransport(active=True, refuse=0)
    r = _with_transports(monkeypatch, dead, fresh)
    assert r._open_channel() == "channel"
    assert dead.closed and fresh.opened == 1


def test_another_open_error_on_a_live_connection_fails_only_that_command(monkeypatch) -> None:
    # Dropping the shared connection would kill the other commands running on it, and hide this error behind theirs.
    slow = _FakeTransport(active=True, refuse=0, error=paramiko.SSHException("Timeout opening channel."))
    r = _with_transports(monkeypatch, slow)
    with pytest.raises(RunnerError, match="ssh: opening session: Timeout opening channel."):
        r._open_channel()
    assert not slow.closed


def test_a_connection_that_died_is_opened_again_once(monkeypatch) -> None:
    # paramiko may still call the transport active when the socket has already reported the end.
    broken = _FakeTransport(active=True, refuse=0, error=EOFError())
    broken2 = _FakeTransport(active=True, refuse=0, error=OSError("connection reset"))
    r = _with_transports(monkeypatch, broken, broken2)
    with pytest.raises(RunnerError, match="opening session: connection reset"):
        r._open_channel()
    assert broken.closed and broken2.closed


def test_a_stopping_read_does_not_open_a_session_after_waiting(monkeypatch) -> None:
    live = _FakeTransport(active=True, refuse=1)
    r = _with_transports(monkeypatch, live)
    r._sessions = 1
    stop = {"now": False}

    def sleep(seconds: float) -> None:
        stop["now"] = True  # the read is told to stop while this thread waits

    monkeypatch.setattr(ssh_mod.time, "sleep", sleep)
    runner_mod.set_stop_check(lambda: stop["now"])
    try:
        with pytest.raises(runner_mod.Stopped):
            r._open_channel()
    finally:
        runner_mod.set_stop_check(None)
    assert live.opened == 0 and not live.closed


def test_stdin_is_base64_script(ssh_server) -> None:
    seen: dict[str, str] = {}

    def respond(cmd: str, stdin: str) -> tuple[bytes, bytes, int]:
        seen["stdin"] = stdin
        return base64.b64encode(b'{"ok":true,"data":null}'), b"", 0

    srv = ssh_server(behaviour=Behaviour(respond=respond))
    r = runner(srv)
    try:
        r.run("# body", {"name": "x"})
    finally:
        r.close()
    assert seen["stdin"].endswith("\r\n")
    script_b64, count = seen["stdin"].strip().split(".")
    assert count == "0"
    assert base64.b64decode(script_b64, validate=True).decode().endswith("# body")


def test_silence_past_the_command_timeout_says_so(ssh_server) -> None:
    def respond(cmd: str, stdin: str) -> tuple[bytes, bytes, int]:
        time.sleep(2)
        return base64.b64encode(b'{"ok":true,"data":null}'), b"", 0

    srv = ssh_server(behaviour=Behaviour(respond=respond))
    r = runner(srv, command_timeout=0.5)
    try:
        with pytest.raises(RunnerError, match=r"no output from PowerShell for 0\.5 s"):
            r.run("# body", {})
    finally:
        r.close()


def test_input_too_large_for_windows_openssh_fails_fast(ssh_server) -> None:
    # Over about 70 KB of stdin, Windows OpenSSH stalls until the timeout (#56, lab, Windows Server 2022): refuse it
    # at once, before any connection, and say what to use instead.
    srv = ssh_server()
    r = runner(srv)
    try:
        with pytest.raises(RunnerError, match="too large for SSH.*WinRM"):
            r.run("$out = $p.v", {"v": "x" * 60_000})
    finally:
        r.close()
    assert srv.connections == 0


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"host": ""}, "host and username"),
        ({"password": ""}, "password or private_key"),
        ({"host_key": "garbage"}, "host_key"),
        ({"private_key": "-----BEGIN NOTHING-----", "password": ""}, "private_key"),
    ],
)
def test_validation(kw: dict[str, Any], match: str) -> None:
    base: dict[str, Any] = {
        "host": "h",
        "username": "u",
        "password": "p",
        "host_key": authorized_line(key("ecdsa-host")),
    }
    base.update(kw)
    with pytest.raises(ValueError, match=match):
        SSHRunner(SSHConfig(**base))


def test_key_algorithms() -> None:
    assert key_algorithms("ssh-rsa") == ["rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"]
    assert key_algorithms("ssh-ed25519") == ["ssh-ed25519"]
