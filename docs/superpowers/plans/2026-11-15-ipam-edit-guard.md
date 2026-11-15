# IPAM edit guard and push trail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse edits to the core IPAM fields a push reads unless the user may change the dependent scope or
reservation, stop no-op pushes, and record on every push which edits it carried (issue #102).

**Architecture:** A new module `netbox_windows_dhcp_plugin/ipam_guard.py` knows the watched fields and the dependent
objects; a `pre_save` receiver (connected in `signals.connect()`) raises NetBox's `AbortRequest`, the documented
plugin mechanism. The push queue carries a list of "triggers" that is stored on the waiting `core.Job` and copied to
a new `SyncRun.triggers` field when the push runs.

**Tech Stack:** NetBox 4.7 plugin (Django 6.1), netbox-branching (optional), NetBox Django test runner.

**Spec:** `docs/superpowers/specs/2026-11-15-ipam-edit-guard-design.md`

## Global Constraints

- Branch `fix/102-ipam-edit-guard` from `main`; draft PR; squash-merge by the owner. Version `0.4.0` (not bumped in
  this plan: the beta is a branch build).
- Watched fields only: `ipam.Prefix.prefix`; `ipam.IPRange.start_address`, `end_address` (address part);
  `ipam.IPAddress.address` (address part); `dcim.MACAddress.mac_address`.
- Permissions: `netbox_windows_dhcp_plugin.change_scope` for Prefix / IPRange, `...change_reservation` for IPAddress /
  MACAddress, checked per dependent object (constraints count).
- Every `netbox_branching` import stays in `integrations/branching.py` behind `branching_installed()`.
- ruff, line length 120, no em dashes anywhere, comments explain why in plain English.
- Model change: migration `0004_syncrun_triggers`, `makemigrations --check` clean, query-count baselines refreshed in
  both modes (see CLAUDE.md "Model changes need a migration").
- `CHANGELOG.md` under Unreleased, `docs/permissions.md`, `docs/DESIGN.md` updated in the task that changes behaviour.
- Never `make test-netbox` while committing (it swaps the baseline file).

## Review Focus

1. **Background API bulk edits** (`?background=true`, NetBox 4.7): run in a worker under `event_tracking` with the
   user; a forbidden edit must fail per object, not slip through. Test in Task 2.
2. **A Prefix shared by scopes on two servers**, the user may change only one: refused (every dependent must pass).
   Test in Task 2.
3. **IPAddress mask-only change** (`198.18.10.25/24` to `/32`): the address part is the same, so allowed and no push.
   Test in Task 1.
4. **Merge of a branch** whose linked-IP edit was made by a permitted user, merged by a user without
   `change_reservation`: the merge succeeds and queues the push. Test in Task 2.
5. **Edit during the debounce by a second user**: both users appear in the push's triggers. Test in Task 4.

---

### Task 1: Watched fields and dependents (`ipam_guard.py`)

**Files:**
- Create: `netbox_windows_dhcp_plugin/ipam_guard.py`
- Test: `tests_netbox/test_ipam_guard.py`

**Interfaces:**
- Produces:
  - `WATCHED: dict[str, tuple[str, ...]]` keyed by `"ipam.prefix"`, `"ipam.iprange"`, `"ipam.ipaddress"`,
    `"dcim.macaddress"`.
  - `changed_fields(instance) -> tuple[str, ...]`: watched fields whose push-relevant value differs from the stored
    row; `()` for unwatched models, new objects (`instance._state.adding`) or a missing row. One query.
  - `dependents(instance) -> list[tuple[str, QuerySet]]`: `[("change_scope", Scope qs)]` or
    `[("change_reservation", Reservation qs)]` (perm codename without app label) with
    `select_related("server")` / `("scope__server")`.
  - `push_value(label, field, value)`: normalises for comparison: `.ip` of a netaddr IPNetwork for the address
    fields, `str()` of the prefix (network and length), canonical string of the MAC.

- [ ] **Step 1: Write the failing tests** in `tests_netbox/test_ipam_guard.py` (`TestCase`, fixtures from
  `tests_netbox/fixtures.py`):
  - `test_address_change_detected`: reservation on `198.18.10.25/24`; set `address="198.18.10.26/24"` ->
    `changed_fields(ip) == ("address",)`.
  - `test_mask_only_change_ignored`: `/24` -> `/32` -> `()`.
  - `test_description_change_ignored`: IP `description` change -> `()`.
  - `test_range_start_detected`, `test_prefix_length_detected`, `test_mac_detected` (`aa:bb:cc:00:00:01` ->
    `...02`, assert `("mac_address",)`; case-only change `AA:BB...` -> `()`).
  - `test_new_object_ignored`: unsaved `IPAddress` -> `()`.
  - `test_dependents_of_prefix`: two scopes on two servers share one prefix -> codename `"change_scope"` and both
    scopes in the queryset; an IP with no reservation -> `[]`.
- [ ] **Step 2: Run, expect import failure**
  `make test-netbox ARGS=tests_netbox.test_ipam_guard` (or the `manage.py test` line from CLAUDE.md with that label).
- [ ] **Step 3: Implement** `ipam_guard.py` with the interfaces above. Read stored values with
  `type(instance).objects.filter(pk=instance.pk).values(*fields).first()`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: know which IPAM fields reach Windows and what depends on them`.

### Task 2: Refuse the save (`pre_save` guard)

**Files:**
- Modify: `netbox_windows_dhcp_plugin/ipam_guard.py`, `netbox_windows_dhcp_plugin/signals.py` (`connect()`),
  `netbox_windows_dhcp_plugin/integrations/branching.py`
- Test: `tests_netbox/test_ipam_guard.py`, `tests_netbox/test_branch_pushes.py`
- Docs: `docs/permissions.md`, `docs/DESIGN.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: `changed_fields`, `dependents` (Task 1); `signals.push_suppressed` state (`_local.suppressed`).
- Produces:
  - `check_edit(sender, instance, raw=False, **kwargs) -> None` in `ipam_guard.py`, connected with
    `pre_save.connect(check_edit, sender=<model>, dispatch_uid=f"{PLUGIN}.guard.<label>")` for the four models.
    Stores the result on `instance._nbwd_push_fields = changed_fields(instance)` before any early return except
    `raw` (Task 3 reads it).
  - `integrations.branching.applying_branch() -> bool`: True while netbox-branching merges or reverts into main.
    Find how branching marks that (a contextvar or the merge job's request in `netbox_branching`); if it offers
    nothing, set a module-level `ContextVar` from the `pre_merge`/`pre_revert` signals already connected in
    `_connect_push_signals()` and reset it in `post_merge`/`post_revert` and on job start. False when branching is
    not installed.
- Refusal message (exact copy, one line per dependent, at most 5 then "and N more"):
  `Changing {field} would change {kind} {server}/{key} on Windows. You need the "{verb} {kind}" permission on it.`
  with kind `scope` / `reservation`, key as the UI shows it (scope id; `scope_id/ip` for reservations).

- [ ] **Step 1: Write the failing tests** (users get `ipam`/`dcim` view+change through `ObjectPermission`, as in
  `tests_netbox/test_review.py:123`):
  - `test_ui_edit_refused`: POST to the IP address edit view with a new address -> the form re-renders with the
    message, and the address is unchanged in the DB.
  - `test_api_patch_refused`: PATCH `/api/ipam/ip-addresses/<pk>/` -> 400, body contains `change_reservation`.
  - `test_bulk_edit_refused`, `test_csv_import_update_refused` (bulk import with `id` column).
  - `test_background_bulk_api_refused` (Review Focus 1): PATCH list with `?background=true`, run the job, assert the
    object unchanged and the job reports the error.
  - `test_allowed_with_permission`, `test_allowed_with_matching_constraint` (`{"scope__server__name": "dhcp1"}`),
    `test_refused_with_other_constraint`, `test_shared_prefix_needs_every_scope` (Review Focus 2).
  - `test_description_edit_allowed_without_permission`, `test_no_user_allowed` (plain `ip.save()` outside
    `event_tracking`), `test_push_suppressed_allowed`, `test_script_user_checked` (save inside
    `event_tracking(NetBoxFakeRequest({"user": user, "id": uuid4()}))` -> `AbortRequest`).
  - In `test_branch_pushes.py`: `test_guard_applies_in_branch`, `test_merge_not_checked` (Review Focus 4).
- [ ] **Step 2: Run, expect failures** (edits succeed today).
- [ ] **Step 3: Implement** `check_edit` and `applying_branch()`; user from `netbox.context.current_request`;
  permission check with `queryset.restrict(user, "change")` compared to the full queryset (one query per model).
- [ ] **Step 4: Run the new tests and the whole suite** `make test-netbox`; expect all OK.
- [ ] **Step 5: Docs.** `docs/permissions.md`: replace the "in effect a DHCP permission" sentence with the rule,
  the four fields, what is not checked (nbshell and management commands, merges, imports) and a sample message.
  `docs/DESIGN.md`: one paragraph under permissions. `CHANGELOG.md` Unreleased, "Changed": the refusal (a stricter
  rule, why). Run `make lint` and `uv run pytest tests/test_docs.py`.
- [ ] **Step 6: Commit** `feat: refuse IPAM edits that change Windows without the DHCP permission (#102)`.

### Task 3: No push for edits Windows never sees

**Files:**
- Modify: `netbox_windows_dhcp_plugin/signals.py` (`_changed`)
- Test: `tests_netbox/test_push.py` (or the file holding the existing `_changed` queueing tests: grep
  `queue_pushes`/`push_debounce` in `tests_netbox/`)

**Interfaces:**
- Consumes: `instance._nbwd_push_fields` set by `check_edit` (Task 2).

- [ ] **Step 1: Failing tests:** `test_ip_description_edit_queues_no_push` (no `PushJob` for the server after
  commit, using `captureOnCommitCallbacks(execute=True)`); `test_ip_address_edit_queues_push` (still queued);
  `test_mac_edit_queues_push`.
- [ ] **Step 2: Run, expect the first to fail.**
- [ ] **Step 3: Implement:** in `_changed`, for labels in `_LOOKUP_BY_PK` on save (not delete), return when
  `getattr(instance, "_nbwd_push_fields", None) == ()`. A missing attribute (save without our `pre_save`, e.g. raw)
  keeps today's behaviour.
- [ ] **Step 4: Run, expect PASS; refresh query-count baselines if the view tests moved.**
- [ ] **Step 5: Commit** `fix: do not queue a push for IPAM edits Windows never sees`.

### Task 4: Push trail (`SyncRun.triggers`)

**Files:**
- Modify: `netbox_windows_dhcp_plugin/models/sync.py`, `netbox_windows_dhcp_plugin/jobs.py` (`enqueue_push`,
  `PushJob.run`, `PushJob._run`), `netbox_windows_dhcp_plugin/signals.py` (`queue_pushes`, `_flush`),
  `netbox_windows_dhcp_plugin/integrations/branching.py` (`_queue`), `netbox_windows_dhcp_plugin/views.py:391`,
  `netbox_windows_dhcp_plugin/api/views.py:105`, `netbox_windows_dhcp_plugin/ui/panels.py`,
  `netbox_windows_dhcp_plugin/views.py` (SyncRun layout), `netbox_windows_dhcp_plugin/api/serializers.py`
  (SyncRun `fields`), GraphQL SyncRun type if it lists fields
- Create: `netbox_windows_dhcp_plugin/migrations/0004_syncrun_triggers.py`
- Test: `tests_netbox/test_push_trail.py`

**Interfaces:**
- Produces:
  - `SyncRun.triggers = models.JSONField(default=list, blank=True, verbose_name=_("triggered by"))`.
  - A trigger is `{"source": "edit" | "sync_now" | "merge" | "revert" | "retry", "user": str | None,
    "request_id": str | None, "branch": str}` (missing keys allowed; `user` is the username).
  - `enqueue_push(server, user=None, delay=0, branch="", dry_run=False, notify_always=True, attempt=1,
    triggers=())`: inside the existing advisory lock, if a job is waiting, append the new triggers not already in its
    `data["triggers"]` and save it, then return `None` (as today); otherwise enqueue and set
    `job.data = {"triggers": list(triggers)}` before the block commits. Verify NetBox's JobRunner does not overwrite
    `job.data` before `run()` starts.
  - `queue_pushes(server_ids, user=None, delay=0, branch="", notify_always=True, triggers=())` passes them through.
  - `PushJob._run` sets `sync_run.triggers = (self.job.data or {}).get("triggers", [])`; the busy retry passes the
    same list on (`triggers=...`), so a folded retry adds them to the waiting job.
- Callers: `_flush` builds one trigger per pending `(user, request_id)` with `source="edit"` and groups them per
  user as today; Sync now (UI and API) `source="sync_now"`; `branching._queue` `source="merge"` / `"revert"` with
  `branch=name`.
- UI: `SyncRunTriggersPanel` on the sync run page (right column, above Summary): one row per trigger: source, user,
  branch, and for `request_id` a link to `{% url 'core:objectchange_list' %}?request_id=<id>` titled "Changes".
  API: `triggers` read-only in the serializer.

- [ ] **Step 1: Failing tests** in `tests_netbox/test_push_trail.py`:
  - `test_edit_records_trigger`: edit a reservation's IP as `alice` inside `event_tracking`; run the queued push with
    the fake runner; `run.triggers == [{"source": "edit", "user": "alice", "request_id": str(req.id), ...}]`.
  - `test_debounced_edits_are_merged` (Review Focus 5): `alice` then `bob` edit before the push starts -> one push,
    triggers name both.
  - `test_sync_now_trigger`, `test_merge_trigger` (branching only, skip otherwise),
    `test_busy_retry_keeps_triggers` (server lock held -> retry job carries the triggers).
  - `test_sync_run_page_links_changelog`: GET the sync run page -> contains `?request_id=<id>`.
  - API: `test_sync_run_api_has_triggers`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** model field, then generate the migration with the read-only mount workaround from
  CLAUDE.md (`makemigrations --dry-run -v 3`, save as `0004_syncrun_triggers.py`).
- [ ] **Step 4: Implement** queue, job, callers, panel, serializer.
- [ ] **Step 5: Run** the new tests, then the whole `make test-netbox`; refresh both query-count baselines
  (standalone and branching, per CLAUDE.md) if counts moved; `make lint`, `make test`.
- [ ] **Step 6: Docs:** `docs/push.md` (or wherever sync runs are described: grep "sync run" in `docs/`) gets a
  "Triggered by" paragraph; CHANGELOG Unreleased "Added".
- [ ] **Step 7: Commit** `feat: record on each push which edits it carried`.

### Task 5: PR, CI, beta build

- [ ] **Step 1:** Run once without `--keepdb` (CLAUDE.md: branching connection leaks) and confirm OK.
- [ ] **Step 2:** Push the branch, open a draft PR (template `.github/pull_request_template.md`, "Closes #102"),
  wait for CI green on all checks.
- [ ] **Step 3:** Adversarial review of the branch by a fresh reviewer; fix findings; CI green again.
- [ ] **Step 4:** `make dist`; send the wheel path (or the branch tarball URL) to netbox-lab to install on the lab
  NetBox. Do not install it on the VM ourselves.

### Task 6: Lab walkthrough with the owner

- [ ] **Step 1:** `make lab-seed` (Windows DHCP seed; lab settings from `dev/env/lab.env`), create the two
  `WindowsServer` objects on the lab NetBox through its API, import and apply the proposals.
- [ ] **Step 2:** Create user `ipam-only` with `ipam.*` and `dcim.*` view/change but no plugin permission.
- [ ] **Step 3:** Walk the owner through: as `ipam-only`, edit a reserved IP's address (refused, message names the
  reservation); edit its description (allowed, no push); grant `change_reservation` limited to one server; edit
  again (allowed); open the push's sync run and follow "Changes" to the change log.
- [ ] **Step 4:** Tell netbox-lab when the owner is done, so it can destroy the lab.
