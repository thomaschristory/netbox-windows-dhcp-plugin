# Workers and jobs

Every connection to Windows runs in a NetBox background job, run by the NetBox worker (`manage.py rqworker`). Web
requests only queue jobs, so a slow or unreachable Windows server never holds up the NetBox UI.

## The jobs

| Job name | Started by | Does |
|---|---|---|
| Windows DHCP connection test | Test connection | Connects, checks the `DhcpServer` module, counts IPv4 scopes |
| Windows DHCP import | Import now (or the Import menu), the scheduler | Reads the server, writes an import branch, a branch you chose, main, or a proposal |
| Windows DHCP push | Sync now, saves, merges, reverts | Makes the server match NetBox |
| Windows DHCP dry run | Dry run | Plans a push without writing |
| Windows DHCP leases | Load leases | Reads the leases of one scope |
| Windows DHCP import scheduler | NetBox, every minute | Queues imports for servers whose import interval has passed, then deletes old sync runs |

The jobs belong to their Windows server and appear under **Operations > Jobs**, with their log. Each one except the
lease read and the scheduler also leaves a [sync run](servers.md#sync-runs).

## What the worker needs

- The same `configuration.py` and `PLUGINS_CONFIG` as the web service, including every credential profile.
- The environment variables and files the profiles read (`password_env`, `password_file`, ...).
- For SSH without a pinned host key: the `known_hosts` file of the user the worker runs as (not read when the
  credential profile sets `ssh_host_keys`).
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
- A scheduled import that finds its server busy is queued again a minute later, for up to half an hour; the waiting
  attempts leave no failed sync run. After that it fails, and the next interval brings a new one.
- An import started by hand that finds its server busy fails; run it again.
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
  stay silent before it is abandoned. SSH connections must open within 30 seconds. Over WinRM it bounds one wait
  for output, so a command that keeps the connection alive without finishing is abandoned after ten times that
  long, and never before 10 minutes (20 minutes with the default).
- Reading a server (import, push, dry run) makes a few calls per scope (reservations and policies are read in
  batches of up to 64 per call). The server-wide lists (IPv4 and DHCPv6) are read first, one after another; then
  up to `max_concurrency` (4) scopes, IPv4 and DHCPv6 alike, are read at the same time, each command over its own
  WinRM connection or SSH session. The changes of a push are always sent one after another, in order. When a
  scope fails, the scopes after it stop at their next command and the job waits for the commands already sent.
  When the job times out, every scope stops at its next command and the job waits at most 30 seconds for the
  commands already sent, so it can still record the failed run before the worker kills it.
- `max_concurrency` caps one job, not the Windows host. Imports and pushes of one Windows server record wait for
  each other (see above), but jobs of other records run alongside, and so do lease views and connection tests.
  Several records that reach Windows through the same host (one management host with `computer_name`, or the same
  DHCP server entered twice) and several workers can therefore open several times `max_concurrency` sessions on
  one host. Windows limits them: WinRM allows 30 shells per user by default (`MaxShellsPerUser`, and
  `MaxConcurrentOperationsPerUser`), and Windows OpenSSH allows 10 sessions per connection by default (`MaxSessions`
  in `sshd_config`, often lower on hardened servers). An SSH session refused while other commands of the job run
  on the same connection is tried again for as long as those run (up to the command timeout, 5 minutes by default),
  since ten PowerShell commands at once can take about 10 s on a small server; a refusal with nothing else of the
  job's running fails that command at once with the server's reason. A refused WinRM shell fails the command. Keep the setting low (the
  default 4; above 10 a warning is logged, and above 30 it is cut down to 30), lower it (`1` reads one scope at a
  time) if a server, a firewall or a management host struggles, and raise it only when a server with many scopes
  reads too slowly and has sessions to spare. The time still grows with the number of scopes, divided by the setting at best.
- A push, dry run or import job may run for an hour; with netbox-branching an import may run as long as its
  `job_timeout` when that is longer (provisioning a branch can be slow).

## Sync run retention

The import scheduler also deletes the [sync runs](servers.md#sync-runs) older than `sync_run_retention_days` (90 by
default; `0` keeps every run), oldest first, at most 500 per minute, so the first runs after an upgrade catch up
on a long history step by step. Once nothing more is due it looks again an hour later, so a lower setting can take
up to an hour to apply. A run's push triggers ("Triggered by") go with it, and a closed import proposal keeps its
items but no longer links the run.

Some runs are kept whatever their age, because the plugin still reads them. For each Windows server:

- the newest run of each kind (import, push, dry run, connection test): the scheduler times the next import from
  the newest import, and the server's Import panel describes it;
- the newest push that reached Windows: the next push reads from it the scopes still to copy to a failover partner;
- the newest run of each of the two newest import branches: the Import panel keeps an earlier import branch that
  still waits to be merged in sight;
- the runs open import proposals belong to;
- runs whose job is still queued or running (a job still "running" two hours after it started was abandoned, and
  its run is no longer kept).

Runs with a journal entry are never deleted
either: delete the entry to let the run go. A deleted run's tags and bookmarks go with it, and users subscribed to
it may get a notification that it was deleted. The deletes are not recorded in the change log.

The runs kept are the newest of each server, whoever looks. A user whose view permission on sync runs is limited
by their attributes (only some kinds, for example) can see the newest run they may view deleted; the Import panel
then describes an older one, or none.

`sync_run_retention_days` must be a whole number of days from 1 to 36500 (or `0`). Any other value deletes nothing:
NetBox logs a warning at startup, and the scheduler's job log says so too. A failing delete is logged in the
scheduler's job, does not stop the imports it queues, and is tried again 15 minutes later. Each batch is deleted at
once, so a NetBox protection rule (`PROTECTION_RULES`) on sync runs that refuses one of them stops retention
altogether. If you need such a rule, make it refuse only runs that retention keeps anyway, such as runs with a
journal entry.

NetBox's own `JOB_RETENTION` setting prunes the jobs: a run whose job is gone keeps its summary and loses the link.

## Notifications

A push started with **Sync now** or by a merge or revert always notifies the user who started it, through NetBox's
job notifications. A push that follows an ordinary save only notifies when it fails, so a day of edits does not
flood anyone's notifications. A push queued again because the server was busy keeps the setting of the first, and
the waiting attempts do not notify: only the attempt that pushes does, or the last one when it gives up. One
exception: a waiting attempt that finds another push already queued for the server stops retrying and notifies with
its own setting, since that other push carries the changes. Lease reads never notify: the page updates itself.
