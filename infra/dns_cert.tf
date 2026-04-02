# ── Route53 Hosted Zone ───────────────────────────────────────────────────────
# homeops.now was registered 2026-04-02. If the hosted zone was auto-created
# by Route53 at registration, import it instead of creating:
#   terraform import aws_route53_zone.homeops <ZONE_ID>

resource "aws_route53_zone" "homeops" {
  name    = var.domain
  comment = "HomeOps dashboard — homeops.now"

  tags = {
    Name        = var.domain
    Environment = var.environment
    Project     = "homeops"
  }
}

# ── ACM Certificate (us-east-1 — required for CloudFront) ────────────────────

resource "aws_acm_certificate" "homeops" {
  provider                  = aws.us_east_1
  domain_name               = var.domain
  subject_alternative_names = ["*.${var.domain}"]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name        = var.domain
    Environment = var.environment
    Project     = "homeops"
  }
}

# ── DNS validation records for ACM ───────────────────────────────────────────

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.homeops.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  allow_overwrite = true
  name            = each.value.name
  records         = [each.value.record]
  ttl             = 60
  type            = each.value.type
  zone_id         = aws_route53_zone.homeops.zone_id
}

resource "aws_acm_certificate_validation" "homeops" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.homeops.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# ── Route53 DNS Records ───────────────────────────────────────────────────────

# Apex: homeops.now → CloudFront (A alias)
resource "aws_route53_record" "apex" {
  zone_id = aws_route53_zone.homeops.zone_id
  name    = var.domain
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.frontend.domain_name
    zone_id                = aws_cloudfront_distribution.frontend.hosted_zone_id
    evaluate_target_health = false
  }
}

# API subdomain: api.homeops.now → EC2 Elastic IP
resource "aws_route53_record" "api" {
  zone_id = aws_route53_zone.homeops.zone_id
  name    = "api.${var.domain}"
  type    = "A"
  ttl     = 300
  records = [aws_eip.homeops.public_ip]
}

# Grafana subdomain: grafana.homeops.now → EC2 Elastic IP
resource "aws_route53_record" "grafana" {
  zone_id = aws_route53_zone.homeops.zone_id
  name    = "grafana.${var.domain}"
  type    = "A"
  ttl     = 300
  records = [aws_eip.homeops.public_ip]
}
