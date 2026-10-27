# How objects map

The plugin has one model for each Windows DHCP concept, under the **Windows DHCP** menu. Every DHCP object belongs
to a [Windows server](servers.md), directly or through its scope, and keeps the name it has on Windows. Scopes and
reservations point at core NetBox objects (prefix, IP range, IP address, MAC address), so DHCP addresses show up in
IPAM like any other.

## Summary

| On Windows | Plugin model | Core NetBox objects it points at |
|---|---|---|
| DHCP server | **Windows server** (name, connection settings, optional device or virtual machine) | |
| IPv4 scope | **Scope** | **Prefix** (scope ID and mask), **IP Range** (start and end) |
| DHCPv6 scope | **Scope** | **Prefix** (a /64) |
| Superscope | **Superscope** | |
| Exclusion range | **Exclusion range** (on a scope) | |
| IPv4 reservation | **Reservation** | **IP Address**, **MAC Address** (or a client ID) |
| DHCPv6 reservation | **Reservation** (DUID and IAID) | **IP Address** |
| User or vendor class | **Class** | |
| Option definition (standard or of a vendor class) | **Option definition** | |
| Option value (server, scope or reservation; for a user class, vendor class or policy) | **Option value** | |
| Policy | **Policy** | |
| Failover relationship | **Failover relationship** | |
| MAC filter | **MAC filter** | |
| Allow and deny list switches | **MAC filter lists** | |
| Lease | not stored | read live on the scope's [Leases tab](leases.md) |

An import creates the core objects, or reuses the ones it finds, in the Windows server's **import VRF** (global when
empty): the **Prefix** of each scope, the **IP Range** of each IPv4 scope, the **IP Address** of each reservation
and the **MAC Address** of each IPv4 reservation identified by one. Failover partners share one IP range per scope
this way. A prefix, IP range, IP address or MAC address that a scope or reservation points at cannot be deleted
while it does.

## Names

Objects keep their Windows names. A scope's or a reservation's **name** is its Windows name, and its
**description** is the Windows description. Two Windows servers can both have a scope called "LAN": names only need
to be unique within one server where Windows wants that too (classes, policies, superscopes, failover
relationships, compared without case as on Windows). In lists, a scope with a prefix shows as
`<server>/<scope id>`, for example `dhcp01/10.1.0.0`, so scopes of different servers are easy to tell apart.

## Scopes (IPv4)

| Windows | Scope field |
|---|---|
| Scope ID and subnet mask | Prefix |
| Start and end of the range | IP range (inside the prefix) |
| Name, description | Name, description |
| Lease duration | Lease duration (seconds). Empty keeps the Windows value |
| State (Active, Inactive) | State |
| Type (DHCP, BOOTP, Both) | Type |
| Delay, max BOOTP clients, activate policies | Delay (ms), max BOOTP clients, activate policies. "No BOOTP limit" (4294967295 on Windows) is empty in NetBox |
| Superscope | Superscope |
| Option 3, 6, 15 ... | Option values on the scope |

A server has one scope per prefix. A new scope defaults to active, type DHCP, no delay, policies on.

Options 1 (subnet mask) and 51 (lease time) are never option values in NetBox: the prefix and the lease duration
hold them. An option value with one of those codes is reported and not pushed.

A subnet mask cannot be changed in place on Windows (only by removing the scope with everything in it), so a push
refuses a changed prefix length.

## Superscopes

A **Superscope** belongs to a server and has the Windows superscope name. Member scopes name it in their
**Superscope** field. On Windows, removing a superscope keeps its scopes, and so does deleting one in NetBox: its
scopes simply leave it. Windows has no DHCPv6 superscopes.

## Exclusion ranges

An **Exclusion range** belongs to a scope, with a start and an end address inside the scope's prefix. Windows has no
way to change one, so a changed range is pushed as a remove and an add.

## Reservations (IPv4)

