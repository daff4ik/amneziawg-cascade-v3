#!/bin/bash
# =====================================================================
#  stealth-setup.sh — Stealth-режим веб-панели:
#  сайт-заглушка + Nginx + Certbot + секретный путь к панели.
#  Единая реализация (вызывается вручную или из deploy.sh --update).
#
#  Свой сайт: положите файлы в /var/www/stealth ДО запуска —
#  тогда заглушка по умолчанию создаваться не будет.
# =====================================================================
set -euo pipefail

PANEL_CONF="/etc/amnezia-panel.conf"
STEALTH_CONF="/etc/amnezia-stealth.conf"
WEB_ROOT="/var/www/stealth"

[[ $EUID -ne 0 ]] && { echo "Ошибка: запускать от root (sudo $0)"; exit 1; }
[[ -f "$PANEL_CONF" ]] || { echo "Ошибка: веб-панель не установлена (нет $PANEL_CONF)"; exit 1; }

echo "=== Настройка Stealth-режима веб-панели ==="
# Параметры можно передать через окружение (так делает deploy.sh):
#   STEALTH_DOMAIN, STEALTH_SECRET_PATH
DOMAIN="${STEALTH_DOMAIN:-}"
SECRET_PATH="${STEALTH_SECRET_PATH:-}"
[[ -z "$DOMAIN" ]] && read -p "Введите ваш домен или субдомен (например, mysite.com): " DOMAIN
[[ -z "$DOMAIN" ]] && { echo "Домен обязателен."; exit 1; }

[[ -z "$SECRET_PATH" ]] && read -p "Введите кодовое слово для скрытого пути (латиница/цифры, без слэшей): " SECRET_PATH
SECRET_PATH=$(echo "$SECRET_PATH" | tr -d '/' | sed 's/[^a-zA-Z0-9_-]//g')
[[ -z "$SECRET_PATH" ]] && { echo "Кодовое слово обязательно."; exit 1; }

# =====================================================================
# Сайт-заглушка (создаём только если пользователь не положил свой сайт)
# =====================================================================
if [[ ! -f "$WEB_ROOT/index.html" ]]; then
    echo "В $WEB_ROOT нет ваших файлов — создаю нейтральную заглушку «сайт в разработке»."
    echo "Замените её на свой сайт позже (просто загрузите файлы в $WEB_ROOT)."
    mkdir -p "$WEB_ROOT"
    cat > "$WEB_ROOT/index.html" <<'EOF'
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Сайт в разработке</title>
    <style>
        body { background: #000; color: #ccc; font-family: Arial, sans-serif; text-align: center; margin: 0; }
        p { margin-top: 45vh; font-size: 16px; }
    </style>
</head>
<body>
    <p>Сайт в разработке</p>
</body>
</html>
EOF
    chmod -R 755 "$WEB_ROOT"
else
    echo "Найден ваш сайт в $WEB_ROOT — использую его как заглушку."
fi

# =====================================================================
# Nginx + Certbot
# =====================================================================
# nginx работает от www-data: путь к корню сайта должен быть проходим для всех
chmod 755 /var/www
chmod -R 755 "$WEB_ROOT"

echo "Установка Nginx и Certbot..."
apt update -qq < /dev/null
apt install -y -qq nginx certbot python3-certbot-nginx < /dev/null

# Если сертификат уже выпущен внешним инструментом (например, acme.sh через
# tls-alpn-01 — когда хостер фильтрует порт 80 и certbot не работает),
# пишем SSL-конфиг сами и certbot не вызываем.
SSL_CERT="/etc/amnezia-panel-ssl/fullchain.pem"
SSL_KEY="/etc/amnezia-panel-ssl/privkey.pem"
HAVE_SSL=0
[[ -f "$SSL_CERT" && -f "$SSL_KEY" ]] && HAVE_SSL=1

if [[ $HAVE_SSL -eq 1 ]]; then
cat > /etc/nginx/sites-available/amnezia-stealth <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name $DOMAIN;

    ssl_certificate     $SSL_CERT;
    ssl_certificate_key $SSL_KEY;
    ssl_protocols TLSv1.2 TLSv1.3;

    root $WEB_ROOT;
    index index.html index.htm;

    location / {
        try_files \$uri \$uri/ =404;
    }

    # Строгий редирект для секретного пути (добавляет слэш на конце, если его нет)
    location = /$SECRET_PATH {
        return 301 /$SECRET_PATH/;
    }

    # Проксирование панели Amnezia
    location /$SECRET_PATH/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Поддержка WebSocket (на будущее)
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF
else
cat > /etc/nginx/sites-available/amnezia-stealth <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    root $WEB_ROOT;
    index index.html index.htm;

    location / {
        try_files \$uri \$uri/ =404;
    }

    # Строгий редирект для секретного пути (добавляет слэш на конце, если его нет)
    location = /$SECRET_PATH {
        return 301 /$SECRET_PATH/;
    }

    # Проксирование панели Amnezia
    location /$SECRET_PATH/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Поддержка WebSocket (на будущее)
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF
fi

ln -sf /etc/nginx/sites-available/amnezia-stealth /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx

if [[ $HAVE_SSL -eq 1 ]]; then
    echo "✅ Найден готовый SSL-сертификат (/etc/amnezia-panel-ssl) — HTTPS настроен."
else
echo "Получение SSL-сертификата Let's Encrypt для $DOMAIN..."
if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
        --register-unsafely-without-email --redirect; then
    echo "✅ SSL сертификат получен успешно!"
else
    echo "⚠️ Не удалось получить SSL. Убедитесь, что DNS-запись указывает на этот сервер."
    echo "⚠️ Если хостер фильтрует порт 80 (anti-DDoS), выпустите сертификат через acme.sh:"
    echo "   curl https://get.acme.sh | sh -s email=you@example.com"
    echo "   ~/.acme.sh/acme.sh --issue --alpn -d $DOMAIN --server letsencrypt \\"
    echo "     --pre-hook 'systemctl stop nginx' --post-hook 'systemctl start nginx'"
    echo "   ~/.acme.sh/acme.sh --install-cert -d $DOMAIN \\"
    echo "     --key-file /etc/amnezia-panel-ssl/privkey.pem \\"
    echo "     --fullchain-file /etc/amnezia-panel-ssl/fullchain.pem \\"
    echo "     --reloadcmd 'systemctl reload nginx'"
    echo "   и запустите этот скрипт ещё раз."
fi
fi

# =====================================================================
# Замыкание панели локально + сохранение параметров stealth
# =====================================================================
sed -i '/^host=/d; /^ssl=/d; /^base_url=/d' "$PANEL_CONF"
{
    echo "host=127.0.0.1"
    echo "ssl=false"
    echo "base_url=/$SECRET_PATH"
} >> "$PANEL_CONF"

cat > "$STEALTH_CONF" <<EOF
domain=$DOMAIN
secret_path=$SECRET_PATH
EOF
chmod 600 "$STEALTH_CONF"

systemctl restart amnezia-panel

echo "=============================================="
echo "✅ Stealth-режим успешно активирован!"
echo "=============================================="
echo "🌍 Заглушка: https://$DOMAIN"
echo "🔐 Панель:   https://$DOMAIN/$SECRET_PATH/"
echo "=============================================="
echo "Прямой доступ по IP заблокирован. Если понадобится вернуть"
echo "доступ по IP (например, при сбое домена), используйте скрипт:"
echo "sudo amnezia-panel-access.sh on"
