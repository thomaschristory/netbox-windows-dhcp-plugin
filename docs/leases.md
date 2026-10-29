# Leases

Leases are read live from Windows and shown on a **Leases** tab of the scope page. They are not stored in
NetBox models, and NetBox IP addresses are not updated from them.

## Reading the leases of a scope

1. Open a scope (**Windows DHCP > Scopes**). The **Leases** tab appears next to the scope's other tabs.
2. Click **Load leases**. A background job connects to Windows and reads the scope's IPv4 or DHCPv6 leases.
3. The panel refreshes itself every few seconds while the job runs, then shows the result.

![The Leases tab of a scope after Load leases: the scope's reservations with client ID, host name, state, expiry and
description](images/leases-tab.webp)

| Column | Meaning |
|---|---|
| IP address | The leased address |
| Client ID or DUID | The MAC address (IPv4) or DUID (DHCPv6) of the client |
| IAID | DHCPv6 only |
| Host name | As the client reported it |
| State | The Windows address state of an IPv4 lease, for example `Active` or `ActiveReservation`. Windows reports no state for DHCPv6 leases, so it shows n/a there |
| Expires (UTC) | When the lease ends, as Windows reports it |
| Description | The Windows lease description |

Leases are sorted by address. At most 5000 are kept from one read; above that, the panel says how many there are and
shows the first 5000.

## How reads are kept

- Only the latest read of a scope is kept, in the data of its job. Each new read deletes the earlier ones.
- The job belongs to the Windows server and records the scope, so the read also appears in the server's jobs, for
  users who may view jobs.
- One read per scope waits or runs at a time: the button is disabled meanwhile, and a second request only gets a
  warning that the leases are already being read.
- A read still waiting or running after 15 minutes counts as lost (the worker may have stopped): the panel says so
  and you can load the leases again.
- A failed read shows its reason, for example a scope that no longer exists on Windows.

## Who can see and load leases

- Seeing the tab needs `view` on the scope and on Windows servers. The panel shows the leases only when the user may
  view that scope's Windows server (object permissions included).
- Loading leases connects to Windows, so, like the connection test, it needs `change` on the Windows server too.

## Branches

Leases are read for the scope as it is in main. Inside a branch the **Load leases** button is hidden and reads are
refused: switch to main.
