#!/bin/bash
# =====================================================================
#  amnezia-proto-telemt.sh
#  Telemt — MTProxy для Telegram на Rust с эмуляцией TLS (маскировка
#  под HTTPS выбранного домена). Ссылка tg://proxy открывается
#  прямо в Telegram. Используется официальный установщик telemt.
#
#  Команды: install | remove | restart | status
#  Переменные окружения (опционально):
#    TELEMT_PORT (по умолчанию 8445), TELEMT_DOMAIN (маскировочный домен),
#    ENDPOINT_HOST (адрес для ссылки, иначе — внешний IP сервера)
# =====================================================================
set -euo pipefail

NAME="telemt"
TITLE="Telemt (Telegram-прокси, TLS)"
STATE_DIR="/etc/amnezia-protocols"
STATE_FILE="$STATE_DIR/$NAME.conf"
INSTALLER="/tmp/telemt-install.sh"

MIRRORS=(
    "https://raw.githubusercontent.com/telemt/telemt/master"
    "https://ghproxy.net/https://raw.githubusercontent.com/telemt/telemt/master"
    "https://mirror.ghproxy.com/https://raw.githubusercontent.com/telemt/telemt/master"
)

ok()   { echo -e "\033[1;32m[OK]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERR]\033[0m $*" >&2; }

[[ $EUID -ne 0 ]] && { err "Запускать от root (sudo $0)"; exit 1; }

endpoint_host() {
    if [[ -n "${ENDPOINT_HOST:-}" ]]; then echo "$ENDPOINT_HOST"; return; fi
    [[ -f /etc/amnezia-cascade.env ]] && source /etc/amnezia-cascade.env
    if [[ -n "${ENDPOINT_HOST:-}" ]]; then echo "$ENDPOINT_HOST"; return; fi
    curl -fsS -m 10 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}'
}

fetch_installer() {
    for m in "${MIRRORS[@]}"; do
        curl -fsSL --connect-timeout 5 --max-time 30 "$m/install.sh" -o "$INSTALLER" && return 0
    done
    err "Не удалось скачать установщик telemt"; exit 1
}

do_install() {
    PORT="${TELEMT_PORT:-8445}"
    DOMAIN="${TELEMT_DOMAIN:-www.microsoft.com}"

    if [[ -f "$STATE_FILE" ]]; then
        err "Уже установлен. Сначала удалите: $0 remove"; exit 1
    fi
    if ss -ltn | awk '{print $4}' | grep -qE "[:.]${PORT}\$"; then
        err "Порт $PORT занят. Выберите другой: TELEMT_PORT=9443 $0 install"; exit 1
    fi

    fetch_installer
    SECRET=$(openssl rand -hex 16)
    echo "Установка telemt (порт $PORT, маскировка под $DOMAIN)..."
    sh "$INSTALLER" install -l ru -d "$DOMAIN" -p "$PORT" -s "$SECRET" < /dev/null \
        || { err "Установщик telemt завершился с ошибкой"; exit 1; }
    sleep 1
    systemctl is-active --quiet telemt || { err "telemt не запустился: journalctl -u telemt"; exit 1; }

    # Клиентский секрет FakeTLS: ee + 32hex + hex(домен)
    HEX_DOMAIN=$(printf '%s' "$DOMAIN" | xxd -p | tr -d '\n')
    HOST=$(endpoint_host)
    LINK="tg://proxy?server=$HOST&port=$PORT&secret=ee${SECRET}${HEX_DOMAIN}"

    mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
    cat > "$STATE_FILE" <<EOF
TITLE="$TITLE"
PORT="$PORT"
DOMAIN="$DOMAIN"
HOST="$HOST"
CLIENT_LINK="$LINK"
EOF
    chmod 600 "$STATE_FILE"

    ok "$TITLE установлен (порт $PORT)"
    echo ""
    echo "Ссылка для Telegram (открыть на устройстве):"
    echo "$LINK"
    echo "https://t.me/proxy?server=$HOST&port=$PORT&secret=ee${SECRET}${HEX_DOMAIN}"
    command -v qrencode >/dev/null 2>&1 && { echo ""; qrencode -t ansiutf8 "$LINK"; }
}

do_remove() {
    if fetch_installer; then
        sh "$INSTALLER" purge < /dev/null || true
    fi
    # На случай недоступности установщика — ручная зачистка
    systemctl disable --now telemt 2>/dev/null || true
    rm -f /etc/systemd/system/telemt.service "$STATE_FILE"
    systemctl daemon-reload
    ok "$TITLE удалён"
}

do_restart() {
    [[ -f "$STATE_FILE" ]] || { err "$TITLE не установлен"; exit 1; }
    systemctl restart telemt
    sleep 1
    systemctl is-active --quiet telemt || { err "telemt не запустился: journalctl -u telemt"; exit 1; }
    ok "$TITLE перезапущен"
}

do_status() {
    if [[ -f "$STATE_FILE" ]]; then
        source "$STATE_FILE"
        if systemctl is-active --quiet telemt; then STATE="работает"; else STATE="ОСТАНОВЛЕН"; fi
        echo "installed=1"
        echo "state=$STATE"
        echo "title=$TITLE"
        echo "port=$PORT"
        echo "domain=$DOMAIN"
        echo "link=$CLIENT_LINK"
    else
        echo "installed=0"
        echo "title=$TITLE"
    fi
}

case "${1:-status}" in
    install) do_install ;;
    remove)  do_remove ;;
    restart) do_restart ;;
    status)  do_status ;;
    *) echo "Использование: $0 install|remove|restart|status"; exit 1 ;;
esac
