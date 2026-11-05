# Multi-Tenant GitHub Webhook Processing Platform

## 1. System overview

This project is a multi-tenant, event-driven backend platform that receives GitHub webhooks, verifies their authenticity, stores events, applies configurable routing rules, and asynchronously delivers notifications to Slack.

The purpose is to demonstrate practical backend engineering, PostgreSQL transactions, asynchronous processing, AWS infrastructure, security, and failure recovery. The implementation should prioritize correctness and simple, understandable code over extra frameworks or features.

**MVP scope:** GitHub is the only event source; Slack Incoming Webhooks are the only notification destination. Multiple tenants can configure their own sources, destinations, and routing rules.

**Deployment plan:** Develop and test locally, deploy to AWS for a real end-to-end demonstration, record results and screenshots, then destroy billable resources. The system is not intended to run continuously.

## 2. Architecture diagram

```mermaid
flowchart TD
    GH[GitHub webhook] --> ALB[Application Load Balancer]
    ALB --> API[FastAPI on ECS Fargate]
    API -->|Validate HMAC, dedupe, save event and deliveries atomically| DB[(RDS PostgreSQL)]
    DB -->|Read unpublished delivery records| PUB[Outbox publisher]
    PUB -->|Send delivery ID| SQS[Amazon SQS standard queue]
    SQS --> WORKER[ECS delivery worker]
    WORKER -->|Send notification| SLACK[Slack Incoming Webhook]
    WORKER -->|Read and update delivery state| DB
    SQS -->|Exceeded retry policy| DLQ[SQS dead-letter queue]
    PUB -->|Mark published after successful send| DB
```

This is a **logical** diagram, not a complete networking diagram. The publisher and delivery worker may initially run as two loops in the same background ECS service, using the same codebase as the API but a different startup command. The API and worker both access PostgreSQL. The worker needs outbound internet access to Slack.

### AWS network placement

- Internet-facing ALB in public subnets.
- API and worker ECS Fargate tasks in private application subnets.
- RDS PostgreSQL in private database subnets, not publicly accessible.
- Security groups allow only necessary communication (ALB to API; API/worker to RDS).
- NAT Gateway or carefully selected VPC endpoints provide required outbound connectivity; Slack requires internet egress.
- IAM task roles grant only required permissions; ECS execution role covers image pulls, logging, and injected secrets.
- HTTPS is required before sending real credentials or authenticated management requests over the public internet. A suitable domain/certificate strategy must be selected before deployment.

## 3. Technology stack

| Area | Technology | Purpose |
|---|---|---|
| Language | Python 3.13 | Backend and worker code |
| HTTP API | FastAPI + Uvicorn | Webhook ingestion and management endpoints |
| Database | PostgreSQL 16 | Durable events, configuration, and delivery state |
| Database access | psycopg 3 + psycopg_pool | Explicit SQL, transactions, connection management |
| Migrations | Versioned SQL files | Controlled schema evolution |
| Local runtime | Docker + Docker Compose | API, database, and background processes |
| Testing | pytest + HTTPX | API, integration, and failure tests |
| Container compute | Amazon ECS on Fargate | API and background worker |
| Registry | Amazon ECR | Store Docker images |
| Load balancing | AWS Application Load Balancer | Route external HTTP(S) requests |
| Managed database | Amazon RDS for PostgreSQL | Cloud-hosted persistent database |
| Queue | Amazon SQS + dead-letter queue | Asynchronous delivery and failed-message isolation |
| Destination | Slack Incoming Webhooks | Send notifications |
| Infrastructure as code | Terraform | Reproducible AWS provisioning and teardown |
| Monitoring | Amazon CloudWatch | Logs, metrics, and alarms |
| CI | GitHub Actions | Tests and validation |
| Secrets | AWS Secrets Manager or SSM Parameter Store | Store sensitive runtime configuration |

**Not planned for MVP:** SQLAlchemy ORM, React dashboard, Kubernetes, Kafka, Redis, SES, S3 archiving, or multiple destination types.

## 4. Component responsibilities

### FastAPI webhook receiver

Endpoint: `POST /webhooks/github/{source_id}`.

