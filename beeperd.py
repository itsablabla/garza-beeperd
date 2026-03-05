#!/usr/bin/env python3
"""
garza-beeperd — GARZA Beeper Relay Daemon
==========================================
A resilient, auto-updating, Tailscale-aware mesh daemon that:
  • Connects to local Beeper Desktop API WebSocket (<2s latency)
  • Forwards every message to GARZA Comm Center in real-time
  • Auto-detects Tailscale IP and registers in mesh (Cloudflare KV)
  • Peers discover each other via KV — mesh survives node failures
  • Exposes health API on port 7373 (reachable over Tailscale)
  • Auto-updates from GitHub every 24 hours
  • Auto-restarts via LaunchAgent (Mac) or systemd (Linux)

Version: 1.1.0
GitHub:  https://github.com/itsablabla/garza-beeperd
"""

__version__ = "1.1.0"
GITHUB_REPO = "itsablabla/garza-beeperd"
GITHUB_RAW  = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/beeperd.py"

import os, sys, json, time, logging, threading, socket, hashlib, platform
import subprocess, signal, argparse
from pathlib import Path
from datetime import datetime, timezone

# ── Paths & Config ────────────────────────────────────────────────────────────
CONFIG_DIR   = Path.home() / ".garza" / "beeperd"
CONFIG_FILE  = CONFIG_DIR / "config.json"
LOG_FILE     = CONFIG_DIR / "beeperd.log"
PID_FILE     = CONFIG_DIR / "beeperd.pid"

BEEPER_WS    = "ws://localhost:23373/v1/ws"
BEEPER_HTTP  = "http://localhost:23373"
HEALTH_PORT  = 7373

DEFAULT_CONFIG = {
    "garza_ingest_url": "https://primary-production-f10f7.up.railway.app/webhook/comm-center-ingest",
    "beeper_token":     "",
    "node_id":          "",
    "node_name":        socket.gethostname(),
    "auto_update":      True,
    "update_interval":  86400,
    "heartbeat_interval": 30,
    "vip_senders": [
        "jessica", "jessica garza", "jess",
        "eric", "eric schuele",
        "kevin", "kevin crawford",
        "mom", "dad"
    ]
}

PLATFORM_MAP = {
    "telegram":  "Telegram",
    "whatsapp":  "WhatsApp",
    "imessage":  "iMessage",
    "slack":     "Slack",
    "signal":    "Signal",
    "instagram": "Instagram",
    "twitter":   "Twitter/X",
    "linkedin":  "LinkedIn",
    "discord":   "Discord",
    "messenger": "Messenger",
    "sms":       "SMS",
    "matrix":    "Matrix",
}

# Cloudflare KV — baked in for zero-config mesh
_CF_TOKEN   = "dg5t0vjIl3_sLbzN3QK34A30qHoaQVyrAcDDqAF6"
_CF_ACCOUNT = "14adde85f76060c6edef6f3239d36e6a"
_CF_NS_ID   = "aaec3be87d9b4746b4acc8e68fbac8d7"
_CF_KV_BASE = f"https://api.cloudflare.com/client/v4/accounts/{_CF_ACCOUNT}/storage/kv/namespaces/{_CF_NS_ID}"

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
    "tailscale_ip":  None,
    "mesh_peers":    [],
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
    if not cfg.get("node_id"):
        cfg["node_id"] = hashlib.sha256(
            f"{socket.gethostname()}{platform.node()}".encode()
        ).hexdigest()[:12]
        save_config()
    return cfg


