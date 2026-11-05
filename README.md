# GitHub Webhook Processing Platform

A multi-tenant GitHub webhook platform under development. This first step provides
a local FastAPI application, PostgreSQL 16 with psycopg 3, and an initial SQL
schema. Webhook processing, queues, and AWS infrastructure are future work.

## Prerequisites

- Docker Desktop running with Linux containers and Docker Compose v2.
- An available local port 8000.

Python and PostgreSQL do not need to be installed on your host machine.
Run the following commands from the project directory. Examples use PowerShell.

## Configure

```powershell
Copy-Item .env.example .env
```

Edit `.env` and replace `POSTGRES_PASSWORD` with your own local password before
starting. Do not commit `.env` or use production credentials here.

Compose reads `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` from `.env`
and passes them to both containers. It sets the API's `POSTGRES_HOST` to `db`
(the database service name) and `POSTGRES_PORT` to `5432`.

## Start

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

The initial build downloads images and installs the Python dependencies inside
the API image. Compose waits for PostgreSQL's `pg_isready` health check before
starting the API. Allow a moment for Uvicorn to begin accepting requests.

The API is available only on localhost at `http://localhost:8000`.
PostgreSQL is accessible to the API through the Compose network and has no
published host port. Database files persist in the `postgres_data` named volume.
These settings are for local development; no AWS resources are created.

## Verify

```powershell
curl.exe -i http://localhost:8000/health
curl.exe -i http://localhost:8000/health/db
```

Both should return HTTP 200 with `{"status":"ok"}`.

- `/health` confirms that the API can respond without checking PostgreSQL.
- `/health/db` opens a psycopg connection and runs `SELECT 1`. Connection and
  query timeouts are each three seconds. Database errors return HTTP 503 with
  `{"detail":"Database unavailable"}` without exposing connection details.

Interactive API documentation is at `http://localhost:8000/docs`.

To verify database failure handling:

```powershell
docker compose stop db
curl.exe -i http://localhost:8000/health
curl.exe -i http://localhost:8000/health/db
docker compose start db
```

While PostgreSQL is stopped, `/health` should return HTTP 200 and `/health/db`
should return HTTP 503. After PostgreSQL becomes ready again, repeat the database
health request and confirm HTTP 200.

For troubleshooting:

```powershell
docker compose ps
docker compose logs api db
```

## Apply the initial database migration

Start the containers first and confirm the database health endpoint succeeds.
Apply this migration **once**, from the project directory in PowerShell:

```powershell
Get-Content -Raw migrations/001_initial_schema.sql | docker compose exec -T db sh -c 'psql -X --set=ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'
```

The shell inside the container expands its existing database environment
variables; no password needs to appear in the command. The migration runs in a
transaction, and `ON_ERROR_STOP` makes SQL errors stop execution. An error before
commit rolls back the schema changes. Check `$LASTEXITCODE` immediately after
the command: zero indicates success.

This adds tables to the existing database without resetting the volume. It does
not run automatically on container startup. There is no migration tracking table:
apply numbered migrations in order and keep track of which succeeded. Reapplying
this file fails on existing tables; do not reset data or hide that error with
`IF NOT EXISTS`.

To inspect the tables interactively:

```powershell
docker compose exec db sh -c 'psql -X --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'
```

At the `psql` prompt:

```text
\dt
\d rules
\d events
\d deliveries
\d outbox
\q
```

The schema contains:

| Table | Responsibility |
|---|---|
| `tenants` | Tenant records |
| `sources` | Tenant-owned GitHub sources and secret references |
| `destinations` | Tenant-owned Slack destinations and webhook URL references |
| `rules` | Exact GitHub event type routing from a source to a destination |
| `events` | GitHub payloads, unique delivery identifiers, and source ownership |
| `deliveries` | One job per event/destination, its originating rule, retries, leases, and result |
| `outbox` | One publication job per delivery, linked to the same event |

Composite foreign keys prevent cross-tenant links and ensure a delivery's rule
uses the event's source and the selected destination. Deletes are restricted when
rows are referenced. Unique `(source_id, github_delivery_id)` prevents duplicate
GitHub ingestion; unique `(event_id, destination_id)` prevents multiple delivery
jobs to the same destination for one event.

The explicit `outbox` table follows the Step 2 decision, replacing the earlier
architecture suggestion to reuse delivery rows for publication. Future ingestion
must insert the event, deliveries, and outbox rows in one transaction. Publication
and notification status are independent. The publisher will initially use
transactional row locking; worker lease tokens identify individual claims so
stale workers cannot overwrite a newer claim. These processes are not implemented
yet. Management API key storage is deferred to the authentication step.

Secret reference columns must identify recoverable values in a secure secret
store; they must not contain raw GitHub secrets or Slack webhook URLs. The secret
store implementation is deferred. Error fields should contain only sanitized
messages, never secrets or sensitive payloads. Applications must still authenticate
requests and scope queries by tenant; foreign keys do not provide read isolation.

## Stop

```powershell
docker compose down
```

This removes the containers and network while preserving the database volume.
Restart with `docker compose up -d` (add `--build` after changing application code
or requirements).

## Reset local database data

The following command permanently deletes this Compose project's database volume.
Run it only when you intentionally want to discard all local database data:

```powershell
docker compose down --volumes
```

The official PostgreSQL image applies the database name, user, and password only
when initializing an empty data directory. Changing `.env` does not update an
existing database's credentials. For this disposable local foundation, reset the
volume if you need to initialize with different settings, then start again.

## Files

| File | Responsibility |
|---|---|
| `main.py` | FastAPI application and the two health endpoints |
| `requirements.txt` | FastAPI, Uvicorn, and psycopg dependencies |
| `Dockerfile` | Python 3.13 application image running as a non-root user |
| `compose.yaml` | API, PostgreSQL 16, readiness check, networking, and storage |
| `.env.example` | Example local configuration to copy to `.env` |
| `.dockerignore` | Excludes secrets and generated files from the build context |
| `.gitignore` | Excludes local secrets, environments, and generated files from Git |
| `migrations/001_initial_schema.sql` | Initial tables, relationships, constraints, and indexes |
| `docs/architecture.md` | Planned system architecture and development stages |
