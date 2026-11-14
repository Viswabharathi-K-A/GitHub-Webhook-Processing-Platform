"""Opt-in tests: require exclusive access to a disposable local PostgreSQL cluster."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

import bootstrap_database
from main import database_config


@pytest.fixture
def bootstrap_db(monkeypatch):
    # These tests create a database and a cluster-wide role. Never run against
    # AWS or alongside other bootstrap commands, including parallel pytest runs.
    if os.environ.get("RUN_BOOTSTRAP_DATABASE_TESTS") != "1":
        pytest.skip("Set RUN_BOOTSTRAP_DATABASE_TESTS=1 on an exclusive local cluster")

    config = database_config()
    name = "test_bootstrap_" + uuid4().hex
    with psycopg.connect(**config, autocommit=True) as admin:
        if admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'webhook_app'"
        ).fetchone():
            pytest.skip("Existing webhook_app role must not be changed by tests")
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            config["dbname"] = name
            with psycopg.connect(**config, autocommit=True) as connection:
                monkeypatch.setenv("APP_DATABASE_PASSWORD", "test-'quoted-password")
                yield connection
        finally:
            # Drop the disposable database first to remove its role grants.
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
            admin.execute("DROP ROLE IF EXISTS webhook_app")


def test_migration_commits_and_rejects_reapplication(bootstrap_db):
    bootstrap_database.migrate(bootstrap_db)
    assert bootstrap_db.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    ).fetchone() == (7,)

    with pytest.raises(psycopg.errors.DuplicateTable):
        bootstrap_database.migrate(bootstrap_db)
    bootstrap_db.execute("ROLLBACK")
    assert bootstrap_db.execute("SELECT count(*) FROM events").fetchone() == (0,)


def test_failed_grants_roll_back_role_creation(bootstrap_db):
    # Without the migration, the first table grant fails after CREATE ROLE.
    with pytest.raises(psycopg.errors.UndefinedTable):
        bootstrap_database.create_app_user(bootstrap_db)
    assert bootstrap_db.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = 'webhook_app'"
    ).fetchone() is None


def test_app_user_can_process_events_but_cannot_manage_configuration(bootstrap_db):
    bootstrap_database.migrate(bootstrap_db)
    bootstrap_database.create_app_user(bootstrap_db)
    with pytest.raises(psycopg.errors.DuplicateObject):
        bootstrap_database.create_app_user(bootstrap_db)

    bootstrap_db.execute("INSERT INTO tenants (name) VALUES ('bootstrap-test')")
    bootstrap_db.execute(
        "INSERT INTO sources (tenant_id, name, webhook_secret_ref) "
        "VALUES (1, 'test', 'env:GITHUB_WEBHOOK_SECRET_TENANT_1_TEST')"
    )
    # Authenticate with the actual password, including its quote character.
    with psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=bootstrap_db.info.dbname,
        user="webhook_app",
        password=os.environ["APP_DATABASE_PASSWORD"],
        connect_timeout=3,
        autocommit=True,
    ) as app:
        assert app.execute("SELECT id FROM sources").fetchone() == (1,)
        assert app.execute(
            "INSERT INTO events (tenant_id, source_id, github_delivery_id, "
            "event_type, payload) VALUES (1, 1, %s, 'push', '{}') RETURNING id",
            (uuid4(),),
        ).fetchone() == (1,)
        # Exercise grants even with no jobs yet; subsequent pipeline tests
        # cover valid delivery/outbox rows and their state transitions.
        app.execute("SELECT id FROM deliveries FOR UPDATE")
        app.execute("UPDATE deliveries SET status = 'failed' WHERE false")
        app.execute("UPDATE outbox SET status = 'published' WHERE false")
        for statement in [
            "UPDATE sources SET enabled = false",
            "DELETE FROM events",
            "CREATE TABLE public.unwanted (id integer)",
            "ALTER TABLE events ADD COLUMN unwanted integer",
        ]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(statement)

    assert bootstrap_db.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
        "FROM pg_roles WHERE rolname = 'webhook_app'"
    ).fetchone() == (False, False, False, False, False)