def save_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Tailscale IP detection ────────────────────────────────────────────────────
def get_tailscale_ip() -> str | None:
    """
    Detect the node's Tailscale IP (100.x.x.x) using multiple methods:
    1. tailscale ip -4 command
    2. Parse tailscale status --json
    3. Scan network interfaces for 100.x.x.x addresses
    Returns None if Tailscale is not running.
    """
    # Method 1: tailscale CLI
    for cmd in [["tailscale", "ip", "-4"], ["/usr/local/bin/tailscale", "ip", "-4"],
                ["/usr/bin/tailscale", "ip", "-4"]]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                ip = result.stdout.strip()
                if ip.startswith("100."):
                    log.info(f"🔷 Tailscale IP: {ip} (via CLI)")
                    return ip
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Method 2: tailscale status --json
    for cmd in [["tailscale", "status", "--json"], ["/usr/local/bin/tailscale", "status", "--json"]]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                self_node = data.get("Self", {})
                ips = self_node.get("TailscaleIPs", [])
                for ip in ips:
                    if ip.startswith("100."):
                        log.info(f"🔷 Tailscale IP: {ip} (via status)")
                        return ip
        except Exception:
            pass

    # Method 3: Scan network interfaces for 100.x.x.x
    try:
        import socket as sock
        for iface_info in sock.getaddrinfo(sock.gethostname(), None):
            ip = iface_info[4][0]
            if ip.startswith("100."):
                log.info(f"🔷 Tailscale IP: {ip} (via interfaces)")
                return ip
    except Exception:
        pass

    # Method 4: Check /proc/net/if_inet6 or ip addr on Linux
    if platform.system() == "Linux":
        try:
            result = subprocess.run(["ip", "addr", "show"], capture_output=True, text=True, timeout=5)
            import re
            matches = re.findall(r'inet (100\.\d+\.\d+\.\d+)', result.stdout)
            if matches:
                log.info(f"🔷 Tailscale IP: {matches[0]} (via ip addr)")
                return matches[0]
        except Exception:
            pass

    log.debug("Tailscale not detected on this node")
    return None


