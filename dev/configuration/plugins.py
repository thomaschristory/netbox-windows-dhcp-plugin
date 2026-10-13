# Mounted over /etc/netbox/config/plugins.py in the netbox-docker image. Development only.
from netbox_branching.utilities import DynamicSchemaDict

# netbox-branching must come last.
PLUGINS = ["netbox_dhcp", "netbox_windows_dhcp_plugin", "netbox_change_control", "netbox_branching"]
PLUGINS_CONFIG = {
    "netbox_windows_dhcp_plugin": {
        # Set WINDOWS_DHCP_USERNAME and WINDOWS_DHCP_PASSWORD in dev/env/netbox.env to test against a lab server.
        "credentials": {
            "lab": {"username_env": "WINDOWS_DHCP_USERNAME", "password_env": "WINDOWS_DHCP_PASSWORD"},
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
        }
    }
)
DATABASE_ROUTERS = ["netbox_branching.database.BranchAwareRouter"]
