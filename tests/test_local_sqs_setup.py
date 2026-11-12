import json
from uuid import uuid4

import pytest

from setup_local_sqs import setup_queues
from test_outbox_publisher import local_sqs


@pytest.fixture
def queues(local_sqs):
    sqs, queue_url, _ = local_sqs
    queue_name = queue_url.rsplit("/", 1)[1]
    dlq_name = "setup-dlq-" + uuid4().hex
    # Create first so cleanup also works if setup fails partway through.
    dlq_url = sqs.create_queue(QueueName=dlq_name)["QueueUrl"]
    try:
        setup_queues(sqs, queue_name, dlq_name)
        yield sqs, queue_url, dlq_url, queue_name, dlq_name
    finally:
        sqs.delete_queue(QueueUrl=dlq_url)


def test_setup_attributes_and_rerun_preserve_messages(queues):
    sqs, queue_url, dlq_url, queue_name, dlq_name = queues
    for url in (queue_url, dlq_url):
        sqs.send_message(QueueUrl=url, MessageBody="keep-me")
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"VisibilityTimeout": "20"})
    assert setup_queues(sqs, queue_name, dlq_name) == (queue_url, dlq_url)
    attributes = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])["Attributes"]
    dlq = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["All"])["Attributes"]
    assert attributes["VisibilityTimeout"] == "90"
    assert attributes["MessageRetentionPeriod"] == "345600"
    assert dlq["MessageRetentionPeriod"] == "1209600"
    policy = json.loads(attributes["RedrivePolicy"])
    assert policy["deadLetterTargetArn"] == dlq["QueueArn"]
    assert int(policy["maxReceiveCount"]) == 10
    for url in (queue_url, dlq_url):
        assert sqs.receive_message(QueueUrl=url)["Messages"][0]["Body"] == "keep-me"


def test_unacknowledged_message_moves_to_dlq(queues):
    sqs, queue_url, dlq_url, _, _ = queues
    sqs.send_message(QueueUrl=queue_url, MessageBody="unhandled-message")
    for attempt in range(10):
        messages = sqs.receive_message(
            QueueUrl=queue_url, VisibilityTimeout=0,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )["Messages"]
        assert messages[0]["Body"] == "unhandled-message"
        assert int(messages[0]["Attributes"]["ApproximateReceiveCount"]) == attempt + 1
    assert sqs.receive_message(QueueUrl=queue_url).get("Messages", []) == []
    assert sqs.receive_message(QueueUrl=dlq_url)["Messages"][0]["Body"] == "unhandled-message"
