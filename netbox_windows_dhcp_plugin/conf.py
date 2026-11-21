import base64
import binascii
import contextlib
import ipaddress
import logging
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

PLUGIN_NAME = "netbox_windows_dhcp_plugin"

log = logging.getLogger(__name__)

DEFAULTS = {
    # Credential profiles, referenced by name from WindowsServer.credential_profile and
    # FailoverRelationship.shared_secret_profile. Each value is a dict; every secret can be given inline, from an
    # environment variable (<key>_env) or from a file (<key>_file):
    #   {"dhcp-admin": {"username": "CORP\\svc-dhcp", "password_env": "DHCP_ADMIN_PASSWORD"}}
    # Add "allowed_hosts" (a list of host names or IPs; * and ? match within one label) to limit which hosts a profile's
    # credentials may be sent to. Without it, anyone allowed to edit a Windows server can point it at a machine
    # they control and capture the credentials.
    # CredSSP (needed for failover) hands the password to the server, so it requires HTTPS with certificate
    # verification, as Basic does. "allow_insecure_credssp": True on a profile lifts that, for labs only.
    # A profile can also pin what a server record may otherwise choose (each optional, a list or tuple, an empty one
    # allows nothing it applies to): "transports" (["winrm"], ["ssh"]), "ports" ([5986]; a port does not imply TLS),
    # "kerberos_realms" (["CORP.EXAMPLE"], for WinRM with Kerberos) and "ssh_host_keys" (["ssh-ed25519 AAAA..."]: the
    # only host keys SSH trusts with it). "require_verified_https": True (true or false only) makes WinRM with the
    # profile use HTTPS with certificate verification, whatever the logon, and SSH with it needs "ssh_host_keys".
    "credentials": {},
    # Prefix of the branches created by imports when netbox-branching is installed.
    "import_branch_prefix": "dhcp-import",
    # Open a change request for each import branch when netbox-change-control is installed.
    "import_change_request": True,
    # With netbox-branching, also offer "Import directly": an import written straight into main, without a branch.
    # The user still needs permission to change every object it writes, and change control's protect_main still
    # applies. Set to False to allow imports into a branch only.
    "import_direct": True,
    # Username that scheduled imports run as. It owns import branches and requests their change requests (manual
    # imports use the user who clicked). With branching, scheduled imports fail without it.
    "import_user": None,
    # Applying or dismissing more open import proposal items than this at once runs as a background job instead of
    # inside the web request, so a large first import cannot time out the browser or a reverse proxy. None keeps
    # every review in the request.
    "review_job_threshold": 100,
    # Days to keep sync runs (the history of imports, pushes, dry runs and connection tests). Older runs are deleted
    # by the import scheduler, a batch at a time; runs the plugin still reads are kept whatever their age. 0 or None
    # keeps every run.
    "sync_run_retention_days": 90,
    # Seconds to wait after a save before pushing, so a burst of edits becomes one push (without branching).
    "push_debounce": 10,
    # How many scopes an import, push or dry run reads at the same time from one Windows server record, and the most
    # PowerShell commands it has running there at once (each its own WinRM connection or SSH session). 1 reads one
    # scope after another; 1 to 10 is sensible. Changes are always sent one after another.
    "max_concurrency": 4,
    # Seconds a single PowerShell command may stay silent before it is abandoned. Over WinRM this is one wait for
    # output; a command that keeps the connection alive without finishing is abandoned after ten times this long.
    "command_timeout": 120,
}


def get_setting(key):
    from netbox.plugins.utils import get_plugin_config

    return get_plugin_config(PLUGIN_NAME, key, DEFAULTS[key])


# max_concurrency above this works but is rarely wise: see max_concurrency().
MAX_CONCURRENCY_ADVISED = 10
# ... and above this it is cut down: WinRM allows 30 shells per user by default (MaxShellsPerUser).
MAX_CONCURRENCY_CAP = 30
# Bad or high values already logged by this process: the setting is read by every job, the message is needed once.
_concurrency_logged: set[str] = set()


