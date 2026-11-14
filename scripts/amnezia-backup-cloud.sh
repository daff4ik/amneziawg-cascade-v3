#!/bin/bash
# =====================================================================
#  amnezia-backup-cloud.sh
#  Настройка выгрузки бэкапов в облако через rclone
#  (Google Drive, S3, Яндекс.Диск и др.). После настройки каждый
#  бэкап дополнительно копируется в облако (amnezia-backup.sh).
# =====================================================================
set -euo pipefail

CONF="/etc/amnezia-backup-cloud.conf"

[[ $EUID -ne 0 ]] && { echo "Ошибка: запускать от root (sudo $0)"; exit 1; }

if [[ "${1:-}" == "off" ]]; then
    rm -f "$CONF"
    echo "Выгрузка в облако отключена (локальные бэкапы продолжают работать)"
    exit 0
fi

command -v rclone >/dev/null || { apt update -qq < /dev/null && apt install -y -qq rclone < /dev/null; }

echo "================================================================"
echo " Настройка rclone для облачных бэкапов"
echo "================================================================"
echo " Для Google Drive в мастере выберите:"
echo "   n (New remote) → имя: gdrive → тип: drive"
echo "   client_id/secret — Enter (по умолчанию)"
echo "   scope: 1 (полный доступ)"
echo "   На вопрос 'auto config' ответьте n (сервер без браузера),"
echo "   затем на своём компьютере (например, в MobaXterm/Windows)"
echo "   выполните команду, которую покажет rclone:"
echo "     rclone authorize \"drive\""
echo "   (rclone для Windows: https://rclone.org/downloads/)"
echo "   и вставьте полученный код обратно на сервере."
echo "================================================================"
rclone config

read -p "Имя настроенного remote (например gdrive), пусто — отмена: " R
[[ -z "$R" ]] && { echo "Отменено"; exit 0; }
rclone lsd "$R:" >/dev/null 2>&1 || { echo "Ошибка: remote '$R' не работает"; exit 1; }

echo "RCLONE_REMOTE=\"$R\"" > "$CONF"
chmod 600 "$CONF"
rclone mkdir "$R:amnezia-backups" >/dev/null 2>&1 || true
echo ""
echo "Готово: бэкапы будут выгружаться в $R:amnezia-backups"
echo "Проверить сейчас: /usr/local/bin/amnezia-backup.sh"
echo "Отключить: $0 off"
