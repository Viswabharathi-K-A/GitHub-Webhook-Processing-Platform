# Temporary AWS demonstration

Status: configuration is being prepared. It has not been validated with Terraform
or deployed. Do not run a full apply yet: the image and database bootstrap
procedure must be completed before the ECS services start.

Use PowerShell from the repository root. Commands below are instructions, not
evidence that they have run. Explain each AWS operation and review its result.
Obtain explicit approval before provisioning, uploading secrets/images, running
remote SQL, starting services, or destroying resources.

## Architecture

One Terraform root manages the application in us-west-2:

- Public HTTPS ALB across two public subnets.
- One API Fargate task and one background task containing publisher and worker.
- Public task IPs for outbound connectivity, with restricted security groups.
- Private single-AZ PostgreSQL 16 with 20 GiB encrypted storage.
- Standard SQS delivery queue and DLQ, ECR, scoped IAM and seven-day logs.

There is no NAT Gateway, autoscaling, or multi-AZ database failover. Rolling
deployments can temporarily run extra tasks. ALB, Fargate, RDS, public IPv4,
storage and usage incur charges. Reconfirm the regional estimate before apply;
the earlier low-traffic planning budget was approximately $3-$5 for 24 hours,
excluding domain registration and taxes. Destroy application resources after
recording evidence rather than leaving them running.

## Local preparation

Install Terraform 1.11 or newer (below 2.0), AWS CLI v2 and Docker separately.
Installation needs approval. These commands only inspect local tools:

```powershell
terraform version
aws --version
docker version
terraform -chdir=infra fmt -check
```

After approval to download the provider, initialize without contacting the S3
backend, then validate. This writes local provider files and the dependency lock
file, but creates no AWS resources:

```powershell
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
```

Commit `.terraform.lock.hcl`. Do not commit state, plans, credentials or local
variable files. Formatting fixes should be reviewed before editing more files.

## Account, hostname and protected state

Use a named AWS profile authenticated with temporary credentials where possible.
Never use the root account for deployment. Set the intended profile and region:

```powershell
$env:AWS_PROFILE = 'YOUR_PROFILE'
$env:AWS_DEFAULT_REGION = 'us-west-2'
aws sts get-caller-identity --region us-west-2
```

The STS command is read-only: verify the account and role before any mutation.

Before application provisioning:

1. Obtain a hostname you control and an issued ACM certificate in us-west-2.
   Certificate DNS validation requires access to that hostname's DNS records.
2. Bootstrap a separate S3 state bucket in us-west-2, with versioning,
   encryption, all public access blocked, HTTPS-only access, and restricted IAM.
   This bucket is outside this root's lifecycle. Review its exact creation
   commands and permissions before execution.
3. Grant the operator access to the state object and its `.tflock` object for
   native S3 locking. No DynamoDB table is needed.

Once the bucket exists, initialization contacts AWS and may write backend state
metadata. Review/approve this step before running:

```powershell
terraform -chdir=infra init -reconfigure -backend-config='bucket=YOUR_STATE_BUCKET'
```

The backend region is fixed separately from the application region in
`versions.tf`. Changing the application region does not move the state bucket.

## Inputs and secrets

Create an ignored `infra/demo.tfvars` with non-secret deployment inputs:

```hcl
project_name                   = "webhook-platform"
aws_region                     = "us-west-2"
image_uri                      = "ACCOUNT.dkr.ecr.us-west-2.amazonaws.com/webhook-platform:demo-001"
certificate_arn                = "YOUR_ISSUED_CERTIFICATE_ARN"
github_secret_parameter_arn     = "YOUR_GITHUB_PARAMETER_ARN"
slack_url_parameter_arn         = "YOUR_SLACK_PARAMETER_ARN"
database_password_parameter_arn = "YOUR_APPLICATION_PASSWORD_PARAMETER_ARN"
```

Provision three SSM SecureString values outside Terraform: the tenant-1 GitHub
secret, tenant-1 Slack URL, and restricted `webhook_app` database password.
Use the default AWS-managed SSM key for the current IAM policy. Customer-managed
keys require separately reviewed KMS permissions. Secret creation commands must
avoid putting literal values in shell history or printing decrypted values.

`database_password` is a separate ephemeral input for the RDS administrator
`webhook_admin`. Supply it through a protected interactive workflow during plan
and apply, including applying saved plans. Do not put it in tfvars or command
arguments. Terraform does not save this input in plans/state. Increment
`password_wo_version` in `database.tf` only when deliberately updating the
administrator password. The application password has its own bootstrap process.

ECS injects SSM values into the existing environment variables. Updates require
replacement tasks; running containers do not refresh secrets automatically.

## First deployment: required order

The current ECS services request one task each immediately. A full initial apply
would start them before migrations and image upload. Before deployment, finalize
and review a staged bootstrap procedure that keeps services stopped until ready.
Do not use routine targeted applies as a substitute for that documented sequence.

1. Prepare state, certificate, inputs and operator-provisioned secrets.
2. Provision the network, ECR, database, queues, roles and logs with services
   prevented from starting. Review the exact bootstrap plan first.
