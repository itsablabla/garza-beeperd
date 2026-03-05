#!/usr/bin/env python3
"""
garza-beeperd — GARZA Beeper Relay Daemon
==========================================
A resilient, auto-updating, mesh-aware daemon that:
  • Connects to local Beeper Desktop API WebSocket
  • Forwards every message to GARZA Comm Center in real-time (<2s)
  • Registers itself in the mesh (Cloudflare KV) for failover
  • Exposes health API on port 7373
  • Auto-updates from GitHub every 24 hours
  • Auto-restarts via LaunchAgent (Mac) or systemd (Linux)

Version: 1.0.0
GitHub:  https://github.com/itsablabla/garza-beeperd
"""

__version__ = "1.0.0"
GITHUB_REPO = "itsablabla/garza-beeperd"
GITHUB_RAW   = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/beeperd.py"

import os, sys, json, time, logging, threading, socket, hashlib, platform
import subprocess, signal, argparse
from pathlib import Path
from datetime import datetime, timezone

# ── Paths & Config ────────────────────────────────────────────────────────────
CONFIG_DIR  = Path.home() / ".garza" / "beeperd"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE    = CONFIG_DIR / "beeperd.log"
PID_FILE    = CONFIG_DIR / "beeperd.pid"
VERSION_FILE= CONFIG_DIR / "version"

BEEPER_WS   = "ws://localhost:23373/v1/ws"
BEEPER_HTTP = "http://localhost:23373"
HEALTH_PORT = 7373

# Defaults (overridden by config)
DEFAULT_CONFIG = {
    "garza_ingest_url": "https://primary-production-f10f7.up.railway.app/webhook/comm-center-ingest",
    "garza_mesh_url":   "https://primary-production-f10f7.up.railway.app/webhook/beeper-mesh",
    "beeper_token":     "",
    "node_id":          "",
    "node_name":        socket.gethostname(),
    "auto_update":      True,
    "update_interval":  86400,   # 24 hours
    "heartbeat_interval": 30,    # seconds
    "vip_senders": [
        "jessica", "jessica garza", "jess",
        "eric", "eric schuele",
        "kevin", "kevin crawford",
        "mom", "dad"
    ]
}

PLATFORM_MAP = {
    "telegram":   "Telegram",
    "whatsapp":   "WhatsApp",
    "imessage":   "iMessage",
    "slack":      "Slack",
    "signal":     "Signal",
    "instagram":  "Instagram",
    "twitter":    "Twitter/X",
    "linkedin":   "LinkedIn",
    "discord":    "Discord",
    "messenger":  "Messenger",
    "sms":        "SMS",
    "matrix":     "Matrix",
}

# ── Logging ───────────────────────────────────────────────────────────────────
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("beeperd")

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "started_at":    datetime.now(timezone.utc).isoformat(),
    "messages_sent": 0,
    "last_message":  None,
    "ws_connected":  False,
    "ws_reconnects": 0,
    "last_heartbeat": None,
    "version":       __version__,
    "node_name":     socket.gethostname(),
    "platform":      platform.system(),
}

cfg = {}
seen_ids = set()


# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    global cfg
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
            cfg = {**DEFAULT_CONFIG, **stored}
        except Exception as e:
            log.warning(f"Config load error: {e}, using defaults")
            cfg = DEFAULT_CONFIG.copy()
    else:
        cfg = DEFAULT_CONFIG.copy()
    # Generate node_id if missing
    if not cfg.get("node_id"):
        cfg["node_id"] = hashlib.sha256(
            f"{socket.gethostname()}{platform.node()}{os.getpid()}".encode()
        ).hexdigest()[:12]
        save_config()
    return cfg


def save_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── VIP check ─────────────────────────────────────────────────────────────────
def is_vip(sender: str) -> bool:
    if not sender:
        return False
    s = sender.lower()
    return any(v in s for v in cfg.get("vip_senders", []))


# ── Platform detection ────────────────────────────────────────────────────────
def detect_platform(chat_id: str) -> str:
    if not chat_id:
        return "Beeper"
    chat_lower = chat_id.lower()
    for key, name in PLATFORM_MAP.items():
        if key in chat_lower:
            return name
    return "Beeper"


