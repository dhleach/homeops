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
  value       = "ssh -i ~/.ssh/homeops-ec2.pem ubuntu@${aws_eip.homeops.public_ip}"
}
