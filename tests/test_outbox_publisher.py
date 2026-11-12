import json
import hashlib
import hmac
import os
import socket
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import boto3
import httpx
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from psycopg import sql

from outbox_publisher import database_config, publish_one


class FakeSQS:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    def send_message(self, **message):
        if self.error:
            raise self.error
        self.messages.append(message)
        return {"MessageId": "test-message"}


@pytest.fixture
def database():
    schema = "test_outbox_" + uuid4().hex
    with psycopg.connect(**database_config(), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            migration = Path("migrations/001_initial_schema.sql").read_text()
            connection.execute(migration)
            connection.execute("INSERT INTO tenants (name) VALUES ('test')")
            connection.execute("""
                INSERT INTO sources (tenant_id, name, webhook_secret_ref)
                VALUES (1, 'test', 'env:GITHUB_WEBHOOK_SECRET_TENANT_1_TEST')
            """)
            connection.execute("""
                INSERT INTO destinations (tenant_id, name, webhook_url_ref)
                VALUES (1, 'test', 'test-reference')
            """)
            connection.execute("""
                INSERT INTO rules (tenant_id, source_id, destination_id, event_type)
                VALUES (1, 1, 1, 'push')
            """)
            connection.execute("""
                INSERT INTO events (tenant_id, source_id, github_delivery_id, event_type, payload)
                VALUES (1, 1, %s, 'push', '{}')
            """, (uuid4(),))
            connection.execute("""
                INSERT INTO deliveries (tenant_id, source_id, event_id, destination_id, rule_id)
                VALUES (1, 1, 1, 1, 1)
            """)
            connection.execute("""
                INSERT INTO outbox (tenant_id, event_id, delivery_id) VALUES (1, 1, 1)
            """)
            yield connection, schema
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_success_publishes_once_and_leaves_delivery_pending(database):
    connection, _ = database
    sqs = FakeSQS()
    assert publish_one(connection, sqs, "test-queue") is True
    assert publish_one(connection, sqs, "test-queue") is False
    assert len(sqs.messages) == 1
    assert json.loads(sqs.messages[0]["MessageBody"]) == {"delivery_id": 1}
    assert connection.execute("SELECT status, attempt_count, published_at IS NOT NULL FROM outbox").fetchone() == ("published", 1, True)
    assert connection.execute("SELECT status FROM deliveries").fetchone() == ("pending",)


def test_failed_send_schedules_retry_without_sensitive_error(database):
    connection, _ = database
    error = ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "sensitive"}}, "SendMessage")
    assert publish_one(connection, FakeSQS(error), "test-queue") is True
    assert connection.execute("""
        SELECT status, attempt_count, last_error, next_attempt_at > now(), published_at
        FROM outbox
    """).fetchone() == ("pending", 1, "ClientError", True, None)
    sqs = FakeSQS()
    assert publish_one(connection, sqs, "test-queue") is False
    connection.execute("UPDATE outbox SET next_attempt_at = now()")
    assert publish_one(connection, sqs, "test-queue") is True


def test_future_job_is_not_sent(database):
    connection, _ = database
    connection.execute("UPDATE outbox SET next_attempt_at = now() + interval '1 hour'")
    sqs = FakeSQS()
    assert publish_one(connection, sqs, "test-queue") is False
    assert sqs.messages == []


def test_competing_connection_skips_lock_and_recovers_after_rollback(database):
    connection, schema = database
    sqs = FakeSQS()
    with psycopg.connect(**database_config(), autocommit=True) as competing:
        competing.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        with connection.transaction(force_rollback=True):
            connection.execute("SELECT id FROM outbox FOR UPDATE")
            assert publish_one(competing, sqs, "test-queue") is False
        assert publish_one(competing, sqs, "test-queue") is True
    assert len(sqs.messages) == 1


def test_send_followed_by_database_rollback_can_publish_duplicate(database):
    connection, _ = database
    sqs = FakeSQS()
    with pytest.raises(psycopg.errors.CheckViolation):
        with connection.transaction():
            assert publish_one(connection, sqs, "test-queue") is True
            connection.execute("UPDATE outbox SET attempt_count = -1")
    assert connection.execute("SELECT status, attempt_count FROM outbox").fetchone() == ("pending", 0)
    assert publish_one(connection, sqs, "test-queue") is True
    assert len(sqs.messages) == 2


