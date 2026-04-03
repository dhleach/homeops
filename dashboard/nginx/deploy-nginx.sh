#!/bin/bash
# deploy-nginx.sh — Install Nginx config + get Let's Encrypt cert for api.homeops.now
# Run on EC2 from /home/ubuntu/homeops/dashboard/nginx/
set -e

CONF="api.homeops.now"
SITES_AVAILABLE="/etc/nginx/sites-available"
SITES_ENABLED="/etc/nginx/sites-enabled"

echo "=== Installing Nginx config ==="
sudo cp "$CONF.conf" "$SITES_AVAILABLE/$CONF"
sudo ln -sf "$SITES_AVAILABLE/$CONF" "$SITES_ENABLED/$CONF"

# Remove default site if present
sudo rm -f "$SITES_ENABLED/default"

echo "=== Testing Nginx config (HTTP only - SSL certs don't exist yet) ==="
# Temporarily comment out the SSL server block for initial test
sudo sed -i 's/^server {$/# server {/' "$SITES_AVAILABLE/$CONF" || true
# Just test the HTTP block is valid
sudo nginx -t || true
# Restore
sudo cp "$CONF.conf" "$SITES_AVAILABLE/$CONF"

echo "=== Deploying HTTP-only config first (for certbot challenge) ==="
# Use a minimal HTTP-only config to get the cert
sudo tee "$SITES_AVAILABLE/$CONF" > /dev/null << 'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name api.homeops.now;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://localhost:8000;
    }
}
EOF

sudo mkdir -p /var/www/certbot
sudo nginx -t && sudo systemctl reload nginx

echo "=== Installing certbot ==="
sudo apt-get install -y certbot python3-certbot-nginx

echo "=== Obtaining Let's Encrypt certificate ==="
sudo certbot --nginx -d api.homeops.now --non-interactive --agree-tos -m admin@homeops.now

echo "=== Installing full config with SSL ==="
sudo cp "$CONF.conf" "$SITES_AVAILABLE/$CONF"
sudo nginx -t && sudo systemctl reload nginx

echo "=== Setting up certbot auto-renewal ==="
sudo systemctl enable certbot.timer
sudo systemctl start certbot.timer

echo "Done. api.homeops.now is live with HTTPS."
