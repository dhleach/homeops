# ── S3 Bucket — Frontend Static Files ────────────────────────────────────────

resource "aws_s3_bucket" "frontend" {
  bucket = "homeops-frontend-${var.environment}"

  tags = {
    Name        = "homeops-frontend-${var.environment}"
    Environment = var.environment
    Project     = "homeops"
  }
}

resource "aws_s3_bucket_versioning" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Block all public access — CloudFront accesses via OAC, not public S3 URLs
resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── CloudFront Origin Access Control ─────────────────────────────────────────
# OAC is the modern replacement for OAI — more secure, required for SSE-S3.

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "homeops-frontend-oac-${var.environment}"
  description                       = "OAC for homeops frontend S3 bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# ── S3 Bucket Policy — allow CloudFront OAC only ─────────────────────────────

data "aws_iam_policy_document" "frontend_s3_policy" {
  statement {
    sid    = "AllowCloudFrontOAC"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_s3_policy.json
}