@pytest.fixture
def local_sqs():
    endpoint = os.environ.get("TEST_SQS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("Set TEST_SQS_ENDPOINT_URL to run local emulator integration tests")
    # Fixed dummy credentials and an explicit local endpoint prevent AWS access.
    assert endpoint.startswith(("http://sqs:", "http://localhost:", "http://127.0.0.1:"))
    with closing(boto3.client(
        "sqs", endpoint_url=endpoint, region_name="us-west-2",
        aws_access_key_id="test", aws_secret_access_key="test",
        config=Config(connect_timeout=1, read_timeout=2,
                      retries={"total_max_attempts": 1}),
    )) as client:
        queue_url = client.create_queue(QueueName="integration-" + uuid4().hex)["QueueUrl"]
        try:
            yield client, queue_url, endpoint
        finally:
            client.delete_queue(QueueUrl=queue_url)


@contextmanager
def running_process(module, schema, endpoint, queue_url, port=0):
    env = os.environ.copy()
    env.update(
        AWS_DEFAULT_REGION="us-west-2", AWS_ACCESS_KEY_ID="test",
        AWS_SECRET_ACCESS_KEY="test", AWS_SESSION_TOKEN="",
        AWS_EC2_METADATA_DISABLED="true", SQS_ENDPOINT_URL=endpoint,
        SQS_QUEUE_URL=queue_url, GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL="integration-secret",
    )
    # Only the child process uses this schema; production configuration is unchanged.
    code = (
        "import importlib; "
        f"module = importlib.import_module({module!r}); "
        "config = module.database_config(); "
        f"config['options'] += ' -c search_path={schema}'; "
        "module.database_config = lambda: config; "
    )
    if module == "main":
        code += f"import uvicorn; uvicorn.run(module.app, host='127.0.0.1', port={port}, log_level='warning')"
    else:
        code += "raise SystemExit(module.main())"
    process = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            pytest.fail("Process did not stop gracefully within 25 seconds")


def wait_for(check, process, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, "Background process exited unexpectedly"
        if check():
            return
        time.sleep(0.1)
    pytest.fail("Timed out waiting for background process")


def test_signed_http_webhook_to_real_sqs_and_duplicate(database, local_sqs):
    connection, schema = database
    sqs, queue_url, endpoint = local_sqs
    # Keep the source/rule but remove the fixture's existing event and jobs.
    connection.execute("DELETE FROM outbox")
    connection.execute("DELETE FROM deliveries")
    connection.execute("DELETE FROM events")
    connection.execute("UPDATE sources SET webhook_secret_ref = 'env:GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL'")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    with running_process("main", schema, endpoint, queue_url, port) as api:
        def ready():
            try:
                return httpx.get(base_url + "/health", timeout=1).status_code == 200
            except httpx.TransportError:
                return False
        wait_for(ready, api)
        body = b'{"action":"opened","repository":{"name":"local-test"}}'
        headers = {
            "X-GitHub-Event": "push", "X-GitHub-Delivery": str(uuid4()),
            "X-Hub-Signature-256": "sha256=" + hmac.new(b"integration-secret", body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json",
        }
        invalid = dict(headers, **{"X-Hub-Signature-256": "sha256=" + "0" * 64})
        assert httpx.post(base_url + "/webhooks/github/1", content=body, headers=invalid).status_code == 401
        response = httpx.post(base_url + "/webhooks/github/1", content=body, headers=headers)
        assert response.status_code == 202, response.text
        duplicate = httpx.post(base_url + "/webhooks/github/1", content=body, headers=headers)
        assert duplicate.status_code == 200
        assert duplicate.json() == {"status": "duplicate"}
        for table in ("events", "deliveries", "outbox"):
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone() == (1,)
        delivery_id = connection.execute("SELECT delivery_id FROM outbox").fetchone()[0]
        with running_process("outbox_publisher", schema, endpoint, queue_url) as publisher:
            wait_for(lambda: connection.execute("SELECT status FROM outbox").fetchone() == ("published",), publisher)
        assert publisher.returncode == 0
        messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
        assert len(messages) == 1
        assert json.loads(messages[0]["Body"]) == {"delivery_id": delivery_id}
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=messages[0]["ReceiptHandle"])
        assert sqs.receive_message(QueueUrl=queue_url).get("Messages", []) == []
        assert connection.execute("SELECT status FROM deliveries").fetchone() == ("pending",)


def test_real_sqs_failure_recovers_on_scheduled_retry(database, local_sqs):
    connection, schema = database
    sqs, queue_url, endpoint = local_sqs
    # A missing queue produces a real HTTP SQS error, then becomes available.
    missing_name = "recovery-" + uuid4().hex
    missing_url = queue_url.rsplit("/", 1)[0] + "/" + missing_name
    created_url = None
    try:
        with running_process("outbox_publisher", schema, endpoint, missing_url) as publisher:
            wait_for(lambda: connection.execute("SELECT attempt_count FROM outbox").fetchone()[0] >= 1, publisher)
            assert connection.execute("SELECT status, last_error, next_attempt_at > now() FROM outbox").fetchone() == ("pending", "QueueDoesNotExist", True)
            created_url = sqs.create_queue(QueueName=missing_name)["QueueUrl"]
            # Do not override next_attempt_at: exercise the actual retry delay.
            wait_for(lambda: connection.execute("SELECT status FROM outbox").fetchone() == ("published",), publisher)
        assert publisher.returncode == 0
        assert len(sqs.receive_message(QueueUrl=created_url).get("Messages", [])) == 1
    finally:
        if created_url:
            sqs.delete_queue(QueueUrl=created_url)


def test_endpoint_outage_and_publisher_restart(database, local_sqs):
    connection, schema = database
    sqs, queue_url, endpoint = local_sqs
    with running_process("outbox_publisher", schema, "http://127.0.0.1:9", queue_url) as publisher:
        wait_for(lambda: connection.execute("SELECT attempt_count FROM outbox").fetchone()[0] >= 1, publisher)
        assert connection.execute("SELECT status, last_error FROM outbox").fetchone() == ("pending", "EndpointConnectionError")
    assert publisher.returncode == 0
    with running_process("outbox_publisher", schema, endpoint, queue_url) as restarted:
        wait_for(lambda: connection.execute("SELECT status FROM outbox").fetchone() == ("published",), restarted)
    assert restarted.returncode == 0
    # Restarting after success must not publish the same job again.
    with running_process("outbox_publisher", schema, endpoint, queue_url) as idle:
        time.sleep(2)
        assert idle.poll() is None
    assert idle.returncode == 0
    assert connection.execute("SELECT attempt_count FROM outbox").fetchone() == (2,)
    assert len(sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])) == 1
