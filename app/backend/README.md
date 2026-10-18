# Backend

The backend owns identity, authorization, desired access, node inventory, settings and durable operations. The worker executes and reconciles accepted jobs. Both use PostgreSQL.

## Run from a checkout

Sync the locked backend dependencies with uv, load a private PostgreSQL environment and initialize schema:

```sh
uv sync --locked --python 3.12 --no-default-groups --group backend
PYTHONPATH=app .venv/bin/python -m backend.admin_cli init-schema
PYTHONPATH=app .venv/bin/python -m uvicorn backend.http_api:application --factory --host 127.0.0.1 --port 8080 --no-access-log
```

Use `backend.admin_cli --help` for trusted administration. Bootstrap an administrator with `bootstrap-admin --telegram-id ID`. Token issuance writes credentials to a private file; do not place secrets in logs or arguments.

`/health/live` checks process liveness. `/health/ready` validates schema compatibility. `/openapi.json` exposes the schema. The service factory does not apply deployment migrations.

## References

- [Architecture](../../docs/development/architecture.md)
- [API inventory and contracts](../../docs/development/backend-api.md)
- [Database migrations](../../docs/development/database.md)
- [Development and testing](../../docs/development/testing.md)
- [Temporary access implementation](../../docs/development/temporary-access.md)

Use backend-built intents for driver operations. Keep stable identities and revision fences across retries; preserve unknown outcomes for recovery.
