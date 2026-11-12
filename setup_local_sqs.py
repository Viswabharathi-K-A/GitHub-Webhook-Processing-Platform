import json
from contextlib import closing

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


def setup_queues(sqs, queue_name="deliveries", dlq_name="deliveries-dlq"):
    # CreateQueue with no attributes also returns an existing queue. Configure
    # attributes separately so reruns can update earlier local demo queues.
    dlq_url = sqs.create_queue(QueueName=dlq_name)["QueueUrl"]
    sqs.set_queue_attributes(QueueUrl=dlq_url, Attributes={
        "MessageRetentionPeriod": "1209600",  # 14 days
    })
    dlq_arn = sqs.get_queue_attributes(
        QueueUrl=dlq_url, AttributeNames=["QueueArn"],
    )["Attributes"]["QueueArn"]
    queue_url = sqs.create_queue(QueueName=queue_name)["QueueUrl"]
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={
        "VisibilityTimeout": "90",
        "MessageRetentionPeriod": "345600",  # 4 days
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "10"}),
    })
    return queue_url, dlq_url


def main():
    # Deliberately local-only: never use ambient AWS endpoints or credentials.
    try:
        with closing(boto3.client(
            "sqs", endpoint_url="http://sqs:5000", region_name="us-west-2",
            aws_access_key_id="test", aws_secret_access_key="test", aws_session_token="",
            config=Config(connect_timeout=3, read_timeout=5,
                          retries={"total_max_attempts": 2}, proxies={}),
        )) as sqs:
            queue_url, dlq_url = setup_queues(sqs)
    except (BotoCoreError, ClientError) as error:
        print(f"Local queue setup failed: {type(error).__name__}")
        return 1
    print(f"SQS_QUEUE_URL={queue_url}")
    print(f"Local DLQ URL: {dlq_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