# ── Message processing ────────────────────────────────────────────────────────
def process_message_event(event: dict):
    """Extract message from Beeper WebSocket event and push to GARZA."""
    try:
        import requests
    except ImportError:
        log.error("requests not installed")
        return

    entries = event.get("messages") or event.get("entries") or []
    if not entries:
        entry = event.get("message") or event.get("entry")
        if entry:
            entries = [entry]

    if not entries:
        return

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        msg_id = (
            entry.get("id") or entry.get("messageID") or
            entry.get("entryID") or entry.get("eventID") or
            str(time.time())
        )

        # Dedup
        if msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)
        if len(seen_ids) > 10000:
            # Trim oldest 5000
            seen_ids.difference_update(list(seen_ids)[:5000])

        # Skip outgoing
        if entry.get("isOutgoing") or entry.get("outgoing"):
            continue

        # Get sender
        sender = (
            entry.get("senderName") or entry.get("sender") or
            entry.get("from") or entry.get("author") or ""
        )

        # Get body
        body = entry.get("text") or entry.get("body") or entry.get("content") or ""
        if isinstance(body, dict):
            body = body.get("text") or body.get("body") or str(body)

        # Handle attachments
        if not body or not body.strip():
            attachments = entry.get("attachments") or []
            if attachments:
                body = f"[{len(attachments)} attachment(s)]"
            else:
                continue

        # Chat info
        chat_id   = entry.get("chatID") or entry.get("roomID") or event.get("chatID") or ""
        chat_name = entry.get("chatName") or entry.get("roomName") or chat_id
        platform_name = detect_platform(chat_id)

        # Timestamp
        ts = entry.get("timestamp") or entry.get("ts") or event.get("ts", 0)
        if isinstance(ts, (int, float)) and ts > 1e10:
            ts = ts / 1000
        msg_time = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts else datetime.now(timezone.utc).isoformat()
        )

        payload = {
            "source":    "beeper",
            "platform":  platform_name,
            "messageId": f"beeper_{msg_id}",
            "from":      sender or "Unknown",
            "subject":   f"[{platform_name}] {chat_name}" if chat_name else f"[{platform_name}] Message",
            "body":      body.strip()[:2000],
            "timestamp": msg_time,
            "chatId":    chat_id,
            "isVip":     is_vip(sender),
            "node_id":   cfg.get("node_id", ""),
            "node_name": cfg.get("node_name", socket.gethostname()),
        }

        log.info(f"📨 [{platform_name}] {sender}: {body[:80]}")

        try:
            resp = requests.post(
                cfg["garza_ingest_url"],
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json"}
            )
            if resp.status_code in (200, 201, 202):
                state["messages_sent"] += 1
                state["last_message"] = {
                    "from": sender,
                    "platform": platform_name,
                    "time": msg_time
                }
                log.info(f"✅ Pushed [{platform_name}] {sender}")
            else:
                log.warning(f"⚠️  GARZA returned {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            log.error(f"❌ Push failed: {e}")


# ── WebSocket watcher ─────────────────────────────────────────────────────────
def run_watcher():
    """Main WebSocket watcher loop with exponential backoff reconnect."""
    try:
        import websocket
    except ImportError:
        log.error("websocket-client not installed. Run: pip3 install websocket-client")
        sys.exit(1)

    delay = 5
    max_delay = 120

    while True:
        token = cfg.get("beeper_token", "")
        if not token:
            log.error("No Beeper token configured. Run: beeperd setup")
            time.sleep(30)
            load_config()
            continue

        try:
            log.info(f"🔌 Connecting to Beeper at {BEEPER_WS} ...")
            state["ws_connected"] = False

            ws = websocket.WebSocketApp(
                BEEPER_WS,
                header={"Authorization": f"Bearer {token}"},
                on_open=on_ws_open,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)

        except KeyboardInterrupt:
            log.info("👋 Shutting down")
            break
        except Exception as e:
            log.error(f"❌ Watcher error: {e}")

        state["ws_reconnects"] += 1
        log.info(f"🔄 Reconnecting in {delay}s (attempt #{state['ws_reconnects']})...")
        time.sleep(delay)
        delay = min(delay * 1.5, max_delay)


def on_ws_open(ws):
    state["ws_connected"] = True
    log.info("✅ Connected to Beeper Desktop API")
    ws.send(json.dumps({
        "type":      "subscriptions.set",
        "requestID": "garza-mesh",
        "chatIDs":   ["*"]
    }))
    log.info("📡 Subscribed to all chats")


def on_ws_message(ws, message):
    try:
        event = json.loads(message)
        etype = event.get("type", "")

        if etype == "ready":
            log.info(f"🟢 Beeper ready (v{event.get('version','?')})")
        elif etype == "subscriptions.updated":
            log.info(f"📡 Subscriptions: {len(event.get('chatIDs', []))} chats")
        elif etype == "message.upserted":
            threading.Thread(
                target=process_message_event,
                args=(event,),
                daemon=True
            ).start()
        elif etype == "error":
            log.error(f"Beeper error: {event.get('message','?')}")
    except Exception as e:
        log.error(f"Message parse error: {e}")


def on_ws_error(ws, error):
    state["ws_connected"] = False
    log.error(f"❌ WebSocket error: {error}")


def on_ws_close(ws, code, msg):
    state["ws_connected"] = False
    log.warning(f"🔌 WebSocket closed: {code} {msg}")


# ── Heartbeat / Mesh registration ─────────────────────────────────────────────
def run_heartbeat():
    """Send heartbeat to GARZA mesh coordinator every 30s."""
    try:
        import requests
    except ImportError:
        return

    while True:
        try:
            payload = {
                "node_id":       cfg.get("node_id"),
                "node_name":     cfg.get("node_name", socket.gethostname()),
                "version":       __version__,
                "platform":      platform.system(),
                "ws_connected":  state["ws_connected"],
                "messages_sent": state["messages_sent"],
                "started_at":    state["started_at"],
                "last_message":  state["last_message"],
                "timestamp":     datetime.now(timezone.utc).isoformat(),
            }
            resp = requests.post(
                cfg.get("garza_mesh_url", DEFAULT_CONFIG["garza_mesh_url"]),
                json=payload,
                timeout=8
            )
            state["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            if resp.status_code not in (200, 201, 202):
                log.debug(f"Heartbeat: {resp.status_code}")
        except Exception as e:
            log.debug(f"Heartbeat error: {e}")

        time.sleep(cfg.get("heartbeat_interval", 30))


# ── Auto-updater ──────────────────────────────────────────────────────────────
def check_for_update() -> bool:
    """Check GitHub for a newer version and self-update if available."""
    try:
        import requests
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github.v3+json"}
        )
        if resp.status_code != 200:
            # Fall back to raw version file
            vresp = requests.get(
                f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/VERSION",
                timeout=10
            )
            if vresp.status_code != 200:
                return False
            latest = vresp.text.strip()
        else:
            latest = resp.json().get("tag_name", "").lstrip("v")

        if not latest:
            return False

        def version_tuple(v):
            return tuple(int(x) for x in v.split("."))

        if version_tuple(latest) <= version_tuple(__version__):
            log.info(f"✅ Already up to date (v{__version__})")
            return False

        log.info(f"🆕 Update available: v{__version__} → v{latest}")

        # Download new version
        new_script = requests.get(GITHUB_RAW, timeout=30).text
        script_path = Path(__file__).resolve()
        backup_path = script_path.with_suffix(".py.bak")

        # Backup current
        script_path.rename(backup_path)
        try:
            script_path.write_text(new_script)
            log.info(f"✅ Updated to v{latest} — restarting...")
            # Restart self
            os.execv(sys.executable, [sys.executable, str(script_path)] + sys.argv[1:])
        except Exception as e:
            log.error(f"Update failed: {e} — restoring backup")
            backup_path.rename(script_path)
            return False

    except Exception as e:
        log.debug(f"Update check error: {e}")
        return False


def run_auto_updater():
    """Check for updates every 24 hours."""
    # Wait 5 minutes before first check
    time.sleep(300)
    while True:
        if cfg.get("auto_update", True):
            check_for_update()
        time.sleep(cfg.get("update_interval", 86400))


# ── Health API ────────────────────────────────────────────────────────────────
def run_health_api():
    """Simple HTTP health API on port 7373."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # Suppress access logs

        def do_GET(self):
            if self.path in ("/", "/health", "/status"):
                body = json.dumps({
                    **state,
                    "config": {
                        "node_id":   cfg.get("node_id"),
                        "node_name": cfg.get("node_name"),
                        "auto_update": cfg.get("auto_update"),
                    }
                }, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/ping":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"pong")
            else:
                self.send_response(404)
                self.end_headers()

    try:
        server = HTTPServer(("0.0.0.0", HEALTH_PORT), Handler)
        log.info(f"🌐 Health API listening on http://0.0.0.0:{HEALTH_PORT}")
        server.serve_forever()
    except OSError as e:
        log.warning(f"Health API could not start on port {HEALTH_PORT}: {e}")


# ── Setup wizard ──────────────────────────────────────────────────────────────
def setup_wizard():
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║       garza-beeperd — Setup Wizard                  ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()
    print("This daemon connects to Beeper Desktop and pushes every")
    print("message to your GARZA Comm Center in under 2 seconds.")
    print()
    print("STEP 1 — Enable Beeper Desktop API:")
    print("  Open Beeper → Settings → Developers")
    print("  Toggle 'Beeper Desktop API' ON")
    print("  Click '+' next to 'Approved connections' → create token")
    print()

    token = input("STEP 2 — Paste your Beeper Desktop API token:\n> ").strip()
    if not token:
        print("❌ No token. Exiting.")
        sys.exit(1)

    # Test token
    print("\nTesting connection...")
    try:
        import requests
        r = requests.get(
            f"{BEEPER_HTTP}/v1/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5
        )
        if r.status_code == 200:
            info = r.json()
            print(f"✅ Connected! Beeper v{info.get('version','?')}, user: {info.get('userID','?')}")
        else:
            print(f"⚠️  HTTP {r.status_code} — token may be wrong, but continuing...")
    except Exception as e:
        print(f"⚠️  Could not reach Beeper Desktop API: {e}")
        print("   Make sure Beeper is running and Desktop API is enabled.")
        cont = input("   Continue anyway? (y/N): ").strip().lower()
        if cont != "y":
            sys.exit(1)

    load_config()
    cfg["beeper_token"] = token
    cfg["node_name"] = input(f"\nSTEP 3 — Node name (default: {socket.gethostname()}):\n> ").strip() or socket.gethostname()
    save_config()

    print()
    print("✅ Config saved to", CONFIG_FILE)
    print()

    # Install auto-start
    install = input("Install auto-start service? (Y/n): ").strip().lower()
    if install != "n":
        install_service()

    print()
    print("🚀 Starting garza-beeperd...")
    print(f"   Logs: {LOG_FILE}")
    print(f"   Health: http://localhost:{HEALTH_PORT}/status")
    print()
    run_daemon()


# ── Service installer ─────────────────────────────────────────────────────────
def install_service():
    script_path = str(Path(__file__).resolve())
    python_path = sys.executable
    system = platform.system()

    if system == "Darwin":
        _install_launchagent(python_path, script_path)
    elif system == "Linux":
        _install_systemd(python_path, script_path)
    else:
        log.warning(f"Auto-start not supported on {system}. Run manually: python3 {script_path} run")


def _install_launchagent(python_path: str, script_path: str):
    plist_dir  = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / "com.garza.beeperd.plist"
    plist_dir.mkdir(parents=True, exist_ok=True)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.garza.beeperd</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>{LOG_FILE}</string>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>WorkingDirectory</key>
    <string>{Path.home()}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist)

    # Unload first if already loaded
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True)

    if result.returncode == 0:
        log.info(f"✅ LaunchAgent installed: {plist_path}")
        log.info("🚀 Will auto-start on login and restart if it crashes")
    else:
        log.warning(f"LaunchAgent load warning: {result.stderr}")
        log.info(f"Manual load: launchctl load -w {plist_path}")


def _install_systemd(python_path: str, script_path: str):
    service = f"""[Unit]
Description=GARZA Beeper Relay Daemon
After=network.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python_path} {script_path} run
Restart=always
RestartSec=5
StandardOutput=append:{LOG_FILE}
StandardError=append:{LOG_FILE}
Environment=HOME={Path.home()}

