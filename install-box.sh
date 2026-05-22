#!/bin/bash
# Install yt-play on the RK322x box.
# Usage: sudo ./install-box.sh PROXY_IP
#   PROXY_IP: IP of the machine running yt_proxy.py (e.g. 192.168.1.10)

set -e

PROXY_IP="${1:-}"

if [ -z "$PROXY_IP" ]; then
    echo "Usage: sudo ./install-box.sh PROXY_IP"
    echo "  Example: sudo ./install-box.sh 192.168.1.10"
    exit 1
fi

if [ "$EUID" -ne 0 ]; then
    echo "Run with sudo: sudo ./install-box.sh $PROXY_IP"
    exit 1
fi

echo "==> Installing GStreamer packages..."
apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-libav \
    gstreamer1.0-alsa

echo "==> Verifying hardware decoder..."
if ! gst-inspect-1.0 v4l2slh264dec > /dev/null 2>&1; then
    echo "WARNING: v4l2slh264dec not found. Check kernel version (requires 6.6+)."
fi

echo "==> Installing yt-play..."
install -m 755 yt-play /usr/local/bin/yt-play

echo "==> Setting proxy IP to $PROXY_IP..."
echo "export RK_PROXY_IP=$PROXY_IP" > /etc/profile.d/rk322x-proxy.sh
chmod 644 /etc/profile.d/rk322x-proxy.sh

echo ""
echo "Done. Log out and back in, then test with:"
echo "  yt-play dQw4w9WgXcQ"
