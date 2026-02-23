#!/usr/bin/env python3
"""List recent Telegram chat IDs from bot updates."""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from bridge import load_dotenv


def main() -> int:
    load_dotenv(".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("[fatal] TELEGRAM_BOT_TOKEN is required", file=sys.stderr)
        return 2

    url = f"https://api.telegram.org/bot{token}/getUpdates?limit=100"
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    if not body.get("ok"):
        print(f"[fatal] telegram api error: {body}", file=sys.stderr)
        return 2

    updates = body.get("result", [])
    if not updates:
        print("No updates yet. Send a message to your bot first, then run again.")
        return 0

    seen = {}
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if not isinstance(cid, int):
            continue
        seen[cid] = {
            "type": chat.get("type"),
            "title": chat.get("title"),
            "username": chat.get("username"),
            "first_name": chat.get("first_name"),
            "last_name": chat.get("last_name"),
        }

    if not seen:
        print("No chat IDs found in updates.")
        return 0

    print("Found chat IDs:")
    for cid, meta in seen.items():
        label = meta.get("title") or meta.get("username") or meta.get("first_name") or "unknown"
        print(f"- {cid} ({meta.get('type')}, {label})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
