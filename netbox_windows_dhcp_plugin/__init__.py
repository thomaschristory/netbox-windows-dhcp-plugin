from .version import __version__

try:
    from netbox.plugins import PluginConfig
except ImportError:  # imported outside NetBox (pure unit tests, build tools)
    PluginConfig = None

if PluginConfig is not None:
    from .conf import DEFAULTS

    class WindowsDHCPConfig(PluginConfig):
        name = "netbox_windows_dhcp_plugin"
        verbose_name = "Windows DHCP"
        description = "Manage Microsoft Windows DHCP servers from NetBox, kept in sync in both directions"
        version = __version__
        author = "Thomas Christory"
        author_email = "9317624+thomaschristory@users.noreply.github.com"
        base_url = "windows-dhcp"
        min_version = "4.7.0"
        max_version = "4.7.99"
        required_settings = []
        default_settings = DEFAULTS

        def ready(self):
            super().ready()
            import logging

            from . import (
                jobs,  # noqa: F401 - registers the import scheduler system job
                signals,
            )
            from .conf import profiles_without_allowed_hosts
            from .integrations import branching

            branching.register()
            signals.connect()
            unrestricted = profiles_without_allowed_hosts()
            if unrestricted:
                logging.getLogger(__name__).warning(
                    "credential profiles without allowed_hosts can be sent to any host: %s", ", ".join(unrestricted)
                )

    config = WindowsDHCPConfig
