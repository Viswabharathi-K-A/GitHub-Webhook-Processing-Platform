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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["migrate", "create-app-user"])
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
            else:
                create_app_user(connection)
    except (psycopg.Error, OSError, KeyError, ValueError):
        # Database errors can contain SQL or credentials. Keep output generic.
        print(f"Database bootstrap action '{args.action}' failed.")
        return 1

    print(f"Database bootstrap action '{args.action}' succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
