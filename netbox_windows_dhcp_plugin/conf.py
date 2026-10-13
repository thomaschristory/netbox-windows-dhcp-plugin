import os
from dataclasses import dataclass
from pathlib import Path

PLUGIN_NAME = "netbox_windows_dhcp_plugin"

DEFAULTS = {
    # Credential profiles, referenced by name from WindowsServer.credential_profile and
    # FailoverRelationship.shared_secret_profile. Each value is a dict; every secret can be given inline, from an
    # environment variable (<key>_env) or from a file (<key>_file):
    #   {"dhcp-admin": {"username": "CORP\\svc-dhcp", "password_env": "DHCP_ADMIN_PASSWORD"}}
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
    # Maximum number of PowerShell commands run at the same time against one server.
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
