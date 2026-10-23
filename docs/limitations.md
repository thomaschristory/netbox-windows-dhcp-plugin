# Limitations

## Not yet checked on a real server

The plugin is pre-release. Every feature is tested against an in-memory fake Windows server and against the real
PowerShell scripts run with stub cmdlets. A first, partial lab run against Windows Server 2025 checked superscope
creation and partial removal, MAC filter removal, option values on reservations (IPv4 and DHCPv6) and on policies, and
the DHCPv6 lease properties; what it found is fixed. Superscope rename failed in that run: its corrected parameters
were read from `Get-Command` on the server, but the fixed rename has not run there yet. Some cmdlets and property
names have still not been run against a real Windows DHCP server. They are marked `# unverified:` in the code. The
open questions include:

- whether option values of every user and vendor class are read at each level;
- where Windows returns the class data (IPv4 and DHCPv6), and how it reports an unlimited lease time;
- the property names returned for scope settings (offer delay, BOOTP limit, activate policies, superscope), policies,
  failover relationships, DHCPv6 scopes, IPv4 leases and exclusion ranges;
- DHCPv6 reservation removal and class updates;
- renaming a superscope, removing a whole superscope (its scopes should stay), and vendor option definitions;
- how Windows renumbers policies: that removing one closes the gap, and that changing a processing order moves the
  others down and refuses a number past the end the way creating one does (seen only for creates);
- whether a MAC prefix condition written in uppercase reads back in lowercase;
- what renewal (T1) and rebinding (T2) times Windows gives a DHCPv6 scope created with a short preferred lifetime and
  no timers, and whether it refuses timers past the preferred lifetime (a push never sends those: see
  [DHCPv6](dhcpv6.md#rules));
- failover replication after a push, and removing a scope that is in a relationship, which need two servers;
- whether changing the client ID, type, DUID or IAID of an existing reservation makes Windows recreate it and drop
  its option values (#42). Either way the push puts back any reservation option value an update removed.

The lab tests that settle them are written (see [Development](development.md#lab-tests)). A first partial run against
Windows Server 2025 found and fixed a few defects (the DHCPv6 rebinding time default, how policy conditions and policy
order compare), but the questions above stay open until a full run. Until then, try the plugin on a lab server before
a production one, and use **Dry run** before turning **Sync enabled** on.

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
