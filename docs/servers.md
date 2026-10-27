# Windows servers

A **Windows server** says how to reach the Windows host behind one netbox-plugin-dhcp DHCP server. Each sys4 DHCP
server has at most one. Find them under **Windows DHCP > Windows servers**.

Windows servers are bookkeeping, not DHCP configuration: with netbox-branching they always live in main and are never
branched.

## Fields

| Field | Default | Meaning |
|---|---|---|
| DHCP server | | The netbox-plugin-dhcp server this Windows host serves (**Generic DHCP server** on the Windows server page and in the list, where the server's name itself opens the Windows server) |
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

The form refuses a host the credential profile does not allow, Basic over HTTP or without verification, Kerberos
without a realm, and SSH without host verification.

Windows servers can be created, edited and deleted in bulk, and imported from CSV, like other NetBox objects.

## Actions

The Windows server page has four buttons. Each queues a background job and returns at once; the result appears in
the **Sync runs** table at the bottom of the page (reload it) and in the job's log. All four need the `change`
permission on the Windows server (see [Permissions](permissions.md)).

### Test connection

Connects, checks that the `DhcpServer` PowerShell module is there, and counts the IPv4 scopes. Use it after every
change to the connection settings.

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

Every test, import, push and dry run leaves a **sync run** (**Windows DHCP > Sync runs**), linked to its NetBox job:

| Field | Meaning |
|---|---|
| Kind | Push, Dry run, Import or Connection test |
| Status | Pending, Running, Completed or Failed |
| Started, Completed | When the run started and ended |
| Branch | The branch whose merge triggered the push, or that the import created |
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

Deleting a Windows server deletes its sync runs, sync state and import proposals. The sys4 DHCP server and the DHCP
objects stay, and nothing is removed from Windows. Without the sync state, a new Windows server for the same host
manages nothing at first: the next push adopts the objects that are identical on both sides, and leaves the rest of
Windows alone until it is imported.
