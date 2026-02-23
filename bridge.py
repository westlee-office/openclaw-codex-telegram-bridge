#!/usr/bin/env python3
"""Telegram bridge that routes messages through OpenClaw and Codex CLI."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


ALLOWED_MODES = {"auto", "claw", "codex", "hybrid"}
DEFAULT_LOW_QUALITY_PATTERNS = [
    "i don't know",
    "i do not know",
    "cannot help",
    "can't help",
    "unable to",
    "not sure",
    "모르겠",
    "잘 모르",
    "죄송",
]


@dataclass
class ModelResult:
    ok: bool
    text: str
    error: str = ""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Config:
    telegram_token: str
    allowed_chat_ids: Optional[set]
    router_mode: str
    show_route_debug: bool
    telegram_log_enabled: bool
    telegram_log_path: str
    history_turns: int
    telegram_poll_timeout: int
    telegram_retry_sleep: float
    openclaw_bin: str
    openclaw_agent: str
    openclaw_timeout: int
    openclaw_local: bool
    openclaw_thinking: Optional[str]
    openclaw_extra_args: List[str]
    openclaw_min_chars: int
    low_quality_patterns: List[str]
    codex_bin: str
    codex_timeout: int
    codex_workdir: str
    codex_model: Optional[str]
    codex_profile: Optional[str]
    codex_extra_args: List[str]

    @staticmethod
    def from_env(require_telegram_token: bool = True) -> "Config":
        token = (
            must_env("TELEGRAM_BOT_TOKEN")
            if require_telegram_token
            else os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        )
        mode = os.getenv("ROUTER_MODE", "auto").strip().lower()
        if mode not in ALLOWED_MODES:
            raise ValueError(
                f"ROUTER_MODE must be one of {sorted(ALLOWED_MODES)}, got: {mode!r}"
            )

        allowed = parse_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))
        patterns_raw = os.getenv("OPENCLAW_LOW_QUALITY_PATTERNS", "").strip()
        patterns = (
            [p.strip().lower() for p in patterns_raw.split("|") if p.strip()]
            if patterns_raw
            else DEFAULT_LOW_QUALITY_PATTERNS
        )

        return Config(
            telegram_token=token,
            allowed_chat_ids=allowed,
            router_mode=mode,
            show_route_debug=env_bool("SHOW_ROUTE_DEBUG", False),
            telegram_log_enabled=env_bool("TELEGRAM_LOG_ENABLED", True),
            telegram_log_path=(
                os.getenv("TELEGRAM_LOG_PATH", "logs/telegram_history.jsonl").strip()
                or "logs/telegram_history.jsonl"
            ),
            history_turns=max(0, env_int("HISTORY_TURNS", 4)),
            telegram_poll_timeout=max(1, env_int("TELEGRAM_POLL_TIMEOUT", 25)),
            telegram_retry_sleep=max(0.2, env_float("TELEGRAM_RETRY_SLEEP", 2.0)),
            openclaw_bin=os.getenv("OPENCLAW_BIN", "openclaw").strip(),
            openclaw_agent=os.getenv("OPENCLAW_AGENT", "main").strip(),
            openclaw_timeout=max(10, env_int("OPENCLAW_TIMEOUT", 120)),
            openclaw_local=env_bool("OPENCLAW_LOCAL", False),
            openclaw_thinking=os.getenv("OPENCLAW_THINKING", "").strip() or None,
            openclaw_extra_args=shlex.split(
                os.getenv("OPENCLAW_EXTRA_ARGS", "").strip()
            ),
            openclaw_min_chars=max(20, env_int("OPENCLAW_MIN_CHARS", 80)),
            low_quality_patterns=patterns,
            codex_bin=os.getenv("CODEX_BIN", "codex").strip(),
            codex_timeout=max(20, env_int("CODEX_TIMEOUT", 180)),
            codex_workdir=os.getenv("CODEX_WORKDIR", os.getcwd()).strip(),
            codex_model=os.getenv("CODEX_MODEL", "").strip() or None,
            codex_profile=os.getenv("CODEX_PROFILE", "").strip() or None,
            codex_extra_args=shlex.split(os.getenv("CODEX_EXTRA_ARGS", "").strip()),
        )


def must_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw.strip())


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if value and ((value[0] == value[-1]) and value[0] in {"'", '"'}):
                value = value[1:-1]
            os.environ[key] = value


def parse_chat_ids(raw: str) -> Optional[set]:
    raw = raw.strip()
    if not raw:
        return None
    chat_ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        chat_ids.add(int(part))
    return chat_ids


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_jsonl_atomic(path: str, payload: dict) -> None:
    if not path:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def log_telegram_event(enabled: bool, path: str, event_type: str, payload: dict) -> None:
    if not enabled:
        return
    record = {"ts_utc": utc_now_iso(), "event": event_type}
    record.update(payload)
    try:
        append_jsonl_atomic(path, record)
    except Exception as exc:
        print(f"[warn] telegram logging failed: {exc}", file=sys.stderr)


def tg_request(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"telegram api error ({method}): {body}")
    return body["result"]


def split_for_telegram(text: str, limit: int = 3900) -> List[str]:
    text = (text or "").strip()
    if not text:
        return ["(empty response)"]
    chunks: List[str] = []
    remain = text
    while len(remain) > limit:
        cut = remain.rfind("\n", 0, limit)
        if cut < int(limit * 0.6):
            cut = limit
        chunks.append(remain[:cut].rstrip())
        remain = remain[cut:].lstrip()
    chunks.append(remain)
    return chunks


def send_message(token: str, chat_id: int, text: str) -> None:
    for chunk in split_for_telegram(text):
        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": chunk})


def send_typing(token: str, chat_id: int) -> None:
    try:
        tg_request(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


def send_and_log(
    cfg: Config,
    chat_id: int,
    text: str,
    *,
    kind: str,
    extra: Optional[dict] = None,
) -> None:
    send_message(cfg.telegram_token, chat_id, text)
    payload = {"kind": kind, "chat_id": chat_id, "text": text}
    if extra:
        payload.update(extra)
    log_telegram_event(cfg.telegram_log_enabled, cfg.telegram_log_path, "outgoing", payload)


def extract_openclaw_text(stdout: str) -> str:
    clean = (stdout or "").strip()
    if not clean:
        return ""
    try:
        payload = json.loads(clean)
    except json.JSONDecodeError:
        return clean

    result = payload.get("result") if isinstance(payload, dict) else None
    payloads = result.get("payloads", []) if isinstance(result, dict) else []
    if isinstance(payloads, list):
        for item in payloads:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return clean


def run_command(cmd: Sequence[str], timeout_s: int) -> Tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_s,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_openclaw(prompt: str, cfg: Config) -> ModelResult:
    cmd = [cfg.openclaw_bin, "agent"]
    if cfg.openclaw_local:
        cmd.append("--local")
    cmd.extend(
        [
            "--agent",
            cfg.openclaw_agent,
            "--message",
            prompt,
            "--json",
            "--timeout",
            str(cfg.openclaw_timeout),
        ]
    )
    if cfg.openclaw_thinking:
        cmd.extend(["--thinking", cfg.openclaw_thinking])
    cmd.extend(cfg.openclaw_extra_args)

    try:
        code, stdout, stderr = run_command(cmd, cfg.openclaw_timeout + 15)
    except subprocess.TimeoutExpired as exc:
        return ModelResult(
            ok=False,
            text="",
            error=f"openclaw timeout after {cfg.openclaw_timeout + 15}s",
            exit_code=124,
            stdout=(exc.stdout or ""),
            stderr=(exc.stderr or ""),
        )
    except FileNotFoundError:
        return ModelResult(ok=False, text="", error=f"openclaw binary not found: {cmd[0]}")

    text = extract_openclaw_text(stdout)
    if code != 0:
        error = (stderr or stdout or "").strip() or f"openclaw exited with code {code}"
        return ModelResult(
            ok=False, text=text, error=error, exit_code=code, stdout=stdout, stderr=stderr
        )

    if not text:
        return ModelResult(
            ok=False,
            text="",
            error="openclaw returned empty output",
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
        )

    return ModelResult(ok=True, text=text, exit_code=code, stdout=stdout, stderr=stderr)


def build_codex_prompt(
    user_text: str,
    history: Sequence[Tuple[str, str]],
    claw_draft: Optional[str],
) -> str:
    lines = [
        "You are answering a Telegram user.",
        "Reply in the user's language unless they ask otherwise.",
        "Be concise, concrete, and helpful.",
    ]
    if history:
        lines.append("")
        lines.append("Recent context:")
        for role, msg in history:
            tag = "User" if role == "user" else "Assistant"
            lines.append(f"{tag}: {msg}")
    if claw_draft:
        lines.append("")
        lines.append("Draft answer from OpenClaw:")
        lines.append(claw_draft)
        lines.append("Improve it or replace it if needed.")
    lines.append("")
    lines.append("User message:")
    lines.append(user_text)
    lines.append("")
    lines.append("Final answer:")
    return "\n".join(lines)


def run_codex(user_text: str, history: Sequence[Tuple[str, str]], claw_draft: Optional[str], cfg: Config) -> ModelResult:
    prompt = build_codex_prompt(user_text=user_text, history=history, claw_draft=claw_draft)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="codex-bridge-", suffix=".txt", delete=False) as tmp:
            temp_path = tmp.name

        cmd = [
            cfg.codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "-C",
            cfg.codex_workdir,
            "-o",
            temp_path,
        ]
        if cfg.codex_model:
            cmd.extend(["-m", cfg.codex_model])
        if cfg.codex_profile:
            cmd.extend(["-p", cfg.codex_profile])
        cmd.extend(cfg.codex_extra_args)
        cmd.append(prompt)

        try:
            code, stdout, stderr = run_command(cmd, cfg.codex_timeout)
        except subprocess.TimeoutExpired as exc:
            return ModelResult(
                ok=False,
                text="",
                error=f"codex timeout after {cfg.codex_timeout}s",
                exit_code=124,
                stdout=(exc.stdout or ""),
                stderr=(exc.stderr or ""),
            )
        except FileNotFoundError:
            return ModelResult(ok=False, text="", error=f"codex binary not found: {cfg.codex_bin}")

        with open(temp_path, "r", encoding="utf-8") as f:
            answer = f.read().strip()

        if code != 0:
            detail = (stderr or stdout or "").strip() or f"codex exited with code {code}"
            return ModelResult(
                ok=False, text=answer, error=detail, exit_code=code, stdout=stdout, stderr=stderr
            )
        if not answer:
            return ModelResult(
                ok=False,
                text="",
                error="codex returned empty output",
                exit_code=code,
                stdout=stdout,
                stderr=stderr,
            )
        return ModelResult(ok=True, text=answer, exit_code=code, stdout=stdout, stderr=stderr)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def should_fallback_to_codex(user_text: str, claw_text: str, cfg: Config) -> Tuple[bool, str]:
    msg_len = len(user_text.strip())
    ans_len = len(claw_text.strip())
    if ans_len == 0:
        return True, "empty"
    if msg_len > 20 and ans_len < cfg.openclaw_min_chars:
        return True, "too_short"
    lower = claw_text.lower()
    for pattern in cfg.low_quality_patterns:
        if pattern and pattern in lower:
            return True, f"pattern:{pattern}"
    return False, "ok"


def route_answer(
    user_text: str,
    history: Sequence[Tuple[str, str]],
    mode: str,
    cfg: Config,
) -> Tuple[str, str]:
    if mode == "codex":
        codex = run_codex(user_text, history, claw_draft=None, cfg=cfg)
        if codex.ok:
            return codex.text, "codex"
        return f"[codex error]\n{codex.error}", "codex_error"

    claw = run_openclaw(user_text, cfg)
    if mode == "claw":
        if claw.ok:
            return claw.text, "claw"
        return f"[openclaw error]\n{claw.error}", "claw_error"

    if mode == "hybrid":
        codex = run_codex(
            user_text=user_text,
            history=history,
            claw_draft=claw.text if claw.text else None,
            cfg=cfg,
        )
        if codex.ok:
            return codex.text, "claw+codex"
        if claw.ok:
            return claw.text, "claw_fallback_after_codex_error"
        return f"[openclaw error]\n{claw.error}\n\n[codex error]\n{codex.error}", "both_error"

    # auto
    if claw.ok:
        fallback, reason = should_fallback_to_codex(user_text, claw.text, cfg)
        if not fallback:
            return claw.text, "claw_auto_ok"
        codex = run_codex(
            user_text=user_text,
            history=history,
            claw_draft=claw.text,
            cfg=cfg,
        )
        if codex.ok:
            return codex.text, f"codex_fallback({reason})"
        return claw.text, f"claw_used_codex_failed({reason})"

    codex = run_codex(user_text=user_text, history=history, claw_draft=None, cfg=cfg)
    if codex.ok:
        return codex.text, "codex_fallback(openclaw_error)"
    return f"[openclaw error]\n{claw.error}\n\n[codex error]\n{codex.error}", "both_error"


def command_help(default_mode: str) -> str:
    return (
        "OpenClaw + Codex Telegram bridge is running.\n"
        "Commands:\n"
        "/mode auto|claw|codex|hybrid\n"
        "/status\n"
        "/help\n"
        f"Default mode: {default_mode}"
    )


def main() -> int:
    load_dotenv(os.getenv("DOTENV_PATH", ".env"))

    try:
        cfg = Config.from_env()
    except Exception as exc:
        print(f"[fatal] invalid config: {exc}", file=sys.stderr)
        return 2

    print(
        f"[boot] bridge started mode={cfg.router_mode} codex_workdir={cfg.codex_workdir}",
        file=sys.stderr,
    )
    if cfg.allowed_chat_ids is not None:
        print(f"[boot] allowed chats: {sorted(cfg.allowed_chat_ids)}", file=sys.stderr)

    running = True

    def stop_handler(signum: int, _frame) -> None:
        nonlocal running
        running = False
        print(f"[signal] received {signum}, stopping", file=sys.stderr)

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    offset: Optional[int] = None
    history: Dict[int, List[Tuple[str, str]]] = {}
    mode_by_chat: Dict[int, str] = {}

    while running:
        payload = {
            "timeout": cfg.telegram_poll_timeout,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            payload["offset"] = offset

        try:
            updates = tg_request(cfg.telegram_token, "getUpdates", payload)
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            print(f"[warn] polling failed: {exc}", file=sys.stderr)
            time.sleep(cfg.telegram_retry_sleep)
            continue
        except Exception as exc:
            print(f"[warn] unexpected polling error: {exc}", file=sys.stderr)
            time.sleep(cfg.telegram_retry_sleep)
            continue

        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1

            is_edited = "edited_message" in update
            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                continue
            text = message.get("text")
            chat = message.get("chat")
            if not isinstance(text, str) or not isinstance(chat, dict):
                continue
            chat_id = chat.get("id")
            if not isinstance(chat_id, int):
                continue
            text = text.strip()
            if not text:
                continue

            sender = message.get("from") if isinstance(message.get("from"), dict) else {}
            msg_id = message.get("message_id")
            incoming_meta = {
                "chat_id": chat_id,
                "chat_type": chat.get("type"),
                "chat_title": chat.get("title"),
                "chat_username": chat.get("username"),
                "from_id": sender.get("id"),
                "from_username": sender.get("username"),
                "from_first_name": sender.get("first_name"),
                "message_id": msg_id,
                "telegram_date": message.get("date"),
                "update_id": update_id,
                "is_edited": is_edited,
                "text": text,
            }
            log_telegram_event(
                cfg.telegram_log_enabled,
                cfg.telegram_log_path,
                "incoming",
                incoming_meta,
            )

            if cfg.allowed_chat_ids is not None and chat_id not in cfg.allowed_chat_ids:
                log_telegram_event(
                    cfg.telegram_log_enabled,
                    cfg.telegram_log_path,
                    "incoming_ignored",
                    {"chat_id": chat_id, "update_id": update_id, "reason": "chat_not_allowed"},
                )
                continue

            if text.startswith("/start") or text.startswith("/help"):
                help_text = command_help(cfg.router_mode)
                send_and_log(
                    cfg,
                    chat_id,
                    help_text,
                    kind="command_reply",
                    extra={"command": "help", "update_id": update_id, "reply_to_message_id": msg_id},
                )
                continue

            if text.startswith("/mode"):
                parts = text.split()
                if len(parts) == 2 and parts[1].lower() in ALLOWED_MODES:
                    mode_by_chat[chat_id] = parts[1].lower()
                    send_and_log(
                        cfg,
                        chat_id,
                        f"Mode set to: {mode_by_chat[chat_id]}",
                        kind="command_reply",
                        extra={"command": "mode", "update_id": update_id, "reply_to_message_id": msg_id},
                    )
                else:
                    send_and_log(
                        cfg,
                        chat_id,
                        "Usage: /mode auto|claw|codex|hybrid",
                        kind="command_reply",
                        extra={"command": "mode", "update_id": update_id, "reply_to_message_id": msg_id},
                    )
                continue

            if text.startswith("/status"):
                active_mode = mode_by_chat.get(chat_id, cfg.router_mode)
                status_text = (
                    f"mode={active_mode}\n"
                    f"history_turns={cfg.history_turns}\n"
                    f"openclaw_agent={cfg.openclaw_agent}\n"
                    f"codex_workdir={cfg.codex_workdir}"
                )
                send_and_log(
                    cfg,
                    chat_id,
                    status_text,
                    kind="command_reply",
                    extra={"command": "status", "update_id": update_id, "reply_to_message_id": msg_id},
                )
                continue

            send_typing(cfg.telegram_token, chat_id)
            active_mode = mode_by_chat.get(chat_id, cfg.router_mode)
            prior = history.get(chat_id, [])
            context = prior[-(cfg.history_turns * 2) :] if cfg.history_turns > 0 else []

            clean_answer, route_tag = route_answer(
                user_text=text, history=context, mode=active_mode, cfg=cfg
            )
            answer = clean_answer
            if cfg.show_route_debug:
                answer = f"{answer}\n\n[route: {route_tag}]"

            history.setdefault(chat_id, [])
            history[chat_id].append(("user", text))
            history[chat_id].append(("assistant", clean_answer))
            max_items = cfg.history_turns * 2
            if max_items > 0 and len(history[chat_id]) > max_items:
                history[chat_id] = history[chat_id][-max_items:]

            send_and_log(
                cfg,
                chat_id,
                answer,
                kind="assistant_reply",
                extra={
                    "mode": active_mode,
                    "route_tag": route_tag,
                    "update_id": update_id,
                    "reply_to_message_id": msg_id,
                },
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
