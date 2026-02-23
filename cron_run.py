#!/usr/bin/env python3
"""Run one routed turn (codex/claw/auto/hybrid) for cron automation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Tuple

from bridge import ALLOWED_MODES, Config, load_dotenv, route_answer, send_message


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one OpenClaw/Codex routed turn and optionally send to Telegram."
    )
    parser.add_argument("--dotenv", default=".env", help="Path to .env file")
    parser.add_argument("--mode", choices=sorted(ALLOWED_MODES), help="Routing mode override")
    parser.add_argument("--prompt", help="Prompt text")
    parser.add_argument("--prompt-file", help="Read prompt from file")
    parser.add_argument("--stdin", action="store_true", help="Read prompt from stdin")
    parser.add_argument("--chat-id", type=int, help="Telegram chat id to send output")
    parser.add_argument(
        "--history-file",
        default=".cron_history.json",
        help="Path to JSON history file (used for better Codex context)",
    )
    parser.add_argument(
        "--history-key",
        default="",
        help="Custom history bucket key (default: chat:<id> or mode:<mode>)",
    )
    parser.add_argument("--no-history", action="store_true", help="Disable history read/write")
    parser.add_argument("--show-route", action="store_true", help="Append route tag to output")
    return parser.parse_args()


def resolve_prompt(args: argparse.Namespace) -> str:
    selected = [bool(args.prompt), bool(args.prompt_file), bool(args.stdin)]
    if sum(selected) > 1:
        raise ValueError("Use only one input source: --prompt, --prompt-file, or --stdin")

    if args.prompt:
        return args.prompt.strip()
    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    if args.stdin or not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise ValueError("Prompt is required. Pass --prompt, --prompt-file, or --stdin.")


def load_history(path: str) -> Dict[str, List[Tuple[str, str]]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    normalized: Dict[str, List[Tuple[str, str]]] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        rows: List[Tuple[str, str]] = []
        for item in value:
            if (
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], str)
            ):
                rows.append((item[0], item[1]))
        normalized[key] = rows
    return normalized


def save_history(path: str, data: Dict[str, List[Tuple[str, str]]]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    serializable = {k: [[r, m] for r, m in v] for k, v in data.items()}
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def build_history_key(args: argparse.Namespace, mode: str) -> str:
    if args.history_key:
        return args.history_key
    if args.chat_id is not None:
        return f"chat:{args.chat_id}"
    return f"mode:{mode}"


def main() -> int:
    args = parse_args()
    load_dotenv(args.dotenv)

    prompt = resolve_prompt(args)
    if not prompt:
        raise ValueError("Prompt cannot be empty")

    require_token = args.chat_id is not None
    cfg = Config.from_env(require_telegram_token=require_token)
    mode = (args.mode or cfg.router_mode).lower()
    if mode not in ALLOWED_MODES:
        raise ValueError(f"Invalid mode: {mode}")

    history_store: Dict[str, List[Tuple[str, str]]] = {}
    history_key = build_history_key(args, mode)
    context: List[Tuple[str, str]] = []
    if not args.no_history:
        history_store = load_history(args.history_file)
        max_items = cfg.history_turns * 2
        rows = history_store.get(history_key, [])
        context = rows[-max_items:] if max_items > 0 else []

    answer, route_tag = route_answer(user_text=prompt, history=context, mode=mode, cfg=cfg)
    final = f"{answer}\n\n[route: {route_tag}]" if args.show_route else answer

    if not args.no_history:
        rows = history_store.get(history_key, [])
        rows.append(("user", prompt))
        rows.append(("assistant", answer))
        max_items = cfg.history_turns * 2
        if max_items > 0 and len(rows) > max_items:
            rows = rows[-max_items:]
        history_store[history_key] = rows
        save_history(args.history_file, history_store)

    if args.chat_id is not None:
        if not cfg.telegram_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required when --chat-id is used")
        send_message(cfg.telegram_token, args.chat_id, final)

    print(final)

    if route_tag in {"claw_error", "codex_error", "both_error"}:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        raise SystemExit(2)
