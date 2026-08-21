# ── Cognito OIDC application ────────────────────────────────────────────────
#
# The public dashboard is a browser-based OAuth client. It uses Cognito's
# managed login with authorization code + PKCE; no client secret is issued to
# the frontend. The backend validates the resulting access token directly
# against Cognito's issuer and JWKS endpoint.

resource "aws_cognito_user_pool" "homeops" {
  name                = "homeops-${var.environment}"
  username_attributes = ["email"]
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = {
    Name        = "homeops-user-pool-${var.environment}"
    Environment = var.environment
    Project     = "homeops"
  }
}

resource "aws_cognito_resource_server" "homeops" {
  identifier = "https://api.homeops.now"
  name       = "HomeOps API"
  user_pool_id = aws_cognito_user_pool.homeops.id

  scope {
    scope_name        = "diagnostic:read"
    scope_description = "Ask HomeOps for bounded HVAC diagnostics"
  }
}

resource "aws_cognito_user_pool_client" "frontend" {
  name                                 = "homeops-frontend-${var.environment}"
  user_pool_id                         = aws_cognito_user_pool.homeops.id
  generate_secret                      = false
  prevent_user_existence_errors        = "ENABLED"
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  supported_identity_providers         = ["COGNITO"]
  allowed_oauth_scopes = [
    "openid",
    "email",
    "profile",
    "${aws_cognito_resource_server.homeops.identifier}/diagnostic:read",
  ]
  callback_urls = [
    "https://homeops.now/auth/callback",
    "http://localhost:5173/auth/callback",
  ]
  logout_urls = [
    "https://homeops.now/",
    "http://localhost:5173/",
  ]
}

resource "aws_cognito_user_pool_domain" "homeops" {
  domain       = var.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.homeops.id
}

locals {
  homeops_cognito_issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.homeops.id}"
  homeops_cognito_authority = "https://${var.cognito_domain_prefix}.auth.${var.aws_region}.amazoncognito.com"
  homeops_cognito_scope    = "${aws_cognito_resource_server.homeops.identifier}/diagnostic:read"
}

# These are non-secret runtime settings. The EC2 instance reads them with its
# existing SSM role and writes a mode-0600 Compose environment file at deploy
# time, keeping the checked-out repository free of runtime configuration.
resource "aws_ssm_parameter" "ask_homeops_oidc_issuer" {
  name  = "/homeops/${var.environment}/ask-homeops-oidc-issuer"
  type  = "String"
  value = local.homeops_cognito_issuer
  tags  = { Environment = var.environment, Project = "homeops" }
}

resource "aws_ssm_parameter" "ask_homeops_oidc_audience" {
  name  = "/homeops/${var.environment}/ask-homeops-oidc-audience"
  type  = "String"
  value = aws_cognito_user_pool_client.frontend.id
  tags  = { Environment = var.environment, Project = "homeops" }
}

resource "aws_ssm_parameter" "ask_homeops_oidc_audience_claim" {
  name  = "/homeops/${var.environment}/ask-homeops-oidc-audience-claim"
  type  = "String"
  value = "client_id"
  tags  = { Environment = var.environment, Project = "homeops" }
}

resource "aws_ssm_parameter" "ask_homeops_oidc_jwks_url" {
  name  = "/homeops/${var.environment}/ask-homeops-oidc-jwks-url"
  type  = "String"
  value = "${local.homeops_cognito_issuer}/.well-known/jwks.json"
  tags  = { Environment = var.environment, Project = "homeops" }
}

resource "aws_ssm_parameter" "ask_homeops_diagnostic_scope" {
  name  = "/homeops/${var.environment}/ask-homeops-diagnostic-scope"
  type  = "String"
  value = local.homeops_cognito_scope
  tags  = { Environment = var.environment, Project = "homeops" }
}

resource "aws_ssm_parameter" "ask_homeops_limiter_backend" {
  name  = "/homeops/${var.environment}/ask-homeops-limiter-backend"
  type  = "String"
  value = "redis"
  tags  = { Environment = var.environment, Project = "homeops" }
}

resource "aws_ssm_parameter" "ask_homeops_redis_url" {
  name  = "/homeops/${var.environment}/ask-homeops-redis-url"
  type  = "String"
  value = "redis://127.0.0.1:6379/0"
  tags  = { Environment = var.environment, Project = "homeops" }
}
