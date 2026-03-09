"""
WebSocket Chat Server — Phase 1 (broken) + Phase 2 (Valkey pub/sub bridge)
==========================================================================

Run two instances simultaneously:
    PORT=8001 python server.py
    PORT=8002 python server.py

Phase 1 proves the disconnect:
    Client A → Instance 8001 (in-memory manager)   ← isolated
    Client B → Instance 8002 (in-memory manager)   ← isolated
    A sends a message → B never receives it

Phase 2 fixes it with a Valkey pub/sub bridge:
    Every instance:
      • publishes outgoing messages to Valkey channel "chat:global"
      • subscribes to "chat:global" in a background coroutine
      • when a message arrives from Valkey, broadcasts it to ALL
        locally connected WebSocket clients

    Result: regardless of which instance a client connects to,
    every client on every instance sees every message.

Toggle with the PHASE env var:
    PHASE=1 PORT=8001 python server.py   ← broken (no Valkey)
    PHASE=2 PORT=8001 python server.py   ← fixed  (Valkey bridge)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Set

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ─────────────────────────────────────────────────────────────────────────────
# Config — driven by environment variables so the same file runs both instances
# ─────────────────────────────────────────────────────────────────────────────
PORT         = int(os.getenv("PORT",  "8001"))
PHASE        = int(os.getenv("PHASE", "2"))     # 1 = broken, 2 = fixed
VALKEY_URL   = os.getenv("VALKEY_URL", "redis://localhost:6379/1")
CHAT_CHANNEL = "chat:global"

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [:{PORT}] %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# In-process WebSocket connection manager
# Holds only the sockets connected to THIS instance.
# In Phase 1 this is all we have — and it is why instances are isolated.
# ─────────────────────────────────────────────────────────────────────────────
class LocalConnectionManager:
    def __init__(self):
        self.connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.add(ws)
        log.info("WS connected   | local_clients=%d", len(self.connections))

    def disconnect(self, ws: WebSocket):
        self.connections.discard(ws)
        log.info("WS disconnected | local_clients=%d", len(self.connections))

    async def broadcast_local(self, payload: dict):
        """Push to every socket connected to THIS instance only."""
        dead: Set[WebSocket] = set()
        for ws in self.connections:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        self.connections -= dead


manager = LocalConnectionManager()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Valkey pub/sub bridge
# ─────────────────────────────────────────────────────────────────────────────
valkey_pub: aioredis.Redis | None = None   # used for PUBLISH
valkey_sub: aioredis.Redis | None = None   # dedicated client for SUBSCRIBE


async def publish_to_valkey(payload: dict) -> None:
    """
    Publish a chat message to the shared Valkey channel.
    Every subscribed instance (including this one) will receive it
    and broadcast to their local WebSocket clients.
    """
    if valkey_pub is None:
        return
    await valkey_pub.publish(CHAT_CHANNEL, json.dumps(payload))
    log.info("→ Valkey PUBLISH : %s", payload)


async def valkey_subscriber_loop() -> None:
    """
    Background coroutine — runs for the lifetime of the process.

    Subscribes to CHAT_CHANNEL. When a message arrives (published by
    any instance), deserialises it and broadcasts to all local WebSocket
    clients on THIS instance.

    This is the bridge: it decouples message origin (any instance) from
    message delivery (every instance's local sockets).
    """
    global valkey_sub
    valkey_sub = aioredis.Redis.from_url(VALKEY_URL, decode_responses=True)
    pubsub = valkey_sub.pubsub()
    await pubsub.subscribe(CHAT_CHANNEL)
    log.info("Subscribed to Valkey channel '%s'", CHAT_CHANNEL)

    async for raw in pubsub.listen():
        if raw["type"] != "message":
            continue
        try:
            payload = json.loads(raw["data"])
            log.info("← Valkey RECEIVE : %s", payload)
            await manager.broadcast_local(payload)
        except (json.JSONDecodeError, Exception) as exc:
            log.warning("Bad message from Valkey: %s — %s", raw["data"], exc)


# ─────────────────────────────────────────────────────────────────────────────
# App lifespan
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global valkey_pub

    if PHASE == 2:
        # Separate client for publishing (pub/sub client cannot issue other cmds)
        valkey_pub = aioredis.Redis.from_url(VALKEY_URL, decode_responses=True)
        await valkey_pub.ping()
        log.info("Valkey publish client connected")

        # Start the subscriber in the background
        sub_task = asyncio.create_task(valkey_subscriber_loop())
        log.info("Phase 2 — Valkey pub/sub bridge ACTIVE on port %d", PORT)
    else:
        sub_task = None
        log.warning("Phase 1 — NO Valkey bridge. Instances are ISOLATED. "
                    "Clients on different instances cannot see each other.")

    yield

    if sub_task:
        sub_task.cancel()
        try:
            await sub_task
        except asyncio.CancelledError:
            pass
    if valkey_pub:
        await valkey_pub.aclose()
    log.info("Server on port %d shut down", PORT)


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title=f"WS Chat Server :{PORT} (Phase {PHASE})",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the chat client for manual browser testing."""
    with open("client.html") as f:
        return f.read()


@app.get("/health")
async def health():
    return {
        "port":             PORT,
        "phase":            PHASE,
        "local_clients":    len(manager.connections),
        "valkey_bridge":    PHASE == 2,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint
# ─────────────────────────────────────────────────────────────────────────────
@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    """
    Each client connects with a username in the URL path:
        ws://localhost:8001/ws/alice
        ws://localhost:8002/ws/bob

    Message flow — Phase 1 (broken):
        Client → this instance's manager.broadcast_local()
        Only clients on THIS instance see the message.

    Message flow — Phase 2 (fixed):
        Client → publish_to_valkey()
            → Valkey channel "chat:global"
                → valkey_subscriber_loop() on EVERY instance
                    → manager.broadcast_local() on EVERY instance
                        → every connected client everywhere
    """
    await manager.connect(websocket)

    # Announce join
    join_msg = {
        "type":      "system",
        "text":      f"{username} joined on :{PORT}",
        "sender":    "system",
        "origin_port": PORT,
        "ts":        int(time.time()),
    }

    if PHASE == 2:
        await publish_to_valkey(join_msg)
    else:
        await manager.broadcast_local(join_msg)

    try:
        while True:
            raw = await websocket.receive_text()

            chat_msg = {
                "type":        "chat",
                "text":        raw,
                "sender":      username,
                "origin_port": PORT,       # ← shows WHICH instance handled this msg
                "ts":          int(time.time()),
            }

            if PHASE == 2:
                # ── Phase 2: publish to Valkey, NOT directly to local sockets.
                # The subscriber loop will pick it up and broadcast locally.
                # This instance's own clients get it the same way as every other
                # instance — through Valkey. No special-casing needed.
                await publish_to_valkey(chat_msg)
            else:
                # ── Phase 1: broadcast only to this instance's sockets.
                # Clients on the other instance will never see this.
                await manager.broadcast_local(chat_msg)

    except WebSocketDisconnect:
        manager.disconnect(websocket)

        leave_msg = {
            "type":        "system",
            "text":        f"{username} left",
            "sender":      "system",
            "origin_port": PORT,
            "ts":          int(time.time()),
        }

        if PHASE == 2:
            await publish_to_valkey(leave_msg)
        else:
            await manager.broadcast_local(leave_msg)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point — reads PORT from env
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,        # reload=False when running two instances manually
        log_level="info",
    )