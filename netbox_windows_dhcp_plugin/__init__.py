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
        description = "Sync netbox-plugin-dhcp data with Microsoft Windows DHCP servers"
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
            from .integrations import branching

            branching.register()

    config = WindowsDHCPConfig
