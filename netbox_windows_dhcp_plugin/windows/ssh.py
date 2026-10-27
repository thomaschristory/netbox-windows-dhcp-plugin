"""SSH transport through paramiko (Windows OpenSSH server).

Port of ``internal/runner/ssh.go``:

* auth: public key (PEM/OpenSSH; the password is tried as passphrase, then the key
  unencrypted), then password, then keyboard-interactive answering every prompt with
  the password (Windows OpenSSH often only offers keyboard-interactive);
* host key: ``insecure`` > pinned ``host_key`` (authorized_keys format) >
  ``known_hosts_file`` (default ``~/.ssh/known_hosts``). The host key algorithms
  offered are restricted to the types known for the host, so a server that also has an
  ed25519 key is not reported as a mismatch when only its RSA key is pinned;
* one lazily opened connection is reused (under a lock), one channel per script; if
  opening a channel fails the connection is dropped and re-opened once;
* a non-zero exit status is a result, not a transport error.

paramiko is imported lazily so the rest of the Windows layer imports without it.
This module must not import Django or NetBox.
"""

from __future__ import annotations

import base64
import io
import os
import socket
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .runner import ExecResult, RunnerError, execute

# Windows OpenSSH stalls on more input than this, waiting until the timeout (between 70 and 90 KB in the lab, Windows
# Server 2022, whether the script is read as one line or to the end). The plugin's scripts send under 10 KB; refuse
# larger ones at once rather than hang.
MAX_STDIN = 64 * 1024

__all__ = ["SSHConfig", "SSHRunner", "key_algorithms"]


@dataclass
class SSHConfig:
    host: str
    username: str
    password: str = ""
    private_key: str = ""  # PEM or OpenSSH private key text
    port: int = 22
    host_key: str = ""  # pinned key, authorized_keys format: "ssh-ed25519 AAAA... [comment]"
    known_hosts_file: str = ""  # default ~/.ssh/known_hosts
    insecure: bool = False  # skip host key verification (lab use only)
    timeout: float = 30.0  # connect, handshake and auth timeout, seconds
    command_timeout: float = 300.0  # max silence while reading a script's output, seconds


def key_algorithms(key_type: str) -> list[str]:
    """Host key algorithms that verify a key of ``key_type`` (RSA has three)."""
    if key_type == "ssh-rsa":
        return ["rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"]
    return [key_type]


def _paramiko():
    try:
        import paramiko
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise RunnerError("ssh: the paramiko package is not installed") from e
    return paramiko


def _parse_private_key(pem: str, password: str):
    paramiko = _paramiko()
    classes = [getattr(paramiko, n) for n in ("Ed25519Key", "ECDSAKey", "RSAKey") if hasattr(paramiko, n)]
    passwords = [password, None] if password else [None]
    last: Exception | None = None
    for pw in passwords:
        for cls in classes:
            try:
                return cls.from_private_key(io.StringIO(pem), password=pw)
            except Exception as e:  # noqa: BLE001 - wrong class or wrong passphrase; try the next
                last = e
    raise ValueError(f"ssh: parsing private_key: {last}")


def _parse_authorized_key(line: str):
    paramiko = _paramiko()
    fields = line.strip().split()
    # Allow an optional leading host pattern, as in a known_hosts line.
    if len(fields) >= 3 and not fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
        fields = fields[1:]
    if len(fields) < 2:
        raise ValueError("ssh: parsing host_key: want '<type> <base64> [comment]'")
    key_type, b64 = fields[0], fields[1]
    try:
        raw = base64.b64decode(b64, validate=True)
        return paramiko.PKey.from_type_string(key_type, raw)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"ssh: parsing host_key: {e}") from e


