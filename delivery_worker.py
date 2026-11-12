import asyncio
import json
import logging
import math
import os
import signal
from threading import Event
from uuid import uuid4

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from psycopg.rows import dict_row

from slack_sender import SlackSendError, send_slack


logger = logging.getLogger(__name__)
LEASE_SECONDS = 60
VISIBILITY_SECONDS = 90
MAX_ATTEMPTS = 5
MAX_VISIBILITY_SECONDS = 3600


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


def claim_delivery(connection, delivery_id):
    """Return (action, job, delay); caller supplies an autocommit connection."""
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""
                SELECT d.*, e.event_type
                FROM deliveries d
                JOIN events e ON e.tenant_id = d.tenant_id AND e.id = d.event_id
                WHERE d.id = %s
                FOR UPDATE OF d
            """, (delivery_id,))
            job = cursor.fetchone()
            if job is None:
                return "missing", None, 0
            if job["status"] in ("succeeded", "failed"):
                return "terminal", job, 0

            # Read the time after acquiring the lock, not before a possible wait.
            cursor.execute("SELECT clock_timestamp() AS now")
            now = cursor.fetchone()["now"]
            due = (job["lease_expires_at"] if job["status"] == "processing"
                   else job["next_attempt_at"])
            if due is not None and due > now:
                delay = min(MAX_VISIBILITY_SECONDS, max(1, math.ceil((due - now).total_seconds())))
                return "deferred", job, delay

            # Active claims were deferred above, even on their final attempt.
            # An expired final claim may have sent before crashing: never send a sixth time.
            if job["attempt_count"] >= MAX_ATTEMPTS:
                cursor.execute("""
                    UPDATE deliveries SET status = 'failed', completed_at = clock_timestamp(),
                        lease_token = NULL, lease_expires_at = NULL, last_error = 'attempts_exhausted'
                    WHERE id = %s
                """, (delivery_id,))
                return "terminal", job, 0

            token = uuid4()
            cursor.execute("""
                UPDATE deliveries
                SET status = 'processing', lease_token = %s,
                    lease_expires_at = clock_timestamp() + %s * interval '1 second',
                    attempt_count = attempt_count + 1, last_error = NULL,
                    completed_at = NULL
                WHERE id = %s
            """, (token, LEASE_SECONDS, delivery_id))
            job["lease_token"] = token
            job["attempt_count"] += 1
    return "claimed", job, 0


def load_destination(connection, job):
    # Recheck the lease and enabled destination immediately before the send.
    return connection.execute("""
        SELECT dest.webhook_url_ref, dest.enabled
        FROM deliveries d
        JOIN destinations dest ON dest.tenant_id = d.tenant_id AND dest.id = d.destination_id
        WHERE d.id = %s AND d.tenant_id = %s AND d.lease_token = %s
          AND d.status = 'processing' AND d.lease_expires_at > clock_timestamp()
    """, (job["id"], job["tenant_id"], job["lease_token"])).fetchone()


def complete_delivery(connection, job):
    with connection.transaction():
        # Acquire the lock before checking expiry so lock waits cannot make a
        # previously evaluated lease check stale.
        connection.execute("SELECT id FROM deliveries WHERE id = %s FOR UPDATE", (job["id"],))
        result = connection.execute("""
            UPDATE deliveries
            SET status = 'succeeded', completed_at = clock_timestamp(),
                lease_token = NULL, lease_expires_at = NULL, last_error = NULL
            WHERE id = %s AND tenant_id = %s AND lease_token = %s
              AND status = 'processing' AND lease_expires_at > clock_timestamp()
            RETURNING id
        """, (job["id"], job["tenant_id"], job["lease_token"])).fetchone()
    return result is not None


def record_failure(connection, job, error):
    retry = error.retryable and job["attempt_count"] < MAX_ATTEMPTS
    delay = max(5 * 2 ** (job["attempt_count"] - 1), error.retry_after or 0) if retry else 0
    status = "pending" if retry else "failed"
    with connection.transaction():
        connection.execute("SELECT id FROM deliveries WHERE id = %s FOR UPDATE", (job["id"],))
        result = connection.execute("""
            UPDATE deliveries
            SET status = %s, last_error = %s, lease_token = NULL, lease_expires_at = NULL,
                next_attempt_at = clock_timestamp() + %s * interval '1 second',
                completed_at = CASE WHEN %s = 'failed' THEN clock_timestamp() ELSE NULL END
            WHERE id = %s AND tenant_id = %s AND lease_token = %s
              AND status = 'processing' AND lease_expires_at > clock_timestamp()
            RETURNING id
        """, (status, str(error), delay, status, job["id"], job["tenant_id"],
              job["lease_token"])).fetchone()
    if result is None:
        return "lease_lost", 0
    return status, min(delay, MAX_VISIBILITY_SECONDS)


async def process_message(connection, sqs, queue_url, message):
    try:
        payload = json.loads(message["Body"])
        delivery_id = payload["delivery_id"]
        if type(delivery_id) is not int or not 0 < delivery_id <= 9223372036854775807:
            raise ValueError("Invalid delivery ID")
    except (KeyError, TypeError, ValueError, RecursionError):
        logger.warning("outcome=invalid_message")
        return

    receipt = message["ReceiptHandle"]
    action, job, delay = claim_delivery(connection, delivery_id)
    if action == "missing":
        logger.warning("delivery_id=%s outcome=missing_delivery", delivery_id)
        return
    if action == "deferred":
        sqs.change_message_visibility(
            QueueUrl=queue_url, ReceiptHandle=receipt, VisibilityTimeout=delay,
        )
        return
    if action == "claimed":
        try:
            destination = load_destination(connection, job)
            if destination is None:
                logger.warning("delivery_id=%s outcome=lease_lost", delivery_id)
                return
            if not destination[1]:
                raise SlackSendError("destination_disabled")
            text = (f"GitHub {job['event_type']} event received. "
                    f"Event {job['event_id']}, delivery {delivery_id}.")
            await send_slack(job["tenant_id"], destination[0], text)
        except SlackSendError as error:
            outcome, delay = record_failure(connection, job, error)
            logger.info("delivery_id=%s attempt=%s outcome=%s reason=%s",
                        delivery_id, job["attempt_count"], outcome, error)
            if outcome == "lease_lost":
                return
            if outcome == "pending":
                # The database commit comes first. If this fails, the next receipt
                # consults next_attempt_at and cannot send early.
                sqs.change_message_visibility(
                    QueueUrl=queue_url, ReceiptHandle=receipt, VisibilityTimeout=delay,
                )
                return
        else:
            if not complete_delivery(connection, job):
                logger.warning("delivery_id=%s outcome=lease_lost", delivery_id)
                return
            logger.info("delivery_id=%s outcome=succeeded", delivery_id)

    # Both paths reach here only after a durable terminal database outcome.
    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)


async def run_worker(db_config, sqs, queue_url, stop):
    while not stop.is_set():
        try:
            response = sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=5,
                VisibilityTimeout=VISIBILITY_SECONDS,
            )
            for message in response.get("Messages", []):
                if stop.is_set():
                    break
                with psycopg.connect(**db_config, autocommit=True) as connection:
                    await process_message(connection, sqs, queue_url, message)
        except (psycopg.Error, BotoCoreError, ClientError) as error:
            logger.warning("outcome=infrastructure_error error_type=%s", type(error).__name__)
            stop.wait(5)
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        db_config = database_config()
        queue_url = os.environ.get("SQS_QUEUE_URL", "").strip()
        region = os.environ.get("AWS_DEFAULT_REGION", "").strip()
        if not queue_url or not region:
            raise ValueError("Missing queue configuration")
        sqs = boto3.client(
            "sqs", region_name=region,
            endpoint_url=os.environ.get("SQS_ENDPOINT_URL", "").strip() or None,
            config=Config(connect_timeout=3, read_timeout=8,
                          retries={"total_max_attempts": 1}),
        )
    except (KeyError, ValueError, BotoCoreError):
        logger.error("Missing or invalid database/SQS configuration")
        return 1

    stop = Event()

    def request_shutdown(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    logger.info("Delivery worker started")
    try:
        return asyncio.run(run_worker(db_config, sqs, queue_url, stop))
    finally:
        sqs.close()
        logger.info("Delivery worker stopped")


if __name__ == "__main__":
    raise SystemExit(main())
