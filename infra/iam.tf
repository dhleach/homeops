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
  description        = "HomeOps EC2 instance role — CloudWatch + S3 config read"

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
