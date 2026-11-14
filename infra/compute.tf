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

resource "aws_ecs_service" "api" {
  name            = "${var.project_name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  launch_type     = "FARGATE"
  desired_count   = var.start_services ? 1 : 0

  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [
    aws_lb_listener.https,
    aws_iam_role_policy.execution,
    aws_route.internet,
    aws_route_table_association.public,
    aws_vpc_security_group_ingress_rule.api_from_alb,
    aws_vpc_security_group_egress_rule.alb_to_api,
    aws_vpc_security_group_egress_rule.task_https,
    aws_vpc_security_group_egress_rule.task_to_database,
    aws_vpc_security_group_ingress_rule.database_from_task,
  ]
}

resource "aws_ecs_service" "background" {
  name            = "${var.project_name}-background"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.background.arn
  launch_type     = "FARGATE"
  desired_count   = var.start_services ? 1 : 0

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.background.id]
    assign_public_ip = true
  }

  depends_on = [
    aws_iam_role_policy.execution,
    aws_iam_role_policy.background_queue,
    aws_route.internet,
    aws_route_table_association.public,
    aws_vpc_security_group_egress_rule.task_https,
    aws_vpc_security_group_egress_rule.task_to_database,
    aws_vpc_security_group_ingress_rule.database_from_task,
    aws_sqs_queue_redrive_allow_policy.dlq,
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

resource "aws_ecs_task_definition" "bootstrap" {
  family                   = "${var.project_name}-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution["bootstrap"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # No ECS service uses this definition. Launch each action explicitly and
  # inspect its exit code before proceeding. The script needs no AWS task role.
  container_definitions = jsonencode([{
    name        = "bootstrap"
    image       = var.image_uri
    essential   = true
    stopTimeout = 30
    command     = ["python", "bootstrap_database.py", "migrate"]
    environment = [
      { name = "POSTGRES_HOST", value = aws_db_instance.main.address },
      { name = "POSTGRES_PORT", value = "5432" },
      { name = "POSTGRES_DB", value = aws_db_instance.main.db_name },
      { name = "POSTGRES_USER", value = aws_db_instance.main.username },
      { name = "PGSSLMODE", value = "require" },
    ]
    secrets = [
      { name = "POSTGRES_PASSWORD", valueFrom = var.database_admin_password_parameter_arn },
      { name = "APP_DATABASE_PASSWORD", valueFrom = var.database_password_parameter_arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.background.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "bootstrap"
      }
    }
  }])

  depends_on = [aws_iam_role_policy.execution]
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
