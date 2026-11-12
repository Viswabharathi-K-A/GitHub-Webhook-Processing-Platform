import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import psycopg
import pytest
from aiohttp import web
from botocore.exceptions import ClientError

import delivery_worker as worker
import slack_sender
from outbox_publisher import publish_one
from slack_sender import SlackSendError
from test_outbox_publisher import database, local_sqs
from test_slack_sender import local_receiver


@pytest.fixture
def worker_database(database, monkeypatch):
    # Reuse the existing isolated PostgreSQL schema and single pending job.
    connection, schema = database
    connection.execute("""
        UPDATE destinations SET webhook_url_ref = 'env:SLACK_WEBHOOK_URL_TENANT_1_DEMO'
    """)
    config = worker.database_config()
    config["options"] += f" -c search_path={schema}"
    monkeypatch.setattr(worker, "database_config", lambda: config)
    return connection


class QueueReceipt:
    def __init__(self):
        self.deleted = []
        self.deferred = []

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])

    def change_message_visibility(self, **kwargs):
        self.deferred.append(kwargs["VisibilityTimeout"])


def message(body='{"delivery_id": 1}'):
    return {"Body": body, "ReceiptHandle": "receipt"}


def test_competing_claims_have_only_one_owner(worker_database):
    barrier = Barrier(2)

    def claim():
        with psycopg.connect(**worker.database_config(), autocommit=True) as connection:
            barrier.wait(timeout=5)
            return worker.claim_delivery(connection, 1)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == ["claimed", "deferred"]
    assert worker_database.execute("SELECT status, attempt_count FROM deliveries").fetchone() == ("processing", 1)


def test_expired_claim_replaced_and_stale_completion_rejected(worker_database):
    _, old_job, _ = worker.claim_delivery(worker_database, 1)
    worker_database.execute("UPDATE deliveries SET lease_expires_at = now() - interval '1 second'")
    assert worker.complete_delivery(worker_database, old_job) is False
    action, new_job, _ = worker.claim_delivery(worker_database, 1)
    assert action == "claimed"
    assert new_job["lease_token"] != old_job["lease_token"]
    assert worker.complete_delivery(worker_database, old_job) is False
    assert worker.complete_delivery(worker_database, new_job) is True
    assert worker_database.execute("SELECT status, attempt_count FROM deliveries").fetchone() == ("succeeded", 2)


@pytest.mark.parametrize("state", ["active", "future", "succeeded", "failed"])
def test_existing_states_do_not_send(worker_database, monkeypatch, state):
    async def unexpected_send(*args):
        pytest.fail("No Slack call should occur")

    monkeypatch.setattr(worker, "send_slack", unexpected_send)
    if state == "active":
        worker.claim_delivery(worker_database, 1)
    elif state == "future":
        worker_database.execute("UPDATE deliveries SET next_attempt_at = now() + interval '1 hour'")
    else:
        worker_database.execute("UPDATE deliveries SET status = %s", (state,))
    sqs = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    if state in ("succeeded", "failed"):
        assert sqs.deleted == ["receipt"]
        assert sqs.deferred == []
    else:
        assert sqs.deleted == []
        assert len(sqs.deferred) == 1
        assert 1 <= sqs.deferred[0] <= 3600


@pytest.mark.parametrize("body", ['{', '[]', '{"delivery_id": true}', '{"delivery_id": 0}',
                                   '{"delivery_id": 9223372036854775808}', '{"delivery_id": 999}'])
def test_invalid_or_missing_delivery_is_not_deleted(worker_database, body):
    sqs = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message(body)))
    assert sqs.deleted == []
    assert worker_database.execute("SELECT attempt_count FROM deliveries").fetchone() == (0,)


