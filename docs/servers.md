# Windows servers

A **Windows server** is one Windows DHCP server and how to reach it. Every scope, superscope, class, option
definition, option value, policy, failover relationship and MAC filter in the plugin belongs to one. Find them
under **Windows DHCP > Windows servers**.

The Windows server itself is connection settings and bookkeeping, not DHCP configuration: with netbox-branching it
always lives in main and is never branched. The DHCP configuration under it (scopes and the rest) is branched like
any other NetBox data.

![The Windows servers list: dhcp01 and dhcp02 with host, transport, WinRM authentication, credential profile, sync
state and device](images/servers-list.webp)

## Fields

| Field | Default | Meaning |
|---|---|---|
| Name | | How NetBox calls the server, unique. Sync runs, import branches (`dhcp-import/<name>/<timestamp>`) and scope names in lists use it |
| Device | empty | The device the DHCP service runs on, for reference. The connection always uses **Host** |
| Virtual machine | empty | The same for a virtual machine. Set a device or a virtual machine, not both |
| Host | | Host name or IP address to connect to. A plain name or address: no port, user or path |
| Transport | WinRM | WinRM or SSH |
| Port | empty | Empty means 5986 (WinRM over HTTPS), 5985 (WinRM over HTTP) or 22 (SSH) |
| Credential profile | | Name of a [credential profile](configuration.md#credential-profiles). Its `allowed_hosts` must cover the host |
| WinRM authentication | NTLM | NTLM, Basic, Kerberos or CredSSP (failover), see [Preparing Windows](windows-setup.md#authentication-methods) |
| WinRM over HTTPS | on | Off uses HTTP; NTLM, Kerberos and CredSSP messages are still encrypted |
| Verify host | on | Check the TLS certificate (WinRM) or the host key (SSH). Cannot be turned off for SSH, nor for Basic |
| Kerberos realm | empty | Required with Kerberos, for example `CORP.EXAMPLE` |
| Kerberos SPN | empty | Empty means `WSMAN/<host>` |
| SSH host key | empty | The server's host key in `authorized_keys` form; when empty, the worker's `~/.ssh/known_hosts` is used |
| Remote DHCP server | empty | Run the cmdlets against this server through the host (`computer_name`). See [the double hop](windows-setup.md#managing-a-dhcp-server-through-another-host) |
| Import VRF | empty (global) | VRF of the prefixes, IP ranges and IP addresses that imports create or reuse |
| Sync enabled | on | Push NetBox changes to this server. Off stops pushes (manual and automatic), not imports or dry runs |
| Import interval | empty | Minutes between [scheduled imports](import.md#scheduled-imports); empty or 0 means none |

The form refuses a device and a virtual machine together, a host the credential profile does not allow, Basic over HTTP or without verification, Kerberos
without a realm, and SSH without host verification.

Windows servers can be created, edited and deleted in bulk, and imported from CSV, like other NetBox objects.

## Actions

The Windows server page has four buttons. Each queues a background job and returns at once; the result appears in
the **Sync runs** table at the bottom of the page (reload it) and in the job's log. All four need the `change`
permission on the Windows server (see [Permissions](permissions.md)).

### Test connection

Connects, checks that the `DhcpServer` PowerShell module is there, and counts the IPv4 scopes. Use it after every
change to the connection settings. It also records the DHCP server's computer name, DNS name and addresses (**Reported names**),
which imports and pushes record too: its failover partners use them to match it, see
[Failover](failover.md#how-it-syncs).

### Import (Import now)

Reads the whole server and brings the differences into NetBox. Without netbox-branching the button is **Import now**
and writes an import proposal. With it, **Import** opens a menu: **Import into branch** (an import branch, the
default), **Import into this branch** (only while you work in a branch) and **Import directly** (into main, after a
confirmation). See [Import](import.md#choosing-where-an-import-writes). Refused while another import of the same server is queued or running. The **Import** panel on
the server page shows whether it is queued, running or done, and where the result went.

### Sync now

Pushes NetBox to the server: creates, updates and deletes what differs. See [Push](push.md). Refused when **Sync
enabled** is off, or when a push for the server is already waiting to start (that one will read the latest data).

### Dry run

Plans a push without writing anything. The planned changes and anything that would not be pushed go into the sync
run summary. A dry run works with **Sync enabled** off, so you can check a server before turning sync on.

## Sync runs

Every test, import, push and dry run leaves a **sync run** (**Windows DHCP > Sync runs**), linked to its NetBox job.
The summary on the right holds what the run planned and did:

![A push sync run: kind, status, job, start and end, and its summary with one planned and applied
change](images/quickstart-6-push.webp)


| Field | Meaning |
|---|---|
| Kind | Push, Dry run, Import or Connection test |
| Status | Pending, Running, Completed or Failed |
| Started, Completed | When the run started and ended |
| Branch | The branch whose merge triggered the push, or that the import wrote into |
| Destination | Imports only: where the import wrote (import branch, chosen branch, main or import proposal) |
| Summary | Counts and lists, see below |
| Error | One line per failed change, or the reason the run failed |

The summary of a **push** or **dry run** holds:

- `changes`, `create`, `update`, `delete`: what was planned;
- `planned`: the planned changes (the first 1000), each with its kind, Windows key and values;
- `not_pushed`: changes the push refused, each with the reason (see [what a push will not do](push.md#what-a-push-will-not-do));
- `unmanaged`: how many Windows objects NetBox does not manage and the push left alone;
- `notes`: what the read of Windows left out (for example a scope deleted on Windows during the read);
- for a push, `applied`, `failed` and `skipped` counts, and `errors` and `not_attempted` lists.

The summary of an **import** holds the number of differences by action and `notes` about what was left out. With
branching it also holds how many changes were written into the branch. Without branching it also holds `review`
(differences sent to the import proposal) and `dismissed` (differences left out because a reviewer had already
dismissed exactly the same change); `changes` counts both.

The job log (**Operations > Jobs**, or the job linked from the sync run) has one line per change and per note.

## Deleting a Windows server

Deleting a Windows server deletes, in NetBox, everything that belongs to it: its scopes, superscopes, reservations,
exclusion ranges, classes, option definitions and values, policies, MAC filters, the failover relationships it is the
primary of, sync runs, sync state and import proposals. A server that is the partner in a failover relationship cannot
be deleted until that relationship is deleted: deleting it along with the partner would make the next push to the
primary remove failover on Windows. Nothing is removed from Windows, and the prefixes, IP ranges, IP addresses and MAC
addresses the scopes and reservations pointed at stay in IPAM. A new Windows server for the same host starts empty:
import it to bring its configuration back. The first push after that adopts the objects that are identical on both
sides.

The page of a linked device or virtual machine lists its Windows servers in a **Windows DHCP servers** panel. When
the device or virtual machine is deleted, the Windows server stays, without the link.
