#!/bin/bash
# Install the media proxy server on any Linux machine (Debian/Ubuntu).
# Usage: sudo ./install-proxy.sh

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Run with sudo: sudo ./install-proxy.sh"
    exit 1
fi

PROXY_USER="${SUDO_USER:-$USER}"
INSTALL_DIR="/opt/rk322x-proxy"
PYTHON=$(command -v python3 || true)

if [ -z "$PYTHON" ]; then
    echo "Error: python3 not found. Install it first."
    exit 1
fi

echo "==> Installing system dependencies..."
apt-get install -y ffmpeg nodejs python3-pip

echo "==> Installing yt-dlp..."
pip3 install -q --break-system-packages yt-dlp 2>/dev/null \
    || pip3 install -q yt-dlp

echo "==> Installing proxy to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
install -m 644 proxy/yt_proxy.py "$INSTALL_DIR/yt_proxy.py"

echo "==> Creating systemd service..."
cat > /etc/systemd/system/yt-proxy.service << EOF
[Unit]
Description=YouTube/Media Proxy for RK322x
After=network.target

[Service]
User=$PROXY_USER
ExecStart=$PYTHON $INSTALL_DIR/yt_proxy.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now yt-proxy.service

echo ""
echo "Done. Proxy running on port 8091."
echo "Check status: systemctl status yt-proxy"
echo ""
echo "Optional: add a YouTube cookies file for authenticated access:"
echo "  $INSTALL_DIR/yt_proxy.py  ->  set COOKIES = '/path/to/cookies.txt'"
