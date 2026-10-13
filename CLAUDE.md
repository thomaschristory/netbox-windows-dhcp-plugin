# Conventions

- Layering is strict: `windows/` (pure Python, no Django or NetBox imports) is used by `connection.py` and `jobs.py`;
  models, views and API never talk to Windows directly.
- Never interpolate a value into PowerShell. Values reach scripts only through the `$p` JSON literal built by
  `windows/psscript.py`.
- Never log, store or serialise secrets. Credentials come from `conf.get_credentials()` and stay in memory.
- Keep netbox-branching and change control imports inside `integrations/branching.py`; the plugin must work without
  them.
- Never modify netbox-plugin-dhcp tables; add our own models linked by OneToOne or ForeignKey.
- sys4 models validate in `save()`: save objects one at a time, never bulk update them.
- New Windows cmdlets get a fake server implementation and a pwsh stub test. Mark anything not checked on a real
  server with `# unverified:`.
- Model changes need a migration (`makemigrations --check` runs in CI) and updated `tests/query_counts.json`
  baselines (`UPDATE_QUERY_COUNTS=1`, without `--parallel`).
- ruff, line length 120. No em dashes in code, docs or messages.
- Update `CHANGELOG.md` under Unreleased with every user-visible change.
