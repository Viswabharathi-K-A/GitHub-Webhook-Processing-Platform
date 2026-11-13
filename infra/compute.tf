resource "aws_ecr_repository" "app" {
  name                 = var.project_name
  image_tag_mutability = "IMMUTABLE"

  # Temporary demo: allow teardown to delete the repository and its images.
  force_delete = true
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.project_name}/api"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "background" {
  name              = "/ecs/${var.project_name}/background"
  retention_in_days = 7
}

resource "aws_ecs_cluster" "main" {
  name = var.project_name
}

locals {
  database_environment = [
    { name = "POSTGRES_HOST", value = aws_db_instance.main.address },
    { name = "POSTGRES_PORT", value = "5432" },
    { name = "POSTGRES_DB", value = aws_db_instance.main.db_name },
    { name = "POSTGRES_USER", value = "webhook_app" },
    { name = "PGSSLMODE", value = "require" },
  ]

  database_secrets = [
    { name = "POSTGRES_PASSWORD", valueFrom = var.database_password_parameter_arn },
  ]

  queue_environment = [
    { name = "AWS_DEFAULT_REGION", value = var.aws_region },
    { name = "SQS_QUEUE_URL", value = aws_sqs_queue.deliveries.url },
  ]
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project_name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution["api"].arn
  task_role_arn            = aws_iam_role.task["api"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name        = "api"
    image       = var.image_uri
    essential   = true
    stopTimeout = 30
    command     = ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]
    environment = local.database_environment
    secrets = concat(local.database_secrets, [
      { name = "GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL", valueFrom = var.github_secret_parameter_arn },
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "background" {
  family                   = "${var.project_name}-background"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution["background"].arn
  task_role_arn            = aws_iam_role.task["background"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # Both containers are essential: either process exiting replaces the task.
  container_definitions = jsonencode([
    {
      name        = "publisher"
      image       = var.image_uri
      essential   = true
      stopTimeout = 30
      command     = ["python", "outbox_publisher.py"]
      environment = concat(local.database_environment, local.queue_environment)
      secrets     = local.database_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.background.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    },
    {
      name        = "worker"
      image       = var.image_uri
      essential   = true
      stopTimeout = 30
      command     = ["python", "delivery_worker.py"]
      environment = concat(local.database_environment, local.queue_environment)
      secrets = concat(local.database_secrets, [
        { name = "SLACK_WEBHOOK_URL_TENANT_1_DEMO", valueFrom = var.slack_url_parameter_arn },
      ])
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.background.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    },
  ])
}
