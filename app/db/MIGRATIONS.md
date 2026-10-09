# Database migrations

Node Plane uses an explicit, ordered PostgreSQL migration runner on the existing
psycopg adapter. No ORM or Alembic dependency is required. The authoritative journal
is `backend_schema_revisions`; the historical `schema_meta.schema_version` marker
is not a migration journal.

## Deployment

Installation and update run this command from the **new release**, with the shared
PostgreSQL configuration, before activating its symlink or starting its services:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli init-schema
```

Read history and compatibility without changing the database:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli schema-status
```

Both commands use `DB_BACKEND=postgres` and `POSTGRES_DSN` from the installation's
runtime environment. Never print the DSN in operator logs.

The runner takes a transaction-scoped PostgreSQL advisory lock shared by all
migrators, then validates the journal. Pending DDL, data backfills and journal
entries commit together. Any failure rolls back the entire pending batch, leaving
the previous revision intact. Lock acquisition has a 30-second timeout. Running
`init-schema` again after a successful deployment is a no-op. A failed deployment
must be diagnosed before retrying; migrations do not replay remote node operations.

The baseline adopts current unversioned backend installations as well as clean
databases. It preserves accounts, credentials, grants, peers, command IDs and
immutable intent payloads. It includes the earlier additive device/region schema
changes and their metadata backfills. The second revision adds anonymous monthly
node traffic and seeds only the current UTC month without replacing existing totals.
This is not a converter for the retired PTB architecture or arbitrary older prototypes.

HTTP readiness validates history/compatibility and required tables without DDL.
The worker and trusted administration commands also check compatibility before
mutations. Persistent settings no longer create tables on reads. The embedded SSH
workstation bridge retains audit bootstrap only for unversioned old releases, so
those installations can still be upgraded; migrated installations use read-only
schema checks. Repository `initialize_schema` methods remain historical/test
fixtures, not the deployment mechanism.

## Add a revision

1. Add a standalone file under `migration_revisions` with `upgrade(conn)`.
   Freeze its SQL and data transformations. Do not import changing application
   repositories or call their schema initializers from a migration.
2. Append a consecutive `Revision` to `REVISIONS` in `migrations.py` with a unique
   name and `minimum_reader`. Never edit, rename, remove or reorder a released
   revision: its source checksum is recorded and validated.
3. Prefer additive expand/contract changes that let the previous application run.
   `minimum_reader` identifies the oldest **migration revision** whose application
   can safely use the resulting schema. Incompatible changes require explicitly
   quiescing old applications/workers before migration; ordinary automatic updates
   must remain compatible with their retained previous release.
4. Test an empty database and a populated previous revision, repeat application,
   concurrent runners, failure rollback and old-reader compatibility on disposable
   PostgreSQL. Keep the runtime model, backups and isolated repository fixtures
   consistent with the new schema.

All registry/history validation failures stop deployment before pending changes.
An older migrator refuses a newer database; it never silently downgrades it.
Older application readers can use a newer additive schema only when their known
revision checksums match and all journal entries permit their reader revision.
If a newer revision requires a newer reader, backend readiness and workers fail
closed; switching the application symlink back does not downgrade the database.

No automatic schema downgrade command is provided. Data loss cannot be reversed
by rolling back application binaries. Migration history is not configuration
backup content and must survive backup restoration and controller reset.

## Regression tests

Run separately against an explicitly disposable PostgreSQL database; the normal
unit suite uses a psycopg double and must not be mixed into this process:

```sh
PYTHONPATH=app NODE_PLANE_TEST_POSTGRES_DSN=postgresql://... \
  .venv/bin/python -m unittest tests.test_migrations_postgres tests.test_backups_postgres
```

Tests create unique schemas and remove them afterwards. Never use the production
`POSTGRES_DSN` as the test DSN.