def test_worker_continues_after_handled_failure(worker_database, monkeypatch):
    async def fail(*args):
        raise SlackSendError("http_500", retryable=True)

    stop = Event()

    class Queue(QueueReceipt):
        polls = 0

        def receive_message(self, **kwargs):
            self.polls += 1
            if self.polls == 2:
                stop.set()
                return {}
            return {"Messages": [message()]}

    monkeypatch.setattr(worker, "send_slack", fail)
    sqs = Queue()
    assert asyncio.run(worker.run_worker(worker.database_config(), sqs, "queue", stop)) == 0
    assert sqs.polls == 2
    assert sqs.deleted == []
    assert sqs.deferred == [5]
    assert worker_database.execute("SELECT status, last_error, completed_at FROM deliveries").fetchone() == ("pending", "http_500", None)


def test_disabled_destination_is_not_sent(worker_database, monkeypatch):
    worker_database.execute("UPDATE destinations SET enabled = false")

    async def unexpected_send(*args):
        pytest.fail("Disabled destination must not receive a notification")

    monkeypatch.setattr(worker, "send_slack", unexpected_send)
    sqs = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert sqs.deleted == ["receipt"]
    assert worker_database.execute("SELECT status, last_error FROM deliveries").fetchone() == ("failed", "destination_disabled")


def test_lease_lost_during_http_does_not_complete_or_delete(worker_database, monkeypatch):
    replacement_token = uuid4()

    async def send(*args):
        worker_database.execute("UPDATE deliveries SET lease_token = %s", (replacement_token,))

    monkeypatch.setattr(worker, "send_slack", send)
    sqs = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert sqs.deleted == []
    assert worker_database.execute("SELECT status, lease_token FROM deliveries").fetchone() == ("processing", replacement_token)


def test_database_failure_after_send_keeps_message(worker_database, monkeypatch):
    sent = []

    async def send(*args):
        sent.append(args)

    worker_database.execute("ALTER TABLE deliveries ADD CONSTRAINT fail_completion CHECK (status <> 'succeeded')")
    monkeypatch.setattr(worker, "send_slack", send)
    sqs = QueueReceipt()
    with pytest.raises(psycopg.errors.CheckViolation):
        asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert len(sent) == 1
    assert sqs.deleted == []
    assert worker_database.execute("SELECT status, completed_at FROM deliveries").fetchone() == ("processing", None)


def test_delete_failure_does_not_repeat_successful_send(worker_database, monkeypatch):
    sent = []

    async def send(*args):
        # Another connection sees the committed lease, without waiting on a lock.
        with psycopg.connect(**worker.database_config(), autocommit=True) as connection:
            assert connection.execute("SELECT status FROM deliveries FOR UPDATE NOWAIT").fetchone() == ("processing",)
        sent.append(args)

    class FailingDelete(QueueReceipt):
        def delete_message(self, **kwargs):
            with psycopg.connect(**worker.database_config(), autocommit=True) as connection:
                assert connection.execute("SELECT status FROM deliveries").fetchone() == ("succeeded",)
            raise ClientError({"Error": {"Code": "ServiceUnavailable"}}, "DeleteMessage")

    monkeypatch.setattr(worker, "send_slack", send)
    with pytest.raises(ClientError):
        asyncio.run(worker.process_message(worker_database, FailingDelete(), "queue", message()))
    sqs = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert len(sent) == 1
    assert sqs.deleted == ["receipt"]


def test_real_queue_to_http_success_and_duplicate(worker_database, local_sqs, monkeypatch):
    sqs, queue_url, _ = local_sqs
    received = []

    async def handler(request):
        received.append(await request.json())
        return web.Response(text="ok")

    async def check():
        async with local_receiver(handler) as url:
            monkeypatch.setattr(slack_sender, "resolve_slack_url", lambda *_: url)
            assert publish_one(worker_database, sqs, queue_url)
            for attempt in range(2):
                receipt = sqs.receive_message(QueueUrl=queue_url, VisibilityTimeout=90)["Messages"][0]
                await worker.process_message(worker_database, sqs, queue_url, receipt)
                if attempt == 0:
                    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"delivery_id": 1}))

    asyncio.run(check())
    assert len(received) == 1
    assert received[0]["text"] == "GitHub push event received. Event 1, delivery 1."
    assert worker_database.execute("""
        SELECT status, attempt_count, completed_at IS NOT NULL,
               lease_token, lease_expires_at FROM deliveries
    """).fetchone() == ("succeeded", 1, True, None, None)
    assert sqs.receive_message(QueueUrl=queue_url).get("Messages", []) == []


