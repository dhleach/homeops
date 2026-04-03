variable "aws_region" {
  description = "Primary AWS region for all resources except ACM (which must be us-east-1)"
  type        = string
  default     = "us-east-1"
}

variable "domain" {
  description = "Root domain for the dashboard (registered in Route53)"
  type        = string
  default     = "homeops.now"
}

variable "environment" {
  description = "Deployment environment tag"
  type        = string
  default     = "production"
}

variable "tailscale_ip" {
  description = "Tailscale IP of the Pi — only source allowed SSH access to EC2"
  type        = string
  default     = "100.115.21.72"
}

variable "ec2_instance_type" {
  description = "EC2 instance type — t4g.micro is ARM64, free-tier eligible"
  type        = string
  default     = "t4g.micro"
}

variable "ebs_volume_size_gb" {
  description = "Root EBS volume size in GB"
  type        = number
  default     = 20
}

variable "ssh_public_key" {
  description = "SSH public key to inject into EC2 for admin access"
  type        = string
  # Set via: export TF_VAR_ssh_public_key="$(cat ~/.ssh/id_ed25519.pub)"
  # or in terraform.tfvars
}

variable "agent_ip" {
  description = "Bob agent container public IP for SSH access"
  type        = string
}

variable "tailscale_authkey" {
  description = "Tailscale auth key for EC2 to join Tailnet"
  type        = string
  sensitive   = true
}