3. Build and push the production image to the created repository.
4. Run a controlled one-off task with private database access to apply the
   reviewed migration and create the restricted application login.
5. Create tenant/source/destination/rule configuration through reviewed SQL.
6. Start both services, point the hostname at the ALB and verify HTTPS.

### Build and push image

After ECR exists, set these values from the actual repository and chosen unique
tag. Immutable tags cannot be overwritten:

```powershell
$repository = 'ACCOUNT.dkr.ecr.us-west-2.amazonaws.com/webhook-platform'
$registry = $repository.Split('/')[0]
$image = "${repository}:demo-001"
docker build --platform linux/amd64 --target production -t $image .
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin $registry
docker push $image
```

The build downloads dependencies and writes a local image. ECR login retrieves a
temporary token and updates local Docker credentials. Push uploads the image and
incurs storage usage. Check each command's exit code before proceeding; update
`image_uri` to match the pushed image.

### Database bootstrap: unfinished prerequisite

The production image excludes `migrations/`; there is not yet an approved
migration task or command. Define a small, explicit one-off procedure before
deployment. Do not run migrations on every application startup or open RDS to
the internet for convenience.

The procedure must:

- Carry the reviewed `migrations/001_initial_schema.sql` to the controlled task.
- Obtain administrator credentials securely, without granting the application
  execution roles access to those credentials.
- Run migration 001 once, fail on SQL errors, and verify its transaction commits.
- Create `webhook_app` using the password stored in its SSM parameter, granting
  only required schema/table/sequence privileges and no schema ownership or DDL.
- Configure tenant 1 using `env:GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL` and
  `env:SLACK_WEBHOOK_URL_TENANT_1_DEMO` references, never raw secret values.
- Verify application database access using TLS and clean up bootstrap artifacts.

Application connections currently use `PGSSLMODE=require`: traffic is encrypted,
but server identity is not verified as it would be with `verify-full` and a
trusted RDS CA bundle. Keep this limitation explicit when reviewing deployment.

## Reviewed plan and apply

Only after bootstrap/startup ordering is resolved, create and inspect a plan:

```powershell
terraform -chdir=infra plan -var-file=demo.tfvars -out=deploy.tfplan
terraform -chdir=infra show deploy.tfplan
```

Plan reads AWS and writes a local plan; it does not provision resources. Review
account, region, resource count, public access, IAM, costs and destructive changes.
Saved plans may contain sensitive metadata and must remain uncommitted.

Only after explicit approval for the reviewed plan:

```powershell
terraform -chdir=infra apply deploy.tfplan
```

Apply changes AWS resources and starts billing. A failed apply can leave billable
resources; inspect state and AWS before deciding the recovery action.

## Verify and record evidence

Read deployment identifiers from state:

```powershell
terraform -chdir=infra output
$cluster = terraform -chdir=infra output -raw ecs_cluster_name
$api = terraform -chdir=infra output -raw api_service_name
$background = terraform -chdir=infra output -raw background_service_name
$targetGroup = terraform -chdir=infra output -raw api_target_group_arn
aws ecs describe-services --cluster $cluster --services $api $background --region us-west-2
aws elbv2 describe-target-health --target-group-arn $targetGroup --region us-west-2
```

The AWS describe commands are read-only. Confirm one running task per service,
healthy ALB targets and no startup errors in the output log groups.

Use the certificate's matching hostname, not the raw ALB hostname:

```powershell
curl.exe --fail-with-body https://YOUR_HOSTNAME/health
curl.exe --fail-with-body https://YOUR_HOSTNAME/health/db
```

Then send an authorized signed GitHub webhook, verify the database/outbox/SQS/
Slack path, and exercise duplicate delivery, retries and DLQ recovery. Receiving
an SQS message changes its visibility and receive count, so queue inspection
must be deliberate. Do not publish payloads, Slack URLs or secrets in evidence.
Record actual results; Terraform validation alone proves no cloud behavior.

## Teardown

After evidence collection, review a destroy plan, then obtain explicit approval:

```powershell
terraform -chdir=infra plan -destroy -var-file=demo.tfvars -out=destroy.tfplan
terraform -chdir=infra show destroy.tfplan
# Run only after approval:
terraform -chdir=infra apply destroy.tfplan
```

Deletion discards RDS data without a final snapshot, ECR images, queued messages
and CloudWatch logs. Save sanitized evidence first. Verify no application tasks,
ALB, RDS instances/backups/snapshots or associated public IPs remain. Inspect
failed deletions and delayed billing reports.

SSM parameters, certificate/DNS records, bootstrap artifacts and the state bucket
are outside this root. Review them separately and delete only explicitly approved
items. Retain protected state until cleanup is verified; versioned bucket objects
and versions need separate handling if the bucket is ultimately removed.

## References

- [Terraform S3 state and native locking](https://developer.hashicorp.com/terraform/language/backend/s3)
- [Terraform initialization](https://developer.hashicorp.com/terraform/cli/commands/init)
- [ECR image authentication and push](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html)
- [ECS SSM secret injection](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-ssm-paramstore.html)
