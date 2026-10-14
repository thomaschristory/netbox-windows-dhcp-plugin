# Permissions

Every model has the usual NetBox `view`, `add`, `change` and `delete` permissions, under the
`netbox_windows_dhcp_plugin` app, for example `netbox_windows_dhcp_plugin.change_windowsserver`. sys4 objects
(subnets, reservations, options) keep netbox-plugin-dhcp's own permissions (`netbox_dhcp.*`).

Actions that connect to Windows need more than viewing:

| Action | Needs |
|---|---|
| Test connection, Sync now, Dry run | `change` on the Windows server |
| Import now | `change` on the Windows server; with branching also `netbox_branching.add_branch`, and `netbox_change_control.add_changerequest` when change control opens a change request |
| See the Leases tab | `view` on the subnet and on its Windows server |
| Load leases | `view` and `change` on the Windows server |
| Apply or dismiss import proposal items | `change` on the items; for **Apply all** and **Dismiss all**, `change` on the proposal as well. To apply, also the permission for each NetBox object the item writes |
| See sync runs, import proposals | `view` on them |

Object permissions work as usual: a permission limited by constraints (for example to some Windows servers) only
allows the action on the matching objects. In the REST API, a server the user may not change is reported as not
found.

## Applying proposal items

Applying an item writes NetBox objects as the reviewing user, so the change log shows who accepted it. Besides
`change` on the item, the user needs:

- `add`, `change` or `delete` (for a create, update or delete item) on the item's main object, for example
  `netbox_dhcp.add_subnet` or `netbox_windows_dhcp_plugin.change_windowspolicy`. Items without it stay pending and
  the missing permission is reported;
- the permissions for everything else the item writes, for example `ipam.add_prefix`, `ipam.add_iprange`,
  `ipam.add_ipaddress` and `dcim.add_macaddress` for a new scope or reservation. An item that breaks one of these is
  rolled back and marked failed.

## Suggested roles

| Role | Permissions |
|---|---|
| DHCP viewer | `view` on the plugin's models and sys4's models |
| DHCP operator | viewer, plus `change` on Windows servers (actions and leases) |
| DHCP editor | operator, plus `add`, `change`, `delete` on sys4 and the plugin's DHCP models, and on prefixes, IP ranges, IP addresses and MAC addresses |
| Import user (`import_user`) | `view` and `change` on Windows servers; with branching `netbox_branching.add_branch` (and `netbox_change_control.add_changerequest`) |

With branching, NetBox's own branching and change control permissions govern who may merge, and so who triggers a
push.
