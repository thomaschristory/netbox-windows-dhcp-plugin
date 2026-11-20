"""SSH transport through paramiko (Windows OpenSSH server).

Port of ``internal/runner/ssh.go``:

* auth: public key (PEM/OpenSSH; the password is tried as passphrase, then the key
  unencrypted), then password, then keyboard-interactive answering every prompt with
  the password (Windows OpenSSH often only offers keyboard-interactive);
* host key: ``insecure`` > pinned ``host_key`` (authorized_keys format) >
  ``trusted_host_keys`` > ``known_hosts_file`` (default ``~/.ssh/known_hosts``).
  ``trusted_host_keys`` (None: no limit) also bounds ``host_key``, and cannot be
  combined with ``insecure``. The host key algorithms
  offered are restricted to the types known for the host, so a server that also has an
  ed25519 key is not reported as a mismatch when only its RSA key is pinned;
* one lazily opened connection is reused (under a lock), one channel per script; if
  opening a channel fails on a dead connection, it is dropped and re-opened once. A
  live connection is never dropped for a failed open (the other commands on it go
  on); when the server refuses a session while others are open (sshd MaxSessions),
  the open is tried again a few times;
* a non-zero exit status is a result, not a transport error.

paramiko is imported lazily so the rest of the Windows layer imports without it.
This module must not import Django or NetBox.
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import posixpath
import re
import secrets
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import psscript
from . import runner as _runner
from .runner import MAX_OUTPUT, MAX_STDERR, ExecResult, RunnerError, Stopped, execute, raise_if_stopped

# Windows OpenSSH stalls on long stdin, waiting until the timeout (a 65 000 character line hung, 50 000 went through,
# lab, Windows Server 2022, whether the script is read as one line or to the end). A longer script input goes to a file
# over SFTP instead, and only its path and the secrets go on stdin (issue #63). Most scripts send under 24 KB.
STDIN_INLINE_MAX = 32 * 1024
# What the stdin line may still hold once the script is in a file: the path and the secrets, a few hundred bytes.
MAX_STDIN = 32 * 1024

# Waits between tries when the server refuses one more session on the connection (sshd MaxSessions, 10 by default):
# the commands already running on it finish within seconds, freeing a session.
SESSION_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0, 4.0)

# How long after one of our sessions closed a refusal still counts as the server being full: the server frees the
# slot a moment after the channel closes, so a refusal in that gap is worth waiting out.
SESSION_CLOSE_GRACE = 2.0

__all__ = ["SESSION_RETRY_DELAYS", "STDIN_INLINE_MAX", "SSHConfig", "SSHRunner", "key_algorithms", "windows_path"]


def windows_path(sftp_path: str) -> str:
    """A path as Windows OpenSSH's SFTP server writes it ("/C:/Users/x/f") in a form .NET opens ("C:/Users/x/f")."""
    return sftp_path[1:] if re.match(r"^/[A-Za-z]:/", sftp_path) else sftp_path


def _refused(paramiko: Any, e: Exception) -> bool:
    """
    Whether opening a session failed because the server refused it. paramiko keeps the refusal (ChannelException) in
    one slot per connection, so when several sessions are opened at once another thread may take it, and this one
    only gets "Unable to open channel." (paramiko 3 and 4).
    """
    if isinstance(e, paramiko.ChannelException):
        return True
    return type(e) is paramiko.SSHException and str(e) == "Unable to open channel."


class _Stale(Exception):  # noqa: N818 - internal control flow
    """The connection cannot open a session any more and must be replaced."""


