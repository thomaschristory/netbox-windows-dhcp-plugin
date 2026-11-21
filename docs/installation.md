# Installation

## Requirements

- NetBox 4.7.
- Python 3.12, 3.13 or 3.14.
- A NetBox background worker (`rqworker`). Every connection to Windows runs in a job.
- PostgreSQL connections from the worker that do not go through a transaction-pooling proxy (pgbouncer in
  transaction mode): the plugin holds a PostgreSQL advisory lock per Windows server while an import or push runs,
  and that lock belongs to the database session.

Optional:

- [netbox-branching](https://github.com/netboxlabs/netbox-branching) 1.2.2 or later: imports become branches and
  pushes follow merges.
- [netbox-change-control](https://github.com/antoinekh/netbox-change-control) 0.6 or later (needs branching): each
  import branch gets a change request, and merges need approval.
- Kerberos: system GSSAPI libraries (for example `libkrb5-dev` and `krb5-user` on Debian or Ubuntu) and a
  `krb5.conf` on the NetBox hosts.

## Install the package

Install it into NetBox's virtual environment, and add it to `local_requirements.txt` so NetBox upgrades keep it:

```bash
source /opt/netbox/venv/bin/activate
pip install netbox-windows-dhcp-plugin
echo netbox-windows-dhcp-plugin >> /opt/netbox/local_requirements.txt
```

With Kerberos support:

```bash
pip install "netbox-windows-dhcp-plugin[kerberos]"
```

With netbox-branching and netbox-change-control:

```bash
pip install "netbox-windows-dhcp-plugin[branching]"
```

For the [netbox-docker](https://github.com/netbox-community/netbox-docker) image, add the same names to
`plugin_requirements.txt` and rebuild the image, as its documentation describes.

## Enable it

In `configuration.py`, enable the plugin:

```python
PLUGINS = ["netbox_windows_dhcp_plugin"]
```

With branching and change control, `netbox_branching` must come last, and NetBox needs its schema-aware database
settings and router:

```python
from netbox_branching.utilities import DynamicSchemaDict

PLUGINS = ["netbox_windows_dhcp_plugin", "netbox_change_control", "netbox_branching"]
DATABASES = DynamicSchemaDict({"default": {...}})  # your existing database settings
DATABASE_ROUTERS = ["netbox_branching.database.BranchAwareRouter"]
```

The plugin detects branching and change control when NetBox starts. It works the same way without them, with
an [import proposal](import.md#without-branching-import-proposals) queue instead of branches and a push after each save.

!!! warning "Change control needs a policy"
    netbox-change-control approves a change request only through its **policies**. Without one, a request (an import's
    included) stays in **Needs review** whatever its reviews say, and its branch can never be merged. Before the first
    import, create at least one policy under **Change Control > Policies**, with a rule naming who approves, for
    example one review from the network team. A policy with no object types applies to every branch. A request
    opened before the policy existed picks it up when it is returned to draft and submitted again.

Then add at least one credential profile, as described in [Configuration](configuration.md).

## Migrate and restart

```bash
cd /opt/netbox/netbox
python manage.py migrate
sudo systemctl restart netbox netbox-rq
```

Restart the worker as well as the web service: the worker runs every job, and it reads the same configuration.

## Check it

- The navigation menu has a **Windows DHCP** entry.
- The NetBox log has no line saying `credential profiles without allowed_hosts can be sent to any host`. If it has
  one, add `allowed_hosts` to the profiles it lists (see [Configuration](configuration.md#allowed_hosts)).
- **Operations > Jobs** lists a job called "Windows DHCP import scheduler", which NetBox runs every minute and which
  queues the [scheduled imports](import.md#scheduled-imports).

## Upgrading

Install the new version, run `migrate`, and restart NetBox and the worker. Read the [changelog](changelog.md) first:
while the plugin is below 1.0, a minor version can change behaviour.

### Upgrading from 0.6.x to 0.7.0

No migration. WinRM now ignores the worker's proxy environment variables (`HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`) and connects straight to each DHCP server. If your worker reaches the DHCP servers through such a proxy
on purpose, add `"proxy": "env"` to the credential profiles concerned before upgrading (and to the failover shared
secret profiles used with those servers); see [Configuration](configuration.md#proxy). If you had set `NO_PROXY`
for the DHCP servers, it is no longer needed. SSH is unchanged.

Credential profiles also take a new optional key, `require_verified_https` (see
[Configuration](configuration.md#pinning-transport-port-realm-and-host-key)); without it nothing changes. NetBox now
logs a warning at startup for profile keys the plugin does not know, and the plugin requires `requests` 2.32.4 or
later (pip upgrades it with the plugin).

### Upgrading from 0.6.0 to 0.6.1

No migration. A policy whose MAC address condition Windows refuses (colons) or reads as other devices (a byte after
the first written as one digit, half a byte before the wildcard: `02-4e-42-a*` matches `00-24-e4-2a-*` on Windows)
is shown as not pushed, and the deletes of its part of the configuration wait until it is fixed. A branch merge or
revert that brings such a condition back still goes through; the push then names the policy. A dry run lists such policies:
edit the condition so each byte is two hex digits (`02-4e-42-0a-*`, say) and push again. See
[Mapping](mapping.md) for the rules.

### Upgrading from 0.5.x to 0.6.0

No migration. Credential profiles take four new optional keys, `transports`, `ports`, `kerberos_realms` and
`ssh_host_keys` (see [Configuration](configuration.md#pinning-transport-port-realm-and-host-key)); without them
nothing changes, except:

- **Failover secrets over insecure CredSSP.** A failover shared secret profile used with a server on CredSSP without
  HTTPS and certificate verification (a lab) now needs `"allow_insecure_credssp": True` too, like the server's own
  profile. Creating a relationship, or changing its secret profile or primary server, is refused on save until then;
  other edits of existing relationships still save as far as this rule goes (the secret profile's `allowed_hosts` is
  still checked on every save, as before), but the push fails the failover changes that send the secret.
- **Stored values outside the choices.** A Windows server whose transport, WinRM authentication or failover
  authentication was written straight to the database with other casing (`KERBEROS`, `Basic`) is refused when
  connecting: edit it in NetBox and pick the value again.

### Upgrading from 0.4.x to 0.5.0

0.5.0 brings one migration, applied by `migrate`: `0006` adds **Failover authentication** to Windows servers, empty
(same as WinRM authentication) for every existing one, so nothing changes until you set it. Then:

- **Kerberos with failover.** A failover server no longer needs CredSSP for every call: keep its WinRM
  authentication (Kerberos, say) and set **Failover authentication: CredSSP**, then run **Test connection**, which
  checks both logons. See [Failover](failover.md#connecting-to-the-servers).
- **SSH servers.** A script over 32 KB now goes to the server as a file over SFTP instead of failing, so the
  OpenSSH `sftp` subsystem must stay enabled (the Windows default). Most scripts stay under that size.

### Upgrading from 0.3.0 to 0.4.0

0.4.0 brings two migrations, applied by `migrate`:

- `0004` adds the push trail ("Triggered by" on sync runs). It changes no data.
- `0005` adds two indexes on sync runs.

Check these before upgrading (the [changelog](changelog.md) has the full list):

- **Old sync runs are deleted.** The import scheduler now deletes sync runs older than `sync_run_retention_days`
  (90 by default), keeping the ones the plugin still reads and those with a journal entry. Set it to `0` before
  upgrading to keep every run. See [Sync run retention](workers.md#sync-run-retention).
- **REST API, large reviews.** Applying or dismissing more than `review_job_threshold` (100) open proposal items
  through the API now answers `202` with a background job instead of `200` with the counts, and `409` while one runs
  for the same proposal. Update clients that apply large proposals, or raise the setting. See [API](api.md).
- **`max_concurrency` takes effect.** A value left in `PLUGINS_CONFIG`, which did nothing until now, now sets how
  many scopes a read sends at once (4 by default). Keep it within your WinRM (`MaxShellsPerUser`, 30) and OpenSSH
  (`MaxSessions`, 10) limits, or set it to `1` for the old behaviour. See
  [Workers](workers.md#timeouts-and-concurrency).
- **IPAM edits need the plugin's permission.** Changing the prefix, the range start or end, the address or the MAC
  of an IPAM object a scope or reservation uses now needs `change_scope` or `change_reservation` on it. Grant it to
  users who renumber in IPAM. See [Editing IPAM objects used by DHCP](ipam-edits.md).
- **Deletes wait while something cannot be pushed.** A push leaves out, with a reason, what Windows would refuse, and
  while anything of a family (the IPv4 scopes and what hangs off them, the DHCPv6 scopes, the server-level policies)
  is left out, it deletes nothing in that family. Check the first dry run after upgrading: it lists what waits and
  why. See [Push](push.md).
- **Two Windows server rows for one DHCP server** (same host, port and remote server) can no longer be edited until
  one of them is deleted or pointed elsewhere.

### Upgrading from 0.2.0 to 0.3.0

0.3.0 brings two migrations, applied by `migrate`:

- `0002` turns the sync key columns into text and adds an index on sync runs. It changes no data.
- `0003` only does something on a database upgraded from 0.1.x: it removes the content types of the 0.1.x models
  that 0.2.0 dropped, and the change log entries of their objects (see [With netbox-docker](#with-netbox-docker)).

0.3.0 also refuses some settings that 0.2.0 accepted. Check these before upgrading (the
[changelog](changelog.md) has the full list, under Changed and Security):

- **`allowed_hosts`** is read more strictly: an address pattern only matches IP addresses and a name pattern only
  matches names. A bare `*` no longer covers IPv6 addresses, `*.*.*.*` no longer matches four-label names, and an
  IPv4 pattern needs all four parts (`10.*.*.*`, not `10.*`). See [Configuration](configuration.md#allowed_hosts).
- **Hosts written as numbers** that a resolver reads as an IPv4 address (`127.1`, `2130706433`, or a name whose last
  label is a number) are refused as `host` and `computer_name`; a server saved with one stops connecting until the
  host is corrected.
- **Failover partners**: a push only creates a failover relationship when the partner is in the `allowed_hosts` of
  the primary server's credential profile (when that profile has `allowed_hosts`).
- **Credential values set to `None`** (for example `os.environ.get()` of a missing variable) are a configuration
  error instead of the text `None`.

### Upgrading from 0.1.x

0.1.x kept scopes, reservations and options in netbox-plugin-dhcp and only added Windows settings next to them. From
0.2.0 the plugin holds the whole Windows DHCP configuration in its own models and no longer uses netbox-plugin-dhcp.
There is no data migration: **0.2.0 is a fresh start**, and the Windows servers themselves are the source to rebuild
from. Nothing on Windows changes during the upgrade.

To stay on netbox-plugin-dhcp instead, pin `netbox-windows-dhcp-plugin<0.2`. The 0.1.x line (latest 0.1.4, branch
`release/0.1` in the repository) keeps getting bug fixes, but no new features.

1. **Merge or delete open import branches** (with netbox-branching). Their changes are written against the 0.1.x
   models and cannot be merged after the upgrade.
2. **Upgrade the package** in NetBox's virtual environment (`pip install -U netbox-windows-dhcp-plugin`, or the
   version in `local_requirements.txt` or `plugin_requirements.txt`).
3. **Update `PLUGINS`.** Remove `netbox_dhcp` if it was there only for this plugin. Its data (servers, subnets,
   reservations, options) is left untouched in the database; uninstall netbox-plugin-dhcp separately, following its
   own instructions, if nothing else uses it.
4. **Migrate**: `python manage.py migrate`. The new migration drops the plugin's 0.1.x tables: the Windows servers'
   connection settings, sync state, sync runs and import proposals are lost, along with the 0.1.x Windows settings
   (scope and reservation settings, exclusion ranges, classes, policies, failover relationships, MAC filters).
5. **Clean up content types** (optional): `python manage.py remove_stale_contenttypes` removes the entries NetBox
   keeps for models that no longer exist. From 0.3.0, `migrate` already removes the plugin's own 0.1.x entries,
   together with **the change log entries of the 0.1.x objects** (whose tables 0.2.0 dropped), so the command only
   has other apps' leftovers to deal with. It skips the apps that are no longer installed (netbox-plugin-dhcp once
   removed from `PLUGINS`) unless you add `--include-stale-apps`, and it can then stop on their change log entries;
   those leftovers are harmless and can stay.
   With netbox-docker, see [the note below](#with-netbox-docker): the container runs this step itself.
6. **Restart** NetBox and the worker.
7. **Recreate the Windows servers** (**Windows DHCP > Windows servers > Add**, now with a name of their own), run
   **Test connection**, and **import each one**. Review and merge (or apply) the import. Import the primary of each
   failover relationship first (see [Failover](failover.md#how-it-syncs)).

The prefixes, IP ranges, IP addresses and MAC addresses that 0.1.x imports created stay in IPAM, and the new
imports reuse the ones they find in the server's import VRF. Sync state starts empty. Objects become managed when you
apply the import proposal (or import straight into main). After a branch import, the first push adopts the objects
that are identical on both sides; until then a push deletes nothing (see [Managed objects](push.md#managed-objects)).
Leave **Sync enabled** off and run a **Dry run** before the first push.

#### With netbox-docker

The netbox-docker image migrates the database itself when the container starts and, whenever a migration was
applied, then runs `remove_stale_contenttypes`, `clearsessions` and `reindex --lazy`. Its start script stops at the
first error.

**On 0.2.0**, on a database upgraded from 0.1.x, the content type cleanup fails on the change log entries of the
0.1.x models, so the NetBox container exits with a `ProtectedError` like this one on the first start after the
upgrade, and again on the first start after **every later migration** (of NetBox, of this plugin or of any other
plugin):

```text
django.db.models.deletion.ProtectedError: ("Cannot delete some instances of model 'ContentType' because they are
referenced through protected foreign keys: 'ObjectType.contenttype_ptr', 'ObjectChange.changed_object_type'.", ...)
```

**From 0.3.0**, migration `0003` removes the plugin's own leftovers during `migrate`: the content types of the six
0.1.x models that 0.2.0 dropped (`WindowsScope`, `WindowsReservation`, `WindowsOptionDefinition`,
`WindowsOptionQualifier`, `WindowsVendorOptionDefinition`, `WindowsVendorOption`) and everything that still pointed
at them: the change log entries of those objects, their permissions, search index rows and jobs, and their place in
lists of object types that keep at least one other type (permissions, tags, custom fields, change control policies).
**That change log history is deleted**; the objects themselves were already gone since 0.2.0. `migrate` prints
how many content types and change log entries it removed. Upgrading to 0.3.0 or later is therefore enough: the first
start applies `0003`, the cleanup that follows succeeds and NetBox comes up.

The migration leaves a type alone, change log included, when removing it would change something an administrator
set up: when it is the last object type of a permission, tag, custom field, change control policy or similar (an
empty list can mean "every type"), or when a bookmark, a notification, a subscription or an open branch's changes
still use it. `migrate` then prints `Kept the old type ...` with the reason. Edit or delete what it names (merge or
delete the old branch), then run `migrate netbox_windows_dhcp_plugin 0002` and `migrate` again to retry.

If the container still exits, on 0.2.0 or because of another app's leftovers, the migrations have already been
applied at that point, so nothing is lost:

1. Start the stack again (`docker compose up -d`; it also starts the worker, which waits for NetBox). With the
   migrations done, the start script skips the cleanup steps and NetBox comes up. On 0.2.0 this happens again after
   the next migration, until you upgrade.
2. Run the two steps it skipped:

    ```bash
    docker compose exec netbox /opt/netbox/netbox/manage.py clearsessions
    docker compose exec netbox /opt/netbox/netbox/manage.py reindex --lazy
    ```

The content types left behind are harmless, as in step 5.

## Removing it

1. Remove `netbox_windows_dhcp_plugin` from `PLUGINS` and restart NetBox and the worker. Nothing is pushed to or
   removed from Windows.
2. To drop its tables, follow NetBox's guide to removing a plugin. This deletes the DHCP configuration the plugin
   holds (scopes, reservations, options and the rest). The prefixes, IP ranges, IP addresses and MAC addresses that
   imports created are core NetBox objects and stay.
