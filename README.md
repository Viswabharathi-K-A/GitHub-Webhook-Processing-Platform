# GitHub Webhook Processing Platform

A multi-tenant GitHub webhook platform under development. This first step provides
a local FastAPI application, PostgreSQL 16 with psycopg 3, and an initial SQL
schema. Signed webhook ingestion, an outbox publisher, and a delivery worker with
bounded retries are implemented, with Compose worker integration and explicit
local queue/DLQ setup. A manual local DLQ recovery procedure is provided below;
AWS infrastructure remains future work.

## Remaining roadmap

The detailed [architecture and roadmap](docs/architecture.md#7-development-roadmap)
distinguish implemented behavior from planned work:

| Stage | Goal |
|---|---|
| 2 — Delivery worker | Slack-only delivery with PostgreSQL idempotency/leases, bounded retries, SQS visibility and practical DLQ recovery |
| 3 — Tenant isolation | Operator-managed SQL configuration, tenant-scoped secret references, isolation tests and simple CI |
| 4 — Temporary AWS deployment | One readable Terraform root, minimal ECS/RDS/SQS/ALB infrastructure, scoped IAM and HTTPS in us-west-2 |
| 5 — Operational verification | Useful logs/alarms, failure and recovery checks, bounded load measurements and lightweight deployment procedures |
| 6 — Portfolio and cleanup | Record real evidence, document tradeoffs, destroy and inspect billable resources |

Security and tests are part of every stage. Delivery attempts are at least once;
Slack notifications can duplicate after uncertain outcomes. The project does
not promise exactly-once delivery. Configuration is managed by the operator
through SQL; there is no public management API or separate API-key system.
Keep explicit SQL and the existing codebase instead of adding frameworks.
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
stale workers cannot overwrite a newer claim. The publisher and delivery worker
include failure handling and retries. Tenant/source/destination/rule configuration
uses explicit operator-run SQL, as shown in the local demonstration below.

Secret reference columns must identify recoverable values in a secure secret
store; they must not contain raw GitHub secrets or Slack webhook URLs. The secret
store implementation is deferred. Error fields should contain only sanitized
messages, never secrets or sensitive payloads. Applications must still authenticate
webhooks and scope queries by tenant; foreign keys do not provide read isolation.

## Minimal tenant security

This portfolio project has no self-service management endpoints, API-key table,
or administrative CLI. A trusted operator creates and updates configuration using
database credentials and explicit SQL transactions. Use the existing demo SQL
below; substitute the actual tenant/source/destination IDs when changing records.
Disable records with tenant-scoped updates rather than deleting delivery history:

```sql
UPDATE rules SET enabled = false WHERE tenant_id = YOUR_TENANT_ID AND id = YOUR_RULE_ID;
```

Public GitHub requests are authenticated using raw-body HMAC-SHA256 verification.
The API derives tenant identity from the registered source, not the request
payload. Composite foreign keys prevent cross-tenant links; routing queries also
include tenant ownership. Use a separate secret value for each tenant's source.
Existing tests cover cross-tenant constraints, signatures, and routing isolation.

Secret references are restricted at resolution time to:

```text
env:GITHUB_WEBHOOK_SECRET_TENANT_<tenant_id>_<NAME>
env:SLACK_WEBHOOK_URL_TENANT_<tenant_id>_<NAME>
```

`<NAME>` contains uppercase letters, digits, and underscores. The host `.env`
variable `GITHUB_WEBHOOK_SECRET_LOCAL` stays unchanged: Compose passes it to the
API as `GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL`. Slack's existing tenant-1 demo
mapping is unchanged. Provision distinct values and explicit container environment
mappings for other tenants; database reference columns contain no raw secrets.

Existing sources using the old unscoped GitHub reference need a one-time operator
update before starting the new API image. For the existing tenant-1 demo, run this
in `psql` (no schema migration is needed):

```sql
BEGIN;
UPDATE sources
SET webhook_secret_ref = 'env:GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL'
WHERE tenant_id = 1 AND webhook_secret_ref = 'env:GITHUB_WEBHOOK_SECRET_LOCAL'
RETURNING id, tenant_id, webhook_secret_ref;
COMMIT;
```

This update is not automatic and does not change the secret value or GitHub's
configured secret. Coordinate the reference update and API recreation because
the old API image expects the old name. Other legacy references require their
own tenant-specific mapping/update. Migration `001` remains the only migration;
there is no migration `002` to apply.

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
duplicates. The worker handles terminal jobs without resending; publication is not exactly once.

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

The Compose publisher and worker depend on the local emulator's health check even
when configured for AWS. A blank endpoint selects AWS; explicitly set
`http://sqs:5000` for local publishing.

For a local publisher demonstration, set `SQS_ENDPOINT_URL=http://sqs:5000`,
`AWS_ACCESS_KEY_ID=test`, `AWS_SECRET_ACCESS_KEY=test`, and
`AWS_DEFAULT_REGION=us-west-2` in your uncommitted `.env`. Clear any AWS session
token. Create or configure both local queues explicitly:

```powershell
docker compose --profile setup run --rm --build queue-init
```

`queue-init` waits for Moto, runs `setup_local_sqs.py`, and exits. The script uses
only `http://sqs:5000`, region `us-west-2`, and fixed dummy credentials, ignoring
ambient AWS endpoints and HTTP proxies. It does not create AWS resources.

| Queue | Settings |
|---|---|
| `deliveries` | Standard queue; visibility 90 seconds; retention 4 days; redrive after 10 unacknowledged receives |
| `deliveries-dlq` | Standard dead-letter queue; retention 14 days |

The DLQ is created first, then its ARN is attached to the delivery queue's redrive
policy. Rerunning setup updates attributes without deleting or purging queues.
Existing messages are preserved by the script; normal retention expiry still
applies, including if you shorten a previously configured retention period.
Moto remains in-memory: rerun setup after it loses its state. Neither publisher
nor worker starts queue setup automatically.

The script prints `SQS_QUEUE_URL` and a separate DLQ URL. Copy the delivery URL
into `.env`; the worker consumes the delivery queue, never the DLQ. Its five
claimed attempts are separate from SQS receive count: active leases, early
receipts, and infrastructure errors can consume receives. Terminal application
failures are committed and deleted, not automatically moved to the DLQ. Messages
the worker cannot acknowledge, such as malformed messages, can reach the DLQ.
See the local DLQ recovery procedure below for operator inspection and replay.
Moto redrive tests do not establish AWS retention or IAM behavior.

After applying the initial migration and
setting a nonempty `GITHUB_WEBHOOK_SECRET_LOCAL` in `.env`, start the API and
publisher. Tenant-1 source rows must reference
`env:GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL` for this Compose configuration.
The integration tests generate their own source, rule,
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
VALUES (:demo_tenant_id, 'local-github', 'env:GITHUB_WEBHOOK_SECRET_TENANT_' || :demo_tenant_id || '_LOCAL')
RETURNING id AS demo_source_id \gset
INSERT INTO destinations (tenant_id, name, webhook_url_ref)
VALUES (:demo_tenant_id, 'local-placeholder', 'env:SLACK_WEBHOOK_URL_LOCAL')
RETURNING id AS demo_destination_id \gset
INSERT INTO rules (tenant_id, source_id, destination_id, event_type)
VALUES (:demo_tenant_id, :demo_source_id, :demo_destination_id, 'push');
COMMIT;
\echo Demo tenant ID: :demo_tenant_id
\echo Demo source ID: :demo_source_id
\q
```

Copy the printed tenant and source IDs into the prompts below. Compose maps
only tenant 1 by default; provision the matching API environment variable first
if this example created a different tenant ID. The Python code runs inside
the API container, reads its configured secret without printing it, and signs
the exact bytes sent over HTTP. It submits the same delivery twice:

```powershell
$demoSourceId = Read-Host 'Demo source ID'
$demoTenantId = Read-Host 'Demo tenant ID'
@'
import hashlib
import hmac
import os
import sys
import urllib.request
from uuid import uuid4

source_id = int(sys.argv[1])
body = b'{"ref":"refs/heads/main","repository":{"name":"local-demo"}}'
secret = os.environ[f'GITHUB_WEBHOOK_SECRET_TENANT_{int(sys.argv[2])}_LOCAL'].encode()
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
'@ | docker compose exec -T api python - $demoSourceId $demoTenantId
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

## Delivery worker and retries

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
| Retryable failure with attempts remaining | Commit `pending`, retry time, and sanitized reason; clear the lease before deferring visibility |
| Permanent failure or exhausted attempts | Commit `failed` and completion time, clear the lease, then delete the message |
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

Each delivery has at most **five claimed attempts**, including claims interrupted
by a crash before an HTTP response or database outcome. The first four retryable
failures wait **5, 10, 20, and 40 seconds**. The fifth failure is terminal. An active
fifth claim is left alone; after expiry it is marked failed without a sixth send.
Handled send failures no longer stop the worker.

Timeouts, DNS/connection failures, HTTP 408/429, and HTTP 5xx are retryable.
Invalid or disabled destinations, invalid/missing local secret references, TLS
validation failures, redirects, other HTTP failures, and unexpected success bodies
are terminal. A missing/expired lease is not a destination failure: the stale
worker neither updates the job nor deletes the message.

A valid whole-number `Retry-After` on a retryable response is a minimum wait;
the worker uses the greater of that delay and normal backoff. Missing, negative,
fractional, and malformed values fall back to backoff. Slack documents seconds,
so HTTP-date values are not interpreted. Values beyond 2,147,483,647 seconds are
recorded as terminal `retry_after_out_of_range` for inspection rather than silently
shortened. PostgreSQL retains the full due time; visibility is deferred in intervals
of at most one hour. Early receipts check that due time without claiming another
attempt. This avoids requesting SQS's exact maximum visibility allowance.

Retry/failure updates require the current unexpired lease, checked after acquiring
the row lock. The database commit precedes visibility changes and deletion. If a
visibility change fails, the next receipt still observes the committed retry time.
If a database update fails, the message stays unacknowledged and the claim can be
recovered after expiry. Database/SQS errors use a five-second reconnect/retry delay.
A deletion failure after either terminal outcome is safe: a later receipt skips
HTTP and retries deletion. Terminal failures remain inspectable in `deliveries`;
they are not automatically sent to a DLQ. A timeout or crash after Slack accepts
a request can still produce a duplicate notification.

Still remaining: AWS DLQ recovery verification and SSM secret resolution.
`docs/architecture.md` describes the full target
design, including these features that are not yet implemented.

### Start the Compose worker

This command sends real Slack messages when configured with a real webhook.
It has not been executed as part of local verification. First finish the local
database/queue/publisher setup above. The publisher must run separately to put
delivery IDs on the queue. For a destination belonging to tenant 1, register
`env:SLACK_WEBHOOK_URL_TENANT_1_DEMO` as its `webhook_url_ref` and set the matching
`SLACK_WEBHOOK_URL_TENANT_1_DEMO` variable in your uncommitted `.env` or shell to the
secret Slack URL using your normal secure secret-entry method. Use your actual tenant ID;
the earlier demo placeholder `env:SLACK_WEBHOOK_URL_LOCAL` is not a valid worker
reference. Keep the URL out of SQL, logs, committed files, and shell history.

The worker has its own service and waits for healthy PostgreSQL and Moto. The
demo secret mapping is explicit and is passed only to the worker. For another
tenant, add its corresponding `SLACK_WEBHOOK_URL_TENANT_<id>_<NAME>` mapping under
the worker's `environment` and set that variable locally. No arbitrary `.env`
variables are forwarded. Start the complete local set after queue setup:

```powershell
docker compose --profile publisher --profile worker up --build -d api publisher worker
docker compose logs --tail 30 worker
docker compose stop worker publisher
```

The `publisher` and `worker` profiles are independent; normal `docker compose up`
starts only API/database. The separate `setup` profile is for explicit queue setup.
The existing `SQS_QUEUE_URL` must identify the delivery queue created earlier;
local publishing/consumption uses `SQS_ENDPOINT_URL=http://sqs:5000` with dummy AWS
credentials. Queue setup does not edit `.env`. The worker has a 30-second shutdown
grace period; the documented OS DNS shutdown limitation still applies.

An eventual AWS worker task role needs `sqs:ReceiveMessage`, `sqs:DeleteMessage`,
and `sqs:ChangeMessageVisibility` scoped to its delivery queue. The publisher's
send-only permission is insufficient for the worker.

### Inspect and recover one local DLQ message

This procedure is for Compose/Moto only, not AWS. Start the database and Moto
and run queue setup first. Fix the underlying database/worker problem before
replaying anything. Replaying a recoverable delivery can send a real Slack message.
Do not purge either queue or reset delivery statuses/attempt counts for recovery.

Open an interactive Python session in a temporary worker container. This overrides
the worker command; it does not start another message consumer or modify files:

```powershell
docker compose --profile worker run --rm --no-deps worker python -i
```

Paste the following at the Python prompt. Use only the fixed local endpoint and
dummy credentials below, regardless of the AWS settings in `.env`:

```python
import json
import boto3
import psycopg
from botocore.config import Config
from delivery_worker import database_config

sqs = boto3.client(
    'sqs', endpoint_url='http://sqs:5000', region_name='us-west-2',
    aws_access_key_id='test', aws_secret_access_key='test', aws_session_token='',
    config=Config(connect_timeout=3, read_timeout=5,
                  retries={'total_max_attempts': 1}, proxies={}),
)
queue_url = sqs.get_queue_url(QueueName='deliveries')['QueueUrl']
dlq_url = sqs.get_queue_url(QueueName='deliveries-dlq')['QueueUrl']
print(sqs.get_queue_attributes(
    QueueUrl=dlq_url,
    AttributeNames=['ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible'],
)['Attributes'])
```

Counts are approximate. To inspect one message, run:

```python
messages = sqs.receive_message(
    QueueUrl=dlq_url, MaxNumberOfMessages=1, WaitTimeSeconds=1,
    VisibilityTimeout=300,
    MessageSystemAttributeNames=['ApproximateReceiveCount'],
).get('Messages', [])
print('Messages received:', len(messages))
```

If none is returned, stop or poll again; one empty receive does not prove the
queue is empty. Receiving changes the receive count and hides that message for
five minutes. It does not delete it. Continue only when one message was received:

```python
message = messages[0]
payload = json.loads(message['Body'])
assert isinstance(payload, dict), 'Malformed message; leave in DLQ'
delivery_id = payload.get('delivery_id')
assert type(delivery_id) is int and 0 < delivery_id <= 9223372036854775807, 'Invalid delivery ID; leave in DLQ'
print('Message ID:', message['MessageId'], 'Delivery ID:', delivery_id)
print('Receive count:', message.get('Attributes', {}).get('ApproximateReceiveCount'))
query = (
    'SELECT id, tenant_id, status, attempt_count, last_error, '
    "(attempt_count < 5 AND ((status = 'pending' AND next_attempt_at <= clock_timestamp()) "
    "OR (status = 'processing' AND lease_expires_at <= clock_timestamp()))) AS ready "
    'FROM deliveries WHERE id = %s'
)
with psycopg.connect(**database_config()) as connection:
    row = connection.execute(query, (delivery_id,)).fetchone()

print(row)
```

The tuple contains delivery ID, tenant ID, status, attempt count, sanitized error
code, and whether it is ready to retry. Do not print raw message bodies, event
payloads, environment values, or destination URLs. If validation fails, stop;
do not continue to the replay commands.

Decide based on the database record:

| Result | Operator action |
|---|---|
| Missing record or malformed message | Leave in DLQ and investigate; do not invent a replacement delivery ID |
| `pending`, not yet due, or an active `processing` lease | Wait until due/expired and inspect again |
| Nonterminal delivery with `ready = True` | After fixing the cause and confirming the tenant/ID, replay below |
| `succeeded` | No replay needed; delete only after explicitly confirming it is a stale duplicate |
| `failed`, or five attempts already used | Investigate the stored failure; replay will not grant a new attempt or reset the delivery |

For a ready delivery, recheck the database and run these statements **one at a
time**, within the visibility window. Stop on any exception or failed assertion:

```python
with psycopg.connect(**database_config()) as connection:
    row = connection.execute(query, (delivery_id,)).fetchone()

assert row is not None and row[-1] is True, 'Delivery is not ready for recovery'
sent = sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({'delivery_id': delivery_id}))
assert sent.get('MessageId'), 'Send did not confirm success; keep DLQ message'
deleted = sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=message['ReceiptHandle'])
print('Requeued delivery:', delivery_id)
```

Delete only after the send succeeds. If sending fails or its outcome is uncertain,
leave the DLQ message. A crash or deletion failure after sending can produce a
duplicate; the worker uses database status/leases to handle repeated messages.
The readiness check is advisory: the worker rechecks state when claiming the job.
This does not promise exactly-once Slack delivery after an uncertain HTTP outcome.

If abandoning inspection before deletion, release the message early with:

```python
released = sqs.change_message_visibility(QueueUrl=dlq_url, ReceiptHandle=message['ReceiptHandle'], VisibilityTimeout=0)
```

If the receipt expired, receive and inspect again rather than using an old receipt.
For a confirmed `succeeded` duplicate, the delete statement above may be used on
its own after rechecking the ID/status; it must not be used for an unresolved job.
Close the session with `sqs.close()` and `exit()`. Start the normal worker if it
was stopped, then inspect the specific delivery's status/attempt count and worker
logs. Verify `succeeded` or investigate its recorded failure; do not infer success
from an empty queue alone. No database reset is part of this procedure.

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

The retry tests additionally exercise backoff, early duplicate receipts, final
claim expiry, stale failure updates, database/visibility/deletion failures,
`Retry-After` parsing and long delays, and real Moto/HTTP retry followed by success.

Local queue/Compose stage verified on November 11, 2026: **97 tests passed in
47.87 seconds**. `tests/test_local_sqs_setup.py` verifies queue attributes,
repeatable updates that preserve messages, and Moto redrive after ten receives.
Both the test image and queue-init image built successfully. Compose validation
passed, and the actual `queue-init` command succeeded twice with the same queue
URLs. The local `deliveries` and `deliveries-dlq` queues remain configured in Moto;
`.env` was not modified. No worker was started against a real Slack destination.

Previous retry-stage verification on November 11, 2026: **95 tests passed in 43.70 seconds** using
PostgreSQL and Moto, including the existing ingestion/publisher/sender suite.
The test image build, Compose configuration validation, and whitespace checks
passed. The real queue-to-local-HTTP retry test waited for the persisted retry
schedule and message visibility before succeeding. No AWS resources or real Slack
notifications were used. DLQ recovery and cloud behavior remain unverified.

Historical verification on November 10, 2026: **72 tests passed in 36.90 seconds** with
PostgreSQL 16, Moto, and aiohttp 3.14.5. The test image built successfully and
Compose configuration validation passed. This verifies the initial worker and
sender plus the existing ingestion/publisher behavior; it does not verify real
Slack delivery, AWS permissions, or the remaining retry/DLQ design.

Compose starts the local emulator automatically for tests and waits for its
health check. In the publisher suite, five focused tests use a fake SQS sender; three integration tests
use real boto3 HTTP requests with explicit local endpoints and dummy credentials.
Each integration test creates and deletes unique queues. No AWS access is needed.
Without `TEST_SQS_ENDPOINT_URL`, emulator integration tests are skipped.

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
| `setup_local_sqs.py` | Explicit, repeatable Moto delivery queue and DLQ configuration |
| `tests/test_local_sqs_setup.py` | Queue settings, repeatability, message preservation, and Moto redrive tests |
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