@dataclass
class SSHConfig:
    host: str
    username: str
    # Secrets are kept out of repr: error reporting tools print local variables.
    password: str = field(default="", repr=False)
    private_key: str = field(default="", repr=False)  # PEM or OpenSSH private key text
    port: int = 22
    host_key: str = ""  # pinned key, authorized_keys format: "ssh-ed25519 AAAA... [comment]"
    known_hosts_file: str = ""  # default ~/.ssh/known_hosts
    insecure: bool = False  # skip host key verification (lab use only)
    # The only host keys this connection may trust, in authorized_keys format; None for no limit. Set, it bounds
    # host_key (which must be one of them) and replaces known_hosts when host_key is empty. An empty tuple trusts
    # nothing. Lets whoever owns the credentials decide which servers they are sent to (issue #103).
    trusted_host_keys: tuple[str, ...] | None = None
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
    """Return ``(key, encrypted)``; ``encrypted`` is True when the key only opened with the password."""
    paramiko = _paramiko()
    classes = [getattr(paramiko, n) for n in ("Ed25519Key", "ECDSAKey", "RSAKey") if hasattr(paramiko, n)]
    # Without a passphrase first: paramiko also accepts a passphrase on a key that has none, so trying the password
    # first would not tell whether it is the key's passphrase.
    passwords = [None, password] if password else [None]
    last: Exception | None = None
    for pw in passwords:
        for cls in classes:
            try:
                return cls.from_private_key(io.StringIO(pem), password=pw), pw is not None
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


