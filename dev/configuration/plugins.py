# Mounted over /etc/netbox/config/plugins.py in the netbox-docker image. Development only.
import os

from netbox_branching.utilities import DynamicSchemaDict

# Lab servers often only have the HTTP listener; CredSSP (failover) needs allow_insecure_credssp there. Never in
# production.
_lab_profile = {
    "username_env": "WINDOWS_DHCP_USERNAME",
    "password_env": "WINDOWS_DHCP_PASSWORD",
    "allow_insecure_credssp": True,
}
# SSH key logon (WINDOWS_DHCP_LAB_SSH_KEY_FILE, absolute or relative to the repository; the gitignored
# dev/env/lab.compose.yml mounts an absolute one at the same path): the key alone, without a password, as a key-only
# deployment would run. make lab-e2e picks this profile over SSH when the key file is set.
_lab_key_profile = {
    "username_env": "WINDOWS_DHCP_USERNAME",
    "private_key_file": os.path.join("/plugin", os.environ.get("WINDOWS_DHCP_LAB_SSH_KEY_FILE", "missing-key")),
}
# A production profile should always carry "allowed_hosts", so its credentials can only be sent to the DHCP servers
# (see docs/configuration.md). Here it is opt-in: set WINDOWS_DHCP_DEV_ALLOWED_HOSTS in dev/env/lab.env to a comma
# separated list of host names, addresses or patterns ("10.20.0.*,dhcp*.example.com") when the stack holds real
# lab credentials. It is not on by default because the NetBox tests (make test-netbox) create servers with made-up
# host names under this same profile name, and they would all be refused.
_lab_hosts = [h.strip() for h in os.environ.get("WINDOWS_DHCP_DEV_ALLOWED_HOSTS", "").split(",") if h.strip()]
if _lab_hosts:
    _lab_profile["allowed_hosts"] = _lab_key_profile["allowed_hosts"] = _lab_hosts

# netbox-branching must come last.
PLUGINS = ["netbox_windows_dhcp_plugin", "netbox_change_control", "netbox_branching"]
PLUGINS_CONFIG = {
    "netbox_windows_dhcp_plugin": {
        # Set WINDOWS_DHCP_USERNAME and WINDOWS_DHCP_PASSWORD in dev/env/lab.env to test against a lab server.
        "credentials": {
            "lab": _lab_profile,
            "lab-ssh-key": _lab_key_profile,
            # The failover phase of make lab-e2e (WINDOWS_DHCP_LAB_PARTNER set).
            "lab-failover": {"secret_env": "WINDOWS_DHCP_LAB_FAILOVER_SECRET"},
        },
    },
}

# netbox-branching needs a schema-aware DATABASES mapping and its router (values from env/postgres.env).
DATABASES = DynamicSchemaDict(
    {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netbox",
            "USER": "netbox",
            "PASSWORD": "netbox",
            "HOST": "postgres",
            "PORT": "",
            "CONN_MAX_AGE": 300,
            # NETBOX_TEST_DB lets several test runs work side by side, each in its own database.
            "TEST": {"NAME": os.environ.get("NETBOX_TEST_DB", "test_netbox")},
        }
    }
)
DATABASE_ROUTERS = ["netbox_branching.database.BranchAwareRouter"]
