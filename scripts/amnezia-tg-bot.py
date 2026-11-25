#!/usr/bin/env python3
# =====================================================================
#  amnezia-tg-bot.py
#  Telegram-бот управления каскадом: кнопочное меню + Mini App
#  (веб-панель внутри Telegram). Long polling, только stdlib.
#  Настройки — /etc/amnezia-notify.conf:
#    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
#    TELEGRAM_BOT_ALLOWED — доп. chat_id через запятую (опционально)
# =====================================================================
import json
import os
import re
import ssl
import subprocess
import time
import urllib.parse
import urllib.request

CONF = "/etc/amnezia-notify.conf"
PANEL_CONF = "/etc/amnezia-panel.conf"
STEALTH_CONF = "/etc/amnezia-stealth.conf"
CLIENTS_DIR = "/root/clients"
LOG = "/var/log/amnezia-tg-bot.log"
API = "https://api.telegram.org/bot{token}/{method}"
PANEL_API = "https://127.0.0.1:8000"
MAX_MSG_AGE = 120
PENDING_TTL = 120

def load_conf(path):
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg

CFG = load_conf(CONF)
TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = set()
for cid in re.split(r"[,\s]+", CFG.get("TELEGRAM_CHAT_ID", "") + " " + CFG.get("TELEGRAM_BOT_ALLOWED", "")):
    cid = cid.strip().strip("\\").strip('"').strip("'").strip()
    if cid:
        ALLOWED.add(cid)

# URL панели для Mini App (если настроен stealth на домене)
_stealth = load_conf(STEALTH_CONF)
PANEL_URL = ""
if _stealth.get("domain") and _stealth.get("secret_path"):
    PANEL_URL = f"https://{_stealth['domain']}/{_stealth['secret_path']}/"

PENDING = {}  # chat_id -> (action, timestamp)

# --- Локальный API панели (заявки на регистрацию) ---
PANEL_TOKEN = load_conf(PANEL_CONF).get("api_token", "")
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