def _key_in(key, keys) -> bool:
    """Whether ``key`` is one of ``keys``: same type and same key material (comments and spacing do not count)."""
    return any(key.get_name() == k.get_name() and key.asbytes() == k.asbytes() for k in keys)


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
        # When the password opened the key it is the key's passphrase, not the account's password.
        self._pkey, self._password_is_passphrase = (
            _parse_private_key(cfg.private_key, cfg.password) if cfg.private_key else (None, False)
        )
        self._pinned = None
        self._trusted: list[Any] | None = None
        self._known: dict[str, Any] = {}
        self._known_path = ""
        if cfg.trusted_host_keys is not None:
            if cfg.insecure:
                raise ValueError("ssh: insecure cannot be combined with trusted_host_keys")
            if isinstance(cfg.trusted_host_keys, str):
                # A bare string would be read one character at a time.
                raise ValueError("ssh: trusted_host_keys must be a list of keys")
            self._trusted = []
            for number, line in enumerate(cfg.trusted_host_keys, 1):
                try:
                    self._trusted.append(_parse_authorized_key(line))
                except ValueError as e:
                    # Name the trusted key, not the server's host_key, so the right setting gets fixed.
                    raise ValueError(f"ssh: trusted host key {number}: {e}") from None
        if not cfg.insecure:
            if cfg.host_key:
                self._pinned = _parse_authorized_key(cfg.host_key)
                if self._trusted is not None and not _key_in(self._pinned, self._trusted):
                    raise ValueError("ssh: host_key is not one of the trusted host keys")
            elif self._trusted is None:
                self._load_known_hosts()
        self._lock = threading.Lock()
        self._transport = None
        # Sessions of this runner open right now (all on the one connection), to tell a full server from one that
        # refuses sessions outright.
        self._sessions = 0
        # When the last of them closed. The server frees a session slot a moment after we close the channel, so a
        # refusal just after a close is still most likely our own sessions filling the server, not a server that
        # refuses sessions outright.
        self._last_closed = float("-inf")
        # Sessions being opened right now by other threads (not yet answered by the server).
        self._opening = 0

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
        if self._trusted:
            # Offer only the types of the trusted keys, so a server with several keys sends one that can match.
            return list(dict.fromkeys(a for k in self._trusted for a in key_algorithms(k.get_name())))
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
        if self._trusted is not None:
            if not _key_in(key, self._trusted):
                raise RunnerError(
                    f"ssh: host key mismatch for {self.cfg.host}: server sent a {key.get_name()} key that is not one "
                    "of the trusted host keys"
                )
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
        # A key passphrase is never offered as a password: a server that refuses the key would learn it.
        if pw and not self._password_is_passphrase:
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
        t = self._connected(addr)
        try:
            return self._open_session(t)
        except _Stale:  # the connection died: reconnect once
            self._drop(t)
        t = self._connected(addr)
        try:
            return self._open_session(t)
        except _Stale as e:
            self._drop(t)
            raise RunnerError(f"ssh: opening session: {e.__cause__}") from e.__cause__

    def _connected(self, addr: str):
        try:
            return self._get_transport()
        except RunnerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RunnerError(f"ssh: connecting to {addr}: {e}") from e

    def _open_session(self, t):
        """
        A new session on ``t``. Only a dead connection raises _Stale (dropped and opened again). A live one is never
        dropped here, since that would kill every command running on it: an open that fails on it fails this command
        only. When the server refuses a session while others of ours are open, it is probably full (sshd
        MaxSessions), so the open is tried again a few times while those finish.
        """
        # unverified: Windows OpenSSH past MaxSessions answers with a channel open failure, as OpenSSH does (checked
        # against paramiko's own server only).
        paramiko = _paramiko()
        for delay in (*SESSION_RETRY_DELAYS, None):
            # A parallel read that is stopping sends nothing more, even after waiting here.
            raise_if_stopped()
            # Counted while it opens: another thread refused meanwhile must see this one as ours, even before the
            # server's answer reaches us.
            chan, error = None, None
            with self._lock:
                self._opening += 1
            try:
                chan = t.open_session(timeout=self.cfg.timeout)
            except Exception as e:  # noqa: BLE001 - paramiko raises SSHException, EOFError, socket errors...
                error = e
            finally:
                # Also on an interrupt, or a leftover count would make every later refusal look like a full server.
                with self._lock:
                    self._opening -= 1
                    if chan is not None:
                        self._sessions += 1
            if chan is not None:
                return chan
            # The connection itself is gone (paramiko may not have noticed yet when the socket reports it).
            if not t.is_active() or isinstance(error, EOFError | OSError):
                raise _Stale from error
            if not _refused(paramiko, error):
                raise RunnerError(f"ssh: opening session: {error}") from error
            with self._lock:
                # Ours fill the server if any are open, being opened by another thread, or just closed (the server
                # frees a slot a moment after the close). Several threads refused at once see each other opening,
                # so a server that refuses every session takes up to the whole retry budget to report it.
                busy = (
                    self._sessions > 0
                    or self._opening > 0
                    or time.monotonic() - self._last_closed < SESSION_CLOSE_GRACE
                )
            if delay is None or not busy:
                # Nothing of ours to wait for (or waited long enough): the reason first, the likely cause after.
                raise RunnerError(
                    f"ssh: {self.cfg.host} refused to open a session: {error}. If it allows fewer sessions per "
                    "connection (sshd MaxSessions, 10 by default) than the plugin runs at once (max_concurrency), "
                    "lower max_concurrency or raise MaxSessions"
                ) from error
            time.sleep(delay)
        raise AssertionError("not reached")

    def _close_channel(self, chan) -> None:
        try:
            chan.close()
        finally:
            with self._lock:
                self._sessions -= 1
                self._last_closed = time.monotonic()

    def _exec(self, argv: list[str], stdin: str) -> ExecResult:
        if len(stdin) <= STDIN_INLINE_MAX:
            return self._exec_stdin(argv, stdin)
        return self._exec_via_file(argv, stdin)

    def _exec_via_file(self, argv: list[str], stdin: str) -> ExecResult:
        """
        Upload the script part of a long stdin line over SFTP and send its path instead (issue #63). The file holds
        what stdin would have carried, minus the secrets, which stay on stdin. It sits in the account's home folder
        under a random name; the bootstrap checks it against the checksum sent on stdin, deletes it once read, and
        this removes it too when the script failed.

        The upload's session is closed before the script's opens, so a long script holds one session at a time like
        any other (sshd MaxSessions).
        """
        # Checked on Windows Server 2025 (domain lab, #63): the SFTP home is "/C:/Users/<user>", sftp-server.exe
        # creates and removes the file, and .NET reads it as "C:/Users/<user>/...". unverified: Windows Server 2022.
        head, rest = psscript.split_script_part(stdin)
        remote = self._upload(head, len(stdin))
        line = psscript.script_file_line(rest, windows_path(remote), head)
        if len(line) > MAX_STDIN:
            self._remove(remote)
            raise RunnerError(f"ssh: the secrets are too long for stdin ({len(line)} bytes)")
        try:
            res = self._exec_stdin(argv, line)
        except Exception:
            # PowerShell may never have read (and so deleted) it.
            self._remove(remote)
            raise
        if res.exit_code != 0:
            self._remove(remote)
        return res

    def _sftp(self):
        """An SFTP client on a new session, which times out like a command (a stalled server must not hang a job)."""
        chan = self._open_channel()
        try:
            chan.settimeout(self.cfg.command_timeout)
            chan.invoke_subsystem("sftp")
            return chan, _paramiko().SFTPClient(chan)
        except BaseException:
            self._close_channel(chan)
            raise

    def _upload(self, head: str, size: int) -> str:
        try:
            chan, sftp = self._sftp()
        except (RunnerError, Stopped):
            # Stopped is not a failure: a parallel read that must stop tells it apart by its type.
            raise
        except Exception as e:  # noqa: BLE001 - paramiko raises SSHException, IOError, EOFError...
            raise self._upload_error(size, e) from e
        remote = ""
        error: Exception | None = None
        try:
            remote = posixpath.join(sftp.normalize("."), f"nbwd-{secrets.token_hex(8)}.txt")
            # "x": never write over a file that is already there.
            # Windows OpenSSH's sftp-server accepts it (lab, Windows Server 2025: a 200 KB script, nothing left behind).
            with sftp.open(remote, "wx") as f:
                f.write(head.encode("ascii"))
        except Exception as e:  # noqa: BLE001
            error = e
        finally:
            with contextlib.suppress(Exception):
                sftp.close()
            self._close_channel(chan)
        if error is not None:
            if remote:
                # A partial upload holds no secret, but leave nothing behind. A fresh session: the one that failed
                # may be stalled, and would make this wait another timeout.
                self._remove(remote)
            raise self._upload_error(size, error) from error
        return remote

    @staticmethod
    def _upload_error(size: int, e: Exception) -> RunnerError:
        return RunnerError(
            f"ssh: the script ({size} bytes) is too long for stdin (Windows OpenSSH stalls on more than "
            f"{STDIN_INLINE_MAX}), and uploading it over SFTP failed: {e}. Enable the sftp subsystem in sshd_config "
            "or use WinRM for this server"
        )

    def _remove(self, remote: str) -> None:
        """Remove a script file the bootstrap did not; a failure here must not hide the script's own result."""
        # Cleanup runs even when the thread's commands must stop: it sends no script, and the file would stay.
        check = getattr(_runner._stop_check, "check", None)
        _runner.set_stop_check(None)
        try:
            chan, sftp = self._sftp()
        except Exception:  # noqa: BLE001
            return
        finally:
            _runner.set_stop_check(check)
        try:
            with contextlib.suppress(Exception):
                sftp.remove(remote)
        finally:
            with contextlib.suppress(Exception):
                sftp.close()
            self._close_channel(chan)

    def _exec_stdin(self, argv: list[str], stdin: str) -> ExecResult:
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
                        # Keep reading past the cap so the remote side never blocks, but stop keeping it.
                        if len(stderr) < MAX_STDERR:
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
                if len(stdout) > MAX_OUTPUT:
                    raise RunnerError(f"ssh: PowerShell sent more than {MAX_OUTPUT // (1024 * 1024)} MiB of output")
            if not chan.status_event.wait(self.cfg.command_timeout):
                raise RunnerError("ssh: timed out waiting for the exit status")
            code = chan.exit_status
            th.join(self.cfg.timeout)
            return ExecResult(stdout=bytes(stdout), stderr=bytes(stderr), exit_code=int(code))
        finally:
            self._close_channel(chan)
