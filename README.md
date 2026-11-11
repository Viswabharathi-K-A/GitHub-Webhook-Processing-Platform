# GitHub Webhook Processing Platform

A multi-tenant GitHub webhook platform under development. This first step provides
a local FastAPI application, PostgreSQL 16 with psycopg 3, and an initial SQL
schema. Signed webhook ingestion, an outbox publisher, and the delivery worker's
claim-and-success path are implemented. Worker retry policy, DLQ recovery,
Compose worker service integration, and AWS infrastructure remain future work.

## Remaining roadmap

The detailed [architecture and roadmap](docs/architecture.md#7-development-roadmap)
distinguish implemented behavior from planned work:

| Stage | Goal |
|---|---|
| 2 — Delivery worker | Slack-only delivery with PostgreSQL idempotency/leases, bounded retries, SQS visibility and practical DLQ recovery |
| 3 — Tenant management | Hashed API keys, tenant-scoped configuration, protected secret references, isolation tests and simple CI |
| 4 — Temporary AWS deployment | One readable Terraform root, minimal ECS/RDS/SQS/ALB infrastructure, scoped IAM and HTTPS in us-west-2 |
| 5 — Operational verification | Useful logs/alarms, failure and recovery checks, bounded load measurements and lightweight deployment procedures |
| 6 — Portfolio and cleanup | Record real evidence, document tradeoffs, destroy and inspect billable resources |

Security and tests are part of every stage. Delivery attempts are at least once;
Slack notifications can duplicate after uncertain outcomes. The project does
not promise exactly-once delivery. Use tenant API keys rather than OAuth/Cognito,
and keep explicit SQL and the existing codebase instead of adding frameworks.
The AWS plan is a temporary demonstration with documented availability and
networking tradeoffs, not a continuously running production service.

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
architecture suggestion to reuse delivery rows for publication. Ingestion
inserts the event, deliveries, and outbox rows in one transaction. Publication
and notification status are independent. The publisher uses
transactional row locking; worker lease tokens will identify individual claims so
stale workers cannot overwrite a newer claim. The publisher and initial delivery
worker are implemented; worker failure classification and bounded retries remain
future work. Management API key storage is deferred to the authentication step.

Secret reference columns must identify recoverable values in a secure secret
store; they must not contain raw GitHub secrets or Slack webhook URLs. The secret
store implementation is deferred. Error fields should contain only sanitized
messages, never secrets or sensitive payloads. Applications must still authenticate
requests and scope queries by tenant; foreign keys do not provide read isolation.

## Outbox publisher

The publisher runs separately from the API and processes one due outbox job per
transaction. It holds a `FOR UPDATE SKIP LOCKED` row lock while sending to SQS,
then marks the row published only after the send succeeds. Other publishers skip
locked rows. Delivery notification status is unchanged.

Failed sends remain pending and retry after 5 seconds, doubling to a maximum of
300 seconds. There is no attempt limit that silently discards jobs. Idle polling
waits one second; database failures trigger a five-second reconnect delay.
Logs contain job IDs and outcomes, not payloads, secrets, or raw exception messages.
Shutdown finishes the current bounded send/transaction before exiting.

A crash after SQS accepts a message but before the database commit can produce
duplicates. The future worker must tolerate these; publication is not exactly once.

Set these values in `.env` before enabling the publisher:

- `AWS_DEFAULT_REGION=us-west-2` (Oregon).
- `SQS_QUEUE_URL`: the existing standard queue URL.
- `SQS_ENDPOINT_URL`: blank for AWS, or a reachable local emulator endpoint.

Compose provides an optional [Moto server](https://docs.getmoto.org/en/latest/docs/server_mode.html) SQS emulator, reachable inside the Compose
network at `http://sqs:5000`. It has no published host port and uses disposable
in-memory queues. Inside containers, use `sqs`, not host `localhost`.
Local emulator credentials are dummy values `test` / `test`. For an existing AWS development
queue, Compose forwards `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optional
`AWS_SESSION_TOKEN` from your environment or uncommitted `.env`. It does not mount
host AWS profiles. Never put credentials in committed files. AWS sends can incur
charges; the runtime role/user needs `sqs:SendMessage` on the selected queue.
ECS should use a task role rather than static keys.

The Compose publisher still depends on the local emulator's health check even
when configured for AWS. A blank endpoint selects AWS; explicitly set
`http://sqs:5000` for local publishing.

For a local publisher demonstration, set `SQS_ENDPOINT_URL=http://sqs:5000`,
`AWS_ACCESS_KEY_ID=test`, `AWS_SECRET_ACCESS_KEY=test`, and
`AWS_DEFAULT_REGION=us-west-2` in your uncommitted `.env`. Clear any AWS session
token. Start the emulator and create a queue using the existing test image:

```powershell
docker compose --profile test build tests
docker compose --profile publisher up -d sqs
docker compose --profile test run --rm tests python -c "import boto3; s=boto3.client('sqs', endpoint_url='http://sqs:5000', region_name='us-west-2', aws_access_key_id='test', aws_secret_access_key='test'); print(s.create_queue(QueueName='deliveries')['QueueUrl'])"
```

Set `SQS_QUEUE_URL` to the returned URL. After applying the initial migration and
setting a nonempty `GITHUB_WEBHOOK_SECRET_LOCAL` in `.env`, start the API and
publisher. Source rows must reference `env:GITHUB_WEBHOOK_SECRET_LOCAL` for this
Compose configuration. The integration tests generate their own source, rule,
secret, and signed request without modifying your `.env` or public schema.

```powershell
docker compose --profile publisher up --build -d api publisher
docker compose logs publisher
```

### Standalone local demonstration

After the queue creation, migration, environment setup, and startup above, create
the routing configuration below in the local database. This writes demo records;
run it once per demonstration. Repeating it creates another tenant and source.
The destination is a placeholder reference: this stage sends to SQS, not Slack.

Open `psql`:

```powershell
docker compose exec db sh -c 'psql -X --set=ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'
```

Paste this SQL into `psql`. `\gset` stores the generated IDs, so the example does
not assume an empty database or IDs starting at one:

```sql
BEGIN;
INSERT INTO tenants (name) VALUES ('local-demo') RETURNING id AS demo_tenant_id \gset
INSERT INTO sources (tenant_id, name, webhook_secret_ref)
VALUES (:demo_tenant_id, 'local-github', 'env:GITHUB_WEBHOOK_SECRET_LOCAL')
RETURNING id AS demo_source_id \gset
INSERT INTO destinations (tenant_id, name, webhook_url_ref)
VALUES (:demo_tenant_id, 'local-placeholder', 'env:SLACK_WEBHOOK_URL_LOCAL')
RETURNING id AS demo_destination_id \gset
INSERT INTO rules (tenant_id, source_id, destination_id, event_type)
VALUES (:demo_tenant_id, :demo_source_id, :demo_destination_id, 'push');
COMMIT;
\echo Demo source ID: :demo_source_id
\q
```

Copy the printed source ID into the prompt below. The Python code runs inside
the API container, reads its configured secret without printing it, and signs
the exact bytes sent over HTTP. It submits the same delivery twice:

```powershell
$demoSourceId = Read-Host 'Demo source ID'
@'
import hashlib
import hmac
import os
import sys
import urllib.request
from uuid import uuid4

source_id = int(sys.argv[1])
body = b'{"ref":"refs/heads/main","repository":{"name":"local-demo"}}'
secret = os.environ['GITHUB_WEBHOOK_SECRET_LOCAL'].encode()
headers = {
    'Content-Type': 'application/json',
    'X-GitHub-Event': 'push',
    'X-GitHub-Delivery': str(uuid4()),
    'X-Hub-Signature-256': 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest(),
}
for _ in range(2):
    request = urllib.request.Request(
        f'http://localhost:8000/webhooks/github/{source_id}', data=body, headers=headers
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.status, response.read().decode())
'@ | docker compose exec -T api python - $demoSourceId
```

Expect `202 {"status":"accepted"}` followed by `200 {"status":"duplicate"}`.
In `psql`, replace `YOUR_SOURCE_ID` below with that source ID:

```sql
SELECT e.id AS event_id, d.id AS delivery_id, d.status AS delivery_status,
       o.status AS publication_status, o.attempt_count
FROM events e
JOIN deliveries d ON d.event_id = e.id AND d.tenant_id = e.tenant_id
JOIN outbox o ON o.delivery_id = d.id AND o.tenant_id = d.tenant_id
WHERE e.source_id = YOUR_SOURCE_ID;
```

For one execution of the Python example, expect one row: delivery `pending` and,
after the publisher processes it, publication `published`. Check the local queue:

```powershell
docker compose --profile test run --rm tests python -c "import boto3; s=boto3.client('sqs', endpoint_url='http://sqs:5000', region_name='us-west-2', aws_access_key_id='test', aws_secret_access_key='test'); q=s.get_queue_url(QueueName='deliveries')['QueueUrl']; print([m['Body'] for m in s.receive_message(QueueUrl=q, MaxNumberOfMessages=10, WaitTimeSeconds=2).get('Messages', [])])"
```

Expect a message containing the matching `delivery_id`. Receiving temporarily
hides messages; this inspection does not delete them. Existing demo messages may
also appear. These manual demo commands are instructions, not a claim that a
real GitHub or Slack demonstration has been executed.

Stop the publisher when finished:

```powershell
docker compose stop publisher
```

Queue creation is explicit; restarting/recreating Moto can lose its queues and
messages. Recreate the demo queue as needed. This differs from durable AWS SQS.
The publisher profile is optional, so normal API/database startup does not require
SQS credentials. To inspect progress with `psql`, query:

```sql
SELECT id, delivery_id, status, attempt_count, next_attempt_at, last_error
FROM outbox ORDER BY id;
```

The automated integration suite below exercises a complete signed HTTP webhook
through an actual Uvicorn process, PostgreSQL, a separate publisher process, and
real boto3 requests to Moto. It also checks invalid signatures and duplicate
webhook handling. A separate Compose-container demo is optional; no manual
GitHub repository webhook delivery is claimed by these tests.

## Initial delivery worker

`delivery_worker.py` receives one SQS message at a time using five-second long
polling and a 90-second visibility timeout. In a short PostgreSQL transaction it
claims a due delivery with a fresh UUID token and a 60-second lease, increments
its attempt count, and commits before HTTP. It looks up the event and destination
through tenant-matching joins. The notification contains only the event type and
event/delivery IDs, not the full GitHub payload.

| Delivery state | Current worker behavior |
|---|---|
| `succeeded` or `failed` | Delete the received message without sending to Slack |
| Active lease or future retry time | Keep the message and defer visibility until eligible |
| Due `pending` or expired lease | Claim with a new token, then check the destination and ownership before sending |
| Successful Slack response | Commit `succeeded`, clear the lease, then delete the message |
| Lease lost before completion | Do not record success or delete the message |
| Invalid message or missing delivery | Log a sanitized reason and leave the message unacknowledged |

`slack_sender.py` supports local environment secret references of the form
`env:SLACK_WEBHOOK_URL_TENANT_<tenant_id>_<NAME>`. A tenant cannot resolve a
reference in another tenant's namespace. The secret value must be an HTTPS URL
on exactly `hooks.slack.com`, with the `/services/<id>/<id>/<token>` path and
port 443. Credentials in URLs, query strings, fragments, alternate hosts, and
redirects are rejected. The sender uses public IPv4 addresses only; validation
happens in the resolver used by the connector, with no second DNS lookup between
validation and connection. TLS certificate and hostname verification remain
enabled. Proxy environment variables are ignored.

The added `aiohttp` dependency provides a ten-second total request timeout,
including waiting for DNS and the response body. The system DNS resolver runs
in a thread: cancellation stops the request, but a stuck OS lookup can delay
interpreter shutdown. This is not a hard process-exit deadline. Database statements
have their own three-second timeout. The current timing values are starting
settings, not a throughput or availability guarantee. See the
[aiohttp client reference](https://docs.aiohttp.org/en/stable/client_reference.html)
for connector and timeout behavior and
[Slack's incoming webhook documentation](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)
for the POST/response contract.

**This is the success-path stage, not the complete retry worker.** On an invalid
destination, unavailable secret, timeout, or unsuccessful Slack response, the
worker records a sanitized reason when it still owns the lease, keeps the SQS
message, and exits with code 1 for inspection. It does not mark failure terminal
or automatically classify HTTP errors yet. Restarting after visibility/lease
expiry can attempt delivery again; a timeout or crash after Slack accepted a
request can therefore produce duplicate notifications. Do not configure automatic
restart for send failures until retry limits and failure policy are implemented.
Database/SQS errors use a five-second reconnect/retry delay. A deletion failure
after success is safe: a later receipt sees the terminal database state and skips
the HTTP call.

Still remaining: retryable/permanent error classification, attempt limits,
`Retry-After` handling, DLQ configuration/recovery, SSM secret resolution, and a
dedicated Compose worker service. `docs/architecture.md` describes the full target
design, including these features that are not yet implemented.

### Optional standalone worker invocation

This command sends real Slack messages when configured with a real webhook.
It has not been executed as part of local verification. First finish the local
database/queue/publisher setup above. The publisher must run separately to put
delivery IDs on the queue. For a destination belonging to tenant 1, register
`env:SLACK_WEBHOOK_URL_TENANT_1_DEMO` as its `webhook_url_ref` and set the matching
`SLACK_WEBHOOK_URL_TENANT_1_DEMO` environment variable in your shell to the secret
Slack URL using your normal secure secret-entry method. Use your actual tenant ID;
the earlier demo placeholder `env:SLACK_WEBHOOK_URL_LOCAL` is not a valid worker
reference. Keep the URL out of SQL, logs, committed files, and shell history.

Reuse the publisher's database/SQS environment with a different command:

```powershell
docker compose --profile publisher run --rm --build -e SLACK_WEBHOOK_URL_TENANT_1_DEMO publisher python delivery_worker.py
```

The `-e` option forwards the existing shell variable without putting its value
in the command. Compose does not automatically pass arbitrary variables from
`.env` to this container. The existing `SQS_QUEUE_URL` must identify the queue
created earlier, and local publishing/consumption uses `SQS_ENDPOINT_URL=http://sqs:5000`
with dummy AWS credentials. No worker service or cloud resources are provisioned
by this implementation.

An eventual AWS worker task role needs `sqs:ReceiveMessage`, `sqs:DeleteMessage`,
and `sqs:ChangeMessageVisibility` scoped to its delivery queue. The publisher's
send-only permission is insufficient for the worker.

## Tests

With the database running, run:

```powershell
docker compose --profile test build tests
docker compose --profile test run --rm tests
```

The development image includes pytest; the production image does not. Tests use
the existing PostgreSQL service with a unique temporary schema per test, apply
the migration there, and drop only that schema afterward. The database user must
be allowed to create schemas. Use local disposable development data, not a remote
production database. An interrupted test may leave a `test_outbox_...` schema for
manual inspection and cleanup. Ingestion tests use the same isolation approach
with `test_ingestion_...` schemas.

`tests/test_delivery_worker.py` reuses the publisher suite's isolated schema and
Moto fixtures. It covers competing claims, expiry and stale-token rejection,
deferred/terminal jobs, failed sends, completion/deletion failures, and a real
publisher-to-Moto-to-worker-to-local-HTTP flow with duplicate suppression after
success. `tests/test_slack_sender.py` tests secret isolation, URL/DNS restrictions,
the actual IP passed to the connector, proxy bypass prevention, redirect refusal,
and DNS/response timeouts. Local HTTP URLs are wired in only by tests; production
has no option to disable destination validation. No real Slack messages are sent.

Verified locally on November 10, 2026: **72 tests passed in 36.90 seconds** with
PostgreSQL 16, Moto, and aiohttp 3.14.5. The test image built successfully and
Compose configuration validation passed. This verifies the initial worker and
sender plus the existing ingestion/publisher behavior; it does not verify real
Slack delivery, AWS permissions, or the remaining retry/DLQ design.

Compose starts the local emulator automatically for tests and waits for its
health check. Five focused tests use a fake SQS sender; three integration tests
use real boto3 HTTP requests with explicit local endpoints and dummy credentials.
Each integration test creates and deletes unique queues. No AWS access is needed.
Without `TEST_SQS_ENDPOINT_URL`, the three emulator tests are skipped.

Coverage includes success, delayed retry, future jobs, competing database locks,
rollback recovery, and the duplicate window after sending before commit. Process
tests cover invalid/valid signatures, duplicate ingestion, missing-queue failure
and recovery on the real retry schedule, an unreachable endpoint, graceful stop,
and restart with both pending and published jobs. Child processes are terminated
and awaited before their schemas and queues are removed.

`tests/test_webhook_ingestion.py` additionally tests disabled/nonmatching routing,
concurrent duplicate requests, full rollback on delivery/outbox insertion failure,
cross-tenant rule constraints, and invalid/oversized payloads. It calls the actual
ASGI application with HTTPX and uses PostgreSQL without requiring another server
process or an additional testing dependency.

Historical ingestion/publisher suite verification on November 10, 2026:
**22 tests passed in
31.48 seconds**, including 14 ingestion cases and the eight existing publisher
tests. The development image build and Compose configuration validation passed.
The standalone manual demo instructions above were not executed in this run.

Historical publisher-only verification on November 10, 2026 (before the separate
ingestion tests were added): **8 tests passed in 24.93 seconds** using
PostgreSQL 16 and `motoserver/moto:5.1.21`. The test image built successfully and
Compose configuration validation passed. The end-to-end test observed HTTP 401
for an invalid signature, 202 for a valid webhook, and 200 for its duplicate,
with one event, delivery, outbox job, and queue message. All publisher subprocesses
stopped gracefully; restart tests preserved pending work and skipped published
work. A post-run inspection found zero remaining test schemas and zero emulator
queues. No AWS resources or Git commits were created for this verification.

The optional manual emulator-stop commands below have not been executed. The
automated suite instead verifies a real missing-queue HTTP error followed by
scheduled recovery, and a refused endpoint followed by publisher restart.
Moto is an emulator; AWS IAM enforcement, durable queue behavior, and actual
GitHub network delivery are outside this local verification.

For optional manual outage verification with a configured demo publisher:

```powershell
docker compose stop sqs
# Submit a signed webhook matching an enabled rule; inspect outbox and logs.
docker compose logs --tail 30 publisher
docker compose --profile publisher start sqs
# Recreate the demo queue if Moto lost its in-memory state.
docker compose --profile publisher restart publisher
```

Confirm pending attempts increase during the outage, `last_error` is sanitized,
and the same job becomes published after recovery. A published row must not be
resent simply because the publisher restarts. This manual emulator-stop scenario
is distinct from the automated missing-queue and refused-endpoint tests.

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
| `main.py` | FastAPI health endpoints and signed GitHub webhook ingestion |
| `outbox_publisher.py` | PostgreSQL polling, SQS publication, and retries |
| `delivery_worker.py` | SQS reception, database leases, and successful delivery completion |
| `slack_sender.py` | Tenant-bound local secrets and restricted Slack HTTP delivery |
| `tests/test_outbox_publisher.py` | PostgreSQL-backed publisher behavior tests |
| `tests/test_webhook_ingestion.py` | Routing, concurrent deduplication, rollback, tenant constraints, and payload validation tests |
| `tests/test_delivery_worker.py` | Worker state, ordering, and local queue-to-HTTP tests |
| `tests/test_slack_sender.py` | Secret, destination, DNS, and HTTP behavior tests |
| `requirements.txt` | FastAPI, Uvicorn, psycopg, boto3, and aiohttp dependencies |
| `Dockerfile` | Python 3.13 application image running as a non-root user |
| `compose.yaml` | API, PostgreSQL 16, readiness check, networking, and storage |
| `.env.example` | Example local configuration to copy to `.env` |
| `.dockerignore` | Excludes secrets and generated files from the build context |
| `.gitignore` | Excludes local secrets, environments, and generated files from Git |
| `migrations/001_initial_schema.sql` | Initial tables, relationships, constraints, and indexes |
| `docs/architecture.md` | Planned system architecture and development stages |