def test_backoff_and_fifth_failure_are_terminal(worker_database, monkeypatch):
    calls = []

    async def fail(*args):
        calls.append(args)
        raise SlackSendError("http_500", retryable=True)

    monkeypatch.setattr(worker, "send_slack", fail)
    sqs = QueueReceipt()
    for attempt in range(1, 6):
        asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
        row = worker_database.execute("""
            SELECT status, attempt_count, lease_token, lease_expires_at,
                   completed_at IS NOT NULL FROM deliveries
        """).fetchone()
        assert row == ("pending" if attempt < 5 else "failed", attempt, None, None, attempt == 5)
        if attempt < 5:
            # Early duplicate receipts must not consume another attempt.
            early = QueueReceipt()
            asyncio.run(worker.process_message(worker_database, early, "queue", message()))
            assert len(calls) == attempt
            assert early.deleted == []
            worker_database.execute("UPDATE deliveries SET next_attempt_at = now()")
    assert sqs.deferred == [5, 10, 20, 40]
    assert sqs.deleted == ["receipt"]
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert len(calls) == 5


def test_final_active_claim_is_preserved_then_exhausted_after_crash(worker_database, monkeypatch):
    worker_database.execute("UPDATE deliveries SET attempt_count = 4")
    action, job, _ = worker.claim_delivery(worker_database, 1)
    assert action == "claimed"
    assert job["attempt_count"] == 5

    async def unexpected(*args):
        pytest.fail("An active or exhausted claim must not send")

    monkeypatch.setattr(worker, "send_slack", unexpected)
    sqs = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert sqs.deleted == []
    assert worker_database.execute("SELECT status FROM deliveries").fetchone() == ("processing",)
    worker_database.execute("UPDATE deliveries SET lease_expires_at = now() - interval '1 second'")
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert sqs.deleted == ["receipt"]
    assert worker_database.execute("SELECT status, attempt_count, last_error FROM deliveries").fetchone() == ("failed", 5, "attempts_exhausted")


@pytest.mark.parametrize("retryable", [True, False])
def test_stale_failure_does_not_update_or_acknowledge(worker_database, monkeypatch, retryable):
    replacement = uuid4()

    async def fail(*args):
        worker_database.execute("UPDATE deliveries SET lease_token = %s", (replacement,))
        raise SlackSendError("test_failure", retryable=retryable)

    monkeypatch.setattr(worker, "send_slack", fail)
    sqs = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert sqs.deleted == sqs.deferred == []
    assert worker_database.execute("SELECT status, lease_token, last_error FROM deliveries").fetchone() == ("processing", replacement, None)


@pytest.mark.parametrize("retryable", [True, False])
def test_failure_commit_error_keeps_message_and_claim(worker_database, monkeypatch, retryable):
    async def fail(*args):
        raise SlackSendError("test_failure", retryable=retryable)

    worker_database.execute("ALTER TABLE deliveries ADD CONSTRAINT reject_error CHECK (last_error IS NULL)")
    monkeypatch.setattr(worker, "send_slack", fail)
    sqs = QueueReceipt()
    with pytest.raises(psycopg.errors.CheckViolation):
        asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert sqs.deleted == sqs.deferred == []
    assert worker_database.execute("SELECT status, lease_token IS NOT NULL FROM deliveries").fetchone() == ("processing", True)


