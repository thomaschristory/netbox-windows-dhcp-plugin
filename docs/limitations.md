# Limitations

## Checked on a real server

Every feature is tested against an in-memory fake Windows server and against the real PowerShell scripts run with stub
cmdlets. 0.2.0 keeps the Windows side of 0.1.x (scripts, transports, sync rules; option values are now lists) and
changed the NetBox models around it. 0.2.0 passed the full lab tests (`make test-lab`, 31 tests, and `make lab-e2e`) on
two Windows Server 2025 servers over WinRM HTTP with CredSSP (2026-10-29). With 0.1.0, the lab tests (see
[Development](development.md#lab-tests)) ran in full against two Windows Server 2022 DHCP servers in a workgroup: every
cmdlet the plugin uses, a full import, push of creates, updates and deletes, the Leases tab, and a failover relationship
created, replicated and removed through a push (see [Failover](failover.md#connecting-to-the-servers)). Testers of the
release candidates ran Windows Server 2025. What they found is fixed. The walkthroughs in
[Reservations](reservations.md) and [Failover](failover.md#a-working-failover-step-by-step) were run on two Windows
Server 2025 servers (2026-10-27): reservations created, changed, given an option and deleted, and a load balance
relationship created and replicated through pushes over WinRM with CredSSP.

Connections checked on a real server: WinRM over HTTP with NTLM and CredSSP, WinRM over HTTPS with certificate
verification and NTLM, CredSSP or Basic, and SSH with a password (failover included over CredSSP and SSH).

Not seen on a real server yet:

- **Kerberos**: the lab is a workgroup, without a domain;
- **SSH key authentication**;
- running the cmdlets on another server through **`computer_name`** (`-ComputerName`, and the single CIM session
  the batched reads open for it), and the fully qualified name a domain member reports for itself;

Over SSH, Windows OpenSSH stalls on more than about 70 KB of script input, so the plugin refuses a script above
64 KB with an error that says to use WinRM. Its scripts send under 24 KB (the batched reads cap what they send); only very large
parameter lists come close.

Try the plugin on a lab server before a production one, and use **Dry run** before turning **Sync enabled** on.

## Scope of v1

- **DHCP only.** DNS records, DHCP server roles and features, and server-wide settings beyond those listed in
  [How objects map](mapping.md) are out of scope. The DHCP Server role must already be installed.
- **No authorization in Active Directory.** The plugin does not authorize DHCP servers.
- **Leases are read only and not stored.** No lease history, no IP address updates from leases, at most 5000 leases
  shown per read. Windows reports no state for DHCPv6 leases, so their state shows as n/a.
- **DHCPv6** is limited to scopes, reservations, user classes and values of standard DHCPv6 options. DHCPv6
  vendor classes, custom option definitions and exclusion ranges are left alone. See [DHCPv6](dhcpv6.md).
- **Failover** is IPv4 only, as on Windows.
- **No data migration from 0.1.x.** 0.2.0 starts empty: recreate the Windows servers and import them again (see
  [Upgrading from 0.1.x](installation.md#upgrading-from-01x)). Data in netbox-plugin-dhcp is neither read nor
  changed.

## Windows rules the plugin will not work around

Some changes are only possible on Windows by removing an object with everything in it. The plugin refuses them
rather than delete data: a scope's subnet mask, an option definition's type or array flag, removing a policy
condition, a failover relationship's partner or authentication. The dry run shows a subnet mask change as not pushed
(and it holds back the IPv4 deletes); for the others the push fails on them. Make them on Windows by hand, then
import. See [Push](push.md#refused-while-pushing).

Windows also refuses some policies outright: a condition value with a wildcard in the middle (`host*.example.com`;
put it at the start or the end), and IP ranges or ordinary option values on a policy with an FQDN condition (errors
DHCP 20135 and 20136). A push of such a policy fails the same way, so fix it in NetBox.

Edits of a prefix, IP range or IP address in IPAM are not checked against the scopes and reservations that use them
(only the permission to change them is, see [IPAM edits](ipam-edits.md)). A range that ends up outside its prefix, or
a reservation outside its scope's prefix, is left out of the next push with a reason in the dry run, and the Windows
object stays as it was until NetBox is fixed. The same goes for scopes of one server sharing addresses, overlapping
exclusion ranges and two reservations for one client in a scope: NetBox accepts them, the push leaves them out (see
[Push](push.md#refused-when-the-push-is-planned)). While anything in a family cannot be pushed, nothing in that family
is deleted; the dry run lists what is waiting and why. So a single object left out (in any scope of the server) holds
back every IPv4 delete, every DHCPv6 delete, or every server-level policy delete, until it is fixed; a create that needs
one of those deletes first (the same client's reservation at a new address in the scope, an exclusion range moved over
its old addresses, a renamed failover relationship) waits with it. A scope sharing addresses with a scope NetBox manages
but no longer holds is not left out: the push deletes the old one first, and if that delete is held back (changed on
Windows, hand-made objects in it) the new scope fails at Windows. A scope's subnet mask still cannot change through a
push: split or merge the scope on Windows, then import.

## NetBox and branching

- NetBox 4.7 only (the plugin declares `max_version = 4.7.99`).
- An empty merge sends no signal, so it queues no push.
- Edits made directly in main push without approval, even with change control.

## Operations

- The worker's database connection must not go through pgbouncer in transaction mode (advisory locks).
- A DHCPv6 reservation deleted on Windows while a server is being read fails that read (import, push or dry run):
  Windows reports it with a database error code (DHCP 20013), which the plugin does not take for "deleted". The
  next run works. An IPv4 reservation or a policy deleted during a read only makes its scope be read again.
- Reading a server takes a few PowerShell calls per scope, `max_concurrency` scopes at a time: the option values
  of a scope's reservations and the IP ranges and values of its policies are read in batches of up to 64 per call,
  so the number of calls grows 64 times slower with the number of reservations or policies. The server-wide lists
  (IPv4 and DHCPv6, about 15 calls) are read one after another first. A push or an import of a server with
  thousands of scopes still takes minutes.
- `max_concurrency` caps the sessions of one job, not of one Windows host: several server records reaching the same
  host, or several jobs at once, add up (see [Workers](workers.md#timeouts-and-concurrency)).
- Two Windows servers that hold the same subnet without being failover partners share one IP range in NetBox when
  they are imported into the same VRF, and an import will not move that range for one of them: the scope change
  fails and names the other scope. To avoid the sharing, give each server its own import VRF before its first
  import; a scope that already exists keeps the VRF of its prefix, so for those see
  [Troubleshooting](troubleshooting.md#import). Import the failover relationship if they are partners. A range
  change and a brand new failover relationship found in the same import no longer need a second run: the scope is
  written first and already counts as shared with the partner when every scope of the relationship is in NetBox (or
  created by the same run) and the user applying it may add the relationship. A proposal's scope item applied without
  the relationship's item still waits for it. If the relationship then fails anyway (a stale item, for example), the
  range change stays: NetBox has the new range for both servers but no relationship. Import or apply the relationship
  again, or put the range back by hand.
- Deleting a device, virtual machine or interface whose IP address or MAC address a reservation uses is refused by
  NetBox, on purpose: removing the reservation with it would also remove it from Windows on the next push. The
  refusal links the reservation, and the pages of the device, virtual machine, interface, IP address and MAC address
  list the reservations that use them in a **DHCP reservations** panel (see
  [Reservations](reservations.md#deleting-a-device-or-address-a-reservation-uses)). Delete the reservation, or
  unassign the address from the interface, first (a primary or out-of-band IP, or an interface's primary MAC
  address, must stop being primary before NetBox lets it be unassigned). Deleting a module or an FHRP group is
  refused the same way when a reservation uses one of its interfaces' addresses or its virtual IP address; those
  pages have no panel, so look at the interface or IP address page.