- Look up the registered source and its secret.
- Verify `X-Hub-Signature-256` against the **raw request bytes**, using HMAC-SHA256 and constant-time comparison.
- Validate body size and relevant GitHub headers.
- Deduplicate on `(source_id, github_delivery_id)` with a database uniqueness constraint.
- Evaluate the source's routing rules.
- In one PostgreSQL transaction, insert the event and any pending delivery rows.
- Return promptly without waiting for Slack delivery.
- Expose separate authenticated management endpoints for tenants, sources, Slack destinations, and rules.

### PostgreSQL

Initial tables (exact columns and constraints will be designed before implementation):

| Table | Responsibility |
|---|---|
| `tenants` | Tenant records |
| `api_keys` | Hashed high-entropy management API keys and tenant association |
| `sources` | Registered GitHub webhook sources and securely stored secrets |
| `destinations` | Tenant-owned Slack destination configuration |
| `rules` | Event-to-destination routing rules |
| `events` | Accepted GitHub events and delivery identifiers |
| `deliveries` | Per-destination jobs, outbox publication state, attempts, leases, and results |

Use tenant-scoped queries and composite constraints where necessary to prevent cross-tenant references. Store secret material securely; webhook HMAC secrets must be recoverable for verification and cannot be stored only as hashes.

### Outbox publisher

- Find delivery rows that have not been published to SQS.
- Safely coordinate concurrent publishers (for example, using PostgreSQL row locking/claims).
- Send messages containing only a `delivery_id`.
- Mark publication successful **only after** SQS confirms the send.
- Retry failed publications without losing committed events.
- Accept that a crash after SQS accepts a message but before PostgreSQL is updated can cause duplicate queue messages.

### Amazon SQS

- Standard queue for delivery IDs; at-least-once message delivery, no ordering guarantee.
- Visibility timeout and retry settings must be consistent with worker execution and lease duration.
- Dead-letter queue captures messages that exceed the retry policy.
- A recovery/reconciliation process must handle stale database leases and delivery rows associated with DLQ messages.

### ECS delivery worker

- Poll SQS, load the delivery and destination from PostgreSQL, and safely claim the job.
- Post a concise notification to the configured Slack Incoming Webhook URL.
- Track attempts and distinguish success, permanent failure, and retryable failure.
- Delete SQS messages only when it is safe to do so; a competing worker's active lease alone is not sufficient reason to discard a message.
- Retry transient failures with bounded attempts and suitable backoff.
- Record outcomes in PostgreSQL; reconcile expired leases and dead-lettered jobs.
- Never claim exactly-once delivery to Slack: a crash after Slack accepts a request but before recording success can cause a duplicate notification.

### Observability

- Structured logs with request/event/delivery correlation IDs.
- Do not log secrets, API keys, full sensitive payloads, or Slack webhook URLs.
- CloudWatch alarms for queue age, DLQ depth, and meaningful application failures.
- Capture performance measurements and failure-recovery evidence for the README.

## 5. End-to-end event flow

1. A GitHub repository generates a push, pull-request, or workflow event.
2. GitHub sends an HTTP POST to the public webhook endpoint through the ALB.
3. FastAPI finds the source, verifies the HMAC signature against the raw body, and rejects invalid requests.
4. FastAPI checks the GitHub delivery ID. A previously accepted delivery is handled idempotently.
5. FastAPI evaluates tenant-specific rules and commits an event plus pending delivery rows in a single database transaction.
6. FastAPI acknowledges the accepted event without waiting for Slack.
7. The outbox publisher reads unpublished deliveries, sends delivery IDs to SQS, and records successful publication.
8. The worker receives an SQS message and safely claims the corresponding database delivery.
9. The worker sends a Slack notification and records the result.
10. On success, the worker deletes the queue message. On transient failure, the job is retried according to policy. Repeated failures reach the DLQ and must be reconciled with database state.

### Example SQS message

```json
{"delivery_id": "delivery-123"}
```

The database remains the source of truth; the queue message does not need to contain webhook secrets or the entire GitHub payload.

## 6. Important design decisions and tradeoffs

### Simplicity first

Use one Python codebase, explicit SQL, and a small number of modules. Prefer direct, understandable functions to elaborate abstractions. Add files and dependencies only when there is a concrete need. Codex is an advisor/reviewer and must not edit without approval, per `AGENTS.md`.

