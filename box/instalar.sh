#!/bin/bash
# Instala o lado da TV box (rodar no proprio box, como root).
set -e
cd "$(dirname "$0")"

# quickjs: runtime JavaScript que o yt-dlp usa para resolver o desafio do
# YouTube. O padrao dele e o Deno, que nao tem binario para armv7.
if ! command -v qjs >/dev/null; then
  apt-get install -y quickjs
fi

install -m 755 tv-player tv-receiver tv-receiver-audio tv-web /usr/local/bin/
install -m 644 ./*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tv-player tv-receiver tv-receiver-audio tv-web

echo "pronto. Fixe o modo de video em /boot/armbianEnv.txt:"
echo '  extraargs=coherent_pool=2M cma=128M video=HDMI-A-1:1920x1080@60'
echo
echo "Pagina do celular: http://$(hostname -I | awk '{print $1}'):8080"
echo
echo "Se o YouTube responder \"Sign in to confirm you're not a bot\", exporte os"
echo "cookies de uma conta descartavel (formato Netscape) para:"
echo "  /usr/local/etc/yt-cookies.txt   (chmod 600, dono root)"
