#!/bin/bash
# =====================================================================
#  amnezia-proto-amneziawg.sh
#  AmneziaWG 3.1 — основной VPN-протокол каскада. Устанавливается
#  deploy.sh вместе с туннелем; здесь только статус и обновление
#  пакетов до новой версии из PPA разработчиков.
#
#  Команды: status | update
# =====================================================================
set -euo pipefail

TITLE="AmneziaWG 3.1 (основной VPN)"
AWG_DIR="/etc/amnezia/amneziawg"

[[ $EUID -ne 0 ]] && { echo "Запускать от root (sudo $0)"; exit 1; }

do_status() {
    if [[ -f "$AWG_DIR/awg1.conf" ]]; then
        echo "installed=1"
        if systemctl is-active --quiet awg-quick@awg1; then echo "state=работает"; else echo "state=ОСТАНОВЛЕН"; fi
        echo "title=$TITLE"
        PORT=$(grep -oP '^ListenPort\s*=\s*\K\d+' "$AWG_DIR/awg1.conf" 2>/dev/null | head -1)
        echo "port=${PORT:-585}/udp"
        VER=$(dpkg-query -W -f='${Version}' amneziawg-tools 2>/dev/null || echo "?")
        echo "version=$VER"
        echo "clients=$(grep -c '^\[Peer\]' "$AWG_DIR/awg1.conf" 2>/dev/null || echo 0)"
    else
        echo "installed=0"
        echo "title=$TITLE"
        echo "error=основной протокол ставится deploy.sh"
    fi
}

case "${1:-status}" in
    status) do_status ;;
    update) exec /usr/local/bin/amnezia-update-awg.sh ;;
    *) echo "Использование: $0 status|update (установка — через deploy.sh)"; exit 1 ;;
esac
