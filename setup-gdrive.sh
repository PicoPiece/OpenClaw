#!/bin/bash
set -euo pipefail

RCLONE="/home/picopiece/.local/bin/rclone"

echo "=== Google Drive Authorization cho OpenClaw Backup ==="
echo ""
echo "Script nay se mo 1 URL. Ban can:"
echo "  1. Copy URL va mo tren trinh duyet (may tinh hoac dien thoai)"
echo "  2. Dang nhap Google account"
echo "  3. Cho phep 'rclone' truy cap Google Drive"
echo "  4. Doi cho den khi man hinh nay bao 'Success'"
echo ""
echo "Nhan Enter de bat dau..."
read -r

$RCLONE authorize "drive"

echo ""
echo "=== Kiem tra ket noi ==="
$RCLONE lsd gdrive: 2>&1 && echo "OK! Google Drive da ket noi." || echo "FAIL: Chua ket noi duoc."
echo ""
echo "Neu OK, backup tu dong se upload len Google Drive."
echo "Test thu: bash /home/picopiece/openclaw/backup-gdrive.sh"