def _log_once(level: int, value, message: str) -> None:
    key = f"{level}:{value!r}"
    if key not in _concurrency_logged:
        _concurrency_logged.add(key)
        log.log(level, message)


def max_concurrency():
    """
    The max_concurrency setting. A value that is not a whole number of at least 1 is a configuration mistake: it is
    logged and the default is used, so imports and pushes keep working. Above MAX_CONCURRENCY_ADVISED a warning is
    logged, and above MAX_CONCURRENCY_CAP the value is cut down to it. Each message is logged once per process.
    """
    value = get_setting("max_concurrency")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        default = DEFAULTS["max_concurrency"]
        message = f"max_concurrency must be a whole number of at least 1, not {value!r}: using {default}"
        _log_once(logging.ERROR, value, message)
        return default
    if value > MAX_CONCURRENCY_CAP:
        _log_once(
            logging.WARNING,
            value,
            f"max_concurrency {value} is above {MAX_CONCURRENCY_CAP} (the default WinRM MaxShellsPerUser): using "
            f"{MAX_CONCURRENCY_CAP}",
        )
        return MAX_CONCURRENCY_CAP
    if value > MAX_CONCURRENCY_ADVISED:
        _log_once(
            logging.WARNING,
            value,
            f"max_concurrency {value} is above {MAX_CONCURRENCY_ADVISED}: each command is its own WinRM shell or SSH "
            "session on the server, and Windows OpenSSH allows 10 sessions per connection by default (MaxSessions)",
        )
    return value