class SSHRunner:
    """Runner over SSH. Thread-safe: channels on one shared transport."""

    transport_name = "ssh"

    def __init__(self, cfg: SSHConfig) -> None:
        if not cfg.host or not cfg.username:
            raise ValueError("ssh: host and username are required")
        if not cfg.password and not cfg.private_key:
            raise ValueError("ssh: password or private_key is required")
        self.cfg = cfg
        self.port = cfg.port or 22
        self._pkey = _parse_private_key(cfg.private_key, cfg.password) if cfg.private_key else None
        self._pinned = None
        self._known: dict[str, Any] = {}
        self._known_path = ""
        if not cfg.insecure:
            if cfg.host_key:
                self._pinned = _parse_authorized_key(cfg.host_key)
            else:
                self._load_known_hosts()
        self._lock = threading.Lock()
        self._transport = None

    # -- host keys ---------------------------------------------------------

    def _known_hosts_name(self) -> str:
        return self.cfg.host if self.port == 22 else f"[{self.cfg.host}]:{self.port}"

    def _load_known_hosts(self) -> None:
        paramiko = _paramiko()
        path = self.cfg.known_hosts_file or os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")
        self._known_path = path
        try:
            hk = paramiko.HostKeys(path)
        except OSError as e:
            raise ValueError(
                f"ssh: reading known_hosts_file {path} (set host_key, or insecure to skip verification): {e}"
            ) from e
        entry = hk.lookup(self._known_hosts_name())
        self._known = dict(entry) if entry else {}

    def host_key_algorithms(self) -> list[str] | None:
        """The host key algorithms to offer, or None for paramiko's defaults (insecure)."""
        if self._pinned is not None:
            return key_algorithms(self._pinned.get_name())
        if self._known:
            out: list[str] = []
            for t in self._known:
                for a in key_algorithms(t):
                    if a not in out:
                        out.append(a)
            return out
        return None

    def verify_host_key(self, key) -> None:
        if self.cfg.insecure:
            return
        if self._pinned is not None:
            if key.get_name() != self._pinned.get_name() or key.asbytes() != self._pinned.asbytes():
                raise RunnerError(f"ssh: host key mismatch for {self.cfg.host}: server sent {key.get_name()}")
            return
        if not self._known:
            raise RunnerError(
                f"ssh: host {self._known_hosts_name()} is not in {self._known_path}; add it (ssh-keyscan), "
                "set host_key, or set insecure"
            )
        want = self._known.get(key.get_name())
        if want is None or want.asbytes() != key.asbytes():
            raise RunnerError(f"ssh: host key mismatch for {self._known_hosts_name()} (known_hosts {self._known_path})")

    # -- connection --------------------------------------------------------

    def _open_socket(self) -> socket.socket:
        return socket.create_connection((self.cfg.host, self.port), timeout=self.cfg.timeout)

    def _connect(self):
        paramiko = _paramiko()
        sock = self._open_socket()
        t = paramiko.Transport(sock)
        try:
            t.banner_timeout = self.cfg.timeout
            t.handshake_timeout = self.cfg.timeout
            t.auth_timeout = self.cfg.timeout
            algos = self.host_key_algorithms()
            if algos:
                opts = t.get_security_options()
                supported = [a for a in algos if a in t._key_info]  # noqa: SLF001 - the setter validates against it
                if supported:
                    opts.key_types = supported
            t.start_client(timeout=self.cfg.timeout)
            self.verify_host_key(t.get_remote_server_key())
            self._authenticate(t)
        except Exception:
            t.close()
            raise
        return t

    def _authenticate(self, t) -> None:
        paramiko = _paramiko()
        user, pw = self.cfg.username, self.cfg.password
        errors: list[str] = []
        if self._pkey is not None:
            try:
                t.auth_publickey(user, self._pkey)
            except paramiko.AuthenticationException as e:
                errors.append(f"publickey: {e}")
            if t.is_authenticated():
                return
        if pw:
            try:
                t.auth_password(user, pw, fallback=False)
            except paramiko.AuthenticationException as e:
                errors.append(f"password: {e}")
            if t.is_authenticated():
                return

            def answer(_title, _instructions, prompts):
                return [pw for _ in prompts]

            try:
                t.auth_interactive(user, answer)
            except paramiko.AuthenticationException as e:
                errors.append(f"keyboard-interactive: {e}")
            if t.is_authenticated():
                return
        raise RunnerError("ssh: authentication failed: " + "; ".join(errors or ["no method succeeded"]))

    def _get_transport(self):
        with self._lock:
            if self._transport is not None and self._transport.is_active():
                return self._transport
            if self._transport is not None:
                self._transport.close()
            self._transport = self._connect()
            return self._transport

    def _drop(self, t) -> None:
        with self._lock:
            if self._transport is t:
                t.close()
                self._transport = None

    def close(self) -> None:
        with self._lock:
            if self._transport is not None:
                self._transport.close()
                self._transport = None

    # -- running -----------------------------------------------------------

    def run(self, script: str, params: Mapping[str, Any] | None = None) -> bytes:
        return execute(self.transport_name, self._exec, script, params)

    def _open_channel(self):
        addr = f"{self.cfg.host}:{self.port}"
        try:
            t = self._get_transport()
        except RunnerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RunnerError(f"ssh: connecting to {addr}: {e}") from e
        try:
            return t.open_session(timeout=self.cfg.timeout)
        except Exception:  # noqa: BLE001 - stale connection: reconnect once
            self._drop(t)
        try:
            t = self._get_transport()
        except RunnerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RunnerError(f"ssh: connecting to {addr}: {e}") from e
        try:
            return t.open_session(timeout=self.cfg.timeout)
        except Exception as e:  # noqa: BLE001
            self._drop(t)
            raise RunnerError(f"ssh: opening session: {e}") from e

    def _exec(self, argv: list[str], stdin: str) -> ExecResult:
        if len(stdin) > MAX_STDIN:
            raise RunnerError(
                f"ssh: script too large for SSH ({len(stdin)} bytes, at most {MAX_STDIN}): Windows OpenSSH stalls on "
                "larger input; use WinRM for this server"
            )
        chan = self._open_channel()
        try:
            chan.settimeout(self.cfg.command_timeout)
            chan.exec_command(" ".join(argv))
            stderr = bytearray()

            def read_stderr() -> None:
                try:
                    while True:
                        b = chan.recv_stderr(32768)
                        if not b:
                            return
                        stderr.extend(b)
                except Exception:  # noqa: BLE001, S110 - timeout or closed; stdout reader reports it
                    return

            th = threading.Thread(target=read_stderr, daemon=True)
            th.start()
            chan.sendall(stdin.encode("ascii"))
            chan.shutdown_write()
            stdout = bytearray()
            while True:
                try:
                    b = chan.recv(32768)
                except TimeoutError:
                    raise RunnerError(f"ssh: no output from PowerShell for {self.cfg.command_timeout:g} s") from None
                if not b:
                    break
                stdout.extend(b)
            if not chan.status_event.wait(self.cfg.command_timeout):
                raise RunnerError("ssh: timed out waiting for the exit status")
            code = chan.exit_status
            th.join(self.cfg.timeout)
            return ExecResult(stdout=bytes(stdout), stderr=bytes(stderr), exit_code=int(code))
        finally:
            chan.close()
