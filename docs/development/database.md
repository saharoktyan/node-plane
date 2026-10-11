# Database and migrations

PostgreSQL stores controller state. The migration runner uses psycopg and an ordered revision registry. `backend_schema_revisions` records the applied revision names and checksums.

## Deployment

From the new release with the installation's private environment loaded:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli init-schema
PYTHONPATH=app .venv/bin/python -m backend.admin_cli schema-status
```

`init-schema` applies pending revisions; `schema-status` reads compatibility/history. Both use `DB_BACKEND=postgres` and `POSTGRES_DSN`. Do not print the DSN.

Install/update applies migrations before switching the active release and starting services. A transaction-scoped advisory lock serializes runners, with a 30-second acquisition timeout. Pending DDL, backfills and journal writes commit together; failure rolls the pending batch back. Reapplying a successful batch is a no-op.

Readiness and mutation entry points validate required schema/history. An older migrator refuses a newer database. An older application may read an additive newer schema only when revision checksums match and `minimum_reader` permits it.

## Add a revision

1. Add a standalone `upgrade(conn)` under `app/db/migration_revisions/`.
2. Append a consecutive revision in `app/db/migrations.py` with a unique name and `minimum_reader`.
3. Freeze SQL/backfills in the revision. Avoid imports from changing repositories or schema initializers.
4. Prefer additive changes compatible with the retained release. Coordinate shutdown for incompatible changes.
5. Test empty and populated databases, repeated application, concurrency, failure rollback and old-reader compatibility.

Released revision sources are immutable: do not edit, rename, remove or reorder them. The source checksum is validated. Migration history survives configuration restore and controller reset.

## Test against PostgreSQL

Use a disposable database, with the integration tests in a separate process:

```sh
PYTHONPATH=app NODE_PLANE_TEST_POSTGRES_DSN=postgresql://... \
  .venv/bin/python -m unittest tests.test_migrations_postgres tests.test_backups_postgres
```

Tests create/remove isolated schemas. Never substitute the production DSN. A configuration restore does not downgrade schema; switching binaries cannot reverse data loss. There is no automatic schema downgrade command.
