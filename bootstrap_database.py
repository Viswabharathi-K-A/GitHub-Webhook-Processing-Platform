"""Explicit operator commands; never run automatically on application startup."""

import argparse
import os
from pathlib import Path

import psycopg
from psycopg import sql


def migrate(connection):
    # This file owns its BEGIN/COMMIT. Autocommit avoids an outer transaction.
    migration = Path(__file__).parent / "migrations" / "001_initial_schema.sql"
    connection.execute(migration.read_text(encoding="utf-8"), prepare=False)


def create_app_user(connection):
    password = os.environ["APP_DATABASE_PASSWORD"]
    if not password:
        raise ValueError("Application password is empty")

    # Do not silently alter an existing login or hide partially completed setup.
    with connection.transaction():
        # PostgreSQL utility statements do not accept password bind parameters.
        # Literal safely quotes the value; never print this statement or errors.
        connection.execute(sql.SQL(
            "CREATE ROLE webhook_app LOGIN PASSWORD {} "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        ).format(sql.Literal(password)))
        connection.execute(sql.SQL(
            "GRANT CONNECT ON DATABASE {} TO webhook_app"
        ).format(sql.Identifier(connection.info.dbname)))
        connection.execute("GRANT USAGE ON SCHEMA public TO webhook_app")
        connection.execute(
            "GRANT SELECT ON sources, destinations, rules, events, deliveries, "
            "outbox TO webhook_app"
        )
        connection.execute(
            "GRANT INSERT ON events, deliveries, outbox TO webhook_app"
        )
        connection.execute("GRANT UPDATE ON deliveries, outbox TO webhook_app")
        connection.execute(
            "GRANT USAGE ON SEQUENCE events_id_seq, deliveries_id_seq, "
            "outbox_id_seq TO webhook_app"
        )


def configure_demo(connection):
    with connection.transaction():
        # Serialize setup and prevent configuration writes between check/insert.
        connection.execute(
            "LOCK TABLE tenants, sources, destinations, rules IN EXCLUSIVE MODE"
        )
        existing = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM tenants) "
            "OR EXISTS (SELECT 1 FROM sources) "
            "OR EXISTS (SELECT 1 FROM destinations) "
            "OR EXISTS (SELECT 1 FROM rules)"
        ).fetchone()[0]
        if existing:
            raise ValueError("Demo setup requires empty configuration tables")

        tenant_id = connection.execute(
            "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
            ("aws-demo",),
        ).fetchone()[0]
        # ECS currently injects only tenant-1 secret names. Never reset a sequence
        # or silently bind another tenant to those secrets to bypass this check.
        if tenant_id != 1:
            raise ValueError("Demo setup requires a fresh tenant-1 identity")

        source_id = connection.execute(
            "INSERT INTO sources (tenant_id, name, webhook_secret_ref) "
            "VALUES (%s, %s, %s) RETURNING id",
            (tenant_id, "aws-github", "env:GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL"),
        ).fetchone()[0]
        destination_id = connection.execute(
            "INSERT INTO destinations (tenant_id, name, webhook_url_ref) "
            "VALUES (%s, %s, %s) RETURNING id",
            (tenant_id, "aws-slack", "env:SLACK_WEBHOOK_URL_TENANT_1_DEMO"),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO rules (tenant_id, source_id, destination_id, event_type) "
            "VALUES (%s, %s, %s, %s)",
            (tenant_id, source_id, destination_id, "push"),
        )

    print(f"Demo tenant ID: {tenant_id}; GitHub source ID: {source_id}")
    return source_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["migrate", "create-app-user", "configure-demo"]
    )
    args = parser.parse_args()

    try:
        with psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            connect_timeout=5,
            options="-c statement_timeout=30000 -c lock_timeout=5000",
            autocommit=True,
        ) as connection:
            if args.action == "migrate":
                migrate(connection)
            elif args.action == "create-app-user":
                create_app_user(connection)
            else:
                configure_demo(connection)
    except (psycopg.Error, OSError, KeyError, ValueError):
        # Database errors can contain SQL or credentials. Keep output generic.
        print(f"Database bootstrap action '{args.action}' failed.")
        return 1

    print(f"Database bootstrap action '{args.action}' succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
