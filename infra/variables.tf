variable "project_name" {
  description = "Short prefix for this demo's AWS resource names."
  type        = string
  default     = "webhook-platform"
}

variable "aws_region" {
  description = "AWS region for the application resources."
  type        = string
  default     = "us-west-2"
}

variable "image_uri" {
  description = "Application image URI in ECR, including an explicit tag or digest."
  type        = string
}

variable "start_services" {
  description = "Start one task per ECS service only after the image is pushed and database bootstrap is complete."
  type        = bool
  default     = false
}

variable "certificate_arn" {
  description = "ARN of an issued ACM certificate in the application region for the HTTPS hostname."
  type        = string
}

variable "github_secret_parameter_arn" {
  description = "SSM SecureString parameter ARN injected as GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL for the tenant-1 demo."
  type        = string
}

variable "slack_url_parameter_arn" {
  description = "SSM SecureString parameter ARN injected as SLACK_WEBHOOK_URL_TENANT_1_DEMO for the tenant-1 demo."
  type        = string
}

variable "database_password" {
  description = "RDS administrator password for webhook_admin, supplied to the write-only argument; separate from the application user's password."
  type        = string
  sensitive   = true
  ephemeral   = true
}

variable "database_password_parameter_arn" {
  description = "SSM SecureString parameter ARN containing the restricted webhook_app user's password, injected as POSTGRES_PASSWORD; create this user during database bootstrap."
  type        = string
}

variable "database_admin_password_parameter_arn" {
  description = "SSM SecureString parameter ARN for the bootstrap task's webhook_admin password; its value must match database_password supplied to RDS."
  type        = string
}
