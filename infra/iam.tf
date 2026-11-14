locals {
  ecs_task_trust_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Execution roles are used by ECS to start containers, not by application code.
resource "aws_iam_role" "execution" {
  for_each = toset(["api", "background", "bootstrap"])

  name               = "${var.project_name}-${each.key}-execution"
  assume_role_policy = local.ecs_task_trust_policy
}

resource "aws_iam_role_policy" "execution" {
  for_each = {
    api = {
      log_group_arn = aws_cloudwatch_log_group.api.arn
      parameter_arns = [
        var.database_password_parameter_arn,
        var.github_secret_parameter_arn,
      ]
    }
    background = {
      log_group_arn = aws_cloudwatch_log_group.background.arn
      parameter_arns = [
        var.database_password_parameter_arn,
        var.slack_url_parameter_arn,
      ]
    }
    # Only the explicitly launched bootstrap task receives the admin password.
    bootstrap = {
      log_group_arn = aws_cloudwatch_log_group.background.arn
      parameter_arns = [
        var.database_admin_password_parameter_arn,
        var.database_password_parameter_arn,
      ]
    }
  }

  name = "container-startup"
  role = aws_iam_role.execution[each.key].id

  # ECR authorization tokens require Resource "*"; image reads are scoped below.
  # SSM parameters use the AWS-managed aws/ssm key for this demo.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${each.value.log_group_arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameters"
        Resource = each.value.parameter_arns
      },
    ]
  })
}

# Application roles are separate from execution roles. The API has no attached
# permissions; only the background task needs to call AWS APIs at runtime.
resource "aws_iam_role" "task" {
  for_each = toset(["api", "background"])

  name               = "${var.project_name}-${each.key}-task"
  assume_role_policy = local.ecs_task_trust_policy
}

resource "aws_iam_role_policy" "background_queue" {
  name = "delivery-queue"
  role = aws_iam_role.task["background"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
      ]
      Resource = aws_sqs_queue.deliveries.arn
    }]
  })
}
