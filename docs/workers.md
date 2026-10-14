# Workers and jobs

Every connection to Windows runs in a NetBox background job, run by the NetBox worker (`manage.py rqworker`). Web
requests only queue jobs, so a slow or unreachable Windows server never holds up the NetBox UI.

## The jobs

| Job name | Started by | Does |
|---|---|---|
| Windows DHCP connection test | Test connection | Connects, checks the `DhcpServer` module, counts IPv4 scopes |
| Windows DHCP import | Import now, the scheduler | Reads the server, writes an import branch or proposal |
| Windows DHCP push | Sync now, saves, merges, reverts | Makes the server match NetBox |
| Windows DHCP dry run | Dry run | Plans a push without writing |
| Windows DHCP leases | Load leases | Reads the leases of one scope |
| Windows DHCP import scheduler | NetBox, every minute | Queues imports for servers whose import interval has passed |

The jobs belong to their Windows server and appear under **Operations > Jobs**, with their log. Each one except the
lease read and the scheduler also leaves a [sync run](servers.md#sync-runs).

## What the worker needs

- The same `configuration.py` and `PLUGINS_CONFIG` as the web service, including every credential profile.
- The environment variables and files the profiles read (`password_env`, `password_file`, ...).
- For SSH without a pinned host key: the `known_hosts` file of the user the worker runs as.
- For Kerberos: the GSSAPI libraries and `krb5.conf`.
- For an internal CA: `REQUESTS_CA_BUNDLE` pointing at a bundle that includes it.
- Network access to the Windows servers' WinRM or SSH port.

With netbox-branching, pushes after a merge start in the worker that runs the merge, after its transaction has
committed. A merge that is aborted, or has no changes, queues nothing.

## One job per server

Imports and pushes of one Windows server never run at the same time, across all workers: an import reading Windows
while a push writes it would propose half-pushed changes back. A PostgreSQL advisory lock per server enforces this.

- A push that finds its server busy is queued again a minute later, for up to two hours, so a merge's push is never
  lost.
- An import that finds its server busy fails; the next one (manual or scheduled) runs normally.
- A dry run that finds its server busy fails; run it again.

The lock is held by the worker's database session, so the worker must not reach PostgreSQL through a
transaction-pooling proxy (pgbouncer in transaction mode). Session pooling is fine.

## Queues and duplicates

- A push or dry run is not queued while another of the same kind for the same server is waiting to start.
- An import is not queued while another for the same server is queued or running.
- A job still waiting 15 minutes after it was due is considered lost (the worker died, or Redis lost it) and no
  longer blocks new pushes. An import still "running" after two hours is considered abandoned.

## Timeouts and concurrency

- `command_timeout` (120 seconds) is the WinRM connection and operation timeout, and the longest an SSH command may
  stay silent before it is abandoned. SSH connections must open within 30 seconds.
- One job runs at most `max_concurrency` commands at once against a server (4). Reading a large server makes one
  call per scope and per reservation list, so raise it for servers with many scopes, if the server copes.
- A push or dry run job may run for an hour; an import uses netbox-branching's `job_timeout` when branching is
  installed (provisioning a branch can be slow), RQ's default otherwise.

## Notifications

A push started with **Sync now** or by a merge or revert always notifies the user who started it, through NetBox's
job notifications. A push that follows an ordinary save only notifies when it fails, so a day of edits does not
flood anyone's notifications. A push queued again because the server was busy keeps the setting of the first. Lease
reads never notify: the page updates itself.
