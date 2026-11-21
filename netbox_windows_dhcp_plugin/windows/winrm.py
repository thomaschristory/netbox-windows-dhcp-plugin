"""WinRM transport through pypsrp.

Port of ``internal/runner/winrm.go`` / ``winrm_ntlm.go``. Each script opens a new
WSMan connection and WinRS shell, runs ``powershell.exe -EncodedCommand <bootstrap>``,
writes the base64 script to its stdin and closes everything. A new connection per call
is deliberate: NTLM authenticates a TCP connection, and the Go provider saw EOFs, 401s
and hangs when connections were pooled or shared between concurrent calls.

pypsrp handles SPNEGO/NTLM/Kerberos/CredSSP message encryption over plain HTTP itself
(``encryption="auto"``), so the server can keep ``AllowUnencrypted=false``.

CredSSP hands the password to the DHCP server, which can then act as the user towards other servers. Failover needs
that: Add-DhcpServerv4Failover and friends make the server talk to its partner as the caller, which an NTLM or plain
Kerberos logon cannot do (the "double hop", refused with PermissionDenied).

pypsrp is imported lazily so the rest of the Windows layer imports without it.
This module must not import Django or NetBox.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .runner import MAX_OUTPUT, ExecResult, RunnerError, execute, raise_if_stopped

__all__ = [
    "AUTH_BASIC",
    "AUTH_CREDSSP",
    "AUTH_KERBEROS",
    "AUTH_NTLM",
    "PROXY_ENV",
    "PROXY_NONE",
    "WinRMConfig",
    "WinRMRunner",
    "kerberos_user",
]

AUTH_NTLM = "ntlm"
AUTH_BASIC = "basic"
AUTH_KERBEROS = "kerberos"
AUTH_CREDSSP = "credssp"
AUTHS = (AUTH_NTLM, AUTH_BASIC, AUTH_KERBEROS, AUTH_CREDSSP)
# Whether WinRM goes through the HTTP proxy named in the worker's environment (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY and
# their lower-case forms, minus NO_PROXY). "none" (the default, issue #105) connects straight to the server: NetBox
# containers often have a proxy for internet access, and WinRM to an internal DHCP server should not silently take it.
# "env" follows those variables, for sites that reach their DHCP servers through a proxy on purpose.
PROXY_NONE = "none"
PROXY_ENV = "env"
PROXIES = (PROXY_NONE, PROXY_ENV)
_STDIN_CHUNK = 64 * 1024
# A command that never finishes is abandoned after this many times the per-wait timeout, and never sooner than the
# minimum, so that slow but healthy commands (a large lease list, failover replication) still pass.
_TOTAL_TIMEOUT_FACTOR = 10
_MIN_TOTAL_TIMEOUT = 600
# Waits before trying a call again when a TCP connection was refused before its command was sent (issue #132). Under
# parallel calls the network path to the server (a NAT router, Docker Desktop's port forwarding) sometimes answers a
# new connection with a refusal although WinRM is up. A refused connection carries nothing, so trying again is safe
# even for a write; a server that really is down still fails within a few seconds.
CONNECT_RETRY_DELAYS = (0.5, 1.0, 2.0)


@dataclass
class WinRMConfig:
    host: str
    username: str
    password: str = field(repr=False)  # kept out of repr: error reporting tools print local variables
    port: int | None = None  # default 5986 with HTTPS, 5985 without
    https: bool = True
    cert_validation: bool = True  # False skips TLS certificate checks (lab use only)
    auth: str = AUTH_NTLM  # ntlm, basic, kerberos or credssp
    kerberos_realm: str = ""  # appended to the user name as user@REALM
    kerberos_spn: str = ""  # e.g. HTTP/dhcp01.example.com; default WSMAN/<host> (pypsrp default)
    proxy: str = PROXY_NONE  # "none": always connect directly; "env": follow the proxy environment variables
    timeout: int = 60  # seconds: connection/read timeout and WS-Man operation timeout (one wait for output)
    total_timeout: float = 0  # seconds one command may run in all; 0 means 10 x timeout, at least 10 minutes
    path: str = "wsman"

    @property
    def effective_port(self) -> int:
        if self.port:
            return self.port
        return 5986 if self.https else 5985

    @property
    def effective_total_timeout(self) -> float:
        if self.total_timeout:
            return float(self.total_timeout)
        return float(max(_TOTAL_TIMEOUT_FACTOR * int(self.timeout), _MIN_TOTAL_TIMEOUT))


def kerberos_user(username: str, realm: str = "") -> str:
    """Strip ``DOMAIN\\`` / ``@REALM`` from a user name and append ``@REALM`` when given."""
    u = username.split("\\", 1)[1] if "\\" in username else username
    u = u.split("@", 1)[0]
    realm = realm.strip().upper()
    return f"{u}@{realm}" if realm else u


def refused_connect(exc: BaseException) -> bool:
    """True when ``exc`` comes from a TCP connection the server side refused, so nothing was sent on it.

    requests and urllib3 wrap the socket error several times (ConnectionError, MaxRetryError, NewConnectionError) in
    ``__cause__``, ``__context__``, ``reason`` or the arguments, so look through all of them.
    """
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        e = todo.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, ConnectionRefusedError):
            return True
        nested = [e.__cause__, e.__context__, getattr(e, "reason", None), *e.args]
        todo.extend(x for x in nested if isinstance(x, BaseException))
    return False


def _prepare_session(wsman: Any, proxy: str) -> None:
    """Build pypsrp's HTTP session now and lock it down before anything is sent.

    Redirects: the allowed_hosts check approved one host. The HTTP library would follow a redirect and answer the
    authentication challenge of whatever host it points at; WinRM never redirects, so refusing costs nothing.

    Proxies: pypsrp's session reads the proxy environment variables, both when it is built and again on every request
    (requests' ``trust_env``), and its own ``no_proxy`` option only clears the scheme in use. With ``proxy="none"``
    the session stops reading the environment altogether and holds no proxy, so no request of this connection (the
    logon rounds of NTLM, Kerberos or CredSSP included: they reuse the request's settings) goes to a proxy, whatever
    HTTP_PROXY, HTTPS_PROXY or ALL_PROXY say. The CA bundle variables (REQUESTS_CA_BUNDLE) were read when the session
    was built and still apply.

    pypsrp builds its HTTP session on first use and offers no option for either, so build it now and set them.
    """
    transport = getattr(wsman, "transport", None)
    build = getattr(transport, "_build_session", None)
    if build is None:
        # A pypsrp release that changed its internals: stop rather than run without the guard.
        raise RunnerError("winrm: this pypsrp version does not let the plugin forbid HTTP redirects")
    session = build()
    session.max_redirects = 0
    if proxy != PROXY_ENV:
        session.trust_env = False
        session.proxies = {}
    transport.session = session


class WinRMRunner:
    """Runner over WinRM. Thread-safe: every call uses its own connection."""

    transport = "winrm"

    def __init__(self, cfg: WinRMConfig) -> None:
        if not cfg.host or not cfg.username or not cfg.password:
            raise ValueError("winrm: host, username and password are required")
        auth = (cfg.auth or AUTH_NTLM).lower()
        if auth not in AUTHS:
            raise ValueError(f"winrm: unsupported auth {cfg.auth!r} (want ntlm, basic, kerberos or credssp)")
        if auth == AUTH_KERBEROS and not cfg.kerberos_realm and "@" not in cfg.username:
            raise ValueError("winrm: kerberos_realm is required with kerberos auth (or give the user as user@REALM)")
        if cfg.proxy not in PROXIES:
            # Never repeat the value: a proxy URL often holds a password.
            raise ValueError("winrm: unsupported proxy setting (want none or env; a proxy URL is not accepted)")
        cfg.auth = auth
        self.cfg = cfg

    def wsman_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``pypsrp.wsman.WSMan`` (split out for tests)."""
        cfg = self.cfg
        timeout = int(cfg.timeout)
        kwargs: dict[str, Any] = {
            "server": cfg.host,
            "port": cfg.effective_port,
            "username": cfg.username,
            "password": cfg.password,
            "ssl": cfg.https,
            "path": cfg.path,
            "auth": cfg.auth,
            "cert_validation": cfg.cert_validation,
            "connection_timeout": timeout,
            # The read timeout must outlast one WS-Man receive (operation timeout).
            "operation_timeout": timeout,
            "read_timeout": timeout + 10,
            "encryption": "auto",
        }
        if cfg.proxy != PROXY_ENV:
            # pypsrp's own switch only covers the scheme in use; _prepare_session closes the rest.
            kwargs["no_proxy"] = True
        if cfg.auth == AUTH_BASIC and not cfg.https:
            # Basic cannot encrypt messages; over HTTP the server must allow unencrypted
            # traffic. Only sensible on an isolated lab network.
            kwargs["encryption"] = "never"
        if cfg.auth == AUTH_KERBEROS:
            kwargs["username"] = kerberos_user(cfg.username, cfg.kerberos_realm)
            if cfg.kerberos_spn:
                service, _, host = cfg.kerberos_spn.partition("/")
                kwargs["negotiate_service"] = service
                if host:
                    kwargs["negotiate_hostname_override"] = host.split(":", 1)[0]
        return kwargs

    def run(self, script: str, params: Mapping[str, Any] | None = None) -> bytes:
        return execute(self.transport, self._exec, script, params)

    def _exec(self, argv: list[str], stdin: str) -> ExecResult:
        """Run one command, trying again only when a connection was refused before the command was sent."""
        for delay in (*CONNECT_RETRY_DELAYS, None):
            # A parallel read that is stopping sends nothing more, even after waiting here.
            raise_if_stopped()
            sent: list[bool] = []
            try:
                return self._exec_once(argv, stdin, sent)
            except Exception as e:
                # Once the command went out, Windows may have run it: a failure after that is never tried again,
                # refused connection or not, so that a write cannot run twice.
                if sent or delay is None or not refused_connect(e):
                    raise
            time.sleep(delay)
        raise AssertionError("not reached")

    def _exec_once(self, argv: list[str], stdin: str, sent: list[bool]) -> ExecResult:
        """One attempt. Appends to ``sent`` just before the command is sent to Windows."""
        try:
            from pypsrp.shell import CommandState, Process, SignalCode, WinRS
            from pypsrp.wsman import WSMan
        except ImportError as e:  # pragma: no cover - dependency is declared
            raise RunnerError("winrm: the pypsrp package is not installed") from e
        if self.cfg.auth == AUTH_CREDSSP:
            # pypsrp only imports it on the first request, with a less helpful message.
            try:
                import requests_credssp  # noqa: F401
            except ImportError as e:
                raise RunnerError("winrm: CredSSP needs the requests-credssp package (pypsrp[credssp])") from e

        wsman = WSMan(**self.wsman_kwargs())
        with wsman:
            _prepare_session(wsman, self.cfg.proxy)
            with WinRS(wsman) as shell:
                proc = Process(shell, argv[0], argv[1:])
                # Until here only the logon and the shell creation went out, and a failed shell creation leaves
                # nothing behind on Windows.
                sent.append(True)
                proc.begin_invoke()
                data = stdin.encode("ascii")
                # Stay well below the default 150 KiB WS-Man envelope size per Send.
                chunks = [data[i : i + _STDIN_CHUNK] for i in range(0, len(data), _STDIN_CHUNK)] or [b""]
                for i, chunk in enumerate(chunks):
                    proc.send(chunk, end=i == len(chunks) - 1)
                self._wait(proc, CommandState.DONE, SignalCode.TERMINATE)
                # Best-effort cleanup; the shell is deleted next anyway.
                with contextlib.suppress(Exception):
                    proc.signal(SignalCode.TERMINATE)
                rc = -1 if proc.rc is None else int(proc.rc)
                return ExecResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=rc)

    def _wait(self, proc: Any, done: Any, terminate: Any) -> None:
        """Collect output until the command ends, like pypsrp's ``end_invoke`` but with an end.

        pypsrp treats the WS-Man operation timeout as "no output yet" and polls again for ever, so ``timeout`` only
        bounds one wait: a cmdlet that hangs on the server would hold the job (and the server's lock) until the job
        queue kills the worker. The total is generous on purpose, because a command that is still working must not
        be cut off. A write abandoned here may still complete on the server; the next run sees what is there.
        """
        total = self.cfg.effective_total_timeout
        deadline = time.monotonic() + total
        while proc.state != done:
            if time.monotonic() > deadline:
                with contextlib.suppress(Exception):
                    proc.signal(terminate)
                raise RunnerError(f"winrm: PowerShell did not finish within {total:g} s")
            proc.poll_invoke()
            if len(proc.stdout) + len(proc.stderr) > MAX_OUTPUT:
                with contextlib.suppress(Exception):
                    proc.signal(terminate)
                raise RunnerError(f"winrm: PowerShell sent more than {MAX_OUTPUT // (1024 * 1024)} MiB of output")

    def close(self) -> None:
        """Nothing to close: connections are per call."""