def get_tailscale_peers() -> list[dict]:
    """Get all online Tailscale peers from tailscale status."""
    peers = []
    for cmd in [["tailscale", "status", "--json"], ["/usr/local/bin/tailscale", "status", "--json"]]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for peer_key, peer in data.get("Peer", {}).items():
                    ips = peer.get("TailscaleIPs", [])
                    v4_ip = next((ip for ip in ips if "." in ip), None)
                    if v4_ip and peer.get("Online", False):
                        peers.append({
                            "hostname": peer.get("HostName", peer_key[:8]),
                            "ip":       v4_ip,
                            "os":       peer.get("OS", "unknown"),
                            "online":   peer.get("Online", False),
                        })
            return peers
        except Exception:
            pass
    return peers


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

        if msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)
        if len(seen_ids) > 10000:
            seen_ids.difference_update(list(seen_ids)[:5000])

        if entry.get("isOutgoing") or entry.get("outgoing"):
            continue

        sender = (
            entry.get("senderName") or entry.get("sender") or
            entry.get("from") or entry.get("author") or ""
        )

        body = entry.get("text") or entry.get("body") or entry.get("content") or ""
        if isinstance(body, dict):
            body = body.get("text") or body.get("body") or str(body)

        if not body or not body.strip():
            attachments = entry.get("attachments") or []
            if attachments:
                body = f"[{len(attachments)} attachment(s)]"
            else:
                continue

        chat_id   = entry.get("chatID") or entry.get("roomID") or event.get("chatID") or ""
        chat_name = entry.get("chatName") or entry.get("roomName") or chat_id
        platform_name = detect_platform(chat_id)

        ts = entry.get("timestamp") or entry.get("ts") or event.get("ts", 0)
        if isinstance(ts, (int, float)) and ts > 1e10:
            ts = ts / 1000
        msg_time = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts else datetime.now(timezone.utc).isoformat()
        )

        payload = {
            "source":       "beeper",
            "platform":     platform_name,
            "messageId":    f"beeper_{msg_id}",
            "from":         sender or "Unknown",
            "subject":      f"[{platform_name}] {chat_name}" if chat_name else f"[{platform_name}] Message",
            "body":         body.strip()[:2000],
            "timestamp":    msg_time,
            "chatId":       chat_id,
            "isVip":        is_vip(sender),
            "node_id":      cfg.get("node_id", ""),
            "node_name":    cfg.get("node_name", socket.gethostname()),
            "tailscale_ip": state.get("tailscale_ip"),
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
    try:
        import websocket
    except ImportError:
        log.error("websocket-client not installed")
        sys.exit(1)

    delay = 5
    max_delay = 120

    while True:
        token = cfg.get("beeper_token", "")
        if not token:
            log.error("No Beeper token. Run: beeperd setup")
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
    """
    Write heartbeat to Cloudflare KV every 30s.
    Includes Tailscale IP so peers can reach this node directly.
    Also reads all other mesh nodes from KV to populate state['mesh_peers'].
    """
    try:
        import requests
    except ImportError:
        return

    # Detect Tailscale IP once at startup
    ts_ip = get_tailscale_ip()
    state["tailscale_ip"] = ts_ip
    if ts_ip:
        log.info(f"🔷 Registered on Tailscale mesh: {ts_ip}")
    else:
        log.info("ℹ️  Tailscale not detected — mesh will use public ingest only")

    while True:
        try:
            node_id = cfg.get("node_id", "unknown")
            payload = {
                "node_id":       node_id,
                "node_name":     cfg.get("node_name", socket.gethostname()),
                "version":       __version__,
                "platform":      platform.system(),
                "ws_connected":  state["ws_connected"],
                "messages_sent": state["messages_sent"],
                "started_at":    state["started_at"],
                "last_message":  state["last_message"],
                "tailscale_ip":  ts_ip,
                "timestamp":     datetime.now(timezone.utc).isoformat(),
            }

            # Write this node's heartbeat to KV (TTL=90s — if silent for 90s, auto-expires)
            kv_url = f"{_CF_KV_BASE}/values/beeper_mesh_{node_id}"
            resp = requests.put(
                kv_url,
                data=json.dumps(payload),
                headers={
                    "Authorization": f"Bearer {_CF_TOKEN}",
                    "Content-Type":  "application/json",
                },
                params={"expiration_ttl": 90},
                timeout=8
            )
            state["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            if resp.status_code == 200:
                log.debug(f"💓 Heartbeat OK (node={node_id}, ts={ts_ip or 'none'})")
            else:
                log.debug(f"Heartbeat KV: {resp.status_code}")

            # Read all mesh peers from KV
            _refresh_mesh_peers(requests)

        except Exception as e:
            log.debug(f"Heartbeat error: {e}")

        time.sleep(cfg.get("heartbeat_interval", 30))


def _refresh_mesh_peers(requests_mod):
    """Read all beeper_mesh_* keys from KV and update state['mesh_peers']."""
    try:
        list_url = f"{_CF_KV_BASE}/keys"
        resp = requests_mod.get(
            list_url,
            headers={"Authorization": f"Bearer {_CF_TOKEN}"},
            params={"prefix": "beeper_mesh_"},
            timeout=8
        )
        if resp.status_code != 200:
            return

        keys = [k["name"] for k in resp.json().get("result", [])]
        peers = []
        my_node_id = cfg.get("node_id", "")

        for key in keys:
            node_id = key.replace("beeper_mesh_", "")
            if node_id == my_node_id:
                continue  # Skip self
            try:
                val_resp = requests_mod.get(
                    f"{_CF_KV_BASE}/values/{key}",
                    headers={"Authorization": f"Bearer {_CF_TOKEN}"},
                    timeout=5
                )
                if val_resp.status_code == 200:
                    peer_data = val_resp.json()
                    peers.append({
                        "node_id":      node_id,
                        "node_name":    peer_data.get("node_name", node_id),
                        "tailscale_ip": peer_data.get("tailscale_ip"),
                        "version":      peer_data.get("version", "?"),
                        "ws_connected": peer_data.get("ws_connected", False),
                        "platform":     peer_data.get("platform", "?"),
                        "last_seen":    peer_data.get("timestamp"),
                        "messages_sent": peer_data.get("messages_sent", 0),
                    })
            except Exception:
                pass

        state["mesh_peers"] = peers
        if peers:
            peer_names = [f"{p['node_name']} ({p['tailscale_ip'] or 'no-ts'})" for p in peers]
            log.debug(f"🕸️  Mesh peers: {', '.join(peer_names)}")

    except Exception as e:
        log.debug(f"Mesh peer refresh error: {e}")


# ── Auto-updater ──────────────────────────────────────────────────────────────
def check_for_update() -> bool:
    try:
        import requests
        vresp = requests.get(
            f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/VERSION",
            timeout=10
        )
        if vresp.status_code != 200:
            return False
        latest = vresp.text.strip()
        if not latest:
            return False

        def version_tuple(v):
            return tuple(int(x) for x in v.split("."))

        if version_tuple(latest) <= version_tuple(__version__):
            log.info(f"✅ Already up to date (v{__version__})")
            return False

        log.info(f"🆕 Update available: v{__version__} → v{latest}")
        new_script = requests.get(GITHUB_RAW, timeout=30).text
        script_path = Path(__file__).resolve()
        backup_path = script_path.with_suffix(".py.bak")
        script_path.rename(backup_path)
        try:
            script_path.write_text(new_script)
            log.info(f"✅ Updated to v{latest} — restarting...")
            os.execv(sys.executable, [sys.executable, str(script_path)] + sys.argv[1:])
        except Exception as e:
            log.error(f"Update failed: {e} — restoring backup")
            backup_path.rename(script_path)
            return False

    except Exception as e:
        log.debug(f"Update check error: {e}")
        return False


def run_auto_updater():
    time.sleep(300)  # Wait 5 min before first check
    while True:
        if cfg.get("auto_update", True):
            check_for_update()
        time.sleep(cfg.get("update_interval", 86400))


# ── Health API ────────────────────────────────────────────────────────────────
def run_health_api():
    """
    HTTP health API on port 7373.
    Binds to 0.0.0.0 so it's reachable over Tailscale from other mesh nodes.
    """
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path in ("/", "/health", "/status"):
                body = json.dumps({
                    **state,
                    "config": {
                        "node_id":      cfg.get("node_id"),
                        "node_name":    cfg.get("node_name"),
                        "auto_update":  cfg.get("auto_update"),
                        "tailscale_ip": state.get("tailscale_ip"),
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
            elif self.path == "/mesh":
                # Return mesh peers for dashboard
                body = json.dumps({
                    "node_id":      cfg.get("node_id"),
                    "node_name":    cfg.get("node_name"),
                    "tailscale_ip": state.get("tailscale_ip"),
                    "peers":        state.get("mesh_peers", []),
                }, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    try:
        server = HTTPServer(("0.0.0.0", HEALTH_PORT), Handler)
        log.info(f"🌐 Health API: http://localhost:{HEALTH_PORT}/status")
        ts_ip = state.get("tailscale_ip")
        if ts_ip:
            log.info(f"🔷 Reachable over Tailscale: http://{ts_ip}:{HEALTH_PORT}/status")
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

    # Detect Tailscale before setup
    ts_ip = get_tailscale_ip()
    if ts_ip:
        print(f"🔷 Tailscale detected: {ts_ip}")
        print("   This node will be registered in the mesh with its Tailscale IP.")
    else:
        print("ℹ️  Tailscale not detected. Install Tailscale for mesh networking.")
        print("   https://tailscale.com/download")
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
    print("\nTesting connection to Beeper Desktop API...")
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
    default_name = socket.gethostname()
    cfg["node_name"] = input(f"\nSTEP 3 — Node name (default: {default_name}):\n> ").strip() or default_name
    save_config()

    print()
    print("✅ Config saved to", CONFIG_FILE)
    print()

    install = input("Install auto-start service? (Y/n): ").strip().lower()
    if install != "n":
        install_service()

    print()
    print("🚀 Starting garza-beeperd...")
    print(f"   Logs:   {LOG_FILE}")
    print(f"   Health: http://localhost:{HEALTH_PORT}/status")
    if ts_ip:
        print(f"   Mesh:   http://{ts_ip}:{HEALTH_PORT}/mesh")
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
        log.warning(f"Auto-start not supported on {system}.")


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
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
"""
    plist_path.write_text(plist)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True)

    if result.returncode == 0:
        log.info(f"✅ LaunchAgent installed: {plist_path}")
        log.info("🚀 Auto-starts on login, restarts if it crashes")
    else:
        log.warning(f"LaunchAgent load warning: {result.stderr}")
        log.info(f"Manual load: launchctl load -w {plist_path}")


def _install_systemd(python_path: str, script_path: str):
    service = f"""[Unit]
Description=GARZA Beeper Relay Daemon
After=network.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={python_path} {script_path} run
Restart=always
RestartSec=5
StandardOutput=append:{LOG_FILE}
StandardError=append:{LOG_FILE}
Environment=HOME={Path.home()}
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
"""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / "garza-beeperd.service"
    service_path.write_text(service)

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "garza-beeperd"],
        capture_output=True, text=True
    )

    if result.returncode == 0:
        log.info("✅ systemd service installed and started")
    else:
        log.warning(f"systemd install warning: {result.stderr}")
        log.info("Manual: systemctl --user enable --now garza-beeperd")


# ── Main daemon runner ────────────────────────────────────────────────────────
def run_daemon():
    log.info(f"🚀 garza-beeperd v{__version__} starting on {socket.gethostname()}")
    log.info(f"   Node ID:  {cfg.get('node_id')}")
    log.info(f"   Ingest:   {cfg.get('garza_ingest_url')}")
    log.info(f"   Health:   http://localhost:{HEALTH_PORT}/status")

    PID_FILE.write_text(str(os.getpid()))

    threads = [
        threading.Thread(target=run_health_api,   daemon=True, name="health"),
        threading.Thread(target=run_heartbeat,    daemon=True, name="heartbeat"),
        threading.Thread(target=run_auto_updater, daemon=True, name="updater"),
    ]
    for t in threads:
        t.start()

    run_watcher()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="beeperd",
        description="GARZA Beeper Relay Daemon — resilient, Tailscale-aware, auto-updating"
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run",       help="Start the daemon (foreground)")
    sub.add_parser("setup",     help="Interactive setup wizard")
    sub.add_parser("install",   help="Install auto-start service only")
    sub.add_parser("update",    help="Check for and apply updates now")
    sub.add_parser("status",    help="Show daemon status")
    sub.add_parser("logs",      help="Tail daemon logs")
    sub.add_parser("stop",      help="Stop the daemon")
    sub.add_parser("mesh",      help="Show mesh peer status")
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
            ts_ip = s.get("config", {}).get("tailscale_ip") or s.get("tailscale_ip")
            print(f"\n{'─'*54}")
            print(f"  garza-beeperd v{s.get('version','?')}")
            print(f"  Node:       {s.get('node_name','?')} ({s.get('config',{}).get('node_id','?')})")
            print(f"  Platform:   {s.get('platform','?')}")
            print(f"  Tailscale:  {ts_ip or '⚠️  not detected'}")
            print(f"  WS:         {'🟢 Connected' if s.get('ws_connected') else '🔴 Disconnected'}")
            print(f"  Messages:   {s.get('messages_sent',0)} sent")
            print(f"  Reconnects: {s.get('ws_reconnects',0)}")
            peers = s.get("mesh_peers", [])
            if peers:
                print(f"  Mesh peers: {len(peers)}")
                for p in peers:
                    status = "🟢" if p.get("ws_connected") else "🔴"
                    print(f"    {status} {p['node_name']} ({p.get('tailscale_ip','no-ts')}) v{p.get('version','?')}")
            last = s.get("last_message")
            if last:
                print(f"  Last msg:   [{last['platform']}] {last['from']} @ {last['time']}")
            print(f"  Started:    {s.get('started_at','?')}")
            print(f"{'─'*54}\n")
        except Exception:
            print("❌ Daemon not running (or health API unreachable)")
            print("   Start with: beeperd run")

    elif args.cmd == "logs":
        if LOG_FILE.exists():
            subprocess.run(["tail", "-f", str(LOG_FILE)])
        else:
            print("No log file found")

    elif args.cmd == "mesh":
        try:
            import requests
            r = requests.get(f"http://localhost:{HEALTH_PORT}/mesh", timeout=3)
            data = r.json()
            ts_ip = data.get("tailscale_ip")
            peers = data.get("peers", [])
            print(f"\n{'─'*54}")
            print(f"  🕸️  GARZA Beeper Mesh")
            print(f"  This node: {data.get('node_name')} ({ts_ip or 'no tailscale'})")
            if peers:
                print(f"  Peers ({len(peers)}):")
                for p in peers:
                    status = "🟢" if p.get("ws_connected") else "🔴"
                    ts = p.get("tailscale_ip", "no-ts")
                    print(f"    {status} {p['node_name']} — {ts} — v{p.get('version','?')} — {p.get('messages_sent',0)} msgs")
            else:
                print("  No other mesh nodes detected.")
                print("  Install garza-beeperd on another machine to join the mesh.")
            print(f"{'─'*54}\n")
        except Exception:
            print("❌ Daemon not running")

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
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", "garza-beeperd"],
                capture_output=True
            )
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
