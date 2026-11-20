# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Credential profiles can pin what a Windows server's editors otherwise choose: `transports` (WinRM, SSH), `ports`,
  `kerberos_realms` (WinRM with Kerberos) and `ssh_host_keys`. With `ssh_host_keys`, a server without its own pinned
  key trusts only the profile's keys, not the worker's `known_hosts`. Each key is optional and must be a list (or
  tuple); an empty one allows nothing it applies to. A failover shared secret's profile limits apply to the primary
  server's connection that carries it (over SSH, the server must then pin one of its keys). Checked when a server or
  failover relationship is saved and before every connection or push (#103).

### Changed

- Connections refuse a transport, WinRM authentication or failover authentication value outside the known choices
  (only possible for a record written straight to the database, `KERBEROS` or `Basic`, say), so every check reads the
  value the transport uses. Failover authentication only opens a second logon for `credssp` (#103).
- A failover shared secret profile used with a server on CredSSP without HTTPS and certificate verification (a lab)
  now needs `allow_insecure_credssp` too, as the server's own profile does: the secret travels over that connection.
  Until it is set, creating a relationship or changing its secret profile or primary server is refused on save, and
  for existing relationships the push fails the failover changes that send the secret. See
  [Upgrading](docs/installation.md#upgrading-from-05x-to-060) (#103).

## [0.5.0] - 2026-11-20

0.5.0 lets a Windows server use CredSSP only for failover writes and its own logon (Kerberos, say) for the rest,
sends long scripts over SSH as a file instead of refusing them, and records what a domain lab showed about Kerberos,
SSH keys and `-ComputerName`. It adds migration `0006` (one field, empty for existing servers); nothing else to
change. See [Upgrading from 0.4.x to 0.5.0](docs/installation.md#upgrading-from-04x-to-050).

### Added

- **Failover authentication** on a Windows server: with CredSSP there, the plugin logs on with CredSSP only for the
  calls that make the server reach its failover partner (creating, changing and deleting a relationship, its scopes,
  and replication after a push), and with the server's own **WinRM authentication** (Kerberos, say) for everything
  else. Kerberos cannot carry that hop, even with constrained delegation. The connection test checks both logons.
  The same rules as CredSSP apply: HTTPS with certificate verification, unless the credential profile sets
  `allow_insecure_credssp`. Available in the form, CSV import, REST API, GraphQL and filters. Migration `0006` adds
  the field, empty for existing servers (#141).

### Changed

- Over SSH, a script over 32 KB no longer fails with "too large for SSH, use WinRM". Windows OpenSSH stalls on long
  stdin, so the plugin uploads the script over SFTP to the account's home folder under a random name, sends only its
  path, a checksum and any secret on stdin, and the script deletes the file once read (a file replaced in the
  meantime is refused). The server needs the `sftp` subsystem, which Windows OpenSSH enables by default (#63).

### Documentation

- Kerberos, SSH key logons and **Remote DHCP server** (`-ComputerName`) were checked in a domain lab (Windows Server
  2025, #62). Kerberos over WinRM HTTP and HTTPS and SSH with a key work for reads and writes. Failover writes need
  the password on the first server: CredSSP or SSH with a password. Kerberos delegation does not help, contrary to
  what the docs said (resource-based constrained delegation tried), and an SSH key logon cannot do them either.
  `-ComputerName` passed with CredSSP; with Kerberos it makes the same hop that failed. Troubleshooting lists Python
  3.13's `Missing Authority Key Identifier` certificate error.

## [0.4.1] - 2026-11-19

0.4.1 fixes parallel reads failing on an occasional refused connection. No migration, no setting or API change.

### Fixed

- A parallel read over WinRM (`max_concurrency` above 1) could fail with `Connection refused` when the network path
  to the server (a NAT router, Docker Desktop's port forwarding) refused one of the many new connections, although
  WinRM was up. A WinRM call whose connection is refused before its command is sent is now tried again after 0.5, 1
  and 2 s; once the command has been sent nothing is tried again, so a write never runs twice (#132).

## [0.4.0] - 2026-11-18

0.4.0 makes reads faster (batched reads, and `max_concurrency` now reads several scopes at once), keeps the failover
secret out of the scripts, guards IPAM edits that change what is pushed, and makes a push leave out, with a reason,
what Windows would refuse, without deleting anything on Windows because of it. It adds migrations `0004` and `0005`.
Before upgrading, read the **Upgrading** notes below and
[Installation](docs/installation.md#upgrading-from-030-to-040): sync runs older than 90 days are deleted, large
proposal reviews answer `202` in the REST API, a `max_concurrency` left in the settings takes effect, IPAM edits that
change Windows need the plugin's `change` permission, and a push holds deletes back while something it cannot write
is waiting.

### Added

- Each push's sync run lists what queued it ("Triggered by"): the edits it carries, with the user and a link to the
  change log entries of that request, a Sync now click, or a branch merge or revert. Edits made while a push waits to
  start are added to that push. Also in the REST API (`triggers` on sync runs). Adds migration `0004`.
- Old sync runs are now deleted: new setting `sync_run_retention_days` (default 90, `0` keeps every run). The import
  scheduler deletes older runs, oldest first, at most 500 a minute, and keeps the runs the plugin still reads
  whatever their age (the newest of each kind per server, the newest push that reached Windows, the runs of open
  import proposals, the newest runs of the two newest import branches, runs still queued or running, runs with a
  journal entry). Until now the history grew without limit. **Upgrading deletes sync runs older than 90 days**
  unless the setting says otherwise. Runs with a journal entry are kept; a deleted run's tags and bookmarks go with
  it, and users subscribed to it may get a deletion notification. Adds migration `0005` (two indexes). (#106)
- Applying or dismissing many import proposal items at once runs in the background: above the new
  `review_job_threshold` setting (100 open items by default), Apply all, Dismiss all and the bulk buttons queue a
  **Windows DHCP import review** job of the proposal, run as the requesting user with the same permission checks
  (made again when it starts), instead of working inside the web request, where a large first import timed out the
  browser or the reverse proxy. The message links the job, and the proposal page shows it in a Background review
  panel and on a new Jobs tab, and the REST API gives its status and counts as `review_job` on each import
  proposal. One review job per proposal at a time. Items a newer import replaced before the job started are skipped
  and counted (`superseded`). (#101)
- The pages of a device, virtual machine, interface, IP address and MAC address show a **DHCP reservations** panel
  listing the reservations (IPv4 and DHCPv6) that use the object or, for a device or virtual machine, the addresses
  of its interfaces, with their server, scope, IP address and MAC address. NetBox still refuses to delete these
  objects while a reservation uses them; the panel shows why before the delete is tried. Only reservations the user
  may view are listed, and the panel is hidden when there is none. (#107)
- A push (and its dry run) now leaves out, with a reason under **Not pushed**, what Windows would refuse instead of
  failing on it: an IPv4 scope range outside its prefix, reversed, or holding the network or broadcast address; a
  reservation outside its scope's prefix; an exclusion range outside its scope's prefix or reversed; IPv4 scopes of
  one server sharing addresses (a scope made on Windows by hand included); exclusion ranges of one scope sharing
  addresses; two reservations of one scope for the same client (DHCPv6: the same DUID and IAID). These mostly come
  from IPAM edits, which the plugin's forms do not see. When one of the clashing objects is already on Windows, it
  keeps being pushed. What hangs off a left-out object is left out with it, and a scope policy IP range must run
  upwards inside the scope's prefix. Imports are not affected. (#108)
- **Delete pause:** While anything in a family cannot be pushed, nothing in that family is deleted; the dry run lists
  what is waiting and why. The families are the IPv4 scopes (with their reservations, exclusions, policies, option
  values, superscopes and failover relationships), the DHCPv6 scopes, and the server-level policies with the option
  values set through them; classes, option definitions, plain server option values and MAC filters are not held up. A
  scope whose prefix moved in IPAM, a reservation that changed address or a renamed policy looks to the push like a new
  object plus a deleted one, so when the new one cannot be pushed the old one now stays on Windows instead of being
  deleted. A create that needs a waiting delete first waits too, instead of failing at Windows on every push. A scope
  subnet mask change (a split or a merge) is now refused when the push is planned, so it holds deletes back too. Waiting
  changes come first in the dry run's list. The waiting deletes happen on the first push with nothing left out in their
  family; a policy refused for its processing order holds deletes back only when it is new on Windows (a renamed
  policy, say). A simple move of a scope from one failover relationship to another now goes through in one push; if the
  relationship taking it fails at Windows, the push puts the scope back where it was. Scopes swapped between
  relationships are refused (move one at a time). A new partner, a new hot standby role or turning authentication
  off on a failover relationship is now refused in the dry run too. (#108)
- The Windows server form refuses a second server for one DHCP server (same host ignoring case and a trailing dot,
  same port, same remote DHCP server), and the failover relationship form refuses a new partner the primary server's
  credential profile does not allow (`allowed_hosts`), which the push already refused. Imports are not affected.
  **Upgrading:** two existing Windows servers for one DHCP server can no longer be edited until one of them is
  deleted or pointed elsewhere. (#108)

### Changed

- **REST API:** above `review_job_threshold`, `POST import-proposals/<id>/apply/` and `/dismiss/` answer `202` with
  the job instead of `200` with the counts, and `POST import-proposal-items/apply/` and `/dismiss/` answer `202` with
  `{"jobs": [...]}` (one per proposal); the counts are in the job's `data` once it has finished. A second large
  review of a proposal while one waits or runs answers `409`. Below the threshold the answers are unchanged. Clients
  that apply large proposals through the API must handle the `202`, or raise the setting. (#101)
- Reading a Windows server (import, push, dry run) no longer starts PowerShell once per reservation and twice per
  policy: the option values of a scope's reservations, and the IP ranges and option values of its policies (and of
  the server-level policies), are read in batches of up to 64 per call, with one CIM session per batch when
  `computer_name` is set. A scope with 3,000 reservations and 10 policies took more than 3,020 calls; it now takes
  about 52, and a server's import or push holds its lock for far less time. An IPv4 reservation or a scope policy
  deleted on Windows during the read makes that scope be read again; an IPv4 reservation used to fail the read
  instead (Windows answers DHCP 20016, which is now taken as "deleted" for these reads only). A server-level policy or
  a DHCPv6 reservation (Windows answers with a database error code) deleted during the read still fails it, as
  before; the next run works. Checked on Windows Server 2025. (#100)
- `max_concurrency` now does something: reading a server (import, push, dry run) reads up to that many scopes (4 by
  default), IPv4 and DHCPv6, at the same time, each PowerShell command over its own WinRM connection or SSH session,
  instead of one after another. The result is the same as before, in the same order, and a failing scope fails the
  read with the same error; the scopes after it stop at their next command. The server-wide lists are still read
  one after another (all of them first now, DHCPv6 included), and a push still sends its changes one after
  another. The cap is per job, not per Windows host: see [Workers](docs/workers.md#timeouts-and-concurrency) for
  WinRM and OpenSSH session limits. Set it to `1` to keep the old behaviour, for example when a firewall or the DHCP
  server limits connections. **Upgrading:** a `max_concurrency` left in `PLUGINS_CONFIG`, which did nothing until
  now, takes effect. A value that is not a whole number of at least 1 is now logged and the default used; above 10
  a warning is logged, and above 30 (the default WinRM `MaxShellsPerUser`) 30 is used. A job that times out waits
  at most 30 seconds for the commands in flight.
  In the lab (two servers, WinRM with CredSSP or NTLM; 7 scopes, 6 IPv4 and 1 DHCPv6, and 5 IPv4 scopes) a full read
  took about 30% less time with the default of 4, with no connection errors at up to 8. (#99)
- SSH: a session that fails to open on a live connection no longer drops that connection, which killed every
  command running on it and hid the real error behind theirs; only that command fails. When the server refuses a
  session while other commands run on the connection (sshd `MaxSessions`), it is tried again for a few seconds
  first. The message gives the server's reason, then names `MaxSessions` and `max_concurrency` as a possible
  cause. A dead connection is still opened again once. (#99)
- The failover shared secret no longer appears in the PowerShell script sent to the DHCP server: it follows the
  script on the same input line and is handed to it in a variable. PowerShell script block logging (event 4104) on
  the DHCP server no longer records it, nor does module logging of `ConvertFrom-Json` (checked with PowerShell 7
  and with Windows PowerShell 5.1 on Windows Server 2025 in the lab). Module logging and transcription can still
  record the failover cmdlet's own `-SharedSecret` parameter when they are on. A script input line cut short in transit is
  now refused instead of running. (#104)
- Editing a prefix, IP range, IP address or MAC address that a scope or reservation uses now needs the plugin's
  `change` permission on that scope or reservation when the edit changes what is pushed to Windows (the prefix,
  the range's start or end, the address, the MAC). Until now `ipam` / `dcim` permissions alone were enough to change
  production DHCP. The save is refused with a message naming the scope or reservation. Other fields (description,
  status, tags...) are not affected. Grant `change_scope` / `change_reservation` to users who renumber in IPAM (see
  [Editing IPAM objects used by DHCP](docs/ipam-edits.md)). With netbox-branching the edit is checked in the branch,
  and again when the branch is merged or reverted, as the merging or reverting user: a merge that would change a scope
  or reservation that user may not change is refused and nothing is merged. The check reads the database the save
  writes to (a merge started from code running in a branch, such as a script, is checked against main), and a save
  is checked as the merging user only when it is one of the branch's replayed changes. A merge or revert with no user
  (a merge job whose user was deleted) is refused when it would change Windows. An import branch's own changes
  merge without the permission, also after a sync of main's edits to other fields (description, tags) or a hand
  edit of those fields in the branch; a change in the branch of the address, prefix, range or MAC (even of its mask
  alone), by hand or synced from main, is still checked, and so is reverting an import branch (it puts back values
  Windows no longer holds). At merge, an edit is also checked against the scopes and reservations the branch puts on
  the object (squash creates them after replaying the edit), and a squash merge that folds a hand edit into an
  object's creation is checked like the iterative strategy checks it. Issues
  #102, #113, #115, #116.
- Direct edits of a prefix, IP range, IP address or MAC address that do not change what Windows receives
  (description, status, tags...) no longer queue a push (a branch merge still queues one per server it touches).

### Fixed

- A scope policy whose stored IP ranges do not parse (written by a branch merge, a script or nbshell) no longer fails
  every push, dry run and import of its server: the push leaves that policy out with a reason naming it, and an
  import proposes Windows' ranges for it. Changing the prefix of its scope says which policy to fix instead of
  failing with a server error. (#108)
- A scheduled import that finds its server busy (another import or a push running) is queued again a minute later,
  for up to half an hour, instead of waiting a whole import interval. The waiting attempts leave no failed sync run
  and send no notification. An import started by hand still fails at once and says why. (#109)
- A push job log lists at most 1000 changes it did not push, then how many more (the sync run summary has the same
  limit), instead of one line for each. (#109)
- "Select all matching" on the import proposal item list refuses a query parameter that is neither a filter of the
  list nor a paging, sorting or table parameter, and changes nothing: a mistyped filter name (`proposal=7` for
  `proposal_id=7`) used to widen the selection to every open item. A saved filter is checked the same way, and one
  that does not exist or that the user may not see is refused too. A refused selection no longer also reports
  "Applied 0 item(s)". (#109)
- A scope range change and a new failover relationship found in the same import no longer fail the scope change once
  with "import the failover relationship first": the import into a branch or into main, and applying both proposal
  items in one go, write the scope as shared with the partner when the relationship's scopes are all in NetBox (or
  created by the same run) and the user may add it. Applying the scope item alone still waits for the relationship.
  If the relationship then fails, the range change stays (see [Known limitations](docs/limitations.md)). (#109)
- The lease read job no longer lets an error while closing the connection hide its own outcome. (#109)

### Performance

- Applying proposal items (and importing into main) no longer copies every queued change log event for each item:
  each item's events are kept apart and added to the request's once it succeeds, so the cost grows linearly with the
  number of items. (#101)

## [0.3.0] - 2026-11-15

0.3.0 is the result of a code audit. It tightens validation and the matching of `allowed_hosts`, so some setups
that 0.2.0 accepted are now refused: `allowed_hosts` address patterns no longer match DNS names (and short ones such
as `10.*` match nothing), hosts written as numbers (`127.1`, `2130706433`) are refused, a new failover partner must be
in the primary's `allowed_hosts`, and a reservation client ID or DUID must be hex bytes (see Changed and Security).
It adds migrations `0002` and `0003`, and fixes the netbox-docker start failure on databases upgraded from 0.1.x.

### Security

- `allowed_hosts`: an address pattern such as `10.20.*.*` also matched DNS names like `10.20.evil.example`. Address
  patterns (four parts of digits and wildcards, or anything with a `:`) now match IP addresses only, and name
  patterns match names only. Check your profiles: a bare `*` no longer covers IPv6 addresses, and `*.*.*.*` is read
  as an IPv4 pattern, so it no longer matches four-label names. A shorter pattern of digits and wildcards that
  holds a number (`10.*`, `192.168.*`) is read as an address pattern too and matches nothing: an IPv4 pattern needs
  all four parts (`10.*.*.*`).
- Hosts written as numbers that a resolver reads as an IPv4 address (`2130706433`, `0x7f.1`, `127.1`, or any name
  whose last label is a number) are refused as `host` and `computer_name`, on save and before every connection. A
  Windows server already saved with such a host stops connecting until the host is corrected.
- A push refuses to create a failover relationship whose partner is outside the `allowed_hosts` of the primary
  server's credential profile: creating it makes the primary contact the partner as the plugin's account. If the
  profile lists only the primary, add the partner (its `computer_name` when set, else its `host`). Existing
  relationships, their updates and deletes, and profiles without `allowed_hosts` are not affected.
- WinRM no longer follows HTTP redirects, so the logon only goes to the host that was checked. A redirect is now
  an error.
- SSH: when the profile's `password` is the passphrase of an encrypted private key and the server refuses the key,
  the passphrase is no longer offered to the server as an account password. An encrypted key whose passphrase is
  also the account password loses that fallback.
- An inline credential set to `None` (for example `os.environ.get()` of a missing variable) was sent as the text
  `None`: a wrong password on every job, or a guessable failover secret. It is now a configuration error naming the
  profile and the key. This applies to the keys a job uses: `username` and `password`, and `secret` when a failover
  relationship reads it. A key that is not used and set to `None` reads as not set.
- An unexpected error while applying an import proposal item no longer sends change events (event rules, webhooks)
  for objects that were rolled back. This completes the fix for GHSA-5x5m-vqw4-wq85.
- Hardening: the output read from a server is bounded (256 MiB per command, 64 KiB of error text in messages), and
  the transport settings keep passwords and private keys out of their Python `repr`.

### Changed

- New migration `0002`: run `manage.py migrate` when upgrading. It turns the sync key columns into text and adds
  an index; no data changes.
- New migration `0003`: on a database upgraded from 0.1.x it deletes the content types of the 0.1.x models that
  0.2.0 dropped, and the change log entries of their objects (see Fixed). On other databases it does nothing.
- A reservation's client ID and DUID must be hex bytes (separated by `-`, `:`, `.` or spaces, or not at all) and are
  stored in the `aa-bb-cc` form Windows reports, so a saved value can differ in spelling from what was typed. The
  form and the REST API (400) refuse anything else, such as `printer-01`.
- A DHCPv6 scope must be on a /64 prefix to be saved, and its lifetimes and timers must be in order when both
  values of a pair are set (T1 < T2 < preferred lifetime <= valid lifetime). A T1 or T2 of 0 counts as empty.
  Before, such a scope was saved and then not pushed, or refused by Windows. Existing scopes are not touched.
- A Windows server's port must be between 1 and 65535. `0` is no longer accepted on save: leave the field empty for
  the default port.
- A connection test is not queued again while one for the same server is waiting to start: the button shows a
  warning and the REST action answers 409 (it always answered 202). A looping script or repeated clicks could pile
  up logons and lock the service account out. The warning names the waiting job, and the 409 body holds its id in
  `job`, so a test that no worker picks up can be found and deleted; it stops counting after 15 minutes.
- After a push that stopped early (lost connection, command timeout, unexpected error), failover replication is no
  longer tried in the same run: the scopes wait in `replication_pending` and the next push replicates them.
- Sync run summaries: the `not_pushed`, `errors` and `not_attempted` lists hold at most 1000 entries each, with a
  `<list>_truncated` flag when cut and a new `not_pushed_count`. The run's error text is capped the same way. The
  counts stay exact.
- The Windows server CSV import accepts an `ssh_host_key` column.
- Removed unused parameters from internal sync helpers (`sync.diff.diff`, `sync.windows_state.hidden_records`).
- Development: the dev stack listens on 127.0.0.1 only; `make test-lab`, `make lab-seed` and `make lab-wipe` read
  lab settings from `dev/env/lab.env`, and they and `make lab-e2e` refuse to run when the tracked
  `dev/env/netbox.env` holds a lab password or failover secret (`make test` warns and leaves the lab tests out);
  release builds take the build backend and twine from `uv.lock`, checked by hash, and a tag publishes only after
  lint and the unit tests pass.

### Fixed

- On a database upgraded from 0.1.x, `remove_stale_contenttypes` stopped with a `ProtectedError` on the change log
  entries of the 0.1.x models, and with netbox-docker (which runs it after every migration) the NetBox container
  exited on the first start after each new migration, not only after the upgrade. Migration `0003` now removes the
  plugin's own leftovers during `migrate`: the content types of the six 0.1.x models 0.2.0 dropped and what still
  pointed at them. **This deletes the change log history of those 0.1.x objects** (their tables were already dropped
  in 0.2.0), along with their permissions, search index rows and jobs. `migrate` prints how many content types and
  change log entries it removed. A type is kept, with its change log, when removing it would leave a permission, tag,
  custom field or change control policy with no object type, or when a bookmark, notification, subscription or open
  branch still uses it; `migrate` names it. Content types of other apps, netbox-plugin-dhcp included, are not
  touched. With netbox-branching, the migration is skipped on branch schemas (`fake_on_branch`): content types live
  only in main.
- Applying import proposal items while working in a netbox-branching branch wrote the objects into the branch while
  main recorded them as managed, so the next push from main could delete them on the Windows server. It is now
  refused, with an error in the UI and 409 from the REST API: switch to main to apply. Dismissing still works in a
  branch.
- Importing one Windows server could move an IP range it shared with a scope of another server that is not its
  failover partner; the next push then rewrote that other server's scope range. The scope change now fails with a
  message naming the other scope. Import the failover relationship if the servers are partners, edit the shared IP
  range in NetBox when both servers got the same change, or point one scope at its own prefix and IP range in
  another VRF. A separate import VRF only keeps servers apart when set before their first import.
- Reservation IP addresses and a scope's first IP range are created in the VRF of the scope's prefix, not the
  server's import VRF, so a scope made by hand on a prefix in another VRF no longer attaches its reservations to an
  unrelated address.
- A scope that stayed in its failover relationship because it still had leases was treated as added by hand on
  Windows by later pushes, and never deleted. It is now deleted by a later push once its leases are gone. A scope
  already in that state must be removed from the relationship on Windows once.
- A push that hit an unexpected error lost the record of what it had already written. It now stops, saves the sync
  state and the summary, and reports the change as `unexpected error (<type>), see the worker log`.
- A change inside a scope that failed after part of it may have reached Windows now has its scope replicated to
  the failover partner (or kept in `replication_pending`). A failed server-level change (a class, an option
  definition, a MAC filter, a server option) replicates nothing until it is applied, unless the push stopped on it
  (lost connection, unexpected error): then all the relationship's scopes wait in `replication_pending` for the
  next push. A failed failover change triggers no replication.
- A scope delete no longer counts as done when only its removal from a failover relationship answered "not found":
  the scope itself is then deleted, or reported as failed.
- A missing PowerShell cmdlet was read as "object not found". It is now an error: a read no longer reports drift,
  and a push no longer counts a delete as done, because a cmdlet does not exist on the server.
- A WinRM command that never finishes is abandoned after ten times `command_timeout` (at least 10 minutes, so 20
  minutes with the default) with `winrm: PowerShell did not finish within N s`, instead of holding the job until
  the queue killed the worker.
- Imports get the same one hour job timeout as pushes (or netbox-branching's `job_timeout` when that is longer), so
  the first import of a large server no longer dies after five minutes.
- A push waiting for a busy server no longer sends a "completed" notification every minute: only the attempt that
  pushes, or the final failure, notifies. One exception: a waiting attempt that finds another push already queued
  for the server stops there and notifies once with its own setting, since that other push carries its changes.
- With netbox-branching, a scheduled import without a usable `import_user` fails before connecting to the Windows
  server instead of after reading it.
- An over-long or malformed name a server reports for itself no longer fails every connection test, import and push
  of that server: such names are ignored, and at most 32 names are kept.
- A connection that cannot be closed at the end of a job no longer leaves its sync run in the running state, and
  an import no longer fails when the connection cannot be closed after the server was read: a warning is logged
  instead.
- The import scheduler goes on with the other servers when one cannot be queued, and logs "Queued an import" only
  when one was queued.
- An option definition whose Windows type was typed in another case (`string`, `Dword`) blocked every push with
  "the type cannot change" and came back in every import. The type is now compared without case and stored in
  Windows' spelling.
- A policy whose IP ranges were not pairs of address strings (numbers, for example) could be saved and then stopped
  every push and import of its server. Such ranges are now refused on save, as are IPv6 ranges and ranges outside
  the policy's scope.
- Very long class or policy names (a sync key over 255 characters) made a push or an import fail with a database
  error. The key columns are now text (migration `0002`).
- A reservation client ID or DUID written without leading zeros (`0:1b:21:a:b:c`) was pushed as a different,
  shorter client ID. Each group is now padded to a byte, and ambiguous group widths are refused.
- Creating a scope whose IP range is not inside the scope's prefix is refused before anything is sent to Windows,
  instead of creating a different scope there.
- A MAC filter update that fails halfway puts the old entry back instead of leaving the address out of its list.
  If putting it back fails too, the error says that the address was left out and why.
- When a scope range move fails and the old range cannot be put back, the error names the range the scope was left
  at.
- Malformed list output from a server is reported as a Windows output error instead of an empty object or a
  traceback.
- Two reviewers acting on the same proposal item, or a new import replacing it meanwhile, no longer overwrite each
  other's decision or bring a replaced item back; the counts report only what the request changed. An item that
  was dismissed or replaced while an apply request was running is no longer written to NetBox or recorded as
  managed.
- A value the database refuses while one import change is applied now fails that change with its message, instead
  of stopping the import or answering HTTP 500.
- The review item for deleting a duplicate policy, superscope, failover relationship or MAC filter (the same name
  in another case) links and permission-checks the copy that is deleted.
- **Apply selected** and **Dismiss selected** with "select all matching" on a list whose filter is no longer valid
  (for example a deleted proposal) acted on every open item of every proposal. It now acts on nothing and says so.
- A REST `PATCH` with `"scope": null` on a reservation's option value no longer detaches the value from the
  reservation's scope.
- Searching option definitions or option values for a character such as `²` answered HTTP 500.

### Performance

- Applying proposal items reads only the kinds of NetBox objects the selected items are about and repeats fewer
  lookups, so applying a few items on a large server is much lighter.
- Option value, policy, exclusion range, reservation and failover lists (UI and REST) no longer run extra queries
  per row for the scope's server and prefix; the import proposal REST list no longer counts items it does not
  return.
- Branch merges and reverts read only the DHCP-related changes of the branch; creating a prefix, IP range, IP
  address or MAC address no longer runs a plugin lookup, and editing one runs one instead of two.
- Sync state rows are written in batches, which speeds up the first push after a large import.
- A database index serves the "latest run of a server" lookups of the import scheduler and the server page
  (migration `0002`).

### Documentation

- Upgrading from 0.1.x with netbox-docker: the upgrade notes explain the `ProtectedError` from the image's automatic
  `remove_stale_contenttypes` step (on 0.2.0 it comes back after every new migration), what migration `0003` removes
  and keeps, and how to start NetBox again by hand on 0.2.0 or for other apps' leftovers. The troubleshooting page
  points there.

## [0.1.4] - 2026-11-15

Security fixes backported from 0.3.0. 0.1.x stays on netbox-plugin-dhcp and gets fixes only.

### Security

- `allowed_hosts`: an address pattern such as `10.20.*.*` also matched DNS names like `10.20.evil.example`. Address
  patterns (four parts of digits and wildcards, or anything with a `:`) now match IP addresses only, and name
  patterns match names only. Check your profiles: a bare `*` no longer covers IPv6 addresses, and `*.*.*.*` is read
  as an IPv4 pattern, so it no longer matches four-label names. A shorter pattern of digits and wildcards that
  holds a number (`10.*`, `192.168.*`) is read as an address pattern too and matches nothing: an IPv4 pattern needs
  all four parts (`10.*.*.*`).
- Hosts written as numbers that a resolver reads as an IPv4 address (`2130706433`, `0x7f.1`, `127.1`, or any name
  whose last label is a number) are refused as `host` and `computer_name`, on save and before every connection. A
  Windows server already saved with such a host stops connecting until the host is corrected.
- A push refuses to create a failover relationship whose partner is outside the `allowed_hosts` of the primary
  server's credential profile: creating it makes the primary contact the partner as the plugin's account. If the
  profile lists only the primary, add the partner (its `computer_name` when set, else its `host`). Existing
  relationships, their updates and deletes, and profiles without `allowed_hosts` are not affected.
- An unexpected error while applying an import proposal item no longer sends change events (event rules, webhooks)
  for objects that were rolled back. This completes the fix for GHSA-5x5m-vqw4-wq85.
- WinRM no longer follows HTTP redirects, so the logon only goes to the host that was checked. A redirect is now
  an error.
- SSH: when the profile's `password` is the passphrase of an encrypted private key and the server refuses the key,
  the passphrase is no longer offered to the server as an account password. An encrypted key whose passphrase is
  also the account password loses that fallback.
- Hardening: the transport settings keep passwords and private keys out of their Python `repr`.

## [0.2.0] - 2026-11-13

Release candidates 0.2.0rc1 (2026-10-29) and 0.2.0rc2 (2026-11-13) went to PyPI for testing; what testing
them found is listed under Fixed and Security.

The plugin is standalone: it no longer needs netbox-plugin-dhcp. This is a fresh start, with no data migration from
0.1.x: read the upgrade notes before upgrading. Installs that stay on netbox-plugin-dhcp keep the 0.1.x line.

### Changed (breaking)

- The plugin is standalone: it no longer depends on netbox-plugin-dhcp and holds the whole Windows DHCP
  configuration in its own models. A Windows server has its own name; scopes (IPv4 and DHCPv6), superscopes,
  reservations, exclusion ranges, classes, option definitions, option values, policies, failover relationships, MAC
  filters and the list switches are plugin models. Scopes and reservations still point at core NetBox objects
  (prefix, IP range, IP address, MAC address), which imports create or reuse in the server's import VRF as before.
- Imported objects keep their Windows names: the `subnet_name_template` and `reservation_name_template` settings and
  the `<server>/<scope id>` names are gone. Lists and selectors still show a scope as `<server>/<scope id>` so scopes
  of different servers are told apart.
- Option values are lists, so values containing commas or leading and trailing spaces are imported and pushed like
  any other; the `\,` escape is gone. Standard options are named by their code.
- The Leases tab moved to the plugin's scope page.
- The **Windows DHCP** menu lists Superscopes, Scopes, Reservations, Option definitions and Option values; the
  "settings" entries (scope, reservation and option definition settings, option qualifiers, vendor option
  definitions and values) are gone.
- Object permissions name the new models (scope, reservation, option value...): permissions granted on
  netbox-plugin-dhcp's models or on the removed 0.1.x models no longer apply here.
- The REST endpoints and GraphQL queries follow the new models; the old ones (scope and reservation settings, option
  definition settings, option qualifiers, vendor option definitions and values) are gone.
- **No upgrade path from 0.1.x: 0.2.0 is a fresh start.** Its migration drops the plugin's 0.1.x tables, so the
  Windows servers' connection settings, sync state, sync runs and import proposals are lost. Data in
  netbox-plugin-dhcp is left untouched. Before upgrading, merge or delete open import branches; after it, recreate
  the Windows servers and import each one again. Remove `netbox_dhcp` from `PLUGINS` if it was there only for this
  plugin. See [Installation](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/installation/#upgrading-from-01x).
  To stay on netbox-plugin-dhcp, pin `netbox-windows-dhcp-plugin<0.2`: the 0.1.x line (branch `release/0.1`) keeps
  getting bug fixes, but no new features.

### Added

- Documentation: diagrams of the workflows: how the pieces fit together (home page), where an import writes
  and how proposal items move (Import), when a push runs, what it does and when it deletes, the branch review cycle
  (Push), and failover replication (Failover).
- Documentation: every NetBox screenshot retaken on 0.2.0 (Windows Server 2025 lab), and the step-by-step pages
  (new scope, reservations, failover, branching) walk through the 0.2.0 forms.
- Lease durations, DHCPv6 lifetimes and T1/T2 show in days next to the seconds on scope and policy pages, for
  example `28800 (0.33 days)`.
- A Windows server can be linked to the device or virtual machine it runs on, and that device or virtual machine's
  page lists its Windows servers.
- NetBox's global search finds superscopes, scopes, reservations and option definitions, and Windows servers by
  name.
- The reservation form creates its IP address and MAC address in place: a **+** button next to each field opens
  NetBox's add form in a pop-up, so a new reservation needs no trip to IPAM or Devices first. The scope form does
  the same for its prefix and IP range.
- Option values on reservations through the REST API (netbox-plugin-dhcp could not do it,
  [netbox-plugin-dhcp#69](https://github.com/sys4/netbox-plugin-dhcp/issues/69)).

### Changed

- pypsrp 0.9.1 or later is required (with the `credssp` extra, and with the plugin's `kerberos` extra).

### Fixed

- Option values and defaults now compare and fingerprint according to their definition type: numeric-looking
  String and Binary values remain exact. Values without a local definition use the effective Windows definition.
  Older option fingerprints must be refreshed by a matching push or accepted import before authorizing deletion
  (#73).
- Parent edits that would strand dependent options, policies, classes or failover memberships are refused with
  field errors instead of silently omitting their configuration from both servers. Safe moves remain supported
  (#74).
- Failover memberships protected because they changed on Windows remain protected across repeated pushes and
  unrelated relationship settings updates, while scopes NetBox added can still be removed from the same
  relationship (#75).
- A rolled-back save or branch merge (including a dry run) no longer leaves servers queued for a push that a later,
  unrelated commit in the same worker would send, or that would hide a later edit's own push.
- Direct edits of main still queue pushes while an unrelated branch is merging or reverting. Merge and revert
  replays retain their own attributed completion push (#76).
- Failover lease-preflight failures preserve earlier push progress, stop further I/O after transport loss, and
  prevent dependent scope deletions (#77).
- An existing failover relationship can be left empty by clearing its final membership; new empty relationships
  remain refused (#78).
- Scope and reservation names retain all 255 supported characters in both address families. Option-definition
  descriptions are no longer shortened to 200 characters (#79).
- A failover relationship whose scopes Windows already holds in another relationship (made by hand, or renamed on
  Windows) made every push fail with the cmdlet's error. It is now left out with a reason ("not pushed") and the rest
  of the push goes ahead.
- NetBox accepted an IPv4 reservation with neither a MAC address nor a client ID, or a DHCPv6 one without DUID or
  IAID, which the push could then only leave out ("not pushed"). The form, the REST API and CSV import now refuse
  them. Existing incomplete reservations are kept; editing one asks for the missing client.
- A push that failed on some changes logged "changed N object(s)" with the number planned. The job log now gives
  what was applied, failed and not attempted.
- A Windows server's Import panel no longer says differences wait for review once the import proposal has been
  applied or dismissed; it shows the proposal's status.

### Security

- [GHSA-5x5m-vqw4-wq85](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/security/advisories/GHSA-5x5m-vqw4-wq85)
  (fixed in 0.2.0rc2 and 0.1.3): failed import-proposal applications now discard events for rolled-back writes
  instead of dispatching actions for rejected changes. Successful items retain their events, lazy serialization and pre-existing snapshots.

## [0.1.3] - 2026-11-13

A security fix backported from 0.2
([GHSA-5x5m-vqw4-wq85](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/security/advisories/GHSA-5x5m-vqw4-wq85)).
0.1.x stays on netbox-plugin-dhcp and gets fixes only.

### Security

- Failed import-proposal applications now discard events for rolled-back writes instead of dispatching actions for
  rejected changes. Successful items retain their events, lazy serialization and pre-existing snapshots.

## [0.1.2] - 2026-10-29

Three fixes backported from 0.2. 0.1.x stays on netbox-plugin-dhcp and gets fixes only.

### Fixed

- A push that failed on some changes logged "changed N object(s)" with the number planned. The job log now gives
  what was applied, failed and not attempted.
- A Windows server's Import panel no longer says differences wait for review once the import proposal has been
  applied or dismissed; it shows the proposal's status.
- A failover relationship whose scopes Windows already holds in another relationship (made by hand, or renamed on
  Windows) made every push fail with the cmdlet's error. It is now left out with a reason ("not pushed") and the rest
  of the push goes ahead.

## [0.1.1] - 2026-10-29

Two fixes found on Windows Server 2025. 0.1.x stays on netbox-plugin-dhcp and gets fixes only; new features go to
0.2, which no longer needs netbox-plugin-dhcp.

### Added

- Documentation: a Reservations page (create, type, options, change, delete) and a step-by-step failover
  walkthrough, each with screenshots of NetBox and of the Windows DHCP console and PowerShell output, taken on two
  Windows Server 2025 servers.
- Documentation: Reservations through the API, an end-to-end script that creates a reservation with the REST API,
  waits for the push and deletes it again, with the calls, the output and the errors to expect.

### Fixed

- Pushing a DNS server option (6, or 23 for DHCPv6) no longer fails with "is not a valid DNS server" when the
  Windows server cannot query that address: option values are set with `-Force`, which skips the check.
- Adding or removing scopes in an existing failover relationship, or changing its settings, no longer fails with
  "Failover relationship ... is already in LoadBalance mode" (Windows Server 2025): the mode is sent only when it
  changes.

## [0.1.0] - 2026-10-27

Release candidates 0.1.0rc1 to 0.1.0rc5 (2026-10-21 to 2026-10-23) went to PyPI for testing; what testing them found
is listed under Fixed.

First release. Every v1 feature is in place and tested against the fake server and PowerShell stubs. The lab tests
against real Windows servers (`make test-lab`, `make lab-e2e`) ran in full on two Windows Server 2022 DHCP servers,
failover included; what they found is listed under Fixed, and the few details they could not reach are listed in
the documentation's limitations page.

### Added

Connections and security

- Windows server connection settings linked to netbox-plugin-dhcp servers: WinRM (NTLM, Basic, Kerberos or CredSSP, HTTPS by
  default; CredSSP is what failover writes need) or SSH, with an optional `computer_name` for a management host.
- Credential profiles in `PLUGINS_CONFIG`, each secret inline, from an environment variable or a file. Nothing secret
  is stored in the database, the change log or job logs.
- `allowed_hosts` on credential profiles limits which hosts their credentials can be sent to, checked when a Windows
  server is saved and before every connection. Basic authentication requires HTTPS with certificate checks. SSH
  always verifies the host key (pinned key or the worker's known_hosts).
- "Test connection" button and REST action, run as a background job.

Models and API

- Models for what netbox-plugin-dhcp does not hold: Windows scope and reservation settings (including the DHCPv6
  IAID), exclusion ranges, user and vendor classes, option definitions (Windows data type and default value),
  option qualifiers, vendor class option definitions and values, policies, failover relationships, MAC filters and
  the allow and deny list switches.
- Sync bookkeeping: sync state, sync runs and import proposals.
- REST API and GraphQL for every model. Names of classes, policies, superscopes and failover relationships compare
  without case, as on Windows.

Import (Windows to NetBox)

- IPv4 scopes, superscopes, exclusion ranges, reservations, user and vendor classes, option definitions, vendor class
  options, policies (conditions, IP ranges, processing order, lease time), failover relationships, MAC filters and
  option values at server, scope, reservation, user class and policy level. They become netbox-plugin-dhcp objects
  plus the prefixes, IP ranges, IP addresses and MAC addresses they need, in the server's import VRF. Objects missing
  on Windows are proposed for deletion.
- DHCPv6 scopes (lifetimes, T1 and T2, state, preference), reservations (DUID, IAID), user classes and option values
  against netbox-plugin-dhcp's standard DHCPv6 definitions. DHCPv6 vendor classes and values, other option codes and
  exclusion ranges are noted and left alone.
- Option values containing a comma, stored as `\,` (Kea's escape).
- With netbox-branching, the **Import** button is a menu: **Import into branch** (an import branch, the default),
  **Import into this branch** (the branch you work in, no new branch) and **Import directly** (into main, after a
  confirmation page, with your permissions checked object by object and change control's `protect_main` respected;
  the new `import_direct` setting turns it off). The REST import action takes the same choice as `destination`. Sync
  runs record where each import wrote (new **Destination** column and filter), and the Import panel says so while
  the import is queued, running and done (#36).
- With netbox-branching, each import writes into a branch `dhcp-import/<server>/<timestamp>` (reused while open) and,
  with netbox-change-control, opens a change request submitted for review.
- Without branching, each import fills an import proposal whose items are applied or dismissed one by one, in bulk
  or for the whole proposal, from the UI or the REST API. Applying writes as the reviewing user, checks their
  permissions, and refuses items whose NetBox object changed since the import.
- "Import now" button and REST action, and scheduled imports every `import_interval` minutes (`import_user` for
  scheduled imports with branching).

Push (NetBox to Windows)

- A reconcile per server that creates, updates and deletes every kind above, in dependency order, so Windows matches
  NetBox.
- Only objects recorded in sync state are deleted. Objects created by hand on Windows are left alone, and so are
  managed objects changed on Windows since the last push and objects whose removal would take unmanaged ones with
  them. Identical objects are adopted as managed; applying an import item adopts it straight away.
- NetBox data Windows cannot hold (a scope without a pool, a reservation without a MAC address, options 1 and 51,
  values of codes Windows does not define) is reported in the sync run instead of pushed. A scope subnet mask or an
  option definition type is never changed in place, and a scope with leases is never force-removed.
- With netbox-branching, merging or reverting a branch queues a push for each Windows server it touched. Other saves
  (all of them without branching, direct edits of main with it) queue one push per server after commit,
  `push_debounce` seconds later. `sync_enabled = False` stops pushes.
- "Sync now" and "Dry run" buttons and REST actions. A dry run records the planned changes in its sync run.
- Failover: relationships are written on the primary, with the shared secret read from the credential profile named
  in `shared_secret_profile`; the changed scopes are then replicated to the partner. A scope leaving a relationship
  leaves it before it is deleted, and a scope with leases is refused before anything changes.
- One import or push per server at a time across all workers (a PostgreSQL advisory lock); a busy push retries a
  minute later.

Leases

- A Leases tab on the subnet page of Windows scopes (IPv4 and DHCPv6) reads the live leases in a background job.
  Viewing needs the subnet and its Windows server, loading needs permission to change the server, and is refused
  inside a branch. Leases are not stored.

Documentation

- A documentation site (https://thomaschristory.github.io/netbox-windows-dhcp-plugin/): installation, preparing
  Windows, configuration, a first-sync walkthrough, how objects map, import, push, DHCPv6, failover, leases,
  permissions, REST API and GraphQL, security, workers and jobs, troubleshooting and limitations.
- Screenshots from a lab run on real Windows servers: the first-sync walkthrough step by step, import proposals and
  import branches with change requests, a new scope built in NetBox and pushed, a failover relationship, the Leases
  tab and a failed push.
- Installation: netbox-change-control needs at least one policy, or change requests (an import's included) can never be
  approved. How objects map: netbox-plugin-dhcp 0.2.0 does not save comments from its edit forms
  (sys4/netbox-plugin-dhcp#68).
- `SECURITY.md`: report vulnerabilities through GitHub's private vulnerability reporting.

Development

- `dev/scripts/lab-server-setup.ps1` prepares a Windows Server VM as the lab server (DHCP role, a least-privilege
  account, WinRM over HTTPS).
- Opt-in lab tests against a real Windows DHCP server: `make test-lab` (the client over WinRM, one round trip per
  unverified cmdlet) and `make lab-e2e` (import, push and leases on the dev stack). They only touch `nbwd-test-`
  objects in `198.18.0.0/15` and `2001:db8:4e42::/64`.

### Fixed

- The Windows servers list links each server's name to its Windows server page (actions, Import panel, sync runs),
  as the sync run pages do, saved table layouts included; the generic netbox-plugin-dhcp server stays linked in a
  new **Generic DHCP server** column (#38).
- The plugin's buttons (Test connection, Sync now, Dry run, Import now, and Apply all, Dismiss all, Apply and Dismiss
  on the review queue) were rendered without a CSRF token, so clicking them failed with "CSRF verification failed"
  (403). They now carry the token (#19).
- Read-only pages (sync runs, import proposals and their items) showed Edit, Clone, Add, Import or Edit Selected
  buttons that led to a "None" address and a 404. Pages now show only the buttons whose views exist; Delete and the
  review actions stay. Lists without CSV import or bulk edit no longer offer those buttons either (#23).

Found by a first, partial lab run against Windows Server 2025:

- A Windows object that does not exist is reported as missing again, instead of as an object with every field empty.
  Windows PowerShell 5.1 printed "nothing found" as `{}`; the script envelope now turns it into `null`, and the
  client also reads `{}` as missing. This affected MAC filters, superscopes, policies, DHCPv6 scopes, classes,
  option values and every other lookup (#15).
- Renaming a superscope uses the cmdlet's real `-Name` parameter instead of `-SuperscopeName`, which Windows rejected
  (#14).
- Option values on an IPv4 reservation are written and removed with `-ReservedIP` alone: Windows refuses `-ScopeId`
  next to it, so a push of a reservation option failed (#20).
- Option values on a DHCPv6 reservation are written and removed with `-ReservedIP` alone, for the same reason with
  `-Prefix` (#17).
- DHCPv6 leases no longer read an `AddressState` property Windows does not have; the Leases tab shows their state as
  n/a, since Windows reports none for DHCPv6 (#22).
- The policy lab test uses a condition Windows accepts (`*.Example.Test`), checks FQDN policies apart from policy IP
  ranges and option values (which Windows refuses on FQDN policies, DHCP 20135 and 20136), and the fake server now
  refuses a wildcard in the middle of a condition, as Windows does (#18).
- A new DHCPv6 scope pushed without renewal and rebinding times now gets Windows' own default rebinding time (T2),
  6.4 days (6 days 9:36), instead of 6 days 10 hours, so it matches a scope made on Windows without them (#16).
  Renewal and rebinding times left empty in NetBox now follow the scope's preferred lifetime (half and 80 % of it,
  which gives Windows' 4 and 6.4 days for the default 8 days), so a scope with a preferred lifetime shorter than
  6.4 days no longer gets timers past it, which Windows documents as invalid. An existing scope keeps its Windows
  timers while they still fit below the preferred lifetime. When only one of T1 and T2 is set in NetBox, the empty
  one is now worked out against it (T1 at 5/8 of T2, or T2 halfway between T1 and the preferred lifetime, when the
  default would not fit), instead of a default that could land on the wrong side and be refused by Windows.
- Policy conditions Windows stores in its own spelling no longer show as changed after every push or import: a MAC
  prefix such as `02-4e-42-ab*` (read back as `02-4e-42-ab-*`) and a host name in mixed case (read back in
  lowercase) now compare equal, so a second push is a no-op (#21). Upgrading from 0.1.0rc1: a policy whose condition
  NetBox spells differently from Windows was recorded under its old spelling, so until the next push records it
  again, deleting it in NetBox is reported as changed on Windows and not deleted. Push once after upgrading.
- A push now creates and updates the policies of each level in ascending processing order instead of by name, so
  recreating policies whose names sort the other way no longer fails with DHCP 20110. Policies Windows moves down
  when another takes their number are set back in the same push, and a processing order that would leave a gap is
  reported in the plan instead of failing on Windows (#24). After a policy is deleted in NetBox without renumbering
  the ones after it, the push no longer moves the others into the wrong order: it leaves that level as Windows holds
  it and asks for the renumbering. Policies Windows renumbers by itself (after a delete, for example) are no longer taken for
  policies changed on Windows when NetBox later deletes them: the processing order is no longer part of what the
  push compares to notice hand edits.

Found by testing 0.1.0rc2:
- The Windows server page has an **Import** panel saying whether the latest import is queued, running or finished
  and where it went. With branching it names and links the import branch and its change request and says main has
  not changed; without branching it links the import proposal and does not mention branches. It refreshes itself
  while the job runs, and the sync run page shows the same for its own run. The message after **Import now** also
  says where the import will go (#32).
- Changing a scope's pool so that one end grows and the other shrinks (for example `.10-.200` to `.11-.210`) no
  longer fails with DHCP 20023, which Windows returns for that change in one call. The push changes the range in
  two steps: it first narrows to the addresses both pools share (or, when they do not overlap, widens over both),
  then sets the final range. If the second step fails, it tries to put the old range back (#30).
- Updating a hot standby failover relationship no longer fails with "A parameter cannot be found that matches
  parameter name 'ServerRole'": `Set-DhcpServerv4Failover` has no such parameter, so the push no longer sends it.
  Windows cannot change the role (active or standby) in place, so a push that changes it is refused with a message
  saying to recreate the relationship. After switching a relationship to hot standby, the push checks the role
  Windows chose against NetBox. Mode changes no longer wait for a confirmation nobody can answer (#33).
- The policy lab test allows the lease time option (51) Windows lists next to a policy's own option values (#29).
- The missing-module unit test no longer fails on a host with the Windows DHCP management tools (RSAT) installed: it
  runs PowerShell with an empty module path (#31).

Found by testing 0.1.0rc3:
- A reservation description or name change no longer drops its option values on Windows (IPv4 and DHCPv6, #42).
  The push only sends the client ID, type, DUID or IAID when they change, and puts back any reservation option value
  Windows removed during the update. The dry run is unchanged: it still lists only the reservation update. If a value
  cannot be put back, the reservation change is reported as failed, naming each lost option and saying whether it
  comes back by itself: the next push recreates a value NetBox holds, but values NetBox does not hold (DHCPv6 vendor
  class values, option codes the plugin does not sync, values added on Windows since the last import) must be put
  back by hand.
- Without branching, an import that finds only differences a reviewer already dismissed no longer reports "No
  differences found." The Import panel and the sync run now say that nothing new waits for review and how many
  dismissed differences stay ignored while Windows shows the same values. The count of differences waiting for review
  no longer includes dismissed ones, a proposal you may not view is no longer read as "no differences", and the sync
  run summary and job log record how many differences were dismissed earlier (#43). An earlier run whose proposal a
  later import took over (or that was deleted) says so and links the server's open proposal instead of claiming a
  proposal you may not view, and a run recorded before this fix with no proposal linked says only how many
  differences it found.
- The Windows scopes list shows each scope's prefix in its own column, next to the subnet name, as the
  netbox-plugin-dhcp subnet list does, so scopes with similar or repeated names are told apart without opening them.
  The column links to the prefix and sorts by network (#47).
- With branching, the Import panel no longer calls a merged or deleted branch "the open import branch", on the
  Windows server page or on the sync run of an earlier import. It says what became of the branch: being merged,
  merged into main (with the time), merged and since deleted, or deleted without being merged, read from the branch
  or from its deletion in the change log. With no record you may view, it says the outcome is not known rather than
  guessing a merge. A merged import no longer reads as "main matches Windows", the deleted branch row links its
  deletion record, and the server panel links the server's import history (#44).

Found by testing 0.1.0rc4:
- Failover replication after a push to the primary no longer fails with `AmbiguousParameterSet`. The plugin passed
  both `-Name` and `-ScopeId` to `Invoke-DhcpServerv4FailoverReplication`, which takes one or the other; it now
  replicates the changed scopes with `-ScopeId` only, never widening the copy to the whole relationship (#51).
- A failed failover replication is retried by the next push to the primary, even one with nothing else to write, so
  the partner no longer stays behind until the next change. The push's sync run lists the scopes still to replicate.
  A scope that is not yet in the relationship on Windows waits for a later push instead of blocking the others.

Found by the full lab run (two Windows Server 2022 servers; earlier notes said 2025, which is what the rc testers
ran):
- Over SSH, about one PowerShell run in fifteen hung until the timeout: Windows OpenSSH sometimes loses the end of
  stdin, and the script loader read stdin to its end. It now reads the one line the script is sent as. Each hung
  run also kept its SSH session open, so after a few the server refused new ones (#56). Windows OpenSSH also stalls
  on more than about 70 KB of input: the SSH transport refuses a script above 64 KB at once, saying to use WinRM, and
  a run that stays silent past the command timeout now says so instead of giving an empty error.
- Failover relationships could not be created over WinRM: `Add-DhcpServerv4Failover` asked for a confirmation nobody
  can answer ("PowerShell is in NonInteractive mode"). Add, Set and replication now send `-Force`. The removes never
  do: they do not ask while the relationship is healthy, and with the partner unreachable `-Force` would remove the
  relationship or scope on one server only. Such a push now fails with "not in NORMAL state" and changes nothing.
- Failover writes need the DHCP server to reach its partner as you, which NTLM and Kerberos cannot pass on
  (`PermissionDenied`). The new **CredSSP** WinRM authentication makes them work; see the failover page. CredSSP
  hands the password to the server, so it requires HTTPS with certificate verification, unless the credential
  profile sets `allow_insecure_credssp` (for labs).
- An import or push no longer fails when a scope disappears while the server is being read (for example the partner's
  copy, when a failover relationship is removed at that moment): that scope counts as gone, the rest is read. The
  sync run's notes and the job log name each scope left out this way (#58).
- A failover relationship imported with message authentication on made every later push to that server fail: the
  import cannot read the secret, so NetBox had no shared secret profile and each push tried to turn authentication
  off, which Windows refuses. Without a profile NetBox now leaves authentication as Windows has it; a profile still
  turns it on with that secret. Found while taking the documentation screenshots.
- A sync run's page listed every import proposal of every server instead of its own (the filter it used did not
  exist).
- The Kerberos SPN help text read "Defaults to WSMAN/": the host placeholder was dropped as an HTML tag.
- A failover partner Windows names by its computer name now matches a server NetBox knows by IP address, and one it
  names by address matches a server NetBox knows by name (#57). Windows names the partner by its computer name on one
  side of a relationship, so that side's import failed and its pushes saw a partner change Windows cannot make. Test
  connection, imports and pushes now record the DHCP server's computer name, DNS name and addresses (**Reported
  names**, migration 0006, cleared when the host or computer name changes), and partners match on them as well as on
  the host. A name two servers report matches neither, and the error says so.
