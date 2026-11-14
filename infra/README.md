# Temporary AWS demonstration

Status: configuration is being prepared. It has not been validated with Terraform
or deployed. ECS services default to zero tasks. Complete validation and review
the database bootstrap procedure before provisioning; upload the image
and complete bootstrap before enabling services.

Use PowerShell from the repository root. Commands below are instructions, not
evidence that they have run. Explain each AWS operation and review its result.
Obtain explicit approval before provisioning, uploading secrets/images, running
remote SQL, starting services, or destroying resources.

## Architecture

One Terraform root manages the application in us-east-2:

- Public HTTPS ALB across two public subnets.
- One API Fargate task and one background task containing publisher and worker.
- Public task IPs for outbound connectivity, with restricted security groups.
- Private single-AZ PostgreSQL 16 with 20 GiB encrypted storage.
- Standard SQS delivery queue and DLQ, ECR, scoped IAM and seven-day logs.

There is no NAT Gateway, autoscaling, or multi-AZ database failover. Rolling
deployments can temporarily run extra tasks. ALB, Fargate, RDS, public IPv4,
storage and usage incur charges. Reconfirm the regional estimate before apply;
the earlier Oregon low-traffic planning budget was approximately $3-$5 for 24
hours, excluding domain registration and taxes. Recalculate for Ohio before
provisioning. Destroy application resources after
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

The current AWS Settings project is Idea Incubator in Ohio (`us-east-2`). Use
browser-based temporary credentials, not root credentials or long-term access
keys. Project IAM users cannot sign in to the console; the project owner signs
in through AWS Settings and receives an assumed-role session.

Run this interactive login locally and select Idea Incubator in the browser:

```powershell
aws login --profile webhook-login --region us-east-2
aws sts get-caller-identity --profile webhook-login --region us-east-2
```

Login writes a local temporary-credential cache. STS only reads identity. Confirm
the intended account and an assumed-role ARN such as `AccountFullAccessRole`;
that role name does not override project service/region restrictions.

For compatibility with Terraform and its S3 backend, use the CLI's documented
process-credential bridge. These commands write only local AWS profile settings;
they do not create AWS resources. Run them once after reviewing the profile name:

```powershell
aws configure set credential_process 'aws configure export-credentials --profile webhook-login --format process' --profile webhook-terraform
aws configure set region us-east-2 --profile webhook-terraform
```

Keep the two profile names distinct to avoid recursive credential resolution.
Set the profile for subsequent Terraform and AWS commands in this terminal:

```powershell
$env:AWS_PROFILE = 'webhook-terraform'
$env:AWS_DEFAULT_REGION = 'us-east-2'
aws sts get-caller-identity --region us-east-2
```

The STS command is read-only: verify the account and role before any mutation.
When the login session expires, repeat `aws login --profile webhook-login
--region us-east-2`. Never print the output of `export-credentials` or copy
credentials into Terraform, `.env`, container definitions or Git. The CLI's
process bridge supplies refreshed credentials directly to the consuming tool.

AWS documentation lists the required services as supported in the project
experience. Actual permissions, quotas and availability still need verification.
Do not activate advanced features or change account plans as part of setup.

Before application provisioning:

1. Obtain a hostname you control and an issued ACM certificate in us-east-2.
   Certificate DNS validation requires access to that hostname's DNS records.
