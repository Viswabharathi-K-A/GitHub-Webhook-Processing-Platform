output "ecr_repository_url" {
  description = "Repository URL used to tag and push the application image."
  value       = aws_ecr_repository.app.repository_url
}

output "alb_dns_name" {
  description = "ALB DNS name; point the certificate's matching hostname here before HTTPS verification."
  value       = aws_lb.api.dns_name
}

output "api_target_group_arn" {
  description = "Target group ARN used to inspect API task health."
  value       = aws_lb_target_group.api.arn
}

output "ecs_cluster_name" {
  description = "ECS cluster name for service and task commands."
  value       = aws_ecs_cluster.main.name
}

output "api_service_name" {
  description = "API service name for status and deployment commands."
  value       = aws_ecs_service.api.name
}

output "background_service_name" {
  description = "Background service name for status and deployment commands."
  value       = aws_ecs_service.background.name
}

output "delivery_queue_url" {
  description = "Delivery queue URL for inspection commands."
  value       = aws_sqs_queue.deliveries.url
}

output "dlq_url" {
  description = "Dead-letter queue URL for inspection and recovery commands."
  value       = aws_sqs_queue.dlq.url
}

output "database_address" {
  description = "Private PostgreSQL hostname, reachable only through the permitted VPC network paths."
  value       = aws_db_instance.main.address
}

output "api_log_group_name" {
  description = "CloudWatch log group for API logs."
  value       = aws_cloudwatch_log_group.api.name
}

output "background_log_group_name" {
  description = "CloudWatch log group for publisher and worker logs."
  value       = aws_cloudwatch_log_group.background.name
}
