# Security policy

## Supported versions

While the plugin is below 1.0, security fixes go to the latest release of each line:

| Line | Branch | Gets fixes |
|---|---|---|
| 0.2.x | `main` | Yes, in the next release |
| 0.1.x (with netbox-plugin-dhcp) | `release/0.1` | Yes, as 0.1.x bug fix releases |
| Older releases and release candidates | | No: upgrade to the latest release of your line |

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting:
[Report a vulnerability](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/security/advisories/new)
(**Security > Report a vulnerability** on the repository). Only the maintainer sees the report, and the fix and an
advisory are published together.

See the [security page](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/security/) of the
documentation for how the plugin handles credentials and connections.