2. Bootstrap a separate S3 state bucket in us-east-2, with versioning,
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
aws_region                     = "us-east-2"
start_services                 = false
image_uri                      = "ACCOUNT.dkr.ecr.us-east-2.amazonaws.com/webhook-platform:demo-001"
certificate_arn                = "YOUR_ISSUED_CERTIFICATE_ARN"
github_secret_parameter_arn     = "YOUR_GITHUB_PARAMETER_ARN"
slack_url_parameter_arn         = "YOUR_SLACK_PARAMETER_ARN"
database_password_parameter_arn = "YOUR_APPLICATION_PASSWORD_PARAMETER_ARN"
database_admin_password_parameter_arn = "YOUR_ADMINISTRATOR_PASSWORD_PARAMETER_ARN"
```

Provision four SSM SecureString values outside Terraform: the tenant-1 GitHub
secret, tenant-1 Slack URL, restricted `webhook_app` database password, and the
`webhook_admin` password matching the ephemeral RDS input.
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

Keep `start_services = false` for the initial apply. Terraform creates the ECS
services with zero tasks, so they do not pull the image or connect to PostgreSQL.
Other resources, including ALB and RDS, still incur charges. Supply the intended
ECR image URI even before that image is uploaded; the task definitions reference
it but do not run it. No targeted apply is needed for this startup sequence.

1. Prepare state, certificate, inputs and operator-provisioned secrets.
2. Review and approve an initial plan with `start_services = false`, then
   provision infrastructure with both ECS services at zero tasks.
3. Build and push the production image to the created repository.
4. Run a controlled one-off task with private database access to apply the
   reviewed migration and create the restricted application login.
5. Create tenant/source/destination/rule configuration through reviewed SQL.
6. Set `start_services = true` in the local inputs, review and approve a new plan,
   then apply it to start one task per service. Point the hostname at the ALB
   and verify HTTPS. Keep this value true for subsequent running deployments.

Setting the input back to false and applying stops service tasks, but does not
destroy the remaining infrastructure or stop all charges.

### Build and push image

After ECR exists, set these values from the actual repository and chosen unique
tag. Immutable tags cannot be overwritten:

```powershell
$repository = 'ACCOUNT.dkr.ecr.us-east-2.amazonaws.com/webhook-platform'
$registry = $repository.Split('/')[0]
$image = "${repository}:demo-001"
docker build --platform linux/amd64 --target production -t $image .
aws ecr get-login-password --region us-east-2 | docker login --username AWS --password-stdin $registry
docker push $image
```

The build downloads dependencies and writes a local image. ECR login retrieves a
temporary token and updates local Docker credentials. Push uploads the image and
incurs storage usage. Check each command's exit code before proceeding; update
`image_uri` to match the pushed image.

### Database bootstrap

The production image includes `bootstrap_database.py` and migration 001. A
separate task definition uses the administrator secret; normal service roles
cannot read it. The operator needs permission to run this task and pass its
execution role. Do not run migrations automatically on application startup.

Keep services stopped. After the initial apply and image push, read identifiers
from state, checking each Terraform command succeeds before continuing:

```powershell
$cluster = terraform -chdir=infra output -raw ecs_cluster_name
$bootstrapDefinition = terraform -chdir=infra output -raw bootstrap_task_definition_arn
$bootstrapSubnets = terraform -chdir=infra output -json public_subnet_ids | ConvertFrom-Json
$bootstrapSecurityGroup = terraform -chdir=infra output -raw background_security_group_id
```

The following block launches one task, incurs brief Fargate/public IPv4 usage,
and changes PostgreSQL. Obtain approval for each action before running it.
Run the block once per action, in this order, checking success before proceeding:

1. `$action = 'migrate'`: create the schema.
2. `$action = 'create-app-user'`: create the restricted login and grants.
3. `$action = 'configure-demo'`: insert the tenant, source, Slack destination and
   enabled `push` routing rule.

Do not run actions concurrently. Repeated migration/user setup fails if the
objects exist; demo configuration rejects nonempty configuration tables.
Build and push an image containing all three actions before launching tasks.

```powershell
$action = 'migrate' # Later invocations: 'create-app-user', then 'configure-demo'
$bootstrapRequest = @{
    cluster = $cluster
    taskDefinition = $bootstrapDefinition
    launchType = 'FARGATE'
    count = 1
    clientToken = [guid]::NewGuid().ToString()
    networkConfiguration = @{
        awsvpcConfiguration = @{
            subnets = @($bootstrapSubnets)
            securityGroups = @($bootstrapSecurityGroup)
            assignPublicIp = 'ENABLED'
        }
    }
    overrides = @{
        containerOverrides = @(@{
            name = 'bootstrap'
            command = @('python', 'bootstrap_database.py', $action)
        })
    }
}
# Initialized Terraform directory is ignored by Git. The request has no secrets.
$requestPath = Join-Path (Resolve-Path 'infra/.terraform').Path 'bootstrap-request.json'
[System.IO.File]::WriteAllText($requestPath, ($bootstrapRequest | ConvertTo-Json -Depth 10))
$launchJson = aws ecs run-task --cli-input-json "file://$requestPath" --region us-east-2 --output json --no-cli-pager
if ($LASTEXITCODE -ne 0) { throw 'Launch outcome uncertain; inspect ECS before retrying.' }
$launch = $launchJson | ConvertFrom-Json
if (@($launch.failures).Count -gt 0 -or @($launch.tasks).Count -ne 1) {
    throw 'ECS did not launch exactly one task; inspect the launch response.'
}
$bootstrapTask = $launch.tasks[0].taskArn
$bootstrapTask