class CredentialError(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    username: str = ""
    password: str = ""
    private_key: str = ""
    secret: str = ""

    def __repr__(self):
        # Never print secrets, for example in job logs or tracebacks.
        return f"Credentials(username={self.username!r})"


def _resolve(profile_name, profile, key, environ, needed):
    if key in profile:
        if profile[key] is None:
            if not needed:
                # A key this connection does not use (a WinRM profile with "private_key": None, or a "secret" read
                # from a variable that is only set where failover authentication is used) counts as not set, so
                # it does not stop a profile that works without it.
                return ""
            # Typically "password": os.environ.get("X") with X missing. str(None) would log on as "None": a wrong
            # password on every job (and an account lockout), or a guessable failover secret.
            raise CredentialError(
                f"credential profile {profile_name!r}: {key} is set to None (is an environment variable missing?)"
            )
        return str(profile[key])
    if f"{key}_env" in profile:
        var = profile[f"{key}_env"]
        if var not in environ:
            raise CredentialError(f"credential profile {profile_name!r}: environment variable {var} is not set")
        return environ[var]
    if f"{key}_file" in profile:
        path = Path(profile[f"{key}_file"])
        try:
            return path.read_text().strip() if key != "private_key" else path.read_text()
        except OSError as exc:
            raise CredentialError(f"credential profile {profile_name!r}: cannot read {path}: {exc.strerror}") from exc
    return ""


# Every key a credential profile may hold: each secret inline, as <key>_env or as <key>_file, plus the limits. A new
# profile key goes in LIMIT_KEYS (or SECRET_KEYS), or NetBox warns about it at startup.
SECRET_KEYS = ("username", "password", "private_key", "secret")
LIMIT_KEYS = (
    "allowed_hosts",
    "allow_insecure_credssp",
    "transports",
    "ports",
    "kerberos_realms",
    "ssh_host_keys",
    "require_verified_https",
)
PROFILE_KEYS = frozenset(LIMIT_KEYS) | {f"{key}{suffix}" for key in SECRET_KEYS for suffix in ("", "_env", "_file")}


def profiles_with_unknown_keys(profiles=None):
    """
    {profile name: [keys the plugin does not know]}. A misspelt limit ("require_verified_http") is otherwise ignored
    without a word, so the profile is less strict than its owner thinks.
    """
    profiles = get_setting("credentials") if profiles is None else profiles
    unknown = {}
    for name, profile in profiles.items():
        keys = sorted(str(key) for key in profile if key not in PROFILE_KEYS) if isinstance(profile, dict) else []
        if keys:
            unknown[name] = keys
    return unknown


def warn_unknown_profile_keys(profiles=None):
    """Log, at startup, each profile's unknown keys: names only, never a value (a misspelt key may hold a secret)."""
    # The plugin's own logger, like the other startup warnings: this module's logger exists before Django applies
    # LOGGING, and a configuration with disable_existing_loggers would silence it.
    logger = logging.getLogger(PLUGIN_NAME)
    for name, keys in profiles_with_unknown_keys(profiles).items():
        logger.warning("credential profile %r has keys the plugin does not know (ignored): %s", name, ", ".join(keys))


def get_credentials(profile_name, profiles=None, environ=None, needed=("username", "password")):
    """
    Resolve a credential profile from the plugin configuration.

    `needed` names the keys the caller is going to use: an inline None is an error for those, and reads as not set
    for the others.
    """
    profiles = get_setting("credentials") if profiles is None else profiles
    environ = os.environ if environ is None else environ
    if profile_name not in profiles:
        raise CredentialError(f"credential profile {profile_name!r} is not defined in PLUGINS_CONFIG")
    profile = profiles[profile_name]
    return Credentials(**{key: _resolve(profile_name, profile, key, environ, key in needed) for key in SECRET_KEYS})


# One DNS label. Underscores appear in some Windows host names, so they are accepted.
_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
# A last label that is a number ("127.1", "2130706433", "0x7f.1", "010.1.1.1"): the resolver reads such a name as
# an IPv4 address written the old way, so it would connect to an address the allowed_hosts check never saw as one.
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
# One part of an IPv4 pattern in allowed_hosts: digits and wildcards only.
_IPV4_PATTERN_PART = re.compile(r"[0-9*?]+")


def normalise_host(host):
    """
    Return the host in a canonical form, or None when it is not a plain host name or IP address.

    Anything else (a port, "user@", "/", "#", spaces, an IPv6 zone) is refused: the transport libraries would read
    such a value differently from the allowed_hosts check, and could connect somewhere the check never saw.
    """
    # "DHCP01.corp.example." and "dhcp01.corp.example" are the same host.
    host = host.rstrip(".").lower()
    if "%" not in host:
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            pass
    if len(host) <= 253 and _HOSTNAME.fullmatch(host) and not _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
        return host
    return None


def _is_ip(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _is_ip_pattern(pattern):
    """Whether an allowed_hosts pattern describes IP addresses ("10.20.*.*", "2001:db8::*") rather than names."""
    if ":" in pattern:
        return True
    parts = pattern.rstrip(".").split(".")
    if not all(_IPV4_PATTERN_PART.fullmatch(part) for part in parts):
        return False
    # A shorter pattern holding a number ("10.*", "192.168.*") was meant as an address range too. It matches no
    # address (an address has four parts), and classing it here stops it from allowing names such as "10.com".
    # Wildcards alone ("*", "*.*.*") stay name patterns.
    return len(parts) == 4 or any(part.isdigit() for part in parts)


def _pattern(pattern):
    # Like a TLS wildcard, * and ? stay within one label: "dhcp*.corp.example" does not match
    # "dhcp.evil.example.corp.example" through extra dots.
    pattern = pattern.rstrip(".").lower()
    with contextlib.suppress(ValueError):
        pattern = str(ipaddress.ip_address(pattern))
    return re.compile(re.escape(pattern).replace(r"\*", "[^.]*").replace(r"\?", "[^.]"))


def host_allowed(profile_name, profile, host):
    """True when the profile's credentials may be sent to `host`. A profile without `allowed_hosts` allows any."""
    patterns = profile.get("allowed_hosts")
    if patterns is None:
        return True
    if isinstance(patterns, str):
        # A bare string would otherwise be matched one character at a time.
        raise CredentialError(f"credential profile {profile_name!r}: allowed_hosts must be a list")
    host = normalise_host(host)
    if host is None:
        return False
    # An address pattern only matches addresses and a name pattern only matches names: otherwise "10.20.*.*" would
    # also allow the DNS name "10.20.evil.example", and "*" would allow every IPv6 address.
    is_ip = _is_ip(host)
    return any(_is_ip_pattern(pattern) == is_ip and _pattern(pattern).fullmatch(host) for pattern in patterns)


def check_host_allowed(profile_name, *hosts, profiles=None):
    """Raise CredentialError unless every given host (empty ones are skipped) is allowed for the profile."""
    profiles = get_setting("credentials") if profiles is None else profiles
    if profile_name not in profiles:
        raise CredentialError(f"credential profile {profile_name!r} is not defined in PLUGINS_CONFIG")
    for host in hosts:
        if host and normalise_host(host) is None:
            raise CredentialError(f"{host!r} is not a valid host name or IP address")
        if host and not host_allowed(profile_name, profiles[profile_name], host):
            raise CredentialError(f"credential profile {profile_name!r} may not be used with host {host!r}")


def insecure_credssp_allowed(profile_name, profiles=None):
    """
    Whether the profile lets CredSSP run without HTTPS and certificate verification. Over plain HTTP, or without
    verification, CredSSP's inner NTLM does not prove who the server is, and CredSSP then hands the password to
    whoever answered. An unknown profile allows nothing (the connection reports it).
    """
    profiles = get_setting("credentials") if profiles is None else profiles
    value = (profiles.get(profile_name) or {}).get("allow_insecure_credssp", False)
    if not isinstance(value, bool):
        # "yes" or 1 would read as allowed; make the administrator say exactly what they mean.
        raise CredentialError(f"credential profile {profile_name!r}: allow_insecure_credssp must be true or false")
    return value


def profiles_without_allowed_hosts(profiles=None):
    profiles = get_setting("credentials") if profiles is None else profiles
    return sorted(name for name, profile in profiles.items() if "allowed_hosts" not in profile)


# Transport names a profile's "transports" may list: the values of choices.TransportChoices (not imported here, so
# this module loads without Django).
TRANSPORTS = ("winrm", "ssh")


def _profile_list(profile_name, profile, key, check):
    """
    The values of a list-valued profile key, each passed through `check` (which returns the value to compare, or
    None when it is not acceptable), or None when the profile does not set the key.
    """
    if key not in profile:
        return None
    values = profile[key]
    # A bare string would otherwise be read one character at a time, and None (an environment variable that is not
    # set) would silently lift the restriction: both are refused, so the administrator says exactly what they mean.
    if not isinstance(values, list | tuple):
        raise CredentialError(f"credential profile {profile_name!r}: {key} must be a list")
    out = []
    for value in values:
        checked = check(value)
        if checked is None:
            raise CredentialError(f"credential profile {profile_name!r}: {key} holds an invalid entry {value!r}")
        out.append(checked)
    return out


def _check_transport(value):
    return value if isinstance(value, str) and value in TRANSPORTS else None


def _check_port(value):
    # bool is an int in Python: True would read as port 1.
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535 else None


def _check_realm(value):
    # Realms are compared without case, as Windows does (AD realms are the DNS domain in capitals).
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


# Host key types the SSH transport (paramiko) can load. Other well-formed types (ssh-dss, sk-*, certificates) would
# only fail when connecting, for every server of the profile.
SSH_HOST_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521")


def ssh_host_key_blob(text):
    """
    The key material of an SSH host key written as the server's SSH host key field takes it (authorized_keys format,
    "<type> <base64> [comment]", optionally after a known_hosts host pattern), or None when it does not parse. Two
    spellings of one key (another comment, other spacing) give the same bytes.
    """
    if not isinstance(text, str):
        return None
    fields = text.strip().split()
    # The same reading as the SSH transport: an optional leading host pattern, as in a known_hosts line.
    if len(fields) >= 3 and not fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
        fields = fields[1:]
    if len(fields) < 2:
        return None
    key_type, b64 = fields[0], fields[1]
    if key_type not in SSH_HOST_KEY_TYPES:
        return None
    try:
        blob = base64.b64decode(b64, validate=True)
        # The key itself starts with its own type name: a key pasted under another type's label is not this key.
        (length,) = struct.unpack(">I", blob[:4])
    except (binascii.Error, ValueError, struct.error):
        return None
    if blob[4 : 4 + length] != key_type.encode("ascii", "replace"):
        return None
    return blob


def _check_host_key(value):
    return value.strip() if ssh_host_key_blob(value) is not None else None


def profile_transports(profile_name, profile):
    return _profile_list(profile_name, profile, "transports", _check_transport)


def profile_ports(profile_name, profile):
    return _profile_list(profile_name, profile, "ports", _check_port)


def profile_kerberos_realms(profile_name, profile):
    return _profile_list(profile_name, profile, "kerberos_realms", _check_realm)


def profile_ssh_host_keys(profile_name, profile):
    """The profile's SSH host keys (each checked, as given), or None when it does not limit them."""
    return _profile_list(profile_name, profile, "ssh_host_keys", _check_host_key)


def profile_require_verified_https(profile_name, profile):
    """Whether the profile makes WinRM use HTTPS with certificate verification (issue #147). Absent is False."""
    value = profile.get("require_verified_https", False)
    if not isinstance(value, bool):
        # "yes", 1 or None (an environment variable that is not set) would otherwise read one way or the other; make
        # the administrator say exactly what they mean.
        raise CredentialError(f"credential profile {profile_name!r}: require_verified_https must be true or false")
    return value


def connection_pin_errors(
    profile_name, profile, *, transport, port, winrm_auth, kerberos_realm, ssh_host_key, https, verify_tls
):
    """
    What a server's connection settings break among the profile's "transports", "ports", "kerberos_realms",
    "ssh_host_keys" and "require_verified_https", as {server field: message}; empty when nothing. Raises
    CredentialError for a malformed profile.

    These keys let the profile's owner decide what the people who may edit a server record otherwise decide: how the
    credentials travel (transport, port, WinRM over verified HTTPS), which realm's KDC receives the password
    (Kerberos), and which machine SSH trusts to receive it (host key).
    """
    errors = {}
    require_verified = profile_require_verified_https(profile_name, profile)
    # NTLM and Kerberos do not need HTTPS to log on, but without it (or without checking the certificate) only the
    # logon vouches for the server, and an editor-chosen Kerberos SPN or a relayed NTLM exchange can defeat that.
    if require_verified and transport == "winrm":
        if not https:
            errors["winrm_https"] = (
                f"credential profile {profile_name!r} sets require_verified_https: WinRM must use HTTPS, not HTTP"
            )
        elif not verify_tls:
            errors["verify_tls"] = (
                f"credential profile {profile_name!r} sets require_verified_https: WinRM must verify the server's "
                "certificate"
            )
    transports = profile_transports(profile_name, profile)
    if transports is not None and transport not in transports:
        errors["transport"] = f"credential profile {profile_name!r} may not be used over {transport}"
    ports = profile_ports(profile_name, profile)
    if ports is not None and port is not None and port not in ports:
        errors["port"] = f"credential profile {profile_name!r} may not be used with port {port}"
    realms = profile_kerberos_realms(profile_name, profile)
    # Only WinRM with Kerberos uses the server's realm (CredSSP failover writes then borrow it for the user name).
    realm = (kerberos_realm or "").strip().upper()
    # No realm on the server (only possible for a record written straight to the database: the form requires one)
    # would leave the realm to the worker's krb5.conf or a realm in the profile's user name, which the list does not
    # cover, so it is refused too.
    if realms is not None and transport == "winrm" and winrm_auth == "kerberos" and realm not in realms:
        errors["kerberos_realm"] = (
            f"credential profile {profile_name!r} may not be used with Kerberos realm {realm or '(none)'}"
        )
    keys = profile_ssh_host_keys(profile_name, profile)
    if require_verified and transport == "ssh" and keys is None:
        # Otherwise an editor could switch the server to SSH and paste the host key of a machine they control: the
        # password would go to it, the very interception this key closes for WinRM. The profile's ssh_host_keys are
        # SSH's equivalent of the certificate authorities the worker trusts: chosen by the profile's owner.
        # setdefault: when "transports" already refuses SSH, that plainer message stays.
        errors.setdefault(
            "transport",
            f"credential profile {profile_name!r} sets require_verified_https, which needs ssh_host_keys for SSH "
            '(or transports: ["winrm"])',
        )
    if keys is not None and transport == "ssh":
        allowed = {ssh_host_key_blob(key) for key in keys}
        if not keys:
            errors["ssh_host_key"] = f"credential profile {profile_name!r} trusts no SSH host key"
        elif ssh_host_key and ssh_host_key_blob(ssh_host_key) is None:
            errors["ssh_host_key"] = (
                f"not a valid SSH host key for credential profile {profile_name!r} (want one of "
                f"{', '.join(SSH_HOST_KEY_TYPES)})"
            )
        elif ssh_host_key and ssh_host_key_blob(ssh_host_key) not in allowed:
            errors["ssh_host_key"] = f"credential profile {profile_name!r} does not trust this SSH host key"
    return errors


def server_kerberos_realm(server):
    """A server's realm as the connection uses it and the profile's kerberos_realms check reads it."""
    return (server.kerberos_realm or "").strip().upper()


def check_secret_connection(profile_name, server, profiles=None):
    """
    Raise CredentialError unless `server`'s own connection may carry the failover shared secret of `profile_name`
    (issue #103). The secret travels over it, so the secret profile's transports, ports, Kerberos realms, SSH host
    keys and require_verified_https (issue #147) apply to that connection, and so does its say on insecure CredSSP.
    Partners stay allowed_hosts only. Checked when a relationship is saved and before each push sends the secret.
    """
    profiles = get_setting("credentials") if profiles is None else profiles
    if profile_name not in profiles:
        raise CredentialError(f"credential profile {profile_name!r} is not defined in PLUGINS_CONFIG")
    profile = profiles[profile_name]
    errors = connection_pin_errors(
        profile_name,
        profile,
        transport=server.transport,
        port=server.effective_port,
        winrm_auth=server.winrm_auth,
        kerberos_realm=server_kerberos_realm(server),
        ssh_host_key=server.ssh_host_key,
        https=server.winrm_https,
        verify_tls=server.verify_tls,
    )
    if errors:
        raise CredentialError("; ".join(errors.values()))
    if profile_name == server.credential_profile:
        return  # the connection checks already apply this profile's keys and CredSSP consent
    if (
        server.transport == "ssh"
        and profile_ssh_host_keys(profile_name, profile) is not None
        and not (server.ssh_host_key or "").strip()
    ):
        # Without its own key the connection trusts the server profile's keys (or known_hosts), not the secret
        # profile's, so only a pinned key (checked against the secret's list above) shows which machine answers.
        raise CredentialError(f"pin the server's SSH host key to send credential profile {profile_name!r} over SSH")
    credssp = "credssp" in (server.winrm_auth, server.failover_auth)
    if (
        server.transport == "winrm"
        and credssp
        and not (server.winrm_https and server.verify_tls)
        and not insecure_credssp_allowed(profile_name, profiles)
    ):
        # The server's profile allowed CredSSP without verified HTTPS; the secret's owner has not.
        raise CredentialError(
            f"credential profile {profile_name!r} does not set allow_insecure_credssp, and this server uses CredSSP "
            "without HTTPS and certificate verification"
        )
