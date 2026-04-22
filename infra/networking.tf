# ── VPC & Networking ──────────────────────────────────────────────────────────
# Uses the default VPC to keep the config simple. A dedicated VPC would be
# appropriate for a production multi-service deployment; for a single-EC2
# portfolio project the default VPC is fine.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ── Security Group ────────────────────────────────────────────────────────────

resource "aws_security_group" "homeops_ec2" {
  name        = "homeops-ec2-${var.environment}"
  description = "HomeOps EC2: HTTPS public, SSH from Tailscale only"
  vpc_id      = data.aws_vpc.default.id

  # HTTPS — public (CloudFront and direct API access)
  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # HTTP — redirect to HTTPS via Nginx
  ingress {
    description = "HTTP (redirect to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # SSH — Bob agent container public IP (Derek accesses via Tailscale, no SG rule needed)
  ingress {
    description = "SSH from Bob agent container (public IP - Derek uses Tailscale)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${var.agent_ip}/32"]
  }

  # Prometheus remote_write from Pi (Tailscale only)
  ingress {
    description = "Prometheus remote_write from Pi (Tailscale)"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = ["${var.tailscale_ip}/32"]
  }

  # k3s API server - Pi control plane needs to reach EC2
  # worker on 6443
  ingress {
    description = "k3s API server from Pi (Tailscale)"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = ["100.115.21.72/32"]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "homeops-ec2-${var.environment}"
    Environment = var.environment
    Project     = "homeops"
  }
}
