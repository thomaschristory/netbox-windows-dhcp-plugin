# DHCPv6

Windows keeps DHCPv6 apart from IPv4: its own scopes, reservations, classes and option values, with their own
cmdlets. The plugin syncs the core of it.

## What is synced

| Windows | NetBox |
|---|---|
| DHCPv6 scope | sys4 subnet with a /64 prefix (no pool). Preferred and valid lifetimes, renew timer (T1) and rebind timer (T2) on the subnet; state and preference in scope settings |
| DHCPv6 reservation | sys4 host reservation: DUID, the reserved IPv6 address, and the IAID in reservation settings |
| DHCPv6 user class | User and vendor class with address family IPv6 |
| DHCPv6 option values (server, scope, reservation) | sys4 options using sys4's standard DHCPv6 definitions, optionally for a user class through an option qualifier |

Keys of DHCPv6 objects start with `v6/` (for example `v6/2001:db8:1::` for a scope), so they never meet an IPv4 key.
Addresses are compared in one spelling (lowercase, compressed), including inside option values.

## What is read but left alone

These are noted in each import's sync run and never touched by a push. They count as children of their scope or
reservation, so a scope holding any of them is never deleted by a push.

- DHCPv6 vendor classes and vendor class option values: writing a vendor class needs an enterprise number the
  plugin does not keep.
- Values of option codes sys4 has no standard DHCPv6 definition for. DHCPv6 option definitions are not synced.
- DHCPv6 exclusion ranges.
- Values with leading or trailing spaces.

NetBox objects of the same kinds (DHCPv6 vendor classes, values of other definitions) are left out of pushes the same
way.

## Rules

- **Scopes are /64.** A NetBox subnet with another prefix length is not pushed.
- **Lifetimes.** Left empty in NetBox, they keep the Windows values; a new scope gets Windows' defaults: preferred
  8 days and valid 12 days. Renewal (T1) and rebinding (T2) times left empty follow the preferred lifetime: a new
  scope gets half and 80 % of it (4 days and 6.4 days, that is 6 days 9:36, for the default 8 days, which is what
  Windows gives a scope made without them), and an existing scope keeps its Windows timers unless they no longer fit
  below the preferred lifetime (after you shorten it in NetBox), in which case they are worked out the same way.
  When you set only one of T1 and T2, the empty one is placed on the right side of it: T1 at 5/8 of T2, or T2
  halfway between T1 and the preferred lifetime, when the default would not fit.
  Windows expects T1 < T2 < preferred lifetime. A scope without a name is named after its prefix.
- **Reservations** need a DUID and an IAID. If a reservation has several IPv6 addresses, the first one (by NetBox ID)
  is the reserved one. Changing the address creates a new reservation and removes the old one, since the address is
  part of its key.
- **No policies.** Windows has none for DHCPv6: a DHCPv6 option value cannot have a policy qualifier.
- **No failover.** Windows has none for DHCPv6, so DHCPv6 changes are never replicated.
- **Deletes are never forced.** A scope with leases fails with DHCP 20007 and is removed by a later push.

The [Leases](leases.md) tab works for DHCPv6 scopes too, and shows the DUID and IAID of each lease.
