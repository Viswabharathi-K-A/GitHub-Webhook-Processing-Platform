import hashlib
import hmac
import json
import os
import re
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb


app = FastAPI(title="GitHub Webhook Processing Platform")
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


def database_config():
    return {
        "host": os.environ["POSTGRES_HOST"],
        "port": os.environ.get("POSTGRES_PORT", "5432"),
        "dbname": os.environ["POSTGRES_DB"],
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
        "connect_timeout": 3,
        "options": "-c statement_timeout=3000",
    }


def resolve_webhook_secret(tenant_id, reference):
    prefix = f"env:GITHUB_WEBHOOK_SECRET_TENANT_{tenant_id}_"
    if not isinstance(reference, str) or not re.fullmatch(
        re.escape(prefix) + r"[A-Z0-9_]+", reference
    ):
        raise HTTPException(status_code=503, detail="Webhook secret unavailable")
    secret = os.environ.get(reference.removeprefix("env:"))
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook secret unavailable")
    return secret.encode("utf-8")


def reject_json_constant(value):
    # Python otherwise accepts NaN and Infinity, which are not valid JSON.
    raise ValueError("Invalid JSON constant")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/db")
def database_health():
    try:
        with psycopg.connect(**database_config()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise HTTPException(
                        status_code=503, detail="Database unavailable"
                    )
    except (psycopg.Error, KeyError):
        raise HTTPException(
            status_code=503, detail="Database unavailable"
        ) from None

    return {"status": "ok"}


@app.post("/webhooks/github/{source_id}")
async def github_webhook(
    request: Request,
    source_id: int = Path(gt=0, le=9223372036854775807),
):
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", event_type):
        raise HTTPException(status_code=400, detail="Invalid GitHub event header")
    try:
        delivery_header = request.headers.get("X-GitHub-Delivery", "")
        if len(delivery_header) != 36:
            raise ValueError("Invalid UUID length")
        github_delivery_id = UUID(delivery_header)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid GitHub delivery UUID"
        ) from None

    # Enforce the limit on actual bytes, even without a Content-Length header.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook body too large")
        body.extend(chunk)

    duplicate = False
    try:
        async with await psycopg.AsyncConnection.connect(
            **database_config(), autocommit=True
        ) as connection:
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    # Keep the source enabled and its secret stable until commit.
                    await cursor.execute(
                        """
                        SELECT tenant_id, webhook_secret_ref
                        FROM sources
                        WHERE id = %s AND enabled = true
                        FOR SHARE
                        """,
                        (source_id,),
                    )
                    source = await cursor.fetchone()
                    if source is None:
                        raise HTTPException(status_code=404, detail="Source not found")
                    tenant_id, secret_reference = source
                    secret = resolve_webhook_secret(tenant_id, secret_reference)
                    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(expected, signature[7:].lower()):
                        raise HTTPException(status_code=401, detail="Invalid signature")

                    try:
                        payload = json.loads(
                            body.decode("utf-8"), parse_constant=reject_json_constant
                        )
                        if not isinstance(payload, dict):
                            raise ValueError("Expected JSON object")
                        # Also reject non-finite numbers produced by overflow (1e999).
                        json.dumps(payload, allow_nan=False)
                    except (ValueError, RecursionError):
                        raise HTTPException(
                            status_code=400, detail="Invalid JSON payload"
                        ) from None

                    # The unique constraint arbitrates concurrent duplicates.
                    # A conflict waits for the other transaction to commit/roll back.
                    await cursor.execute(
                        """
                        INSERT INTO events
                            (tenant_id, source_id, github_delivery_id, event_type, payload)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (source_id, github_delivery_id) DO NOTHING
                        RETURNING id
                        """,
                        (tenant_id, source_id, github_delivery_id, event_type, Jsonb(payload)),
                    )
                    event = await cursor.fetchone()
                    if event is None:
                        duplicate = True
                    else:
                        event_id = event[0]
                        await cursor.execute(
                            """
                            SELECT r.id, r.destination_id
                            FROM rules r
                            JOIN destinations d
                              ON d.tenant_id = r.tenant_id AND d.id = r.destination_id
                            WHERE r.tenant_id = %s AND r.source_id = %s
                              AND r.event_type = %s AND r.enabled = true
                              AND d.enabled = true
                            ORDER BY r.id
                            FOR SHARE OF r, d
                            """,
                            (tenant_id, source_id, event_type),
                        )
                        rules = await cursor.fetchall()
                        for rule_id, destination_id in rules:
                            await cursor.execute(
                                """
                                INSERT INTO deliveries
                                    (tenant_id, source_id, event_id, destination_id, rule_id)
                                VALUES (%s, %s, %s, %s, %s)
                                RETURNING id
                                """,
                                (tenant_id, source_id, event_id, destination_id, rule_id),
                            )
                            delivery_id = (await cursor.fetchone())[0]
                            await cursor.execute(
                                """
                                INSERT INTO outbox (tenant_id, event_id, delivery_id)
                                VALUES (%s, %s, %s)
                                """,
                                (tenant_id, event_id, delivery_id),
                            )
            # Leaving transaction() successfully commits before acknowledging.
    except psycopg.DataError:
        # For example, JSON containing Unicode that PostgreSQL jsonb cannot store.
        raise HTTPException(status_code=400, detail="Invalid payload data") from None
    except (psycopg.Error, KeyError):
        raise HTTPException(status_code=503, detail="Database unavailable") from None

    if duplicate:
        return JSONResponse(status_code=200, content={"status": "duplicate"})
    return JSONResponse(status_code=202, content={"status": "accepted"})
