#!/bin/bash
# =====================================================================
#  amnezia-update-awg.sh
#  Обновление пакетов AmneziaWG до свежей версии из PPA разработчиков
#  на SERVER-A и SERVER-B. После обновления перезапускает интерфейсы.
#  Клиенты и конфиги не затрагиваются.
# =====================================================================
set -euo pipefail

[[ $EUID -ne 0 ]] && { echo "Ошибка: запускать от root (sudo $0)"; exit 1; }

SERVER_B_IP=""; SERVER_B_PORT="22"; SERVER_B_USER="root"
[[ -f /etc/amnezia-cascade.env ]] && source /etc/amnezia-cascade.env

awg_pkgs() { dpkg-query -W -f='${Package}\n' 2>/dev/null | grep '^amneziawg' || true; }

echo "=== SERVER-A ==="
BEFORE=$(dpkg-query -W -f='${Version}' amneziawg-tools 2>/dev/null || echo "-")
apt update -qq < /dev/null
PKGS=$(awg_pkgs)
[[ -z "$PKGS" ]] && { echo "Пакеты AmneziaWG не найдены"; exit 1; }
# shellcheck disable=SC2086
apt install -y --only-upgrade $PKGS < /dev/null
AFTER=$(dpkg-query -W -f='${Version}' amneziawg-tools 2>/dev/null || echo "-")
if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "  уже свежая версия: $AFTER"
else
    echo "  обновлено: $BEFORE → $AFTER"
    systemctl restart awg-quick@awg0 awg-quick@awg1
    echo "  интерфейсы awg0/awg1 перезапущены"
fi

if [[ -n "${SERVER_B_IP:-}" && -f /root/.ssh/id_ed25519 ]]; then
    echo "=== SERVER-B ($SERVER_B_IP) ==="
    ssh -i /root/.ssh/id_ed25519 -o BatchMode=yes -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 -p "${SERVER_B_PORT:-22}" "${SERVER_B_USER:-root}@$SERVER_B_IP" \
        "apt update -qq < /dev/null \
         && apt install -y --only-upgrade \$(dpkg-query -W -f='\${Package}\n' | grep '^amneziawg') < /dev/null \
         && systemctl restart awg-quick@awg0 \
         && dpkg-query -W -f='  версия на SERVER-B: \${Version}\n' amneziawg-tools" \
        || echo "  [WARN] не удалось обновить SERVER-B — выполните там вручную: apt install --only-upgrade amneziawg amneziawg-tools"
fi

echo "Готово."
