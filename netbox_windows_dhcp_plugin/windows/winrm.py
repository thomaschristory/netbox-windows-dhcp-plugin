"""WinRM transport through pypsrp.

Port of ``internal/runner/winrm.go`` / ``winrm_ntlm.go``. Each script opens a new
WSMan connection and WinRS shell, runs ``powershell.exe -EncodedCommand <bootstrap>``,
writes the base64 script to its stdin and closes everything. A new connection per call
is deliberate: NTLM authenticates a TCP connection, and the Go provider saw EOFs, 401s
and hangs when connections were pooled or shared between concurrent calls.

pypsrp handles SPNEGO/NTLM/Kerberos message encryption over plain HTTP itself
(``encryption="auto"``), so the server can keep ``AllowUnencrypted=false``.

pypsrp is imported lazily so the rest of the Windows layer imports without it.
This module must not import Django or NetBox.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .runner import ExecResult, RunnerError, execute

__all__ = ["AUTH_BASIC", "AUTH_KERBEROS", "AUTH_NTLM", "WinRMConfig", "WinRMRunner", "kerberos_user"]

AUTH_NTLM = "ntlm"
AUTH_BASIC = "basic"
AUTH_KERBEROS = "kerberos"
AUTHS = (AUTH_NTLM, AUTH_BASIC, AUTH_KERBEROS)
_STDIN_CHUNK = 64 * 1024


@dataclass
class WinRMConfig:
    host: str
    username: str
    password: str
    port: int | None = None  # default 5986 with HTTPS, 5985 without
    https: bool = True
    cert_validation: bool = True  # False skips TLS certificate checks (lab use only)
    auth: str = AUTH_NTLM  # ntlm, basic or kerberos
    kerberos_realm: str = ""  # appended to the user name as user@REALM
    kerberos_spn: str = ""  # e.g. HTTP/dhcp01.example.com; default WSMAN/<host> (pypsrp default)
    timeout: int = 60  # seconds: connection/read timeout and WS-Man operation timeout
    path: str = "wsman"

    @property
    def effective_port(self) -> int:
        if self.port:
            return self.port
        return 5986 if self.https else 5985


def kerberos_user(username: str, realm: str = "") -> str:
    """Strip ``DOMAIN\\`` / ``@REALM`` from a user name and append ``@REALM`` when given."""
    u = username.split("\\", 1)[1] if "\\" in username else username
    u = u.split("@", 1)[0]
    return f"{u}@{realm.upper()}" if realm else u


class WinRMRunner:
    """Runner over WinRM. Thread-safe: every call uses its own connection."""

    transport = "winrm"

    def __init__(self, cfg: WinRMConfig) -> None:
        if not cfg.host or not cfg.username or not cfg.password:
            raise ValueError("winrm: host, username and password are required")
        auth = (cfg.auth or AUTH_NTLM).lower()
        if auth not in AUTHS:
            raise ValueError(f"winrm: unsupported auth {cfg.auth!r} (want ntlm, basic or kerberos)")
        if auth == AUTH_KERBEROS and not cfg.kerberos_realm and "@" not in cfg.username:
            raise ValueError("winrm: kerberos_realm is required with kerberos auth (or give the user as user@REALM)")
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
        try:
            from pypsrp.shell import Process, SignalCode, WinRS
            from pypsrp.wsman import WSMan
        except ImportError as e:  # pragma: no cover - dependency is declared
            raise RunnerError("winrm: the pypsrp package is not installed") from e

        wsman = WSMan(**self.wsman_kwargs())
        with wsman, WinRS(wsman) as shell:
            proc = Process(shell, argv[0], argv[1:])
            proc.begin_invoke()
            data = stdin.encode("ascii")
            # Stay well below the default 150 KiB WS-Man envelope size per Send.
            chunks = [data[i : i + _STDIN_CHUNK] for i in range(0, len(data), _STDIN_CHUNK)] or [b""]
            for i, chunk in enumerate(chunks):
                proc.send(chunk, end=i == len(chunks) - 1)
            proc.end_invoke()
            # Best-effort cleanup; the shell is deleted next anyway.
            with contextlib.suppress(Exception):
                proc.signal(SignalCode.TERMINATE)
            rc = -1 if proc.rc is None else int(proc.rc)
            return ExecResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=rc)

    def close(self) -> None:
        """Nothing to close: connections are per call."""