| Windows | Reservation field |
|---|---|
| IP address | IP address (an IPAM IP address inside the scope's prefix) |
| Client ID | MAC address (an IPAM MAC address), or **client ID** when the client is identified by something else |
| Name, description | Name, description |
| Type (DHCP, BOOTP, Both) | Reservation type (Both by default) |

A scope has one reservation per IP address. A reservation without a name gets its IP address as its name, as
Windows does. See [Reservations](reservations.md) for a walkthrough.

## Option definitions and values

An **Option definition** belongs to a server and an address family, and optionally to a vendor class. It holds the
code, name, exact Windows type (`Byte`, `Word`, `DWord`, `DWordDword`, `IPv4Address`, `String`, `BinaryData`,
`EncapsulatedData`, ...), the multi-valued flag and the default value. Windows' own definitions of the standard
options (codes RFC 2132 and its successors assign) are not imported: Windows ships them, and a push never touches
them. The type and the array flag of a definition cannot be changed in place on Windows, so a push refuses those
changes.

An **Option value** belongs to a server and an address family, and is set on the server, a scope or a reservation
(its **level**): leave scope and reservation empty for a server value, set a scope for a scope value, set a
reservation for a reservation value (its scope is filled in from the reservation). It names the option by its
**code**, so standard options need no definition in NetBox; the plugin knows their names (`3` routers, `6`
domain-name-servers, `15` domain-name, ...) and shows them next to the code. A value can be restricted, as Windows
allows, to:

- a **vendor class**, for an option that vendor class defines;
- a **user class**: only clients of that class get the value;
- a **policy**: only clients matching the policy get the value (IPv4 only; not on reservations, and not together
  with a user class).

The **value** is a list of strings, one per Windows value, for example `["10.1.0.1"]` or
`["10.1.0.53", "10.1.0.54"]`. Commas and leading or trailing spaces inside a value are kept as they are. Numbers
compare equal in hex and decimal (`0x1` and `1`); text compares exactly. A server holds one value per level, code,
class and policy.

## Classes

A **Class** holds the class name, type (user or vendor), the ASCII data the client sends, and the address family
(IPv4 or IPv6). Names compare without case, as on Windows. A vendor class's own options are option definitions and
option values with that vendor class set: Windows ships several vendor classes that define the same codes, and
their option codes 1 and 51 are ordinary vendor options, not the mask and lease time.

## Policies

A **Policy** is server level (no scope) or scope level (a scope). It holds:

- the processing order, enabled flag and description;
- the condition (AND or OR) and the **criteria**, as JSON by kind: `vendor_class`, `user_class`, `mac_address`,
  `client_id`, `relay_agent`, `circuit_id`, `remote_id`, `subscriber_id`, `fqdn`. Each is a list starting with the
  operator, for example `{"vendor_class": ["EQ", "MSFT"], "mac_address": ["EQ", "00-15-5d*"]}`;
- the lease duration (seconds; empty keeps the Windows value);
- for scope policies, the IP ranges, for example `[["10.1.0.10", "10.1.0.50"]]`.

Option values set for a policy name it in their **Policy** field, at the same level as the policy (a scope policy's
values on its scope, a server policy's values on the server).

Windows takes a wildcard only at the start or the end of a condition value: `*.example.com` and `00-15-5d*` are fine,
`host*.example.com` is refused. A policy with an FQDN condition cannot hold IP ranges or ordinary option values on
Windows (errors DHCP 20135 and 20136); use a MAC address or class condition for those.

Windows stores some criteria in its own spelling: a MAC prefix gets a separator before the wildcard (`02-4e-42-ab*`
reads back `02-4e-42-ab-*`, hex digits compared without case) and a host name (`fqdn`) is lowercased. The sync
compares criteria the way Windows stores them, so either spelling in NetBox is no change. Other criteria (classes,
client ID, relay agent fields) compare exactly.

A policy without criteria is not pushed (Windows requires at least one), nor are two policies with the same
processing order at the same level. Windows numbers the policies of a level 1, 2, 3... without gaps, so give them
consecutive processing orders: a push creates them in that order, and refuses a number past the end of the list.
After deleting a policy, renumber the ones after it: until then the push leaves that level alone (see
[Push](push.md#order)). Windows cannot clear a condition with an update, so a push refuses a removed condition;
remove it on Windows and import.

## Failover relationships

See [Failover](failover.md).

## MAC filters

A **MAC filter** is a MAC address or pattern (`00-15-5d-01-02-03`, `00-15-5d-*`) on the allow or deny list of a
server. **MAC filter lists** holds the two switches that turn the lists on. The switches are only pushed when NetBox
holds a MAC filter lists object for the server; delete it to stop managing them.

## Windows keys

Each object has a **Windows key**, its identity on the server. Keys appear in sync runs, proposal items and sync
state. Windows names are not case sensitive, so keys use lowercase.

| Kind | Example key |
|---|---|
| `class` | `user/printers`, `vendor/msft 5.0` |
| `optiondef` | `252`, `msft 5.0/1` (a vendor class definition) |
| `scope` | `10.1.0.0` |
| `superscope` | `campus` |
| `exclusion` | `10.1.0.0/10.1.0.200-10.1.0.254` |
| `reservation` | `10.1.0.0/10.1.0.20` |
| `policy` | `server/printers`, `10.1.0.0/printers` |
| `option` | `server/6`, `10.1.0.0/3`, `10.1.0.0/10.1.0.20/12`, `...;vendor=msft 5.0`, `...;user=printers`, `...;policy=printers` |
| `failover` | `ha` |
| `macfilter` | `00-15-5d-01-02-03` |
| `filterlists` | `server` |
| `class6`, `scope6`, `reservation6`, `option6` | `v6/user/lab`, `v6/2001:db8:1::`, `v6/2001:db8:1::/2001:db8:1::10`, `v6/server/23` |

When NetBox holds more than one object for one Windows object (two copies of a class that differ only in case, say),
the extra ones get a `#<id>` suffix; an import proposes deleting them, and a push refuses them.
