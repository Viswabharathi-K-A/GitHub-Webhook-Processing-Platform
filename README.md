# GitHub Webhook Processing Platform

A multi-tenant GitHub webhook platform under development. This first step provides
only a local FastAPI application and PostgreSQL 16 with psycopg 3. Webhook
processing, application tables, queues, and AWS infrastructure are future work.

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
| `docs/architecture.md` | Planned system architecture and development stages |
