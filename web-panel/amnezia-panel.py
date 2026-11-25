#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AmneziaWG Web Panel — Optimized, Mobile-First, Secure"""
import asyncio
import os
import re
import hmac
import hashlib
import shlex
import smtplib
import secrets
import sqlite3
import subprocess
import time
import urllib.request
import urllib.parse
import json
import datetime
from email.message import EmailMessage
from fastapi import FastAPI, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
import uvicorn

CONFIG_FILE   = "/etc/amnezia-panel.conf"
AWG_IFACE     = "awg1"
AWG_CONF      = f"/etc/amnezia/amneziawg/{AWG_IFACE}.conf"
AWG0_CONF     = "/etc/amnezia/amneziawg/awg0.conf"
CLIENTS_DIR   = "/root/clients"
ADD_SCRIPT    = "/usr/local/bin/amnezia-add-client.sh"
REMOVE_SCRIPT = "/usr/local/bin/amnezia-remove-client.sh"
NOTIFY_SCRIPT = "/usr/local/bin/amnezia-notify.sh"
NOTIFY_CONF   = "/etc/amnezia-notify.conf"
TRAFFIC_DB    = "/var/lib/amnezia-panel/traffic.db"
SERVERS_FILE  = "/var/lib/amnezia-panel/servers.json"
SERVER_LABEL  = "__SERVER_LABEL__"
COMMIT_STATE_FILE = "/var/lib/amnezia-panel/installed_commit"
REPO_API_URL      = "https://api.github.com/repos/daff4ik/amneziawg-cascade-v3/commits"

SESSION_COOKIE = "amnezia_session"
USER_COOKIE    = "amnezia_user_session"
SESSION_DAYS   = 7

CASCADE_ENV = "/etc/amnezia-cascade.env"
AUTH_LOG    = "/var/log/amnezia-panel-auth.log"

# ================ Пользователи-абоненты (личные кабинеты) ================
USERS_DB   = "/var/lib/amnezia-panel/users.db"
MAIL_CONF  = "/etc/amnezia-mail.conf"
STEALTH_CONF = "/etc/amnezia-stealth.conf"
MAX_DEVICES_PER_USER = 10

