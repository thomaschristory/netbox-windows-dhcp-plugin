# IPAM edits that change Windows need the DHCP permission (issue #102)

Short design, 2026-11-15.

## Why

Scopes and reservations point at core NetBox objects. Editing one of those objects changes what the next push
writes to Windows, and today that needs only NetBox's own `ipam` / `dcim` permission. Someone with no plugin
permission can renumber a reservation or shrink a scope, and the change reaches production DHCP after the push
debounce. The owner's two concerns:

1. Whoever changes DHCP must hold the DHCP permission. Someone allowed to edit the scope or reservation may do it
   through IPAM; nobody else may.
2. There is no trail: nothing links a push to the edit that caused it.

## Goals

- Refuse the save (UI edit, bulk edit, bulk import, REST API) when a user without `change` on a dependent scope or
  reservation changes a field a push reads.
- Each push records which edits it carried (user and request), so a DHCP change can be traced to its NetBox edit.
- Edits to fields a push never reads stop queueing pushes.

## Non-goals

- Holding pushes for approval. A push reconciles the whole server, so the next push by anyone would carry a held
  change anyway; only refusing the edit protects Windows.
- Checks on saves with no user behind them (nbshell, management commands); NetBox records no change log entry for
  them either. Custom scripts and background API requests do carry their user (NetBox wraps them
  in `event_tracking` with a request), so they are checked.
- Deletes: the links are PROTECT, so these objects cannot be deleted while in use (#107 covers that experience).
- A backport to `release/0.1` (no new features there).

## Approaches

- **A. `pre_save` receiver raising `AbortRequest` (picked).** The mechanism NetBox's plugin docs describe for a plugin
  to interrupt a create, edit or delete (`docs/plugins/development/exceptions.md`, whose example is a `pre_save`
  receiver on a core model). The docs keep it for complex logic; this check (linked objects, changed fields,
  object permissions) is that. NetBox's edit, bulk edit and bulk import views and the REST API already turn it into
  an error message or HTTP 400.
- **B. A `CustomValidator`.** Not open to plugins: validators live in the admin's `CUSTOM_VALIDATORS` setting, and
  injecting one from the plugin would patch the admin's configuration.
- **C. Keep as is, documented (0.3.0).** Rejected by the owner: no trail and no DHCP permission.
- **D. Hold the push instead of refusing.** Rejected: see the non-goals.

## Design

**Watched fields.** Only what a push reads (`sync/netbox_state.py`):

| Model | Field compared | Dependent objects | Permission needed |
|---|---|---|---|
| `ipam.Prefix` | `prefix` (network and length) | scopes with this prefix | `change_scope` |
| `ipam.IPRange` | `start_address`, `end_address` (address part) | scopes with this range | `change_scope` |
| `ipam.IPAddress` | `address` (address part) | reservations with this IP | `change_reservation` |
| `dcim.MACAddress` | `mac_address` | reservations with this MAC | `change_reservation` |

**Guard.** A `pre_save` receiver on these four models, connected in `signals.connect()`:

1. Leave at once for new objects (nothing can point at them yet), inside `push_suppressed()` (import apply writes
   what Windows already has), and when `current_request` holds no
   authenticated user.
2. Read the stored values of the watched fields (one query by primary key) and compare. No change: leave.
3. Find the dependent scopes or reservations. None: leave.
4. Check `change` on each dependent object for the request user, object permissions and their constraints included
   (`restrict(user, "change")` or `has_perm` with the object). Every one must pass.
5. Otherwise raise `AbortRequest` naming the field, the scope or reservation (server and key, as the UI shows them)
   and the missing permission, for example: "Changing this IP address would change reservation
   dhcp01/198.18.10.0/198.18.10.25 on Windows. You need the change reservation permission on it."

Branches: the guard runs for saves in a branch too (the request user is known), and again at merge and revert, as
the merging or reverting user against main's scopes and reservations. Changed after the adversarial review of #112:
the first version skipped merges, which let an edit made in an old branch, where nothing used the object yet, reach
Windows unchecked once main had built a scope or reservation on it. The owner chose to check the merging user (not
each change's author) so the rule does not depend on the merge strategy.

**No-op pushes.** For the four core models, `_changed` queues a push only when a watched field changed (or on
delete). The `pre_save` receiver marks the instance with the result of step 2 so `post_save` does not query again.

**Trail.** Each push records the edits that triggered it: `[{user, request_id}]`, kept on the sync run and shown on
its page with a link to the change log filtered by that request (`/core/changelog/?request_id=...`). When an edit
lands while a push for that server is already waiting (the debounce), its user and request are added to that waiting
push instead of being lost. This covers edits to the plugin's own models as well, which go through the same queue.
Pushes started by hand (Sync now) or by a branch merge record that instead.

## Left to the builder

- How the trail of a waiting push is stored and appended (job data, a field on the sync run, or a small model), as
  long as it survives the debounce and the busy retries.
- Exact message wording; the query shape for the permission check; query-count baselines in both modes.
- Tests (NetBox tier): refused for UI edit, bulk edit, CSV import and API; allowed with the permission, with a
  constrained permission matching the object, without a request, under `push_suppressed`; refused with a constraint
  not matching; unwatched-field edits allowed and no push queued; inside a branch; the trail across a debounced
  second edit.
- Docs: `docs/permissions.md` (replace the "in effect a DHCP permission" paragraph), `docs/DESIGN.md`,
  `CHANGELOG.md` under Unreleased.

## Release (my call)

A stricter rule that refuses edits that used to pass: ship as 0.4.0. The owner tries it first as a beta built from
the branch (wheel handed to netbox-lab, which installs it on the lab), before anything goes to PyPI. The walkthrough: seed Windows (`make lab-seed`), import, give a
test user only IPAM permissions, edit a reservation's IP address and see the refusal, then grant `change_reservation`
and see the edit go through and the push record its trail.