### Transactional outbox

Saving an event and sending to SQS cannot be one PostgreSQL transaction. Therefore, commit the event and pending delivery records together, then publish them asynchronously. Reuse `deliveries` as the outbox initially, while keeping **queue publication state** separate from **notification delivery state**.

### Idempotency and delivery guarantees

- Unique `(source_id, github_delivery_id)` for webhook ingestion.
- Worker operations must tolerate repeated SQS messages and crashes.
- SQS standard queues do not guarantee ordering.
- Slack notifications cannot be guaranteed exactly once. Document at-least-once attempts and possible duplicates after uncertain outcomes.

### Multi-tenancy

Every management operation must authenticate and enforce the tenant boundary. Database queries and relationships must prevent one tenant from accessing another tenant's sources, rules, events, or destinations. Include cross-tenant tests.

### Security

- Verify GitHub HMAC signatures using raw bytes.
- Protect management APIs with high-entropy API keys, stored as hashes.
- Protect source secrets and Slack webhook URLs; do not expose them in logs or API responses.
- Slack-only destinations reduce arbitrary-URL SSRF risk. Restrict outbound destinations to the expected Slack webhook hosts, disable redirects, and validate outbound connections carefully.
- Use HTTPS for real deployments, least-privilege IAM, private RDS, and restricted security groups.

### Worker choice

Use an ECS worker rather than Lambda for the initial implementation. This allows straightforward reuse of psycopg/database code and avoids introducing another compute model. Lambda can be discussed as an alternative, not added merely to increase the AWS service count.

### Local-first development

Build and test ingestion, outbox, retries, and the worker locally before AWS deployment. Use a local SQS-compatible emulator or a temporary development SQS queue; choose the simplest practical option during implementation.

### One-time AWS deployment

Deploy only after the local system works. Configure budget alerts (which are **not** hard spending caps). Record an authentic end-to-end demo, test results, screenshots, and Terraform configuration. Run `terraform destroy` and verify that no billable resources or unmanaged snapshots, load balancers, IPs, logs, or other resources remain. AWS charges may be delayed in billing reports.

## 7. Development roadmap

| Stage | Work | Deliverable |
|---|---|---|
| 0 — Foundation | FastAPI health endpoint, PostgreSQL, psycopg, Docker Compose, basic tests | Working local API and database |
| 1 — Ingestion | Tenant/API key configuration, sources, rules, HMAC verification, deduplication, atomic writes | Secure GitHub webhook receiver |
| 2 — Async processing | Delivery/outbox schema, publisher, SQS, ECS-style worker, Slack, retries, DLQ, failure tests | Reliable local event pipeline |
| 3 — AWS deployment | Terraform VPC, subnets, IAM, ECR, ECS, ALB, RDS, SQS, secrets, HTTPS | Working cloud deployment |
| 4 — Operations | CloudWatch logging/alarms, CI, isolation/security checks, load and crash tests | Measured, reviewable system |
| 5 — Portfolio and cleanup | Real demo video, screenshots, architecture README, design tradeoffs, Terraform teardown | Resume-ready project with no intentionally running AWS infrastructure |

**Development rule:** Implement one small feature at a time, test it, understand it, and only then continue. Avoid speculative functionality.

## 8. What to ask Codex

> Read `AGENTS.md` and `docs/architecture.md`. Review the current design or file for correctness, security, and unnecessary complexity. Suggest only changes with a clear benefit, distinguish must-fix issues from optional improvements, and explain the reasoning. Do not edit files or run deployment commands. Propose a plan and wait for my approval; if approved, work one file at a time unless I explicitly request otherwise.

## 9. Open implementation decisions

These should be resolved incrementally rather than guessed in advance:

- Exact SQL schema and delivery state transitions.
- Publisher claiming strategy and recovery after partial SQS publication.
- Worker leases, retry scheduling, visibility timeouts, and DLQ reconciliation.
- Safe Slack webhook URL validation and secret storage.
- Local SQS emulator versus temporary AWS queue.
- HTTPS endpoint/domain strategy for the one-time deployment.
- Minimal Terraform layout and deployment/teardown scripts.
