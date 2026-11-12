import asyncio
import hashlib
import hmac
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql

import main


@pytest.fixture
def database(monkeypatch):
    schema = "test_ingestion_" + uuid4().hex
    config = main.database_config()
    with psycopg.connect(**config, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            connection.execute(Path("migrations/001_initial_schema.sql").read_text())
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
            config["options"] += f" -c search_path={schema}"
            monkeypatch.setattr(main, "database_config", lambda: config)
            monkeypatch.setenv("GITHUB_WEBHOOK_SECRET_TENANT_1_TEST", "test-secret")
            yield connection
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def send_webhook(client, body=b'{}', delivery_id=None, source_id=1, secret=b"test-secret"):
    return await client.post(f"/webhooks/github/{source_id}", content=body, headers={
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": delivery_id or str(uuid4()),
        "X-Hub-Signature-256": "sha256=" + hmac.new(
            secret, body, hashlib.sha256
        ).hexdigest(),
        "Content-Type": "application/json",
    })


def post_webhook(body=b'{}'):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            return await send_webhook(client, body)
    return asyncio.run(send())


def row_counts(connection):
    return connection.execute("""
        SELECT (SELECT count(*) FROM events),
               (SELECT count(*) FROM deliveries),
               (SELECT count(*) FROM outbox)
    """).fetchone()


@pytest.mark.parametrize("setup", [
    "UPDATE rules SET enabled = false",
    "UPDATE destinations SET enabled = false",
    "UPDATE rules SET event_type = 'issues'",
])
def test_unmatched_or_disabled_routing_keeps_only_event(database, setup):
    database.execute(setup)
    assert post_webhook().status_code == 202
    assert row_counts(database) == (1, 0, 0)


def test_concurrent_duplicates_create_one_event_and_jobs(database):
    async def send_duplicates():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            delivery_id = str(uuid4())
            return await asyncio.gather(
                send_webhook(client, delivery_id=delivery_id),
                send_webhook(client, delivery_id=delivery_id),
            )

    responses = asyncio.run(send_duplicates())
    assert sorted(response.status_code for response in responses) == [200, 202]
    assert row_counts(database) == (1, 1, 1)


@pytest.mark.parametrize("table", ["deliveries", "outbox"])
def test_insertion_failure_rolls_back_entire_webhook(database, table):
    # Force a real database failure after the earlier inserts, only in this schema.
    database.execute(sql.SQL(
        "ALTER TABLE {} ADD CONSTRAINT reject_test_insert CHECK (false)"
    ).format(sql.Identifier(table)))
    assert post_webhook().status_code == 503
    assert row_counts(database) == (0, 0, 0)
    database.execute(sql.SQL(
        "ALTER TABLE {} DROP CONSTRAINT reject_test_insert"
    ).format(sql.Identifier(table)))
    assert post_webhook().status_code == 202
    assert row_counts(database) == (1, 1, 1)


@pytest.mark.parametrize("relationship", ["source_id", "destination_id"])
def test_rule_cannot_reference_another_tenants_resources(database, relationship):
    database.execute("INSERT INTO tenants (name) VALUES ('other')")
    database.execute("""
        INSERT INTO sources (tenant_id, name, webhook_secret_ref)
        VALUES (2, 'other', 'env:GITHUB_WEBHOOK_SECRET_TENANT_2_OTHER')
    """)
    database.execute("""
        INSERT INTO destinations (tenant_id, name, webhook_url_ref)
        VALUES (2, 'other', 'other-reference')
    """)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        database.execute(sql.SQL("UPDATE rules SET {} = 2 WHERE id = 1").format(
            sql.Identifier(relationship)
        ))
    assert post_webhook().status_code == 202
    assert database.execute("SELECT tenant_id, destination_id FROM deliveries").fetchall() == [(1, 1)]


@pytest.mark.parametrize("body, status", [
    (b'{', 400),
    (b'[]', 400),
    (b'{"value": NaN}', 400),
    (b'{"value": 1e999}', 400),
    (b'{"value": "\\u0000"}', 400),
    (b'x' * (main.MAX_WEBHOOK_BODY_BYTES + 1), 413),
])
def test_invalid_or_oversized_payload_leaves_no_records(database, body, status):
    assert post_webhook(body).status_code == status
    assert row_counts(database) == (0, 0, 0)


@pytest.mark.parametrize("reference", [
    "env:GITHUB_WEBHOOK_SECRET_TENANT_2_TEST",
    "env:GITHUB_WEBHOOK_SECRET_TENANT_10_TEST",
    "env:GITHUB_WEBHOOK_SECRET_LOCAL",
    "env:POSTGRES_PASSWORD",
])
def test_webhook_cannot_resolve_unscoped_or_other_tenant_secrets(database, monkeypatch, reference):
    # Even a provisioned value with a matching signature must not bypass scope.
    monkeypatch.setenv(reference.removeprefix("env:"), "test-secret")
    database.execute("UPDATE sources SET webhook_secret_ref = %s", (reference,))
    assert post_webhook().status_code == 503
    assert row_counts(database) == (0, 0, 0)


def test_two_tenants_use_separate_secrets_and_keep_events_and_routing_isolated(database, monkeypatch):
    database.execute("INSERT INTO tenants (name) VALUES ('other')")
    database.execute("""
        INSERT INTO sources (tenant_id, name, webhook_secret_ref)
        VALUES (2, 'other', 'env:GITHUB_WEBHOOK_SECRET_TENANT_2_TEST')
    """)
    database.execute("""
        INSERT INTO destinations (tenant_id, name, webhook_url_ref)
        VALUES (2, 'other', 'env:SLACK_WEBHOOK_URL_TENANT_2_TEST')
    """)
    database.execute("""
        INSERT INTO rules (tenant_id, source_id, destination_id, event_type)
        VALUES (2, 2, 2, 'push')
    """)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET_TENANT_2_TEST", "other-secret")

    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            # Tenant 1's signature must not authenticate tenant 2's source.
            assert (await send_webhook(client, source_id=2)).status_code == 401
            assert row_counts(database) == (0, 0, 0)
            delivery_id = str(uuid4())
            # Payload tenant IDs are untrusted; the registered source owns the event.
            assert (await send_webhook(client, body=b'{"tenant_id":2}', delivery_id=delivery_id)).status_code == 202
            assert (await send_webhook(
                client, body=b'{"tenant_id":1}', delivery_id=delivery_id,
                source_id=2, secret=b"other-secret",
            )).status_code == 202

    asyncio.run(send())
    assert database.execute("SELECT tenant_id, source_id FROM events ORDER BY tenant_id").fetchall() == [(1, 1), (2, 2)]
    assert database.execute("SELECT tenant_id, source_id, destination_id FROM deliveries ORDER BY tenant_id").fetchall() == [(1, 1, 1), (2, 2, 2)]
    assert database.execute("SELECT tenant_id FROM outbox ORDER BY tenant_id").fetchall() == [(1,), (2,)]
