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

  # Bootstrap: install Docker, Docker Compose, Nginx, clone homeops repo, bake SSH keys, join Tailscale, get TLS cert, join k3s
  # IMPORTANT: Do NOT use set -e — individual section failures are logged and handled gracefully.
  # Progress is written to /var/log/homeops-bootstrap.log for post-boot debugging.
  user_data = <<-EOF
    #!/bin/bash
    LOG=/var/log/homeops-bootstrap.log
    echo "=== Bootstrap started $(date -u) ===" > $LOG

    # ── 1. System packages ───────────────────────────────────────────────────
    apt-get update -y >> $LOG 2>&1
    # Install AWS CLI v2 (v1 from apt is EOL and can fail with IMDSv2)
    apt-get install -y docker.io docker-compose-v2 nginx git curl unzip >> $LOG 2>&1
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip >> $LOG 2>&1
    unzip -q /tmp/awscliv2.zip -d /tmp >> $LOG 2>&1
    /tmp/aws/install >> $LOG 2>&1 && echo "[OK] AWS CLI v2 installed" >> $LOG || echo "[WARN] AWS CLI v2 install failed" >> $LOG
    rm -rf /tmp/awscliv2.zip /tmp/aws
    usermod -aG docker ubuntu >> $LOG 2>&1
    systemctl enable docker nginx >> $LOG 2>&1
    systemctl start docker nginx >> $LOG 2>&1
    echo "[OK] packages installed" >> $LOG

    # ── 2. SSH keys ───────────────────────────────────────────────────────────
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
    echo "[OK] SSH keys baked" >> $LOG

    # ── 3. Clone repo ─────────────────────────────────────────────────────────
    git clone https://github.com/dhleach/homeops.git /home/ubuntu/homeops >> $LOG 2>&1 \
      && chown -R ubuntu:ubuntu /home/ubuntu/homeops \
      && echo "[OK] repo cloned" >> $LOG \
      || echo "[WARN] repo clone failed" >> $LOG

    # ── 4. Tailscale ─────────────────────────────────────────────────────────
    # Auth key pulled from SSM — NOT interpolated from Terraform vars (would land in state plaintext)
    curl -fsSL https://tailscale.com/install.sh | sh >> $LOG 2>&1
    if command -v tailscale &>/dev/null; then
      TS_AUTHKEY=$(/usr/local/bin/aws ssm get-parameter \
        --name "/homeops/${var.environment}/tailscale-authkey" \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text \
        --region ${var.aws_region} 2>/dev/null)
      if [ -z "$TS_AUTHKEY" ]; then
        echo "[WARN] Tailscale authkey not found in SSM — Tailscale will not join" >> $LOG
      else
        tailscale up --authkey="$TS_AUTHKEY" --hostname="homeops-ec2" --accept-routes >> $LOG 2>&1
      fi
      # Verify Tailscale actually joined (has an IP)
      for i in 1 2 3 4 5; do
        TS_IP=$(tailscale ip --4 2>/dev/null)
        if [ -n "$TS_IP" ]; then
          echo "[OK] Tailscale joined: $TS_IP" >> $LOG
          break
        fi
        echo "[WAIT] Tailscale not ready, attempt $i/5..." >> $LOG
        sleep 10
      done
      [ -z "$TS_IP" ] && echo "[WARN] Tailscale failed to join after 5 attempts" >> $LOG
    else
      echo "[WARN] Tailscale install failed — install manually after boot" >> $LOG
    fi

    # ── 5. Nginx + certbot (HTTP-first, then SSL) ─────────────────────────────
    # CRITICAL: Use HTTP-only config first. DO NOT load SSL config until cert exists.
    # k3s Traefik was blocking port 80 previously — k3s is now installed with --disable traefik.
    apt-get install -y certbot python3-certbot-nginx >> $LOG 2>&1
    mkdir -p /var/www/certbot

    # HTTP-only config — lets certbot ACME challenge through
    cat > /etc/nginx/sites-available/api.homeops.now << 'NGINXEOF'
