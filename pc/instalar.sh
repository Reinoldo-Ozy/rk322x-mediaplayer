#!/bin/bash
# Instala o painel no menu de aplicativos, com o caminho correto desta copia.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
sed "s|@EXEC@|python3 $DIR/tv-cast-gui.py|" "$DIR/tv-cast.desktop.in" \
  > "$APPS/tv-cast.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
echo "instalado: $APPS/tv-cast.desktop"
echo
echo "Se o box nao estiver em 192.168.10.159, defina o endereco:"
echo "  mkdir -p ~/.config && echo 'TV_BOX_HOST=SEU.IP.AQUI' >> ~/.config/tv-cast.conf"
