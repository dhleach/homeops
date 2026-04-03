# ── SSH Key Pair ──────────────────────────────────────────────────────────────

resource "aws_key_pair" "homeops" {
  key_name   = "homeops-${var.environment}"
  public_key = var.ssh_public_key

  tags = {
    Name        = "homeops-${var.environment}"
    Environment = var.environment
    Project     = "homeops"
  }
}

# ── AMI — latest Ubuntu 24.04 LTS ARM64 ──────────────────────────────────────

data "aws_ami" "ubuntu_arm64" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

# ── EC2 Instance ──────────────────────────────────────────────────────────────

resource "aws_instance" "homeops" {
  ami                    = data.aws_ami.ubuntu_arm64.id
  instance_type          = var.ec2_instance_type
  key_name               = aws_key_pair.homeops.key_name
  vpc_security_group_ids = [aws_security_group.homeops_ec2.id]
  iam_instance_profile   = aws_iam_instance_profile.homeops_ec2.name

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.ebs_volume_size_gb
    delete_on_termination = true
    encrypted             = true

    tags = {
      Name        = "homeops-ec2-root-${var.environment}"
      Environment = var.environment
      Project     = "homeops"
    }
  }

  # Bootstrap: install Docker, Docker Compose, Nginx, clone homeops repo, bake SSH keys, join Tailscale
  user_data = <<-EOF
    #!/bin/bash
    set -e
    apt-get update -y
    apt-get install -y docker.io docker-compose-v2 nginx git curl

    # Add ubuntu user to docker group
    usermod -aG docker ubuntu

    # Enable and start services
    systemctl enable docker nginx
    systemctl start docker nginx

    # Clone homeops repo
    git clone https://github.com/dhleach/homeops.git /home/ubuntu/homeops
    chown -R ubuntu:ubuntu /home/ubuntu/homeops

    # Bake authorized SSH keys — survives instance replacement
    mkdir -p /home/ubuntu/.ssh
    chmod 700 /home/ubuntu/.ssh
    cat >> /home/ubuntu/.ssh/authorized_keys << 'SSHKEYS'
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK4NwtPsdoheR2mUazj1QydrJXYp/qtWbEUDmgQiWES3 homeops-production
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHeD3GdgQoCeFJNsimj5MzcUZDHG/pFemcScU0qRg5Tz bobclawbot@openclaw
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIF0AyrnOaA5cfz8vA3JcP+eeWiXavnts2KDj1Byl5Kfx dhlea@LAPTOP-DH9TGJI8
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK4NwtPsdoheR2mUazj1QydrJXYp/qtWbEUDmgQiWES3 leachd@pi-homeops
SSHKEYS
    chmod 600 /home/ubuntu/.ssh/authorized_keys
    chown -R ubuntu:ubuntu /home/ubuntu/.ssh

    # Install and join Tailscale
    curl -fsSL https://tailscale.com/install.sh | sh
    tailscale up --authkey="${var.tailscale_authkey}" --hostname="homeops-ec2" --accept-routes

    # Install certbot for Let's Encrypt
    apt-get install -y certbot python3-certbot-nginx

    # Deploy Nginx config for api.homeops.now -> FastAPI
    mkdir -p /var/www/certbot
    cp /home/ubuntu/homeops/dashboard/nginx/api.homeops.now.conf /etc/nginx/sites-available/api.homeops.now
    ln -sf /etc/nginx/sites-available/api.homeops.now /etc/nginx/sites-enabled/api.homeops.now
    rm -f /etc/nginx/sites-enabled/default

    # Install HTTP-only config first so certbot challenge can succeed
    cat > /etc/nginx/sites-available/api.homeops.now << 'NGINXEOF'
server {
    listen 80;
    listen [::]:80;
    server_name api.homeops.now;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { proxy_pass http://localhost:8000; }
}
NGINXEOF
    nginx -t && systemctl reload nginx

    # Obtain Let's Encrypt cert with retry (DNS may not resolve immediately after EIP assignment)
    for i in 1 2 3 4 5; do
      if certbot --nginx -d api.homeops.now --non-interactive --agree-tos -m admin@homeops.now; then
        echo "certbot succeeded on attempt $i" >> /var/log/homeops-bootstrap.log
        break
      fi
      echo "certbot attempt $i failed, retrying in 60s..." >> /var/log/homeops-bootstrap.log
      sleep 60
    done

    # Install full config with SSL paths (certbot may have already modified the file; overwrite cleanly)
    cp /home/ubuntu/homeops/dashboard/nginx/api.homeops.now.conf /etc/nginx/sites-available/api.homeops.now
    nginx -t && systemctl reload nginx

    # Enable certbot auto-renewal
    systemctl enable certbot.timer
    systemctl start certbot.timer || true

    echo "Bootstrap complete" > /var/log/homeops-bootstrap.log
  EOF

  tags = {
    Name        = "homeops-${var.environment}"
    Environment = var.environment
    Project     = "homeops"
  }
}

# ── Elastic IP ────────────────────────────────────────────────────────────────
# Stable public IP that survives stop/start cycles.

resource "aws_eip" "homeops" {
  instance = aws_instance.homeops.id
  domain   = "vpc"

  tags = {
    Name        = "homeops-eip-${var.environment}"
    Environment = var.environment
    Project     = "homeops"
  }
}
