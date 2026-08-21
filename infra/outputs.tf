output "ec2_public_ip" {
  description = "Elastic IP of the homeops EC2 instance"
  value       = aws_eip.homeops.public_ip
}

output "ec2_instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.homeops.id
}

output "cloudfront_domain" {
  description = "CloudFront distribution domain (before custom domain propagates)"
  value       = aws_cloudfront_distribution.frontend.domain_name
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID — needed for cache invalidations in CI/CD"
  value       = aws_cloudfront_distribution.frontend.id
}

output "s3_bucket_name" {
  description = "S3 bucket name for frontend deploys (aws s3 sync)"
  value       = aws_s3_bucket.frontend.bucket
}

output "route53_zone_id" {
  description = "Route53 hosted zone ID for homeops.now"
  value       = aws_route53_zone.homeops.zone_id
}

output "acm_certificate_arn" {
  description = "ACM certificate ARN (us-east-1) for CloudFront"
  value       = aws_acm_certificate.homeops.arn
}

output "ssh_connect" {
  description = "SSH command to connect to EC2"
  value       = "ssh -i ~/.ssh/id_ed25519 ubuntu@${aws_eip.homeops.public_ip}"
}

output "cognito_oidc_issuer" {
  description = "Cognito issuer URL used by the Ask HomeOps backend"
  value       = local.homeops_cognito_issuer
}

output "cognito_managed_login_authority" {
  description = "Cognito managed-login authority used by the browser"
  value       = local.homeops_cognito_authority
}

output "cognito_frontend_client_id" {
  description = "Public Cognito app-client ID used by the frontend"
  value       = aws_cognito_user_pool_client.frontend.id
}

output "cognito_diagnostic_scope" {
  description = "Custom scope requested by the frontend and required by the API"
  value       = local.homeops_cognito_scope
}

output "cognito_frontend_scope" {
  description = "Complete OAuth scope string for the frontend authorization request"
  value       = "openid email profile ${local.homeops_cognito_scope}"
}
