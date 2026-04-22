# ── IAM Role for EC2 ─────────────────────────────────────────────────────────
# Minimal permissions: CloudWatch agent metrics + logs, S3 config read.
# No broad AdministratorAccess — principle of least privilege.

data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "homeops_ec2" {
  name               = "homeops-ec2-role-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
  description        = "HomeOps EC2 instance role - CloudWatch + S3 config read"

  tags = {
    Name        = "homeops-ec2-role-${var.environment}"
    Environment = var.environment
    Project     = "homeops"
  }
}

# CloudWatch Agent — publish metrics and logs
resource "aws_iam_role_policy_attachment" "cloudwatch_agent" {
  role       = aws_iam_role.homeops_ec2.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# S3 read-only for config bucket (future use — Grafana provisioning files etc.)
data "aws_iam_policy_document" "s3_config_read" {
  statement {
    sid     = "ReadConfigBucket"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      aws_s3_bucket.frontend.arn,
      "${aws_s3_bucket.frontend.arn}/*",
    ]
  }
}

resource "aws_iam_policy" "s3_config_read" {
  name        = "homeops-ec2-s3-read-${var.environment}"
  description = "Allow EC2 to read homeops S3 bucket"
  policy      = data.aws_iam_policy_document.s3_config_read.json

  tags = {
    Environment = var.environment
    Project     = "homeops"
  }
}

resource "aws_iam_role_policy_attachment" "s3_config_read" {
  role       = aws_iam_role.homeops_ec2.name
  policy_arn = aws_iam_policy.s3_config_read.arn
}

# SSM — k3s node token: EC2 reads it during bootstrap; homeops-deploy user writes it from Pi
data "aws_iam_policy_document" "ssm_k3s_token_read" {
  statement {
    sid     = "ReadBootstrapSecrets"
    effect  = "Allow"
    actions = ["ssm:GetParameter"]
    # EC2 reads k3s token + Gemini API key during bootstrap
    resources = [
      "arn:aws:ssm:${var.aws_region}:*:parameter/homeops/${var.environment}/k3s-node-token",
      "arn:aws:ssm:${var.aws_region}:*:parameter/homeops/${var.environment}/gemini-api-key"
    ]
  }
  # Needed to decrypt SecureString params (AWS-managed key aws/ssm)
  statement {
    sid     = "DecryptSSMSecrets"
    effect  = "Allow"
    actions = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.aws_region}:*:key/*"]
    condition {
      test     = "StringLike"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "ssm_k3s_token_read" {
  name        = "homeops-ec2-ssm-k3s-token-${var.environment}"
  description = "Allow EC2 to read k3s node token from SSM for automated cluster join"
  policy      = data.aws_iam_policy_document.ssm_k3s_token_read.json

  tags = {
    Environment = var.environment
    Project     = "homeops"
  }
}

resource "aws_iam_role_policy_attachment" "ssm_k3s_token_read" {
  role       = aws_iam_role.homeops_ec2.name
  policy_arn = aws_iam_policy.ssm_k3s_token_read.arn
}

# SSM write — allow homeops-deploy IAM user (used on Pi) to store k3s token
# This lets `aws ssm put-parameter` work from the Pi without needing admin creds
data "aws_iam_policy_document" "ssm_k3s_token_write" {
  statement {
    sid    = "WriteK3sToken"
    effect = "Allow"
    actions = [
      "ssm:PutParameter",
      "ssm:GetParameter",
      "ssm:DeleteParameter"
    ]
    resources = [
      "arn:aws:ssm:${var.aws_region}:*:parameter/homeops/*"
    ]
  }
  statement {
    sid     = "KMSForSSM"
    effect  = "Allow"
    actions = ["kms:GenerateDataKey", "kms:Decrypt"]
    # AWS managed key — wildcard account ID is required here since we don't know account ID at plan time
    resources = ["arn:aws:kms:${var.aws_region}:*:key/*"]
    condition {
      test     = "StringLike"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "ssm_k3s_token_write" {
  name        = "homeops-deploy-ssm-k3s-token-${var.environment}"
  description = "Allow homeops-deploy IAM user to write k3s token to SSM from Pi"
  policy      = data.aws_iam_policy_document.ssm_k3s_token_write.json

  tags = {
    Environment = var.environment
    Project     = "homeops"
  }
}

resource "aws_iam_user_policy_attachment" "ssm_k3s_token_write" {
  user       = "homeops-deploy"
  policy_arn = aws_iam_policy.ssm_k3s_token_write.arn
}

# ── Instance Profile ──────────────────────────────────────────────────────────

resource "aws_iam_instance_profile" "homeops_ec2" {
  name = "homeops-ec2-profile-${var.environment}"
  role = aws_iam_role.homeops_ec2.name

  tags = {
    Name        = "homeops-ec2-profile-${var.environment}"
    Environment = var.environment
    Project     = "homeops"
  }
}
