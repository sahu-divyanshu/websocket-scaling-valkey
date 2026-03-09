# WebSocket Scaling with Valkey (Redis) Pub/Sub

This project demonstrates how to scale WebSockets across multiple server instances using a message bus.

## The Problem (Phase 1)
WebSocket connections are stateful and tied to a specific server. Without a bridge, users on `Server A` cannot communicate with users on `Server B`.

## The Solution (Phase 2)
We implement a **Valkey Pub/Sub Bridge**. Every server instance:
1.  **Publishes** outgoing chat messages to a global Valkey channel.
2.  **Subscribes** to that same channel in a background task.
3.  **Broadcasts** messages received from Valkey to its own local clients.



## Setup & Execution
1. Install dependencies: `pip install -r requirements.txt`
2. Run Instance 1: `PHASE=2 PORT=8001 python server.py`
3. Run Instance 2: `PHASE=2 PORT=8002 python server.py`
4. Connect via browser at `http://localhost:8001` and `http://localhost:8002`.
