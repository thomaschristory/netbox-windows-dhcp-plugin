# Mounted over /etc/netbox/config/plugins.py in the netbox-docker image. Development only.
import os

from netbox_branching.utilities import DynamicSchemaDict

# netbox-branching must come last.
PLUGINS = ["netbox_windows_dhcp_plugin", "netbox_change_control", "netbox_branching"]
PLUGINS_CONFIG = {
    "netbox_windows_dhcp_plugin": {
        # Set WINDOWS_DHCP_USERNAME and WINDOWS_DHCP_PASSWORD in dev/env/lab.env to test against a lab server.
        "credentials": {
            # Lab servers often only have the HTTP listener; CredSSP (failover) needs this there. Never in production.
            "lab": {
                "username_env": "WINDOWS_DHCP_USERNAME",
                "password_env": "WINDOWS_DHCP_PASSWORD",
                "allow_insecure_credssp": True,
            },
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
