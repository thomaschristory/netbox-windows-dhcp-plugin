# How objects map

netbox-plugin-dhcp ("sys4") is modelled on Kea, so it has no place for several Windows concepts. The plugin reuses
sys4 objects where they fit and adds its own models, linked to them, for the rest. It never changes sys4's tables.

## Summary

| On Windows | In NetBox | Plugin model for the Windows-only part |
|---|---|---|
| DHCP server | sys4 **DHCP Server** | **Windows server** (connection settings) |
| IPv4 scope | sys4 **Subnet** with one **Pool** | **Scope settings** (`WindowsScope`) |
| DHCPv6 scope | sys4 **Subnet** (a /64, no pool) | **Scope settings** (state, preference) |
| Superscope | sys4 **Shared Network** | |
| Exclusion range | | **Exclusion range** |
| IPv4 reservation | sys4 **Host Reservation** | **Reservation settings** (type) |
| DHCPv6 reservation | sys4 **Host Reservation** | **Reservation settings** (IAID) |
| Option definition | sys4 **Option Definition** (server-scoped) | **Option definition settings** (Windows type and default) |
| Option value | sys4 **Option** (on the server, a subnet or a reservation) | **Option qualifier** (user class or policy) |
| User or vendor class | | **User and vendor classes** (`WindowsClass`) |
| Vendor class option definition | | **Vendor option definition** |
| Vendor class option value | | **Vendor option value** |
| Policy | | **Policy** |
| Failover relationship | | **Failover relationship** |
| MAC filter | | **MAC filter** |
| Allow and deny list switches | | **MAC filter lists** |
| Lease | not stored | read live on the subnet's [Leases tab](leases.md) |

sys4 needs some NetBox core objects too. An import creates them, or reuses the ones it finds, in the Windows
server's **import VRF** (global when empty): the **Prefix** of each scope, the **IP Range** of its pool, the **IP
Address** of each reservation and the **MAC Address** of each IPv4 reservation.

## Names

sys4 names are unique per model across all servers, and two Windows servers can both have a scope called "LAN". So
imported objects get names built from the server and the scope:

| Object | Name | Example |
|---|---|---|
| Subnet | `subnet_name_template`, default `{server}/{scope_id}` | `dhcp01/10.1.0.0` |
| Host reservation | `reservation_name_template`, default `{server}/{scope_id}/{ip_address}` | `dhcp01/10.1.0.0/10.1.0.20` |
| Shared network | `<server>/<superscope>` | `dhcp01/Campus` |

The Windows **name** of a scope or reservation goes in the sys4 object's **description**, and the Windows
**description** goes in its **comments**. Edit those fields to rename or describe the object on Windows.

## Scopes (IPv4)

| Windows | NetBox |
|---|---|
| Scope ID and subnet mask | The subnet's prefix |
| Start and end of the range | The pool's IP range |
| Name, description | Subnet description, comments |
| Lease duration | Subnet valid lifetime (seconds). Empty keeps the Windows value |
| State (Active, Inactive) | Scope settings: state |
| Type (DHCP, BOOTP, Both) | Scope settings: type |
| Delay, max BOOTP clients, activate policies | Scope settings. "No BOOTP limit" (4294967295 on Windows) is empty in NetBox |
| Option 3, 6, 15 ... | sys4 options on the subnet |

A subnet without scope settings is pushed as active, type DHCP, no delay, policies on.

Options 1 (subnet mask) and 51 (lease time) are never options in NetBox: the prefix and the valid lifetime hold
them. A NetBox option with one of those codes is reported and not pushed.

A subnet mask cannot be changed in place on Windows (only by removing the scope with everything in it), so a push
refuses a changed prefix length.

## Superscopes

A superscope is a sys4 shared network named `<server>/<superscope>`, with the Windows name in its description.
Member scopes are its subnets. sys4 wants a prefix covering all of them, so the import uses (or creates, in the
import VRF) the smallest supernet of the member scopes, at most a /8.

On Windows, removing a superscope keeps its scopes; the import moves the subnets back under the server before
deleting the shared network. Deleting a shared network by hand in NetBox deletes its subnets (sys4 behaviour), and the
next push then removes those scopes from Windows.

## Exclusion ranges

An **Exclusion range** belongs to a subnet, with a start and an end address. Windows has no way to change one, so a
changed range is pushed as a remove and an add.

## Reservations (IPv4)

| Windows | NetBox |
|---|---|
| IP address | The reservation's IPv4 address |
| Client ID (MAC address) | The reservation's MAC address (or its client ID when it is not a MAC address) |
| Name, description | Description, comments |
| Type (DHCP, BOOTP, Both) | Reservation settings: type (Both when there are none) |

A reservation without a name gets its IP address as its name, as Windows does.

## Option definitions and values

Windows definitions of codes sys4 already defines (its standard DHCPv4 definitions) are left to sys4. Other codes
become server-scoped sys4 option definitions, with the exact Windows type (`Byte`, `Word`, `DWord`, `DWordDword`,
`IPv4Address`, `String`, `BinaryData`, `EncapsulatedData`, ...) and default value kept in **Option definition
settings**. The type and the array flag of a definition cannot be changed in place on Windows, so a push refuses
those changes.

Option values are sys4 options on the DHCP server (server level), a subnet (scope level) or a host reservation.
An **Option qualifier** restricts a value to a user class, or to a policy, as Windows allows.

sys4 stores a list of values as one comma-separated string. A comma inside one value is stored as `\,` (Kea's
escape). Numbers compare equal in hex and decimal (`0x1` and `1`); text compares exactly.

## Classes

**User and vendor classes** hold the class name, type (user or vendor), the ASCII data the client sends, and the
address family (IPv4 or IPv6). Names compare without case, as on Windows.

A vendor class's own options are **Vendor option definitions** (code, name, Windows type, multi-valued, default)
and **Vendor option values** (on the server, a scope or a reservation, optionally for a user class or a policy, with
the value kept as a list). They are separate models because sys4 allows one definition per code and server, and
Windows ships several vendor classes that define the same codes.

## Policies

A **Policy** is server level (no subnet) or scope level (a subnet). It holds:

- the processing order, enabled flag and description;
- the condition (AND or OR) and the **criteria**, as JSON by kind: `vendor_class`, `user_class`, `mac_address`,
  `client_id`, `relay_agent`, `circuit_id`, `remote_id`, `subscriber_id`, `fqdn`. Each is a list starting with the
  operator, for example `{"vendor_class": ["EQ", "MSFT"], "mac_address": ["EQ", "00-15-5d*"]}`;
- the lease duration (seconds; empty keeps the Windows value);
- for scope policies, the IP ranges, for example `[["10.1.0.10", "10.1.0.50"]]`.

Option values set for a policy carry the policy in their option qualifier (or vendor option value).

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

When NetBox holds more than one object for one Windows object (two subnets on one prefix, two copies of one option),
the extra ones get a `#<id>` suffix; an import proposes deleting them, and a push refuses them.
