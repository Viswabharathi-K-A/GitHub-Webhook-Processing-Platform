import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
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
                VALUES (1, 'test', 'env:GITHUB_WEBHOOK_SECRET_TEST')
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
