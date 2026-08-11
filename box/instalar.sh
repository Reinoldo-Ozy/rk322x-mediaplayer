#!/bin/bash
# Instala o lado da TV box (rodar no proprio box, como root).
set -e
cd "$(dirname "$0")"
install -m 755 tv-player tv-receiver tv-receiver-audio /usr/local/bin/
install -m 644 ./*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tv-player tv-receiver tv-receiver-audio
echo "pronto. Fixe o modo de video em /boot/armbianEnv.txt:"
echo '  extraargs=coherent_pool=2M cma=128M video=HDMI-A-1:1920x1080@60'
