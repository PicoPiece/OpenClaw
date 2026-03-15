#!/bin/bash
set -euo pipefail

echo "=== OpenClaw Firewall Setup ==="
echo "This script requires sudo. Run it in a terminal where you can enter your password."
echo ""

sudo ufw default deny incoming
sudo ufw default allow outgoing

sudo ufw allow ssh
sudo ufw allow 9090/tcp   # Prometheus
sudo ufw allow 8080/tcp   # Jenkins
sudo ufw allow 3000/tcp   # Grafana
sudo ufw allow 8000/tcp   # xiaozhi-server
sudo ufw allow 8002/tcp   # xiaozhi-web
sudo ufw allow 8003/tcp   # xiaozhi
sudo ufw allow 8005/tcp   # parent-dashboard
sudo ufw allow 9101/tcp   # python monitor

echo ""
echo "Enabling ufw..."
echo "y" | sudo ufw enable

echo ""
echo "=== Firewall status ==="
sudo ufw status numbered

echo ""
echo "DONE. Port 18789 (OpenClaw) is NOT allowed -- localhost only."
echo "If Docker bypasses ufw, OpenClaw is still safe because it binds to 127.0.0.1."
