"""SSH transport against an in-process paramiko SSH server (port of the Go runner tests).

The server plays a Windows OpenSSH host: it accepts the exec request, reads the base64
script from stdin and answers with the fake DHCP server's envelope.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import os
import socket
import struct
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
    # Whether the sftp subsystem is there (Windows OpenSSH has it unless sshd_config drops it).
    sftp: bool = True
    # Seconds each SFTP write waits before answering (a stalled server).
    sftp_stall: float = 0.0


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
    # Every thread the server starts (accept loop, paramiko transports, one per command), joined at teardown so none
    # outlives its test: a thread failing later would be blamed on whichever test runs then.
    threads: list = field(default_factory=list)
    # Set at teardown: commands still being answered give up quietly instead of writing to a closed connection.
    closing: threading.Event = field(default_factory=threading.Event)
    # Files written over SFTP, by path as the server names it ("/C:/Users/svc/..."), and every one ever written.
    files: dict[str, bytes] = field(default_factory=dict)
    uploads: list[tuple[str, bytes]] = field(default_factory=list)

    def read_file(self, path: str) -> str:
        """What the bootstrap does with a script file: read it, then delete it."""
        with self.lock:
            sftp_path = path if path.startswith("/") else "/" + path
            return self.files.pop(sftp_path).decode("ascii")

    def drop_all(self) -> None:
        for t in self.transports:
            t.close()

    def start_thread(self, target: Callable[..., None], *args: Any) -> None:
        t = threading.Thread(target=target, args=args, daemon=True)
        with self.lock:
            self.threads.append(t)
        t.start()


SFTP_HOME = "/C:/Users/svc"


class _Handle(paramiko.SFTPHandle):
    def __init__(self, srv: SSHServer, path: str) -> None:
        super().__init__()
        self.srv, self.path, self.buf = srv, path, bytearray()

    def write(self, offset: int, data: bytes) -> int:
        if self.srv.behaviour.sftp_stall:
            # A server that stops answering mid-upload; the teardown ends the wait.
            self.srv.closing.wait(self.srv.behaviour.sftp_stall)
        self.buf[offset : offset + len(data)] = data
        return paramiko.SFTP_OK

    def close(self) -> None:
        with self.srv.lock:
            self.srv.files[self.path] = bytes(self.buf)
            self.srv.uploads.append((self.path, bytes(self.buf)))


class _Sftp(paramiko.SFTPServerInterface):
    """An in-memory SFTP server whose home is a Windows path, as Windows OpenSSH reports it."""

    def __init__(self, server: paramiko.ServerInterface, srv: SSHServer) -> None:
        super().__init__(server)
        self.srv = srv

    def canonicalize(self, path: str) -> str:
        return SFTP_HOME if path in (".", "") else path

    def open(self, path: str, flags: int, attr: Any) -> Any:
        if flags & os.O_EXCL and path in self.srv.files:
            return paramiko.SFTP_FAILURE
        return _Handle(self.srv, path)

    def remove(self, path: str) -> int:
        with self.srv.lock:
            return paramiko.SFTP_OK if self.srv.files.pop(path, None) is not None else paramiko.SFTP_NO_SUCH_FILE

    def session_ended(self) -> None:
        # The SFTP session counted against max_sessions like an exec one; it is free again.
        with self.srv.lock:
            self.srv.open_sessions -= 1


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
        self.srv.start_thread(self._serve, channel, cmd)
        return True

    def _serve(self, channel, cmd: str) -> None:
        try:
            data = bytearray()
            while True:
                b = channel.recv(65536)
                if not b:
                    break
                data.extend(b)
            respond = self.srv.behaviour.respond or (
                lambda c, s: fake_host_response(self.srv.fake, s, self.srv.read_file)
            )
            stdout, stderr, code = respond(cmd, data.decode())
            if self.srv.closing.is_set():
                # The test is over and its connection closed: there is nobody left to answer.
                return
            if stdout:
                channel.sendall(stdout)
            if stderr:
                channel.sendall_stderr(stderr)
            channel.send_exit_status(code)
            channel.close()
        except (OSError, EOFError):
            # Only a connection closed by the teardown is expected here; anything else is a real failure.
            if not self.srv.closing.is_set():
                raise
        finally:
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
                if srv.closing.is_set():
                    # The teardown's wake-up connection.
                    conn.close()
                    return
                srv.connections += 1
                t = paramiko.Transport(conn)
                for hk in srv.host_keys:
                    t.add_server_key(hk)
                if srv.behaviour.sftp:
                    t.set_subsystem_handler("sftp", paramiko.SFTPServer, _Sftp, srv)
                srv.transports.append(t)
                with srv.lock:
                    # A paramiko Transport is itself a thread: join it at teardown too.
                    srv.threads.append(t)
                try:
                    t.start_server(server=_Iface(srv))
                except Exception:  # noqa: BLE001 - client gave up (e.g. host key mismatch)
                    t.close()

        srv.start_thread(accept)
        return srv

    yield start
    for srv in servers:
        srv.closing.set()
        srv.drop_all()
    for s in socks:
        # Closing a listening socket does not wake a thread blocked in accept() on every platform: connect once so
        # the accept loop sees the closing flag and ends.
        with contextlib.suppress(OSError), socket.create_connection(s.getsockname(), timeout=1):
            pass
        s.close()
    for srv in servers:
        with srv.lock:
            threads = list(srv.threads)
        for t in threads:
            t.join(timeout=5)
        alive = [t.name for t in threads if t.is_alive()]
        assert not alive, f"SSH test server threads still running after the test: {alive}"


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


def test_trusted_host_keys_replace_known_hosts(ssh_server) -> None:
    # Issue #103: a credential profile's ssh_host_keys. Without a key of its own the server must present one of
    # them; the worker's known_hosts is not read at all.
    srv = ssh_server(host_keys=[key("rsa-host"), key("ecdsa-host")])
    trusted = (authorized_line(key("ecdsa-other")), f"  {authorized_line(key('ecdsa-host'))}  ")
    r = runner(srv, host_key="", known_hosts_file="/nonexistent", trusted_host_keys=trusted)
    assert r.host_key_algorithms() == [key("ecdsa-host").get_name()]
    try:
        DhcpClient(r).list_scopes()
    finally:
        r.close()
    other = runner(srv, host_key="", known_hosts_file="/nonexistent", trusted_host_keys=(trusted[0],))
    with pytest.raises(RunnerError, match="not one of the trusted host keys"):
        other.run("# x", {})
    nothing = runner(srv, host_key="", known_hosts_file="/nonexistent", trusted_host_keys=())
    with pytest.raises(RunnerError, match="not one of the trusted host keys"):
        nothing.run("# x", {})


def test_trusted_host_keys_bound_the_pinned_key(ssh_server) -> None:
    srv = ssh_server()
    pinned = authorized_line(srv.host_keys[0])
    # The same key with another comment still counts.
    r = runner(srv, trusted_host_keys=(f"{pinned.rsplit(' ', 1)[0]} another-comment",))
    try:
        DhcpClient(r).list_scopes()
    finally:
        r.close()
    with pytest.raises(ValueError, match="not one of the trusted host keys"):
        runner(srv, trusted_host_keys=(authorized_line(key("ecdsa-other")),))
    with pytest.raises(ValueError, match="insecure cannot be combined"):
        runner(srv, insecure=True, trusted_host_keys=(pinned,))
    with pytest.raises(ValueError, match="must be a list"):
        runner(srv, trusted_host_keys=pinned)
    with pytest.raises(ValueError, match="trusted host key 2"):
        runner(srv, trusted_host_keys=(pinned, "garbage"))
    # Well formed, but not a type paramiko loads: the error names the trusted key, not the server's.
    blob = base64.b64encode(struct.pack(">I", 7) + b"ssh-foo" + b"x").decode()
    with pytest.raises(ValueError, match="trusted host key 1"):
        runner(srv, host_key="", trusted_host_keys=(f"ssh-foo {blob}",))


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


def test_a_refused_open_keeps_waiting_while_ours_run_past_the_back_off(ssh_server, monkeypatch) -> None:
    # Windows takes about 10 s to run ten PowerShell commands at once (lab, Server 2025): a refused open must keep
    # trying while ours run, not give up when the short back-off ends.
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_DELAYS", (0.01,))
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_POLL", 0.05)
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
        assert c.list_scopes() == []
        t.join()
    finally:
        r.close()
    assert first == [[]]
    assert srv.refused > 0 and srv.connections == 1


def test_a_session_still_refused_fails_that_command_only(ssh_server, monkeypatch) -> None:
    # Past the wait limit the refused command fails, with the likely cause; the refusal must not drop the connection:
    # the command already running on it goes on and succeeds.
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_DELAYS", (0.05, 0.05))
    monkeypatch.setattr(ssh_mod, "SESSION_WAIT_MAX", 0.2)
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
        with pytest.raises(RunnerError, match="had no free session for .* while other commands of this job held them"):
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
    # A session of ours that never ends would otherwise be waited for up to five minutes.
    monkeypatch.setattr(ssh_mod, "SESSION_WAIT_MAX", 0.1)
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


def test_a_refusal_just_after_our_session_closed_is_tried_again(monkeypatch) -> None:
    # The server frees a slot a moment after our channel closes: a refusal in that gap is waited out, not reported
    # as a server that refuses sessions (this raced in CI when the counter had just dropped to zero).
    live = _FakeTransport(active=True, refuse=2)
    r = _with_transports(monkeypatch, live)
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_DELAYS", (0.01,) * 3)
    r._sessions = 1
    r._close_channel(_ClosableChannel())
    assert r._sessions == 0
    assert r._open_channel() == "channel"
    assert not live.closed and live.opened == 1


def test_the_session_wait_ends_at_the_command_timeout_when_shorter(monkeypatch) -> None:
    live = _FakeTransport(active=True, refuse=10_000)
    r = _with_transports(monkeypatch, live)
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_POLL", 0.02)
    monkeypatch.setattr(ssh_mod, "SESSION_WAIT_MAX", 300.0)
    r.cfg.command_timeout = 0.3
    r._sessions = 1  # ours never finish here
    started = time.monotonic()
    with pytest.raises(RunnerError, match="had no free session"):
        r._open_channel()
    assert 0.2 < time.monotonic() - started < 2


def test_a_refusal_long_after_the_last_close_fails_at_once(monkeypatch) -> None:
    live = _FakeTransport(active=True, refuse=99)
    r = _with_transports(monkeypatch, live)
    monkeypatch.setattr(ssh_mod, "SESSION_RETRY_DELAYS", (5.0,) * 3)
    r._last_closed = time.monotonic() - ssh_mod.SESSION_CLOSE_GRACE - 1
    started = time.monotonic()
    with pytest.raises(RunnerError):
        r._open_channel()
    assert time.monotonic() - started < 1


def test_a_refusal_while_another_open_is_pending_is_tried_again(monkeypatch) -> None:
    # Another thread's session is accepted by the server but its answer has not reached us yet: nothing is counted
    # as open, yet the server is full of ours, so the refused open must wait (this raced in CI).
    live = _FakeTransport(active=True, refuse=2)
    r = _with_transports(monkeypatch, live)
    r._opening = 1  # the other thread's open, still in flight
    assert r._open_channel() == "channel"
    assert not live.closed and live.opened == 1
    assert r._opening == 1 and r._sessions == 1


def test_an_interrupted_open_leaves_no_count_behind(monkeypatch) -> None:
    # A leftover count would make every later refusal on this runner look like a full server and wait.
    live = _FakeTransport(active=True, refuse=0, error=KeyboardInterrupt())
    r = _with_transports(monkeypatch, live)
    with pytest.raises(KeyboardInterrupt):
        r._open_channel()
    assert r._opening == 0 and r._sessions == 0


class _ClosableChannel:
    def close(self) -> None:
        pass


def test_a_refusal_taken_by_another_thread_still_keeps_the_connection(monkeypatch) -> None:
    # paramiko keeps one refusal per connection: a thread whose refusal another took gets this plain error.
    live = _FakeTransport(active=True, refuse=0, error=paramiko.SSHException("Unable to open channel."))
    r = _with_transports(monkeypatch, live)
    r._sessions = 1
    with pytest.raises(RunnerError, match="had no free session .*Unable to open channel"):
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
    checks = {"n": 0}

    def stopping() -> bool:
        # The first check, before the open, passes; the read is told to stop while this thread waits after the refusal.
        checks["n"] += 1
        return checks["n"] > 1

    runner_mod.set_stop_check(stopping)
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
        # Silent for longer than the command timeout; the teardown ends the wait instead of letting it run on.
        srv.closing.wait(2)
        return base64.b64encode(b'{"ok":true,"data":null}'), b"", 0

    srv = ssh_server(behaviour=Behaviour(respond=respond))
    r = runner(srv, command_timeout=0.5)
    try:
        with pytest.raises(RunnerError, match=r"no output from PowerShell for 0\.5 s"):
            r.run("# body", {})
    finally:
        r.close()


def test_a_long_script_goes_through_a_file_over_sftp(ssh_server) -> None:
    # Windows OpenSSH stalls on long stdin (#56, #63): the script part goes to a file in the home folder, stdin only
    # names it, and the file is gone afterwards.
    lines: list[str] = []
    srv = ssh_server()

    def respond(cmd: str, stdin: str) -> tuple[bytes, bytes, int]:
        lines.append(stdin)
        return fake_host_response(srv.fake, stdin, srv.read_file)

    srv.behaviour.respond = respond
    r = runner(srv)
    try:
        client = DhcpClient(r)
        client.add_scope(Scope(**{**SCOPE.__dict__, "description": "d" * 40_000}))
        assert client.get_scope("10.1.20.0").description == "d" * 40_000
    finally:
        r.close()
    # The add went through a file; the get, short, stayed on stdin.
    first, second = lines
    assert first.startswith("!") and len(first) < 1_000
    assert not second.startswith("!")
    (path, content), *_ = srv.uploads
    assert path.startswith(SFTP_HOME + "/nbwd-") and len(content) > ssh_mod.STDIN_INLINE_MAX
    # The fake host read it as Windows would ("C:/Users/..."), and nothing is left behind.
    path_b64, digest = first.split(".")[0][1:].split(":")
    assert base64.b64decode(path_b64).decode() == path[1:]
    assert digest == hashlib.sha256(content).hexdigest()
    assert r._sessions == 0
    assert srv.files == {}


def test_a_long_script_keeps_its_secrets_on_stdin(ssh_server) -> None:
    seen: dict[str, str] = {}
    srv = ssh_server()

    def respond(cmd: str, stdin: str) -> tuple[bytes, bytes, int]:
        seen["stdin"] = stdin
        srv.read_file(base64.b64decode(stdin.split(".")[0][1:].split(":")[0]).decode())
        return base64.b64encode(b'{"ok":true,"data":null}'), b"", 0

    srv.behaviour.respond = respond
    r = runner(srv)
    try:
        r.run("$out = $p.v", {"v": "x" * 60_000, "shared_secret": "correct-horse"})
    finally:
        r.close()
    (_, content), *_ = srv.uploads
    assert b"correct-horse" not in base64.b64decode(content)
    pairs = seen["stdin"].strip().split(".")[1:-1]
    assert [base64.b64decode(v).decode() for _, v in (x.split(":") for x in pairs)] == ["correct-horse"]


def test_a_script_file_left_behind_is_removed(ssh_server) -> None:
    # PowerShell failed before reading the file: the runner removes it.
    srv = ssh_server(behaviour=Behaviour(respond=lambda c, s: (b"", b"boom", 1)))
    r = runner(srv)
    try:
        with pytest.raises(RunnerError, match="boom"):
            r.run("$out = $p.v", {"v": "x" * 60_000})
    finally:
        r.close()
    assert len(srv.uploads) == 1 and srv.files == {}


def test_a_stalled_upload_times_out(ssh_server) -> None:
    srv = ssh_server(behaviour=Behaviour(sftp_stall=3))
    r = runner(srv, command_timeout=0.5)
    try:
        with pytest.raises(RunnerError, match="uploading it over SFTP failed"):
            r.run("$out = $p.v", {"v": "x" * 60_000})
    finally:
        r.close()
    assert srv.commands == []


def test_long_scripts_hold_one_session_each(ssh_server) -> None:
    # The upload's session closes before the script's opens: two long scripts at once fit in two sessions.
    srv = ssh_server(behaviour=Behaviour(max_sessions=2))

    def respond(cmd: str, stdin: str) -> tuple[bytes, bytes, int]:
        srv.read_file(base64.b64decode(stdin.split(".")[0][1:].split(":")[0]).decode())
        time.sleep(0.3)  # both scripts run at the same time
        return base64.b64encode(b'{"ok":true,"data":null}'), b"", 0

    srv.behaviour.respond = respond
    r = runner(srv)
    errors: list[BaseException] = []

    def one() -> None:
        try:
            r.run("$out = $p.v", {"v": "x" * 60_000})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=one) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        r.close()
    assert not errors, errors
    # A refused open is retried (the server frees a slot a moment after a close), so refusals are not counted here.
    assert len(srv.uploads) == 2 and srv.files == {}
    assert r._sessions == 0


def test_a_stopping_read_stops_before_the_upload(ssh_server) -> None:
    srv = ssh_server()
    r = runner(srv)
    runner_mod.set_stop_check(lambda: True)
    try:
        with pytest.raises(runner_mod.Stopped):
            r.run("$out = $p.v", {"v": "x" * 60_000})
    finally:
        runner_mod.set_stop_check(None)
        r.close()
    assert srv.uploads == [] and srv.commands == []


def test_a_stop_after_the_upload_still_removes_the_file(ssh_server, monkeypatch) -> None:
    srv = ssh_server()
    r = runner(srv)
    upload = r._upload

    def then_stop(head: str, size: int) -> str:
        remote = upload(head, size)
        runner_mod.set_stop_check(lambda: True)
        return remote

    monkeypatch.setattr(r, "_upload", then_stop)
    try:
        with pytest.raises(runner_mod.Stopped):
            r.run("$out = $p.v", {"v": "x" * 60_000})
    finally:
        runner_mod.set_stop_check(None)
        r.close()
    assert len(srv.uploads) == 1 and srv.files == {} and srv.commands == []


def test_a_replaced_script_file_is_refused(ssh_server) -> None:
    srv = ssh_server()
    original = srv.read_file

    def swapped(path: str) -> str:
        original(path)
        return base64.b64encode(b"# another script").decode()

    srv.read_file = swapped
    r = runner(srv)
    try:
        with pytest.raises(RunnerError, match="does not match its checksum"):
            r.run("$out = $p.v", {"v": "x" * 60_000})
    finally:
        r.close()


def test_a_long_script_without_sftp_says_what_to_do(ssh_server) -> None:
    srv = ssh_server(behaviour=Behaviour(sftp=False))
    r = runner(srv)
    try:
        with pytest.raises(RunnerError, match="too long for stdin.*SFTP failed.*sftp subsystem.*WinRM"):
            r.run("$out = $p.v", {"v": "x" * 60_000})
    finally:
        r.close()
    assert srv.commands == []


def test_a_short_script_stays_on_stdin(ssh_server) -> None:
    srv = ssh_server()
    r = runner(srv)
    try:
        DhcpClient(r).add_scope(SCOPE)
    finally:
        r.close()
    assert srv.uploads == []


@pytest.mark.parametrize(
    ("given", "want"),
    [("/C:/Users/svc/f.txt", "C:/Users/svc/f.txt"), ("/home/svc/f.txt", "/home/svc/f.txt"), ("C:/x", "C:/x")],
)
def test_windows_path(given: str, want: str) -> None:
    assert ssh_mod.windows_path(given) == want


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


def test_a_stopping_read_leaves_the_line_while_waiting_its_turn(monkeypatch) -> None:
    # Another open is ahead in line: a read told to stop must not wait for its turn.
    live = _FakeTransport(active=True, refuse=0)
    r = _with_transports(monkeypatch, live)
    monkeypatch.setattr(ssh_mod, "SESSION_WAIT_MAX", 30.0)
    r._line.append(object())  # an earlier refused open, still waiting
    checks = {"n": 0}

    def stopping() -> bool:
        checks["n"] += 1
        return checks["n"] > 1

    runner_mod.set_stop_check(stopping)
    started = time.monotonic()
    try:
        with pytest.raises(runner_mod.Stopped):
            r._open_channel()
    finally:
        runner_mod.set_stop_check(None)
    assert time.monotonic() - started < 2
    assert live.opened == 0 and len(r._line) == 1


def test_a_waiter_gives_up_once_none_of_ours_is_left(monkeypatch) -> None:
    # Ours were running, so the refused open waits; when the last one closes and the server still refuses, the open
    # fails with the MaxSessions hint instead of waiting out the deadline.
    live = _FakeTransport(active=True, refuse=10_000)
    r = _with_transports(monkeypatch, live)
    monkeypatch.setattr(ssh_mod, "SESSION_WAIT_MAX", 30.0)
    monkeypatch.setattr(ssh_mod, "SESSION_CLOSE_GRACE", 0.1)
    r._sessions = 1
    threading.Timer(0.3, lambda: r._close_channel(_ClosableChannel())).start()
    started = time.monotonic()
    with pytest.raises(RunnerError, match="MaxSessions"):
        r._open_channel()
    assert time.monotonic() - started < 3
    assert not r._line


def test_waiters_take_freed_sessions_in_the_order_they_were_refused(monkeypatch) -> None:
    # Three opens refused in turn; then sessions free up one at a time: the first refused gets the first one.
    class Slots(_FakeTransport):
        def __init__(self) -> None:
            super().__init__(active=True, refuse=0)
            self.free = 0
            self.lock = threading.Lock()

        def open_session(self, timeout: float | None = None) -> str:
            with self.lock:
                if self.free <= 0:
                    raise paramiko.ChannelException(2, "Connect failed")
                self.free -= 1
                return "channel"

    live = Slots()
    r = _with_transports(monkeypatch, live)
    monkeypatch.setattr(ssh_mod, "SESSION_WAIT_MAX", 30.0)
    r._sessions = 3
    order: list[int] = []

    def work(i: int) -> None:
        r._open_channel()
        order.append(i)

    threads = []
    for i in range(3):
        th = threading.Thread(target=work, args=(i,))
        th.start()
        threads.append(th)
        time.sleep(0.15)  # refused in this order
    for _ in range(3):
        with live.lock:
            live.free += 1
        r._close_channel(_ClosableChannel())
        time.sleep(0.4)
    for th in threads:
        th.join(5)
    assert order == [0, 1, 2]
