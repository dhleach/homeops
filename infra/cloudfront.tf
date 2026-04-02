# ── CloudFront Distribution ───────────────────────────────────────────────────

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # US + Europe only — cheapest
  comment             = "homeops.now frontend"
  aliases             = [var.domain]

  # ── S3 Origin (frontend static files) ──────────────────────────────────────
  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "homeops-frontend-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  # ── Default Cache Behaviour (serve React SPA) ───────────────────────────────
  default_cache_behavior {
    target_origin_id       = "homeops-frontend-s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6" # CachingOptimized (AWS managed)

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_router.arn
    }
  }

  # ── SPA fallback: all paths → index.html ────────────────────────────────────
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.homeops.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  tags = {
    Name        = "homeops-frontend-${var.environment}"
    Environment = var.environment
    Project     = "homeops"
  }
}

# ── CloudFront Function: SPA routing ─────────────────────────────────────────
# Rewrites all non-file requests to index.html so React Router works correctly.

resource "aws_cloudfront_function" "spa_router" {
  name    = "homeops-spa-router-${var.environment}"
  runtime = "cloudfront-js-2.0"
  comment = "Rewrite SPA routes to index.html"
  publish = true

  code = <<-JS
    async function handler(event) {
      var request = event.request;
      var uri = request.uri;
      // Pass through requests with a file extension (JS, CSS, images, etc.)
      if (!uri.match(/\.[a-zA-Z0-9]+$/)) {
        request.uri = '/index.html';
      }
      return request;
    }
  JS
}
