#!/usr/bin/env python3
# =====================================================================
#  amnezia-tg-bot.py
#  Telegram-бот управления каскадом: команды из личного или группового
#  чата. Long polling, внешних зависимостей нет (только stdlib).
#  Токен и разрешённые чаты берёт из /etc/amnezia-notify.conf:
#    TELEGRAM_BOT_TOKEN — токен бота (обязательно)
#    TELEGRAM_CHAT_ID   — основной чат (обязательно)
#    TELEGRAM_BOT_ALLOWED — доп. chat_id через запятую (опционально)
# =====================================================================
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

CONF = "/etc/amnezia-notify.conf"
CLIENTS_DIR = "/root/clients"
LOG = "/var/log/amnezia-tg-bot.log"
API = "https://api.telegram.org/bot{token}/{method}"
MAX_MSG_AGE = 120  # команды старше 2 минут (накопились офлайн) игнорируем

def load_conf():
    cfg = {}
    if os.path.exists(CONF):
        with open(CONF, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg

CFG = load_conf()
TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = set()
for cid in re.split(r"[,\s]+", CFG.get("TELEGRAM_CHAT_ID", "") + " " + CFG.get("TELEGRAM_BOT_ALLOWED", "")):
    if cid.strip():
        ALLOWED.add(cid.strip())

def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API.format(token=TOKEN, method=method), data=data)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())

def send(chat_id, text):
    if not text:
        text = "(пусто)"
    for i in range(0, len(text), 3900):
        try:
            api("sendMessage", chat_id=chat_id, text=text[i:i + 3900],
                parse_mode="HTML", disable_web_page_preview="true")
        except Exception as e:
            log(f"sendMessage: {e}")

def send_doc(chat_id, path, caption=""):
    boundary = "----amneziabot"
    with open(path, "rb") as f:
        content = f.read()
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n').encode()
    if caption:
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n').encode()
    body += (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="document"; filename="{os.path.basename(path)}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode()
    body += content + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(API.format(token=TOKEN, method="sendDocument"), data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()

def run(cmd, inp=None, timeout=180):
    try:
        r = subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)

HELP = """<b>Команды бота:</b>
/status — состояние туннеля и сервисов
/clients — список клиентов и трафик
/add &lt;имя&gt; — создать клиента (конфиг придёт файлом + vpn:// ссылкой)
/del &lt;имя&gt; — удалить клиента
/restart — перезапуск туннелей awg0+awg1 (клиенты отвалятся на ~10 сек)
/restart_panel — перезапуск веб-панели
/backup — создать бэкап сейчас
/reboot — перезагрузка сервера (подтверждение: /reboot ДА)
/help — эта справка"""

def cmd_status():
    lines = []
    for iface in ("awg0", "awg1"):
        _, svc = run(["systemctl", "is-active", f"awg-quick@{iface}"])
        lines.append(f"<b>{iface}</b>: {esc(svc.strip() or '?')}")
    rc, _ = run(["ping", "-c", "2", "-W", "2", "10.10.10.1"], timeout=10)
    lines.append(f"<b>Туннель до SERVER-B</b>: {'OK' if rc == 0 else 'FAIL'}")
    _, hs = run(["awg", "show", "awg0", "latest-handshakes"], timeout=10)
    try:
        age = int(time.time()) - int(hs.split()[1])
        lines.append(f"<b>Handshake awg0</b>: {age} сек назад")
    except (IndexError, ValueError):
        lines.append("<b>Handshake awg0</b>: нет")
    try:
        n = len([f for f in os.listdir(CLIENTS_DIR) if f.endswith(".conf")])
    except FileNotFoundError:
        n = 0
    lines.append(f"<b>Клиентов</b>: {n}")
    _, up = run(["uptime", "-p"], timeout=5)
    lines.append(f"<b>Uptime</b>: {esc(up.strip())}")
    return "\n".join(lines)

def cmd_clients():
    rc, out = run(["/usr/local/bin/amnezia-list-clients.sh"], timeout=60)
    return "<pre>" + esc(out[-3800:] or "нет данных") + "</pre>"

def cmd_add(chat_id, name):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name or ""):
        send(chat_id, "Имя: латиница/цифры/_/- до 32 символов. Пример: /add phone")
        return
    send(chat_id, f"Создаю клиента <code>{esc(name)}</code>...")
    rc, out = run(["/usr/local/bin/amnezia-add-client.sh", name], inp="y\n", timeout=240)
    conf = os.path.join(CLIENTS_DIR, f"{name}.conf")
    if rc != 0 or not os.path.exists(conf):
        send(chat_id, "❌ Ошибка создания:\n<pre>" + esc(out[-1500:]) + "</pre>")
        return
    send(chat_id, f"✅ Клиент <code>{esc(name)}</code> создан. Конфиг ниже + vpn:// ссылка.")
    try:
        send_doc(chat_id, conf, caption=f"{name}.conf — импорт в AmneziaVPN")
    except Exception as e:
        log(f"send_doc: {e}")
    rc2, link = run(["/usr/local/bin/amnezia-vpn-link.py", conf, "VPN"], timeout=15)
    if rc2 == 0 and link.strip():
        send(chat_id, "🔗 <code>" + esc(link.strip()) + "</code>")

