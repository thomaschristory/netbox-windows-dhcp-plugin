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

### Upgrading from 0.1.x

0.1.x kept scopes, reservations and options in netbox-plugin-dhcp (sys4) and only added Windows settings next to
them. From 0.2.0 the plugin holds the whole Windows DHCP configuration in its own models and no longer uses sys4.
There is no data migration: **0.2.0 is a fresh start**, and the Windows servers themselves are the source to rebuild
from. Nothing on Windows changes during the upgrade.

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
5. **Clean up content types**: `python manage.py remove_stale_contenttypes`, which removes the entries of the
   dropped models (and of sys4's, if you removed it).
6. **Restart** NetBox and the worker.
7. **Recreate the Windows servers** (**Windows DHCP > Windows servers > Add**, now with a name of their own), run
   **Test connection**, and **import each one**. Review and merge (or apply) the import. Import the primary of each
   failover relationship first (see [Failover](failover.md#how-it-syncs)).

The prefixes, IP ranges, IP addresses and MAC addresses that 0.1.x imports created stay in IPAM, and the new
imports reuse the ones they find in the server's import VRF. Sync state starts empty, so the first push after the
import adopts the objects that are identical on both sides and manages nothing else until then (see
[Managed objects](push.md#managed-objects)). Leave **Sync enabled** off and run a **Dry run** before the first push.

## Removing it

1. Remove `netbox_windows_dhcp_plugin` from `PLUGINS` and restart NetBox and the worker. Nothing is pushed to or
   removed from Windows.
2. To drop its tables, follow NetBox's guide to removing a plugin. This deletes the DHCP configuration the plugin
   holds (scopes, reservations, options and the rest). The prefixes, IP ranges, IP addresses and MAC addresses that
   imports created are core NetBox objects and stay.
