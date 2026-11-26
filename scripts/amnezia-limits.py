#!/usr/bin/env python3
# =====================================================================
#  amnezia-limits.py
#  Лимиты скорости и дневного трафика для клиентов awg1.
#  Лимиты хранятся в /var/lib/amnezia-panel/users.db (таблица limits)
#  и задаются из веб-панели: на пользователя целиком или на устройство.
#
#  Команды:
#    apply   — применить все лимиты через tc (download: htb на egress,
#              upload: полисер на ingress интерфейса awg1)
#    monitor — проверить дневные квоты: урезать скорость при превышении,
#              восстановить на новый день (запускать по cron каждую минуту)
#    clear   — снять все tc-правила с awg1
# =====================================================================
import os
import re
import sqlite3
import subprocess
import sys
import time

USERS_DB = "/var/lib/amnezia-panel/users.db"
TRAFFIC_DB = "/var/lib/amnezia-panel/traffic.db"
AWG_IFACE = "awg1"
AWG_CONF = f"/etc/amnezia/amneziawg/{AWG_IFACE}.conf"

CREATE_SQL = """CREATE TABLE IF NOT EXISTS limits(
    scope TEXT, target TEXT,
    max_kbps INT DEFAULT 0,
    quota_mb INT DEFAULT 0,
    throttle_kbps INT DEFAULT 0,
    baseline_bytes INT DEFAULT 0,
    baseline_day TEXT DEFAULT '',
    throttled INT DEFAULT 0,
    PRIMARY KEY(scope, target))"""

def run(cmd):
    subprocess.run(cmd, shell=True, capture_output=True)

def peers():
    """Имя клиента -> (ip, pubkey) из конфига awg1."""
    out = {}
    if not os.path.exists(AWG_CONF):
        return out
    with open(AWG_CONF, encoding="utf-8") as f:
        content = f.read()
    for block in re.split(r"\n(?=\[Peer\])", content):
        nm = re.search(r"#\s*Client:\s*(\S+)", block)
        pk = re.search(r"PublicKey\s*=\s*(\S+)", block)
        ip = re.search(r"AllowedIPs\s*=\s*([0-9.]+)", block)
        if nm and pk and ip:
            out[nm.group(1)] = (ip.group(1), pk.group(1))
    return out

def load_limits():
    if not os.path.exists(USERS_DB):
        return []
    conn = sqlite3.connect(USERS_DB)
    conn.execute(CREATE_SQL)
    rows = conn.execute(
        "SELECT scope,target,max_kbps,quota_mb,throttle_kbps,"
        "baseline_bytes,baseline_day,throttled FROM limits").fetchall()
    conn.close()
    return rows

def devices_of(scope, target):
    """Имена конфигов цели: device — сам конфиг; user — все его устройства."""
    if scope == "device":
        return [target]
    if not os.path.exists(USERS_DB):
        return []
    conn = sqlite3.connect(USERS_DB)
    rows = conn.execute("SELECT client_name FROM devices WHERE owner=?", (target,)).fetchall()
    conn.close()
    return [r[0] for r in rows]

def traffic_bytes(pubkeys):
    if not pubkeys or not os.path.exists(TRAFFIC_DB):
        return 0
    conn = sqlite3.connect(TRAFFIC_DB)
    q = ("SELECT COALESCE(SUM(total_rx+total_tx),0) FROM client_stats "
         "WHERE pubkey IN (%s)" % ",".join("?" * len(pubkeys)))
    val = conn.execute(q, pubkeys).fetchone()[0]
    conn.close()
    return val

def tc_clear():
    run(f"tc qdisc del dev {AWG_IFACE} root 2>/dev/null")
    run(f"tc qdisc del dev {AWG_IFACE} ingress 2>/dev/null")

def tc_init():
    run(f"tc qdisc add dev {AWG_IFACE} root handle 1: htb default 0")
    run(f"tc class add dev {AWG_IFACE} parent 1: classid 1:1 htb rate 10000mbit")
    run(f"tc qdisc add dev {AWG_IFACE} handle ffff: ingress")

def tc_limit_ip(ip, kbps, classid):
    # download (сервер -> клиент): класс htb + фильтр по адресу назначения
    run(f"tc class add dev {AWG_IFACE} parent 1:1 classid 1:{classid} htb rate {kbps}kbit")
    run(f"tc filter add dev {AWG_IFACE} parent 1: protocol ip u32 "
        f"match ip dst {ip}/32 classid 1:{classid}")
    # upload (клиент -> сервер): полисер на входе (лишние пакеты отбрасываются,
    # TCP сам снижает скорость)
    run(f"tc filter add dev {AWG_IFACE} parent ffff: protocol ip u32 "
        f"match ip src {ip}/32 police rate {kbps}kbit burst 64k drop")

def apply_limits():
    tc_clear()
    limits = load_limits()
    if not limits:
        return
    tc_init()
    prs = peers()
    cid = 10
    for scope, target, max_kbps, _quota, throttle_kbps, _b, _d, throttled in limits:
        eff = throttle_kbps if (throttled and throttle_kbps) else max_kbps
        if not eff:
            continue
        for name in devices_of(scope, target):
            ip = prs.get(name, ("", ""))[0]
            if ip:
                cid += 1
                tc_limit_ip(ip, eff, cid)

def monitor():
    today = time.strftime("%Y-%m-%d")
    limits = load_limits()
    if not limits:
        return
    prs = peers()
    conn = sqlite3.connect(USERS_DB)
    conn.execute(CREATE_SQL)
    changed = False
    for scope, target, _max, quota_mb, throttle_kbps, baseline, bday, throttled in limits:
        if not quota_mb:
            continue
        names = devices_of(scope, target)
        pubs = [prs[n][1] for n in names if n in prs]
        cur = traffic_bytes(pubs)
        if bday != today:
            # Новый день: сброс счётчика и снятие урезания
            conn.execute("UPDATE limits SET baseline_bytes=?, baseline_day=?, throttled=0 "
                         "WHERE scope=? AND target=?", (cur, today, scope, target))
            if throttled:
                changed = True
            continue
        if cur - baseline >= quota_mb * 1024 * 1024 and not throttled and throttle_kbps:
            conn.execute("UPDATE limits SET throttled=1 WHERE scope=? AND target=?",
                         (scope, target))
            changed = True
    conn.commit()
    conn.close()
    if changed:
        apply_limits()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "apply"
    if cmd == "apply":
        apply_limits()
    elif cmd == "monitor":
        monitor()
    elif cmd == "clear":
        tc_clear()
    else:
        print("Использование: amnezia-limits.py apply|monitor|clear")
        sys.exit(1)
