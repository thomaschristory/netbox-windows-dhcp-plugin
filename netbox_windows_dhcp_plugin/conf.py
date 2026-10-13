import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

PLUGIN_NAME = "netbox_windows_dhcp_plugin"

DEFAULTS = {
    # Credential profiles, referenced by name from WindowsServer.credential_profile and
    # FailoverRelationship.shared_secret_profile. Each value is a dict; every secret can be given inline, from an
    # environment variable (<key>_env) or from a file (<key>_file):
    #   {"dhcp-admin": {"username": "CORP\\svc-dhcp", "password_env": "DHCP_ADMIN_PASSWORD"}}
    # Add "allowed_hosts" (a list of host names or IPs, * and ? wildcards allowed) to limit which hosts a profile's
    # credentials may be sent to. Without it, anyone allowed to edit a Windows server can point it at a machine
    # they control and capture the credentials.
    "credentials": {},
    # Name given to imported netbox-plugin-dhcp objects, whose names must be unique across all servers.
    "subnet_name_template": "{server}/{scope_id}",
    "reservation_name_template": "{server}/{scope_id}/{ip_address}",
    # Prefix of the branches created by imports when netbox-branching is installed.
    "import_branch_prefix": "dhcp-import",
    # Open a change request for each import branch when netbox-change-control is installed.
    "import_change_request": True,
    # Seconds to wait after a save before pushing, so a burst of edits becomes one push (without branching).
    "push_debounce": 10,
    # Maximum number of PowerShell commands one job runs at the same time against a server.
    "max_concurrency": 4,
    # Seconds before a single PowerShell command is abandoned.
    "command_timeout": 120,
}


def get_setting(key):
    from netbox.plugins.utils import get_plugin_config

    return get_plugin_config(PLUGIN_NAME, key, DEFAULTS[key])


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


def _resolve(profile_name, profile, key, environ):
    if key in profile:
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


def get_credentials(profile_name, profiles=None, environ=None):
    """Resolve a credential profile from the plugin configuration."""
    profiles = get_setting("credentials") if profiles is None else profiles
    environ = os.environ if environ is None else environ
    if profile_name not in profiles:
        raise CredentialError(f"credential profile {profile_name!r} is not defined in PLUGINS_CONFIG")
    profile = profiles[profile_name]
    return Credentials(
        **{
            key: _resolve(profile_name, profile, key, environ)
            for key in ("username", "password", "private_key", "secret")
        }
    )


def _normalise_host(host):
    # "DHCP01.corp.example." and "dhcp01.corp.example" are the same host.
    return host.strip().rstrip(".").lower()


def host_allowed(profile_name, profile, host):
    """True when the profile's credentials may be sent to `host`. A profile without `allowed_hosts` allows any."""
    patterns = profile.get("allowed_hosts")
    if patterns is None:
        return True
    if isinstance(patterns, str):
        # A bare string would otherwise be matched one character at a time.
        raise CredentialError(f"credential profile {profile_name!r}: allowed_hosts must be a list")
    host = _normalise_host(host)
    return any(fnmatch.fnmatchcase(host, _normalise_host(pattern)) for pattern in patterns)


def check_host_allowed(profile_name, *hosts, profiles=None):
    """Raise CredentialError unless every given host (empty ones are skipped) is allowed for the profile."""
    profiles = get_setting("credentials") if profiles is None else profiles
    if profile_name not in profiles:
        raise CredentialError(f"credential profile {profile_name!r} is not defined in PLUGINS_CONFIG")
    for host in hosts:
        if host and not host_allowed(profile_name, profiles[profile_name], host):
            raise CredentialError(f"credential profile {profile_name!r} may not be used with host {host!r}")


def profiles_without_allowed_hosts(profiles=None):
    profiles = get_setting("credentials") if profiles is None else profiles
    return sorted(name for name, profile in profiles.items() if "allowed_hosts" not in profile)
