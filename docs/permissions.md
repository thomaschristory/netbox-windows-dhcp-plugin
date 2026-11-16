# Permissions

Every model has the usual NetBox `view`, `add`, `change` and `delete` permissions, under the
`netbox_windows_dhcp_plugin` app, for example `netbox_windows_dhcp_plugin.change_windowsserver` or
`netbox_windows_dhcp_plugin.add_reservation`. The prefixes, IP ranges, IP addresses and MAC addresses that scopes and
reservations point at keep NetBox's own permissions (`ipam.*`, `dcim.*`).

## Editing IPAM objects that Windows uses

[Editing IPAM objects used by DHCP](ipam-edits.md) covers this in full: the rule, the messages, role recipes,
branching and the push trail. In short:

Changing some fields of those objects changes what the next push writes to Windows. Such an edit is a DHCP change,
so besides NetBox's own `change` permission on the object it needs `change` on every scope or reservation that uses
it:

| Object | Fields that reach Windows | Also needs |
|---|---|---|
| Prefix | the prefix (network and length) | `netbox_windows_dhcp_plugin.change_scope` on the scopes using it |
| IP range | start and end address | `netbox_windows_dhcp_plugin.change_scope` on the scopes using it |
| IP address | the address (not the mask) | `netbox_windows_dhcp_plugin.change_reservation` on the reservations using it |
| MAC address | the MAC address | `netbox_windows_dhcp_plugin.change_reservation` on the reservations using it |

Without it the save is refused, in the UI, bulk import and the REST API (HTTP 400) alike, with a message such as:

```text
Changing address would change reservation dhcp01/198.18.10.0/198.18.10.25 on Windows. You need the "change
reservation" permission on it.
```

Scopes and reservations the user may not view are neither named nor counted. Object permissions count, and are checked
against the values before and after the edit: a permission limited to one Windows server (for example the constraint
`{"scope__server__name": "dhcp01"}` on reservations) allows the edit only for that server's reservations, and one
limited to part of a subnet (`{"ip_address__address__net_host_contained": "198.18.10.0/25"}`) does not let an
address move out of it. Every other
field (status, VRF, tenant, description, tags, DNS name, assigned interface) never reaches Windows, needs no plugin
permission, and queues no push.

Custom scripts and background API requests are checked like any request, as the user who started them. Not checked:
saves with no user behind them (`nbshell`, management commands) and imports (they write what Windows already has).
With branching, the edit is checked when it is made in the branch, against
the scopes and reservations the branch holds, and again when the branch is merged or reverted, as the merging or
reverting user, against main: a merge that would change a scope or reservation that user may not change is refused
and nothing is merged.

Actions that connect to Windows need more than viewing:

| Action | Needs |
|---|---|
| Test connection, Sync now, Dry run | `change` on the Windows server |
| Import now, Import into branch | `change` on the Windows server; with branching also `netbox_branching.add_branch`, and `netbox_change_control.add_changerequest` when change control opens a change request |
| Import into this branch | `change` on the Windows server, `view` on the branch, and the permission for each NetBox object the import writes into it |
| Import directly (into main) | `change` on the Windows server and the permission for each NetBox object the import writes. Offered only when `import_direct` is on and, if change control's `protect_main` covers DHCP and IPAM objects, with `netbox_change_control.bypass_policy` |
| See the Leases tab | `view` on the scope and on its Windows server |
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
  `netbox_windows_dhcp_plugin.add_scope` or `netbox_windows_dhcp_plugin.change_windowspolicy`. Items without it stay pending and
  the missing permission is reported;
- the permissions for everything else the item writes, for example `ipam.add_prefix`, `ipam.add_iprange`,
  `ipam.add_ipaddress` and `dcim.add_macaddress` for a new scope or reservation. An item that breaks one of these is
  rolled back and marked failed.

**Apply all** and **Dismiss all** also need `change` on the proposal. A large review (more open items than the
`review_job_threshold` setting) runs as a background job as the same user; the job checks every one of these
permissions again when it starts (Apply all and Dismiss all also need `view` on the proposal then), so a permission
taken away while it waits is honoured. It does not check them again for each item while it runs. Opening the job
itself (its log, who asked, its error) needs `core.view_job`; without it, the proposal page and the proposal's
`review_job` in the REST API still show the job's status and counts.

## Suggested roles

| Role | Permissions |
|---|---|
| DHCP viewer | `view` on the plugin's models (and on prefixes, IP ranges, IP addresses and MAC addresses) |
| DHCP operator | viewer, plus `change` on Windows servers (actions and leases) |
| DHCP editor | operator, plus `add`, `change`, `delete` on the plugin's DHCP models, and on prefixes, IP ranges, IP addresses and MAC addresses |
| IPAM editor (no DHCP changes) | `view` and `change` on prefixes, IP ranges, IP addresses and MAC addresses, `view` on the plugin's models; edits that would change Windows are refused (see [Editing IPAM objects used by DHCP](ipam-edits.md#recipes)) |
| Import user (`import_user`) | `view` and `change` on Windows servers; with branching `netbox_branching.add_branch` (and `netbox_change_control.add_changerequest`) |

With branching, NetBox's own branching and change control permissions govern who may merge, and so who triggers a
push.
