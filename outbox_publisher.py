import json
import logging
import os
import signal
from threading import Event

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger(__name__)


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


def publish_one(connection, sqs, queue_url):
    """Process one due row. The caller supplies an autocommit connection."""
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, delivery_id, attempt_count
                FROM outbox
                WHERE status = 'pending' AND next_attempt_at <= now()
                ORDER BY next_attempt_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            row = cursor.fetchone()
            if row is None:
                return False
            outbox_id, delivery_id, attempt_count = row

            # Hold this row's lock until the send outcome is committed.
            # A crash after sending can cause a duplicate on the next attempt.
            try:
                sqs.send_message(
                    QueueUrl=queue_url,
                    MessageBody=json.dumps({"delivery_id": delivery_id}),
                )
            except (BotoCoreError, ClientError) as error:
                delay = min(300, 5 * 2 ** min(attempt_count, 6))
                # Exception messages can contain endpoints or credentials.
                error_type = type(error).__name__
                cursor.execute(
                    """
                    UPDATE outbox
                    SET attempt_count = attempt_count + 1,
                        next_attempt_at = clock_timestamp() + %s * interval '1 second',
                        last_error = %s
                    WHERE id = %s
                    """,
                    (delay, error_type, outbox_id),
                )
                outcome = "retry_scheduled"
            else:
                cursor.execute(
                    """
                    UPDATE outbox
                    SET status = 'published', published_at = clock_timestamp(),
                        attempt_count = attempt_count + 1, last_error = NULL
                    WHERE id = %s
                    """,
                    (outbox_id,),
                )
                outcome = "published"

    # Log success only after the database commit succeeds.
    logger.info("outbox_id=%s delivery_id=%s outcome=%s", outbox_id, delivery_id, outcome)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        db_config = database_config()
        queue_url = os.environ.get("SQS_QUEUE_URL", "").strip()
        region = os.environ.get("AWS_DEFAULT_REGION", "").strip()
        if not queue_url or not region:
            raise ValueError("SQS_QUEUE_URL and AWS_DEFAULT_REGION are required")
    except (KeyError, ValueError):
        logger.error("Missing required database or SQS configuration")
        return 1

    stop = Event()

    def request_shutdown(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    try:
        sqs = boto3.client(
            "sqs",
            region_name=region,
            endpoint_url=os.environ.get("SQS_ENDPOINT_URL", "").strip() or None,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )
    except (BotoCoreError, ValueError):
        logger.error("Unable to initialize SQS client; check configuration and credentials")
        return 1

    logger.info("Outbox publisher started")
    try:
        while not stop.is_set():
            try:
                with psycopg.connect(**db_config, autocommit=True) as connection:
                    while not stop.is_set():
                        if not publish_one(connection, sqs, queue_url):
                            stop.wait(1)
            except psycopg.Error as error:
                logger.warning("Database operation failed error_type=%s", type(error).__name__)
                stop.wait(5)
    finally:
        sqs.close()
    logger.info("Outbox publisher stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