aws ecs wait tasks-stopped --cluster $cluster --tasks $bootstrapTask --region us-east-2
if ($LASTEXITCODE -ne 0) { throw 'Wait failed; inspect the existing task rather than launching another.' }
$resultJson = aws ecs describe-tasks --cluster $cluster --tasks $bootstrapTask --region us-east-2 --output json --no-cli-pager
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the task.' }
$result = $resultJson | ConvertFrom-Json
if (@($result.failures).Count -gt 0 -or @($result.tasks).Count -ne 1) {
    throw 'Task status unavailable; inspect ECS.'
}
$container = @($result.tasks[0].containers | Where-Object name -eq 'bootstrap')
if ($container.Count -ne 1 -or $null -eq $container[0].exitCode -or $container[0].exitCode -ne 0) {
    throw 'Bootstrap failed or never started; inspect task status and bootstrap logs.'
}
Write-Output "Bootstrap action $action succeeded."
Remove-Item -LiteralPath $requestPath
```

`tasks-stopped` and `describe-tasks` only read status. STOPPED alone does not
mean success: image pulls, secret injection or SQL may have failed. Inspect
the container exit code and the background log group's `bootstrap/bootstrap/`
stream. On a timeout or uncertain launch response, inspect existing tasks first;
the request file preserves the client token for an identical launch retry.
Do not generate a new request/token until the previous outcome is understood.

Each action commits separately. If an action fails, investigate before retrying;
do not rerun earlier successful actions. Six local PostgreSQL tests passed for
migration/rerun handling, grant rollback, restricted permissions, correct demo
routing, repeat rejection and demo transaction rollback. These AWS commands have
not been verified against ECS/RDS.

`configure-demo` requires empty configuration tables and a generated tenant ID
of 1, matching the current task environment mappings. It stores only
`env:GITHUB_WEBHOOK_SECRET_TENANT_1_LOCAL` and
`env:SLACK_WEBHOOK_URL_TENANT_1_DEMO` references, never raw secret values. It logs
the committed source ID. Use that ID in GitHub's webhook URL:
`https://YOUR_HOSTNAME/webhooks/github/SOURCE_ID`, selecting `push` events and
the GitHub secret provisioned in SSM.

If setup rolls back after consuming identity values, PostgreSQL sequences are
not rolled back. Empty tables alone therefore do not guarantee tenant ID 1.
Inspect a failed setup rather than resetting sequences or deleting existing
data automatically. Verify all three actions succeed and application access
works before enabling services with `start_services = true`.

Application connections currently use `PGSSLMODE=require`: traffic is encrypted,
but server identity is not verified as it would be with `verify-full` and a
trusted RDS CA bundle. Keep this limitation explicit when reviewing deployment.

## Reviewed plan and apply

After validation and review of the bootstrap procedure, create and inspect the
initial plan with `start_services = false` in `demo.tfvars`:

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
aws ecs describe-services --cluster $cluster --services $api $background --region us-east-2
aws elbv2 describe-target-health --target-group-arn $targetGroup --region us-east-2
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
- [Browser login and process-credential bridge](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)
- [Project access management](https://docs.aws.amazon.com/accounts/latest/reference/sign-up-for-aws.html)
