# REST API and GraphQL

Every model of the plugin has a REST endpoint under `/api/plugins/windows-dhcp/` and GraphQL queries, with the usual
NetBox filtering, brief mode and tags. Sync runs, import proposals and their items are read only, apart from the
review actions below.

The examples use a NetBox v2 API token (`Authorization: Bearer nbt_...`); a v1 token works too, with
`Authorization: Token ...`.

## Endpoints

| Endpoint | Model |
|---|---|
| `windows-servers/` | Windows servers |
| `windows-scopes/` | Scope settings |
| `windows-reservations/` | Reservation settings |
| `exclusion-ranges/` | Exclusion ranges |
| `windows-classes/` | User and vendor classes |
| `windows-option-definitions/` | Option definition settings |
| `windows-option-qualifiers/` | Option qualifiers |
| `windows-policies/` | Policies |
| `vendor-option-definitions/` | Vendor class option definitions |
| `vendor-options/` | Vendor class option values |
| `failover-relationships/` | Failover relationships |
| `mac-filters/` | MAC filters |
| `mac-filter-lists/` | MAC filter list switches |
| `sync-runs/` | Sync runs (read only) |
| `import-proposals/` | Import proposals (read only, plus actions) |
| `import-proposal-items/` | Import proposal items (read only, plus actions) |

sys4 objects (DHCP servers, subnets, pools, reservations, options) stay at netbox-plugin-dhcp's own endpoints under
`/api/plugins/netbox-dhcp/`.

## Creating a Windows server

```bash
curl -s -X POST https://netbox.example/api/plugins/windows-dhcp/windows-servers/ \
  -H "Authorization: Bearer $NETBOX_TOKEN" -H "Content-Type: application/json" \
  -d '{"dhcp_server": 3, "host": "dhcp01.corp.example", "credential_profile": "dhcp-admin", "sync_enabled": false}'
```

The same validation as the form applies: the profile's `allowed_hosts` must cover the host.

## Actions on a Windows server

Each action queues a job and answers `202 Accepted` with the NetBox job. Follow the job at `/api/core/jobs/<id>/`,
or find its sync run with `GET /api/plugins/windows-dhcp/sync-runs/?windows_server_id=<id>`.

| Action | Request | Answers |
|---|---|---|
| Test connection | `POST windows-servers/<id>/test-connection/` | 202 |
| Import | `POST windows-servers/<id>/import/` | 202; 409 when an import is already queued or running; 403 when branching permissions are missing |
| Push (Sync now) | `POST windows-servers/<id>/push/` | 202; 409 when sync is disabled or a push is already waiting |
| Dry run | `POST windows-servers/<id>/dry-run/` | 202; 409 when a dry run is already waiting |

All of them need a token that may write and `change` on the server; a server the user may not change answers 404.

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
`{"dismissed": ...}`. Failed items, and items left pending for a missing permission, can be applied again. The
[permission rules](permissions.md#applying-proposal-items) are the same as in the UI.

List the pending items of a proposal with `GET import-proposal-items/?proposal_id=<id>&status=pending`. Each item
has its `action`, `kind`, `key`, `before` and `after` values, and the NetBox object it touches.

## GraphQL

Each model has a single-object and a list query, prefixed with `windows_dhcp_`: `windows_dhcp_server`,
`windows_dhcp_server_list`, `windows_dhcp_scope_list`, `windows_dhcp_reservation_list`,
`windows_dhcp_exclusion_range_list`, `windows_dhcp_class_list`, `windows_dhcp_option_definition_list`,
`windows_dhcp_policy_list`, `windows_dhcp_option_qualifier_list`, `windows_dhcp_failover_relationship_list`,
`windows_dhcp_mac_filter_list`, `windows_dhcp_mac_filter_lists_list`, `windows_dhcp_vendor_option_definition_list`,
`windows_dhcp_vendor_option_list`, `windows_dhcp_sync_run_list`, `windows_dhcp_import_proposal_list` and
`windows_dhcp_import_proposal_item_list`.

```graphql
{
  windows_dhcp_server_list {
    id
    host
    sync_enabled
    dhcp_server { name }
  }
}
```

GraphQL is read only, as everywhere in NetBox.
