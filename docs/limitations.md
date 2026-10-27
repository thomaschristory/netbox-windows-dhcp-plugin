# Limitations

## Checked on a real server

Every feature is tested against an in-memory fake Windows server and against the real PowerShell scripts run with
stub cmdlets. The lab tests (see [Development](development.md#lab-tests)) have also run in full against two Windows
Server 2022 DHCP servers in a workgroup: every cmdlet the plugin uses, a full import, push of creates, updates and
deletes, the Leases tab, and a failover relationship created, replicated and removed through a push (see
[Failover](failover.md#connecting-to-the-servers)). Testers of the release candidates ran Windows Server 2025. What
they found is fixed. The walkthroughs in [Reservations](reservations.md) and
[Failover](failover.md#a-working-failover-step-by-step) were run on two Windows Server 2025 servers (2026-10-27):
reservations created, changed, given an option and deleted, and a load balance relationship created and replicated
through pushes over WinRM with CredSSP. The lab test suite itself has not been run on Windows Server 2025.

Connections checked on a real server: WinRM over HTTP with NTLM and CredSSP, WinRM over HTTPS with certificate
verification and NTLM, CredSSP or Basic, and SSH with a password (failover included over CredSSP and SSH).

Not seen on a real server yet:

- **Kerberos**: the lab is a workgroup, without a domain;
- **SSH key authentication**;
- running the cmdlets on another server through **`computer_name`** (`-ComputerName`), and the fully qualified name
  a domain member reports for itself.

Over SSH, Windows OpenSSH stalls on more than about 70 KB of script input, so the plugin refuses a script above
64 KB with an error that says to use WinRM. Its scripts send under 10 KB; only very large parameter lists come close.

Try the plugin on a lab server before a production one, and use **Dry run** before turning **Sync enabled** on.

## Scope of v1

- **DHCP only.** DNS records, DHCP server roles and features, and server-wide settings beyond those listed in
  [How objects map](mapping.md) are out of scope. The DHCP Server role must already be installed.
- **No authorization in Active Directory.** The plugin does not authorize DHCP servers.
- **Leases are read only and not stored.** No lease history, no IP address updates from leases, at most 5000 leases
  shown per read. Windows reports no state for DHCPv6 leases, so their state shows as n/a.
- **DHCPv6** is limited to scopes, reservations, user classes and values of sys4's standard DHCPv6 definitions. DHCPv6
  vendor classes, custom option definitions and exclusion ranges are left alone. See [DHCPv6](dhcpv6.md).
- **Failover** is IPv4 only, as on Windows.

## Windows rules the plugin will not work around

Some changes are only possible on Windows by removing an object with everything in it. The plugin refuses them
rather than delete data: a scope's subnet mask, an option definition's type or array flag, removing a policy
condition, a failover relationship's partner or authentication. A dry run does not catch these: the push fails on
them. Make them on Windows by hand, then import. See [Push](push.md#refused-while-pushing).

Windows also refuses some policies outright: a condition value with a wildcard in the middle (`host*.example.com`;
put it at the start or the end), and IP ranges or ordinary option values on a policy with an FQDN condition (errors
DHCP 20135 and 20136). A push of such a policy fails the same way, so fix it in NetBox.

## netbox-plugin-dhcp

- netbox-plugin-dhcp is beta, so the plugin pins `>=0.2,<0.3` and follows its releases.
- Its names are unique across servers, hence the `<server>/<scope id>` naming of imported objects.
- It stores option values as one comma-separated string: commas inside a value are escaped as `\,`, which sys4 only
  accepts in string options, and values with leading or trailing spaces cannot be stored (they are skipped).
- A shared network needs a covering prefix; superscopes wider than a /8 are refused.
- Deleting a shared network in NetBox deletes its subnets, and so, on the next push, their scopes on Windows.

## NetBox and branching

- NetBox 4.7 only (the plugin declares `max_version = 4.7.99`).
- netbox-branching creates merged objects with a raw save, so sys4's own `save()` side effects are not replayed on
  merge. Imports avoid depending on them.
- An empty merge sends no signal, so it queues no push.
- Edits made directly in main push without approval, even with change control.

## Operations

- The worker's database connection must not go through pgbouncer in transaction mode (advisory locks).
- Reading a large server takes one PowerShell call per scope and per reservation list; a push or an import of a
  server with thousands of scopes takes minutes.
