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
                delay = min(43200, max(1, math.ceil((due - now).total_seconds())))
                return "deferred", job, delay

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
    return "claimed", job, 0


def load_destination(connection, job):
    # Recheck the lease and enabled destination immediately before the send.
    return connection.execute("""
        SELECT dest.webhook_url_ref
        FROM deliveries d
        JOIN destinations dest ON dest.tenant_id = d.tenant_id AND dest.id = d.destination_id
        WHERE d.id = %s AND d.tenant_id = %s AND d.lease_token = %s
          AND d.status = 'processing' AND d.lease_expires_at > clock_timestamp()
          AND dest.enabled = true
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
                raise SlackSendError("destination_disabled_or_lease_lost")
            text = (f"GitHub {job['event_type']} event received. "
                    f"Event {job['event_id']}, delivery {delivery_id}.")
            await send_slack(job["tenant_id"], destination[0], text)
        except SlackSendError as error:
            # Step 1/2 deliberately stops on send failure. Preserve the claim
            # and message for inspection; a later restart can reclaim after expiry.
            try:
                connection.execute("""
                    UPDATE deliveries SET last_error = %s
                    WHERE id = %s AND tenant_id = %s AND lease_token = %s
                      AND status = 'processing' AND lease_expires_at > clock_timestamp()
                """, (str(error), delivery_id, job["tenant_id"], job["lease_token"]))
            except psycopg.Error as database_error:
                logger.warning("delivery_id=%s outcome=error_record_failed error_type=%s",
                               delivery_id, type(database_error).__name__)
            # Failure to record the reason must not turn a send failure into an
            # automatic infrastructure retry in this success-path-only stage.
            raise
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
        except SlackSendError as error:
            logger.error("outcome=send_failed reason=%s worker_stopping=true", error)
            return 1
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