def users_db():
    os.makedirs(os.path.dirname(USERS_DB), exist_ok=True)
    conn = sqlite3.connect(USERS_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        pass_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_ts INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS devices(
        client_name TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        label TEXT NOT NULL,
        created_ts INTEGER NOT NULL)""")
    return conn

def hash_password(p: str) -> str:
    salt = secrets.token_hex(8)
    return f"sha256:{salt}:{hashlib.sha256((salt + p).encode()).hexdigest()}"

def get_api_token() -> str:
    """Токен для локальных вызовов API (бот -> панель). Автогенерация при первом обращении."""
    tok = get_config_val("api_token", "")
    if tok:
        return tok
    tok = secrets.token_hex(24)
    try:
        with open(CONFIG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\napi_token={tok}\n")
    except Exception:
        pass
    return tok

def get_user(username: str):
    conn = users_db()
    row = conn.execute("SELECT username, email, pass_hash, status, created_ts FROM users WHERE username=?",
                       (username,)).fetchone()
    conn.close()
    if not row:
        return None
    return {"username": row[0], "email": row[1], "pass_hash": row[2], "status": row[3], "created_ts": row[4]}

def make_user_token(username: str, ts: int) -> str:
    sig = hmac.new(get_api_token().encode(), f"user-session:{username}:{ts}".encode(), hashlib.sha256).hexdigest()
    return f"{username}.{ts}.{sig}"

def check_user_token(token: str) -> str:
    """Возвращает username одобренного пользователя или ''."""
    try:
        username, ts_str, sig = token.rsplit(".", 2)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return ""
    if not 0 <= time.time() - ts <= SESSION_DAYS * 86400:
        return ""
    expected = hmac.new(get_api_token().encode(), f"user-session:{username}:{ts}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return ""
    u = get_user(username)
    return username if u and u["status"] == "approved" else ""

class RedirectToUserLogin(Exception):
    pass

def current_client(request: Request) -> str:
    username = check_user_token(request.cookies.get(USER_COOKIE, ""))
    if not username:
        if request.url.path.startswith("/api/"):
            raise HTTPException(401, "Unauthorized")
        raise RedirectToUserLogin()
    return username

# ================ Почтовые уведомления абонентам (SMTP) ================
def load_mail_conf():
    cfg = {}
    if os.path.exists(MAIL_CONF):
        try:
            with open(MAIL_CONF, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        cfg[k.strip()] = v.strip().strip('"')
        except Exception:
            pass
    return cfg

def send_mail(to_addr: str, subject: str, body: str) -> tuple:
    cfg = load_mail_conf()
    host, port = cfg.get("SMTP_HOST", ""), int(cfg.get("SMTP_PORT", "465") or 465)
    user, pwd = cfg.get("SMTP_USER", ""), cfg.get("SMTP_PASS", "")
    from_addr = cfg.get("SMTP_FROM", "") or user
    if not (host and from_addr):
        return False, "SMTP не настроен (вкладка «Пользователи» → Почтовые уведомления)"
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                if user:
                    s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                s.starttls()
                if user:
                    s.login(user, pwd)
                s.send_message(msg)
        return True, "OK"
    except Exception as e:
        return False, str(e)[:300]

def public_base_url() -> str:
    """Публичный URL панели для ссылок в письмах (stealth-домен или IP:8000)."""
    if os.path.exists(STEALTH_CONF):
        try:
            cfg = {}
            with open(STEALTH_CONF, encoding="utf-8") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        cfg[k.strip()] = v.strip().strip('"').strip("'")
            if cfg.get("domain") and cfg.get("secret_path"):
                return f"https://{cfg['domain']}/{cfg['secret_path']}"
        except Exception:
            pass
    host = CAS.get("ENDPOINT_HOST", "") or "IP-СЕРВЕРА"
    return f"https://{host}:8000"


# ================ Общие константы каскада (пишет deploy.sh) ================
def load_cascade_env():
    env = {
        "INSTALL_MODE": "3",
        "AWG_TUNNEL_PORT": "8443",
        "TUNNEL_A_IP": "10.10.10.2",
        "TUNNEL_B_IP": "10.10.10.1",
        "TUNNEL_A_IP6": "fd00:10:10:10::2",
        "TUNNEL_B_IP6": "fd00:10:10:10::1",
        "CLIENT_SUBNET": "10.8.0",
        "CLIENT_SUBNET6": "fd00:10:8",
        "MTU_AWG0": "1180",
        "MSS_CLAMP": "1000",
        "IPV6_ENABLED": "0",
        "SERVER_B_IP": "",
        "SERVER_B_PORT": "22",
        "SERVER_B_USER": "root",
    }
    if os.path.exists(CASCADE_ENV):
        try:
            with open(CASCADE_ENV, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return env

CAS = load_cascade_env()
IPV6_ENABLED = CAS["IPV6_ENABLED"] == "1"

# Режим установки: 1 = только SERVER-A, 2 = только SERVER-B (управление отсюда), 3 = каскад
INSTALL_MODE = CAS["INSTALL_MODE"]
REMOTE_MODE = INSTALL_MODE == "2"
SERVER_B_IP   = CAS["SERVER_B_IP"]
SERVER_B_PORT = CAS["SERVER_B_PORT"]
SERVER_B_USER = CAS["SERVER_B_USER"]
SSH_KEY = "/root/.ssh/id_ed25519"

MODE_LABELS = {"0": "Конструктор (только панель)", "1": "Только SERVER-A", "2": "Только SERVER-B (управление с A)", "3": "Каскад A+B"}
MODE_LABEL = MODE_LABELS.get(INSTALL_MODE, INSTALL_MODE)

# ================ Конфигурация запуска ================
def get_config_val(key, default):
    if not os.path.exists(CONFIG_FILE): return default
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(f"{key}="):
                return line.strip().split("=", 1)[1]
    return default

BASE_URL = get_config_val("base_url", "")
BIND_HOST = get_config_val("host", "0.0.0.0")
USE_SSL = get_config_val("ssl", "true").lower() == "true"

# ================ Auth ================
def load_credentials():
    """Возвращает (username, password_plain, password_hash).
    password_hash имеет формат sha256:salt:hexdigest (приоритет над plaintext)."""
    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError("no config")
    cfg = {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg.get("username", "admin"), cfg.get("password", ""), cfg.get("password_hash", "")

def verify_password(p: str, password_plain: str, password_hash: str) -> bool:
    if password_hash:
        try:
            algo, salt, hexd = password_hash.split(":", 2)
        except ValueError:
            return False
        if algo != "sha256":
            return False
        digest = hashlib.sha256((salt + p).encode()).hexdigest()
        return hmac.compare_digest(digest, hexd)
    return hmac.compare_digest(p, password_plain)

def make_token(secret: str, ts: int) -> str:
    sig = hmac.new(secret.encode(), f"panel-session:{ts}".encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"

def check_token(token: str, secret: str) -> bool:
    try:
        ts_str, sig = token.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return False
    if not 0 <= time.time() - ts <= SESSION_DAYS * 86400:
        return False
    expected = hmac.new(secret.encode(), f"panel-session:{ts}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)

# --- Антибрутфорс формы логина (in-memory + лог для fail2ban) ---
LOGIN_MAX_FAILS   = 5
LOGIN_BAN_SECONDS = 600
_login_fails = {}  # ip -> (count, first_ts, banned_until)

def _login_blocked(ip: str) -> bool:
    rec = _login_fails.get(ip)
    return bool(rec and rec[2] and time.time() < rec[2])

def _register_login_fail(ip: str, username: str):
    now = time.time()
    cnt, first, _ = _login_fails.get(ip, (0, now, 0))
    if now - first > LOGIN_BAN_SECONDS:
        cnt, first = 0, now
    cnt += 1
    banned_until = now + LOGIN_BAN_SECONDS if cnt >= LOGIN_MAX_FAILS else 0
    _login_fails[ip] = (cnt, first, banned_until)
    try:
        with open(AUTH_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                    f"Failed login attempt from {ip} (user={username})\n")
    except Exception:
        pass

def _clear_login_fails(ip: str):
    _login_fails.pop(ip, None)

class RedirectToLogin(Exception):
    pass

def current_user(request: Request) -> str:
    try:
        u, p, ph = load_credentials()
    except RuntimeError:
        raise HTTPException(500, "config error")
    t = request.cookies.get(SESSION_COOKIE, "")
    if not check_token(t, ph or p):
        if request.url.path.startswith("/api/") or request.url.path.startswith("/download/"):
            raise HTTPException(401, "Unauthorized")
        raise RedirectToLogin()
    return u

# ================ Servers (JSON) ================
def load_servers():
    if not os.path.exists(SERVERS_FILE):
        return {"active_id": None, "servers": []}
    try:
        with open(SERVERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"active_id": None, "servers": []}

def save_servers(data):
    os.makedirs(os.path.dirname(SERVERS_FILE), exist_ok=True)
    with open(SERVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def ensure_current_server_in_list():
    data = load_servers()
    if data.get("servers"):
        return
    try:
        if not os.path.exists(AWG0_CONF):
            return
        with open(AWG0_CONF, encoding="utf-8") as f:
            conf = f.read()
        m = re.search(r"^Endpoint\s*=\s*([\d.]+):(\d+)", conf, re.MULTILINE)
        if m:
            ip = m.group(1)
            data["servers"].append({
                "id": 1, "ip": ip, "port": 22, "user": "root",
                "name": f"Current ({ip})"
            })
            data["active_id"] = 1
            save_servers(data)
    except Exception:
        pass

# ================ Update Check ================
def get_installed_commit():
    if os.path.exists(COMMIT_STATE_FILE):
        with open(COMMIT_STATE_FILE) as f:
            return f.read().strip()
    return ""

def check_github_updates():
    installed_sha = get_installed_commit()
    try:
        req = urllib.request.Request(REPO_API_URL + "?sha=main&per_page=15", headers={'User-Agent': 'Amnezia-Panel'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            commits = json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

    new_commits = []
    for c in commits:
        sha = c["sha"]
        if sha == installed_sha:
            break
        msg = c["commit"]["message"]
        date = c["commit"]["author"]["date"]
        new_commits.append({"sha": sha, "message": msg, "date": date})

    return {
        "has_updates": len(new_commits) > 0,
        "commits": new_commits[:10],
        "latest_sha": commits[0]["sha"] if commits else ""
    }

# ================ SSH helpers ================
def ssh_exec(ip, port, user, password, command, timeout=60):
    cmd = ["sshpass", "-p", password, "ssh", "-n",
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=15",
           "-p", str(port), f"{user}@{ip}", command]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0, r.stdout, r.stderr
    except Exception as e:
        return False, "", str(e)

# ================ Выполнение команд там, где живут протоколы ================
# Режимы 1/3 — локально (этот сервер), режим 2 — на SERVER-B по SSH-ключу.
def srv_exec(command, timeout=60):
    if REMOTE_MODE:
        cmd = ["ssh", "-n", "-i", SSH_KEY,
               "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null",
               "-o", "ConnectTimeout=15",
               "-p", str(SERVER_B_PORT), f"{SERVER_B_USER}@{SERVER_B_IP}", command]
    else:
        cmd = ["bash", "-c", command]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0, r.stdout, r.stderr
    except Exception as e:
        return False, "", str(e)

def srv_read(path):
    ok, out, _ = srv_exec(f"cat {shlex.quote(path)}", timeout=15)
    return out if ok else None

def srv_write(path, content):
    if REMOTE_MODE:
        cmd = ["ssh", "-i", SSH_KEY,
               "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null",
               "-p", str(SERVER_B_PORT), f"{SERVER_B_USER}@{SERVER_B_IP}",
               f"cat > {shlex.quote(path)}"]
        try:
            r = subprocess.run(cmd, input=content, capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        except Exception:
            return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception:
        return False

def do_switch_server(server, password):
    """Переключение SERVER-B через единый движок /usr/local/bin/amnezia-restore-b.sh
    (тот же скрипт доступен и из консоли). Панель лишь передаёт параметры
    через окружение и обновляет active_id в своём списке серверов."""
    ip, port, user = server["ip"], server["port"], server["user"]
    name = server.get("name", ip)

    script = "/usr/local/bin/amnezia-restore-b.sh"
    if not os.path.exists(script):
        return False, ("Не найден /usr/local/bin/amnezia-restore-b.sh — "
                       "обновите систему: deploy.sh --update")

    env = dict(os.environ)
    env.update({
        "RESTORE_B_IP": str(ip),
        "RESTORE_B_PORT": str(port),
        "RESTORE_B_USER": user,
        "RESTORE_B_F2B": "y",
        "RESTORE_B_NO_JSON": "1",
        "SSHPASS": password,
    })
    try:
        r = subprocess.run([script], env=env, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return False, f"Переключение на {ip} превысило таймаут (15 минут)"
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or ""))[-600:]
        return False, f"Ошибка переключения на {ip}: {tail}"

    data = load_servers()
    data["active_id"] = server["id"]
    save_servers(data)
    return True, f"Успешно переключено на {name} ({ip}). Туннель работает."

# ================ Traffic / clients ================
def parse_clients():
    if REMOTE_MODE:
        content = srv_read(AWG_CONF)
        if content is None: return []
    else:
        if not os.path.exists(AWG_CONF): return []
        with open(AWG_CONF, encoding="utf-8") as f: content = f.read()
    out = []
    for block in re.split(r"\n(?=\[Peer\])", content):
        if "[Peer]" not in block: continue
        nm = re.search(r"#\s*Client:\s*(\S+)", block)
        pk = re.search(r"PublicKey\s*=\s*(\S+)", block)
        ip = re.search(r"AllowedIPs\s*=\s*([0-9.]+)", block)
        if nm and pk:
            out.append({"name": nm.group(1), "pubkey": pk.group(1), "ip": ip.group(1) if ip else "?"})
    return out

def get_live_dump():
    """pubkey -> {hs, rx, tx} из 'awg show dump' (в режиме 2 — с SERVER-B)."""
    stats = {}
    try:
        if REMOTE_MODE:
            ok, out, _ = srv_exec(f"awg show {AWG_IFACE} dump", timeout=10)
            if not ok: return stats
        else:
            r = subprocess.run(["awg", "show", AWG_IFACE, "dump"],
                               capture_output=True, text=True, timeout=5)
            out = r.stdout
        for line in out.strip().split("\n")[1:]:
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            def _to_int(v):
                try: return int(v)
                except ValueError: return 0
            stats[parts[0]] = {"hs": _to_int(parts[4]), "rx": _to_int(parts[5]), "tx": _to_int(parts[6])}
    except Exception:
        pass
    return stats

def fmt_handshake(ts):
    if ts == 0: return "никогда"
    return datetime.datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M:%S')

def fmt_bytes(b):
    if b >= 1024**3: return f"{b/1024**3:.2f} GiB"
    if b >= 1024**2: return f"{b/1024**2:.2f} MiB"
    if b >= 1024:    return f"{b/1024:.2f} KiB"
    return f"{int(b)} B"

def enrich(clients):
    live = get_live_dump()
    now = time.time()
    db_stats = {}
    # История трафика (sqlite) хранится там же, где протоколы;
    # в режиме 2 панель показывает live-счётчики из awg dump.
    if not REMOTE_MODE and os.path.exists(TRAFFIC_DB):
        try:
            conn = sqlite3.connect(TRAFFIC_DB)
            cur = conn.cursor()
            cur.execute("SELECT pubkey, total_rx, total_tx, last_handshake FROM client_stats")
            for row in cur.fetchall():
                db_stats[row[0]] = {"total_rx": row[1], "total_tx": row[2], "last_handshake": row[3]}
            conn.close()
        except: pass
    for c in clients:
        # handshake всегда берём из живого awg dump (свежие данные ядра),
        # а не из sqlite-истории — иначе при остановленном коллекторе
        # или редком опросе все клиенты ложно выглядят «офлайн»
        live_st = live.get(c["pubkey"], {})
        if REMOTE_MODE:
            c["rx_bytes"] = live_st.get("rx", 0)
            c["tx_bytes"] = live_st.get("tx", 0)
            c["handshake_ts"] = live_st.get("hs", 0)
        else:
            st = db_stats.get(c["pubkey"], {})
            c["rx_bytes"] = st.get("total_rx", 0)
            c["tx_bytes"] = st.get("total_tx", 0)
            c["handshake_ts"] = live_st.get("hs") or st.get("last_handshake", 0)
        c["rx"] = fmt_bytes(c["rx_bytes"])
        c["tx"] = fmt_bytes(c["tx_bytes"])
        c["handshake"] = fmt_handshake(c["handshake_ts"])
        hs = c["handshake_ts"]
        if hs and now - hs <= 180:
            c["status"] = "ONLINE"
        elif hs or c["rx_bytes"] or c["tx_bytes"]:
            c["status"] = "OFFLINE"
        else:
            c["status"] = "NEVER"
    return clients

def total_traffic(clients):
    rx = tx = 0
    if REMOTE_MODE:
        # В режиме 2 sqlite-база на SERVER-B — суммируем live-счётчики
        rx = sum(c.get("rx_bytes", 0) for c in clients)
        tx = sum(c.get("tx_bytes", 0) for c in clients)
    elif os.path.exists(TRAFFIC_DB):
        try:
            conn = sqlite3.connect(TRAFFIC_DB)
            cur = conn.cursor()
            cur.execute("SELECT SUM(total_rx), SUM(total_tx) FROM client_stats")
            row = cur.fetchone()
            if row:
                rx, tx = row[0] or 0, row[1] or 0
            conn.close()
        except: pass
    return fmt_bytes(rx), fmt_bytes(tx)

# ================ HTML Templates ================

LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>AmneziaWG Panel - Login</title>
<style>
  :root { --bg: #f2f2f7; --surface: #ffffff; --text: #1d1d1f; --text-muted: #86868b; --border: #d2d2d7; --primary: #0071e3; --primary-hover: #0077ed; --danger-bg: #ffe5e5; --danger-text: #d93025; --header-bg: #1d1d1f; --header-text: #ffffff; }
  html[data-theme="dark"] { --bg: #1c1c1e; --surface: #2c2c2e; --text: #f5f5f7; --text-muted: #98989d; --border: #48484a; --primary: #0a84ff; --primary-hover: #409cff; --danger-bg: #3a1a1a; --danger-text: #ff6961; --header-bg: #000000; }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; transition: background-color 0.2s, border-color 0.2s; }
  body { margin: 0; background: var(--bg); color: var(--text); display: flex; flex-direction: column; min-height: 100vh; }
  header { background: var(--header-bg); color: var(--header-text); padding: 1rem; text-align: center; }
  header h1 { margin: 0; font-size: 1.25rem; font-weight: 600; }
  .auth-container { flex: 1; display: flex; align-items: center; justify-content: center; padding: 1rem; }
  .auth-card { background: var(--surface); padding: 2rem; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.06); width: 100%; max-width: 400px; }
  .auth-card h2 { margin-top: 0; margin-bottom: 1.5rem; text-align: center; font-size: 1.25rem; }
  .input-group { margin-bottom: 1.25rem; }
  input { width: 100%; padding: 0.875rem 1rem; border: 1px solid var(--border); border-radius: 10px; background: var(--bg); color: var(--text); font-size: 1rem; }
  input:focus-visible { outline: 2px solid var(--primary); outline-offset: -1px; border-color: transparent; }
  button { width: 100%; padding: 0.875rem; background: var(--primary); color: #fff; border: none; border-radius: 10px; font-size: 1rem; font-weight: 600; cursor: pointer; }
  button:hover { background: var(--primary-hover); }
  .error-msg { background: var(--danger-bg); color: var(--danger-text); padding: 0.75rem; border-radius: 8px; font-size: 0.875rem; text-align: center; margin-bottom: 1.25rem; }
</style>
</head>
<body>
<header><h1>🛡️ AmneziaWG Panel</h1><div style="font-size: 0.85em; opacity: 0.7;">__LABEL__</div></header>
<div class="auth-container">
  <div class="auth-card">
    <h2>Авторизация</h2>
    __ERR__
    <form method="post" action="__BASE__/login">
      <div class="input-group"><input type="text" name="username" placeholder="Логин" required autofocus autocomplete="username"></div>
      <div class="input-group"><input type="password" name="password" placeholder="Пароль" required autocomplete="current-password"></div>
      <button type="submit">Войти</button>
    </form>
  </div>
</div>
</body>
</html>"""

# --- Шаблоны клиентского портала (регистрация, вход, кабинет) ---
USER_AUTH_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>__TITLE__ — __LABEL__</title>
<style>
  :root { --bg: #f2f2f7; --surface: #ffffff; --text: #1d1d1f; --text-muted: #86868b; --border: #d2d2d7; --primary: #0071e3; --primary-hover: #0077ed; --danger-bg: #ffe5e5; --danger-text: #d93025; --ok-bg: #e5f9ed; --ok-text: #1d7a3d; --header-bg: #1d1d1f; --header-text: #ffffff; }
  html[data-theme="dark"] { --bg: #1c1c1e; --surface: #2c2c2e; --text: #f5f5f7; --text-muted: #98989d; --border: #48484a; --primary: #0a84ff; --primary-hover: #409cff; --danger-bg: #3a1a1a; --danger-text: #ff6961; --ok-bg: #16331f; --ok-text: #4ade80; --header-bg: #000000; }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  body { margin: 0; background: var(--bg); color: var(--text); display: flex; flex-direction: column; min-height: 100vh; }
  header { background: var(--header-bg); color: var(--header-text); padding: 1rem; text-align: center; }
  header h1 { margin: 0; font-size: 1.25rem; font-weight: 600; }
  .auth-container { flex: 1; display: flex; align-items: center; justify-content: center; padding: 1rem; }
  .auth-card { background: var(--surface); padding: 2rem; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.06); width: 100%; max-width: 400px; }
  .auth-card h2 { margin-top: 0; margin-bottom: 1.5rem; text-align: center; font-size: 1.25rem; }
  .input-group { margin-bottom: 1.25rem; }
  input { width: 100%; padding: 0.875rem 1rem; border: 1px solid var(--border); border-radius: 10px; background: var(--bg); color: var(--text); font-size: 1rem; }
  input:focus-visible { outline: 2px solid var(--primary); outline-offset: -1px; border-color: transparent; }
  button { width: 100%; padding: 0.875rem; background: var(--primary); color: #fff; border: none; border-radius: 10px; font-size: 1rem; font-weight: 600; cursor: pointer; }
  button:hover { background: var(--primary-hover); }
  .error-msg { background: var(--danger-bg); color: var(--danger-text); padding: 0.75rem; border-radius: 8px; font-size: 0.875rem; text-align: center; margin-bottom: 1.25rem; }
  .ok-msg { background: var(--ok-bg); color: var(--ok-text); padding: 0.75rem; border-radius: 8px; font-size: 0.875rem; text-align: center; margin-bottom: 1.25rem; }
  .links { text-align: center; margin-top: 1rem; font-size: 0.9rem; }
  .links a { color: var(--primary); text-decoration: none; }
</style>
</head>
<body>
<header><h1>🛡️ __LABEL__</h1></header>
<div class="auth-container">
  <div class="auth-card">
    <h2>__TITLE__</h2>
    __ERR__
    __OK__
    __BODY__
  </div>
</div>
</body>
</html>"""

CABINET_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Мои конфиги — __LABEL__</title>
<style>
  :root { --bg: #f2f2f7; --surface: #ffffff; --text: #1d1d1f; --text-muted: #86868b; --border: #e5e5ea; --primary: #0071e3; --primary-hover: #0077ed; --danger: #d93025; --online: #34c759; --offline: #ff9500; --never: #8e8e93; --header-bg: #1d1d1f; --header-text: #ffffff; }
  html[data-theme="dark"] { --bg: #1c1c1e; --surface: #2c2c2e; --text: #f5f5f7; --text-muted: #98989d; --border: #48484a; --primary: #0a84ff; --primary-hover: #409cff; --header-bg: #000000; }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  body { margin: 0; background: var(--bg); color: var(--text); min-height: 100vh; }
  header { background: var(--header-bg); color: var(--header-text); padding: 1rem; }
  .header-content { max-width: 800px; margin: 0 auto; display: flex; justify-content: space-between; align-items: center; }
  header h1 { margin: 0; font-size: 1.15rem; font-weight: 600; }
  header .sub { font-size: 0.8rem; opacity: 0.7; margin-top: 0.2rem; }
  .btn-ghost { background: transparent; border: 1px solid rgba(255,255,255,0.4); color: var(--header-text); padding: 0.4rem 0.8rem; border-radius: 8px; font-size: 0.85rem; text-decoration: none; cursor: pointer; }
  .container { max-width: 800px; margin: 0 auto; padding: 1rem; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 1.25rem; margin-bottom: 1rem; }
  .device { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; padding: 0.75rem; border: 1px solid var(--border); border-radius: 10px; margin-bottom: 0.6rem; }
  .device .name { font-weight: 600; flex: 1; min-width: 120px; }
  .badge { font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 20px; font-weight: 600; }
  .b-on { background: rgba(52,199,89,0.15); color: var(--online); }
  .b-off { background: rgba(255,149,0,0.15); color: var(--offline); }
  .b-never { background: rgba(142,142,147,0.15); color: var(--never); }
  .btn { background: var(--primary); color: #fff; border: none; border-radius: 8px; padding: 0.45rem 0.8rem; font-size: 0.85rem; cursor: pointer; text-decoration: none; display: inline-block; }
  .btn:hover { background: var(--primary-hover); }
  .btn-danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
  input { padding: 0.6rem 0.8rem; border: 1px solid var(--border); border-radius: 10px; background: var(--bg); color: var(--text); font-size: 0.95rem; }
  .muted { color: var(--text-muted); font-size: 0.85rem; }
  .alert { padding: 0.8rem 1rem; border-radius: 10px; margin-bottom: 1rem; font-size: 0.9rem; display: none; }
  .alert.ok { display: block; background: rgba(52,199,89,0.12); color: var(--online); }
  .alert.err { display: block; background: rgba(217,48,37,0.12); color: var(--danger); }
  .vpn-link { word-break: break-all; font-size: 0.75rem; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 0.5rem; margin-top: 0.5rem; display: none; }
</style>
</head>
<body>
<header>
  <div class="header-content">
    <div><h1>🛡️ __LABEL__</h1><div class="sub">Личный кабинет: __USERNAME__</div></div>
    <a href="__BASE__/ulogout" class="btn-ghost">🚪 Выход</a>
  </div>
</header>
<div class="container">
  <div id="cab-alert" class="alert"></div>
  <div class="card">
    <h3 style="margin-top:0;">➕ Добавить устройство</h3>
    <p class="muted">Для каждого устройства (телефон, ноутбук, телевизор) создаётся отдельный конфиг.</p>
    <div style="display:flex;gap:0.5rem;flex-wrap:wrap;">
      <input type="text" id="new-device" placeholder="Название, например: телефон" maxlength="24" style="flex:1;min-width:180px;" autocomplete="off">
      <button class="btn" onclick="addDevice()">Создать конфиг</button>
    </div>
  </div>
  <div class="card">
    <h3 style="margin-top:0;">📱 Мои устройства</h3>
    <div id="devices-list"><div class="muted">Загрузка...</div></div>
  </div>
  <div class="card muted">
    <b>Как подключиться:</b> установите приложение AmneziaVPN → «Добавить профиль» →
    вставьте vpn:// ссылку или импортируйте скачанный .conf файл.
  </div>
</div>
<script>
const BASE_URL = "__BASE__";
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function alertBox(kind, txt){const a=document.getElementById('cab-alert');a.className='alert '+kind;a.textContent=txt;if(txt)setTimeout(()=>{a.className='alert';},6000);}
async function loadDevices(){
  try {
    const r = await fetch(BASE_URL + '/api/cabinet/devices', {credentials:'same-origin'});
    if (r.status === 401) { location.href = BASE_URL + '/ulogin'; return; }
    const list = await r.json();
    const box = document.getElementById('devices-list');
    if (!list.length) { box.innerHTML = '<div class="muted">Устройств пока нет — добавьте первое выше.</div>'; return; }
    box.innerHTML = list.map(d => {
      const badge = d.status === 'ONLINE' ? '<span class="badge b-on">онлайн</span>'
                  : d.status === 'OFFLINE' ? '<span class="badge b-off">офлайн</span>'
                  : '<span class="badge b-never">не подключался</span>';
      return `<div class="device">
        <div class="name">${esc(d.label)} ${badge}<div class="muted">${esc(d.client_name)} · ${esc(d.ip||'')}</div></div>
        <a class="btn" href="${BASE_URL}/cabinet/dl/conf/${encodeURIComponent(d.client_name)}">⬇ .conf</a>
        <button class="btn" onclick="showVpn('${esc(d.client_name)}')">🔗 vpn://</button>
        <button class="btn btn-danger" onclick="delDevice('${esc(d.client_name)}','${esc(d.label)}')">🗑</button>
        <div class="vpn-link" id="vpn-${esc(d.client_name)}"></div>
      </div>`;
    }).join('');
  } catch(e) { document.getElementById('devices-list').innerHTML = '<div class="muted">Ошибка загрузки</div>'; }
}
async function addDevice(){
  const label = document.getElementById('new-device').value.trim();
  if (!label) { alertBox('err','Введите название устройства'); return; }
  const r = await fetch(BASE_URL + '/api/cabinet/devices/add', {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({label})});
  const d = await r.json();
  if (!r.ok) { alertBox('err', d.error || 'Ошибка'); return; }
  document.getElementById('new-device').value = '';
  alertBox('ok', 'Конфиг «' + label + '» создан');
  loadDevices();
}
async function delDevice(clientName, label){
  if (!confirm('Удалить конфиг «' + label + '»? Устройство потеряет доступ.')) return;
  const r = await fetch(BASE_URL + '/api/cabinet/devices/del', {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({client_name: clientName})});
  const d = await r.json();
  if (!r.ok) { alertBox('err', d.error || 'Ошибка'); return; }
  alertBox('ok', 'Конфиг удалён');
  loadDevices();
}
async function showVpn(clientName){
  const el = document.getElementById('vpn-' + clientName);
  if (el.style.display === 'block') { el.style.display = 'none'; return; }
  const r = await fetch(BASE_URL + '/cabinet/dl/vpn/' + encodeURIComponent(clientName), {credentials:'same-origin'});
  if (!r.ok) { alertBox('err','Ссылка недоступна'); return; }
  const txt = await r.text();
  el.textContent = txt;
  el.style.display = 'block';
  try { await navigator.clipboard.writeText(txt); alertBox('ok','vpn:// ссылка скопирована'); } catch(e) {}
}
loadDevices();
</script>
</body>
</html>"""

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>AmneziaWG Panel</title>
<style>
  :root { 
    --bg: #f2f2f7; --surface: #ffffff; --text: #1d1d1f; --text-muted: #86868b; --border: #ececf0; 
    --input-bg: #ffffff; --input-border: #d2d2d7; 
    --primary: #0071e3; --primary-hover: #0077ed; 
    --btn-bg: #e8e8ed; --btn-hover: #dcdce1;
    --danger-bg: #ffe5e5; --danger-text: #d93025; --danger-hover: #ffcccc;
    --success-bg: #e5f7ec; --success-text: #006c2b; --success-border: #009e3d;
    --header-bg: #1d1d1f; --header-text: #ffffff; 
    --online: #34c759; --offline: #ff9500; --never: #8e8e93;
    --font-stack: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  html[data-theme="dark"] { 
    --bg: #000000; --surface: #1c1c1e; --text: #f5f5f7; --text-muted: #98989d; --border: #38383a; 
    --input-bg: #1c1c1e; --input-border: #48484a; 
    --primary: #0a84ff; --primary-hover: #409cff; 
    --btn-bg: #2c2c2e; --btn-hover: #3a3a3c;
    --danger-bg: #3a1a1a; --danger-text: #ff6961; --danger-hover: #4a1e1e;
    --success-bg: #1a3a24; --success-text: #6bd68a; --success-border: #30d158;
    --header-bg: #000000; --header-border: #333333;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--font-stack); padding-bottom: 2rem; }
  a { color: var(--primary); text-decoration: none; }
  *:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }

  header { background: var(--header-bg); border-bottom: 1px solid var(--header-border, transparent); padding: 1rem; position: sticky; top: 0; z-index: 100; }
  .header-content { max-width: 1200px; margin: 0 auto; display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 1rem; }
  .header-brand { display: flex; flex-direction: column; }
  .header-brand h1 { margin: 0; font-size: 1.25rem; font-weight: 600; color: var(--header-text); }
  .header-brand .sub { font-size: 0.85rem; opacity: 0.7; color: var(--header-text); }
  
  .header-controls { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .traffic-summary { background: rgba(255,255,255,0.1); padding: 0.3rem 0.6rem; border-radius: 8px; font-family: monospace; font-size: 0.8rem; color: var(--header-text); line-height: 1.4; border: 1px solid rgba(255,255,255,0.2); }
  .traffic-summary span { font-weight: bold; }
  .rx-color { color: #6bd68a; } .tx-color { color: #60a5fa; }
  
  .btn-ghost { background: transparent; color: var(--header-text); border: 1px solid rgba(255,255,255,0.3); padding: 0.4rem 0.8rem; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }
  .btn-ghost:hover { background: rgba(255,255,255,0.1); }
  
  .tabs { display: flex; gap: 0.5rem; margin: 1.5rem auto 1rem; max-width: 1200px; padding: 0 1rem; overflow-x: auto; }
  .tab { padding: 0.6rem 1.2rem; background: transparent; border: none; font-size: 1rem; font-weight: 600; color: var(--text-muted); cursor: pointer; border-bottom: 2px solid transparent; transition: 0.2s; white-space: nowrap; }
  .tab.active { color: var(--primary); border-bottom-color: var(--primary); }
  .tab:hover:not(.active) { color: var(--text); }
  
  .container { max-width: 1200px; margin: 0 auto; padding: 0 1rem; }
  .view-section { display: none; animation: fadeIn 0.2s ease-in-out; }
  .view-section.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

  .card { background: var(--surface); border-radius: 16px; padding: 1.5rem; margin-bottom: 1.25rem; box-shadow: 0 2px 12px rgba(0,0,0,0.04); border: 1px solid var(--border); }
  .card h3 { margin: 0 0 1rem; font-size: 1.1rem; font-weight: 600; }
  
  .form-group { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  input[type="text"], input[type="number"], input[type="password"] { 
    flex: 1; min-width: 200px; padding: 0.75rem 1rem; border: 1px solid var(--input-border); border-radius: 10px; background: var(--input-bg); color: var(--text); font-size: 1rem; 
  }
  .btn { display: inline-flex; align-items: center; justify-content: center; padding: 0.75rem 1.2rem; border: none; border-radius: 10px; background: var(--btn-bg); color: var(--text); font-size: 0.9rem; font-weight: 500; cursor: pointer; text-decoration: none; min-height: 44px; }
  .btn:hover { background: var(--btn-hover); }
  .btn-primary { background: var(--primary); color: #ffffff; }
  .btn-primary:hover { background: var(--primary-hover); color: #ffffff; }
  .btn-danger { background: var(--danger-bg); color: var(--danger-text); }
  .btn-danger:hover { background: var(--danger-hover); }
  .btn-small { padding: 0.4rem 0.8rem; font-size: 0.85rem; min-height: 32px; border-radius: 6px; }

  /* Стили переключателя (Toggle Switch) */
  .switch { position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: var(--border); transition: .3s; border-radius: 24px; }
  .slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: .3s; border-radius: 50%; box-shadow: 0 1px 3px rgba(0,0,0,0.3); }
  input:checked + .slider { background-color: var(--success-border); }
  input:checked + .slider:before { transform: translateX(20px); }

  .toolbar { display: flex; flex-wrap: wrap; gap: 1rem; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
  .search-box { flex: 1; min-width: 250px; position: relative; }
  .search-box input { width: 100%; padding-left: 2.5rem; border-radius: 10px; }
  .search-box::before { content: "🔍"; position: absolute; left: 0.8rem; top: 50%; transform: translateY(-50%); font-size: 0.9rem; opacity: 0.6; }
  .filter-box select { padding: 0.75rem 1rem; border-radius: 10px; border: 1px solid var(--input-border); background: var(--input-bg); color: var(--text); font-size: 0.95rem; cursor: pointer; min-height: 44px;}

  .table-wrapper { overflow-x: auto; width: 100%; }
  table { width: 100%; border-collapse: collapse; text-align: left; }
  th, td { padding: 0.875rem; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
  th { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); font-weight: 600; cursor: pointer; user-select: none; }
  th:hover { color: var(--text); }
  th.sort-asc::after { content: " \2191"; color: var(--primary); }
  th.sort-desc::after { content: " \2193"; color: var(--primary); }
  
  .status-badge { display: inline-block; padding: 0.25rem 0.5rem; border-radius: 6px; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.03em; }
  .status-online { background: rgba(52, 199, 89, 0.15); color: var(--online); }
  .status-offline { background: rgba(255, 149, 0, 0.15); color: var(--offline); }
  .status-never { background: rgba(142, 142, 147, 0.15); color: var(--never); }

  .actions-cell { display: flex; gap: 0.4rem; flex-wrap: wrap; }
  .empty-state { text-align: center; padding: 3rem 1rem; color: var(--text-muted); }

  @media (max-width: 768px) {
    .header-content { flex-direction: column; align-items: stretch; text-align: center; }
    .header-controls { justify-content: center; }
    .table-wrapper table, .table-wrapper tbody, .table-wrapper tr { display: block; width: 100%; }
    .table-wrapper thead { display: none; }
    .table-wrapper tr { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 1rem; padding: 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
    .table-wrapper td { display: flex; justify-content: space-between; align-items: center; padding: 0.25rem 0; border: none; font-size: 0.95rem; text-align: right; }
    .table-wrapper td::before { content: attr(data-label); color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; font-weight: 600; text-align: left; margin-right: 1rem; }
    .table-wrapper td:last-child { margin-top: 0.5rem; padding-top: 0.75rem; border-top: 1px dashed var(--border); justify-content: flex-end; width: 100%; }
    .actions-cell { justify-content: flex-end; width: 100%; }
    .toolbar { flex-direction: column; align-items: stretch; }
  }

  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 2000; align-items: center; justify-content: center; padding: 1rem; backdrop-filter: blur(2px); }
  .modal-content { background: var(--surface); color: var(--text); padding: 1.5rem; border-radius: 16px; width: 100%; max-width: 600px; max-height: 90vh; overflow-y: auto; position: relative; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
  .modal-close { position: absolute; top: 1rem; right: 1rem; font-size: 1.5rem; cursor: pointer; color: var(--text-muted); background: none; border: none; line-height: 1; padding: 0.5rem; }
  .modal-close:hover { color: var(--text); }
  
  .global-loader { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.8); z-index: 3000; color: #fff; flex-direction: column; align-items: center; justify-content: center; }
  .spinner { width: 40px; height: 40px; border: 4px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: var(--primary); animation: spin 1s linear infinite; margin-bottom: 1rem; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .update-list { margin: 1rem 0; max-height: 200px; overflow-y: auto; font-size: 0.9em; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 0.5rem; }
  .update-item { padding: 0.5rem; border-bottom: 1px solid var(--border); }
  .update-item:last-child { border: none; }
  .alert { padding: 1rem; border-radius: 10px; margin-bottom: 1rem; font-size: 0.95rem; font-weight: 500; display: flex; align-items: center; gap: 0.5rem; }
  .alert.ok { background: var(--success-bg); color: var(--success-text); border: 1px solid var(--success-border); }
  .alert.err { background: var(--danger-bg); color: var(--danger-text); border: 1px solid var(--danger-text); }
</style>
</head>
<body>
<header>
  <div class="header-content">
    <div class="header-brand">
      <h1>🛡️ AmneziaWG Panel</h1>
      <div class="sub">__LABEL__</div>
    </div>
    <div class="header-controls">
      <div class="traffic-summary" title="Суммарный трафик">
        <div>⬇ <span class="rx-color" id="total-rx">__TOTAL_RX__</span></div>
        <div>⬆ <span class="tx-color" id="total-tx">__TOTAL_TX__</span></div>
      </div>
      <button class="btn-ghost" onclick="checkUpdates()">🔄 Обновления</button>
      <button class="btn-ghost" onclick="restartTunnels()">⚡ Рестарт</button>
      <button class="btn-ghost" onclick="toggleTheme()" id="theme-btn">🌙</button>
      <a href="__BASE__/logout" class="btn-ghost">🚪 Выход</a>
    </div>
  </div>
</header>

<div class="tabs">
  <button class="tab active" onclick="switchTab('clients')">👥 Клиенты (<span id="clients-count">__COUNT__</span>)</button>
  <button class="tab" onclick="switchTab('users')">👤 Пользователи <span id="users-pending"></span></button>
  <button class="tab" onclick="switchTab('protocols')">🧩 Протоколы</button>
  __SERVERS_TAB__
  <button class="tab" onclick="switchTab('notify')">🔔 Уведомления</button>
</div>

<div class="container">
  __MSG__
  __ERR__

  <div id="clients-view" class="view-section active">
    __AWG_HINT__
    <div class="card" style="display: flex; gap: 1rem; flex-wrap: wrap;">
      <div style="flex: 1; min-width: 280px;">
        <h3 style="margin-top:0;">➕ Новый клиент</h3>
        <form class="form-group" method="post" action="__BASE__/create" onsubmit="showLoader('Создание клиента...');" style="margin-bottom:0;">
          <input type="text" name="name" placeholder="Имя (латиница, цифры, - _)" pattern="[a-zA-Z0-9_-]+" required autocomplete="off">
          <button type="submit" class="btn btn-primary">Создать</button>
        </form>
      </div>
      <div style="border-left: 1px solid var(--border); padding-left: 1rem; min-width: 250px; display: flex; align-items: center; justify-content: space-between; gap: 1rem;">
        <div>
          <div style="font-weight: 600; font-size: 1rem; margin-bottom: 0.25rem;">Внутрисетевая связь (LAN)</div>
          <div style="font-size: 0.85rem; color: var(--text-muted); line-height: 1.3;">Позволяет VPN-клиентам<br>взаимодействовать друг с другом</div>
        </div>
        <label class="switch">
          <input type="checkbox" id="lan-toggle" onchange="toggleLanStatus()">
          <span class="slider"></span>
        </label>
      </div>
    </div>

    <div class="card">
      <div class="toolbar">
        <div class="search-box">
          <input type="text" id="search-input" name="client-search-filter" placeholder="Поиск по имени или IP..." oninput="applyFilters()" autocomplete="off" data-lpignore="true" data-1p-ignore data-form-type="other">
        </div>
        <div class="filter-box">
          <select id="status-filter" onchange="applyFilters()">
            <option value="all">Все статусы</option>
            <option value="ONLINE">Онлайн</option>
            <option value="OFFLINE">Офлайн</option>
          </select>
        </div>
      </div>

      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th data-sort="name" onclick="setSort('name')">Имя</th>
              <th data-sort="ip" onclick="setSort('ip')">IP</th>
              <th data-sort="status" onclick="setSort('status')">Статус</th>
              <th data-sort="rx_bytes" onclick="setSort('rx_bytes')">RX</th>
              <th data-sort="tx_bytes" onclick="setSort('tx_bytes')">TX</th>
              <th data-sort="handshake_ts" onclick="setSort('handshake_ts')">Handshake</th>
              <th style="text-align: right;">Действия</th>
            </tr>
          </thead>
          <tbody id="clients-tbody"></tbody>
        </table>
        <div id="empty-state" class="empty-state" style="display:none;">Нет клиентов</div>
      </div>
      <div style="text-align: right; font-size: 0.75rem; color: var(--text-muted); margin-top: 1rem;">
        Обновлено: <span id="last-update">--:--:--</span>
      </div>
    </div>
  </div>

  <div id="users-view" class="view-section">
    <div class="card">
      <h3 style="margin-top:0;">👤 Пользователи и их устройства</h3>
      <p style="font-size: 0.9rem; color: var(--text-muted); margin-bottom: 1rem;">
        Ссылка для регистрации абонентов: <code id="reg-link"></code>
        <button class="btn btn-small" onclick="navigator.clipboard.writeText(document.getElementById('reg-link').textContent).then(()=>alert('Ссылка скопирована'))">📋</button><br>
        Заявки появляются здесь (и кнопкой в Telegram-боте) — одобряйте или отклоняйте.
        У одобренного пользователя есть личный кабинет, где он видит только свои конфиги.
      </p>
      <div id="users-list"></div>
    </div>
    <div class="card">
      <h4 style="margin-top:0;">📧 Почтовые уведомления (SMTP)</h4>
      <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.75rem;">
        Письма абонентам: доступ одобрен, нужно обновить конфиг и т.п. Например, для Gmail — хост smtp.gmail.com, порт 465, пароль приложения.
      </p>
      <div id="mail-status"></div>
      <div class="form-group" style="margin-bottom:0.5rem;">
        <input type="text" id="smtp-host" placeholder="SMTP хост (smtp.gmail.com)" autocomplete="off">
        <input type="number" id="smtp-port" placeholder="Порт" value="465" style="max-width:100px;">
      </div>
      <div class="form-group" style="margin-bottom:0.5rem;">
        <input type="text" id="smtp-user" placeholder="SMTP логин (обычно email)" autocomplete="off">
        <input type="password" id="smtp-pass" placeholder="SMTP пароль (пусто = не менять)" autocomplete="new-password">
      </div>
      <div class="form-group">
        <input type="text" id="smtp-from" placeholder="От кого (пусто = логин)" autocomplete="off">
        <button class="btn btn-primary" onclick="saveMail()">Сохранить</button>
        <button class="btn" onclick="testMail()">Тест на email…</button>
      </div>
    </div>
  </div>

  <div id="protocols-view" class="view-section">
    <div class="card">
      <h3 style="margin-top:0;">🧩 Дополнительные протоколы и сервисы</h3>
      <p style="font-size: 0.9rem; color: var(--text-muted); margin-bottom: 1rem;">
        Установка и удаление дополнительных протоколов на __PROTO_HOST__.
        Ссылки для подключения можно импортировать в AmneziaVPN («Добавить профиль → вставить ключ»).
      </p>
      <div id="protocols-list"></div>
    </div>
  </div>

  <div id="servers-view" class="view-section">
    <div class="card">
      <h3 style="margin-top:0;">Управление выходным шлюзом (SERVER B)</h3>
      <p style="font-size: 0.9rem; color: var(--text-muted); margin-bottom: 1rem;">Здесь можно добавить новый VPS за рубежом и переключить на него весь трафик туннеля.</p>
      <div class="table-wrapper">
        <table id="servers-table">
          <thead><tr><th>Имя</th><th>IP</th><th>Статус</th><th style="text-align: right;">Действия</th></tr></thead>
          <tbody id="servers-tbody"></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h4 style="margin-top:0;">Добавить сервер</h4>
      <div class="form-group" style="margin-bottom: 0.5rem;">
        <input type="text" id="add-ip" placeholder="IP адрес">
        <input type="number" id="add-port" placeholder="SSH порт (22)" value="22" style="max-width: 120px;">
        <input type="text" id="add-user" placeholder="Пользователь (root)" value="root" style="max-width: 150px;">
      </div>
      <div class="form-group">
        <input type="password" id="add-pass" placeholder="SSH пароль (не сохраняется)">
        <input type="text" id="add-name" placeholder="Название (опционально)">
        <button class="btn btn-primary" id="add-server-btn" onclick="addServer()">Проверить и добавить</button>
      </div>
    </div>
    <div class="card">
      <h4 style="margin-top:0;">💾 Бэкап SERVER-A</h4>
      <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.75rem;">
        Полный бэкап: ключи, конфиги, клиенты, настройки панели и протоколов.
        Создаётся ежедневно, после каждого изменения клиентов и протоколов,
        а копия дополнительно хранится на SERVER-B. Скачайте архив к себе —
        с ним новый сервер поднимается командой <code>deploy.sh --restore</code>.
        Выгрузка в облако (Google Drive и др.): <code>sudo amnezia-backup-cloud.sh</code>
      </p>
      <button class="btn btn-primary" onclick="backupCreate()">Создать бэкап сейчас</button>
      <div id="backups-list" style="margin-top:0.75rem;font-size:0.85rem;"></div>
    </div>
  </div>

  <div id="notify-view" class="view-section">
    <div class="card">
      <h3 style="margin-top:0;">🔔 Telegram-уведомления</h3>
      <p style="font-size: 0.9rem; color: var(--text-muted); margin-bottom: 1rem;">
        Уведомления о состоянии туннеля, создании и удалении клиентов, переключении серверов и включении LAN.
      </p>
      <div id="notify-status"></div>
      <div class="form-group" style="margin-bottom: 0.5rem;">
        <input type="password" id="tg-token" placeholder="Токен бота (123456789:ABC-DEF1234...)" autocomplete="off">
      </div>
      <div class="form-group">
        <input type="text" id="tg-chat" placeholder="Chat ID (например, 123456789)" autocomplete="off">
        <button class="btn btn-primary" onclick="saveNotify()">Сохранить</button>
        <button class="btn" onclick="testNotify()">Отправить тест</button>
      </div>
      <details style="margin-top: 1rem; font-size: 0.85rem; color: var(--text-muted);">
        <summary style="cursor: pointer;">Как получить токен и chat_id?</summary>
        <ol style="line-height: 1.7; padding-left: 1.2rem;">
          <li>Откройте <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> в Telegram, отправьте <code>/newbot</code> и придумайте имя — BotFather выдаст токен.</li>
          <li>Напишите вашему новому боту любое сообщение.</li>
          <li>Откройте в браузере <code>https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</code> — в поле <code>chat → id</code> будет ваш chat_id (для группы — отрицательный).</li>
        </ol>
      </details>
    </div>
  </div>
</div>

<!-- Модальное окно: LAN -->
<div id="lan-modal" class="modal">
  <div class="modal-content">
    <button class="modal-close" onclick="closeModal('lan-modal')">&times;</button>
    <h3>🌐 Внутрисетевая связь (LAN) активирована</h3>
    <p style="font-size: 0.95rem;">Теперь ваши устройства, подключенные к этому VPN, объединены в общую виртуальную сеть (10.8.0.x) и могут взаимодействовать друг с другом напрямую.</p>
    <div style="background: var(--bg); padding: 1rem; border-radius: 8px; border: 1px solid var(--border);">
      <p style="margin-top:0; font-size: 0.9rem; font-weight:600;">Проверка связи:</p>
      <p style="font-size: 0.85rem; margin-bottom: 0.5rem;">Попробуйте пропинговать IP-адрес нужного устройства. Если пинг не проходит на устройство с Windows, выполните на нём в PowerShell от имени Администратора:</p>
      <code style="display:block; padding: 0.75rem; background: #000; color: #4ade80; border-radius: 6px; font-size: 0.8rem; overflow-x: auto;">
New-NetFirewallRule -DisplayName "Allow Ping from VPN" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -RemoteAddress 10.8.0.0/24 -Action Allow
      </code>
    </div>
    <button class="btn btn-primary" style="margin-top: 1rem; width: 100%;" onclick="closeModal('lan-modal')">Понятно, закрыть</button>
  </div>
</div>

<div id="update-modal" class="modal">
  <div class="modal-content">
    <button class="modal-close" onclick="closeModal('update-modal')">&times;</button>
    <h3>Доступны обновления</h3>
    <div id="update-list" class="update-list"></div>
    <div style="background: var(--bg); padding: 1rem; border-radius: 8px; margin-top: 1rem; border: 1px solid var(--border);">
      <p style="margin-top:0; font-size: 0.9rem; color: var(--text-muted);">Для обновления выполните в консоли сервера:</p>
      <code style="display:block; padding: 0.5rem; background: #000; color: #4ade80; border-radius: 6px; font-size: 0.85rem; overflow-x: auto;">
        sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/daff4ik/amneziawg-cascade-v3/main/deploy.sh)" -- --update
      </code>
    </div>
  </div>
</div>

<div id="global-loader" class="global-loader">
  <div class="spinner"></div>
  <div id="loader-text" style="font-weight: 500;">Подождите...</div>
</div>

<script>
const BASE_URL = "__BASE__";
let _allClients = [];
let _serversData = { active_id: null, servers: [] };
let _currentSort = 'name';
let _sortAsc = true;
let _pollInterval = null;

function initTheme() {
  const saved = localStorage.getItem('theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  document.getElementById('theme-btn').textContent = saved === 'dark' ? '☀️' : '🌙';
}
function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  document.getElementById('theme-btn').textContent = next === 'dark' ? '☀️' : '🌙';
}

function showLoader(txt) { document.getElementById('loader-text').textContent = txt; document.getElementById('global-loader').style.display = 'flex'; }
function hideLoader() { document.getElementById('global-loader').style.display = 'none'; }
function closeModal(id) { document.getElementById(id).style.display = 'none'; }
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function formatDateTime(val) {
  if (!val) return '';
  const d = val instanceof Date ? val : new Date(val);
  if (isNaN(d)) return val;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getDate())}.${pad(d.getMonth()+1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function switchTab(tabId) {
  document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById(`${tabId}-view`).classList.add('active');
  document.querySelector(`button[onclick="switchTab('${tabId}')"]`).classList.add('active');
  if (tabId === 'servers') { loadServers(); loadBackups(); }
  if (tabId === 'notify') loadNotify();
  if (tabId === 'protocols') loadProtocols();
  if (tabId === 'users') { loadUsers(); loadMail(); }
}

// ==================== Users (абоненты) ====================
async function loadUsers() {
  const box = document.getElementById('users-list');
  box.innerHTML = '<div style="color:var(--text-muted);">Загрузка...</div>';
  try {
    const r = await fetch(BASE_URL + '/api/users', {credentials:'same-origin'});
    if (!r.ok) throw new Error('Ошибка сервера');
    const list = await r.json();
    const pending = list.filter(u => u.status === 'pending').length;
    document.getElementById('users-pending').textContent = pending ? `(${pending} ⏳)` : '';
    document.getElementById('reg-link').textContent = location.origin + BASE_URL + '/register';
    if (!list.length) { box.innerHTML = '<div style="color:var(--text-muted);">Пользователей пока нет — отправьте им ссылку для регистрации выше.</div>'; return; }
    box.innerHTML = list.map(u => {
      const badges = {pending: '<span class="status-badge status-offline">⏳ ожидает</span>',
                      approved: '<span class="status-badge status-online">✅ активен</span>',
                      rejected: '<span class="status-badge status-never">🚫 отклонён</span>'};
      let actions = '';
      if (u.status === 'pending') {
        actions = `<button class="btn btn-small btn-primary" onclick="userAction('approve','${u.username}','${escapeHtml(u.username)}')">✅ Одобрить</button>
          <button class="btn btn-small btn-danger" onclick="userAction('reject','${u.username}','${escapeHtml(u.username)}')">❌ Отклонить</button>`;
      } else {
        if (u.status === 'approved') {
          actions += `<button class="btn btn-small" onclick="userNotify('${u.username}','${escapeHtml(u.username)}')">✉️ Уведомить (обновить конфиг)</button>
            <button class="btn btn-small" onclick="userResetPass('${u.username}','${escapeHtml(u.username)}')">🔑 Сброс пароля</button>`;
        }
        actions += ` <button class="btn btn-small btn-danger" onclick="userDelete('${u.username}','${escapeHtml(u.username)}',${u.devices.length})">🗑 Удалить</button>`;
      }
      const devRows = u.devices.map(d => `<div style="display:flex;align-items:center;gap:0.5rem;padding:0.4rem 0.4rem 0.4rem 1.5rem;border-top:1px dashed var(--border);flex-wrap:wrap;">
          <span style="flex:1;min-width:120px;">└ ${escapeHtml(d.label)} <code style="font-size:0.75rem;color:var(--text-muted);">${escapeHtml(d.client_name)}</code></span>
          <a class="btn btn-small" href="${BASE_URL}/download/conf/${encodeURIComponent(d.client_name)}">⬇ .conf</a>
          <button class="btn btn-small btn-danger" onclick="deviceDelete('${d.client_name}','${escapeHtml(d.label)}')">🗑</button>
        </div>`).join('');
      const addDev = u.status === 'approved' ? `<div style="display:flex;gap:0.5rem;padding:0.5rem 0 0 1.5rem;flex-wrap:wrap;">
          <input type="text" id="newdev-${u.username}" placeholder="новое устройство (ноутбук...)" maxlength="24" style="flex:1;min-width:150px;padding:0.4rem 0.6rem;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);">
          <button class="btn btn-small" onclick="userAddDevice('${u.username}')">➕ Добавить конфиг</button>
        </div>` : '';
      return `<div style="border:1px solid var(--border);border-radius:12px;padding:1rem;margin-bottom:0.75rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:0.5rem;flex-wrap:wrap;">
          <div><strong>${escapeHtml(u.username)}</strong> ${badges[u.status] || u.status}
            <div style="font-size:0.8rem;color:var(--text-muted);">${escapeHtml(u.email)}</div>
          </div>
          <div style="display:flex;gap:0.4rem;flex-wrap:wrap;">${actions}</div>
        </div>
        ${devRows}${addDev}
      </div>`;
    }).join('');
  } catch(e) { box.innerHTML = `<div class="alert err">❌ ${escapeHtml(e.message)}</div>`; }
}

async function userAction(action, username, label) {
  showLoader(action === 'approve' ? `Одобрение ${label}...` : `Отклонение ${label}...`);
  try {
    const r = await fetch(BASE_URL + `/api/users/${action}/${username}`, {method:'POST', credentials:'same-origin'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); loadUsers(); }
}

async function userDelete(username, label, devCount) {
  if (!confirm(`Удалить пользователя ${label}?\nБудут удалены все его конфиги (${devCount} шт).`)) return;
  showLoader(`Удаление ${label}...`);
  try {
    const r = await fetch(BASE_URL + `/api/users/delete/${username}`, {method:'POST', credentials:'same-origin'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); loadUsers(); fetchClients(); }
}

async function deviceDelete(clientName, label) {
  if (!confirm(`Удалить конфиг «${label}» (${clientName})?`)) return;
  showLoader('Удаление конфига...');
  try {
    const r = await fetch(BASE_URL + `/api/devices/delete/${clientName}`, {method:'POST', credentials:'same-origin'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); loadUsers(); fetchClients(); }
}

async function userAddDevice(username) {
  const inp = document.getElementById(`newdev-${username}`);
  const label = (inp.value || '').trim();
  if (!label) { alert('Введите название устройства'); return; }
  showLoader('Создание конфига... (до минуты)');
  try {
    const r = await fetch(BASE_URL + '/api/users/add-device', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({username, label})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
    inp.value = '';
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); loadUsers(); fetchClients(); }
}

async function userNotify(username, label) {
  if (!confirm(`Отправить ${label} письмо «обновите конфиги»?`)) return;
  showLoader('Отправка письма...');
  try {
    const r = await fetch(BASE_URL + `/api/users/notify/${username}`, {method:'POST', credentials:'same-origin'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
    alert('✅ Письмо отправлено');
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); }
}

async function userResetPass(username, label) {
  const pwd = prompt(`Новый пароль для ${label} (минимум 8 символов):`);
  if (pwd === null) return;
  if (pwd.length < 8) { alert('Минимум 8 символов'); return; }
  showLoader('Смена пароля...');
  try {
    const r = await fetch(BASE_URL + `/api/users/reset-pass/${username}`, {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: pwd})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
    alert('✅ Пароль изменён. Передайте его пользователю.');
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); }
}

// ==================== Mail settings ====================
async function loadMail() {
  try {
    const r = await fetch(BASE_URL + '/api/mail/settings', {credentials:'same-origin'});
    if (!r.ok) return;
    const d = await r.json();
    document.getElementById('mail-status').innerHTML = d.configured
      ? '<div class="alert ok">✅ SMTP настроен</div>'
      : '<div class="alert err">⚠️ SMTP не настроен — письма абонентам не отправляются</div>';
    document.getElementById('smtp-host').value = d.host || '';
    document.getElementById('smtp-port').value = d.port || '465';
    document.getElementById('smtp-user').value = d.user || '';
    document.getElementById('smtp-from').value = d.from || '';
  } catch(e) {}
}

async function saveMail() {
  showLoader('Сохранение настроек почты...');
  try {
    const r = await fetch(BASE_URL + '/api/mail/settings', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({host: document.getElementById('smtp-host').value.trim(),
        port: document.getElementById('smtp-port').value.trim(),
        user: document.getElementById('smtp-user').value.trim(),
        password: document.getElementById('smtp-pass').value,
        from: document.getElementById('smtp-from').value.trim()})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
    document.getElementById('smtp-pass').value = '';
    await loadMail();
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); }
}

async function testMail() {
  const to = prompt('На какой email отправить тестовое письмо?');
  if (!to) return;
  showLoader('Отправка тестового письма...');
  try {
    const r = await fetch(BASE_URL + '/api/mail/test', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({to: to.trim()})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
    alert('✅ Письмо отправлено, проверьте почту (и спам).');
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); }
}

// ==================== Protocols ====================
async function loadProtocols() {
  const box = document.getElementById('protocols-list');
  box.innerHTML = '<div style="color:var(--text-muted);">Загрузка...</div>';
  try {
    const r = await fetch(BASE_URL + '/api/protocols', {credentials:'same-origin'});
    if (!r.ok) throw new Error('Ошибка сервера');
    const list = await r.json();
    box.innerHTML = list.map(p => {
      const badge = p.installed
        ? `<span class="status-badge status-online">${escapeHtml(p.state || 'работает')}</span>`
        : `<span class="status-badge status-never">не установлен</span>`;
      let body = '';
      if (p.installed) {
        body = `<div style="font-size:0.9rem;margin:0.5rem 0;">Порт: <code>${escapeHtml(p.port || '—')}</code>${p.domain ? ` · маскировка: <code>${escapeHtml(p.domain)}</code>` : ''}</div>`;
        if (p.link) {
          body += `<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:0.6rem;font-size:0.8rem;word-break:break-all;margin-bottom:0.5rem;"><code>${escapeHtml(p.link)}</code></div>
          <button class="btn btn-small" onclick="navigator.clipboard.writeText('${p.link.replace(/'/g, "\\'")}').then(()=>alert('Ссылка скопирована'))">📋 Копировать ссылку</button> `;
        }
        if (p.name === 'amneziawg') {
          body += `<div style="font-size:0.85rem;color:var(--text-muted);margin-bottom:0.5rem;">Версия пакета: ${escapeHtml(p.version || '?')} · клиентов: ${p.clients || 0}</div>
          <button class="btn btn-small btn-primary" onclick="protoAction('amneziawg','update','обновление AmneziaWG на обоих серверах')">⬆ Проверить обновления</button> `;
        }
        if (p.name === 'amnezia-dns') {
          const abOn = p.adblock === '1';
          const abLabel = abOn
            ? `🚫 Реклама: ВКЛ${p.adblock_domains ? ' (' + p.adblock_domains + ' доменов)' : ''} — выключить`
            : '🚫 Реклама: ВЫКЛ — включить';
          body += `<div style="margin:0.5rem 0;">
            <button class="btn btn-small btn-primary" onclick="protoAction('amnezia-dns','use-for-clients','переключение клиентов на свой DNS')">🔀 Клиентам — этот DNS</button>
            <button class="btn btn-small" onclick="protoAction('amnezia-dns','use-cloudflare','возврат DNS Cloudflare')">↩️ Вернуть Cloudflare</button>
            <button class="btn btn-small ${abOn ? '' : 'btn-primary'}" onclick="protoAction('amnezia-dns','${abOn ? 'adblock-off' : 'adblock-on'}','блокировка рекламы')">${abLabel}</button>
          </div>
          <div style="font-size:0.8rem;color:var(--text-muted);margin-bottom:0.5rem;">После переключения DNS клиенты должны заново импортировать конфиг / vpn:// ссылку. Блокировка рекламы применяется сразу.</div>`;
        }
        if (p.name === 'mtproxy' || p.name === 'telemt') {
          body += `<button class="btn btn-small" onclick="protoAction('${p.name}','restart','перезапуск ${escapeHtml(p.title)}')">🔄 Перезапустить</button> `;
        }
        if (p.name !== 'amneziawg') {
          body += `<button class="btn btn-small btn-danger" onclick="protoAction('${p.name}','remove','${escapeHtml(p.title)}')">Удалить</button>`;
        }
      } else {
        body = `<div style="font-size:0.85rem;color:var(--text-muted);margin-bottom:0.5rem;">${escapeHtml(p.error || '')}</div>`;
        if (p.name !== 'amneziawg') {
          if (p.name === 'mtproxy' || p.name === 'telemt') {
            const defDomain = p.name === 'mtproxy' ? 'www.google.com' : 'www.microsoft.com';
            body += `<div style="margin-bottom:0.5rem;">
              <input type="text" id="proto-domain-${p.name}" placeholder="${defDomain}" style="padding:0.45rem 0.6rem;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);width:220px;max-width:100%;">
              <div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.25rem;">Маскировочный домен (пусто = ${defDomain}). Трафик будет выглядеть как HTTPS к этому сайту.</div>
            </div>
            <button class="btn btn-small btn-primary" onclick="protoInstallProxy('${p.name}','${escapeHtml(p.title)}')">Установить</button>`;
          } else {
            body += `<button class="btn btn-small btn-primary" onclick="protoAction('${p.name}','install','${escapeHtml(p.title)}')">Установить</button>`;
          }
        }
      }
      return `<div style="border:1px solid var(--border);border-radius:12px;padding:1rem;margin-bottom:0.75rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:0.5rem;flex-wrap:wrap;">
          <strong>${escapeHtml(p.title || p.name)}</strong>${badge}
        </div>${body}</div>`;
    }).join('');
  } catch(e) { box.innerHTML = `<div class="alert err">❌ ${escapeHtml(e.message)}</div>`; }
}

async function protoAction(name, action, title, domain) {
  if (action === 'remove' && !confirm(`Удалить ${title}?`)) return;
  if (action.startsWith('use-') && !confirm(`${title}?\nКлиентам нужно будет заново импортировать конфиг / vpn:// ссылку.`)) return;
  showLoader(action === 'install' ? `Установка ${title}... (может занять пару минут)` : `Выполнение: ${title}...`);
  try {
    const opts = {method:'POST', credentials:'same-origin'};
    if (domain) {
      opts.headers = {'Content-Type': 'application/json'};
      opts.body = JSON.stringify({domain});
    }
    const r = await fetch(BASE_URL + `/api/protocols/${action}/${name}`, opts);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
    if (action === 'install' && d.link) alert('✅ Установлено!\n\nСсылка для подключения:\n' + d.link);
    if (action.startsWith('use-')) alert('✅ Готово. Клиенты должны заново импортировать конфиг / vpn:// ссылку.');
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); loadProtocols(); }
}

function protoInstallProxy(name, title) {
  const domain = (document.getElementById(`proto-domain-${name}`).value || '').trim();
  if (domain && !/^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/i.test(domain)) {
    alert('Некорректный домен. Пример: www.microsoft.com'); return;
  }
  protoAction(name, 'install', title, domain);
}

// ==================== Telegram Notifications ====================
async function loadNotify() {
  try {
    const r = await fetch(BASE_URL + '/api/notify/status', {credentials:'same-origin'});
    if (!r.ok) return;
    const d = await r.json();
    const el = document.getElementById('notify-status');
    if (d.configured) {
      el.innerHTML = `<div class="alert ok">✅ Уведомления настроены. Бот: <code>${escapeHtml(d.token_masked)}</code>, chat_id: <code>${escapeHtml(d.chat_id)}</code></div>`;
      document.getElementById('tg-chat').value = d.chat_id || '';
    } else {
      el.innerHTML = `<div class="alert err">⚠️ Уведомления пока не настроены</div>`;
    }
  } catch(e) { console.error(e); }
}

async function saveNotify() {
  const token = document.getElementById('tg-token').value.trim();
  const chat_id = document.getElementById('tg-chat').value.trim();
  if (!token || !chat_id) { alert('Заполните токен и chat_id'); return; }
  showLoader('Сохранение настроек...');
  try {
    const r = await fetch(BASE_URL + '/api/notify/settings', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token, chat_id})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка сервера');
    document.getElementById('tg-token').value = '';
    await loadNotify();
  } catch(e) { alert('Ошибка: ' + e.message); }
  finally { hideLoader(); }
}

async function testNotify() {
  showLoader('Отправка тестового сообщения...');
  try {
    const r = await fetch(BASE_URL + '/api/notify/test', {method: 'POST', credentials: 'same-origin'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка отправки');
    alert('✅ Сообщение отправлено! Проверьте Telegram.');
  } catch(e) { alert('❌ ' + e.message); }
  finally { hideLoader(); }
}

// ==================== LAN Settings ====================
async function initLanStatus() {
  try {
    const r = await fetch(BASE_URL + '/api/lan/status', {credentials:'same-origin'});
    if (r.ok) {
        const d = await r.json();
        document.getElementById('lan-toggle').checked = d.enabled;
    }
  } catch(e) {}
}

async function toggleLanStatus() {
  const chk = document.getElementById('lan-toggle');
  const enable = chk.checked;
  
  showLoader('Применение правил маршрутизации...');
  try {
    const r = await fetch(BASE_URL + '/api/lan/toggle', {
        method: 'POST', credentials:'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: enable})
    });
    if (!r.ok) throw new Error('Ошибка сервера');
    
    if (enable) {
        document.getElementById('lan-modal').style.display = 'flex';
    }
  } catch (e) {
    chk.checked = !enable; // Откат UI
    alert('Ошибка при изменении сетевых настроек: ' + e.message);
  } finally {
    hideLoader();
  }
}

// ==================== Clients ====================
function setSort(col) {
  if (_currentSort === col) { _sortAsc = !_sortAsc; } 
  else { _currentSort = col; _sortAsc = true; }
  
  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.getAttribute('data-sort') === col) th.classList.add(_sortAsc ? 'sort-asc' : 'sort-desc');
  });
  applyFilters();
}

function applyFilters() {
  const query = document.getElementById('search-input').value.toLowerCase();
  const status = document.getElementById('status-filter').value;
  
  let filtered = _allClients.filter(c => {
    const matchQuery = c.name.toLowerCase().includes(query) || c.ip.includes(query);
    const matchStatus = status === 'all' || c.status === status;
    return matchQuery && matchStatus;
  });

  filtered.sort((a, b) => {
    let valA = a[_currentSort]; let valB = b[_currentSort];
    if (_currentSort === 'ip') {
        const arrA = (valA || '').split('.');
        const arrB = (valB || '').split('.');
        for (let i = 0; i < 4; i++) {
          const numA = parseInt(arrA[i], 10) || 0;
          const numB = parseInt(arrB[i], 10) || 0;
          if (numA !== numB) return _sortAsc ? numA - numB : numB - numA;
        }
        return 0;
    }
    if (typeof valA === 'string') valA = valA.toLowerCase();
    if (typeof valB === 'string') valB = valB.toLowerCase();
    if (valA < valB) return _sortAsc ? -1 : 1;
    if (valA > valB) return _sortAsc ? 1 : -1;
    return 0;
  });

  renderClients(filtered);
}

function renderClients(clients) {
  const tbody = document.getElementById('clients-tbody');
  const empty = document.getElementById('empty-state');
  document.getElementById('clients-count').textContent = _allClients.length;
  
  if (clients.length === 0) {
    tbody.innerHTML = ''; empty.style.display = 'block'; return;
  }
  empty.style.display = 'none';

  tbody.innerHTML = clients.map(c => {
    const statusClass = c.status === 'ONLINE' ? 'status-online' : (c.status === 'OFFLINE' ? 'status-offline' : 'status-never');
    const statusLabel = c.status === 'ONLINE' ? 'ОНЛАЙН' : (c.status === 'OFFLINE' ? 'ОФФЛАЙН' : 'НИКОГДА');
    const encName = encodeURIComponent(c.name);
    const safeName = escapeHtml(c.name);
    
    return `<tr>
      <td data-label="Имя"><strong>${safeName}</strong></td>
      <td data-label="IP">${escapeHtml(c.ip)}</td>
      <td data-label="Статус"><span class="status-badge ${statusClass}">${statusLabel}</span></td>
      <td data-label="RX">${escapeHtml(c.rx)}</td>
      <td data-label="TX">${escapeHtml(c.tx)}</td>
      <td data-label="Handshake">${c.handshake_ts > 0 ? formatDateTime(new Date(c.handshake_ts * 1000)) : 'никогда'}</td>
      <td data-label="Действия" class="actions-cell">
        <a href="${BASE_URL}/download/conf/${encName}" class="btn btn-small" title="Скачать .conf">.conf</a>
        <a href="${BASE_URL}/download/vpn/${encName}" class="btn btn-small" title="Amnezia Link">vpn://</a>
        <form method="post" action="${BASE_URL}/delete/${encName}" onsubmit="return confirm('Удалить клиента ${safeName}?');" style="margin:0;">
          <button type="submit" class="btn btn-small btn-danger" title="Удалить">×</button>
        </form>
      </td>
    </tr>`;
  }).join('');
  document.getElementById('last-update').textContent = formatDateTime(new Date());
}

async function fetchClients() {
  try {
    const r = await fetch(BASE_URL + '/api/clients', {credentials:'same-origin', cache:'no-store'});
    if (r.status === 401) { location.reload(); return; }
    if (r.ok) { _allClients = await r.json(); applyFilters(); }
  } catch (e) { console.error(e); }
}

// ==================== Servers ====================
async function loadServers() {
  try {
    const r = await fetch(BASE_URL + '/api/servers', {credentials:'same-origin'});
    if (!r.ok) return;
    _serversData = await r.json();
    const tbody = document.getElementById('servers-tbody');
    if (!_serversData.servers.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-muted);">Нет добавленных серверов</td></tr>';
      return;
    }
    tbody.innerHTML = _serversData.servers.map(s => {
      const isActive = s.id === _serversData.active_id;
      const statusHtml = isActive ? `<span class="status-badge status-online">Активный</span>` : `<span class="status-badge status-never">Ожидает</span>`;
      const safeName = escapeHtml(s.name);
      let actions = isActive 
        ? `<span style="color:var(--text-muted); font-size: 0.85rem;">Используется</span>`
        : `<button class="btn btn-small btn-primary" onclick="switchServer(${s.id}, '${safeName.replace(/'/g, "\\'")}')">Переключить</button>
           <button class="btn btn-small btn-danger" onclick="deleteServer(${s.id})">×</button>`;
      return `<tr><td data-label="Имя"><strong>${safeName}</strong></td><td data-label="IP">${escapeHtml(s.ip)}</td><td data-label="Статус">${statusHtml}</td><td data-label="Действия" style="text-align:right;">${actions}</td></tr>`;
    }).join('');
  } catch (e) { console.error(e); }
}

async function loadBackups() {
  const el = document.getElementById('backups-list');
  if (!el) return;
  try {
    const r = await fetch(BASE_URL + '/api/backups', {credentials:'same-origin'});
    if (!r.ok) { el.innerHTML = ''; return; }
    const list = await r.json();
    el.innerHTML = list.map(b =>
      `<div style="display:flex;justify-content:space-between;align-items:center;gap:0.5rem;padding:0.3rem 0;border-bottom:1px solid var(--border);">
        <span>${escapeHtml(b.date)} · ${escapeHtml(b.size)}</span>
        <a class="btn btn-small" href="${BASE_URL}/api/backup/download/${b.name}">⬇ Скачать</a>
      </div>`).join('') || '<span style="color:var(--text-muted);">Бэкапов пока нет</span>';
  } catch (e) { console.error(e); }
}

async function backupCreate() {
  showLoader('Создание бэкапа...');
  try {
    const r = await fetch(BASE_URL + '/api/backup/create', {method:'POST', credentials:'same-origin'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка');
    await loadBackups();
  } catch (e) { alert('❌ ' + e.message); }
  finally { hideLoader(); }
}

async function addServer() {
  const ip = document.getElementById('add-ip').value.trim();
  const port = parseInt(document.getElementById('add-port').value) || 22;
  const user = document.getElementById('add-user').value.trim() || 'root';
  const name = document.getElementById('add-name').value.trim() || ip;
  const pass = document.getElementById('add-pass').value;
  if (!ip || !pass) { alert('IP и пароль обязательны!'); return; }
  const btn = document.getElementById('add-server-btn');
  btn.disabled = true; btn.textContent = '⏳ Подключение...';
  try {
    const r = await fetch(BASE_URL + '/api/servers/add', {
      method: 'POST', credentials:'same-origin', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ip, port, user, password: pass, name})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Server error');
    document.getElementById('add-ip').value = ''; document.getElementById('add-pass').value = ''; document.getElementById('add-name').value = '';
    await loadServers();
  } catch(e) { alert('Ошибка: ' + e.message); } 
  finally { btn.disabled = false; btn.textContent = 'Проверить и добавить'; }
}

async function switchServer(sid, name) {
  if (!confirm(`Переключиться на сервер ${name}?\nВам нужно будет ввести его SSH-пароль.`)) return;
  const pass = prompt(`SSH-пароль для ${name}:`);
  if (!pass) return;
  showLoader(`Настройка туннеля на ${name}... Не закрывайте страницу!`);
  try {
    const r = await fetch(BASE_URL + `/api/servers/switch/${sid}`, {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pass})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Switch failed');
    alert('✅ Успешно! ' + (d.message || ''));
    await loadServers();
  } catch (e) { alert('❌ Ошибка: ' + e.message); } 
  finally { hideLoader(); }
}

async function deleteServer(sid) {
  if (!confirm('Удалить сервер из списка панели?')) return;
  await fetch(BASE_URL + `/api/servers/${sid}`, {method: 'DELETE', credentials: 'same-origin'});
  await loadServers();
}

function restartTunnels() {
  if (!confirm('Перезапустить туннели awg0 и awg1? VPN соединение прервется на пару секунд.')) return;
  const f = document.createElement('form'); f.method = 'POST'; f.action = BASE_URL + '/restart';
  document.body.appendChild(f); f.submit();
}

async function checkUpdates() {
  showLoader('Проверка обновлений...');
  try {
    const r = await fetch(BASE_URL + '/api/check_update', {credentials: 'same-origin'});
    if (r.status === 401) { location.reload(); return; }
    const d = await r.json();
    hideLoader();
    if (d.error) { alert('Ошибка: ' + d.error); }
    else if (!d.has_updates) { alert('У вас установлена последняя версия.'); }
    else {
      document.getElementById('update-list').innerHTML = d.commits.map(c => 
        `<div class="update-item"><div style="color:var(--text-muted);font-size:0.8rem;">${formatDateTime(c.date)}</div>${escapeHtml(c.message)}</div>`
      ).join('');
      document.getElementById('update-modal').style.display = 'flex';
    }
  } catch(e) { hideLoader(); alert(e); }
}

function startPolling() {
  if (_pollInterval) clearInterval(_pollInterval);
  fetchClients();
  _pollInterval = setInterval(fetchClients, 5000);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearInterval(_pollInterval); 
  else startPolling();
});

initTheme();
initLanStatus();
document.querySelector('th[data-sort="ip"]').classList.add('sort-asc');
setSort('ip');
startPolling();
// Бейдж с числом заявок на регистрации — обновляем в фоне
async function pollPendingUsers() {
  try {
    const r = await fetch(BASE_URL + '/api/users', {credentials:'same-origin'});
    if (!r.ok) return;
    const list = await r.json();
    const pending = list.filter(u => u.status === 'pending').length;
    document.getElementById('users-pending').textContent = pending ? `(${pending} ⏳)` : '';
  } catch(e) {}
}
pollPendingUsers();
setInterval(pollPendingUsers, 15000);

</script>
</body>
</html>"""

def render_login(error=""):
    err = f'<div class="error-msg">{error}</div>' if error else ""
    return LOGIN_TEMPLATE.replace("__ERR__", err).replace("__LABEL__", SERVER_LABEL).replace("__BASE__", BASE_URL)

def render_page(clients, message="", error=""):
    msg = f'<div class="alert ok">✅ {message}</div>' if message else ""
    err = f'<div class="alert err">❌ {error}</div>' if error else ""
    trx, ttx = total_traffic(clients)
    # Управление выходным шлюзом (вкладка «Серверы») есть только в каскаде
    servers_tab = '<button class="tab" onclick="switchTab(\'servers\')">🌐 Серверы</button>' if INSTALL_MODE == "3" else ""
    proto_host = "SERVER-B" if REMOTE_MODE else "этом сервере"
    # В режиме 0 без установленного AmneziaWG вкладка клиентов — с подсказкой
    awg_hint = ""
    if INSTALL_MODE == "0" and not os.path.exists(AWG_CONF):
        awg_hint = ('<div class="alert err">AmneziaWG пока не установлен — '
                    'установите его кнопкой во вкладке «🧩 Протоколы», затем здесь появятся клиенты.</div>')
    return (PAGE_TEMPLATE
            .replace("__MSG__", msg)
            .replace("__ERR__", err)
            .replace("__LABEL__", f"{SERVER_LABEL} · {MODE_LABEL}")
            .replace("__SERVERS_TAB__", servers_tab)
            .replace("__PROTO_HOST__", proto_host)
            .replace("__AWG_HINT__", awg_hint)
            .replace("__COUNT__", str(len(clients)))
            .replace("__TOTAL_RX__", trx)
            .replace("__TOTAL_TX__", ttx)
            .replace("__BASE__", BASE_URL))

# ================ Routes ================
app = FastAPI(docs_url=None, redoc_url=None)

@app.exception_handler(RedirectToLogin)
async def _rtl(request: Request, exc: RedirectToLogin):
    return RedirectResponse(f"{BASE_URL}/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    try:
        _, p, ph = load_credentials()
        if check_token(request.cookies.get(SESSION_COOKIE, ""), ph or p):
            return RedirectResponse(f"{BASE_URL}/", status_code=303)
    except RuntimeError:
        pass
    return render_login(error)

@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    if _login_blocked(ip):
        return RedirectResponse(
            f"{BASE_URL}/login?error=Слишком+много+попыток.+Подождите+10+минут", status_code=303)
    try:
        u, p, ph = load_credentials()
    except RuntimeError:
        return RedirectResponse(f"{BASE_URL}/login?error=Config+Error", status_code=303)

    if not (hmac.compare_digest(username, u) and verify_password(password, p, ph)):
        _register_login_fail(ip, username)
        await asyncio.sleep(1)
        return RedirectResponse(f"{BASE_URL}/login?error=Неверный+логин+или+пароль", status_code=303)

    _clear_login_fails(ip)
    r = RedirectResponse(f"{BASE_URL}/", status_code=303)
    r.set_cookie(SESSION_COOKIE, make_token(ph or p, int(time.time())), max_age=SESSION_DAYS*86400,
                 httponly=True, secure=True, samesite="lax", path="/")
    return r

@app.get("/logout")
async def logout():
    r = RedirectResponse(f"{BASE_URL}/login", status_code=303)
    r.delete_cookie(SESSION_COOKIE, path="/")
    return r

def _notify(msg):
    try:
        if os.path.exists(NOTIFY_SCRIPT):
            subprocess.run([NOTIFY_SCRIPT, msg], timeout=10, capture_output=True)
    except Exception:
        pass

# ================ Telegram-уведомления (настройка из панели) ================
def load_notify_conf():
    cfg = {}
    if os.path.exists(NOTIFY_CONF):
        try:
            with open(NOTIFY_CONF, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        cfg[k.strip()] = v.strip().strip('"')
        except Exception:
            pass
    return cfg

def tg_send_buttons(text: str, rows: list):
    """Сообщение админу с inline-кнопками (например, одобрение заявки)."""
    cfg = load_notify_conf()
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = re.split(r"[,\s]+", cfg.get("TELEGRAM_CHAT_ID", ""))[0]
    if not (token and chat_id):
        return
    try:
        import urllib.parse
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False),
        }).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass

@app.get("/api/notify/status")
async def api_notify_status(request: Request):
    current_user(request)
    cfg = load_notify_conf()
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = cfg.get("TELEGRAM_CHAT_ID", "")
    masked = (token[:5] + "…" + token[-4:]) if len(token) > 12 else ("*" * len(token))
    return {"configured": bool(token and chat_id), "token_masked": masked, "chat_id": chat_id}

@app.post("/api/notify/settings")
async def api_notify_save(request: Request):
    current_user(request)
    body = await request.json()
    token = str(body.get("token", "")).strip()
    chat_id = str(body.get("chat_id", "")).strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        return JSONResponse({"error": "Некорректный формат токена (ожидается 123456:ABC-DEF...)"}, status_code=400)
    if not re.fullmatch(r"-?\d{5,}", chat_id):
        return JSONResponse({"error": "Некорректный chat_id (ожидается число)"}, status_code=400)
    try:
        with open(NOTIFY_CONF, "w", encoding="utf-8") as f:
            f.write(f'TELEGRAM_BOT_TOKEN="{token}"\nTELEGRAM_CHAT_ID="{chat_id}"\n')
        os.chmod(NOTIFY_CONF, 0o600)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    # После настройки уведомлений запускаем/перезапускаем бота управления
    if os.path.exists("/etc/systemd/system/amnezia-tg-bot.service"):
        await run_in_threadpool(
            srv_exec, "systemctl enable amnezia-tg-bot >/dev/null 2>&1; systemctl restart amnezia-tg-bot", 30)
    _notify("🔔 <b>Telegram-уведомления</b>\nНастройки обновлены через веб-панель.")
    return {"ok": True}

@app.post("/api/notify/test")
async def api_notify_test(request: Request):
    current_user(request)
    cfg = load_notify_conf()
    if not (cfg.get("TELEGRAM_BOT_TOKEN") and cfg.get("TELEGRAM_CHAT_ID")):
        return JSONResponse({"error": "Сначала сохраните токен и chat_id"}, status_code=400)
    if not os.path.exists(NOTIFY_SCRIPT):
        return JSONResponse({"error": "amnezia-notify.sh не установлен на сервере"}, status_code=500)
    msg = (f"✅ <b>AmneziaWG Panel</b>\nТестовое сообщение.\n"
           f"Хост: <code>{os.uname().nodename}</code>\n"
           f"Время: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    r = await run_in_threadpool(
        lambda: subprocess.run([NOTIFY_SCRIPT, msg], capture_output=True, text=True, timeout=15))
    if r.returncode != 0:
        return JSONResponse({"error": (r.stderr or r.stdout or "send failed").strip()[:300]},
                            status_code=500)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    ensure_current_server_in_list()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, message: str = "", error: str = ""):
    current_user(request)
    return render_page([], message, error)

_clients_cache = {"ts": 0.0, "data": []}
CLIENTS_CACHE_TTL = 3.0

@app.get("/api/clients")
async def api_clients(request: Request):
    current_user(request)
    now = time.time()
    if now - _clients_cache["ts"] < CLIENTS_CACHE_TTL:
        return JSONResponse(_clients_cache["data"])
    data = await run_in_threadpool(lambda: enrich(parse_clients()))
    _clients_cache.update(ts=now, data=data)
    return JSONResponse(data)

# --- API протоколов (amneziawg — основной; остальные — дополнительные) ---
PROTOCOLS = ["amneziawg", "amnezia-dns", "mtproxy", "telemt"]
# Дополнительные действия протоколов помимо install/remove
PROTO_EXTRA_ACTIONS = {"amnezia-dns": ("use-for-clients", "use-cloudflare", "adblock-on", "adblock-off"),
                       "amneziawg": ("update",),
                       "mtproxy": ("restart",),
                       "telemt": ("restart",)}
# Переменная окружения маскировочного домена для каждого прокси-протокола
PROTO_DOMAIN_ENV = {"mtproxy": "MTG_DOMAIN", "telemt": "TELEMT_DOMAIN"}
PROTO_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.I)
# Основной протокол ставится deploy.sh — из панели его нельзя установить/удалить
PROTO_CORE = ("amneziawg",)

def _proto_status(name):
    ok, out, err = srv_exec(f"/usr/local/bin/amnezia-proto-{name}.sh status", timeout=15)
    if not ok and not (out or "").strip():
        return {"name": name, "title": name, "installed": False,
                "error": (err or "недоступно").strip()[:200]}
    info = {"name": name, "installed": False}
    for line in (out or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip().strip('"')
    info["installed"] = info.get("installed") == "1"
    return info

@app.get("/api/protocols")
async def api_protocols(request: Request):
    current_user(request)
    result = await run_in_threadpool(lambda: [_proto_status(p) for p in PROTOCOLS])
    return JSONResponse(result)

@app.post("/api/protocols/{action}/{name}")
async def api_proto_action(request: Request, action: str, name: str):
    current_user(request)
    allowed = PROTO_EXTRA_ACTIONS.get(name, ()) + (() if name in PROTO_CORE else ("install", "remove"))
    if name not in PROTOCOLS or action not in allowed:
        return JSONResponse({"error": "Неизвестная операция"}, status_code=400)
    env_prefix = ""
    if action == "install" and name in PROTO_DOMAIN_ENV:
        try:
            body = await request.json()
        except Exception:
            body = {}
        domain = str(body.get("domain") or "").strip().lower()
        if domain:
            if len(domain) > 253 or not PROTO_DOMAIN_RE.fullmatch(domain):
                return JSONResponse({"error": "Некорректный домен (пример: www.microsoft.com)"}, status_code=400)
            env_prefix = f"{PROTO_DOMAIN_ENV[name]}={domain} "
    ok, out, err = await run_in_threadpool(
        srv_exec, f"{env_prefix}/usr/local/bin/amnezia-proto-{name}.sh {action}", 600)
    if not ok:
        tail = ((err or "") + (out or "")).strip()[-400:]
        return JSONResponse({"error": tail or "Ошибка операции"}, status_code=500)
    _notify(f"🧩 <b>Протокол {name}</b>\nОперация: {action} — OK")
    if action in ("install", "remove"):
        _backup_async()
    link = ""
    if action == "install":
        link = _proto_status(name).get("link", "")
    return {"ok": True, "link": link}

# --- Бэкапы SERVER-A ---
BACKUP_DIR = "/root/amnezia-backups"

def _list_backups():
    items = []
    try:
        names = [f for f in os.listdir(BACKUP_DIR)
                 if re.fullmatch(r"amnezia-backup-[\d-]+\.tar\.gz", f)]
    except FileNotFoundError:
        return []
    for f in sorted(names, reverse=True):
        path = os.path.join(BACKUP_DIR, f)
        st = os.stat(path)
        items.append({"name": f,
                      "size": f"{st.st_size / 1024 / 1024:.1f} МБ",
                      "date": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")})
    return items

def _backup_async():
    """Фоновый бэкап после изменений (клиенты, протоколы)."""
    try:
        subprocess.Popen(["/usr/local/bin/amnezia-backup.sh"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

@app.get("/api/backups")
async def api_backups(request: Request):
    current_user(request)
    return JSONResponse(await run_in_threadpool(_list_backups))

@app.post("/api/backup/create")
async def api_backup_create(request: Request):
    current_user(request)
    ok, out, err = await run_in_threadpool(srv_exec, "/usr/local/bin/amnezia-backup.sh", 300)
    if not ok:
        tail = ((err or "") + (out or "")).strip()[-300:]
        return JSONResponse({"error": tail or "Ошибка бэкапа"}, status_code=500)
    _notify("💾 <b>Бэкап создан</b> (вручную из панели)")
    return {"ok": True}

@app.get("/api/backup/download/{fname}")
async def api_backup_download(request: Request, fname: str):
    current_user(request)
    if not re.fullmatch(r"amnezia-backup-[\d-]+\.tar\.gz", fname):
        raise HTTPException(400, "Некорректное имя файла")
    path = os.path.join(BACKUP_DIR, fname)
    if not os.path.isfile(path):
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path, media_type="application/gzip", filename=fname)

# --- API Локальной Сети (LAN) ---
@app.get("/api/lan/status")
async def api_lan_status(request: Request):
    current_user(request)
    enabled = False
    try:
        if REMOTE_MODE:
            content = srv_read(AWG_CONF) or ""
        else:
            with open(AWG_CONF, "r", encoding="utf-8") as f:
                content = f.read()
        enabled = "iptables -I FORWARD -i awg1 -o awg1 -j ACCEPT" in content
    except Exception:
        pass
    return JSONResponse({"enabled": enabled})

def _apply_lan_rules(enable: bool):
    """Применяет LAN-правила на лету, без перезапуска awg1 (клиенты не отключаются)."""
    subnet = f"{CAS['CLIENT_SUBNET']}.0/24"
    rule = "FORWARD -i awg1 -o awg1 -j ACCEPT"
    if enable:
        script = (f"iptables -C {rule} 2>/dev/null || iptables -I {rule}; "
                  f"ip route add {subnet} dev awg1 table 100 2>/dev/null || true")
    else:
        script = (f"while iptables -D {rule} 2>/dev/null; do :; done; "
                  f"ip route del {subnet} dev awg1 table 100 2>/dev/null || true")
    srv_exec(script, timeout=20)

@app.post("/api/lan/toggle")
async def api_lan_toggle(request: Request):
    current_user(request)
    body = await request.json()
    enable = bool(body.get("enabled", False))

    subnet = f"{CAS['CLIENT_SUBNET']}.0/24"
    lan_rules = [
        "PostUp = iptables -I FORWARD -i awg1 -o awg1 -j ACCEPT\n",
        f"PostUp = ip route add {subnet} dev awg1 table 100 || true\n",
        "PostDown = iptables -D FORWARD -i awg1 -o awg1 -j ACCEPT\n",
        f"PostDown = ip route del {subnet} dev awg1 table 100 || true\n"
    ]

    try:
        if REMOTE_MODE:
            content = srv_read(AWG_CONF)
            if content is None:
                return JSONResponse({"error": "SERVER-B недоступен по SSH"}, status_code=502)
        else:
            with open(AWG_CONF, "r", encoding="utf-8") as f:
                content = f.read()
        lines = content.splitlines(keepends=True)

        # Очищаем конфиг от этих правил на случай, если они уже есть
        clean_lines = [l for l in lines if l not in lan_rules]

        if enable:
            # Вставляем правила перед секцией [Peer] или в конец блока Interface
            insert_idx = len(clean_lines)
            for i, line in enumerate(clean_lines):
                if line.strip().startswith("[Peer]"):
                    insert_idx = i
                    break
            clean_lines = clean_lines[:insert_idx] + lan_rules + clean_lines[insert_idx:]

        if not srv_write(AWG_CONF, "".join(clean_lines)):
            return JSONResponse({"error": "Не удалось записать конфиг"}, status_code=500)

        # Правила применяются напрямую — перезапуск интерфейса не нужен
        await run_in_threadpool(_apply_lan_rules, enable)

        state_str = "ВКЛЮЧЕНА" if enable else "ВЫКЛЮЧЕНА"
        _notify(f"🌐 <b>Локальная сеть (LAN)</b>\nСвязь между клиентами {state_str}")

        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
# ---------------------------------

@app.get("/api/check_update")
async def api_check_update(request: Request):
    current_user(request)
    result = await run_in_threadpool(check_github_updates)
    return JSONResponse(result)

@app.get("/api/servers")
async def api_servers_list(request: Request):
    current_user(request)
    data = load_servers()
    return {"active_id": data.get("active_id"), "servers": data.get("servers", [])}

def _add_server_sync(body):
    ip = body.get("ip", "").strip()
    port = int(body.get("port", 22))
    user = body.get("user", "root").strip()
    password = body.get("password", "")
    name = body.get("name", "").strip() or ip
    if not ip or not password:
        return {"error": "IP и пароль обязательны"}
    ok, out, err = ssh_exec(ip, port, user, password, "echo SSH_OK", timeout=20)
    if not ok or "SSH_OK" not in (out or ""):
        return {"error": f"SSH: {err or out or 'timeout'}"}
    data = load_servers()
    for s in data["servers"]:
        if s["ip"] == ip and s["port"] == port:
            return {"error": "Такой сервер уже добавлен"}
    new_id = max([s["id"] for s in data["servers"]] or [0]) + 1
    server = {"id": new_id, "ip": ip, "port": port, "user": user, "name": name}
    data["servers"].append(server)
    save_servers(data)
    return {"ok": True, "server": server}

def _cascade_only():
    if INSTALL_MODE != "3":
        return JSONResponse({"error": "Управление выходным шлюзом доступно только в каскадном режиме (INSTALL_MODE=3)"},
                            status_code=400)
    return None

@app.post("/api/servers/add")
async def api_servers_add(request: Request):
    current_user(request)
    guard = _cascade_only()
    if guard: return guard
    body = await request.json()
    result = await run_in_threadpool(_add_server_sync, body)
    if "error" in result: return JSONResponse(result, status_code=400)
    return result

@app.delete("/api/servers/{sid}")
async def api_servers_del(request: Request, sid: int):
    current_user(request)
    data = load_servers()
    if data.get("active_id") == sid: return JSONResponse({"error": "Нельзя удалить активный сервер"}, status_code=400)
    data["servers"] = [s for s in data["servers"] if s["id"] != sid]
    save_servers(data)
    return {"ok": True}

@app.post("/api/servers/switch/{sid}")
async def api_servers_switch(request: Request, sid: int):
    current_user(request)
    guard = _cascade_only()
    if guard: return guard
    try: body = await request.json(); password = body.get("password", "")
    except Exception: password = ""
    if not password: return JSONResponse({"error": "Требуется SSH-пароль"}, status_code=400)
    data = load_servers()
    server = next((s for s in data["servers"] if s["id"] == sid), None)
    if not server: return JSONResponse({"error": "Сервер не найден"}, status_code=404)
    if data.get("active_id") == sid: return JSONResponse({"error": "Этот сервер уже активен"}, status_code=400)
    
    ok, msg = await run_in_threadpool(do_switch_server, server, password)
    if ok:
        _notify(f"🌐 <b>Переключение SERVER-B</b>\nНа: <code>{server['ip']}</code> ({server['name']})\nРезультат: OK")
        return {"ok": True, "message": msg}
    else:
        _notify(f"❌ <b>Ошибка переключения SERVER-B</b>\nНа: <code>{server['ip']}</code>\n{msg[:300]}")
        return JSONResponse({"error": msg}, status_code=500)

@app.post("/create")
async def create_client(request: Request, name: str = Form(...)):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    if not n: return RedirectResponse(f"{BASE_URL}/?error=Недопустимое+имя+клиента", status_code=303)
    ok, out, err = await run_in_threadpool(
        srv_exec, f"printf 'y\\n' | {ADD_SCRIPT} {shlex.quote(n)}", 120)
    _clients_cache["ts"] = 0.0
    if ok:
        _notify(f"👤 <b>Новый клиент</b> (через панель)\nИмя: <code>{n}</code>")
        _backup_async()
        return RedirectResponse(f"{BASE_URL}/?message=Клиент+{n}+создан", status_code=303)
    tail = ((err or "") + (out or "")).strip()[:200] or "error"
    return RedirectResponse(f"{BASE_URL}/?error={tail.replace(' ','+').replace(chr(10),' ')}", status_code=303)

@app.post("/delete/{name}")
async def delete_client(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    ok, out, err = await run_in_threadpool(
        srv_exec, f"printf 'y\\n' | {REMOVE_SCRIPT} {shlex.quote(n)}", 90)
    _clients_cache["ts"] = 0.0
    if ok:
        _notify(f"🗑️ <b>Клиент удалён</b> (через панель)\nИмя: <code>{n}</code>")
        _backup_async()
        return RedirectResponse(f"{BASE_URL}/?message=Клиент+{n}+удалён", status_code=303)
    tail = ((err or "") + (out or "")).strip()[:200] or "error"
    return RedirectResponse(f"{BASE_URL}/?error={tail.replace(' ','+').replace(chr(10),' ')}", status_code=303)

@app.post("/restart")
async def restart_tunnels(request: Request):
    current_user(request)
    # awg0 (туннель A<->B) существует только в каскаде; клиентский awg1 — там, где протоколы
    ifaces = "awg0 awg1" if INSTALL_MODE == "3" else "awg1"
    ok, out, err = await run_in_threadpool(
        srv_exec, f"systemctl restart awg-quick@{ifaces.replace(' ', ' awg-quick@')}", 60)
    msg = f"{ifaces}: {'OK' if ok else 'FAIL'}"
    _notify(f"🔄 <b>Перезапуск</b>\n{msg}")
    return RedirectResponse(f"{BASE_URL}/?message={msg.replace(' ','+')}", status_code=303)

def _download_remote_or_local(path, filename):
    if REMOTE_MODE:
        content = srv_read(path)
        if content is None: raise HTTPException(404, "Файл не найден (или SERVER-B недоступен)")
        return Response(content=content, media_type="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    if not os.path.isfile(path): raise HTTPException(404, "Файл не найден")
    return FileResponse(path, media_type="text/plain", filename=filename)

@app.get("/download/conf/{name}")
async def dl_conf(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    return _download_remote_or_local(os.path.join(CLIENTS_DIR, f"{n}.conf"), f"{n}.conf")

@app.get("/download/vpn/{name}")
async def dl_vpn(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    return _download_remote_or_local(os.path.join(CLIENTS_DIR, f"{n}.vpn.txt"), f"{n}.vpn.txt")

# ================ Клиентский портал: регистрация, вход, кабинет ================
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")
EMAIL_RE    = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[a-zA-Z]{2,}$")
DEVICE_LABEL_RE = re.compile(r"^[0-9a-zA-Zа-яА-ЯёЁ _-]{1,24}$")

def render_user_page(title, body, error="", ok=""):
    err = f'<div class="error-msg">{error}</div>' if error else ""
    okh = f'<div class="ok-msg">{ok}</div>' if ok else ""
    return (USER_AUTH_TEMPLATE.replace("__TITLE__", title).replace("__ERR__", err)
            .replace("__OK__", okh).replace("__BODY__", body).replace("__LABEL__", SERVER_LABEL)
            .replace("__BASE__", BASE_URL))

REGISTER_BODY = """<form method="post" action="__BASE__/register">
  <div class="input-group"><input type="text" name="username" placeholder="Логин (латиница, 3-32 символа)" required autocomplete="username"></div>
  <div class="input-group"><input type="email" name="email" placeholder="Email для уведомлений" required autocomplete="email"></div>
  <div class="input-group"><input type="password" name="password" placeholder="Пароль (минимум 8 символов)" required autocomplete="new-password"></div>
  <div class="input-group"><input type="password" name="password2" placeholder="Повторите пароль" required autocomplete="new-password"></div>
  <button type="submit">Зарегистрироваться</button>
</form>
<div class="links">Уже есть аккаунт? <a href="__BASE__/ulogin">Войти</a></div>"""

ULOGIN_BODY = """<form method="post" action="__BASE__/ulogin">
  <div class="input-group"><input type="text" name="username" placeholder="Логин" required autofocus autocomplete="username"></div>
  <div class="input-group"><input type="password" name="password" placeholder="Пароль" required autocomplete="current-password"></div>
  <button type="submit">Войти</button>
</form>
<div class="links">Нет аккаунта? <a href="__BASE__/register">Регистрация</a></div>"""

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = "", ok: str = ""):
    return render_user_page("Регистрация", REGISTER_BODY.replace("__BASE__", BASE_URL), error, ok)

@app.post("/register")
async def register_submit(request: Request, username: str = Form(...), email: str = Form(...),
                          password: str = Form(...), password2: str = Form(...)):
    username = username.strip()
    email = email.strip().lower()
    def fail(msg):
        return RedirectResponse(f"{BASE_URL}/register?error={urllib.parse.quote_plus(msg)}", status_code=303)
    if not USERNAME_RE.fullmatch(username):
        return fail("Логин: латиница/цифры/_/-, 3-32 символа")
    if not EMAIL_RE.fullmatch(email):
        return fail("Некорректный email")
    if len(password) < 8:
        return fail("Пароль должен быть не короче 8 символов")
    if password != password2:
        return fail("Пароли не совпадают")
    try:
        admin_user, _, _ = load_credentials()
        if username.lower() == admin_user.lower():
            return fail("Этот логин занят")
    except RuntimeError:
        pass
    conn = users_db()
    exists = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
    if exists:
        conn.close()
        return fail("Этот логин занят")
    conn.execute("INSERT INTO users(username,email,pass_hash,status,created_ts) VALUES(?,?,?,?,?)",
                 (username, email, hash_password(password), "pending", int(time.time())))
    conn.commit()
    conn.close()
    tg_send_buttons(
        f"🆕 <b>Заявка на регистрацию</b>\nЛогин: <code>{username}</code>\nEmail: <code>{email}</code>",
        [[{"text": "✅ Одобрить", "callback_data": f"ureg:ok:{username}"},
          {"text": "❌ Отклонить", "callback_data": f"ureg:no:{username}"}]])
    _notify(f"🆕 <b>Заявка на регистрацию</b>\nЛогин: <code>{username}</code>\nОдобрите во вкладке «Пользователи» или кнопкой выше.")
    return RedirectResponse(
        f"{BASE_URL}/register?ok={urllib.parse.quote_plus('Заявка отправлена! После одобрения администратором вы сможете войти в личный кабинет.')}",
        status_code=303)

@app.get("/ulogin", response_class=HTMLResponse)
async def ulogin_page(request: Request, error: str = ""):
    if check_user_token(request.cookies.get(USER_COOKIE, "")):
        return RedirectResponse(f"{BASE_URL}/cabinet", status_code=303)
    return render_user_page("Вход в личный кабинет", ULOGIN_BODY.replace("__BASE__", BASE_URL), error)

@app.post("/ulogin")
async def ulogin_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    if _login_blocked(ip):
        return RedirectResponse(
            f"{BASE_URL}/ulogin?error=Слишком+много+попыток.+Подождите+10+минут", status_code=303)
    u = get_user(username.strip())
    ok = False
    if u:
        try:
            _, salt, hexd = u["pass_hash"].split(":", 2)
            ok = hmac.compare_digest(hashlib.sha256((salt + password).encode()).hexdigest(), hexd)
        except ValueError:
            ok = False
    if not ok:
        _register_login_fail(ip, username)
        await asyncio.sleep(1)
        return RedirectResponse(f"{BASE_URL}/ulogin?error=Неверный+логин+или+пароль", status_code=303)
    if u["status"] == "pending":
        return RedirectResponse(
            f"{BASE_URL}/ulogin?error={urllib.parse.quote_plus('Заявка ещё на рассмотрении у администратора')}",
            status_code=303)
    if u["status"] != "approved":
        return RedirectResponse(
            f"{BASE_URL}/ulogin?error={urllib.parse.quote_plus('Заявка отклонена. Свяжитесь с администратором')}",
            status_code=303)
    _clear_login_fails(ip)
    r = RedirectResponse(f"{BASE_URL}/cabinet", status_code=303)
    r.set_cookie(USER_COOKIE, make_user_token(u["username"], int(time.time())), max_age=SESSION_DAYS*86400,
                 httponly=True, secure=True, samesite="lax", path="/")
    return r

@app.get("/ulogout")
async def ulogout():
    r = RedirectResponse(f"{BASE_URL}/ulogin", status_code=303)
    r.delete_cookie(USER_COOKIE, path="/")
    return r

@app.exception_handler(RedirectToUserLogin)
async def _rtul(request: Request, exc: RedirectToUserLogin):
    return RedirectResponse(f"{BASE_URL}/ulogin", status_code=303)

@app.get("/cabinet", response_class=HTMLResponse)
async def cabinet_page(request: Request):
    username = current_client(request)
    return (CABINET_TEMPLATE.replace("__USERNAME__", username)
            .replace("__LABEL__", SERVER_LABEL).replace("__BASE__", BASE_URL))

# --- общий хелпер создания/удаления конфига-устройства ---
def device_client_name(owner: str, label: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", label).strip("_")[:24] or "dev"
    return f"{owner}-{slug}"

def create_device(owner: str, label: str) -> tuple:
    """(ok, client_name_or_error)"""
    label = label.strip()
    if not DEVICE_LABEL_RE.fullmatch(label):
        return False, "Название: буквы/цифры/пробел/_/-, до 24 символов"
    conn = users_db()
    cnt = conn.execute("SELECT COUNT(*) FROM devices WHERE owner=?", (owner,)).fetchone()[0]
    if cnt >= MAX_DEVICES_PER_USER:
        conn.close()
        return False, f"Лимит: не более {MAX_DEVICES_PER_USER} устройств"
    client_name = device_client_name(owner, label)
    if conn.execute("SELECT 1 FROM devices WHERE client_name=?", (client_name,)).fetchone() \
            or os.path.exists(os.path.join(CLIENTS_DIR, f"{client_name}.conf")):
        conn.close()
        return False, "Устройство с таким названием уже есть — выберите другое"
    conn.execute("INSERT INTO devices(client_name,owner,label,created_ts) VALUES(?,?,?,?)",
                 (client_name, owner, label, int(time.time())))
    conn.commit()
    conn.close()
    ok, out, err = srv_exec(f"printf 'y\\n' | {ADD_SCRIPT} {shlex.quote(client_name)}", 120)
    if not ok:
        conn = users_db()
        conn.execute("DELETE FROM devices WHERE client_name=?", (client_name,))
        conn.commit()
        conn.close()
        return False, ((err or "") + (out or "")).strip()[-200:] or "Ошибка создания конфига"
    _clients_cache["ts"] = 0.0
    _backup_async()
    return True, client_name

def delete_device(client_name: str, owner: str = "") -> tuple:
    """(ok, error). owner задан — проверяем владельца (кабинет)."""
    conn = users_db()
    row = conn.execute("SELECT owner FROM devices WHERE client_name=?", (client_name,)).fetchone()
    if not row:
        conn.close()
        return False, "Конфиг не найден"
    if owner and row[0] != owner:
        conn.close()
        return False, "Это не ваш конфиг"
    conn.execute("DELETE FROM devices WHERE client_name=?", (client_name,))
    conn.commit()
    conn.close()
    ok, out, err = srv_exec(f"printf 'y\\n' | {REMOVE_SCRIPT} {shlex.quote(client_name)}", 90)
    _clients_cache["ts"] = 0.0
    _backup_async()
    if not ok:
        return False, ((err or "") + (out or "")).strip()[-200:] or "Ошибка удаления"
    return True, ""

def _devices_status_map():
    """client_name -> (status, ip) по живому dump и конфигу."""
    live = get_live_dump()
    peers = {}
    try:
        if not REMOTE_MODE and os.path.exists(AWG_CONF):
            with open(AWG_CONF, encoding="utf-8") as f:
                content = f.read()
        else:
            content = srv_read(AWG_CONF) or ""
        for block in re.split(r"\n(?=\[Peer\])", content):
            nm = re.search(r"#\s*Client:\s*(\S+)", block)
            pk = re.search(r"PublicKey\s*=\s*(\S+)", block)
            ip = re.search(r"AllowedIPs\s*=\s*([0-9.]+)", block)
            if nm and pk:
                peers[nm.group(1)] = (pk.group(1), ip.group(1) if ip else "")
    except Exception:
        pass
    now = time.time()
    result = {}
    for name, (pk, ip) in peers.items():
        hs = live.get(pk, {}).get("hs", 0)
        status = "ONLINE" if hs and now - hs <= 180 else ("OFFLINE" if hs else "NEVER")
        result[name] = (status, ip)
    return result

@app.get("/api/cabinet/devices")
async def api_cabinet_devices(request: Request):
    username = current_client(request)
    conn = users_db()
    rows = conn.execute(
        "SELECT client_name, label, created_ts FROM devices WHERE owner=? ORDER BY created_ts",
        (username,)).fetchall()
    conn.close()
    statuses = await run_in_threadpool(_devices_status_map)
    return JSONResponse([
        {"client_name": r[0], "label": r[1], "created_ts": r[2],
         "status": statuses.get(r[0], ("NEVER", ""))[0], "ip": statuses.get(r[0], ("", ""))[1]}
        for r in rows])

@app.post("/api/cabinet/devices/add")
async def api_cabinet_add(request: Request):
    username = current_client(request)
    body = await request.json()
    ok, res = await run_in_threadpool(create_device, username, str(body.get("label", "")))
    if not ok:
        return JSONResponse({"error": res}, status_code=400)
    _notify(f"📱 <b>Новое устройство</b> (кабинет)\nПользователь: <code>{username}</code>\nКонфиг: <code>{res}</code>")
    return {"ok": True, "client_name": res}

@app.post("/api/cabinet/devices/del")
async def api_cabinet_del(request: Request):
    username = current_client(request)
    body = await request.json()
    client_name = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("client_name", "")))
    ok, err = await run_in_threadpool(delete_device, client_name, username)
    if not ok:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}

def _check_device_owner(request: Request, client_name: str) -> str:
    username = current_client(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", client_name)
    conn = users_db()
    row = conn.execute("SELECT owner FROM devices WHERE client_name=?", (n,)).fetchone()
    conn.close()
    if not row or row[0] != username:
        raise HTTPException(403, "Это не ваш конфиг")
    return n

@app.get("/cabinet/dl/conf/{client_name}")
async def cabinet_dl_conf(request: Request, client_name: str):
    n = _check_device_owner(request, client_name)
    return _download_remote_or_local(os.path.join(CLIENTS_DIR, f"{n}.conf"), f"{n}.conf")

@app.get("/cabinet/dl/vpn/{client_name}")
async def cabinet_dl_vpn(request: Request, client_name: str):
    n = _check_device_owner(request, client_name)
    return _download_remote_or_local(os.path.join(CLIENTS_DIR, f"{n}.vpn.txt"), f"{n}.vpn.txt")

# ================ Админка: управление пользователями ================
def _list_users():
    conn = users_db()
    users = [dict(zip(("username", "email", "status", "created_ts"), r))
             for r in conn.execute(
                 "SELECT username, email, status, created_ts FROM users ORDER BY created_ts DESC").fetchall()]
    devs = conn.execute("SELECT client_name, owner, label, created_ts FROM devices ORDER BY created_ts").fetchall()
    conn.close()
    by_owner = {}
    for d in devs:
        by_owner.setdefault(d[1], []).append({"client_name": d[0], "label": d[2], "created_ts": d[3]})
    for u in users:
        u["devices"] = by_owner.get(u["username"], [])
    return users

@app.get("/api/users")
async def api_users(request: Request):
    current_user(request)
    return JSONResponse(await run_in_threadpool(_list_users))

def _approve_user(username: str) -> tuple:
    u = get_user(username)
    if not u:
        return False, "Пользователь не найден"
    conn = users_db()
    conn.execute("UPDATE users SET status='approved' WHERE username=?", (u["username"],))
    conn.commit()
    conn.close()
    link = f"{public_base_url()}{BASE_URL}/ulogin"
    send_mail(u["email"], f"{SERVER_LABEL}: доступ одобрен",
              f"Здравствуйте, {u['username']}!\n\n"
              f"Ваша заявка одобрена. Личный кабинет: {link}\n"
              f"В кабинете вы можете создавать конфиги для своих устройств и скачивать их.\n")
    return True, "OK"

def _reject_user(username: str) -> tuple:
    u = get_user(username)
    if not u:
        return False, "Пользователь не найден"
    conn = users_db()
    conn.execute("UPDATE users SET status='rejected' WHERE username=?", (u["username"],))
    conn.commit()
    conn.close()
    return True, "OK"

@app.post("/api/users/approve/{username}")
async def api_user_approve(request: Request, username: str):
    current_user(request)
    ok, msg = await run_in_threadpool(_approve_user, username)
    if not ok:
        return JSONResponse({"error": msg}, status_code=404)
    _notify(f"✅ <b>Заявка одобрена</b>\nПользователь: <code>{username}</code>")
    return {"ok": True}

@app.post("/api/users/reject/{username}")
async def api_user_reject(request: Request, username: str):
    current_user(request)
    ok, msg = await run_in_threadpool(_reject_user, username)
    if not ok:
        return JSONResponse({"error": msg}, status_code=404)
    _notify(f"❌ <b>Заявка отклонена</b>\nПользователь: <code>{username}</code>")
    return {"ok": True}

@app.post("/api/users/delete/{username}")
async def api_user_delete(request: Request, username: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", username)
    conn = users_db()
    devs = [r[0] for r in conn.execute("SELECT client_name FROM devices WHERE owner=?", (n,)).fetchall()]
    conn.close()
    for d in devs:
        await run_in_threadpool(delete_device, d)
    conn = users_db()
    conn.execute("DELETE FROM users WHERE username=?", (n,))
    conn.commit()
    conn.close()
    _notify(f"🗑 <b>Пользователь удалён</b>\nЛогин: <code>{n}</code>, конфигов удалено: {len(devs)}")
    return {"ok": True, "devices_removed": len(devs)}

@app.post("/api/users/add-device")
async def api_user_add_device(request: Request):
    current_user(request)
    body = await request.json()
    username = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("username", "")))
    u = await run_in_threadpool(get_user, username)
    if not u:
        return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
    ok, res = await run_in_threadpool(create_device, username, str(body.get("label", "")))
    if not ok:
        return JSONResponse({"error": res}, status_code=400)
    return {"ok": True, "client_name": res}

@app.post("/api/devices/delete/{client_name}")
async def api_device_delete(request: Request, client_name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", client_name)
    ok, err = await run_in_threadpool(delete_device, n)
    if not ok:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}

@app.post("/api/users/notify/{username}")
async def api_user_notify(request: Request, username: str):
    current_user(request)
    u = await run_in_threadpool(get_user, username)
    if not u:
        return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
    ok, msg = await run_in_threadpool(
        send_mail, u["email"], f"{SERVER_LABEL}: обновите конфиги VPN",
        f"Здравствуйте, {u['username']}!\n\n"
        f"Настройки сервера изменились. Зайдите в личный кабинет и заново "
        f"скачайте конфиги для своих устройств:\n{public_base_url()}{BASE_URL}/ulogin\n")
    if not ok:
        return JSONResponse({"error": msg}, status_code=500)
    return {"ok": True}

@app.post("/api/users/reset-pass/{username}")
async def api_user_reset_pass(request: Request, username: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", username)
    body = await request.json()
    new_pass = str(body.get("password", ""))
    if len(new_pass) < 8:
        return JSONResponse({"error": "Пароль должен быть не короче 8 символов"}, status_code=400)
    conn = users_db()
    cur = conn.execute("UPDATE users SET pass_hash=? WHERE username=?", (hash_password(new_pass), n))
    conn.commit()
    found = cur.rowcount > 0
    conn.close()
    if not found:
        return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
    return {"ok": True}

# --- настройки SMTP (вкладка «Пользователи») ---
@app.get("/api/mail/settings")
async def api_mail_get(request: Request):
    current_user(request)
    cfg = load_mail_conf()
    pwd = cfg.get("SMTP_PASS", "")
    return {"configured": bool(cfg.get("SMTP_HOST") and (cfg.get("SMTP_FROM") or cfg.get("SMTP_USER"))),
            "host": cfg.get("SMTP_HOST", ""), "port": cfg.get("SMTP_PORT", "465"),
            "user": cfg.get("SMTP_USER", ""), "from": cfg.get("SMTP_FROM", ""),
            "pass_masked": "********" if pwd else ""}

@app.post("/api/mail/settings")
async def api_mail_save(request: Request):
    current_user(request)
    body = await request.json()
    host = str(body.get("host", "")).strip()
    port = str(body.get("port", "465")).strip() or "465"
    user = str(body.get("user", "")).strip()
    pwd = str(body.get("password", "")).strip()
    from_addr = str(body.get("from", "")).strip()
    if host and not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
        return JSONResponse({"error": "Некорректный SMTP-хост"}, status_code=400)
    if not re.fullmatch(r"\d{1,5}", port):
        return JSONResponse({"error": "Некорректный порт"}, status_code=400)
    if not pwd:
        pwd = load_mail_conf().get("SMTP_PASS", "")
    try:
        with open(MAIL_CONF, "w", encoding="utf-8") as f:
            f.write(f'SMTP_HOST="{host}"\nSMTP_PORT="{port}"\nSMTP_USER="{user}"\n'
                    f'SMTP_PASS="{pwd}"\nSMTP_FROM="{from_addr}"\n')
        os.chmod(MAIL_CONF, 0o600)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True}

@app.post("/api/mail/test")
async def api_mail_test(request: Request):
    current_user(request)
    body = await request.json()
    to_addr = str(body.get("to", "")).strip()
    if not EMAIL_RE.fullmatch(to_addr):
        return JSONResponse({"error": "Некорректный email"}, status_code=400)
    ok, msg = await run_in_threadpool(
        send_mail, to_addr, f"{SERVER_LABEL}: тест почтовых уведомлений",
        "Это тестовое письмо от панели AmneziaWG. Если вы его видите — SMTP настроен верно.\n")
    if not ok:
        return JSONResponse({"error": msg}, status_code=500)
    return {"ok": True}

# --- точка входа для бота (одобрение/отклонение заявок кнопками) ---
def _bot_authorized(request: Request) -> bool:
    return request.headers.get("x-panel-token", "") == get_api_token()

@app.get("/api/bot/pending")
async def api_bot_pending(request: Request):
    if not _bot_authorized(request):
        raise HTTPException(403, "forbidden")
    def _q():
        conn = users_db()
        rows = conn.execute(
            "SELECT username, email, created_ts FROM users WHERE status='pending' ORDER BY created_ts").fetchall()
        conn.close()
        return [{"username": r[0], "email": r[1], "created_ts": r[2]} for r in rows]
    return JSONResponse(await run_in_threadpool(_q))

@app.post("/api/bot/user-decision")
async def api_bot_user_decision(request: Request):
    if request.headers.get("x-panel-token", "") != get_api_token():
        raise HTTPException(403, "forbidden")
    body = await request.json()
    username = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("username", "")))
    decision = str(body.get("decision", ""))
    u = await run_in_threadpool(get_user, username)
    if not u:
        return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
    if u["status"] != "pending":
        return JSONResponse({"error": f"Заявка уже обработана (статус: {u['status']})"}, status_code=400)
    if decision == "approve":
        await run_in_threadpool(_approve_user, username)
        return {"ok": True, "text": f"✅ {username} одобрен"}
    if decision == "reject":
        await run_in_threadpool(_reject_user, username)
        return {"ok": True, "text": f"❌ {username} отклонён"}
    return JSONResponse({"error": "unknown decision"}, status_code=400)

if __name__ == "__main__":
    kwargs = { "host": BIND_HOST, "port": 8000, "log_level": "info", "access_log": False, "server_header": False }
    if USE_SSL:
        kwargs["ssl_keyfile"] = "/etc/amnezia-panel-ssl/key.pem"
        kwargs["ssl_certfile"] = "/etc/amnezia-panel-ssl/cert.pem"
    uvicorn.run(app, **kwargs)