def cmd_del(chat_id, name):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name or ""):
        send(chat_id, "Пример: /del phone")
        return
    rc, out = run(["/usr/local/bin/amnezia-remove-client.sh", name], inp="y\n", timeout=120)
    if rc == 0:
        send(chat_id, f"🗑 Клиент <code>{esc(name)}</code> удалён")
    else:
        send(chat_id, "❌ Ошибка удаления:\n<pre>" + esc(out[-1500:]) + "</pre>")

def cmd_restart(chat_id):
    send(chat_id, "Перезапускаю awg0 и awg1... клиенты переподключатся сами (~10 сек)")
    rc, out = run(["systemctl", "restart", "awg-quick@awg0", "awg-quick@awg1"], timeout=60)
    time.sleep(3)
    rc2, _ = run(["ping", "-c", "2", "-W", "2", "10.10.10.1"], timeout=10)
    send(chat_id, "✅ Туннели перезапущены, ping: " + ("OK" if rc2 == 0 else "FAIL — смотрите /status"))

def cmd_backup(chat_id):
    send(chat_id, "Создаю бэкап...")
    rc, out = run(["/usr/local/bin/amnezia-backup.sh"], timeout=300)
    send(chat_id, ("✅ " if rc == 0 else "❌ ") + "<pre>" + esc(out[-1500:]) + "</pre>")

def handle(chat_id, text):
    parts = text.split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    log(f"cmd={cmd} arg={arg} chat={chat_id}")
    if cmd in ("/start", "/help"):
        send(chat_id, HELP)
    elif cmd == "/status":
        send(chat_id, cmd_status())
    elif cmd == "/clients":
        send(chat_id, cmd_clients())
    elif cmd == "/add":
        cmd_add(chat_id, arg)
    elif cmd == "/del":
        cmd_del(chat_id, arg)
    elif cmd == "/restart":
        cmd_restart(chat_id)
    elif cmd == "/restart_panel":
        run(["systemctl", "restart", "amnezia-panel"], timeout=30)
        send(chat_id, "✅ Веб-панель перезапущена")
    elif cmd == "/backup":
        cmd_backup(chat_id)
    elif cmd == "/reboot":
        if arg == "ДА":
            send(chat_id, "⚠️ Перезагружаю сервер. Вернусь через пару минут.")
            run(["systemctl", "reboot"], timeout=10)
        else:
            send(chat_id, "⚠️ Это перезагрузит весь SERVER-A. Для подтверждения напишите: /reboot ДА")
    else:
        send(chat_id, "Неизвестная команда. /help — список команд")

def main():
    if not TOKEN or not ALLOWED:
        log("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в /etc/amnezia-notify.conf — бот не запущен")
        return
    log("Бот запущен")
    offset = 0
    while True:
        try:
            resp = api("getUpdates", offset=offset, timeout=30)
        except Exception as e:
            log(f"getUpdates: {e}")
            time.sleep(5)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("channel_post") or {}
            text = (msg.get("text") or "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if not text or chat_id not in ALLOWED:
                continue
            if time.time() - msg.get("date", 0) > MAX_MSG_AGE:
                continue
            try:
                handle(chat_id, text)
            except Exception as e:
                log(f"handle: {e}")
                send(chat_id, f"❌ Внутренняя ошибка: {esc(str(e))}")

if __name__ == "__main__":
    main()