[Install]
WantedBy=default.target
"""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / "garza-beeperd.service"
    service_path.write_text(service)

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    result = subprocess.run(["systemctl", "--user", "enable", "--now", "garza-beeperd"], capture_output=True, text=True)

    if result.returncode == 0:
        log.info(f"✅ systemd service installed and started")
        log.info("   systemctl --user status garza-beeperd")
    else:
        log.warning(f"systemd install warning: {result.stderr}")
        log.info(f"Manual: systemctl --user enable --now garza-beeperd")


# ── Main daemon runner ────────────────────────────────────────────────────────
def run_daemon():
    """Start all daemon threads."""
    log.info(f"🚀 garza-beeperd v{__version__} starting on {socket.gethostname()}")
    log.info(f"   Node ID: {cfg.get('node_id')}")
    log.info(f"   Ingest:  {cfg.get('garza_ingest_url')}")
    log.info(f"   Health:  http://localhost:{HEALTH_PORT}/status")

    # Write PID
    PID_FILE.write_text(str(os.getpid()))

    threads = [
        threading.Thread(target=run_health_api,    daemon=True, name="health"),
        threading.Thread(target=run_heartbeat,     daemon=True, name="heartbeat"),
        threading.Thread(target=run_auto_updater,  daemon=True, name="updater"),
    ]
    for t in threads:
        t.start()

    # Watcher runs in main thread (blocking)
    run_watcher()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="beeperd",
        description="GARZA Beeper Relay Daemon — resilient, auto-updating, mesh-aware"
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run",     help="Start the daemon (foreground)")
    sub.add_parser("setup",   help="Interactive setup wizard")
    sub.add_parser("install", help="Install auto-start service only")
    sub.add_parser("update",  help="Check for and apply updates now")
    sub.add_parser("status",  help="Show daemon status")
    sub.add_parser("logs",    help="Tail daemon logs")
    sub.add_parser("stop",    help="Stop the daemon")
    sub.add_parser("uninstall", help="Remove auto-start service")

    args = parser.parse_args()
    load_config()

    if args.cmd == "setup":
        setup_wizard()

    elif args.cmd == "run":
        if not cfg.get("beeper_token"):
            print("❌ No token configured. Run: beeperd setup")
            sys.exit(1)
        run_daemon()

    elif args.cmd == "install":
        install_service()

    elif args.cmd == "update":
        if check_for_update():
            print("✅ Updated and restarted")
        else:
            print(f"✅ Already on latest version (v{__version__})")

    elif args.cmd == "status":
        try:
            import requests
            r = requests.get(f"http://localhost:{HEALTH_PORT}/status", timeout=3)
            s = r.json()
            print(f"\n{'─'*50}")
            print(f"  garza-beeperd v{s.get('version','?')}")
            print(f"  Node:      {s.get('node_name','?')} ({s.get('config',{}).get('node_id','?')})")
            print(f"  Platform:  {s.get('platform','?')}")
            print(f"  WS:        {'🟢 Connected' if s.get('ws_connected') else '🔴 Disconnected'}")
            print(f"  Messages:  {s.get('messages_sent',0)} sent")
            print(f"  Reconnects:{s.get('ws_reconnects',0)}")
            last = s.get("last_message")
            if last:
                print(f"  Last msg:  [{last['platform']}] {last['from']} @ {last['time']}")
            print(f"  Started:   {s.get('started_at','?')}")
            print(f"{'─'*50}\n")
        except Exception:
            print("❌ Daemon not running (or health API unreachable)")
            print(f"   Start with: beeperd run")

    elif args.cmd == "logs":
        if LOG_FILE.exists():
            subprocess.run(["tail", "-f", str(LOG_FILE)])
        else:
            print("No log file found")

    elif args.cmd == "stop":
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"✅ Sent SIGTERM to PID {pid}")
            except ProcessLookupError:
                print("Process not found (already stopped?)")
            PID_FILE.unlink(missing_ok=True)
        else:
            print("No PID file found")

    elif args.cmd == "uninstall":
        system = platform.system()
        if system == "Darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / "com.garza.beeperd.plist"
            if plist.exists():
                subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
                plist.unlink()
                print("✅ LaunchAgent removed")
            else:
                print("No LaunchAgent found")
        elif system == "Linux":
            subprocess.run(["systemctl", "--user", "disable", "--now", "garza-beeperd"], capture_output=True)
            svc = Path.home() / ".config" / "systemd" / "user" / "garza-beeperd.service"
            if svc.exists():
                svc.unlink()
                subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
                print("✅ systemd service removed")

    else:
        # No command — if configured, run; else setup
        if cfg.get("beeper_token"):
            run_daemon()
        else:
            setup_wizard()


if __name__ == "__main__":
    main()
