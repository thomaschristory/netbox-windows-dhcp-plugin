# REST API and GraphQL

Every model of the plugin, apart from its internal sync state, has a REST endpoint under `/api/plugins/windows-dhcp/`
and GraphQL queries, with the usual NetBox filtering, brief mode and tags. Sync runs, import proposals and their
items are read only, apart from the review actions below.

The examples use a NetBox v2 API token (`Authorization: Bearer nbt_...`); a v1 token works too, with
`Authorization: Token ...`.

## Endpoints

| Endpoint | Model |
|---|---|
| `windows-servers/` | Windows servers |
| `scopes/` | Scopes (IPv4 and DHCPv6) |
| `superscopes/` | Superscopes |
| `reservations/` | Reservations |
| `exclusion-ranges/` | Exclusion ranges |
| `windows-classes/` | User and vendor classes |
| `option-definitions/` | Option definitions |
| `option-values/` | Option values (server, scope and reservation level) |
| `windows-policies/` | Policies |
| `failover-relationships/` | Failover relationships |
| `mac-filters/` | MAC filters |
| `mac-filter-lists/` | MAC filter list switches |
| `sync-runs/` | Sync runs (read only) |
| `import-proposals/` | Import proposals (read only, plus actions) |
| `import-proposal-items/` | Import proposal items (read only, plus actions) |

Prefixes, IP ranges, IP addresses and MAC addresses stay at NetBox's own endpoints (`/api/ipam/...`,
`/api/dcim/mac-addresses/`); scopes and reservations refer to them by ID. Option values hold their value as a list of
strings (`"value": ["10.1.0.53", "10.1.0.54"]`). [Reservations through the API](reservations-api.md) creates a
reservation and an option value on it end to end, from the IP and MAC address objects to the push.

## Creating a Windows server

```bash
curl -s -X POST https://netbox.example/api/plugins/windows-dhcp/windows-servers/ \
  -H "Authorization: Bearer $NETBOX_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "dhcp01", "host": "dhcp01.corp.example", "credential_profile": "dhcp-admin", "sync_enabled": false}'
```

The same validation as the form applies: the profile's `allowed_hosts` must cover the host. Add `"device": <id>` or
`"virtual_machine": <id>` to link the machine the DHCP service runs on.

## Actions on a Windows server

Each action queues a job and answers `202 Accepted` with the NetBox job. Follow the job at `/api/core/jobs/<id>/`,
or find its sync run with `GET /api/plugins/windows-dhcp/sync-runs/?windows_server_id=<id>`.

| Action | Request | Answers |
|---|---|---|
| Test connection | `POST windows-servers/<id>/test-connection/` | 202; 409 when a connection test is already waiting, with the waiting job's id in `job` next to `detail` (`null` if it started meanwhile) |
| Import | `POST windows-servers/<id>/import/`, optionally with `{"destination": ...}` (below) | 202; 409 when an import is already queued or running; 400 for a destination not available here, or for `current` with no branch named or active; 403 when permissions are missing or the destination is refused |
| Push (Sync now) | `POST windows-servers/<id>/push/` | 202; 409 when sync is disabled or a push is already waiting |
| Dry run | `POST windows-servers/<id>/dry-run/` | 202; 409 when a dry run is already waiting |

All of them need a token that may write and `change` on the server; a server the user may not change answers 404.

The import's `destination` is where it writes (see [Import](import.md#choosing-where-an-import-writes)). With
netbox-branching: `branch` (the default: the server's open import branch, or a new one), `current` (the branch named
in `branch`, or the branch the request works in, for example through the `X-NetBox-Branch` header) or `main`. Without
it: `proposal` (the default). A refused destination is never swapped for another one. The sync run records the
destination, and the job's `data` holds it from the moment the import is queued.

```bash
curl -s -X POST https://netbox.example/api/plugins/windows-dhcp/windows-servers/1/dry-run/ \
  -H "Authorization: Bearer $NETBOX_TOKEN"
```

## Reviewing import proposals

| Action | Request |
|---|---|
| Apply every pending item of a proposal | `POST import-proposals/<id>/apply/` |
| Dismiss every pending item of a proposal | `POST import-proposals/<id>/dismiss/` |
| Apply some items | `POST import-proposal-items/apply/` with `{"ids": [12, 13]}` |
| Dismiss some items | `POST import-proposal-items/dismiss/` with `{"ids": [14]}` |

