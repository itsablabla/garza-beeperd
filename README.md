# garza-beeperd

**One-line install. Resilient. Auto-updating. Mesh-aware.**

A lightweight daemon that connects to [Beeper Desktop API](https://developers.beeper.com/desktop-api) and pushes every message to your GARZA Comm Center in **under 2 seconds** — no polling, true event-driven.

## One-Line Install

```bash
curl -fsSL https://raw.githubusercontent.com/itsablabla/garza-beeperd/main/install.sh | bash
```

Works on **macOS** (Intel + Apple Silicon) and **Linux** (x86_64, arm64).

---

## What It Does

- Connects to Beeper Desktop WebSocket (`ws://localhost:23373/v1/ws`)
- Subscribes to **all chats** — Telegram, WhatsApp, iMessage, Slack, Signal, Discord, etc.
- Pushes every incoming message to GARZA Comm Center in real-time
- Registers in the **mesh** — all nodes heartbeat every 30s; dead nodes are routed around
- **Auto-updates** from GitHub every 24 hours — zero maintenance
- **Auto-restarts** via LaunchAgent (Mac) or systemd (Linux)
- Health API at `http://localhost:7373/status`

---

## Mesh Architecture

Run `garza-beeperd` on multiple machines for redundancy:

```
Mac (primary)          VPS (backup)           Mac Mini (backup)
  beeperd run    +      beeperd run    +        beeperd run
      ↓                     ↓                       ↓
  Beeper WS           Beeper Matrix            Beeper WS
      ↓                     ↓                       ↓
      └──────────── GARZA Mesh Coordinator ──────────┘
                            ↓
                    GARZA Comm Center
```

Each node:
- Heartbeats to the mesh coordinator every 30s
- Is marked **dead** if silent for 90s
- GARZA dashboard shows which nodes are active

---

## CLI Commands

```bash
beeperd setup       # Interactive setup wizard (first time)
beeperd run         # Start daemon in foreground
beeperd status      # Show live status
beeperd logs        # Tail logs
beeperd update      # Check for and apply updates now
beeperd install     # Install auto-start service only
beeperd stop        # Stop the daemon
beeperd uninstall   # Remove auto-start service
```

---

## Prerequisites

1. **Beeper Desktop** running on the same machine
2. **Beeper Desktop API** enabled:
   - Open Beeper → Settings → Developers
   - Toggle "Beeper Desktop API" ON
   - Click "+" next to "Approved connections" → create a token
3. **Python 3.8+** (installer handles this automatically)

---

## Config

Config is stored at `~/.garza/beeperd/config.json`:

```json
{
  "beeper_token": "your_token_here",
  "node_name": "Jaden-MacBook-Pro",
  "garza_ingest_url": "https://...",
  "garza_mesh_url": "https://...",
  "auto_update": true,
  "update_interval": 86400,
  "heartbeat_interval": 30,
  "vip_senders": ["jessica", "eric schuele", "kevin", "mom", "dad"]
}
```

---

## Logs

```bash
beeperd logs                          # Live tail
cat ~/.garza/beeperd/beeperd.log      # Full log
```

---

## Auto-Update

The daemon checks GitHub for new releases every 24 hours. When an update is found:
1. Downloads new `beeperd.py`
2. Backs up current version as `beeperd.py.bak`
3. Replaces and restarts via `os.execv` (zero downtime)

To disable: set `"auto_update": false` in config.

---

## Health API

```bash
curl http://localhost:7373/status
```

Returns:
```json
{
  "version": "1.0.0",
  "node_name": "Jaden-MacBook-Pro",
  "ws_connected": true,
  "messages_sent": 247,
  "ws_reconnects": 0,
  "last_message": {
    "from": "Jessica Garza",
    "platform": "Telegram",
    "time": "2026-03-05T02:31:00Z"
  },
  "started_at": "2026-03-04T20:00:00Z"
}
```

---

Built for GARZA OS by Jaden Garza.
