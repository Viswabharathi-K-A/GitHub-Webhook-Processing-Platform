resource "aws_sqs_queue" "dlq" {
  name                      = "${var.project_name}-deliveries-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "deliveries" {
  name                       = "${var.project_name}-deliveries"
  visibility_timeout_seconds = 90 # Matches the worker; longer than its 60-second lease.
  message_retention_seconds  = 345600 # 4 days
  sqs_managed_sse_enabled    = true

  # Receive count includes crashes and deferred messages, not just Slack attempts.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 10
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.deliveries.arn]
  })
}