Applying answers `{"applied": ..., "failed": ..., "missing_permissions": [...]}`; dismissing answers
`{"dismissed": ...}`. The counts hold what this request changed: an item another reviewer decided, or a newer
import replaced, in the meantime is not counted. Failed items, and items left pending for a missing permission, can
be applied again. The [permission rules](permissions.md#applying-proposal-items) are the same as in the UI.

Both apply actions answer `409` with a `detail` when the request is sent in a branch (the `X-NetBox-Branch`
header): proposals are applied to main. Dismissing works either way.

### Large reviews run as a job

When a request picks more open items than the [`review_job_threshold`](configuration.md#settings) setting (100 by
default), the work is not done in the request: it is queued as a background job (**Windows DHCP import review**) that
runs as you, and the answer is `202` instead of the counts above:

- `import-proposals/<id>/apply/` and `/dismiss/` answer with the job, as `test-connection` and `import` do (its `id`,
  `url`, `status`...);
- `import-proposal-items/apply/` and `/dismiss/` answer `{"jobs": [...]}`, one job per proposal the items belong to.

Follow the job either at `GET /api/core/jobs/<id>/` (this needs `core.view_job`) or through the proposal: every
`import-proposals/` object has `review_job`, the newest review job's `status`, `created`, `started`, `completed` and
`data` (`null` when there was none), readable by anyone who may view the proposal. Once the job has finished, its
`data` holds what it did:
`{"action": "apply", "items": 2400, "superseded": 0, "applied": ..., "failed": ..., "missing_permissions": [...],
"remaining": ...}` (`"dismissed"` in place of the three apply counts for a dismissal).

- `superseded` counts the picked items that were no longer open when the job started: a newer import of the server
  replaced them (an import replaces every open item of the proposal with new ones), or another reviewer decided them.
  Review the proposal again. When none of the picked items is left, the job fails and says so.
- `remaining` counts the picked items that were open when the job started and still are: failed, left pending for a
  missing permission, or no longer yours to change. Superseded items are not in it.
- `error` says why the job could not run at all (the proposal is gone, your permissions changed, every picked item
  was superseded, or an import or push of the same server was running); the job then failed.
- `interrupted` is `true` when the job stopped halfway (it timed out, lost its database connection, or hit a bug; the
  job is then errored and its `error` says why): the counts are what it had done until then, every one of those items
  is committed, and webhooks heard of them.

The job checks your permissions when it starts (not again for each item), and only acts on the items picked when it
was queued.

While a review job of a proposal waits or runs, a request that would queue another one for the same proposal answers
`409` with a `detail`, and the waiting job's ID in `job` when you queued it or may view it (`null` otherwise). A job
that no worker finishes stops blocking new reviews 2 hours after it was queued. Below the threshold nothing changes:
the request applies or dismisses the items itself and answers `200` with the counts. Clients that send large reviews
must handle both.

List the pending items of a proposal with `GET import-proposal-items/?proposal_id=<id>&status=pending`. Each item
has its `action`, `kind`, `key`, `before` and `after` values, and the NetBox object it touches.

## GraphQL

Each model has a single-object and a list query, prefixed with `windows_dhcp_`: `windows_dhcp_server`,
`windows_dhcp_server_list`, `windows_dhcp_scope_list`, `windows_dhcp_superscope_list`,
`windows_dhcp_reservation_list`, `windows_dhcp_exclusion_range_list`, `windows_dhcp_class_list`,
`windows_dhcp_option_definition_list`, `windows_dhcp_option_value_list`, `windows_dhcp_policy_list`,
`windows_dhcp_failover_relationship_list`, `windows_dhcp_mac_filter_list`, `windows_dhcp_mac_filter_lists_list`,
`windows_dhcp_sync_run_list`, `windows_dhcp_import_proposal_list` and `windows_dhcp_import_proposal_item_list`.

```graphql
{
  windows_dhcp_scope_list {
    id
    name
    state
    server { name }
    prefix { prefix }
  }
}
```

GraphQL is read only, as everywhere in NetBox.
