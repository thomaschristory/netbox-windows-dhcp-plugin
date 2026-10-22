# Import from Windows

An import reads the whole configuration of one Windows server, compares it with what NetBox holds for that server,
and brings the differences into NetBox **for review**. It never writes to Windows.

## Starting an import

- **Import now** on the Windows server page (with netbox-branching, the **Import** menu, see
  [below](#choosing-where-an-import-writes)), or `POST /api/plugins/windows-dhcp/windows-servers/<id>/import/` (see
  [REST API](api.md)). Runs as the user who asked.
- **On a schedule**, see [below](#scheduled-imports).

Only one import per server runs at a time, and a new one is not queued while another is queued or running. An
import and a push of the same server never run at the same time: an import that finds a push running fails (run it
again later), while a push that finds an import running retries (see [Workers and jobs](workers.md#one-job-per-server)).

## What is compared

Everything the plugin syncs: classes, option definitions, scopes, superscopes, exclusion ranges, reservations,
policies, option values, failover relationships, MAC filters and the list switches, for IPv4, and scopes,
reservations, user classes and option values for DHCPv6 (see [DHCPv6](dhcpv6.md)). Each difference is a **create**,
**update** or **delete** of one object, in dependency order (a scope before its reservations).

- **Create**: on Windows, not in NetBox.
- **Update**: on both sides, with different values.
- **Delete**: in NetBox for this server, not on Windows. This includes objects created in NetBox that no push has
  written yet (sync disabled, or a failed push): dismiss those.

Some Windows data is left out and noted in the sync run instead: DHCPv6 vendor classes and their values, DHCPv6
exclusion ranges, DHCPv6 values of codes sys4 has no standard definition for, and option values with leading or
trailing spaces (sys4's comma-separated storage would lose the spaces). A failover relationship is imported from
one partner only: the first one imported becomes its primary (the server the plugin writes to), and the other
partner's imports leave it alone. Import the server you want to write to first.

## Choosing where an import writes

Without netbox-branching an import always writes an [import proposal](#without-branching-import-proposals). With it,
the **Import** button on the Windows server page is a menu:

| Choice | Writes into | Review |
|---|---|---|
| **Import into branch** (the default) | the server's open import branch, or a new one | the branch (and its change request) is reviewed and merged; main is not changed by the import |
| **Import into this branch** (only while you work in a branch) | the branch you work in; no new branch, no change request | the branch, like your other edits in it |
| **Import directly** | main | none: the changes are in main once the job has run |

**Import directly** first opens a page that names main as the destination and, when you work in a branch, says the
import ignores that branch. If an import branch for the server is still open, it says so too: importing into main
as well writes the same objects twice, so merging that branch afterwards conflicts. Merge or delete it first, or
import into it instead. Nothing is queued until you confirm. It is shown disabled, with the reason, when
`import_direct` is off or when change control's `protect_main` covers DHCP and IPAM objects and you do not have
`netbox_change_control.bypass_policy`.

**Import into this branch** names your branch in the request; if you have switched to another branch (or to main)
in the meantime, the import is refused rather than sent somewhere else.

The destination goes with the job, so it does not depend on the branch the worker or your browser has active later,
and it is checked again when the job starts. A destination that is no longer allowed fails the import with the
reason; it never falls back to another one. Scheduled imports always use an import branch.

Imports into main or into your own branch write as you: every object is checked against your permissions as it is
saved, and a change you may not make (or that change control refuses) is rolled back and listed in the sync run,
while the others stay. Event rules and webhooks only hear about the changes that stayed. New subnets get subnet IDs
above those in main, in open import branches and in branches an import wrote into by choice, so merging any of
them later does not collide. Your branch is checked again just before the import writes into it: if it has started
merging (or is otherwise no longer ready) by then, nothing is written. An import into main queues no push (NetBox then matches Windows) and records the objects it
wrote as managed, as applying a proposal does.

The **Import** panel and the sync run say where each import went: the branch and its change request, the branch
you chose, or main. The **Destination** column of the sync run list shows it for every import, and can be filtered.

## With netbox-branching: import branches

When the import finds differences, it creates a branch named `dhcp-import/<server>/<timestamp>`
(`import_branch_prefix` changes the first part), provisions it, and writes the differences into it with change
tracking, so the branch's diff shows every change.

- With netbox-change-control and `import_change_request` on (the default), it also opens a **change request** for
  the branch and submits it for review.
- If an open import branch for the server already exists, the next import writes into it instead of opening another,
  so it only adds what changed since. A branch is no longer reused once its change request is completed or
  abandoned, or once it is stale (too far behind main to sync).
- An import with no differences creates no branch.
- netbox-branching's `max_working_branches` applies: when it is reached, the import fails and says so.

The Windows server page has an **Import** panel. While the import is queued or running it says so, and refreshes
itself until the job ends. Once it has finished it names the branch the import wrote into, with links to the branch
and its change request, says whether it opened a new branch or added to the open one, and says that main has not
changed (until the branch is merged). An import that found nothing new for an existing branch says the branch
already holds the differences; one that found nothing at all says no branch was created. After an import that failed
or found nothing, an earlier import's branch that is still open stays listed. The sync run page shows the same for
its own run. The panel only shows what you may view: sync runs, jobs, branches and change requests.

Review the branch like any other: look at its diff, sync it with main if it is behind, then merge it (once the change
request is approved). Merging queues a push (when **Sync enabled** is on), which finds nothing to do: NetBox now
matches Windows, and the push records the imported objects as managed. With sync off, the first push after you turn
it on does that.

Writing into a branch needs a NetBox user: manual imports use the user who clicked, scheduled ones the `import_user`
setting. Starting an import also needs permission to add a branch, and to add a change request when one will be
opened (see [Permissions](permissions.md)).

## Without branching: import proposals

The import writes an **import proposal** (**Windows DHCP > Import proposals**) with one **item** per difference
(the **Import** panel on the server page links to it once the job has finished):
the action, the kind and Windows key of the object, its values before and after, and the NetBox object it touches.

On a proposal you can:

- **Apply all** or **Dismiss all** pending items;
- select items in the list and **Apply selected** or **Dismiss selected**;
- open one item and **Apply** or **Dismiss** it.

Proposals and items are written by the import, so they cannot be added or edited by hand; they can be deleted.

Applying writes to NetBox as the reviewing user, so the change log shows who accepted each change. It checks that
the NetBox object still has the item's "before" values; if someone changed it since the import, the item fails and
says so. It also checks the user's permissions: an item whose main object the user may not write is left pending
and the missing permission is reported; every other object it writes (prefixes and addresses included) is checked
as it is saved, and a refusal rolls back the whole item and marks it failed. Failed items can be applied again
later. Applied items do not
trigger a push (NetBox and Windows then agree), and the applied objects are recorded as managed straight away.

A repeated import updates the open proposal: pending and failed items are replaced by the new differences. A
dismissed item stays dismissed, in later proposals too, as long as Windows still shows exactly the same values; if
the Windows values change, it comes back. An import with nothing new to review opens no proposal.

## Scheduled imports

Set **Import interval** (minutes) on a Windows server. A NetBox system job runs every minute and queues an import for
each server whose last import started at least that long ago, unless one is already queued or running.

Scheduled imports run as the user named in `import_user`. With branching they fail without it: a branch and its
change request need an owner. Create a dedicated user, give it the permissions an import needs, and set:

```python
PLUGINS_CONFIG = {
    "netbox_windows_dhcp_plugin": {
        "import_user": "dhcp-import-bot",
    },
}
```

With branching, each scheduled import that finds differences adds to the open import branch, so a server changed by
hand every day builds up one branch to review rather than one per run.

## What an import creates in NetBox

- sys4 subnets, pools, shared networks, host reservations, option definitions and options, named as described in
  [How objects map](mapping.md#names);
- the plugin's own objects (scope and reservation settings, exclusion ranges, classes, policies, failover
  relationships, MAC filters, vendor options);
- the prefixes, IP ranges, IP addresses and MAC addresses sys4 needs, in the server's import VRF. Existing ones are
  reused, so failover partners share one IP range per scope.

Text going into a 200-character description field is cut to 200 characters on both sides, so a long Windows name
does not show as a difference on every run.

New sys4 subnets get subnet IDs higher than any in main and in every open import branch, so two import branches never
merge the same ID.