def test_visibility_failure_preserves_committed_long_retry(worker_database, monkeypatch):
    calls = []

    async def fail(*args):
        calls.append(args)
        raise SlackSendError("http_429", retryable=True, retry_after=86400)

    class FailingVisibility(QueueReceipt):
        def change_message_visibility(self, **kwargs):
            with psycopg.connect(**worker.database_config(), autocommit=True) as connection:
                assert connection.execute("""
                    SELECT status, lease_token, next_attempt_at > now() + interval '23 hours'
                    FROM deliveries
                """).fetchone() == ("pending", None, True)
            assert kwargs["VisibilityTimeout"] == 3600
            raise ClientError({"Error": {"Code": "ServiceUnavailable"}}, "ChangeMessageVisibility")

    monkeypatch.setattr(worker, "send_slack", fail)
    sqs = FailingVisibility()
    with pytest.raises(ClientError):
        asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert sqs.deleted == []
    duplicate = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, duplicate, "queue", message()))
    assert duplicate.deferred == [3600]
    assert len(calls) == 1


def test_terminal_failure_commits_before_delete_and_is_not_resent(worker_database, monkeypatch):
    calls = []

    async def fail(*args):
        calls.append(args)
        raise SlackSendError("http_403")

    class FailingDelete(QueueReceipt):
        def delete_message(self, **kwargs):
            with psycopg.connect(**worker.database_config(), autocommit=True) as connection:
                assert connection.execute("SELECT status, completed_at IS NOT NULL FROM deliveries").fetchone() == ("failed", True)
            raise ClientError({"Error": {"Code": "ServiceUnavailable"}}, "DeleteMessage")

    monkeypatch.setattr(worker, "send_slack", fail)
    with pytest.raises(ClientError):
        asyncio.run(worker.process_message(worker_database, FailingDelete(), "queue", message()))
    sqs = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert len(calls) == 1
    assert sqs.deleted == ["receipt"]


def test_real_queue_retry_then_http_success(worker_database, local_sqs, monkeypatch):
    sqs, queue_url, _ = local_sqs
    requests = []

    async def handler(request):
        requests.append(await request.json())
        if len(requests) == 1:
            return web.Response(status=429, headers={"Retry-After": "6"})
        return web.Response(text="ok")

    async def check():
        async with local_receiver(handler) as url:
            monkeypatch.setattr(slack_sender, "resolve_slack_url", lambda *_: url)
            publish_one(worker_database, sqs, queue_url)
            receipt = sqs.receive_message(QueueUrl=queue_url, VisibilityTimeout=90)["Messages"][0]
            await worker.process_message(worker_database, sqs, queue_url, receipt)
            assert worker_database.execute("SELECT status, attempt_count FROM deliveries").fetchone() == ("pending", 1)
            assert sqs.receive_message(QueueUrl=queue_url).get("Messages", []) == []
            # Exercise the actual persisted schedule and Moto visibility, without
            # changing next_attempt_at or holding a long-lived database transaction.
            # The shared fixture uses a two-second SDK read timeout, so each
            # long poll must be shorter than that while waiting for the retry.
            for _ in range(12):
                messages = sqs.receive_message(
                    QueueUrl=queue_url, WaitTimeSeconds=1, VisibilityTimeout=90,
                ).get("Messages", [])
                if messages:
                    receipt = messages[0]
                    break
            else:
                pytest.fail("Retry message did not become visible")
            await worker.process_message(worker_database, sqs, queue_url, receipt)

    asyncio.run(check())
    assert len(requests) == 2
    assert worker_database.execute("SELECT status, attempt_count FROM deliveries").fetchone() == ("succeeded", 2)
    assert sqs.receive_message(QueueUrl=queue_url).get("Messages", []) == []


def test_lease_lost_before_send_is_not_a_destination_failure(worker_database, monkeypatch):
    original_load = worker.load_destination

    def lose_lease(connection, job):
        connection.execute("UPDATE deliveries SET lease_expires_at = now() - interval '1 second'")
        return original_load(connection, job)

    async def unexpected(*args):
        pytest.fail("A worker without a valid lease must not send")

    monkeypatch.setattr(worker, "load_destination", lose_lease)
    monkeypatch.setattr(worker, "send_slack", unexpected)
    sqs = QueueReceipt()
    asyncio.run(worker.process_message(worker_database, sqs, "queue", message()))
    assert sqs.deleted == sqs.deferred == []
    assert worker_database.execute("SELECT status, last_error FROM deliveries").fetchone() == ("processing", None)