def panel_api(path, method="GET", body=None):
    """Вызов локального API панели с токеном. Возвращает (ok, dict)."""
    if not PANEL_TOKEN:
        return False, {"error": "нет api_token в /etc/amnezia-panel.conf"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(PANEL_API + path, data=data, method=method,
                                 headers={"X-Panel-Token": PANEL_TOKEN,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            return True, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode())
        except Exception:
            return False, {"error": str(e)}
    except Exception as e:
        return False, {"error": str(e)}

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

def kb(rows):
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)

def btn(text, cb):
    return {"text": text, "callback_data": cb}

def main_menu_kb(is_channel=False):
    rows = [
        [btn("📊 Статус", "status"), btn("👥 Клиенты", "clients")],
        [btn("➕ Добавить клиента", "add"), btn("➖ Удалить клиента", "del_menu")],
        [btn("🔄 Рестарт туннелей", "restart"), btn("💾 Бэкап", "backup")],
        [btn("👤 Заявки на регистрацию", "users"), btn("⚙️ Ещё", "more")],
    ]
    # web_app-кнопки Telegram запрещает в каналах — там сообщение целиком падает с 400
    if PANEL_URL and not is_channel:
        rows.insert(3, [{"text": "🖥 Открыть панель (Mini App)", "web_app": {"url": PANEL_URL}}])
    return kb(rows)

def back_kb():
    return kb([[btn("⬅️ Меню", "menu")]])

def send(chat_id, text, markup=None):
    if not text:
        text = "(пусто)"
    for i in range(0, len(text), 3900):
        params = {"chat_id": chat_id, "text": text[i:i + 3900],
                  "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if markup and i + 3900 >= len(text):
            params["reply_markup"] = markup
        try:
            api("sendMessage", **params)
        except Exception as e:
            log(f"sendMessage: {e}")

def show_menu(chat_id, is_channel=False):
    send(chat_id, "<b>⚙️ Управление VPN</b>\nВыберите действие:", main_menu_kb(is_channel))

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

def client_names():
    try:
        return sorted(f[:-5] for f in os.listdir(CLIENTS_DIR) if f.endswith(".conf"))
    except FileNotFoundError:
        return []

def server_name():
    # Имя сервера зашито deploy.sh в скрипт добавления клиентов
    try:
        with open("/usr/local/bin/amnezia-add-client.sh", encoding="utf-8") as f:
            m = re.search(r'^SERVER_NAME="([^"]+)"', f.read(), re.M)
            if m and m.group(1) and not m.group(1).startswith("__"):
                return m.group(1)
    except Exception:
        pass
    return "VPN"

HELP = """<b>Команды бота:</b> (или просто кнопки меню /menu)
/status — состояние туннеля
/clients — список клиентов
/add &lt;имя&gt; — создать клиента
/del &lt;имя&gt; — удалить клиента
/restart — рестарт туннелей awg0+awg1
/restart_panel — рестарт веб-панели
/backup — создать бэкап
/users — заявки на регистрацию (одобрить/отклонить)
/reboot — перезагрузка сервера (подтверждение: /reboot ДА)"""

# ==================== Действия ====================

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
    lines.append(f"<b>Клиентов</b>: {len(client_names())}")
    _, up = run(["uptime", "-p"], timeout=5)
    lines.append(f"<b>Uptime</b>: {esc(up.strip())}")
    return "\n".join(lines)

def cmd_clients():
    rc, out = run(["/usr/local/bin/amnezia-list-clients.sh"], timeout=60)
    return "<pre>" + esc(out[-3800:] or "нет данных") + "</pre>"

def do_add(chat_id, name):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name or ""):
        send(chat_id, "Имя: латиница/цифры/_/- до 32 символов. Попробуйте ещё раз или /menu для отмены.")
        PENDING[chat_id] = ("add", time.time())
        return
    PENDING.pop(chat_id, None)
    send(chat_id, f"Создаю клиента <code>{esc(name)}</code>...")
    rc, out = run(["/usr/local/bin/amnezia-add-client.sh", name], inp="y\n", timeout=240)
    conf = os.path.join(CLIENTS_DIR, f"{name}.conf")
    if rc != 0 or not os.path.exists(conf):
        send(chat_id, "❌ Ошибка создания:\n<pre>" + esc(out[-1500:]) + "</pre>", back_kb())
        return
    send(chat_id, f"✅ Клиент <code>{esc(name)}</code> создан. Конфиг ниже + vpn:// ссылка.", back_kb())
    try:
        send_doc(chat_id, conf, caption=f"{name}.conf — импорт в AmneziaVPN")
    except Exception as e:
        log(f"send_doc: {e}")
    rc2, link = run(["/usr/local/bin/amnezia-vpn-link.py", conf, server_name()], timeout=15)
    if rc2 == 0 and link.strip():
        send(chat_id, "🔗 <code>" + esc(link.strip()) + "</code>")

def do_del(chat_id, name):
    rc, out = run(["/usr/local/bin/amnezia-remove-client.sh", name], inp="y\n", timeout=120)
    if rc == 0:
        send(chat_id, f"🗑 Клиент <code>{esc(name)}</code> удалён", back_kb())
    else:
        send(chat_id, "❌ Ошибка удаления:\n<pre>" + esc(out[-1500:]) + "</pre>", back_kb())

def do_restart(chat_id):
    send(chat_id, "Перезапускаю awg0 и awg1... клиенты переподключатся сами (~10 сек)")
    run(["systemctl", "restart", "awg-quick@awg0", "awg-quick@awg1"], timeout=60)
    time.sleep(3)
    rc, _ = run(["ping", "-c", "2", "-W", "2", "10.10.10.1"], timeout=10)
    send(chat_id, "✅ Туннели перезапущены, ping: " + ("OK" if rc == 0 else "FAIL — смотрите 📊 Статус"), back_kb())

def do_backup(chat_id):
    send(chat_id, "Создаю бэкап...")
    rc, out = run(["/usr/local/bin/amnezia-backup.sh"], timeout=300)
    send(chat_id, ("✅ " if rc == 0 else "❌ ") + "<pre>" + esc(out[-1500:]) + "</pre>", back_kb())

def show_users(chat_id):
    ok, data = panel_api("/api/bot/pending")
    if not ok:
        send(chat_id, f"❌ Не удалось получить заявки: {esc(data.get('error', '?'))}", back_kb())
        return
    if not data:
        send(chat_id, "Заявок на регистрацию нет.", back_kb())
        return
    send(chat_id, f"<b>👤 Заявки на регистрацию ({len(data)}):</b>")
    for u in data[:20]:
        send(chat_id, f"Логин: <code>{esc(u['username'])}</code>\nEmail: <code>{esc(u['email'])}</code>",
             kb([[btn("✅ Одобрить", f"ureg:ok:{u['username']}"),
                  btn("❌ Отклонить", f"ureg:no:{u['username']}")]]))
    send(chat_id, "—", back_kb())

# ==================== Обработчики ====================

def handle_text(chat_id, text, is_channel=False):
    # Ответ на вопрос бота (например, имя нового клиента)
    pend = PENDING.get(chat_id)
    if pend and time.time() - pend[1] < PENDING_TTL and not text.startswith("/"):
        if pend[0] == "add":
            do_add(chat_id, text.strip())
            return
    PENDING.pop(chat_id, None)

    parts = text.split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    log(f"cmd={cmd} arg={arg} chat={chat_id}")
    if cmd in ("/start", "/menu"):
        show_menu(chat_id, is_channel)
    elif cmd == "/help":
        send(chat_id, HELP, back_kb())
    elif cmd == "/status":
        send(chat_id, cmd_status(), back_kb())
    elif cmd == "/clients":
        send(chat_id, cmd_clients(), back_kb())
    elif cmd == "/add":
        if arg:
            do_add(chat_id, arg)
        else:
            PENDING[chat_id] = ("add", time.time())
            send(chat_id, "Введите имя нового клиента (латиница/цифры/_/-):")
    elif cmd == "/del":
        if arg:
            do_del(chat_id, arg)
        else:
            handle_callback(chat_id, "0", "del_menu")
    elif cmd == "/restart":
        do_restart(chat_id)
    elif cmd == "/restart_panel":
        run(["systemctl", "restart", "amnezia-panel"], timeout=30)
        send(chat_id, "✅ Веб-панель перезапущена", back_kb())
    elif cmd == "/backup":
        do_backup(chat_id)
    elif cmd == "/users":
        show_users(chat_id)
    elif cmd == "/reboot":
        if arg == "ДА":
            send(chat_id, "⚠️ Перезагружаю сервер. Вернусь через пару минут.")
            run(["systemctl", "reboot"], timeout=10)
        else:
            send(chat_id, "⚠️ Это перезагрузит весь SERVER-A. Для подтверждения напишите: /reboot ДА", back_kb())
    else:
        send(chat_id, "Неизвестная команда. /menu — кнопки, /help — список команд")

def handle_callback(chat_id, qid, data, is_channel=False):
    log(f"cb={data} chat={chat_id}")
    if data == "menu":
        PENDING.pop(chat_id, None)
        show_menu(chat_id, is_channel)
    elif data == "more":
        send(chat_id, "<b>Дополнительно:</b>", kb([
            [btn("🖥 Рестарт панели", "restart_panel"), btn("⚠️ Перезагрузка сервера", "reboot")],
            [btn("❓ Список команд", "help"), btn("⬅️ Меню", "menu")],
        ]))
    elif data == "status":
        send(chat_id, cmd_status(), back_kb())
    elif data == "clients":
        send(chat_id, cmd_clients(), back_kb())
    elif data == "add":
        PENDING[chat_id] = ("add", time.time())
        send(chat_id, "Введите имя нового клиента (латиница/цифры/_/-):")
    elif data == "del_menu":
        names = client_names()
        if not names:
            send(chat_id, "Клиентов нет.", back_kb())
            return
        rows = [[btn(f"🗑 {n}", f"del_ask:{n}")] for n in names[:20]]
        rows.append([btn("⬅️ Меню", "menu")])
        send(chat_id, "<b>Кого удалить?</b>", kb(rows))
    elif data.startswith("del_ask:"):
        name = data.split(":", 1)[1]
        send(chat_id, f"Точно удалить клиента <code>{esc(name)}</code>?", kb([
            [btn("✅ Да, удалить", f"del_yes:{name}"), btn("❌ Отмена", "menu")],
        ]))
    elif data.startswith("del_yes:"):
        do_del(chat_id, data.split(":", 1)[1])
    elif data == "restart":
        do_restart(chat_id)
    elif data == "restart_panel":
        run(["systemctl", "restart", "amnezia-panel"], timeout=30)
        send(chat_id, "✅ Веб-панель перезапущена", back_kb())
    elif data == "backup":
        do_backup(chat_id)
    elif data == "users":
        show_users(chat_id)
    elif data.startswith("ureg:ok:") or data.startswith("ureg:no:"):
        _, verdict, username = data.split(":", 2)
        ok, res = panel_api("/api/bot/user-decision", "POST",
                            {"username": username,
                             "decision": "approve" if verdict == "ok" else "reject"})
        if ok:
            send(chat_id, res.get("text", "Готово"), back_kb())
        else:
            send(chat_id, f"⚠️ {esc(res.get('error', 'Ошибка'))}", back_kb())
    elif data == "reboot":
        send(chat_id, "⚠️ <b>Перезагрузить SERVER-A?</b>\nVPN отключится на пару минут.", kb([
            [btn("✅ Да, перезагрузить", "reboot_yes"), btn("❌ Отмена", "menu")],
        ]))
    elif data == "reboot_yes":
        send(chat_id, "⚠️ Перезагружаю сервер. Вернусь через пару минут.")
        run(["systemctl", "reboot"], timeout=10)
    elif data == "help":
        send(chat_id, HELP, back_kb())

def setup_bot():
    """Разовая настройка при старте: команды в меню '/' и кнопка Mini App."""
    try:
        commands = [
            {"command": "menu", "description": "⚙️ Главное меню"},
            {"command": "status", "description": "📊 Статус туннеля"},
            {"command": "clients", "description": "👥 Список клиентов"},
            {"command": "add", "description": "➕ Добавить клиента"},
            {"command": "users", "description": "👤 Заявки на регистрацию"},
            {"command": "help", "description": "❓ Справка"},
        ]
        api("setMyCommands", commands=json.dumps(commands))
    except Exception as e:
        log(f"setMyCommands: {e}")
    if PANEL_URL:
        for cid in ALLOWED:
            # Кнопка у поля ввода работает только в личных чатах (положительный id);
            # для групп/каналов API возвращает 400
            if not cid.isdigit():
                continue
            try:
                api("setChatMenuButton", chat_id=cid,
                    menu_button=json.dumps({"type": "web_app", "text": "🖥 Панель",
                                            "web_app": {"url": PANEL_URL}}))
            except Exception as e:
                log(f"setChatMenuButton {cid}: {e}")

def main():
    if not TOKEN or not ALLOWED:
        log("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в /etc/amnezia-notify.conf — бот не запущен")
        return
    log(f"Бот запущен (mini app: {PANEL_URL or 'нет'})")
    setup_bot()
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
            cb = upd.get("callback_query")
            if cb:
                chat = cb.get("message", {}).get("chat", {})
                chat_id = str(chat.get("id", ""))
                if chat_id not in ALLOWED:
                    log(f"IGNORED callback chat={chat_id} (добавьте в TELEGRAM_BOT_ALLOWED)")
                    continue
                try:
                    api("answerCallbackQuery", callback_query_id=cb["id"])
                    handle_callback(chat_id, cb["id"], cb.get("data", ""),
                                    chat.get("type") == "channel")
                except Exception as e:
                    log(f"callback: {e}")
                continue
            msg = upd.get("message") or upd.get("channel_post") or {}
            text = (msg.get("text") or "").strip()
            chat = msg.get("chat", {})
            chat_id = str(chat.get("id", ""))
            if not text:
                continue
            if chat_id not in ALLOWED:
                log(f"IGNORED chat={chat_id} text={text[:30]!r} (добавьте в TELEGRAM_BOT_ALLOWED)")
                continue
            if time.time() - msg.get("date", 0) > MAX_MSG_AGE:
                continue
            try:
                handle_text(chat_id, text, chat.get("type") == "channel")
            except Exception as e:
                log(f"handle_text: {e}")
                send(chat_id, f"❌ Внутренняя ошибка: {esc(str(e))}")

if __name__ == "__main__":
    main()
