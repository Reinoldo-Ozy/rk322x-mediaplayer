#!/bin/bash
# Instala o lado da TV box (rodar no proprio box, como root).
set -e
cd "$(dirname "$0")"

# quickjs: runtime JavaScript que o yt-dlp usa para resolver o desafio do
# YouTube. O padrao dele e o Deno, que nao tem binario para armv7.
if ! command -v qjs >/dev/null; then
  apt-get install -y quickjs
fi

install -m 755 tv-player tv-receiver tv-receiver-audio tv-web tv-remote \
  tv-remote-test /usr/local/bin/
install -m 644 ./*.service /etc/systemd/system/

# Controle infravermelho: o keymap embutido no kernel (rc-rk322x-tvbox) so
# serve para controles com endereco NEC 0x4040. Se o seu usar outro endereco,
# os codigos chegam e nenhuma tecla e gerada -- capture o seu com o modo
# aprendizado do tv-remote-test (porta 8081) e substitua este arquivo.
install -m 644 rk322x-reinoldo.toml /etc/rc_keymaps/
install -m 644 rk322x-reinoldo.toml /usr/lib/udev/rc_keymaps/
if ! grep -q "rk322x-reinoldo.toml" /etc/rc_maps.cfg; then
  printf "gpio_ir_recv\trc-rk322x-tvbox\trk322x-reinoldo.toml\n" >> /etc/rc_maps.cfg
fi
ir-keytable -c -w /etc/rc_keymaps/rk322x-reinoldo.toml >/dev/null 2>&1 || true

# O controle manda KEY_POWER; quem decide o que fazer com ela e o tv-remote,
# nao o logind -- sem isto, apertar POWER DESLIGA a box.
mkdir -p /etc/systemd/logind.conf.d
printf "[Login]\nHandlePowerKey=ignore\nHandlePowerKeyLongPress=ignore\n" \
  > /etc/systemd/logind.conf.d/90-ir-power.conf
systemctl restart systemd-logind

systemctl daemon-reload
systemctl enable --now tv-player tv-receiver tv-receiver-audio tv-web tv-remote

echo "pronto. Fixe o modo de video em /boot/armbianEnv.txt:"
echo '  extraargs=coherent_pool=2M cma=128M video=HDMI-A-1:1920x1080@60'
echo
echo "Pagina do celular: http://$(hostname -I | awk '{print $1}'):8080"
echo
echo "Controle infravermelho: teste e capture as teclas do SEU controle em"
echo "  systemctl start tv-remote-test   ->   http://$(hostname -I | awk '{print $1}'):8081"
echo
echo "Se o YouTube responder \"Sign in to confirm you're not a bot\", exporte os"
echo "cookies de uma conta descartavel (formato Netscape) para:"
echo "  /usr/local/etc/yt-cookies.txt   (chmod 600, dono root)"