server {
    listen 80;
    listen [::]:80;
    server_name api.homeops.now;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { proxy_pass http://localhost:8000; }
}
NGINXEOF
    ln -sf /etc/nginx/sites-available/api.homeops.now /etc/nginx/sites-enabled/api.homeops.now
    rm -f /etc/nginx/sites-enabled/default
    nginx -t >> $LOG 2>&1 \
      && systemctl reload nginx >> $LOG 2>&1 \
      && echo "[OK] nginx HTTP-only config loaded" >> $LOG \
      || echo "[WARN] nginx -t failed — certbot will likely fail too" >> $LOG

    # Wait for EIP association to fully propagate before certbot
    sleep 30

    # Run certbot — max 5 retries to avoid Let's Encrypt rate limit (5 failed validations/hr)
    CERT_OK=0
    for i in $(seq 1 5); do
      if certbot --nginx -d api.homeops.now --non-interactive --agree-tos -m admin@homeops.now >> $LOG 2>&1; then
        echo "[OK] certbot succeeded on attempt $i" >> $LOG
        CERT_OK=1
        break
      fi
      echo "[WARN] certbot attempt $i/5 failed, retrying in 60s..." >> $LOG
      [ $i -lt 5 ] && sleep 60
    done

    # Install full nginx config ONLY if cert was obtained
    if [ $CERT_OK -eq 1 ] && [ -f /home/ubuntu/homeops/dashboard/nginx/api.homeops.now.conf ]; then
      cp /home/ubuntu/homeops/dashboard/nginx/api.homeops.now.conf /etc/nginx/sites-available/api.homeops.now
      nginx -t >> $LOG 2>&1 \
        && systemctl reload nginx >> $LOG 2>&1 \
        && echo "[OK] full nginx SSL config installed" >> $LOG \
        || echo "[WARN] full nginx config failed nginx -t, keeping HTTP-only" >> $LOG
    else
      echo "[WARN] certbot failed — keeping HTTP-only nginx config. Run certbot manually after boot." >> $LOG
    fi

    # Enable certbot auto-renewal
    systemctl enable certbot.timer >> $LOG 2>&1 || true
    systemctl start certbot.timer >> $LOG 2>&1 || true

    # ── 6. Docker Compose (homeops stack) ────────────────────────────────────
    if [ -f /home/ubuntu/homeops/dashboard/docker-compose.yml ]; then
      cd /home/ubuntu/homeops/dashboard
      # Pull GEMINI_API_KEY from SSM — required by backend container
      GEMINI_API_KEY=$(/usr/local/bin/aws ssm get-parameter \
        --name "/homeops/${var.environment}/gemini-api-key" \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text \
        --region ${var.aws_region} 2>/dev/null)
      if [ -z "$GEMINI_API_KEY" ]; then
        echo "[WARN] GEMINI_API_KEY not found in SSM — backend container will fail. Add to SSM: /homeops/${var.environment}/gemini-api-key" >> $LOG
      fi
      GEMINI_API_KEY="$GEMINI_API_KEY" docker compose up -d >> $LOG 2>&1 \
        && echo "[OK] docker compose up" >> $LOG \
        || echo "[WARN] docker compose failed — check secrets/env" >> $LOG
    else
      echo "[WARN] docker-compose.yml not found, skipping" >> $LOG
    fi

    # ── 7. k3s agent ─────────────────────────────────────────────────────────
    # Re-query TS_IP — may have come up during the certbot/docker window even if step 4 timed out
    TS_IP=$(tailscale ip --4 2>/dev/null)
    if [ -z "$TS_IP" ]; then
      echo "[WARN] Skipping k3s join — Tailscale not up, can't reach Pi at ${var.tailscale_ip}" >> $LOG
    else
      # Pull k3s join token from SSM
      K3S_TOKEN=$(/usr/local/bin/aws ssm get-parameter \
        --name "/homeops/${var.environment}/k3s-node-token" \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text \
        --region ${var.aws_region} 2>/dev/null)

      if [ -n "$K3S_TOKEN" ]; then
        K3S_URL="https://${var.tailscale_ip}:6443"
        curl -sfL https://get.k3s.io | K3S_URL=$K3S_URL K3S_TOKEN=$K3S_TOKEN sh -s - agent >> $LOG 2>&1 \
          && echo "[OK] k3s agent joined cluster" >> $LOG \
          || echo "[WARN] k3s agent join failed — join manually" >> $LOG
      else
        echo "[INFO] k3s SSM token not found in SSM — join cluster manually" >> $LOG
      fi
    fi

    echo "=== Bootstrap complete $(date -u) ===" >> $LOG
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
