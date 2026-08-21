# HomeOps Infrastructure (Terraform)

Provisions the AWS half of the [homeops.now](https://homeops.now) production
system. The active application runtime is Docker Compose on one ARM64 EC2
instance; the optional k3s bootstrap is a migration path and is not applied by
the current GitHub Actions deploy workflow.

## Resources

| Resource | Description |
|---|---|
| EC2 t4g.micro (ARM64) | Runs Nginx, Grafana, Prometheus receiver, FastAPI via Docker Compose |
| EBS 20GB gp3 | Root volume, encrypted |
| Elastic IP | Stable public IP for EC2 |
| S3 bucket | Hosts compiled React frontend |
| CloudFront | CDN + HTTPS for frontend, S3 OAC origin |
| ACM certificate | TLS for `homeops.now` + `*.homeops.now`, DNS-validated, us-east-1 |
| Route53 hosted zone | `homeops.now` DNS management |
| Route53 records | Apex → CloudFront, `api.*` → EC2, legacy `grafana.*` → EC2 DNS record |
| Security group | 443/80 public, SSH from the configured `agent_ip`, Prometheus/k3s control traffic from the Pi Tailscale IP |
| IAM role + profile | CloudWatch agent + S3 read for EC2 |
| Cognito user pool | OIDC managed login for the browser-facing Ask HomeOps demo |
| Cognito resource server | `https://api.homeops.now/diagnostic:read` API scope |
| SSM runtime parameters | OIDC and loopback Valkey settings consumed by the EC2 deploy script |

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.6
- AWS CLI configured: `aws configure` (use IAM user with sufficient permissions)
- SSH key pair for EC2 access

## Usage

```bash
cd infra/

# 1. Create your tfvars from the example
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — set ssh_public_key at minimum

# 2. Init
terraform init

# 3. Plan (review what will be created — ~$10/month)
terraform plan

# 4. STOP before a generic apply
# The current production instance has known AMI/bootstrap drift. Read
# terraform-deferred.md and use its allowlisted Ask HomeOps plan instead.

# 5. Note the outputs — you'll need these for CI/CD after the safe rollout
terraform output
```

The normal full-root apply is intentionally deferred while the live EC2
instance is protected by the AMI/user-data drift guard. The safe Ask HomeOps
rollout is documented in
[`terraform-deferred.md`](terraform-deferred.md); its plan must contain no EC2
instance or Elastic IP actions.

The Cognito resources create a public app client for authorization code + PKCE
and a `diagnostic:read` custom scope. After the safe additive rollout, copy the outputs
`cognito_managed_login_authority`, `cognito_frontend_client_id`, and
`cognito_frontend_scope` into the GitHub repository variables
`HOMEOPS_OIDC_AUTHORITY`, `HOMEOPS_OIDC_CLIENT_ID`, and `HOMEOPS_OIDC_SCOPE`.
Use `terraform output -raw cognito_user_pool_id` with
`aws cognito-idp admin-create-user` to invite the allowed demo users before
enabling the frontend login. Public self-registration is disabled.

## CI SSH identity

The EC2 bootstrap in [`ec2.tf`](ec2.tf) installs the dedicated
`homeops-ec2-deploy` public key for the GitHub Actions backend deploy. The
matching private key belongs only in the repository's GitHub Actions
`EC2_DEPLOY_SSH_KEY` secret; never commit or place that private key in
Terraform state. Keep this CI identity separate from the personal
`homeops-production`/Pi key.

## Important: Hosted Zone

Route53 may have **auto-created a hosted zone** when `homeops.now` was registered.
If so, import it before applying to avoid creating a duplicate:

```bash
# Get the zone ID from AWS console or:
aws route53 list-hosted-zones --query "HostedZones[?Name=='homeops.now.'].Id" --output text

# Import (replace Z1234ABCDEF with your zone ID)
terraform import aws_route53_zone.homeops Z1234ABCDEF
terraform plan  # should show 0 changes for the zone
```

## Outputs

After the reviewed additive rollout:

| Output | Use |
|---|---|
| `ec2_public_ip` | SSH target, DNS records |
| `cloudfront_distribution_id` | GitHub Actions cache invalidation |
| `s3_bucket_name` | GitHub Actions `aws s3 sync` target |
| `ssh_connect` | Ready-to-run SSH command |
| `cognito_user_pool_id` | User-pool ID for the admin invite command |
| `cognito_managed_login_authority` | Browser OIDC authority |
| `cognito_frontend_client_id` | Public browser OIDC client ID |
| `cognito_frontend_scope` | Browser OAuth scope string |

## Current interfaces

- The current EC2 Elastic IP resolves as `32.194.69.77` for `api.homeops.now`
  (verified 2026-08-18). Treat the Terraform `ec2_public_ip` output as the
  authority if the instance is recreated.
- The Pi Tailscale peer is `100.115.21.72`; it is supplied as `tailscale_ip`
  and is used for Prometheus scraping and the optional k3s control-plane URL.
- EC2 host services bind through Docker host networking: FastAPI `:8000`,
  Prometheus `:9090`, Grafana `:3000`; Nginx owns public `:80`/`:443`.
- The supported Grafana URL is `https://api.homeops.now/grafana/`. The
  Terraform-created `grafana.homeops.now` DNS record is not currently a
  supported virtual host; see [`docs/architecture.md`](../docs/architecture.md).
- Ask HomeOps quota state uses a loopback-only Valkey container on
  `127.0.0.1:6379`; the backend selects it through the SSM-managed
  `redis://127.0.0.1:6379/0` URL.
- Ask HomeOps bearer tokens are issued by the Cognito user pool and verified by
  the backend against the pool issuer/JWKS. The browser app client has no secret.

For the complete topology and deployment ownership model, see
[`docs/architecture.md`](../docs/architecture.md) and
[`docs/deployment.md`](../docs/deployment.md).

## Cost (~$10-11/month)

| Resource | Cost |
|---|---|
| EC2 t4g.micro | ~$6.00/mo |
| EBS 20GB gp3 | ~$1.60/mo |
| Elastic IP | Free while attached |
| S3 + CloudFront | ~$0.50/mo |
| Route53 hosted zone | $0.50/mo |
| Domain (homeops.now) | ~$1.25/mo (~$15/yr) |
